"""Full classification: the LLM labels every document except Green Office and procurement, which keep
their rule labels (correct in every check). Resumable: documents already answered are skipped.

On pgx, in ~/senate/classify. Every run keeps its own folder under runs/, so runs never mix:
    OCR_DB=~/senate/ocr_out/ocr.db LLM_KEY=... REASONING_EFFORT=high \
        python3 classify_all.py ptm-oss-120b labels_all.tsv runs/oss120b [workers]
    OCR_DB=~/senate/ocr_out/ocr.db LLM_URL=http://localhost:8005/v1 \
        python3 classify_all.py gemma4 labels_all.tsv runs/gemma4 16
    python3 classify_all.py --merge labels_all.tsv runs/gemma4      # rebuild the CSV only
A run folder holds llm_labels.jsonl (one answer per document), doc_labels_final.csv, run.json (model,
endpoint, prompt hash) and the prompt the model was given.
"""
import csv
import hashlib
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from llm_pilot import SYSTEM, URL, classify, reference

RULE_KEPT = {"green_office", "procurement"}


def start_run(model, run_dir):
    """Record what the run uses, and refuse to add answers from another model or prompt to it."""
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {"model": model, "url": URL, "reasoning_effort": os.environ.get("REASONING_EFFORT") or None,
            "prompt_sha": hashlib.sha256(SYSTEM.encode()).hexdigest()[:12]}
    path = run_dir / "run.json"
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if {k: old.get(k) for k in meta} != meta:
            sys.exit(f"{run_dir} already holds a run with {old}\nuse another folder for {meta}")
        return
    path.write_text(json.dumps({**meta, "started": time.strftime("%Y-%m-%d %H:%M")}, indent=1), encoding="utf-8")
    (run_dir / "prompt_th.md").write_text(SYSTEM, encoding="utf-8")


def load_rows(path):
    return list(csv.DictReader(open(path, encoding="utf-8"), delimiter="\t"))


def load_answers(path):
    answers = {}
    try:
        for line in open(path, encoding="utf-8"):
            x = json.loads(line)
            if (x.get("llm") or {}).get("label"):
                answers[x["doc_id"]] = x  # last good answer wins
    except FileNotFoundError:
        pass
    return answers


def run(model, rows_path, run_dir, workers):
    start_run(model, run_dir)
    out_path = run_dir / "llm_labels.jsonl"
    rows = load_rows(rows_path)
    done = load_answers(out_path)
    todo = [r for r in rows if r["doc_class"] not in RULE_KEPT and r["doc_id"] not in done]
    total = sum(1 for r in rows if r["doc_class"] not in RULE_KEPT)
    print(f"{total} documents for the LLM, {len(done)} already done, {len(todo)} to go, {workers} at a time", flush=True)
    t0, n, failed = time.time(), 0, 0

    def one(r):
        res = classify(model, r)
        if not (res.get("llm") or {}).get("label"):  # error or unparseable: ask once more
            res = classify(model, r)
        return res

    with ThreadPoolExecutor(workers) as pool, open(out_path, "a", encoding="utf-8") as out:
        futures = [pool.submit(one, r) for r in todo]
        for f in as_completed(futures):
            res = f.result()
            out.write(json.dumps(res, ensure_ascii=False) + "\n")
            out.flush()
            n += 1
            failed += not (res.get("llm") or {}).get("label")
            if n % 250 == 0 or n == len(todo):
                rate = n / (time.time() - t0)
                print(f"{time.strftime('%H:%M:%S')} {n}/{len(todo)} | {rate*60:.0f} docs/min | "
                      f"failed {failed} | about {(len(todo)-n)/max(rate,1e-9)/60:.0f} min left", flush=True)
    merge(rows_path, run_dir)


def merge(rows_path, run_dir):
    rows = load_rows(rows_path)
    answers = load_answers(run_dir / "llm_labels.jsonl")
    cols = ["doc_id", "label", "source", "closest_label", "other_type", "confidence", "reason",
            "rule_label", "rule_doc_type", "disagree", "cluster", "filename"]
    counts, sources, flips = Counter(), Counter(), Counter()
    final = run_dir / "doc_labels_final.csv"
    with open(final, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            rule = reference(r)
            a = answers.get(r["doc_id"])
            if r["doc_class"] in RULE_KEPT:
                y, source = {"label": rule}, "rule"
            elif a:
                y = a["llm"]
                source = "no_text" if a.get("finish") == "skipped" else "llm"
            else:
                y, source = {"label": rule}, "rule_fallback"  # the LLM never answered
            label = y["label"]
            disagree = "yes" if source == "llm" and label != rule else ""
            w.writerow([r["doc_id"], label, source, y.get("closest_label") or "", y.get("other_type") or "",
                        y.get("confidence") or "", y.get("reason") or "", rule, r["doc_class"], disagree,
                        r["cluster"], r["filename"]])
            counts[label] += 1
            sources[source] += 1
            if disagree:
                flips[(rule, label)] += 1
    print(f"\nwrote {final}")
    print("labels:", dict(counts.most_common()))
    print("source:", dict(sources))
    print(f"LLM disagrees with rules on {sum(flips.values())} documents; most common (rule -> llm):")
    for (a, b), n in flips.most_common(12):
        print(f"  {n:5}  {a} -> {b}")


if __name__ == "__main__":
    if sys.argv[1] == "--merge":
        merge(sys.argv[2], Path(sys.argv[3]))
    else:
        run(sys.argv[1], sys.argv[2], Path(sys.argv[3]), int(sys.argv[4]) if len(sys.argv) > 4 else 8)
