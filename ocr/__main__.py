"""Command line: python -m ocr {run,fix,rescue,reread,recheck,status,export,probe}"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

from collector.__main__ import _interrupt, _single_instance

from .config import api_key, load_config
from .engine import Engine, OcrClient
from .store import OcrStore

log = logging.getLogger("ocr")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(prog="ocr", description="OCR the documents the crawl downloaded.")
    ap.add_argument("-c", "--config", default="ocr.toml", help="config file (default: ocr.toml)")
    sub = ap.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="inspect text layers and OCR the pages that need it "
                                     "(resumes where the last run stopped)")
    run.add_argument("--limit", type=int, help="stop after OCR'ing this many pages (for test runs)")
    run.add_argument("--workers", type=int, help="OCR requests in flight at once (overrides config)")
    fix = sub.add_parser("fix", help="bring earlier output up to the current rules (repair text layers, "
                                     "collapse repeated phrases, queue badly read pages), then OCR "
                                     "what was queued")
    fix.add_argument("--dry-run", action="store_true", help="only count what would change")
    fix.add_argument("--workers", type=int, help="OCR requests in flight at once (overrides config)")
    rescue = sub.add_parser("rescue", help="read the pages still in doubt (cut off, looping, prompt echoed) "
                                           "again in strips, with the primary engine only")
    rescue.add_argument("--dry-run", action="store_true", help="only count what would be read")
    rescue.add_argument("--workers", type=int, help="pages read at once (overrides config)")
    reread = sub.add_parser("reread", help="start this output (output_dir) from another run's OCR state and "
                                           "read the pages that run took from the text layer again by OCR; "
                                           "the other run is left as it is")
    reread.add_argument("--source", default="ocr_out/ocr.db", help="the other run's state (default: ocr_out/ocr.db)")
    reread.add_argument("--dry-run", action="store_true", help="only count what would be read")
    reread.add_argument("--workers", type=int, help="OCR requests in flight at once (overrides config)")
    recheck = sub.add_parser("recheck", help="drop flags the stored text shows were wrong (a loop that is only "
                                             "a table's blank rows), reading nothing again; then rewrite the "
                                             "text files, flagged_pages.csv and mapping.csv")
    recheck.add_argument("--dry-run", action="store_true", help="only count what would change")
    sub.add_parser("status", help="show progress")
    sub.add_parser("export", help="rewrite mapping.csv and any missing text files")
    probe = sub.add_parser("probe", help="send one page to each OCR engine and show what comes back")
    probe.add_argument("--pdf", help="PDF to take the page from (default: one waiting for OCR)")
    probe.add_argument("--page", type=int, default=1)

    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if getattr(args, "workers", None):
        cfg.workers = args.workers
    store = OcrStore(cfg.db_path)
    try:
        if args.command in ("run", "fix", "rescue", "reread"):
            return _run(args, cfg, store)
        if args.command == "status":
            return _status(store)
        if args.command == "probe":
            return _probe(args, cfg, store)
        if args.command == "recheck":
            return _recheck(args, cfg, store)
        from .runner import Runner
        runner = Runner(cfg, store, _client(cfg))
        runner.sync()
        print(f"rewrote {runner.write_missing_docs()} missing text files; "
              f"mapping.csv: {runner.export()} rows -> {cfg.mapping_path}")
        return 0
    finally:
        store.close()


def _engines(cfg) -> list[Engine]:
    engines = [Engine("primary", cfg.primary_url, cfg.primary_model, api_key(cfg.primary_key_env), cfg.timeout)]
    if cfg.backup_url:
        key = api_key(cfg.backup_key_env)
        if key or not cfg.backup_key_env:
            engines.append(Engine("backup", cfg.backup_url, cfg.backup_model, key, cfg.timeout))
        else:
            log.warning("no backup OCR engine: %s is not set (environment or .env)", cfg.backup_key_env)
    return engines


def _client(cfg) -> OcrClient:
    engines = _engines(cfg)
    if len(engines) == 1:
        cfg.backup_workers = 0  # no backup to give a share to
    return OcrClient(engines, attempts=cfg.attempts, max_tokens=cfg.max_tokens,
                     lanes={"primary": cfg.workers, "backup": cfg.backup_workers})


def _run(args, cfg, store) -> int:
    from .runner import Runner

    lock = _single_instance(cfg.lock_path)
    if lock is None:
        print(f"An OCR run is already going (lock: {cfg.lock_path}). "
              "Use `python -m ocr status` to watch it.")
        return 1
    signal.signal(signal.SIGTERM, _interrupt)  # `kill`: stop like Ctrl+C
    _setup_logging(cfg)
    try:
        if args.command == "rescue":
            # The pgx model alone, each reading watched and stopped once it is stuck; a part still
            # in doubt is read again in smaller pieces rather than once more whole.
            primary = _engines(cfg)[0]
            client = OcrClient([primary], attempts=cfg.attempts, max_tokens=cfg.max_tokens, watch=True,
                               read_again=False)
            changes = Runner(cfg, store, client).rescue(dry_run=args.dry_run)
            log.info("rescue %s: %s", "would read" if args.dry_run else "done",
                     ", ".join(f"{n:,} {k}" for k, n in sorted(changes.items())) or "nothing")
            return 0
        runner = Runner(cfg, store, _client(cfg))
        if args.command in ("fix", "reread"):
            if args.command == "fix":
                changes = runner.fix(dry_run=args.dry_run)
            else:
                changes = runner.reread_text_layer(Path(args.source), dry_run=args.dry_run)
            verb = "would change" if args.dry_run else "changed"
            log.info("%s %s: %s", args.command, verb,
                     ", ".join(f"{n:,} {k}" for k, n in sorted(changes.items())) or "nothing")
            if args.dry_run:
                return 0
        runner.run(limit=getattr(args, "limit", None))
    except KeyboardInterrupt:
        # Second Ctrl+C. Every finished page is committed; pages in flight go back in the queue
        # at the next start. Exit now rather than wait for the OCR requests still open.
        log.info("stopped")
        logging.shutdown()
        store.close()
        os._exit(130)
    return 0


def _recheck(args, cfg, store) -> int:
    from .runner import Runner

    lock = None if args.dry_run else _single_instance(cfg.lock_path)
    if not args.dry_run and lock is None:
        print(f"An OCR run is going (lock: {cfg.lock_path}): recheck once it has finished.")
        return 1
    changes = Runner(cfg, store, None).recheck(dry_run=args.dry_run)
    print(f"recheck {'would change' if args.dry_run else 'changed'}: "
          + (", ".join(f"{n:,} {k}" for k, n in sorted(changes.items())) or "nothing"))
    if not args.dry_run:
        print(f"rewrote the changed text files, flagged_pages.csv and mapping.csv in {cfg.out}")
    return 0


def _status(store) -> int:
    from .runner import progress_line

    c = store.counts()
    if not c["docs"]:
        print("Nothing yet: run `python -m ocr run`.")
        return 0
    print("documents: " + ", ".join(f"{n:,} {s}" for s, n in sorted(c["docs"].items())))
    pages = c["pages"]
    print(f"pages:     {pages.get(('text', 'done'), 0):,} from the text layer, "
          + ", ".join(f"{n:,} OCR {s}" for (m, s), n in sorted(pages.items()) if m == "ocr"))
    if c["engines"]:
        print("read by:   " + ", ".join(f"{n:,} {e}" for e, n in c["engines"].items()))
    print(progress_line(store))
    failures = store.recent_failures()
    if failures:
        print("\nrecent failures:")
        for f in failures:
            print(f"  page {f['page']} of {f['path']}: {(f['error'] or '')[:150]}")
    return 0


def _probe(args, cfg, store) -> int:
    from . import pdf

    path, page = args.pdf, args.page
    if not path:
        row = store.db.execute(
            "SELECT d.path, p.page FROM pages p JOIN docs d USING (doc_id)"
            " WHERE p.status='pending' ORDER BY p.prio, p.doc_rank LIMIT 1").fetchone()
        if row is None:
            print("Nothing is waiting for OCR yet: pass --pdf FILE (and --page N).")
            return 1
        path, page = os.path.join(cfg.downloads_dir, row["path"]), row["page"]
    image = pdf.render(path, page, cfg.render_px)
    print(f"page {page} of {path}: {len(image) / 1024:.0f} KB image, {cfg.render_px} px\n")
    ok = True
    for engine in _engines(cfg):
        print(f"== {engine.name}: {engine.model} at {engine.url}")
        started = time.monotonic()
        try:
            out = engine.read(image, 0.0, cfg.max_tokens)
        except Exception as exc:
            print(f"   FAILED: {exc}\n")
            ok = False
            continue
        from .engine import assess
        suspect, notes = assess(out)
        print(f"   {time.monotonic() - started:.1f} s, {len(out['text'])} characters, "
              f"{out['tokens_out']} tokens, finish={out['finish']}, suspect={suspect}, notes={notes}")
        print("   " + out["text"][:600].replace("\n", "\n   ") + "\n")
    return 0 if ok else 1


def _setup_logging(cfg) -> None:
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("ocr")
    root.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    logfile = logging.FileHandler(cfg.log_path, encoding="utf-8")
    logfile.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
    root.addHandler(console)
    root.addHandler(logfile)


if __name__ == "__main__":
    raise SystemExit(main())
