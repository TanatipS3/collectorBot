"""Compare two classification runs: how often they agree, where they differ, and a CSV to review.

python3 compare_runs.py runs/oss120b runs/gemma4
Writes runs/compare_oss120b_vs_gemma4.csv, one row per document the two runs label differently.
Green Office and procurement keep their rule labels in every run, so only the other documents count.
"""
import csv
import sys
from collections import Counter
from pathlib import Path


def load(run_dir):
    with open(Path(run_dir) / "doc_labels_final.csv", encoding="utf-8-sig") as f:
        return {r["doc_id"]: r for r in csv.DictReader(f)}


def main(a_dir, b_dir):
    a, b = load(a_dir), load(b_dir)
    an, bn = Path(a_dir).name, Path(b_dir).name
    docs = [d for d in a if d in b and a[d]["source"] != "rule"]
    same = [d for d in docs if a[d]["label"] == b[d]["label"]]
    print(f"{an} vs {bn}: agree on {len(same):,} of {len(docs):,} documents ({100 * len(same) / len(docs):.1f}%)")
    print(f"  sources {an}: {dict(Counter(a[d]['source'] for d in docs))}")
    print(f"  sources {bn}: {dict(Counter(b[d]['source'] for d in docs))}")
    for name, run in ((an, a), (bn, b)):
        print(f"  {name} differs from the rule labels on {sum(run[d]['label'] != run[d]['rule_label'] for d in docs):,}")

    print(f"\nper {an} label: agree / documents")
    per = Counter(a[d]["label"] for d in docs)
    for label, n in per.most_common():
        k = sum(1 for d in same if a[d]["label"] == label)
        print(f"  {label:13} {k:6,} / {n:6,}  ({100 * k / n:.0f}%)")

    diff = [d for d in docs if a[d]["label"] != b[d]["label"]]
    print(f"\nmost common differences ({an} -> {bn}):")
    for (x, y), n in Counter((a[d]["label"], b[d]["label"]) for d in diff).most_common(15):
        print(f"  {n:5}  {x} -> {y}")
    print("\nwhen all three agree (both runs and the rules):",
          f"{sum(a[d]['label'] == a[d]['rule_label'] for d in same):,} documents")

    out = Path(a_dir).parent / f"compare_{an}_vs_{bn}.csv"
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["doc_id", "filename", "cluster", "rule_label", f"{an}_label", f"{bn}_label",
                    f"{an}_confidence", f"{bn}_confidence", f"{an}_reason", f"{bn}_reason"])
        for d in sorted(diff, key=lambda d: (a[d]["label"], b[d]["label"], a[d]["cluster"])):
            w.writerow([d, a[d]["filename"], a[d]["cluster"], a[d]["rule_label"], a[d]["label"], b[d]["label"],
                        a[d]["confidence"], b[d]["confidence"], a[d]["reason"], b[d]["reason"]])
    print(f"\nwrote {len(diff):,} differences to {out}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
