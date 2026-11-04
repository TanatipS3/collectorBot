"""Feature table for every PDF, and a stratified 10% sample to read (read-only on both DBs)."""
import csv, random, re, sqlite3
from collections import defaultdict
from urllib.parse import unquote
ocr = sqlite3.connect("file:ocr_out/ocr.db?mode=ro", uri=True)
col = sqlite3.connect("file:state/collector.db?mode=ro", uri=True)
ref = {}
for path, referrer, note in col.execute("SELECT path, referrer, note FROM urls WHERE kind=\"file\" AND status=\"done\" AND path IS NOT NULL"):
    if path not in ref or not (note or "").startswith("duplicate"):
        ref[path] = unquote(referrer or "")
first = defaultdict(list)
for doc_id, page, text in ocr.execute("SELECT doc_id, page, text FROM pages WHERE page <= 2 AND status = \"done\" ORDER BY doc_id, page"):
    first[doc_id].append(text or "")
def clean(t):
    t = re.sub(r"<!--.*?-->|[#|*>`_=~]+|-{2,}|\.{3,}", " ", t)
    return re.sub(r"\s+", " ", t).strip()
def cluster(path, found_on):
    host = path.split("/")[0]
    m = re.search(r"/view/(\d+)/([^/?]+)", found_on)
    if m: return f"p{m.group(1)}/{m.group(2)}"
    if host == "document.senate.go.th": return "document/" + path.split("/")[1]
    if "book_shelf" in found_on: return "book_shelf/" + found_on.split("book_shelf/")[1].split("/")[0]
    m = re.search(r"/portals/(\d+)/", path)
    return f"p{m.group(1)}/(no section)" if m else host
rows = []
for doc_id, path, pages, ocr_pages in ocr.execute("SELECT doc_id, path, pages, ocr_pages FROM docs WHERE status != \"skipped\""):
    f = ref.get(path, ""); texts = first.get(doc_id, [])
    snippet = clean(texts[0]) if texts else ""
    if len(snippet) < 80 and len(texts) > 1: snippet = (snippet + " / " + clean(texts[1])).strip(" /")
    m = re.search(r"/portals/(\d+)/", path)
    rows.append({"doc_id": doc_id, "cluster": cluster(path, f), "portal": m.group(1) if m else path.split("/")[0],
                 "filename": path.rsplit("/", 1)[-1], "folder": "/".join(path.split("/")[1:-1]), "pages": pages or 0,
                 "ocr_pages": ocr_pages or 0, "found_on": f, "snippet": snippet[:400]})
with open("classify/features.tsv", "w", encoding="utf-8", newline="") as fh:
    w = csv.DictWriter(fh, rows[0].keys(), delimiter="\t"); w.writeheader(); w.writerows(rows)
by = defaultdict(list)
for r in rows: by[r["cluster"]].append(r)
rng = random.Random(42); sample = []
for c, rs in by.items():
    k = min(len(rs), max(1, round(0.10 * len(rs)), 2 if len(rs) >= 5 else 1))
    sample += rng.sample(rs, k)
with open("classify/sample.tsv", "w", encoding="utf-8", newline="") as fh:
    w = csv.DictWriter(fh, rows[0].keys(), delimiter="\t"); w.writeheader()
    w.writerows(sorted(sample, key=lambda r: (-len(by[r["cluster"]]), r["cluster"], r["filename"])))
print("docs", len(rows), "clusters", len(by), "sample", len(sample), f"({100*len(sample)/len(rows):.1f}%)",
      "no text", sum(1 for r in rows if not r["snippet"]))
