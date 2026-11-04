"""`collector check` against a local fake site with one section per kind of site."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from collector import check
from collector.__main__ import main
from collector.config import Config, load_config

PAGES = {
    # Documents linked plainly, one behind an extension-less link, one on another host.
    "/plain/": """<a href="/plain/about">about</a><a href="/plain/docs">documents</a>
        <a href="/search?q=x">search</a><a href="/plain/list?sort=date">sorted</a>
        <a href="http://files.invalid/big.pdf">elsewhere</a>""",
    "/plain/docs": '<a href="/plain/files/a.pdf">a</a><a href="/plain/get?id=5">b</a>',
    "/plain/about": "about us",
    "/plain/list?sort=date": "a list",
    # A JavaScript application: an empty shell until its scripts run.
    "/app/": '<html><head><script src="/app/main.js"></script></head><body><div id="root"></div></body></html>',
    # Documents opened only by JavaScript.
    "/jsdocs/": """<button onclick="openDoc(42, 'report.pdf')">report</button>
        <a href="javascript:downloadFile(7)">form</a><a href="/jsdocs/more">more</a>""",
    "/jsdocs/more": "nothing here",
    # ASP.NET paging by form posts.
    "/aspx/": """<a href="javascript:__doPostBack('grid','Page$2')">2</a><a href="/aspx/f.pdf">f</a>""",
    # Links but no documents within reach.
    "/deep/": '<a href="/deep/a">a</a>',
    "/deep/a": '<a href="/deep/b">b</a>',
    "/deep/b": '<a href="/deep/c">c</a>',
    "/empty/": "<html><body>Hello</body></html>",
    "/private/": '<a href="/private/x.pdf">x</a>',
    "/thai/": '<a href="/thai/%E0%B8%A3%E0%B8%B2%E0%B8%A2%E0%B8%87%E0%B8%B2%E0%B8%99.pdf">report</a>',
}
FILES = {"/plain/get?id=5": b"%PDF-1.4 by id", "/aspx/f.pdf": b"%PDF-1.4 f"}


class Handler(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):
        Handler.hits.append(self.path)
        if self.path == "/robots.txt":
            return self._send(200, "text/plain", b"User-agent: *\nDisallow: /private/\n")
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/plain/")
            self.end_headers()
            return
        if self.path == "/cf/":
            return self._send(403, "text/html", b"<title>Just a moment...</title>", {"cf-ray": "abc"})
        if self.path in PAGES:
            return self._send(200, "text/html; charset=utf-8", PAGES[self.path].encode())
        if self.path in FILES:
            return self._send(200, "application/pdf", FILES[self.path])
        self._send(404, "text/html", b"not found")

    def _send(self, code, ctype, body, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{server.server_port}"
    server.shutdown()


def run(site, path, **kw):
    kw.setdefault("delay", 0)
    return check.run(f"http://{site}{path}", **kw)


def test_plain_links_are_ready_and_suggest_a_config(site):
    rep = run(site, "/plain/")
    assert rep.verdict == check.READY and rep.exit_code == 0
    assert set(rep.docs) == {f"http://{site}/plain/files/a.pdf", f"http://{site}/plain/get?id=5",
                             "http://files.invalid/big.pdf"}
    assert f"http://{site}/search?q=x" in rep.excluded  # the default exclude is applied while checking
    assert "/search?q=x" not in Handler.hits
    assert [p for p, *_ in rep.exclude_suggestions()] == ["[?&](sort|order|orderby)="]
    cfg = rep.suggested_config()
    assert cfg["crawl_hosts"] == [site] and cfg["file_hosts"] == [site, "files.invalid"]
    assert cfg["output_dir"] == "downloads_127_0_0_1_" + site.split(":")[1]
    text = check.format_report(rep)
    assert "Verdict: READY" in text and "files.invalid (another host)" in text


def test_documents_page_is_read_before_others(site):
    rep = run(site, "/plain/", pages=2)
    assert rep.pages[1].url == f"http://{site}/plain/docs"


def test_javascript_app_is_recognised(site):
    rep = run(site, "/app/")
    assert rep.verdict == check.JS_APP and rep.exit_code == 1
    assert "an empty <div id=root/app>" in rep.app_markers


def test_documents_opened_by_javascript_need_link_rules(site):
    rep = run(site, "/jsdocs/")
    assert rep.verdict == check.NEEDS_LINK_RULES and rep.exit_code == 2
    assert any("openDoc(42, 'report.pdf')" in s for s in rep.js_refs)
    assert "downloadFile" in rep.js_calls

    # With a link rule for openDoc, checked again through -c: the document is found.
    cfg = Config(start_urls=[], crawl_hosts=[site], file_hosts=[site],
                 link_rules=[{"find": r"openDoc\((\d+),\s*'([^']+)'\)", "url": "/docs/{1}/{2}"}])
    rep = run(site, "/jsdocs/", cfg=cfg)
    assert rep.verdict == check.READY
    assert f"http://{site}/docs/42/report.pdf" in rep.docs
    assert not rep.js_refs and "downloadFile" in rep.js_calls  # still worth a note


def test_postback_paging_is_a_note(site):
    rep = run(site, "/aspx/")
    assert rep.verdict == check.READY
    assert any("__doPostBack" in n for n in rep.notes)


def test_no_documents_within_the_pages_read(site):
    rep = run(site, "/deep/", pages=2)
    assert rep.verdict == check.NO_DOCS_YET and rep.exit_code == 2


def test_page_without_links(site):
    assert run(site, "/empty/").verdict == check.NO_LINKS


def test_blocked_by_robots_bot_wall_or_error(site):
    rep = run(site, "/private/")
    assert rep.verdict == check.BLOCKED and "robots.txt" in rep.message
    assert "/private/" not in Handler.hits
    rep = run(site, "/cf/")
    assert rep.verdict == check.BLOCKED and "Bot protection" in rep.message
    assert run(site, "/nothing/").verdict == check.BLOCKED
    assert run(site, "/aspx/f.pdf").verdict == check.NOT_A_PAGE


def test_redirected_start_page_is_the_one_suggested(site):
    rep = run(site, "/redirect")
    assert rep.verdict == check.READY
    assert rep.suggested_config()["start_urls"] == [f"http://{site}/plain/"]


def test_write_fills_the_template_and_never_overwrites(site, tmp_path):
    rep = run(site, "/plain/")
    path = check.write_config(rep, tmp_path / "site.toml")
    cfg = load_config(path)
    assert cfg.start_urls == [f"http://{site}/plain/"]
    assert cfg.crawl_hosts == [site] and cfg.file_hosts == [site, "files.invalid"]
    assert "[?&](sort|order|orderby)=" in cfg.exclude and "/search" in cfg.exclude
    assert cfg.output_dir.startswith("downloads_127_0_0_1")
    assert "Verdict: READY" in path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        check.write_config(rep, path)


def test_report_shows_thai_urls_readably(site):
    rep = run(site, "/thai/")
    assert "/thai/รายงาน.pdf" in check.format_report(rep)
    assert all("%E0" in u for u in rep.docs)  # kept encoded for configs and the crawl


def test_cli(site, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)  # check must not need config.toml or create a state directory
    out = tmp_path / "new.toml"
    assert main(["check", f"http://{site}/plain/", "--pages", "3", "--delay", "0", "--write", str(out)]) == 0
    assert "Verdict: READY" in capsys.readouterr().out and out.exists()
    assert not (tmp_path / "state").exists()
    assert main(["-c", str(out), "check", f"http://{site}/jsdocs/", "--pages", "2", "--delay", "0"]) == 2
    assert main(["-c", str(out), "check", f"http://{site}/plain/", "--write", "x.toml"]) == 1
