"""Copy the OCR text of every document (text/<doc_id>.md) under a readable file name: the name an
LLM gave it (mapping.csv's alt_file_name) or its original file name.

    python -m ocr.rename_text --dry-run            # show what it would do, change nothing
    python -m ocr.rename_text                      # alt names, copied into ocr_out/named/
    python -m ocr.rename_text --name original      # the original file names instead
    python -m ocr.rename_text --move               # move instead of copy (see below)

A copy is the default: mapping.csv's ocr_file points at text/<doc_id>.md, and `python -m ocr export`
writes any of those that are missing again. Writes <dest>/renamed.csv (doc_id, file, original file
name, label) so every renamed file can be traced back to its document.
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
from pathlib import Path

NOT_ALLOWED = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MAX_NAME = 150  # characters before ".md": Windows paths stop at 260


def safe(name: str) -> str:
    """The name as a file name both Windows and Linux accept."""
    name = re.sub(r"\s+", " ", NOT_ALLOWED.sub("-", name)).strip(" .")[:MAX_NAME].rstrip(" .")
    return "_" + name if name.split(".")[0].upper() in RESERVED else name


def documents(mapping: Path, use: str) -> list[dict]:
    """Every document with OCR text, once (a file served from several URLs has a row for each),
    with the name to give it. A document without an alt name keeps its original name."""
    docs, seen = [], set()
    with open(mapping, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if not r["ocr_file"] or r["doc_id"] in seen:
                continue
            seen.add(r["doc_id"])
            original = safe(Path(r["og_filename"]).stem)
            name = safe(r["alt_file_name"]) if use == "alt" else original
            docs.append({"doc_id": r["doc_id"], "name": name or original or r["doc_id"],
                         "og_filename": r["og_filename"], "label": r["label"]})
    return docs


def plan(docs: list[dict]) -> list[dict]:
    """A file name for each, unique without regard to case (Windows ignores it): a name taken
    already (original names repeat: 1.pdf, 18.pdf) gets the document's id added."""
    taken: set[str] = set()
    for d in docs:
        target = d["name"]
        for candidate in (target, f"{target} [{d['doc_id'][:8]}]", f"{target} [{d['doc_id']}]"):
            if candidate.lower() not in taken:
                target = candidate
                break
        taken.add(target.lower())
        d["target"] = target + ".md"
    return docs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ocr.rename_text", description=__doc__.split("\n\n")[0])
    ap.add_argument("--mapping", type=Path, default=Path("ocr_out/mapping.csv"))
    ap.add_argument("--src", type=Path, default=Path("ocr_out/text"), help="where the <doc_id>.md files are")
    ap.add_argument("--dest", type=Path, default=Path("ocr_out/named"))
    ap.add_argument("--name", choices=("alt", "original"), default="alt",
                    help="alt: the LLM name (default); original: the file's own name")
    ap.add_argument("--move", action="store_true", help="move the files instead of copying them")
    ap.add_argument("--dry-run", action="store_true", help="only show what would be done")
    args = ap.parse_args(argv)

    docs = plan(documents(args.mapping, args.name))
    missing = [d for d in docs if not (args.src / f"{d['doc_id']}.md").is_file()]
    renamed = sum(d["target"] != f"{d['doc_id']}.md" for d in docs)
    print(f"{len(docs):,} documents with OCR text | {len(missing):,} of their files not in {args.src} | "
          f"{sum('[' in d['target'] for d in docs):,} names shared, the document id added")
    if args.dry_run:
        for d in docs[:20]:
            print(f"  {d['doc_id']}.md -> {d['target']}")
        print(f"dry run: would {'move' if args.move else 'copy'} {len(docs) - len(missing):,} files "
              f"({renamed:,} renamed) into {args.dest}")
        return 0

    args.dest.mkdir(parents=True, exist_ok=True)
    done = 0
    with open(args.dest / "renamed.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["doc_id", "file", "og_filename", "label"])
        for d in docs:
            source = args.src / f"{d['doc_id']}.md"
            if not source.is_file():
                continue
            (shutil.move if args.move else shutil.copy2)(source, args.dest / d["target"])
            w.writerow([d["doc_id"], d["target"], d["og_filename"], d["label"]])
            done += 1
    print(f"{'moved' if args.move else 'copied'} {done:,} files into {args.dest} "
          f"(list: {args.dest / 'renamed.csv'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
