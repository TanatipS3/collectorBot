"""Command line: python -m collector {crawl,status,manifest,snapshot}"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import signal
import sys
import warnings

from .config import load_config
from .store import Store


def main(argv: list[str] | None = None) -> int:
    # Thai URLs and file names must survive a Windows console using a legacy code page.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    warnings.filterwarnings("ignore", module="requests")  # noisy urllib3/chardet version warning

    ap = argparse.ArgumentParser(prog="collector", description="Crawl a site and download its documents.")
    ap.add_argument("-c", "--config", default="config.toml", help="config file (default: config.toml)")
    sub = ap.add_subparsers(dest="command", required=True)

    crawl = sub.add_parser("crawl", help="crawl and download (resumes where the last run stopped)")
    crawl.add_argument("--workers", type=int, help="parallel downloads (overrides config)")
    crawl.add_argument("--delay", type=float, help="seconds between requests to one host (overrides config)")
    crawl.add_argument("--limit", type=int, help="stop after fetching this many URLs (for test runs)")
    crawl.add_argument("--refresh", action="store_true",
                       help="re-crawl every page to find documents added since the last run")
    crawl.add_argument("--retry-errors", action="store_true", help="retry URLs that failed before")

    sub.add_parser("status", help="show progress counts and recent errors")

    man = sub.add_parser("manifest", help="write a CSV listing every downloaded file")
    man.add_argument("-o", "--output", default="manifest.csv")

    snap = sub.add_parser("snapshot", help="copy the crawl state to one file, e.g. to move the crawl "
                                           "to another machine (safe while a crawl is running)")
    snap.add_argument("output", help="file to write, e.g. collector-state.db")

    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    store = Store(cfg.db_path)
    try:
        if args.command == "crawl":
            return _crawl(args, cfg, store)
        if args.command == "status":
            return _status(store)
        if args.command == "snapshot":
            store.snapshot(args.output)
            print(f"wrote {args.output}: copy it to state/collector.db on the other machine")
            return 0
        return _manifest(args, store)
    finally:
        store.close()


def _single_instance(path):
    """Refuse to start a second crawl on the same state: it would double the load on the site."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    f.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f  # the OS releases the lock when this process exits, however it exits


def _crawl(args, cfg, store) -> int:
    from .crawler import Crawler

    lock = _single_instance(cfg.lock_path)
    if lock is None:
        print(f"Another crawl is already running (lock: {cfg.lock_path}). "
              "Use `python -m collector status` to watch it.")
        return 1
    if args.workers:
        cfg.workers = args.workers
    if args.delay is not None:
        cfg.delay = args.delay
    # `kill` and `systemctl stop` send SIGTERM: stop like Ctrl+C, saving what is in flight.
    signal.signal(signal.SIGTERM, _interrupt)
    _setup_logging(cfg)
    log = logging.getLogger("collector")
    if args.refresh:
        log.info("refresh: re-queued %d pages", store.requeue_pages())
    if args.retry_errors:
        log.info("retry: re-queued %d failed URLs", store.requeue_errors())
    log.info("crawling %s -> %s (site rules=%s, workers=%d, delay=%.2fs, robots=%s)",
             ", ".join(cfg.start_urls), cfg.output_dir, cfg.site or "generic", cfg.workers, cfg.delay,
             cfg.respect_robots)
    Crawler(cfg, store).run(limit=args.limit)
    return 0


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def _status(store) -> int:
    counts = store.counts()
    if not counts:
        print("Nothing crawled yet.")
        return 0
    statuses = ["done", "pending", "fetching", "skipped", "blocked", "error"]
    print(f"{'':6}" + "".join(f"{s:>10}" for s in statuses))
    for kind in ("page", "file"):
        print(f"{kind:6}" + "".join(f"{counts.get((kind, s), 0):>10}" for s in statuses))
    print(f"\ndownloaded: {store.saved_bytes() / 1024 / 1024:.1f} MB (excluding duplicates)")
    errors = store.errors()
    if errors:
        print("\nrecent errors:")
        for e in errors:
            print(f"  [{e['kind']}] {e['note']}  {e['url']}")
    return 0


def _manifest(args, store) -> int:
    n = 0
    # utf-8-sig so Excel shows Thai file names correctly.
    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["path", "url", "final_url", "size", "sha256", "content_type", "found_on", "note", "downloaded_at"])
        for r in store.files():
            w.writerow([r["path"], r["url"], r["final_url"], r["size"], r["sha256"],
                        r["content_type"], r["referrer"], r["note"], r["updated_at"]])
            n += 1
    print(f"wrote {n} rows to {args.output}")
    return 0


def _setup_logging(cfg) -> None:
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("collector")
    root.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    # The log file spans many runs and days.
    logfile = logging.FileHandler(cfg.log_path, encoding="utf-8")
    logfile.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
    root.addHandler(console)
    root.addHandler(logfile)


if __name__ == "__main__":
    raise SystemExit(main())
