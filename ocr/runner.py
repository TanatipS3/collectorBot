"""OCR over everything the crawl downloaded.

1. Every PDF's own text layer is inspected. Pages with good text keep it; scans, pages whose
   text decodes to junk, and image-only pages are queued for OCR.
2. The queued pages are rendered (in worker processes) and read by the OCR engine (in threads).
   The first pages of every document go first, then small documents before large ones.
3. Each finished document is written to text/<doc_id>.md, and mapping.csv links every one back
   to its file and to every URL that served it.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

from . import pdf
from .engine import (BLANK, ILLEGIBLE, PROMPT_VERSION, SPLIT_WHEN, AllDown, OcrClient, assess, better,
                     collapse_repeats, empty_cell_loop, is_echo, join_parts)
from .store import TEXT_LAYER, OcrStore
from .textlayer import needs_another_look

log = logging.getLogger("ocr")

REPORT_EVERY = 30  # seconds between progress lines
EXPORT_EVERY = 1800  # seconds between mapping.csv rewrites during a run
PAUSE_START, PAUSE_MAX = 30, 600  # when no OCR engine answers: wait, doubling up to the max
MAX_CRASHES = 3  # a file that crashes the PDF reader this often is given up on
# rescue: a page is read again as this many strips; a strip still in doubt is read again in halves,
# and those in halves again, down to this depth (2: a page in up to 6 pieces).
RESCUE_STRIPS = 3
RESCUE_DEPTH = 2
REREAD_REASON = "text layer read again"  # pages.reason of the pages reread_text_layer queues

MAPPING_COLUMNS = ["doc_id", "og_filename", "alt_file_name", "og_filepath", "og_url", "final_url",
                   "found_on", "label", "label_en", "label_th",
                   "duplicate", "status", "pages", "pages_text", "pages_ocr", "pages_failed",
                   "pages_suspect", "ocr_file", "sha256", "size", "note"]
FLAGGED_COLUMNS = ["doc_id", "file", "alt_name", "label", "pages_in_doc", "flagged_pages", "cut_or_loop",
                   "prompt_echo", "possible_refusal", "which_pages"]

# The document classes (classify/final_labels.py) and their names: label -> (English, Thai).
LABEL_NAMES = {
    "law": ("Law", "กฎหมาย"),
    "bill": ("Bill", "ร่างกฎหมาย"),
    "plenary": ("Plenary sitting", "การประชุมสภา"),
    "committee": ("Committee work", "งานคณะกรรมาธิการ"),
    "publication": ("Publication", "เอกสารวิชาการและสิ่งพิมพ์"),
    "procurement": ("Procurement", "จัดซื้อจัดจ้าง"),
    "office_admin": ("Office administration", "บริหารงานสำนักงาน"),
    "green_office": ("Green Office", "สำนักงานสีเขียว"),
    "other": ("Other", "อื่น ๆ"),
}


# alt_file_name: what a document is, from its first line of content, since many saved names say
# nothing (1_2618.pdf, 18.pdf). A heading split over short lines is joined until it is this long
# (ประกาศสำนักงานเลขาธิการวุฒิสภา + เรื่อง ...; ร่าง + พระราชบัญญัติ... + ว่าด้วย...).
TITLE_CHARS, TITLE_LINES, TITLE_MAX = 40, 3, 150
_MARKUP = re.compile(r"!\[[^\]]*\]\([^)]*\)|<[^>]*>|[#*_`|>]+")


def _content_lines(text: str):
    """The page's lines without markup, skipping page numbers, blank markers and image
    descriptions."""
    in_figure = False
    for raw in (text or "").splitlines():
        if "<figure" in raw:
            in_figure = "</figure>" not in raw
            continue
        if in_figure:
            in_figure = bool(raw.strip()) and "</figure>" not in raw
            continue
        if raw.strip() in (BLANK, ILLEGIBLE):
            continue
        line = re.sub(r"\s+", " ", _MARKUP.sub(" ", raw)).strip(" -–:.")
        if sum(c.isalpha() for c in line) >= 2:  # not a page number, a rule or an empty cell
            yield line


def first_line_title(pages: list[str]) -> str:
    """The first line of content of the first page that has any, joined with the next lines
    while short. A memo or letter opens with form fields (ส่วนราชการ, ที่, วันที่): its เรื่อง
    line, a few lines down, says what it is about and is added."""
    for text in pages:
        lines = list(_content_lines(text))[:12]
        if not lines:
            continue
        parts = []
        for line in lines[:TITLE_LINES]:
            parts.append(line)
            if len(" ".join(parts)) >= TITLE_CHARS:
                break
        subject = next((line for line in lines[:8] if line.startswith("เรื่อง")), None)
        if subject and not any("เรื่อง" in p for p in parts):
            parts = [parts[0], subject]
        return " ".join(parts)[:TITLE_MAX].strip()
    return ""


def load_labels(path: Path, column: str = "label") -> dict[str, str]:
    """doc_id -> column from one of the classification's final CSVs; none yet is fine."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return {r["doc_id"]: r[column] for r in csv.DictReader(f) if r[column]}
    except FileNotFoundError:
        return {}


def collector_files(db_path: Path) -> list[sqlite3.Row]:
    """Every URL the crawl saved a file for. Duplicates share the stored copy's path."""
    con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT url, final_url, path, sha256, size, referrer, note FROM urls"
            " WHERE kind='file' AND status='done' AND path IS NOT NULL ORDER BY path, url").fetchall()
    finally:
        con.close()


def _is_duplicate(row) -> bool:
    return (row["note"] or "").startswith("duplicate of")


def _is_pdf(path: Path) -> bool:
    # By content, not name: some saved names don't end in .pdf (lis.senate.go.th/'xxxx.pdf').
    with open(path, "rb") as f:
        return b"%PDF" in f.read(1024)


class Runner:
    def __init__(self, cfg, store: OcrStore, client: OcrClient):
        self.cfg = cfg
        self.store = store
        self.client = client
        self.downloads = Path(cfg.downloads_dir)
        self.sources: dict[str, list[sqlite3.Row]] = {}  # path -> every URL that served it
        self.stopping = False
        self.read_this_run = 0
        self.last_report = self.last_export = time.monotonic()

    # --- 0. what the crawl has -------------------------------------------------------------

    def sync(self) -> None:
        by_path: dict[str, list] = defaultdict(list)
        for row in collector_files(self.cfg.collector_db):
            by_path[row["path"]].append(row)
        self.sources = dict(by_path)

        known = {r["path"]: r["doc_id"] for r in self.store.db.execute("SELECT path, doc_id FROM docs")}
        ids = set(known.values())
        new = []
        for path, rows in by_path.items():
            if path in known:
                continue
            main = next((r for r in rows if not _is_duplicate(r)), rows[0])
            sha = main["sha256"] or hashlib.sha256(path.encode()).hexdigest()
            doc_id = sha[:16]
            if doc_id in ids:  # same bytes stored twice (rare): keep both, apart
                doc_id = sha[:10] + hashlib.sha256(path.encode()).hexdigest()[:6]
            ids.add(doc_id)
            if not (self.downloads / path).is_file():
                status, note = "error", "file missing from the downloads folder"
            elif not _is_pdf(self.downloads / path):
                status, note = "skipped", f"not a PDF ({Path(path).suffix.lower() or 'no extension'})"
            else:
                status, note = "new", None
            new.append((doc_id, path, main["sha256"], main["size"], status, note))
        if new:
            self.store.add_docs(new)
            log.info("found %d new files from the crawl (%d PDFs)", len(new),
                     sum(1 for r in new if r[4] == "new"))

    # --- 1. text layers --------------------------------------------------------------------

    def _pool(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(self.cfg.processes, initializer=pdf.worker_init)

    def inspect_all(self) -> None:
        todo = list(self.store.docs_to_inspect())
        if not todo:
            return
        log.info("reading the text layers of %d PDFs (%d processes)", len(todo), self.cfg.processes)
        pool, inflight, done = self._pool(), {}, 0
        try:
            while True:
                while not self.stopping and todo and len(inflight) < self.cfg.processes * 2:
                    # Largest first (the list is sorted by size), so no 1,000-page book is left
                    # running alone at the end.
                    row = todo.pop()
                    inflight[pool.submit(pdf.inspect, str(self.downloads / row["path"]))] = row
                if not inflight:
                    break
                try:
                    finished, _ = wait(inflight, timeout=1, return_when=FIRST_COMPLETED)
                except KeyboardInterrupt:
                    self._stop("finishing the PDFs being read")
                    continue
                broken = False
                for future in finished:
                    row = inflight.pop(future)
                    try:
                        pages = future.result()
                    except BrokenProcessPool:
                        broken = True
                        self._inspection_crashed(row, todo)
                        continue
                    except Exception as exc:
                        self.store.mark_doc(row["doc_id"], "error", f"{type(exc).__name__}: {exc}"[:300])
                        continue
                    done += 1
                    if not pages:
                        self.store.mark_doc(row["doc_id"], "error", "no pages")
                    elif self.store.save_inspection(row["doc_id"], pages) == 0:
                        self.write_doc(row["doc_id"])  # nothing to OCR: finished already
                if broken:
                    # A file crashed the PDF reader and took every document in flight with it.
                    # All of them are tried again; the crash count stops the one that did it.
                    pool.shutdown(wait=False, cancel_futures=True)
                    for row in inflight.values():
                        self._inspection_crashed(row, todo)
                    inflight.clear()
                    pool = self._pool()
                if time.monotonic() - self.last_report >= REPORT_EVERY:
                    self.last_report = time.monotonic()
                    c = self.store.counts()
                    log.info("text layers: %d of %d PDFs read | pages so far: %d usable text, %d need OCR",
                             done, done + len(todo) + len(inflight),
                             sum(n for (m, _), n in c["pages"].items() if m == "text"),
                             sum(n for (m, _), n in c["pages"].items() if m == "ocr"))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _inspection_crashed(self, row, todo: list) -> None:
        if self.store.count_crash(row["doc_id"]) >= MAX_CRASHES:
            self.store.mark_doc(row["doc_id"], "error", "crashes the PDF reader")
            log.warning("giving up on %s: it crashes the PDF reader", row["path"])
        else:
            todo.append(row)

    # --- 2. OCR ----------------------------------------------------------------------------

    def ocr(self, limit: int | None = None) -> None:
        pending = self.store.pending_pages()
        if not pending:
            log.info("no pages waiting for OCR")
            return
        engines = ", ".join(f"{e.name} {e.model} at {e.url}" for e in self.client.engines)
        lanes = self.cfg.workers + self.cfg.backup_workers
        log.info("OCR: %d pages to read | %s | %d at a time (primary %d, backup %d)", pending, engines,
                 lanes, self.cfg.workers, self.cfg.backup_workers)
        renderer = self._pool()
        readers = ThreadPoolExecutor(lanes, thread_name_prefix="ocr")
        # future -> (stage, page row, first reading). Stages: render, read; a page whose reading
        # is still cut off or looping goes on to halves (render) and halves_read.
        inflight: dict = {}
        claimed, pause, resume_at = 0, PAUSE_START, 0.0
        try:
            while True:
                now = time.monotonic()
                exhausted = False
                if not self.stopping and now >= resume_at:
                    want = lanes * 2 - len(inflight)
                    if limit is not None:
                        want = min(want, limit - claimed)
                        exhausted = claimed >= limit
                    if want > 0:
                        rows = self.store.claim(want)
                        exhausted = len(rows) < want
                        for row in rows:
                            future = renderer.submit(pdf.render_page, str(self.downloads / row["path"]),
                                                     row["page"], self.cfg.render_px)
                            inflight[future] = ("render", row, None)
                        claimed += len(rows)
                if not inflight:
                    if self.stopping or exhausted:
                        break
                    time.sleep(1)  # paused: no engine answering
                    continue
                try:
                    finished, _ = wait(inflight, timeout=1, return_when=FIRST_COMPLETED)
                except KeyboardInterrupt:
                    self._stop(f"finishing the {len(inflight)} pages in flight")
                    continue
                broken = False
                for future in finished:
                    stage, row, first = inflight.pop(future)
                    if stage == "render":
                        try:
                            image, ink = future.result()
                        except BrokenProcessPool:
                            broken = True
                            self._render_crashed(row)
                            continue
                        except Exception as exc:
                            self._page_failed(row, f"render: {type(exc).__name__}: {exc}")
                            continue
                        if self.stopping:
                            self.store.release(row["doc_id"], row["page"])
                            continue
                        inflight[readers.submit(self.client.read_page, image, ink)] = ("read", row, None)
                        continue
                    if stage == "halves":
                        try:
                            images = future.result()
                        except Exception as exc:  # the whole-page reading stands
                            broken = broken or isinstance(exc, BrokenProcessPool)
                            self._save(row, first)
                            continue
                        inflight[readers.submit(self.client.read_parts, images)] = ("halves_read", row, first)
                        continue
                    try:
                        result = future.result()
                    except AllDown as exc:
                        if first is not None:  # halves could not be read: the first reading stands
                            self._save(row, first)
                            continue
                        self.store.release(row["doc_id"], row["page"])
                        if now >= resume_at:
                            log.warning("no OCR engine is answering (%s); pausing %d s", exc, pause)
                            resume_at = time.monotonic() + pause
                            pause = min(pause * 2, PAUSE_MAX)
                        continue
                    except Exception as exc:  # a bug, not the engine: keep the run going
                        if first is not None:
                            self._save(row, first)
                        else:
                            self._page_failed(row, f"{type(exc).__name__}: {exc}")
                        continue
                    pause = PAUSE_START
                    if stage == "halves_read":
                        self._save(row, better(first, result))
                    elif result["status"] == "done" and SPLIT_WHEN & set(result["suspect"]) and not self.stopping:
                        future = renderer.submit(pdf.render_halves, str(self.downloads / row["path"]),
                                                 row["page"], self.cfg.render_px)
                        inflight[future] = ("halves", row, result)
                    else:
                        self._save(row, result)
                if broken:
                    renderer.shutdown(wait=False, cancel_futures=True)
                    for future, (stage, row, first) in list(inflight.items()):
                        if stage == "render":
                            del inflight[future]
                            self._render_crashed(row)
                        elif stage == "halves":
                            del inflight[future]
                            self._save(row, first)
                    renderer = self._pool()
                self._periodic()
        finally:
            renderer.shutdown(wait=False, cancel_futures=True)
            readers.shutdown(wait=False, cancel_futures=True)

    def _save(self, row, result: dict) -> None:
        if result["status"] != "done":
            self._page_failed(row, result["error"])
            return
        self.store.finish_page(
            row["doc_id"], row["page"], status="done", text=result["text"], engine=result["engine"],
            model=result["model"], prompt_version=PROMPT_VERSION, suspect=",".join(result["suspect"]),
            notes=",".join(result["notes"]), seconds=result["seconds"],
            tokens_out=result["tokens_out"], error=None)
        self.read_this_run += 1
        if self.store.finish_doc_if_complete(row["doc_id"]):
            self.write_doc(row["doc_id"])

    def _page_failed(self, row, error: str) -> None:
        log.warning("page %d of %s failed: %s", row["page"], row["path"], error[:200])
        self.store.finish_page(row["doc_id"], row["page"], status="failed", error=error[:500])
        if self.store.finish_doc_if_complete(row["doc_id"]):
            self.write_doc(row["doc_id"])

    def _render_crashed(self, row) -> None:
        if row["attempts"] + 1 >= MAX_CRASHES:  # attempts as claimed, before this try
            self._page_failed(row, "crashes the PDF reader")
        else:
            self.store.release(row["doc_id"], row["page"], counted=True)

    def _stop(self, what: str) -> None:
        if self.stopping:
            raise KeyboardInterrupt  # second Ctrl+C: quit now
        self.stopping = True
        log.info("stopping: %s (Ctrl+C again to quit at once; nothing finished is lost)", what)

    def _periodic(self) -> None:
        now = time.monotonic()
        if now - self.last_report >= REPORT_EVERY:
            self.last_report = now
            log.info(progress_line(self.store))
        if now - self.last_export >= EXPORT_EVERY:
            self.last_export = now
            self.export()

    # --- earlier output, brought up to the current rules -----------------------------------

    def fix(self, dry_run: bool = False) -> Counter:
        """Apply the current rules to what earlier runs produced, without redoing what is fine:
        text-layer pages the rules now read differently are inspected again (repaired, or sent to
        OCR); OCR text with a phrase repeated over and over is cut to one copy in place; pages
        read badly (cut off, looping, refused, the prompt echoed back) go back in the queue for
        `run`. Returns what changes; with dry_run nothing is written."""
        self.sync()
        changes: Counter = Counter()
        updates: list[tuple[str, int, dict]] = []
        reinspect: set[str] = set()
        for row in self.store.db.execute(
                "SELECT doc_id, page, method, reason, text, suspect, notes FROM pages WHERE status='done'"):
            if row["method"] == "text":
                if row["reason"] == "blank" or needs_another_look(row["text"] or ""):
                    reinspect.add(row["doc_id"])
                continue
            what, fields = _recheck_ocr(row)
            if what:
                changes[what] += 1
                updates.append((row["doc_id"], row["page"], fields))
        log.info("fix: %d documents to inspect again; OCR pages: %s", len(reinspect),
                 ", ".join(f"{n:,} {k}" for k, n in sorted(changes.items())) or "nothing to change")
        for doc_id, pages in self._inspect_again(sorted(reinspect)):
            old = {r["page"]: r for r in self.store.doc_pages(doc_id)}
            for p in pages:
                o = old.get(p["page"])
                if o is None or o["method"] != "text":
                    continue  # pages already read by OCR keep their reading
                if p["method"] == "ocr":
                    changes[f"text layer -> OCR: {p['reason']}"] += 1
                    updates.append((doc_id, p["page"], {"method": "ocr", "reason": p["reason"], "status": "pending",
                                                        "text": None, "engine": None}))
                elif p["text"] != (o["text"] or ""):
                    changes["text layer repaired"] += 1
                    updates.append((doc_id, p["page"], {"text": p["text"], "reason": p["reason"]}))
        if dry_run or not updates:
            return changes
        self.store.set_pages(updates)
        for doc_id in {u[0] for u in updates}:
            if not self.store.has_open_pages(doc_id):
                self.write_doc(doc_id)  # changed in place; the others are written once read
        self.export()
        return changes

    # --- the text-layer pages read again by OCR, in an output of their own ------------------

    def reread_text_layer(self, source: Path, dry_run: bool = False) -> Counter:
        """Start this output from another run's state (source: its ocr.db) and queue every page
        that run took from the PDF's text layer to be read by OCR instead, for `run`. Pages read
        by OCR there, and blank pages, are kept as they are; the source is only read. Once queued
        there is nothing more to do, so calling it again changes nothing. Returns what is queued;
        with dry_run nothing is written."""
        if Path(source).resolve() == self.cfg.db_path.resolve():
            raise ValueError(f"{source} is this output's own state: set another output_dir")
        fresh = self.store.db.execute("SELECT 1 FROM docs LIMIT 1").fetchone() is None
        if fresh and not dry_run:
            log.info("copying the OCR state of %s", source)
            self.store.copy_from(source)
        db = sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True) if fresh and dry_run \
            else self.store.db
        per_doc = dict(db.execute(f"SELECT doc_id, COUNT(*) FROM pages WHERE {TEXT_LAYER} GROUP BY doc_id"))
        if db is not self.store.db:
            db.close()
        changes = Counter({"text-layer pages to OCR": sum(per_doc.values()), "documents": len(per_doc)})
        if not dry_run and per_doc:
            self.store.queue_text_layer(REREAD_REASON)
        return +changes

    # --- flags looked at again, from the stored text alone ----------------------------------

    def recheck(self, dry_run: bool = False) -> Counter:
        """Drop flags the stored text shows were wrong, reading nothing again and queuing nothing:
        a reading the model finished by itself whose loop is only empty table cells (see
        empty_cell_loop) is a table with blank rows, not a stuck model. A reading cut off at the
        token limit keeps its flags. Then rewrites the changed documents' text files,
        flagged_pages.csv and mapping.csv. Returns what changes; with dry_run nothing is written."""
        changes: Counter = Counter()
        updates: list[tuple[str, int, dict]] = []
        for row in self.store.db.execute(
                "SELECT doc_id, page, text, suspect, notes FROM pages WHERE status='done' AND method='ocr'"
                " AND suspect LIKE '%repetition_loop%'").fetchall():
            suspect = [s for s in row["suspect"].split(",") if s]
            if "truncated" in suspect or not empty_cell_loop(row["text"] or ""):
                continue
            suspect.remove("repetition_loop")
            changes["loop flag dropped: blank table rows"] += 1
            updates.append((row["doc_id"], row["page"], {
                "suspect": ",".join(suspect),
                "notes": ",".join(filter(None, [row["notes"] or "", "blank_rows_not_a_loop"]))}))
        if dry_run:
            return changes
        self.store.set_pages(updates)
        self.sync()
        for doc_id in sorted({doc_id for doc_id, _, _ in updates}):
            if self.store.doc(doc_id)["status"] == "done":
                self.write_doc(doc_id)
        self.write_flagged()
        self.export()
        return changes

    def write_flagged(self) -> int:
        """flagged_pages.csv: the documents with pages a person should look at, most first.
        Returns how many pages are flagged."""
        labels = load_labels(Path(self.cfg.labels_csv))
        alt_names = load_labels(Path(self.cfg.alt_names_csv), "alt_name")
        rows = self.store.db.execute(
            "SELECT d.doc_id, d.path, d.pages, COUNT(*) AS flagged,"
            " SUM(p.suspect LIKE '%truncated%' OR p.suspect LIKE '%repetition_loop%') AS cut_or_loop,"
            " SUM(p.suspect = 'prompt_echo') AS echo, SUM(p.suspect = 'possible_refusal') AS refusal,"
            " GROUP_CONCAT(p.page) AS which"
            " FROM pages p JOIN docs d USING (doc_id) WHERE p.status='done' AND COALESCE(p.suspect, '') != ''"
            " GROUP BY d.doc_id ORDER BY flagged DESC, d.path").fetchall()
        target = self.cfg.out / "flagged_pages.csv"
        tmp = target.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig: Thai in Excel
            w = csv.writer(f)
            w.writerow(FLAGGED_COLUMNS)
            for r in rows:
                w.writerow([r["doc_id"], r["path"], alt_names.get(r["doc_id"], ""), labels.get(r["doc_id"], ""),
                            r["pages"], r["flagged"], r["cut_or_loop"], r["echo"], r["refusal"],
                            ",".join(sorted(r["which"].split(","), key=int))])
        os.replace(tmp, target)
        return sum(r["flagged"] for r in rows)

    # --- the pages still in doubt, read again in strips -------------------------------------

    def rescue(self, dry_run: bool = False) -> Counter:
        """Pages whose reading is still in doubt, read again by the primary engine in strips cut
        between lines; a strip the model gets stuck on or cannot finish is read again in halves,
        down to RESCUE_DEPTH. A page is read this way once (notes: read_in_strips); the new reading
        replaces the old one unless it has more reasons to doubt it. A reading flagged only as a
        possible refusal is first checked again with the current rule. Returns what changed."""
        changes: Counter = Counter()
        rows = self.store.db.execute(
            "SELECT p.doc_id, p.page, p.text, p.suspect, p.notes, d.path FROM pages p JOIN docs d USING (doc_id)"
            " WHERE p.status='done' AND p.method='ocr' AND p.suspect != ''"
            " AND COALESCE(p.notes, '') NOT LIKE '%read_in_strips%' ORDER BY d.path, p.page").fetchall()
        todo, cleared = [], []
        for row in rows:
            suspect = set(row["suspect"].split(","))
            if "possible_refusal" in suspect and "possible_refusal" not in assess({"text": row["text"] or ""})[0]:
                suspect.discard("possible_refusal")
                changes["refusal flag dropped: it is the page's own text"] += 1
                cleared.append((row["doc_id"], row["page"], {"suspect": ",".join(sorted(suspect))}))
            if suspect:
                todo.append(row)
                changes[f"to read in strips: {sorted(suspect)[0]}"] += 1
        log.info("rescue: %d pages to read again in strips; %s", len(todo),
                 ", ".join(f"{n:,} {k}" for k, n in sorted(changes.items())) or "nothing to do")
        if dry_run:
            return changes
        if cleared:
            self.store.set_pages(cleared)
            for doc_id in {c[0] for c in cleared}:
                self.write_doc(doc_id)
        if todo:
            self._rescue(todo, changes)
            self.export()
        return changes

    def _rescue(self, todo: list, changes: Counter) -> None:
        renderer = self._pool()
        readers = ThreadPoolExecutor(self.cfg.workers, thread_name_prefix="rescue")
        queue, inflight, total = list(reversed(todo)), {}, len(todo)
        pause, resume_at, done = PAUSE_START, 0.0, 0
        try:
            while queue or inflight:
                now = time.monotonic()
                while not self.stopping and queue and now >= resume_at and len(inflight) < self.cfg.workers:
                    row = queue.pop()
                    inflight[readers.submit(self._read_in_strips, renderer, row)] = row
                if not inflight:
                    if self.stopping:
                        break
                    time.sleep(1)
                    continue
                try:
                    finished, _ = wait(inflight, timeout=1, return_when=FIRST_COMPLETED)
                except KeyboardInterrupt:
                    self._stop(f"finishing the {len(inflight)} pages being read")
                    continue
                for future in finished:
                    row = inflight.pop(future)
                    try:
                        new = future.result()
                    except AllDown as exc:
                        queue.append(row)
                        if now >= resume_at:
                            log.warning("the OCR engine is not answering (%s); pausing %d s", exc, pause)
                            resume_at = time.monotonic() + pause
                            pause = min(pause * 2, PAUSE_MAX)
                        continue
                    except Exception as exc:  # the old reading stands
                        if isinstance(exc, BrokenProcessPool):
                            renderer.shutdown(wait=False, cancel_futures=True)
                            renderer = self._pool()
                        log.warning("page %d of %s: %s: %s", row["page"], row["path"], type(exc).__name__, exc)
                        changes["could not be read again"] += 1
                        continue
                    pause = PAUSE_START
                    done += 1
                    changes[self._keep_rescued(row, new)] += 1
                if time.monotonic() - self.last_report >= REPORT_EVERY:
                    self.last_report = time.monotonic()
                    log.info("rescue: %d of %d pages read again | %s", done, total,
                             ", ".join(f"{n:,} {k}" for k, n in sorted(changes.items()) if k.startswith("now")))
        finally:
            renderer.shutdown(wait=False, cancel_futures=True)
            readers.shutdown(wait=False, cancel_futures=True)

    def _read_in_strips(self, renderer, row) -> dict:
        """The page read in strips (in a reader thread; PDFs are rendered in the worker processes)."""
        path, page, px = str(self.downloads / row["path"]), row["page"], self.cfg.render_px
        started = time.monotonic()
        strips = renderer.submit(pdf.render_strips, path, page, px, RESCUE_STRIPS).result()
        parts = [self._read_piece(renderer, path, page, image, span, 1) for image, span in strips]
        result = join_parts(parts)
        return {**result, "notes": sorted(set(result["notes"]) | {"read_in_strips"}),
                "seconds": round(time.monotonic() - started, 1)}

    def _read_piece(self, renderer, path: str, page: int, image: bytes, span, depth: int) -> dict:
        out = self.client.read_page(image)
        if out["status"] != "done" or depth >= RESCUE_DEPTH or not SPLIT_WHEN & set(out["suspect"]):
            return out
        halves = renderer.submit(pdf.render_strips, path, page, self.cfg.render_px, 2, span).result()
        split = join_parts([self._read_piece(renderer, path, page, im, sp, depth + 1) for im, sp in halves])
        return better(out, split)

    def _keep_rescued(self, row, new: dict) -> str:
        """Store the new reading unless it is in more doubt than the old; what happened."""
        old = set(filter(None, row["suspect"].split(",")))
        if new["status"] != "done" or len(new["suspect"]) > len(old):
            self.store.set_pages([(row["doc_id"], row["page"], {
                "notes": ",".join(filter(None, [row["notes"] or "", "read_in_strips_kept_old"]))})])
            return "kept the old reading"
        self.store.set_pages([(row["doc_id"], row["page"], {
            "text": new["text"], "engine": new["engine"], "model": new["model"],
            "prompt_version": PROMPT_VERSION, "suspect": ",".join(new["suspect"]),
            "notes": ",".join(new["notes"]), "seconds": new["seconds"], "tokens_out": new["tokens_out"]})])
        self.write_doc(row["doc_id"])
        return f"now {'in doubt: ' + ','.join(new['suspect']) if new['suspect'] else 'fine'}"

    def _inspect_again(self, doc_ids: list[str]):
        """(doc_id, pages) for each document, inspected with the current rules."""
        paths = {r["doc_id"]: r["path"] for r in self.store.db.execute("SELECT doc_id, path FROM docs")}
        todo, inflight, done = list(doc_ids), {}, 0
        pool = self._pool()
        try:
            while todo or inflight:
                while todo and len(inflight) < self.cfg.processes * 2:
                    d = todo.pop()
                    inflight[pool.submit(pdf.inspect, str(self.downloads / paths[d]))] = d
                finished, _ = wait(inflight, return_when=FIRST_COMPLETED)
                for future in finished:
                    d = inflight.pop(future)
                    try:
                        pages = future.result()
                    except Exception as exc:  # inspected fine before: leave it as it was
                        log.warning("could not inspect %s again: %s", paths[d], exc)
                        if isinstance(exc, BrokenProcessPool):
                            pool.shutdown(wait=False, cancel_futures=True)
                            todo += inflight.values()
                            inflight.clear()
                            pool = self._pool()
                            break
                        continue
                    done += 1
                    if done % 500 == 0:
                        log.info("inspected %d of %d documents again", done, len(doc_ids))
                    yield d, pages
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    # --- 3. output -------------------------------------------------------------------------

    def _main_source(self, path: str):
        rows = self.sources.get(path) or []
        return next((r for r in rows if not _is_duplicate(r)), rows[0] if rows else None)

    def text_file(self, doc_id: str) -> Path:
        return self.cfg.text_dir / f"{doc_id}.md"

    def write_doc(self, doc_id: str) -> None:
        """text/<doc_id>.md: where the document came from, then every page in order."""
        doc = self.store.doc(doc_id)
        pages = self.store.doc_pages(doc_id)
        main = self._main_source(doc["path"])
        others = [r["url"] for r in self.sources.get(doc["path"], []) if main is None or r["url"] != main["url"]]
        header = {
            "doc_id": doc_id,
            "og_filename": Path(doc["path"]).name,
            "og_filepath": f"{self.cfg.downloads_dir}/{doc['path']}",
            "og_url": main["url"] if main else "",
            "found_on": (main["referrer"] or "") if main else "",
            "other_urls": others,
            "sha256": doc["sha256"],
            "pages": len(pages),
            "pages_text_layer": sum(p["method"] == "text" for p in pages),
            "pages_ocr": sum(p["method"] == "ocr" and p["status"] == "done" for p in pages),
            "pages_failed": sum(p["status"] == "failed" for p in pages),
        }
        parts = ["---", *(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in header.items()), "---"]
        for p in pages:
            if p["status"] == "failed":
                how, body = f"ocr failed: {p['error'] or ''}"[:200], "[OCR ไม่สำเร็จ]"
            elif p["method"] == "text":
                how, body = "text layer", p["text"] or ""
            else:
                how, body = f"ocr {p['model']}", p["text"] or ""
                if p["suspect"]:
                    how += f" | suspect: {p['suspect']}"
            parts += ["", f"<!-- page {p['page']}/{len(pages)} | {how.replace('--', '- ')} -->", "", body]
        target = self.text_file(doc_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text("\n".join(parts) + "\n", encoding="utf-8")
        os.replace(tmp, target)

    def write_missing_docs(self) -> int:
        """Finished documents without a text file (e.g. the run was killed in between)."""
        missing = [d for d in self.store.done_docs() if not self.text_file(d).is_file()]
        for doc_id in missing:
            self.write_doc(doc_id)
        return len(missing)

    def export(self) -> int:
        """mapping.csv: one row per URL the crawl saved a file from, with that file's OCR output."""
        docs = self.store.all_docs()
        labels = load_labels(Path(self.cfg.labels_csv))
        alt_names = load_labels(Path(self.cfg.alt_names_csv), "alt_name")
        target = self.cfg.mapping_path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        n = 0
        with open(tmp, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig: Thai in Excel
            w = csv.writer(f)
            w.writerow(MAPPING_COLUMNS)
            for path in sorted(self.sources):
                d = docs.get(path)
                done = d is not None and d["status"] == "done"
                label = labels.get(d["doc_id"], "") if d else ""
                en, th = LABEL_NAMES.get(label, ("", ""))
                title = (alt_names.get(d["doc_id"]) or first_line_title(self.store.first_pages_text(d["doc_id"], 3))
                         if d else "")
                for r in self.sources[path]:
                    w.writerow([
                        d["doc_id"] if d else "", Path(path).name, title, f"{self.cfg.downloads_dir}/{path}",
                        r["url"], r["final_url"] or "", r["referrer"] or "", label, en, th,
                        "yes" if _is_duplicate(r) else "",
                        _doc_status(d), d["pages"] if d else "", d["pages_text"] or 0 if d else "",
                        d["pages_ocr"] or 0 if d else "", d["pages_failed"] or 0 if d else "",
                        d["pages_suspect"] or 0 if d else "",
                        self.text_file(d["doc_id"]).as_posix() if done else "",
                        r["sha256"] or "", r["size"] or "", (d["note"] or "") if d else "",
                    ])
                    n += 1
        os.replace(tmp, target)
        return n

    # --- the whole run ---------------------------------------------------------------------

    def run(self, limit: int | None = None) -> None:
        self.sync()
        reset = self.store.reset_claims()
        retried = self.store.requeue_failed(self.cfg.max_page_attempts)
        if reset or retried:
            log.info("back in the queue: %d pages a stopped run was reading, %d that failed last time",
                     reset, retried)
        self.inspect_all()
        self.write_missing_docs()
        self.export()
        if not self.stopping:
            self.ocr(limit)
        rows = self.export()
        log.info(progress_line(self.store))
        log.info("%s %d pages this run | mapping.csv: %d rows | text files in %s",
                 "stopped after" if self.stopping else "finished:", self.read_this_run, rows,
                 self.cfg.text_dir)


def _recheck_ocr(row) -> tuple[str, dict]:
    """(what to do with an OCR page read by an earlier run, the fields to set); ('', {}) if it
    is fine."""
    text = row["text"] or ""
    suspect = set(filter(None, (row["suspect"] or "").split(",")))
    if "read_in_strips" in (row["notes"] or ""):
        return "", {}  # rescue read it in pieces: as good as it gets
    if is_echo(text):
        return "OCR again: prompt echoed", {"status": "pending"}
    bad = suspect & {"truncated", "possible_refusal", "empty"}
    if bad:
        return f"OCR again: {sorted(bad)[0]}", {"status": "pending"}
    collapsed, runs = collapse_repeats(text)
    notes = ",".join(filter(None, [row["notes"] or "", f"repeats_collapsed_{runs}"]))
    if "repetition_loop" in suspect:
        if runs and not assess({"text": collapsed})[0]:
            return "repeats collapsed (was a loop)", {"text": collapsed, "suspect": "", "notes": notes}
        return "OCR again: repetition_loop", {"status": "pending"}
    if runs:
        return "repeats collapsed", {"text": collapsed, "notes": notes}
    return "", {}


def _doc_status(d) -> str:
    if d is None:
        return "not in OCR state"
    return {"new": "waiting", "inspected": "in progress"}.get(d["status"], d["status"])


def progress_line(store: OcrStore) -> str:
    c = store.counts()
    pages = c["pages"]
    left = pages.get(("ocr", "pending"), 0) + pages.get(("ocr", "running"), 0)
    done = pages.get(("ocr", "done"), 0)
    rate = store.ocr_rate()
    eta = f"{left / rate / 60:.1f} h" if rate else "?"
    docs = c["docs"]
    total = sum(docs.values()) - docs.get("skipped", 0)
    return (f"OCR pages {done:,} done, {left:,} left ({c['first_pages_left']:,} of them first pages), "
            f"{pages.get(('ocr', 'failed'), 0):,} failed, {c['suspect']:,} suspect | "
            f"{rate:.0f} pages/min, about {eta} left | documents {docs.get('done', 0):,} of {total:,} done")
