"""Pilot: classify a stratified sample with an LLM and compare with the rule labels.

python llm_pilot.py <model> <labels_all.tsv> <out.jsonl> [per_label] [workers]
Needs TOKENMIND_API_KEY (or OPENAI-style endpoint via LLM_URL / LLM_KEY).
"""
import csv
import json
import os
import random
import re
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

URL = os.environ.get("LLM_URL", "https://tokenmind.pathumma.in.th/v1") + "/chat/completions"
KEY = os.environ.get("LLM_KEY") or os.environ.get("TOKENMIND_API_KEY", "")
SYSTEM = (Path(__file__).parent / "prompt_th.md").read_text(encoding="utf-8")
LABELS = {"law", "bill", "plenary", "committee", "publication", "procurement", "office_admin", "green_office", "other"}

# Rule label (doc_type) -> the 8 categories, splitting bills from sittings.
REF = {
    "law_text": "law", "court_decision": "law", "legal_knowledge": "law",
    "bill_documents": "bill", "plenary_sitting": "plenary", "interpellation": "plenary",
    "committee_report": "committee", "committee_news": "committee", "committee_activity": "committee",
    "academic_brief": "publication", "academic_article": "publication", "senate_publication": "publication",
    "news_digest": "publication", "external_reference": "publication", "procurement": "procurement",
    "budget_finance": "office_admin", "strategy_performance": "office_admin", "hr": "office_admin",
    "governance_transparency": "office_admin", "internal_km": "office_admin", "general_notice": "office_admin",
    "form_template": "office_admin", "other": "other", "green_office": "green_office",
}


def reference(r):
    if r["doc_class"] == "academic_article" and r["cluster"].startswith("p93/"):
        return "law"
    return REF[r["doc_class"]]


def parse_json(text):
    text = re.sub(r"```(json)?", "", text or "")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


OCR_DB = os.environ.get("OCR_DB")  # on pgx: the OCR state, for more page text than the snippet


def first_pages(doc_id, fallback):
    if not OCR_DB:
        return fallback
    import sqlite3
    con = sqlite3.connect(f"file:{OCR_DB}?mode=ro", uri=True)
    pages = con.execute(
        "SELECT page, text FROM pages WHERE doc_id=? AND page<=5 AND status='done' ORDER BY page", (doc_id,)).fetchall()
    con.close()
    texts = [t or "" for p, t in pages if p <= 2]
    if len(re.sub(r"\s", "", "".join(texts))) < 200:  # only a cover so far: read on to page 5
        texts = [t or "" for p, t in pages]
    text = re.sub(r"<!--.*?-->|[#|*>`_=~]+|-{2,}|\.{3,}", " ", " / ".join(texts))
    return re.sub(r"\s+", " ", text).strip() or fallback


def classify(model, r):
    text = first_pages(r["doc_id"], r["snippet"])
    user = (f"ชื่อไฟล์: {r['filename']}\nหมวดบนเว็บไซต์: {r['cluster'].split('/', 1)[-1]}\n"
            f"เนื้อหาช่วงต้น: {text[:1500]}")
    if len(re.sub(r"\s", "", text)) < 30:  # nothing to read: no need to ask a model
        return {"doc_id": r["doc_id"], "ref": reference(r), "doc_type": r["doc_class"], "cluster": r["cluster"],
                "filename": r["filename"], "snippet": r["snippet"][:200], "finish": "skipped", "seconds": 0,
                "llm": {"label": "other", "closest_label": None, "other_type": "อ่านไม่ได้/แทบไม่มีข้อความ",
                        "confidence": "high", "reason": "มีข้อความน้อยกว่า 30 ตัวอักษร"}}
    body = {"model": model, "temperature": 0, "max_tokens": 2000,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}
    if os.environ.get("REASONING_EFFORT"):
        body["reasoning_effort"] = os.environ["REASONING_EFFORT"]
    req = urllib.request.Request(URL, json.dumps(body).encode(), {
        "Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    t = time.time()
    for attempt in range(3):
        try:
            d = json.load(urllib.request.urlopen(req, timeout=180))
            break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            time.sleep(2 ** attempt)
    else:
        return {"doc_id": r["doc_id"], "error": err}
    c = d["choices"][0]
    out = parse_json(c["message"].get("content"))
    if out and "category" in out:  # numbered answers -> label names
        num = {1: "green_office", 2: "procurement", 3: "bill", 4: "committee", 5: "publication",
               6: "plenary", 7: "law", 8: "office_admin", 9: "publication", 0: "other"}
        try:
            out["label"] = num[int(out["category"])]
            if out.get("closest") not in (None, "", "null"):
                out["closest_label"] = num[int(out["closest"])]
        except (ValueError, KeyError, TypeError):
            out["label"] = None
    return {"doc_id": r["doc_id"], "ref": reference(r), "doc_type": r["doc_class"], "cluster": r["cluster"],
            "filename": r["filename"], "snippet": r["snippet"][:200], "llm": out,
            "finish": c.get("finish_reason"), "seconds": round(time.time() - t, 1), "usage": d.get("usage")}


def main():
    model, src, dst = sys.argv[1:4]
    per = int(sys.argv[4]) if len(sys.argv) > 4 else 20
    workers = int(sys.argv[5]) if len(sys.argv) > 5 else 8
    rows = list(csv.DictReader(open(src, encoding="utf-8"), delimiter="\t"))
    by = defaultdict(list)
    for r in rows:
        by[reference(r)].append(r)
    rng = random.Random(9)
    pick = [x for lab in sorted(by) for x in rng.sample(by[lab], min(per, len(by[lab])))]
    t = time.time()
    with ThreadPoolExecutor(workers) as pool, open(dst, "w", encoding="utf-8") as f:
        for res in pool.map(lambda r: classify(model, r), pick):
            f.write(json.dumps(res, ensure_ascii=False) + "\n")
    res = [json.loads(l) for l in open(dst, encoding="utf-8")]
    ok = [x for x in res if x.get("llm") and x["llm"].get("label") in LABELS]
    agree = sum(x["llm"]["label"] == x["ref"] for x in ok)
    print(f"{model}: {len(res)} docs in {time.time()-t:.0f}s | parsed {len(ok)} | agree with rules {agree}/{len(ok)} "
          f"({100*agree/max(len(ok),1):.0f}%) | errors {sum('error' in x for x in res)} | "
          f"avg {sum(x.get('seconds',0) for x in res)/len(res):.1f}s | "
          f"avg tokens in/out {sum((x.get('usage') or {}).get('prompt_tokens',0) for x in res)/len(res):.0f}/"
          f"{sum((x.get('usage') or {}).get('completion_tokens',0) for x in res)/len(res):.0f}")
    per_lab = defaultdict(lambda: [0, 0])
    for x in ok:
        per_lab[x["ref"]][1] += 1
        per_lab[x["ref"]][0] += x["llm"]["label"] == x["ref"]
    print("  per reference label:", {k: f"{a}/{n}" for k, (a, n) in sorted(per_lab.items())})


if __name__ == "__main__":
    main()
