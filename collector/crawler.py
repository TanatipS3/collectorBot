"""Crawl loop: worker threads fetch, the main thread owns the database and the disk layout."""

from __future__ import annotations

import hashlib
import itertools
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests

from . import saver, urls
from .config import Config
from .store import Store

log = logging.getLogger("collector")

# What a busy server answers: these slow the crawl down and are retried within a run. A plain
# 500 is not one of them: it is almost always a broken page, which fails the same way every
# time (it is still retried on the next run).
RETRY_STATUS = {408, 429, 502, 503, 504}
TRANSIENT_ERRORS = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)
MAX_REDIRECTS = 10
MAX_HTML_BYTES = 20 * 1024 * 1024
CHUNK = 256 * 1024
DOC_MIME_PREFIXES = (
    "application/pdf", "application/msword", "application/vnd.ms-",
    "application/vnd.openxmlformats-officedocument", "application/vnd.oasis.opendocument",
    "application/rtf", "application/epub", "application/zip", "application/x-zip",
    "application/x-rar", "application/vnd.rar", "application/x-7z", "text/csv",
)
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([\w-]+)""", re.I)
# Content types that say nothing about what the file is. Some servers send documents with no type
# and a mangled name (senate.go.th's "..._purchase_file.50"), so these get a look at the bytes.
UNTYPED = {"", "application/octet-stream", "binary/octet-stream", "application/force-download",
           "application/x-download", "application/download", "application/unknown"}
_OOXML = (
    (b"word/", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    (b"xl/", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    (b"ppt/", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
)


@dataclass
class Job:
    url: str
    kind: str
    depth: int
    attempts: int = 0


@dataclass
class Result:
    outcome: str  # page | file | skip | blocked | error | abort
    http_status: int | None = None
    content_type: str | None = None
    final_url: str | None = None
    links: set[str] = field(default_factory=set)
    part: Path | None = None
    target: Path | None = None
    sha256: str | None = None
    size: int | None = None
    note: str | None = None
    attempts: int = 0


@dataclass
class _HostState:
    base: float  # normal spacing between requests
    delay: float  # current spacing (grows while the server struggles)
    next_slot: float = 0.0
    paused_until: float = 0.0  # Retry-After or a cool-down: nothing is sent before this
    open: dict[str, int] = field(default_factory=dict)  # requests in progress, per lane
    failures: int = 0  # in a row
    pauses: int = 0  # cool-downs since the last success
    probing: bool = False
    last_ok: float = field(default_factory=time.monotonic)  # when a request last succeeded


class HostGovernor:
    """Keeps the crawler polite to each server (origin), across all worker threads.

    - Requests start at least `delay` seconds apart (or robots.txt's Crawl-delay, if larger).
      At most `max_parallel` downloads are open at once, plus `page_parallel` page requests:
      pages are small and fast, and without slots of their own they would wait behind 100 MB
      downloads, finding no new documents meanwhile.
    - Signs of overload (429, 502, 503, 504, timeouts, dropped connections) double the spacing,
      up to `max_delay`, from the second one in a row: a single failure between successes is a
      broken URL, not a struggling server. Each success shrinks the spacing back toward normal.
      Retry-After is always obeyed.
    - Other failures (403, which is what a block looks like, and 500, which is what a broken
      page looks like) don't slow anything down, but count toward the health check.
    - After `trip_after` failures in a row the server's robots.txt is fetched as a health check.
      If the server answers normally, and some request has succeeded in the last `ok_window`
      seconds, the failures were specific to those URLs and the spacing goes back to normal
      (with several connections open, a batch of broken links can fail 10 in a row in two
      seconds while the server is perfectly fine). If not, it is
      paused for each of `pauses` in turn, and after that given up on for this run: its URLs
      stay queued.
    """

    def __init__(self, delay: float, max_parallel: int, max_delay: float, user_agent: str,
                 stop: threading.Event, trip_after: int = 5,
                 pauses: tuple[float, ...] = (300, 600, 1200, 1800), page_parallel: int = 1,
                 ok_window: float = 30):
        self.delay = delay
        self.ok_window = ok_window
        self.max_parallel = max_parallel
        self.page_parallel = page_parallel
        self.max_delay = max_delay
        self.trip_after = trip_after
        self.pauses = pauses
        self.stop = stop
        self.cond = threading.Condition()
        self.hosts: dict[str, _HostState] = {}
        self.dead: set[str] = set()
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent

    def _state(self, origin: str) -> _HostState:  # caller holds self.cond
        st = self.hosts.get(origin)
        if st is None:
            st = self.hosts[origin] = _HostState(self.delay, self.delay)
        return st

    @contextmanager
    def connection(self, origin: str, lane: str = "file"):
        cap = self.page_parallel if lane == "page" else self.max_parallel
        with self.cond:
            st = self._state(origin)
            while st.open.get(lane, 0) >= cap:
                self._check_alive(origin)
                self.cond.wait(0.5)
            st.open[lane] = st.open.get(lane, 0) + 1
        try:
            yield
        finally:
            with self.cond:
                st.open[lane] -= 1
                self.cond.notify_all()

    def wait_turn(self, origin: str, floor: float | None = None) -> None:
        with self.cond:
            self._check_alive(origin)
            st = self._state(origin)
            if floor and floor > st.base:
                st.base = floor
                st.delay = max(st.delay, floor)
            now = time.monotonic()
            start = max(now, st.next_slot)
            st.next_slot = start + st.delay
        while True:
            with self.cond:
                self._check_alive(origin)
                # A health check or cool-down that began while we waited holds us too.
                start = max(start, st.paused_until)
                left = start - time.monotonic()
                if left <= 0 and not st.probing:
                    return
            if self.stop.wait(min(max(left, 0.05), 0.5)):
                raise _Aborted

    def _check_alive(self, origin: str) -> None:
        if self.stop.is_set() or origin in self.dead:
            raise _Aborted

    def record(self, origin: str, outcome: str, retry_after: float | None = None,
               detail: str = "") -> None:
        """outcome: 'ok', 'overload' (429/502/503/504/timeout/connection error), or 'failed'
        (403, 500: counted toward the health check, but no slow-down)."""
        with self.cond:
            st = self._state(origin)
            if outcome == "ok":
                st.failures = st.pauses = 0
                st.last_ok = time.monotonic()
                st.delay = max(st.base, st.delay * 0.8)
                return
            st.failures += 1
            if outcome == "overload" and st.failures >= 2:
                st.delay = min(self.max_delay, max(st.delay, st.base, 0.5) * 2)
            if retry_after:
                st.paused_until = max(st.paused_until, time.monotonic() + min(retry_after, 3600))
                st.next_slot = max(st.next_slot, st.paused_until)
            probe = st.failures >= self.trip_after and not st.probing
            if probe:
                st.probing = True
            delay, failures = st.delay, st.failures
        log.warning("%s: %s (%d in a row) - now %.1fs between requests%s", origin, detail, failures,
                    delay, f", server asked to wait {retry_after:.0f}s" if retry_after else "")
        if probe:
            self._health_check(origin)

    def _health_check(self, origin: str) -> None:
        healthy = False
        try:
            r = self.session.get(origin + "/robots.txt", timeout=30)
            healthy = r.status_code < 500 and r.status_code not in (403, 408, 429)
        except requests.RequestException:
            pass
        with self.cond:
            st = self._state(origin)
            st.probing = False
            st.failures = 0
            # robots.txt can be fine while everything else fails (a broken or blocking app in
            # front of static files): then nothing will have succeeded for a while.
            specific = healthy and time.monotonic() - st.last_ok < self.ok_window
            if specific:
                st.delay = st.base
                st.next_slot = min(st.next_slot, time.monotonic() + st.base)
                log.info("%s still answers normally; those failures were specific to the URLs, "
                         "back to %.1fs between requests", origin, st.base)
                return
            if st.pauses >= len(self.pauses):
                self.dead.add(origin)
                self.cond.notify_all()
                log.error("%s kept failing through %d cool-downs: it is down or blocking this crawler. "
                          "No more requests to it this run; its URLs stay queued for the next run.",
                          origin, len(self.pauses))
                return
            pause = self.pauses[st.pauses]
            st.pauses += 1
            st.paused_until = time.monotonic() + pause
            st.next_slot = max(st.next_slot, st.paused_until)
        log.warning("%s looks down or is refusing us: pausing it for %s (cool-down %d of %d)",
                    origin, _duration(pause), st.pauses, len(self.pauses))


class Robots:
    """robots.txt rules per origin, fetched once per run and shared by all threads.

    Follows RFC 9309: a missing robots.txt (4xx) allows everything; a server error (5xx)
    or an unreachable host means everything is disallowed.
    """

    def __init__(self, user_agent: str):
        self.user_agent = user_agent
        self.lock = threading.Lock()
        self.rules: dict[str, tuple[RobotFileParser, str]] = {}
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent

    def blocked(self, url: str) -> str | None:
        """None if the URL may be fetched, otherwise the reason it may not."""
        rp, reason = self._entry(url)
        return None if rp.can_fetch(self.user_agent, url) else reason

    def crawl_delay(self, url: str) -> float | None:
        delay = self._entry(url)[0].crawl_delay(self.user_agent)
        return float(delay) if delay else None

    def _entry(self, url: str) -> tuple[RobotFileParser, str]:
        origin = _origin(url)
        with self.lock:
            entry = self.rules.get(origin)
            if entry is None:
                entry = self.rules[origin] = self._load(origin)
        return entry

    def _load(self, origin: str) -> tuple[RobotFileParser, str]:
        rp = RobotFileParser()
        for attempt in (1, 2):
            try:
                r = self.session.get(origin + "/robots.txt", timeout=15)
            except requests.RequestException as e:
                problem = f"robots.txt unreachable ({type(e).__name__})"
            else:
                if r.status_code < 400:
                    rp.parse(r.text.splitlines())
                    return rp, "disallowed by robots.txt"
                if r.status_code < 500:
                    rp.allow_all = True
                    return rp, ""
                problem = f"robots.txt returned HTTP {r.status_code}"
            if attempt == 1:
                time.sleep(3)
        log.warning("%s: %s, so the whole host is treated as disallowed this run", origin, problem)
        rp.disallow_all = True
        return rp, problem


class Crawler:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self.out = Path(cfg.output_dir)
        self.partial = self.out / ".partial"
        self.partial.mkdir(parents=True, exist_ok=True)
        # Half-written downloads from a run that was killed; those URLs are re-queued anyway.
        for stale in self.partial.glob("*.part"):
            stale.unlink(missing_ok=True)
        self.stop = threading.Event()
        self.gov = HostGovernor(cfg.delay, cfg.max_per_host, cfg.max_delay, cfg.user_agent, self.stop,
                                page_parallel=cfg.pages_per_host)
        self.local = threading.local()
        self.robots = Robots(cfg.user_agent) if cfg.respect_robots else None
        self.seen = store.all_urls()
        self.fetched = 0

    # ---------- main thread ----------

    def run(self, limit: int | None = None) -> None:
        self.store.reset_claims()  # URLs a killed run had claimed but not finished
        # robots.txt may have changed, or respect_robots been switched off, since the
        # last run: give previously blocked URLs another check (it costs no request).
        self.store.requeue_blocked()
        # Timeouts and server errors from last time are worth another try.
        retried = self.store.requeue_transient_errors()
        if retried:
            log.info("retrying %d URLs that hit temporary errors last run", retried)
        dropped, revived, moved = self.store.apply_scope(lambda u: urls.classify(u, self.cfg))
        if dropped or revived:
            log.info("config changed: skipping %d queued URLs, re-queued %d", dropped, revived)
        if moved:
            log.info("moved %d queued URLs between the page and download queues", moved)
        for u in self.cfg.start_urls:
            url = urls.normalize(u)
            if url and url not in self.seen:
                self.seen.add(url)
                self.store.add(url, "page", 0, None)
        self.store.commit()

        inflight: dict[Future, Job] = {}
        last_report = 0.0
        stopping = False
        # Pages and files have separate threads and queues, so downloads can't take every
        # thread while pages (which lead to more documents) wait.
        page_threads = max(1, len(self.cfg.crawl_hosts)) * self.cfg.pages_per_host
        with ThreadPoolExecutor(self.cfg.workers, thread_name_prefix="file") as file_pool, \
             ThreadPoolExecutor(page_threads, thread_name_prefix="page") as page_pool:
            lanes = (("file", file_pool, self.cfg.workers), ("page", page_pool, page_threads))
            try:
                while True:
                    for kind, pool, threads in lanes:
                        want = threads * 2 - sum(j.kind == kind for j in inflight.values())
                        if limit is not None:
                            want = min(want, limit - self.fetched)
                        if want <= 0 or stopping:
                            continue
                        dead = [o + "/" for o in self.gov.dead]
                        for row in self.store.claim(want, kind, skip_prefixes=dead):
                            job = Job(row["url"], row["kind"], row["depth"], row["attempts"])
                            inflight[pool.submit(self._fetch, job)] = job
                            self.fetched += 1
                    if not inflight:
                        break
                    done, _ = wait(inflight, timeout=1.0, return_when=FIRST_COMPLETED)
                    for fut in done:
                        self._handle(inflight.pop(fut), fut.result())
                    self.store.commit()
                    if limit is not None and self.fetched >= limit:
                        stopping = True
                    if time.monotonic() - last_report > 15:
                        self.report()
                        last_report = time.monotonic()
            except KeyboardInterrupt:
                log.warning("Interrupted: finishing in-flight requests, then saving state...")
                self.stop.set()
                for fut in list(inflight):
                    self._handle(inflight.pop(fut), fut.result())
                self.store.commit()
        self.report()
        for origin in sorted(self.gov.dead):
            log.warning("%s was given up on this run; run `crawl` again later to finish its URLs", origin)

    def report(self) -> None:
        c = self.store.counts()
        g = lambda kind, status: c.get((kind, status), 0)  # noqa: E731
        pages_left = g("page", "pending") + g("page", "fetching")
        log.info(
            "pages %d done, %d queued | files %d saved (%s), %d queued | skipped %d | robots-blocked %d | errors %d",
            g("page", "done"), pages_left,
            g("file", "done"), _human(self.store.saved_bytes()),
            g("file", "pending") + g("file", "fetching"),
            g("page", "skipped") + g("file", "skipped"),
            g("page", "blocked") + g("file", "blocked"),
            g("page", "error") + g("file", "error"),
        )

    def _enqueue(self, url: str, depth: int, referrer: str) -> None:
        if url in self.seen:
            return
        self.seen.add(url)
        kind = urls.classify(url, self.cfg)
        if kind is not None:
            self.store.add(url, kind, depth, referrer)

    def _handle(self, job: Job, res: Result) -> None:
        common = dict(
            http_status=res.http_status, content_type=res.content_type,
            final_url=res.final_url, attempts=job.attempts + res.attempts,
        )
        if res.outcome == "abort":
            self.store.release(job.url)
        elif res.outcome == "page":
            self.store.finish(job.url, "done", **common)
            for link in res.links:
                self._enqueue(link, job.depth + 1, job.url)
            if res.final_url and res.final_url != job.url and res.final_url not in self.seen:
                self.seen.add(res.final_url)
                self.store.add(res.final_url, "page", job.depth, job.url, status="done", note=f"redirect from {job.url}")
        elif res.outcome == "file":
            try:
                path, note = self._place_file(res)
            except OSError as e:  # one unsavable file must not stop the crawl
                Path(res.part).unlink(missing_ok=True)
                self.store.finish(job.url, "error", note=f"could not save: {e}", **common)
                log.warning("error %s: could not save: %s", job.url, e)
                return
            self.store.finish(job.url, "done", kind="file", path=path, sha256=res.sha256,
                              size=res.size, note=note, **common)
            if note:
                log.info("duplicate %s (same bytes as %s)", job.url, path)
            else:
                log.info("saved %s (%s)", path, _human(res.size or 0))
        elif res.outcome == "skip":
            self.store.finish(job.url, "skipped", note=res.note, **common)
        elif res.outcome == "blocked":
            self.store.finish(job.url, "blocked", note=res.note, **common)
        else:
            self.store.finish(job.url, "error", note=res.note, **common)
            log.warning("error %s: %s", job.url, res.note)

    def _place_file(self, res: Result) -> tuple[str, str | None]:
        assert res.part and res.target and res.sha256
        existing = self.store.path_for_sha(res.sha256)
        if existing and os.path.exists(saver.fs(self.out / existing)):
            os.remove(res.part)
            return existing, f"duplicate of {existing}"
        dest = self.out / res.target
        if os.path.exists(saver.fs(dest)):
            if _sha256_file(saver.fs(dest)) == res.sha256:
                os.remove(res.part)
                return res.target.as_posix(), None
            dest = dest.with_name(f"{dest.stem}_{res.sha256[:8]}{dest.suffix}")
        os.makedirs(saver.fs(dest.parent), exist_ok=True)
        os.replace(res.part, saver.fs(dest))
        return dest.relative_to(self.out).as_posix(), None

    # ---------- worker threads ----------

    def _session(self) -> requests.Session:
        s = getattr(self.local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = self.cfg.user_agent
            if self.cfg.accept_language:
                s.headers["Accept-Language"] = self.cfg.accept_language
            self.local.session = s
        return s

    def _fetch(self, job: Job) -> Result:
        try:
            with self.gov.connection(_origin(job.url), lane=job.kind):
                for attempt in range(1, self.cfg.max_retries + 1):
                    res = self._attempt(job, last=attempt == self.cfg.max_retries)
                    if res is not None:
                        res.attempts = attempt
                        return res
                    # Retry. The governor has already slowed this host down, so the next
                    # attempt waits longer instead of hammering a struggling server.
                raise AssertionError("unreachable")
        except _Aborted:
            return Result("abort")

    def _attempt(self, job: Job, last: bool) -> Result | None:
        """One try at a URL. None means: failed in a way worth retrying."""
        body_origin = None
        try:
            r = self._get(job.url, failed_before=job.attempts > 0)
            if isinstance(r, Result):
                return r
            body_origin = _origin(r.url)
            with r:
                if r.status_code in RETRY_STATUS and not last:
                    return None
                return self._read(job, r)
        except _Aborted:
            raise
        except TRANSIENT_ERRORS as e:
            if body_origin:  # the response started but the body failed (_get records its own)
                self.gov.record(body_origin, "overload", detail=type(e).__name__)
            return None if not last else Result("error", note=f"{type(e).__name__}: {e}")
        except Exception as e:  # never let one bad URL kill the crawl
            return Result("error", note=f"{type(e).__name__}: {e}")

    def _get(self, url: str, failed_before: bool = False) -> requests.Response | Result:
        """GET, following redirects by hand so every hop is throttled and checked against robots.txt.

        failed_before: the URL failed on an earlier run. If it still fails the same way (403,
        500), that says nothing about the server today, so it doesn't count toward the health
        check. (Each run starts by retrying the last run's failures: hundreds of them in a row.)
        """
        session = self._session()
        for hop in range(MAX_REDIRECTS + 1):
            reason = self.robots.blocked(url) if self.robots else None
            if reason:
                note = reason if hop == 0 else f"redirects to {url}: {reason}"
                return Result("blocked", final_url=url, note=note)
            origin = _origin(url)
            self.gov.wait_turn(origin, floor=self.robots.crawl_delay(url) if self.robots else None)
            try:
                r = session.get(url, stream=True, timeout=(15, self.cfg.timeout), allow_redirects=False)
            except TRANSIENT_ERRORS as e:
                self.gov.record(origin, "overload", detail=type(e).__name__)
                raise
            if r.status_code in RETRY_STATUS:
                self.gov.record(origin, "overload", _retry_after(r), detail=f"HTTP {r.status_code}")
            elif failed_before and hop == 0 and (r.status_code == 403 or r.status_code >= 500):
                pass  # still failing, as it was last run
            elif r.status_code == 403 or r.status_code >= 500:
                self.gov.record(origin, "failed", _retry_after(r), detail=f"HTTP {r.status_code}")
            else:
                self.gov.record(origin, "ok")
            location = session.get_redirect_target(r)
            if not location:
                return r
            r.close()
            target = urls.normalize(urljoin(url, location))
            if not target:
                return Result("skip", http_status=r.status_code, note=f"redirect to unusable URL {location!r}")
            url = target
        return Result("error", note="too many redirects")

    def _read(self, job: Job, r: requests.Response) -> Result:
        ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
        disp = r.headers.get("Content-Disposition")
        final = urls.normalize(r.url) or r.url
        base = Result("skip", http_status=r.status_code, content_type=ctype, final_url=final)
        if r.status_code >= 400:
            base.outcome, base.note = "error", f"HTTP {r.status_code}"
            return base

        if ctype in ("text/html", "application/xhtml+xml") and not _is_attachment(disp):
            if job.kind != "page":
                base.note = "expected a document, got an HTML page"
            elif not urls.host_matches(urlsplit(final).netloc, self.cfg.crawl_hosts):
                base.note = f"redirected out of crawl scope to {final}"
            else:
                body = _read_capped(r, MAX_HTML_BYTES)
                base.outcome = "page"
                base.links = urls.extract_links(_decode_html(body, r.headers.get("Content-Type", "")),
                                                final, self.cfg)
            return base

        if _is_document(ctype, final, disp, self.cfg.doc_exts):
            return self._download(r, base, disp)
        if ctype in UNTYPED:
            chunks = r.iter_content(CHUNK)
            head = next(chunks, b"")
            sniffed = _sniff(head)
            if sniffed:
                base.content_type = sniffed
                return self._download(r, base, disp, itertools.chain([head], chunks))
        base.note = f"not a document ({ctype or 'no content-type'})"
        return base

    def _download(self, r: requests.Response, res: Result, disp: str | None, chunks=None) -> Result:
        limit = int(self.cfg.max_file_mb * 1024 * 1024)
        declared = int(r.headers.get("Content-Length") or 0)
        if limit and declared > limit:
            res.note = f"larger than max_file_mb ({_human(declared)})"
            return res
        part = self.partial / f"{uuid.uuid4().hex}.part"
        h = hashlib.sha256()
        size = 0
        try:
            with open(part, "wb") as f:
                for chunk in chunks or r.iter_content(CHUNK):
                    if self.stop.is_set():
                        raise _Aborted
                    size += len(chunk)
                    if limit and size > limit:
                        raise _TooBig
                    h.update(chunk)
                    f.write(chunk)
        except _Aborted:
            part.unlink(missing_ok=True)
            return Result("abort")
        except _TooBig:
            part.unlink(missing_ok=True)
            res.note = "larger than max_file_mb"
            return res
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        if declared and size < declared:
            part.unlink(missing_ok=True)
            raise requests.exceptions.ChunkedEncodingError(f"truncated: got {size} of {declared} bytes")
        res.outcome = "file"
        res.part, res.sha256, res.size = part, h.hexdigest(), size
        res.target = saver.target_path(res.final_url or r.url, res.content_type or "", disp, self.cfg.doc_exts)
        return res


class _Aborted(Exception):
    pass


class _TooBig(Exception):
    pass


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _retry_after(r: requests.Response) -> float | None:
    value = r.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _duration(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds >= 60 else f"{seconds:.0f}s"


def _is_attachment(disp: str | None) -> bool:
    return bool(disp) and disp.lower().lstrip().startswith("attachment")


def _is_document(ctype: str, url: str, disp: str | None, doc_exts=urls.DOC_EXTS) -> bool:
    if ctype.startswith(DOC_MIME_PREFIXES):
        return True
    name = saver.disposition_filename(disp)
    ext = name.rsplit(".", 1)[-1].lower() if name and "." in name else urls.extension(url)
    return ext in doc_exts


def _sniff(head: bytes) -> str | None:
    """Content type of a document, from its first bytes; None if it doesn't look like one."""
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04"):  # Office files are ZIP archives of word/, xl/ or ppt/
        return next((ctype for marker, ctype in _OOXML if marker in head), "application/zip")
    return None


def _read_capped(r: requests.Response, cap: int) -> bytes:
    buf = bytearray()
    for chunk in r.iter_content(CHUNK):
        buf += chunk
        if len(buf) > cap:
            break
    return bytes(buf)


def _decode_html(body: bytes, content_type_header: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type_header, re.I)
    enc = m.group(1) if m else None
    if not enc:
        mm = _META_CHARSET.search(body[:4096])
        enc = mm.group(1).decode("ascii") if mm else "utf-8"
    try:
        return body.decode(enc, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"
