"""Page image -> text through OpenAI-compatible vision endpoints: a primary and a backup, each
with its share of the pages, each standing in for the other when it is down or refuses a page."""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from dataclasses import dataclass

import requests

log = logging.getLogger("ocr")

# Bump when the prompt changes: every page records the version it was read under.
PROMPT_VERSION = "v1"
PROMPT = (
    "อ่านข้อความทั้งหมดในภาพนี้ ทั้งส่วนที่พิมพ์และส่วนที่เขียนด้วยลายมือ\n"
    "- ตอบกลับเป็น Markdown เท่านั้น ไม่ต้องอธิบายหรือใส่ข้อความอื่น\n"
    "- รักษาโครงสร้างเดิมไว้ เช่น หัวข้อ ย่อหน้า ตาราง และลำดับเลข\n"
    "- คงตัวเลข วันที่ ชื่อบุคคล และเลขที่หนังสือไว้ตามที่ปรากฏ ห้ามเดาหรือแก้ไข\n"
    "- ส่วนที่อ่านไม่ออกให้ใส่ [อ่านไม่ออก] แทน ห้ามแต่งข้อความขึ้นเอง\n"
    "- ไม่ต้องบรรยายรูปภาพ ถ้าหน้านี้ไม่มีข้อความเลยให้ตอบว่า [หน้าว่าง]"
)
BLANK = "[หน้าว่าง]"
ILLEGIBLE = "[อ่านไม่ออก]"

THAI = re.compile("[฀-๿]")
# A model that cannot read a page sometimes repeats one phrase until it runs out of tokens.
LOOP = re.compile(r"(.{12,}?)\1{3,}", re.S)
REFUSALS = ("i'm sorry", "i cannot", "unable to", "ขออภัย", "ไม่สามารถอ่าน")
REFUSAL_MAX = 400  # characters: a longer reading with one of those words is the page's own text
_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"\A```[a-zA-Z]*\n(.*?)\n?```\s*\Z", re.S)
_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)  # olmOCR's page metadata

# A model with nothing it can read on a page sometimes writes out its instructions instead.
_ECHO = ("อ่านข้อความทั้งหมดในภาพนี้", "ตอบกลับเป็น Markdown", "รักษาโครงสร้างเดิมไว้",
         "ห้ามเดาหรือแก้ไข", "ห้ามแต่งข้อความขึ้นเอง", "ไม่ต้องบรรยายรูปภาพ",
         "ทั้งส่วนที่พิมพ์และส่วนที่เขียนด้วยลายมือ")
# The same phrase three or more times in a row: a watermark tiled over the page, a map's repeated
# labels, or a model stuck on one line. One copy is what the page says. Tables repeat cells for
# real (ผ่าน | ไม่ผ่าน under every week) and runs without letters are layout: both stay.
_REPEATS = re.compile(r"(?<!\S)(\S.{9,200}?)(?:\s*\1){2,}", re.S)  # short labels (กรรมการ ×5) stay
_TAG = re.compile(r"<[^>]*>")
_TABLE = re.compile(r"\||<t[dhr]\b|</t[dhr]>")
# A reading cut off at the token limit usually ends with the model stuck writing one block of lines
# over and over: a dotted line, an empty table row, a counter (884, 885, 886 ...), a list item under
# a new number each time (๘๑. รายงาน..., ๘๒. รายงาน...). That many copies of a block at the cut-off
# end are the model stuck, not the page; blocks of up to RUNAWAY_BLOCK lines (a table row) are looked
# for. Copies may differ only in numbers that stay the same or count up by one: rows of a real
# table of figures differ by more.
RUNAWAY_COPIES = 6
RUNAWAY_BLOCK = 12
_NUMBER = re.compile(r"[0-9๐-๙]+")
_THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
# A reading can be watched as it comes in (OcrClient(watch=True)): once the model has written the
# same block of lines, or the same phrase, STUCK_COPIES times in a row, it is stopped there instead
# of running on to the token limit, minutes of a GPU other services share. Real pages repeat less
# (a form's dotted lines, a blank table's rows); a part of a page stopped this way is read again
# in smaller pieces, each with fewer copies. Checked every STUCK_CHECK_EVERY characters.
STUCK_COPIES = 12
STUCK_CHECK_EVERY = 300
CUT_OFF = ("length", "stuck")  # finish reasons of a reading that stopped before the page's end
STUCK_PHRASE = 150  # longest phrase looked for, in characters


def stuck(text: str) -> bool:
    """The text so far ends with the model writing one thing over and over: a block of lines (as
    cut_runaway sees them) or a phrase with words in it (a dotted line is not one)."""
    tail = text[-4000:]
    if cut_runaway(tail, STUCK_COPIES)[1]:
        return True
    flat = " ".join(tail.split())
    for period in range(4, STUCK_PHRASE + 1):
        span = period * (STUCK_COPIES - 1)
        if len(flat) < span + period:
            break
        # The end repeats with this period: every character equals the one a period earlier.
        if flat[-span:] == flat[-span - period:-period] and sum(c.isalnum() for c in flat[-period:]) >= 2:
            return True
    return False

# After this many unreachable answers in a row an engine is skipped for DOWN_SECONDS.
DOWN_AFTER = 5
DOWN_SECONDS = 120
# A suspect reading (a loop, a runaway, cut off) is tried once more at this temperature with a
# repetition penalty, which breaks some loops; a page still cut off is then read in halves.
RETRY_TEMPERATURE = 0.3
RETRY_REPETITION_PENALTY = 1.1
# A page with less ink than this is empty (see pdf.ink): an echo there means a blank page.
INK_BLANK = 0.001
# A page whose reading is this suspect is read again in halves (Runner).
SPLIT_WHEN = {"truncated", "repetition_loop"}


class EngineError(Exception):
    def __init__(self, message: str, unavailable: bool, retry_after: float = 0.0):
        super().__init__(message)
        self.unavailable = unavailable  # the server is down or busy, rather than refusing the page
        self.retry_after = retry_after


class AllDown(Exception):
    """No engine could be reached. Not the page's fault: it goes back in the queue."""


@dataclass
class Engine:
    name: str  # primary | backup
    url: str
    model: str
    key: str = ""
    timeout: float = 300.0
    prompt: str = PROMPT

    def read(self, image: bytes, temperature: float, max_tokens: int, watch: bool = False,
             **sampling) -> dict:
        """watch: stream the reading and stop it once the model is stuck (see stuck()); its
        finish is then "stuck"."""
        payload = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **sampling,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": self.prompt},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image).decode()}},
            ]}],
        }
        if watch:
            payload |= {"stream": True, "stream_options": {"include_usage": True}}
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        try:
            r = requests.post(self.url.rstrip("/") + "/chat/completions", json=payload,
                              headers=headers, timeout=self.timeout, stream=watch)
        except requests.RequestException as exc:
            raise EngineError(type(exc).__name__, unavailable=True) from exc
        if r.status_code != 200:
            busy = r.status_code >= 500 or r.status_code in (408, 429)
            try:
                retry_after = float(r.headers.get("Retry-After", 0))
            except ValueError:
                retry_after = 0.0
            raise EngineError(f"HTTP {r.status_code}: {r.text[:200]}", busy, retry_after)
        if watch:
            return self._watch(r)
        try:
            body = r.json()
            choice = body["choices"][0]
            text = choice["message"].get("content") or ""
        except (ValueError, KeyError, IndexError) as exc:
            raise EngineError(f"unexpected reply: {r.text[:200]}", unavailable=False) from exc
        return {"text": clean(text), "finish": choice.get("finish_reason"),
                "tokens_out": (body.get("usage") or {}).get("completion_tokens")}

    @staticmethod
    def _watch(r: requests.Response) -> dict:
        """The streamed reading, stopped once the model is stuck. Closing the connection makes
        the server drop the request."""
        text, finish, tokens, checked = "", None, None, 0
        try:
            for line in r.iter_lines():
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    break
                chunk = json.loads(data)
                tokens = (chunk.get("usage") or {}).get("completion_tokens", tokens)
                for choice in chunk.get("choices") or []:
                    text += (choice.get("delta") or {}).get("content") or ""
                    finish = choice.get("finish_reason") or finish
                if len(text) - checked >= STUCK_CHECK_EVERY:
                    checked = len(text)
                    if stuck(text):
                        finish = "stuck"
                        break
        except requests.RequestException as exc:
            raise EngineError(type(exc).__name__, unavailable=True) from exc
        except (ValueError, KeyError) as exc:
            raise EngineError(f"unexpected stream: {exc}", unavailable=False) from exc
        finally:
            r.close()
        return {"text": clean(text), "finish": finish, "tokens_out": tokens}


def clean(text: str) -> str:
    text = _THINK.sub("", text).strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    front = _FRONT_MATTER.match(text)
    if front and "primary_language" in front.group(1):
        text = text[front.end():].strip()
    return text


def is_echo(text: str) -> bool:
    return sum(phrase in text for phrase in _ECHO) >= 2


def strip_echo(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not any(p in line for p in _ECHO)).strip()


def collapse_repeats(text: str) -> tuple[str, int]:
    """(text with each run of three or more identical phrases cut to one, how many runs)."""
    runs = 0

    def one(m: re.Match) -> str:
        nonlocal runs
        unit = m.group(1)
        if _TABLE.search(unit) or sum(c.isalpha() for c in _TAG.sub("", unit)) < 3:
            return m.group(0)
        runs += 1
        return m.group(1)

    return _REPEATS.sub(one, text), runs


def _repeats(a: str, b: str) -> bool:
    """b is a copy of line a: the same apart from numbers that stay the same or count up by one."""
    if _NUMBER.sub("0", " ".join(a.split())) != _NUMBER.sub("0", " ".join(b.split())):
        return False
    steps = [int(y.translate(_THAI_DIGITS)) - int(x.translate(_THAI_DIGITS))
             for x, y in zip(_NUMBER.findall(a), _NUMBER.findall(b))]
    return all(s in (0, 1) for s in steps)


def cut_runaway(text: str, copies: int = RUNAWAY_COPIES) -> tuple[str, int]:
    """For a reading that hit the token limit: (the text without the block of lines it ended
    repeating at least that many times, keeping the first copy; how many copies were cut). Its
    last line was cut short, so it is left out of the match and dropped with the run."""
    lines = text.splitlines()
    body = [i for i, line in enumerate(lines) if line.strip()][:-1]
    n, best = len(body), (0, 0)
    for block in range(1, RUNAWAY_BLOCK + 1):
        k = 0
        while k + block < n and _repeats(lines[body[n - 1 - k - block]], lines[body[n - 1 - k]]):
            k += 1
        span = k + block  # the run's lines: its first copy and the k that repeat it
        if span // block >= copies and span > best[0]:
            best = (span, block)
    span, block = best
    if not span:
        return text, 0
    last_kept = body[n - span + block - 1]
    return "\n".join(lines[:last_kept + 1]).rstrip(), span // block - 1


def _collapsed(out: dict) -> dict:
    """The reading without the block it ended repeating when cut off, and repeats collapsed."""
    text, cut = cut_runaway(out["text"]) if out.get("finish") in CUT_OFF else (out["text"], 0)
    text, runs = collapse_repeats(text)
    return {**out, "text": text, "collapsed": runs, "runaway": cut}


def join_parts(parts: list[dict]) -> dict:
    """The readings of one page's parts (halves, strips), joined top to bottom into one reading
    with every part's doubts; a part that failed fails the page."""
    failed = next((p for p in parts if p["status"] != "done"), None)
    if failed:
        return failed
    texts = [p["text"] for p in parts if p["text"] != BLANK]
    for i in range(1, len(texts)):  # parts that overlap can give a line to both
        last = texts[i - 1].rstrip().rsplit("\n", 1)[-1].strip()
        if last and texts[i].lstrip().startswith(last):
            texts[i] = texts[i].lstrip()[len(last):].lstrip()
    return {"status": "done", "text": "\n\n".join(texts) or BLANK, "engine": parts[0]["engine"],
            "model": parts[0]["model"],
            "suspect": sorted({s for p in parts for s in p["suspect"]}),
            "notes": sorted({n for p in parts for n in p["notes"]}),
            "seconds": round(sum(p["seconds"] for p in parts), 1),
            "tokens_out": sum(p["tokens_out"] or 0 for p in parts)}


def better(a: dict, b: dict) -> dict:
    """The better of two readings of one page: fewer reasons to doubt it, then more text."""
    if b.get("status") != "done":
        return a
    if a.get("status") != "done":
        return b
    return min((a, b), key=lambda r: (len(r["suspect"]), -len(r["text"])))


def assess(out: dict) -> tuple[list[str], list[str]]:
    """(suspect, notes). Suspect means a person should look at the page; notes are harmless
    observations. Only unambiguous failures are suspect: a flag that fires on healthy pages
    teaches people to ignore all flags."""
    text = out["text"].strip()
    suspect: list[str] = []
    notes: list[str] = []
    if not text:
        return ["empty"], notes
    if text == BLANK:
        return suspect, ["blank"]
    if out.get("finish") in CUT_OFF:
        suspect.append("truncated")
    if out.get("finish") == "stuck":
        notes.append("stopped_stuck")
    # Documents repeat boilerplate legitimately; a stuck model's output is almost all repeat.
    # Only the tail is searched, where a runaway ends, which also keeps the regex cheap.
    tail = text[-4000:]
    loop = LOOP.search(tail)
    if loop and len(loop.group(0)) / len(tail) > 0.5:
        suspect.append("repetition_loop")
    # A refusal is all a reading says. The same words turn up inside real pages: ไม่สามารถอ่านและ
    # เขียนหนังสือได้ in election law, ขออภัยโทษ (a pardon), "unable to" in a treaty.
    if len(text) < REFUSAL_MAX and any(marker in text.lower() for marker in REFUSALS):
        suspect.append("possible_refusal")
    illegible = text.count(ILLEGIBLE)
    if illegible > 8:
        suspect.append("many_illegible")
    elif illegible:
        notes.append(f"illegible_{illegible}")
    if len(THAI.findall(text)) / len(text) < 0.25:
        notes.append("little_thai")  # English pages exist; not a fault
    if out.get("collapsed"):
        notes.append(f"repeats_collapsed_{out['collapsed']}")
    if out.get("runaway"):
        notes.append(f"runaway_cut_{out['runaway']}")
    return suspect, notes


_CELL = re.compile(r"<t[dhr]\b|</t[dhr]>|\|")
_CUT_TAG = re.compile(r"\A[^<>]*>|<[^<>]*\Z")  # a tag the match cut in two, at either end


def empty_cell_loop(text: str) -> bool:
    """What assess() takes for a loop is only empty table cells: no letter or digit in it. A sign-in
    sheet or a form has rows like that for real; a stuck model writes them too, but then runs into
    the token limit, so only a reading the model finished by itself is the page's own blank rows."""
    loop = LOOP.search(text.strip()[-4000:])
    if not loop or not _CELL.search(loop.group(1)):
        return False
    return not any(c.isalnum() for c in _TAG.sub("", _CUT_TAG.sub("", loop.group(1))))


class OcrClient:
    """Thread-safe. read_page() returns a result for the page, or raises AllDown.

    lanes: how many pages each engine reads at once as its own share of the work. An engine with
    no lane (0) is only a fallback. A page goes to the first engine with a free lane and falls back
    to the others if that one fails; when an engine is down, its share goes to the others."""

    def __init__(self, engines: list[Engine], attempts: int = 3, max_tokens: int = 4096,
                 lanes: dict[str, int] | None = None, watch: bool = False, read_again: bool = True):
        """watch: stop a reading once the model is stuck (Engine.read). read_again: read a page
        whose reading is suspect once more with a repetition penalty (rescue instead reads it
        again in smaller pieces)."""
        self.engines = engines
        self.attempts = attempts
        self.max_tokens = max_tokens
        self.watch = watch
        self.read_again = read_again
        self.lock = threading.Lock()
        self.failures = {e.name: 0 for e in engines}
        self.down_until = {e.name: 0.0 for e in engines}
        # Default: the first engine does everything, the rest are fallbacks only.
        self.lanes = lanes or {e.name: (1 << 30 if i == 0 else 0) for i, e in enumerate(engines)}
        self.busy = {e.name: 0 for e in engines}

    def _take_lane(self) -> Engine | None:
        with self.lock:
            now = time.monotonic()
            up = [e for e in self.engines if now >= self.down_until[e.name]]
            choice = next((e for e in up if self.busy[e.name] < self.lanes.get(e.name, 0)), None)
            if choice is None and up:
                choice = up[0]  # every lane of the engines still up is full: overflow onto the first
            if choice is not None:
                self.busy[choice.name] += 1
            return choice

    def read_page(self, image: bytes, ink: float | None = None) -> dict:
        """ink: the page's ink (pdf.ink), when known; an empty page whose reading is the prompt
        echoed back is blank."""
        first = self._take_lane()
        try:
            return self._read(image, first, ink)
        finally:
            if first is not None:
                with self.lock:
                    self.busy[first.name] -= 1

    def read_parts(self, images: list[bytes]) -> dict:
        """One page read as several images (its halves), joined in order."""
        result = join_parts([self.read_page(image) for image in images])
        if result["status"] == "done":
            result["notes"] = sorted(set(result["notes"]) | {"read_in_halves"})
        return result

    def _read(self, image: bytes, first: Engine | None, ink: float | None = None) -> dict:
        errors: list[str] = []
        reached = False
        order = [first] + [e for e in self.engines if e is not first] if first else self.engines
        for engine in order:
            if self._is_down(engine):
                continue
            for attempt in range(1, self.attempts + 1):
                started = time.monotonic()
                try:
                    out = engine.read(image, 0.0, self.max_tokens, watch=self.watch)
                except EngineError as exc:
                    errors.append(f"{engine.name}: {exc}")
                    if not exc.unavailable:
                        reached = True  # up, but refuses this page: no point repeating it
                        break
                    if self._failed(engine):
                        break
                    if attempt < self.attempts:
                        time.sleep(exc.retry_after or min(2 ** attempt, 30))
                    continue
                self._ok(engine)
                return self._result(engine, image, out, time.monotonic() - started, ink)
        if not reached:
            raise AllDown("; ".join(errors[-2:]) or "every engine is marked down")
        return {"status": "failed", "error": "; ".join(errors)[-500:]}

    def _again(self, engine: Engine, image: bytes, **sampling) -> dict | None:
        try:
            return engine.read(image, RETRY_TEMPERATURE, self.max_tokens, watch=self.watch, **sampling)
        except EngineError:
            return None

    def _result(self, engine: Engine, image: bytes, out: dict, seconds: float,
                ink: float | None = None) -> dict:
        if is_echo(out["text"]):
            return self._echo(engine, image, out, seconds, ink)
        out = _collapsed(out)
        suspect, notes = assess(out)
        if self.read_again and {"repetition_loop", "truncated", "empty", "possible_refusal"} & set(suspect):
            again = self._again(engine, image, repetition_penalty=RETRY_REPETITION_PENALTY)
            if again is not None and not is_echo(again["text"]):
                again = _collapsed(again)
                suspect2, notes2 = assess(again)
                if len(suspect2) < len(suspect):
                    out, suspect = again, suspect2
                    notes = notes2 + [f"read_at_t{RETRY_TEMPERATURE}_rp{RETRY_REPETITION_PENALTY}"]
        return self._done(engine, out, suspect, notes, seconds)

    def _echo(self, engine: Engine, image: bytes, out: dict, seconds: float, ink: float | None) -> dict:
        """The model wrote out its instructions: it found nothing it could read."""
        if ink is not None and ink < INK_BLANK:
            return self._done(engine, {**out, "text": BLANK}, [], ["blank", "prompt_echo"], seconds)
        again = self._again(engine, image)
        if again is not None and not is_echo(again["text"]):
            again = _collapsed(again)
            suspect, notes = assess(again)
            return self._done(engine, again, suspect, notes + ["prompt_echo", f"read_at_t{RETRY_TEMPERATURE}"],
                              seconds)
        rest = strip_echo(out["text"])
        if len(rest) < 20:  # a photo or a drawing with no words in it, as far as the model can tell
            return self._done(engine, {**out, "text": BLANK}, ["prompt_echo"], ["blank"], seconds)
        rest = _collapsed({**out, "text": rest})
        suspect, notes = assess(rest)
        return self._done(engine, rest, suspect + ["prompt_echo"], notes, seconds)

    @staticmethod
    def _done(engine: Engine, out: dict, suspect: list[str], notes: list[str], seconds: float) -> dict:
        return {"status": "done", "text": out["text"], "engine": engine.name, "model": engine.model,
                "suspect": suspect, "notes": notes, "seconds": round(seconds, 1),
                "tokens_out": out.get("tokens_out")}

    def _is_down(self, engine: Engine) -> bool:
        with self.lock:
            return time.monotonic() < self.down_until[engine.name]

    def _failed(self, engine: Engine) -> bool:
        """Count an unreachable answer; True if the engine is now marked down."""
        with self.lock:
            self.failures[engine.name] += 1
            if self.failures[engine.name] < DOWN_AFTER:
                return False
            if time.monotonic() >= self.down_until[engine.name]:
                log.warning("%s OCR engine (%s) unreachable %d times in a row; skipping it for %d s",
                            engine.name, engine.url, self.failures[engine.name], DOWN_SECONDS)
            self.down_until[engine.name] = time.monotonic() + DOWN_SECONDS
            return True

    def _ok(self, engine: Engine) -> None:
        with self.lock:
            if self.failures[engine.name] >= DOWN_AFTER:
                log.info("%s OCR engine is answering again", engine.name)
            self.failures[engine.name] = 0
            self.down_until[engine.name] = 0.0
