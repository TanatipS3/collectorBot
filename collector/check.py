"""`python -m collector check <url>`: can the crawler collect this site? Step 0, automated.

Reads the start page and a few pages it links to (politely: robots.txt, one request at a time,
`delay` apart), then reports what a person would otherwise look for in the page source: documents
linked plainly, documents opened by JavaScript instead, a JavaScript application whose HTML is an
empty shell, bot protection, POST-only pagination, logins. Ends with a verdict and a starting
config, which --write saves as a file.
"""

from __future__ import annotations

import heapq
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests
from bs4 import BeautifulSoup

from . import urls
from .config import Config, load_config
from .crawler import MAX_HTML_BYTES, UNTYPED, Robots, _decode_html, _is_document, _read_capped, _sniff

TEMPLATE = Path(__file__).resolve().parent.parent / "config.example.toml"

READY, NEEDS_LINK_RULES, NO_DOCS_YET = "READY", "NEEDS LINK RULES", "NO DOCUMENTS YET"
JS_APP, NO_LINKS, NOT_A_PAGE, BLOCKED, UNREACHABLE = (
    "JAVASCRIPT APP", "NO LINKS", "NOT A PAGE", "BLOCKED", "UNREACHABLE")
EXIT_CODES = {READY: 0, NEEDS_LINK_RULES: 2, NO_DOCS_YET: 2}  # anything else: 1

# The `exclude` of config.example.toml: used while checking, and the start of the suggested one.
DEFAULT_EXCLUDE = ("/search", "[?&](q|keyword|search)=", "/login", "logout")
# More URL families that are usually not worth crawling. Suggested when the site has them.
EXCLUDE_HINTS = (
    ("[?&](sort|order|orderby)=", "sort orders"),
    ("[?&]print=|/print/", "print views"),
)
# Pages more likely to lead to documents are read first.
_PROMISING = re.compile(
    r"download|document|doc|file|report|publication|librar|ebook|book|annual|form|"
    r"เอกสาร|ดาวน์โหลด|รายงาน|ประกาศ|คลัง|หนังสือ|แบบฟอร์ม|วารสาร", re.I)
# What a JavaScript application's HTML looks like before its scripts run.
_APP_MARKERS = (
    (re.compile(r"""<div[^>]+id=["'](?:root|app|__next|__nuxt|q-app)["'][^>]*>\s*</div>""", re.I),
     "an empty <div id=root/app>"),
    (re.compile(r"__NEXT_DATA__"), "Next.js"),
    (re.compile(r"window\.__NUXT__"), "Nuxt"),
    (re.compile(r"\bng-version="), "Angular"),
    (re.compile(r"<noscript>[^<]{0,300}javascript", re.I), "a <noscript> asking for JavaScript"),
)
_BOT_WALL = re.compile(r"just a moment|attention required|captcha|cf-chl|challenge-platform", re.I)
# A JavaScript call in an onclick or javascript: link whose name suggests it opens a file.
_FILE_CALL = re.compile(r"\b([A-Za-z_$][\w$]*(?:download|file|doc|pdf|attach)[\w$]*)\s*\(", re.I)
_HTML = ("text/html", "application/xhtml+xml")


@dataclass
class PageResult:
    url: str
    status: int | None
    docs: int = 0
    note: str = ""


@dataclass
class Report:
    start_url: str
    config_given: bool = False
    final_url: str = ""
    robots: str = "not checked (respect_robots = false)"
    crawl_delay: float | None = None
    gap: float = 0.0
    pages: list[PageResult] = field(default_factory=list)
    docs: dict[str, str] = field(default_factory=dict)  # document URL -> page it was found on
    site_pages: set[str] = field(default_factory=set)  # pages on the site the crawler would follow
    excluded: set[str] = field(default_factory=set)
    other_links: set[str] = field(default_factory=set)
    js_refs: dict[str, str] = field(default_factory=dict)  # snippet naming a file -> page
    js_calls: dict[str, list] = field(default_factory=dict)  # function -> [count, example, page]
    app_markers: list[str] = field(default_factory=list)
    start_text_chars: int = 0
    postback_pages: list[str] = field(default_factory=list)
    login_pages: list[str] = field(default_factory=list)
    robots_skipped: int = 0
    uncovered_docs: Counter = field(default_factory=Counter)  # -c config: doc hosts not in file_hosts
    verdict: str = ""
    message: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.verdict, 1)

    def exclude_suggestions(self) -> list[tuple[str, str, int, str]]:
        """(regex, what it is, links matched, an example) for URL families worth excluding."""
        out = []
        for pattern, label in EXCLUDE_HINTS:
            rx = re.compile(pattern, re.I)
            hits = sorted(u for u in self.site_pages if rx.search(u))
            if hits:
                out.append((pattern, label, len(hits), hits[0]))
        return out

    def suggested_config(self) -> dict:
        start = self.final_url or self.start_url
        host = urlsplit(start).netloc
        doc_hosts = Counter(urlsplit(u).netloc for u in self.docs)
        slug = re.sub(r"[^a-z0-9]+", "_", re.sub(r"^www\.", "", host.lower())).strip("_") or "site"
        return {
            "start_urls": [start],
            "crawl_hosts": [host],
            "file_hosts": [host] + sorted(h for h in doc_hosts if h != host),
            "exclude": list(DEFAULT_EXCLUDE) + [p for p, *_ in self.exclude_suggestions()],
            "output_dir": f"downloads_{slug}",
            "state_dir": f"state_{slug}",
        }


def run(start: str, cfg: Config | None = None, pages: int = 10, delay: float | None = None) -> Report:
    """Check a site. cfg: an existing config to test (its hosts, exclude, link_rules and site
    rules); without one, the generic rules on the start URL's host. pages: requests to make,
    the start page included. delay: seconds between them (default: the config's, or robots.txt's
    Crawl-delay when that is longer)."""
    url = urls.normalize(start)
    if not url:
        raise ValueError(f"not an http(s) URL: {start!r}")
    rep = Report(start_url=url, config_given=cfg is not None)
    if cfg is None:
        cfg = _generic_config(url)
    session = requests.Session()
    session.headers["User-Agent"] = cfg.user_agent
    if cfg.accept_language:
        session.headers["Accept-Language"] = cfg.accept_language
    robots = Robots(cfg.user_agent) if cfg.respect_robots else None
    rep.gap = cfg.delay if delay is None else delay
    if robots:
        rep.robots = robots.describe(url)
        rep.crawl_delay = robots.crawl_delay(url)
        if delay is None and rep.crawl_delay:
            rep.gap = max(rep.gap, rep.crawl_delay)
        if reason := robots.blocked(url):
            return _verdict(rep, BLOCKED, f"The start page may not be fetched: {reason}.")

    first = _fetch(session, url, cfg)
    rep.pages.append(PageResult(url, first.status, note=first.note))
    if first.error:
        return _verdict(rep, UNREACHABLE, f"Could not fetch the start page: {first.error}")
    rep.final_url = first.final_url
    if first.status >= 400:
        if first.bot_wall:
            return _verdict(rep, BLOCKED, f"Bot protection answered HTTP {first.status} (a challenge page, "
                                          "e.g. Cloudflare's). The crawler can't get past it.")
        return _verdict(rep, BLOCKED, f"The start page answered HTTP {first.status}.")
    if first.is_doc:
        return _verdict(rep, NOT_A_PAGE, f"The start URL is a document ({first.content_type}), not a page "
                                         "to crawl from. Start from the page that links to it.")
    if first.html is None:
        return _verdict(rep, NOT_A_PAGE, f"The start URL is not an HTML page ({first.content_type or 'no type'}).")
    final_host = urlsplit(first.final_url).netloc
    if not rep.config_given and not urls.host_matches(final_host, cfg.crawl_hosts):
        rep.notes.append(f"The start page redirects to another host ({final_host}); that host is the one to crawl.")
        cfg = _generic_config(first.final_url)

    queue: list[tuple[int, int, int, str, str]] = []  # (not promising, depth, order, url, found on)
    seen = {url, first.final_url}
    order = 0

    def analyze(page: _Fetched, depth: int) -> int:
        nonlocal order
        found = _analyze(rep, page, cfg, is_start=depth == 0)
        for link in sorted(rep.site_pages - seen):
            seen.add(link)
            order += 1
            heapq.heappush(queue, (0 if _PROMISING.search(unquote(link)) else 1, depth + 1, order, link,
                                   page.final_url))
        return found

    rep.pages[0].docs = analyze(first, 0)
    while len(rep.pages) < max(1, pages) and queue:
        _, depth, _, link, found_on = heapq.heappop(queue)
        if robots and robots.blocked(link):
            rep.robots_skipped += 1
            continue
        time.sleep(rep.gap)
        page = _fetch(session, link, cfg)
        result = PageResult(link, page.status, note=page.note)
        rep.pages.append(result)
        if page.is_doc:  # a link without a file extension that serves a document
            rep.docs.setdefault(page.final_url or link, found_on)
            result.docs = 1
        elif page.html is not None and page.status < 400 and urls.host_matches(
                urlsplit(page.final_url).netloc, cfg.crawl_hosts):
            result.docs = analyze(page, depth)
    return _decide(rep)


def _generic_config(url: str) -> Config:
    host = urlsplit(url).netloc
    return Config(start_urls=[url], crawl_hosts=[host], file_hosts=[host], exclude=list(DEFAULT_EXCLUDE))


@dataclass
class _Fetched:
    url: str
    status: int | None = None
    final_url: str = ""
    content_type: str = ""
    html: str | None = None
    is_doc: bool = False
    bot_wall: bool = False
    error: str = ""

    @property
    def note(self) -> str:
        if self.error:
            return self.error
        if self.is_doc:
            return f"document ({self.content_type or 'by its first bytes'})"
        if self.bot_wall:
            return "bot protection"
        if self.final_url and self.final_url != urls.normalize(self.url):
            return f"redirected to {self.final_url}"
        return "" if self.html is not None or self.status is None or self.status >= 400 else \
            f"not a page ({self.content_type or 'no type'})"


def _fetch(session: requests.Session, url: str, cfg: Config) -> _Fetched:
    """GET, reading the body only if it is HTML (or an error page); documents aren't downloaded."""
    out = _Fetched(url)
    try:
        r = session.get(url, stream=True, timeout=(15, cfg.timeout))
    except requests.RequestException as e:
        out.error = f"{type(e).__name__}: {e}"
        return out
    with r:
        out.status = r.status_code
        out.final_url = urls.normalize(r.url) or r.url
        out.content_type = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
        disp = r.headers.get("Content-Disposition")
        if r.status_code >= 400:
            body = _read_capped(r, 256 * 1024).decode("utf-8", errors="replace")
            cloudflare = "cloudflare" in r.headers.get("Server", "").lower() or any(
                h.lower().startswith("cf-") for h in r.headers)
            out.bot_wall = bool(_BOT_WALL.search(body)) or (cloudflare and r.status_code in (403, 503))
        elif out.content_type in _HTML and not (disp and disp.lower().lstrip().startswith("attachment")):
            out.html = _decode_html(_read_capped(r, MAX_HTML_BYTES), r.headers.get("Content-Type", ""))
        elif _is_document(out.content_type, out.final_url, disp, cfg.doc_exts):
            out.is_doc = True
        elif out.content_type in UNTYPED:
            out.is_doc = _sniff(next(r.iter_content(1024), b"")) is not None
    return out


def _analyze(rep: Report, page: _Fetched, cfg: Config, is_start: bool) -> int:
    """Sort a page's links into the report and look for what hides documents. Returns the number
    of documents linked from the page."""
    html = page.html or ""
    links = urls.extract_links(html, page.final_url, cfg)
    docs = 0
    for link in links:
        host = urlsplit(link).netloc
        ext = urls.extension(link)
        kind = urls.classify(link, cfg)
        if ext in cfg.doc_exts or kind == "file":
            if any(rx.search(link) for rx in cfg.exclude_rx):
                rep.excluded.add(link)
                continue
            docs += 1
            rep.docs.setdefault(link, page.final_url)
            if rep.config_given and not urls.host_matches(host, cfg.file_hosts):
                rep.uncovered_docs[host] += 1
        elif ext in cfg.skip_exts:
            continue
        elif urls.host_matches(host, cfg.crawl_hosts):
            (rep.site_pages if kind == "page" else rep.excluded).add(link)
        else:
            rep.other_links.add(link)

    soup = BeautifulSoup(html, urls._parser())
    names = {unquote(urlsplit(u).path.rsplit("/", 1)[-1]).lower() for u in links}
    doc_name = _doc_name_rx(frozenset(cfg.doc_exts))
    handlers = [v for tag in soup.find_all(True) for a, v in tag.attrs.items()
                if isinstance(v, str) and (a.lower().startswith("on") or v.lstrip().lower().startswith("javascript:"))]
    scripts = [s.get_text() for s in soup.find_all("script")]
    for text, is_handler in [(h, True) for h in handlers] + [(s, False) for s in scripts]:
        named = [(m, unquote(m.group(1).rsplit("/", 1)[-1]).lower()) for m in doc_name.finditer(text)]
        for m, name in named:
            if name not in names:
                snippet = " ".join(text[max(0, m.start() - 50):m.end() + 25].split())
                rep.js_refs.setdefault(snippet, page.final_url)
        # A call such as downloadFile(7) names no file; one whose file the crawler found is handled.
        if is_handler and not any(name in names for _, name in named):
            for m in _FILE_CALL.finditer(text):
                entry = rep.js_calls.setdefault(m.group(1), [0, " ".join(text.split())[:80], page.final_url])
                entry[0] += 1

    if "__doPostBack" in html:
        rep.postback_pages.append(page.final_url)
    if soup.find("input", attrs={"type": re.compile("^password$", re.I)}):
        rep.login_pages.append(page.final_url)
    if is_start:
        rep.app_markers = [label for rx, label in _APP_MARKERS if rx.search(html)]
        for tag in soup(["script", "style", "noscript", "template"]):
            tag.decompose()
        rep.start_text_chars = len(soup.get_text(" ", strip=True))
    return docs


def _doc_name_rx(exts: frozenset[str]) -> re.Pattern:
    # A file name with a document extension, bare or in a path: what a script passes around.
    return re.compile(r"""([^\s"'<>()\\,;=+`]+\.(?:%s))(?![\w])""" % "|".join(sorted(map(re.escape, exts))), re.I)


def _verdict(rep: Report, verdict: str, message: str) -> Report:
    rep.verdict, rep.message = verdict, message
    return rep


def _decide(rep: Report) -> Report:
    start_links = len(rep.site_pages) + len(rep.docs)
    looks_like_app = rep.app_markers and (rep.start_text_chars < 300 or start_links < 3)
    hidden = bool(rep.js_refs or rep.js_calls)
    if rep.postback_pages:
        rep.notes.append(f"{len(rep.postback_pages)} page(s) use ASP.NET __doPostBack: links or page numbers "
                         "that submit a form (POST) can't be followed, so what is behind them is missed.")
    if rep.login_pages:
        rep.notes.append(f"{len(rep.login_pages)} page(s) have a login form. Documents behind a login can't "
                         "be fetched.")
    if rep.robots_skipped:
        rep.notes.append(f"{rep.robots_skipped} page(s) were skipped because robots.txt disallows them.")
    if rep.crawl_delay and rep.crawl_delay > 1:
        rep.notes.append(f"robots.txt asks for {rep.crawl_delay:g} s between requests; the crawl will honour it.")
    for host, n in sorted(rep.uncovered_docs.items()):
        rep.notes.append(f"{n} document(s) on {host} won't be downloaded: add it to file_hosts.")
    if rep.app_markers and not looks_like_app:
        rep.notes.append(f"Built with {', '.join(rep.app_markers)}, but its HTML has the content and links "
                         "already, so that's fine.")

    if looks_like_app and not rep.docs:
        return _verdict(rep, JS_APP, f"This looks like a JavaScript application ({', '.join(rep.app_markers)}): "
                                     "its HTML is an almost empty shell that scripts fill in a browser. The "
                                     "crawler reads the HTML only, so it would find nothing. Look for the "
                                     "site's own API, or use a headless browser.")
    if rep.docs:
        if hidden:
            rep.notes.append("Some documents are opened by JavaScript instead of links (listed above). "
                             "Add link_rules to collect those too.")
        return _verdict(rep, READY, f"The crawler can collect this site: {len(rep.docs)} document link(s) "
                                    f"in {len(rep.pages)} page(s) read.")
    if hidden:
        return _verdict(rep, NEEDS_LINK_RULES, "Documents are opened by JavaScript, not by links (listed above). "
                                               "Add link_rules for those calls, then check again with -c.")
    if not rep.site_pages:
        return _verdict(rep, NO_LINKS, "The start page has no links the crawler can follow. If a browser "
                                       "shows links there, the page builds them with JavaScript.")
    return _verdict(rep, NO_DOCS_YET, f"Read {len(rep.pages)} page(s) and found {len(rep.site_pages)} more "
                                      "to crawl, but no documents yet. They may be deeper: check more pages "
                                      "(--pages 50) or start from the site's documents/downloads page.")


# ---------- output ----------

def format_report(rep: Report) -> str:
    show = urls.decode_component  # Thai URLs as people read them (configs keep them encoded)
    out = [f"Checked {show(rep.start_url)}",
           f"robots.txt: {rep.robots}" + (f", Crawl-delay {rep.crawl_delay:g} s" if rep.crawl_delay else ""),
           f"Read {len(rep.pages)} page(s), {rep.gap:g} s apart:", ""]
    out.append(f"  {'status':>6}  {'docs':>4}  page")
    for p in rep.pages:
        out.append(f"  {p.status if p.status is not None else '-':>6}  {p.docs:>4}  {show(p.url)}"
                   + (f"  ({show(p.note)})" if p.note else ""))
    out.append("")
    if rep.verdict not in (BLOCKED, UNREACHABLE, NOT_A_PAGE):
        out.append(f"Links found: {len(rep.docs)} documents, {len(rep.site_pages)} pages on the site to crawl, "
                   f"{len(rep.excluded)} excluded, {len(rep.other_links)} to other sites.")
        by_host = Counter(urlsplit(u).netloc for u in rep.docs)
        site = urlsplit(rep.final_url or rep.start_url).netloc
        for host, n in by_host.most_common():
            example = next(u for u in sorted(rep.docs) if urlsplit(u).netloc == host)
            out.append(f"  {n:>5}  {host}{'' if host == site else ' (another host)'}   e.g. {show(example)}")
        if rep.js_refs or rep.js_calls:
            out.append("")
            out.append("Opened by JavaScript, so invisible to the crawler without link_rules:")
            for snippet, page in list(rep.js_refs.items())[:8]:
                out.append(f"  {snippet}\n      on {show(page)}")
            for name, (n, example, page) in sorted(rep.js_calls.items(), key=lambda kv: -kv[1][0])[:5]:
                out.append(f"  {name}(...) x{n}, e.g. {example}\n      on {show(page)}")
        hints = rep.exclude_suggestions()
        if hints:
            out.append("")
            out.append("Worth excluding:")
            for pattern, label, n, example in hints:
                out.append(f"  '{pattern}'  {label}: {n} link(s), e.g. {show(example)}")
        out.append("")
    if rep.notes:
        out.append("Notes:")
        out.extend(f"  - {n}" for n in rep.notes)
        out.append("")
    out.append(f"Verdict: {rep.verdict}. {rep.message}")
    if rep.verdict in (READY, NEEDS_LINK_RULES, NO_DOCS_YET) and not rep.config_given:
        out.append("")
        out.append("Starting config (--write <file>.toml saves it, filled into config.example.toml):")
        out.extend("  " + line for line in _toml_lines(rep.suggested_config()))
    return "\n".join(out)


def _toml_lines(cfg: dict) -> list[str]:
    def value(key, v):
        if isinstance(v, list):
            quote = _literal if key == "exclude" else json.dumps
            return "[" + ", ".join(quote(x) for x in v) + "]"
        return json.dumps(v)
    return [f"{k} = {value(k, v)}" for k, v in cfg.items()]


def _literal(s: str) -> str:
    return f"'{s}'" if "'" not in s else json.dumps(s)


def write_config(rep: Report, path: str | Path) -> Path:
    """Save the suggested config as a new file: config.example.toml with the site's values filled in."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"{path} already exists; not overwritten")
    s = rep.suggested_config()
    head = (f"# Written by `python -m collector check {rep.start_url}` on {date.today().isoformat()}.\n"
            f"# Verdict: {rep.verdict}. {rep.message}\n\n")
    if TEMPLATE.exists():
        text = TEMPLATE.read_text(encoding="utf-8")
        lines = {k: f"{k} = {v}" for k, v in (ln.split(" = ", 1) for ln in _toml_lines(s))}
        text = _replace(text, r"(?ms)^start_urls = \[.*?^\]", "start_urls = [\n    %s,\n]" % json.dumps(s["start_urls"][0]))
        text = _replace(text, r"(?m)^crawl_hosts = .*$", lines["crawl_hosts"])
        text = _replace(text, r"(?m)^file_hosts = .*$", lines["file_hosts"])
        text = _replace(text, r'(?m)^output_dir = "[^"]*"', f'output_dir = "{s["output_dir"]}"')
        text = _replace(text, r'(?m)^state_dir = "[^"]*"', f'state_dir = "{s["state_dir"]}"')
        extra = [f"    {_literal(p)},  # {label}, e.g. {ex}" for p, label, _, ex in rep.exclude_suggestions()]
        if extra:
            text = _replace(text, r"(?m)^exclude = \[\n", "exclude = [\n" + "\n".join(extra) + "\n")
        text = head + text
    else:
        text = head + "\n".join(_toml_lines(s)) + "\n"
    path.write_text(text, encoding="utf-8")
    load_config(path)  # what was written must load
    return path


def _replace(text: str, pattern: str, new: str) -> str:
    out, n = re.subn(pattern, lambda m: new, text, count=1)
    if n != 1:
        raise ValueError(f"config.example.toml has no line matching {pattern!r}")
    return out
