"""End-to-end: crawl a local fake site that reproduces senate.go.th's quirks."""

import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from collector.config import Config
from collector.crawler import Crawler, HostGovernor
from collector.store import Store

PDF_A = b"%PDF-1.4 report A"
THAI = "/files/%E0%B8%A3%E0%B8%B2%E0%B8%A2%E0%B8%87%E0%B8%B2%E0%B8%99.pdf"  # รายงาน.pdf

PAGES = {
    "/": """<html><body>
        <a href="/news">news</a>
        <a href="/docs/report.pdf">A</a>
        <a href="/docs/copy.pdf">same bytes as A</a>
        <a href="%s">thai</a>
        <a href="/private/secret.pdf">blocked by robots</a>
        <a href="http://elsewhere.invalid/x.pdf">other host</a>
        <img src="/logo.png">
        <a href="/missing.pdf">404</a>
        <a href="/go">redirects into /private/</a>
        <a href="/shelf">meta refresh into /private/</a>
        <script>var book = '/ebooks/in-script.pdf';</script>
        </body></html>""" % THAI,
    "/news": """<a href="/?&amp;page5=2">2</a>
        <a href="?dl=7&type=4"><i></i></a>
        <ul class="pagination-sm purchase-pager" data-group="1" data-totalpages="3" data-perpage="20"></ul>""",
    "/news?page5=2": '<a href="/docs/page2.pdf">p2</a>',
    "/news?page_g1=2&perpage=20": '<a href="/docs/g1p2.pdf">x</a>',
    "/news?page_g1=3&perpage=20": '<a href="/docs/g1p3.pdf">x</a>',
    "/shelf": '<meta http-equiv="refresh" content="0;URL=/private/book.PDF" />',
    "/many": "".join(f'<a href="/bulk/{i}.pdf">{i}</a>' for i in range(30)),
    # Broken pages (HTTP 500) and forbidden files (403) mixed with working ones, as on senate.go.th.
    "/mixed": "".join(f'<a href="/broken/{i}">b</a><a href="/forbidden/{i}.pdf">x</a>'
                      f'<a href="/bulk/{i}.pdf">f</a>' for i in range(8)),
    # Slow downloads next to a chain of pages, each page leading to the next.
    "/slowmix": "".join(f'<a href="/slow/{i}.pdf">s</a>' for i in range(4)) + '<a href="/chain/0">c</a>',
    **{f"/chain/{i}": f'<a href="/chain/{i + 1}">next</a>' for i in range(5)},
    "/chain/5": "end",
    # senate.go.th's procurement files: no Content-Type, and a name that doesn't end in .pdf.
    "/purchase": '<a href="?dl=9&type=4">pdf</a><a href="/assets/purchase/junk.50">not a document</a>',
}
REDIRECTS = {
    "/news?dl=7&type=4": "/assets/purchase/7_file.pdf",
    "/go": "/private/secret2.pdf",
    "/purchase?dl=9&type=4": "/assets/purchase/9_purchase_file.50",
}
UNTYPED = {  # served with no Content-Type at all
    "/assets/purchase/9_purchase_file.50": b"%PDF-1.6 tender",
    "/assets/purchase/junk.50": b"hello",
}
FILES = {
    "/docs/report.pdf": PDF_A,
    "/docs/copy.pdf": PDF_A,
    THAI: b"%PDF thai",
    "/private/secret.pdf": b"%PDF secret",
    "/ebooks/in-script.pdf": b"%PDF ebook",
    "/docs/page2.pdf": b"%PDF page2",
    "/docs/g1p2.pdf": b"%PDF g1p2",
    "/docs/g1p3.pdf": b"%PDF g1p3",
    "/assets/purchase/7_file.pdf": b"%PDF purchase",
    **{f"/bulk/{i}.pdf": f"%PDF bulk {i}".encode() for i in range(30)},
}


ROBOTS = (200, b"User-agent: *\nDisallow: /private/\n")


class Handler(BaseHTTPRequestHandler):
    hits: list[str] = []
    times: list[tuple[str, float]] = []
    robots = ROBOTS
    blocking = False  # simulate the site banning us: 403 for everything but "/"
    flaky_left = 0  # /flaky.pdf answers "503, retry after 1s" this many times first

    def do_GET(self):
        Handler.hits.append(self.path)
        Handler.times.append((self.path, time.monotonic()))
        if Handler.blocking and self.path not in ("/", "/many"):
            return self._send(403, "text/html", b"forbidden")
        if self.path == "/flaky.pdf":
            if Handler.flaky_left > 0:
                Handler.flaky_left -= 1
                self.send_response(503)
                self.send_header("Retry-After", "1")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._send(200, "application/pdf", b"%PDF flaky")
        if self.path == "/robots.txt":
            return self._send(Handler.robots[0], "text/plain", Handler.robots[1])
        if self.path.startswith("/broken/"):
            return self._send(500, "text/html", b"internal error")
        if self.path.startswith("/forbidden/"):
            return self._send(403, "text/html", b"forbidden")
        if self.path.startswith("/slow/"):
            time.sleep(0.6)
            return self._send(200, "application/pdf", f"%PDF {self.path}".encode())
        if self.path in REDIRECTS:
            self.send_response(302)
            self.send_header("Location", REDIRECTS[self.path])
            self.end_headers()
            return
        if self.path in PAGES:
            return self._send(200, "text/html; charset=UTF-8", PAGES[self.path].encode())
        if self.path in FILES:
            return self._send(200, "application/pdf", FILES[self.path])
        if self.path in UNTYPED:
            return self._send(200, None, UNTYPED[self.path])
        self._send(404, "text/html", b"not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Handler.hits = []
    Handler.times = []
    Handler.robots = ROBOTS
    Handler.blocking = False
    Handler.flaky_left = 0
    yield f"127.0.0.1:{server.server_port}"
    server.shutdown()


def make(tmp_path, host, **overrides):
    opts = dict(
        start_urls=[f"http://{host}/"],
        crawl_hosts=[host],
        file_hosts=[host],
        output_dir=str(tmp_path / "out"),
        state_dir=str(tmp_path / "state"),
        workers=2,
        delay=0,
        max_retries=1,
        # The fake site reproduces senate.go.th's quirks.
        site="senate",
        crawl_host_download_patterns=[r"[?&]dl=\d"],
    )
    opts.update(overrides)
    cfg = Config(**opts)
    return cfg, Store(cfg.db_path)


def rows(tmp_path):
    db = sqlite3.connect(tmp_path / "state" / "collector.db")
    return {u: (k, s, p, n) for u, k, s, p, n in db.execute("SELECT url, kind, status, path, note FROM urls")}


def test_full_crawl(tmp_path, site):
    cfg, store = make(tmp_path, site)
    Crawler(cfg, store).run()
    store.close()

    out = Path(cfg.output_dir) / site.replace(":", "_")
    saved = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    # report.pdf and copy.pdf have identical bytes: whichever finishes first is kept.
    twins = {"docs/report.pdf", "docs/copy.pdf"}
    assert len(saved & twins) == 1
    assert saved - twins == {
        "assets/purchase/7_file.pdf",
        "docs/g1p2.pdf",
        "docs/g1p3.pdf",
        "docs/page2.pdf",
        "ebooks/in-script.pdf",
        "files/รายงาน.pdf",
    }

    r = rows(tmp_path)
    base = f"http://{site}"
    kept = (saved & twins).pop()
    dup = (twins - {kept}).pop()
    assert (out / kept).read_bytes() == PDF_A
    assert r[f"{base}/{dup}"][1] == "done"
    assert r[f"{base}/{dup}"][3] == f"duplicate of {site.replace(':', '_')}/{kept}"
    assert r[f"{base}/private/secret.pdf"][1] == "blocked"
    assert r[f"{base}/private/book.PDF"][1] == "blocked"  # found via meta refresh
    assert r[f"{base}/go"][1] == "blocked"  # 302 into a disallowed path
    assert not [h for h in Handler.hits if h.startswith("/private/")]
    assert r[f"{base}/missing.pdf"][1] == "error"
    assert not any("elsewhere.invalid" in u or u.endswith(".png") for u in r)
    assert not list((Path(cfg.output_dir) / ".partial").iterdir())


def test_robots_server_error_blocks_whole_host(tmp_path, site, monkeypatch):
    monkeypatch.setattr("collector.crawler.time.sleep", lambda s: None)
    Handler.robots = (500, b"oops")
    cfg, store = make(tmp_path, site)
    Crawler(cfg, store).run()
    store.close()

    assert set(Handler.hits) == {"/robots.txt"}  # RFC 9309: 5xx means disallow everything
    (status, note) = next((s, n) for u, (k, s, p, n) in rows(tmp_path).items())
    assert status == "blocked" and "HTTP 500" in note


def test_blocked_urls_are_rechecked_next_run(tmp_path, site):
    cfg, store = make(tmp_path, site)
    Crawler(cfg, store).run()
    store.close()
    secret = f"http://{site}/private/secret.pdf"
    assert rows(tmp_path)[secret][1] == "blocked"

    cfg, store = make(tmp_path, site, respect_robots=False)
    Crawler(cfg, store).run()
    store.close()
    assert rows(tmp_path)[secret][1] == "done"
    assert (Path(cfg.output_dir) / site.replace(":", "_") / "private" / "secret.pdf").exists()


def test_overloaded_server_retry_after_is_obeyed(tmp_path, site):
    Handler.flaky_left = 2
    cfg, store = make(tmp_path, site, start_urls=[f"http://{site}/flaky.pdf"], max_retries=3)
    Crawler(cfg, store).run()
    store.close()

    tries = [t for p, t in Handler.times if p == "/flaky.pdf"]
    assert len(tries) == 3  # 503, 503, then success
    assert all(b - a >= 0.95 for a, b in zip(tries, tries[1:]))  # waited as told each time
    assert rows(tmp_path)[f"http://{site}/flaky.pdf"][1] == "done"


def test_blocked_by_server_backs_off_then_gives_up(tmp_path, site):
    Handler.blocking = True
    cfg, store = make(tmp_path, site, start_urls=[f"http://{site}/many"])
    crawler = Crawler(cfg, store)
    crawler.gov.trip_after = 2
    crawler.gov.pauses = (0.05, 0.05)
    crawler.run()
    store.close()

    origin = f"http://{site}"
    assert crawler.gov.dead == {origin}
    statuses = [s for k, s, p, n in rows(tmp_path).values()]
    assert "pending" in statuses  # gave up instead of trying every URL against a block
    # 3 trips, each 2 failures plus at most 1 request the other connection already had
    # on the wire when the health check started: 9 of 30 URLs, no hammering.
    tried = [h for h in Handler.hits if h.startswith("/bulk/")]
    assert len(tried) <= 9
    assert Handler.hits.count("/robots.txt") >= 4  # robots.txt + a health check per trip

    # Next run (say, the next day): the 403s from the block are retried automatically.
    Handler.blocking = False
    cfg, store = make(tmp_path, site, start_urls=[f"http://{site}/many"])
    Crawler(cfg, store).run()
    store.close()
    after = rows(tmp_path)
    assert all(after[f"{origin}/bulk/{i}.pdf"][1] == "done" for i in range(30))


def test_broken_pages_are_tried_once(tmp_path, site):
    cfg, store = make(tmp_path, site, start_urls=[f"http://{site}/mixed"], max_retries=3,
                      delay=0.01, max_delay=0.1)
    crawler = Crawler(cfg, store)
    crawler.run()
    store.close()

    broken = [h for h in Handler.hits if h.startswith("/broken/")]
    assert sorted(broken) == [f"/broken/{i}" for i in range(8)]  # once each, no retries
    r = rows(tmp_path)
    assert all(r[f"http://{site}{p}"][1] == "error" for p in broken)
    assert all(r[f"http://{site}/bulk/{i}.pdf"][1] == "done" for i in range(8))
    assert crawler.gov.hosts[f"http://{site}"].delay == cfg.delay  # broken pages didn't slow it

    # Next run: all 16 are retried in a row with nothing else to do. Still failing as before,
    # which says nothing about the server: no health check, no cool-down.
    cfg, store = make(tmp_path, site, start_urls=[f"http://{site}/mixed"], delay=0.01)
    crawler = Crawler(cfg, store)
    crawler.gov.trip_after = 2
    crawler.gov.ok_window = 0  # no benefit of the doubt from recent successes
    crawler.gov.pauses = (0.05, 0.05)  # (so a regression fails fast instead of waiting minutes)
    before = len(Handler.hits)
    crawler.run()
    store.close()
    again = Handler.hits[before:]
    assert sorted(h for h in again if h.startswith("/broken/")) == [f"/broken/{i}" for i in range(8)]
    assert len([h for h in again if h.startswith("/forbidden/")]) == 8
    st = crawler.gov.hosts[f"http://{site}"]
    assert st.failures == 0 and st.pauses == 0 and st.paused_until == 0


def test_pages_do_not_wait_behind_downloads(tmp_path, site):
    # One download slot, kept busy for 2.4s by four slow files: the page chain must not
    # queue up behind them.
    cfg, store = make(tmp_path, site, start_urls=[f"http://{site}/slowmix"], workers=1, max_per_host=1)
    Crawler(cfg, store).run()
    store.close()

    last = lambda prefix: max(t for p, t in Handler.times if p.startswith(prefix))  # noqa: E731
    assert len([p for p, t in Handler.times if p.startswith("/slow/")]) == 4
    assert last("/chain/5") < last("/slow/")
    # ...and the download limit still held: never two slow files on the wire at once.
    starts = sorted(t for p, t in Handler.times if p.startswith("/slow/"))
    assert all(b - a >= 0.55 for a, b in zip(starts, starts[1:]))


def test_config_changes_apply_to_queued_urls(tmp_path, site):
    cfg, store = make(tmp_path, site)
    Crawler(cfg, store).run(limit=1)  # only "/" fetched; its links are queued
    store.close()
    report = f"http://{site}/docs/report.pdf"
    assert rows(tmp_path)[report][1] == "pending"

    cfg, store = make(tmp_path, site, exclude=["/docs/"])
    Crawler(cfg, store).run()
    store.close()
    assert rows(tmp_path)[report][1:4:2] == ("skipped", "excluded by config")
    assert not [h for h in Handler.hits if h.startswith("/docs/")]

    cfg, store = make(tmp_path, site)  # exclude removed again
    Crawler(cfg, store).run()
    store.close()
    assert rows(tmp_path)[report][1] == "done"


def test_queued_download_links_move_to_the_download_queue(tmp_path, site):
    # Older versions queued "?dl=" links as pages, so they held the page slot while downloading.
    cfg, store = make(tmp_path, site)
    dl = f"http://{site}/news?dl=7&type=4"
    store.add(dl, "page", 1, None)
    store.commit()
    store.close()
    calls = []
    real = HostGovernor.connection
    def spy(self, origin, lane="file"):
        calls.append(lane)
        return real(self, origin, lane)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(HostGovernor, "connection", spy)
        cfg, store = make(tmp_path, site, start_urls=[])
        Crawler(cfg, store).run()
        store.close()
    assert calls == ["file"]
    assert rows(tmp_path)[dl][:2] == ("file", "done")


def test_resume_does_not_refetch(tmp_path, site):
    cfg, store = make(tmp_path, site)
    Crawler(cfg, store).run(limit=3)  # stop early, like a Ctrl+C
    store.close()
    first = list(Handler.hits)

    cfg, store = make(tmp_path, site)
    Crawler(cfg, store).run()
    store.close()
    second = Handler.hits[len(first):]

    fetched_twice = (set(first) & set(second)) - {"/robots.txt"}
    assert not fetched_twice
    assert (Path(cfg.output_dir) / site.replace(":", "_") / "docs" / "g1p3.pdf").exists()

    cfg, store = make(tmp_path, site)
    before = len(Handler.hits)
    Crawler(cfg, store).run()
    store.close()
    # Nothing left to do. Blocked URLs are re-checked: that needs robots.txt, plus one
    # request for a URL whose *redirect* was blocked (the target may have changed).
    assert set(Handler.hits[before:]) <= {"/robots.txt", "/go"}


def test_status_next_to_a_running_crawl_leaves_its_claims_alone(tmp_path):
    db = tmp_path / "state" / "collector.db"
    crawl = Store(db)
    crawl.add("http://h/a.pdf", "file", 0, None)
    crawl.add("http://h/b.pdf", "file", 0, None)
    [claimed] = crawl.claim(1, "file")
    Store(db).close()  # `python -m collector status`, while the crawl is running
    assert crawl.claim(5, "file")[0]["url"] != claimed["url"]  # not handed out twice
    crawl.close()
    restarted = Store(db)
    assert restarted.reset_claims() == 2  # a new crawl takes back what a killed one left
    restarted.snapshot(str(tmp_path / "copy.db"))
    restarted.close()
    assert Store(tmp_path / "copy.db").counts() == {("file", "pending"): 2}


def test_untyped_documents_are_recognised_by_their_bytes(tmp_path, site):
    cfg, store = make(tmp_path, site, start_urls=[f"http://{site}/purchase"])
    Crawler(cfg, store).run()
    store.close()
    r = rows(tmp_path)
    kind, status, path, note = r[f"http://{site}/purchase?dl=9&type=4"]
    assert status == "done" and path.endswith("assets/purchase/9_purchase_file.50.pdf")
    assert (Path(cfg.output_dir) / path).read_bytes() == b"%PDF-1.6 tender"
    assert r[f"http://{site}/assets/purchase/junk.50"][1::2] == ("skipped", "not a document (no content-type)")
