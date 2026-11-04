"""Name every document from its first pages with an LLM: a file name that says what it is, since
many saved names say nothing (1_2618.pdf, 18.pdf) and first lines repeat across a series.

On pgx, in ~/senate/classify. A run keeps its own folder under runs/, like the classification:
    LLM_URL=http://localhost:8005/v1 python3 name_docs.py gemma4 runs/names_gemma4 [workers]
    python3 name_docs.py --merge runs/names_gemma4        # rebuild final/alt_names.csv only
Writes runs/<run>/names.jsonl (one answer per document, resumable), run.json, the prompt, and
final/alt_names.csv (doc_id, alt_name), which the OCR export puts in mapping.csv as alt_file_name.
Names that still collide get the original file name added: (ชื่อไฟล์เดิม).
"""
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

URL = os.environ.get("LLM_URL", "http://localhost:8005/v1") + "/chat/completions"
KEY = os.environ.get("LLM_KEY", "")
OCR_DB = os.environ.get("OCR_DB", str(Path(__file__).resolve().parent.parent / "ocr_out" / "ocr.db"))
MAX_NAME = 150

PROMPT = """ตั้งชื่อไฟล์ใหม่ให้เอกสารนี้จากเนื้อหา ให้คนรู้ว่าเป็นเอกสารอะไรโดยไม่ต้องเปิดดู
- ภาษาไทย สั้นกระชับ ไม่เกิน 80 ตัวอักษร ไม่ต้องมีนามสกุลไฟล์
- เลือกเฉพาะที่สำคัญ: ประเภทเอกสาร + เรื่องหลัก + สิ่งที่แยกฉบับได้หนึ่งอย่าง (วันที่ หรือ ครั้งที่) ไม่ต้องใส่ทุกรายละเอียด
- บอกประเภทเอกสาร (เช่น ประกาศ บันทึกข้อความ รายงาน ร่างพระราชบัญญัติ สรุปข่าว ระเบียบวาระการประชุม) และเรื่อง
- ใส่สิ่งที่ทำให้แยกจากฉบับอื่นในชุดเดียวกันได้ เช่น วันที่ ครั้งที่ เลขที่ ปี ชื่อหน่วยงานหรือคณะกรรมาธิการ
- วันที่ให้ใช้วันที่ของตัวเอกสาร ไม่ใช่วันของเหตุการณ์ที่เอกสารพูดถึง ถ้าชื่อไฟล์เดิมมีวันที่ให้ใช้วันที่นั้น เขียนแบบ 7 กันยายน 2569
- ใช้เฉพาะข้อมูลที่อยู่ในเนื้อหาหรือชื่อไฟล์เดิม ห้ามเดาหรือแต่งเพิ่ม
- ไม่ต้องสนใจลายน้ำ หัวกระดาษ และเลขหน้า
ตอบ JSON บรรทัดเดียว: {"name": "..."}"""

_MARKUP = re.compile(r"<!--.*?-->|<[^>]*>|[#|*>`_=~]+|-{2,}|\.{3,}", re.S)


def documents() -> list[tuple[str, str, str]]:
    """(doc_id, original file name, opening text) for every PDF read, from the OCR state."""
    con = sqlite3.connect(f"file:{OCR_DB}?mode=ro", uri=True)
    out = []
    for doc_id, path in con.execute("SELECT doc_id, path FROM docs WHERE status IN ('done', 'inspected')"):
        pages = [t or "" for (t,) in con.execute(
            "SELECT text FROM pages WHERE doc_id=? AND page<=3 AND status='done' ORDER BY page", (doc_id,))]
        text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", _MARKUP.sub(" ", "\n".join(pages)))).strip()
        out.append((doc_id, path.rsplit("/", 1)[-1], text[:2000]))
    con.close()
    return out


def name(model: str, filename: str, text: str) -> dict:
    user = f"ชื่อไฟล์เดิม: {filename}\nเนื้อหาช่วงต้น:\n{text}"
    body = {"model": model, "temperature": 0, "max_tokens": 400,
            "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": user}]}
    if os.environ.get("REASONING_EFFORT"):
        body["reasoning_effort"] = os.environ["REASONING_EFFORT"]
    req = urllib.request.Request(URL, json.dumps(body).encode(), {
        "Content-Type": "application/json", **({"Authorization": f"Bearer {KEY}"} if KEY else {})})
    err = ""
    for attempt in range(3):
        try:
            reply = json.load(urllib.request.urlopen(req, timeout=180))["choices"][0]["message"]
            break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            time.sleep(2 ** attempt)
    else:
        return {"error": err}
    out = reply.get("content") or ""
    m = re.search(r"\{.*\}", out, re.S)
    try:
        found = json.loads(m.group(0)).get("name") if m else None
    except ValueError:
        found = None
    if not isinstance(found, str) or not found.strip():
        return {"error": f"no name in: {out[:200]}"}
    found = re.sub(r"\s+", " ", found).strip().removesuffix(".pdf")
    if len(found) > MAX_NAME:  # cut at a space, not inside a word
        found = found[:MAX_NAME].rsplit(" ", 1)[0]
    return {"name": found}


def start_run(model: str, run_dir: Path) -> None:
    """Record what the run uses, and refuse to add answers from another model or prompt to it."""
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {"model": model, "url": URL, "prompt_sha": hashlib.sha256(PROMPT.encode()).hexdigest()[:12]}
    path = run_dir / "run.json"
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if {k: old.get(k) for k in meta} != meta:
            sys.exit(f"{run_dir} already holds a run with {old}\nuse another folder for {meta}")
        return
    path.write_text(json.dumps({**meta, "started": time.strftime("%Y-%m-%d %H:%M")}, indent=1), encoding="utf-8")
    (run_dir / "prompt.md").write_text(PROMPT, encoding="utf-8")


def load_names(path: Path) -> dict[str, dict]:
    names = {}
    try:
        for line in open(path, encoding="utf-8"):
            x = json.loads(line)
            if x.get("name"):
                names[x["doc_id"]] = x  # last good answer wins
    except FileNotFoundError:
        pass
    return names


def run(model: str, run_dir: Path, workers: int) -> None:
    start_run(model, run_dir)
    out_path = run_dir / "names.jsonl"
    done = load_names(out_path)
    docs = documents()
    todo = [d for d in docs if d[0] not in done and d[2]]
    print(f"{len(docs)} documents, {len(done)} named already, {sum(1 for d in docs if not d[2])} without text, "
          f"{len(todo)} to go, {workers} at a time", flush=True)
    t0, n, failed = time.time(), 0, 0
    with ThreadPoolExecutor(workers) as pool, open(out_path, "a", encoding="utf-8") as out:
        futures = {pool.submit(name, model, filename, text): (doc_id, filename) for doc_id, filename, text in todo}
        for f in as_completed(futures):
            doc_id, filename = futures[f]
            res = {"doc_id": doc_id, "filename": filename, **f.result()}
            out.write(json.dumps(res, ensure_ascii=False) + "\n")
            out.flush()
            n += 1
            failed += "error" in res
            if n % 500 == 0 or n == len(todo):
                rate = n / (time.time() - t0)
                print(f"{time.strftime('%H:%M:%S')} {n}/{len(todo)} | {rate * 60:.0f} docs/min | failed {failed} | "
                      f"about {(len(todo) - n) / max(rate, 1e-9) / 60:.0f} min left", flush=True)
    merge(run_dir)


# The names are used as file names: characters Windows does not allow become a dash or a space
# (ครั้งที่ 9/2566 -> ครั้งที่ 9-2566), and a name is cut at a space to fit FINAL_MAX characters,
# the original file name added to a shared name included. A model's "can't name it" is no name:
# the OCR export then uses the document's first line.
FINAL_MAX = 120
STEM_MAX = 50
_SLASHES = re.compile(r"\s*[\\/]\s*")
_NOT_ALLOWED = re.compile(r'[:*?"<>|\x00-\x1f]')
NO_NAME = ("ไม่สามารถระบุชื่อ", "ไม่สามารถตั้งชื่อ", "ไม่มีเนื้อหาในเอกสาร")


def tidy(name: str) -> str:
    name = _NOT_ALLOWED.sub(" ", _SLASHES.sub("-", name))
    return re.sub(r"\s+", " ", name).strip(" .-")


def fit(name: str, limit: int) -> str:
    """The name in at most limit characters: its start (what the document is) and its end (the
    date or number that tells a series apart), with … for the middle, cut at spaces where it can."""
    if len(name) <= limit:
        return name
    words, tail = name.split(" "), []
    while words and len(" ".join([words[-1], *tail])) <= limit // 3:
        tail.insert(0, words.pop())
    room = limit - len(" ".join(tail)) - 2  # for "… "
    head = " ".join(words)[:room + 1]
    if " " in head and len(head.rsplit(" ", 1)[0]) >= room // 2:
        head = head.rsplit(" ", 1)[0]
    return f"{head[:room].rstrip(' -,(')}… {' '.join(tail)}".strip()


def merge(run_dir: Path) -> None:
    names = {d: {**x, "name": fit(tidy(x["name"]), FINAL_MAX)} for d, x in load_names(run_dir / "names.jsonl").items()
             if not any(p in x["name"] for p in NO_NAME)}
    count = Counter(x["name"] for x in names.values())  # after cutting: names that differed past it collide
    final = Path("final") / "alt_names.csv"
    final.parent.mkdir(exist_ok=True)
    seen: Counter = Counter()
    with open(final, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["doc_id", "alt_name", "filename"])
        for doc_id, x in sorted(names.items()):
            alt = x["name"]
            if count[alt] > 1:  # the same document posted twice, or a series the model couldn't tell apart
                suffix = f" ({fit(tidy(Path(x['filename']).stem), STEM_MAX)})"
                alt = fit(alt, FINAL_MAX - len(suffix)) + suffix
                seen[alt] += 1
                if seen[alt] > 1:
                    alt = f"{alt} [{doc_id[:6]}]"
            w.writerow([doc_id, alt, x["filename"]])
    dup = {k: v for k, v in count.items() if v > 1}
    print(f"\nwrote {final}: {len(names)} names; {sum(dup.values())} documents shared {len(dup)} names "
          f"and got their file name added")


if __name__ == "__main__":
    if sys.argv[1] == "--merge":
        merge(Path(sys.argv[2]))
    else:
        run(sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 16)
