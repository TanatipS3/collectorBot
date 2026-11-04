"""Final labels from two classification runs: where the runs agree their label stands; where they
differ, the rules below decide, and every row records which rule did.

python3 final_labels.py runs/oss120b runs/gemma4      (OCR_DB defaults to ../ocr_out/ocr.db)
Writes final/doc_labels_final.csv (every document) and final/review.csv (the rows worth a look).

Decided by, in order:
  rule              Green Office and procurement: the rule labels, correct in every check
  cabinet_series    the Legal Bureau's cabinet resolution summaries are one series: law
  no_text           nothing to read; stays other and goes on the review list
  second_run        the first run had no usable answer (other, failed, or a cover page only); the
                    second run's prompt fixed those cases
  both_runs         the two runs agree
  draft_title       bill vs law: a document about one specific draft not yet enacted (its year still
                    blank, "พ.ศ. ....") or from the Senate's bill section is bill, else law
  group             a difference pattern settled by reading samples (see GROUPS)
  rule_tiebreak     the rule label matches one of the two runs
  unconfirmed       none of the above: the second run's label, on the review list
"""
import csv
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

# (first run, second run) -> label, from reading samples of each pattern.
GROUPS = {
    ("publication", "law"): "law",          # Legal Bureau articles and interviews: law by the prompt
    ("committee", "bill"): "bill",          # committee news and studies of one specific bill
    ("plenary", "publication"): "publication",  # outside bodies' reports and academic summaries
    ("committee", "plenary"): "plenary",    # Senate meeting papers carrying a committee's report
}
CABINET = "สรุปมติคณะรัฐมนตรีที่เกี่ยวข้อง"
# A draft not yet enacted is named with its year left blank: ร่างพระราชบัญญัติ... พ.ศ. ....
DRAFT = re.compile(r"ร่าง\s*(พระราชบัญญัติ|พ\s*\.\s*ร\s*\.\s*บ|พระราชกำหนด|พระราชกฤษฎีกา|รัฐธรรมนูญ|ข้อบังคับ)"
                   r".{0,150}?(พ\s*\.\s*ศ\s*\.|พุทธศักราช)\s*[.…]{2,}")


def load(run_dir):
    with open(Path(run_dir) / "doc_labels_final.csv", encoding="utf-8-sig") as f:
        return {r["doc_id"]: r for r in csv.DictReader(f)}


def opening(db, doc_id, chars=600):
    t = " ".join(t or "" for (t,) in db.execute(
        "SELECT text FROM pages WHERE doc_id=? AND page<=2 ORDER BY page", (doc_id,)))
    return re.sub(r"\s+", " ", t)[:chars]


def decide(a, b, text):
    """(label, decided_by, review) for one document; a and b are its rows in the two runs."""
    x, y = a["label"], b["label"]
    if a["source"] == "rule":
        return x, "rule", False
    if CABINET in text:
        return "law", "cabinet_series", False
    if b["source"] == "no_text":
        return "other", "no_text", True
    if x == "other" or a["source"] in ("no_text", "rule_fallback"):
        return y, "second_run", y == "other"
    if x == y:
        return x, "both_runs", x == "other"
    if {x, y} == {"bill", "law"}:  # document/ is the Senate's own bill consideration section
        bill = a["cluster"].startswith("document/") or DRAFT.search(text)
        return ("bill" if bill else "law"), "draft_title", False
    if (x, y) in GROUPS:
        return GROUPS[(x, y)], "group", False
    if a["rule_label"] in (x, y):
        return a["rule_label"], "rule_tiebreak", False
    return y, "unconfirmed", True


def main(a_dir, b_dir):
    a, b = load(a_dir), load(b_dir)
    an, bn = Path(a_dir).name, Path(b_dir).name
    db = sqlite3.connect(f"file:{os.environ.get('OCR_DB', '../ocr_out/ocr.db')}?mode=ro", uri=True)
    out = Path("final")
    out.mkdir(exist_ok=True)
    cols = ["doc_id", "label", "decided_by", "review", f"{an}_label", f"{bn}_label", "rule_label",
            "other_type", "reason", "cluster", "filename"]
    rows = []
    for d in a:
        ra, rb = a[d], b[d]
        text = opening(db, d) if ra["source"] != "rule" else ""
        label, by, review = decide(ra, rb, text)
        chosen = rb if label == rb["label"] else ra
        rows.append([d, label, by, "yes" if review else "", ra["label"], rb["label"], ra["rule_label"],
                     chosen["other_type"], chosen["reason"], ra["cluster"], ra["filename"]])
    for name, keep in (("doc_labels_final.csv", lambda r: True), ("review.csv", lambda r: r[3])):
        with open(out / name, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(r for r in rows if keep(r))
    print(f"{len(rows):,} documents -> {out / 'doc_labels_final.csv'}; "
          f"{sum(1 for r in rows if r[3]):,} on the review list -> {out / 'review.csv'}")
    print("labels:    ", dict(Counter(r[1] for r in rows).most_common()))
    print("decided by:", dict(Counter(r[2] for r in rows).most_common()))
    print("changed from the first run:", sum(r[1] != r[4] for r in rows),
          "| from the second run:", sum(r[1] != r[5] for r in rows))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
