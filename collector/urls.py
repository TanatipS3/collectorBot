"""URL normalization, scope rules and link extraction."""

from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import parse_qs, quote, unquote_to_bytes, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .config import DOC_EXTS, SKIP_EXTS, Config  # noqa: F401  (re-exported)
from .sites import GENERIC, SiteRules

_HEX = re.compile(r"%[0-9a-fA-F]{2}")
_PATH_SAFE = "/%:@!$&'()*+,;=-._~"
_QUERY_SAFE = _PATH_SAFE + "?"
_JUNK = ("${", "{{", "javascript:", "mailto:", "tel:", "data:", "about:")
_META_REFRESH = re.compile(r"url\s*=\s*(.+)$", re.I)
_LINK_ATTRS = ("href", "src", "data", "data-src", "data-href", "data-url", "data-file")


@lru_cache(maxsize=8)
def _doc_in_text(exts: frozenset[str]) -> re.Pattern:
    # A quoted path ending in a document extension, inside a script, an onclick or a viewer config.
    # No ";" so a meta refresh's content="0;URL=/x.pdf" is not mistaken for a path (the refresh
    # target is extracted separately). Spaces are allowed: real file names have them
    # ('/assets/.../6(1).2 รายงานงบดุล 66.pdf').
    return re.compile(
        r"""["']([^"'<>\r\n\t;]+\.(?:%s))(?:[?#][^"'<>\s]*)?["']""" % "|".join(sorted(map(re.escape, exts))),
        re.I,
    )


def normalize(url: str) -> str | None:
    """Canonical form used for de-duplication, or None if the URL is not crawlable."""
    url = url.strip()
    if not url or any(j in url.lower() for j in _JUNK):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if port and not (parts.scheme == "http" and port == 80 or parts.scheme == "https" and port == 443):
        host = f"{host}:{port}"
    path = _requote(parts.path, _PATH_SAFE) or "/"
    # Drop empty params so "?&page41=2" and "?page41=2" are the same URL.
    query = "&".join(p for p in _requote(parts.query, _QUERY_SAFE).split("&") if p)
    return urlunsplit((parts.scheme.lower(), host, path, query, ""))


def _requote(s: str, safe: str) -> str:
    # Keep existing %XX escapes (some may be TIS-620, not UTF-8), encode raw characters
    # as UTF-8, and upper-case hex so both spellings of a URL compare equal.
    return _HEX.sub(lambda m: m.group(0).upper(), quote(s, safe=safe))


def resolve(base: str, href: str, site: SiteRules = GENERIC) -> str:
    href = site.resolve_href(href.strip().replace("&amp;", "&"))
    return urljoin(base, href)


def extension(url: str) -> str:
    last = urlsplit(url).path.rsplit("/", 1)[-1]
    stem, dot, ext = decode_component(last).rpartition(".")
    # A bare ".PDF" (seen in book_shelf's "statpdf.php?doc=.PDF") is not a file name.
    return ext.lower() if dot and stem else ""


def decode_component(s: str) -> str:
    raw = unquote_to_bytes(s)
    for enc in ("utf-8", "cp874"):  # cp874 is Windows' superset of TIS-620 (Thai)
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def host_matches(host: str, patterns: list[str]) -> bool:
    for p in patterns:
        if p.startswith("*."):
            if host == p[2:] or host.endswith(p[1:]):
                return True
        elif host == p:
            return True
    return False


def classify(url: str, cfg: Config) -> str | None:
    """'page' (crawl for links), 'file' (download), or None (ignore)."""
    if any(rx.search(url) for rx in cfg.exclude_rx):
        return None
    host = urlsplit(url).netloc
    ext = extension(url)
    if ext in cfg.skip_exts:
        return None
    if ext in cfg.doc_exts:
        return "file" if host_matches(host, cfg.file_hosts) else None
    if host_matches(host, cfg.crawl_hosts):
        if host_matches(host, cfg.file_hosts) and any(rx.search(url) for rx in cfg.crawl_host_download_rx):
            return "file"  # so it waits for a download slot, not the page slot
        return "page"  # may still turn out to be a document; decided by Content-Type
    if host_matches(host, cfg.file_hosts) and any(rx.search(url) for rx in cfg.download_rx):
        return "file"
    return None


def extract_links(html: str, base_url: str, cfg: Config | None = None) -> set[str]:
    """Every link on a page, normalized. Without a config: the generic rules and file types."""
    site = cfg.rules if cfg else GENERIC
    doc_exts = cfg.doc_exts if cfg else DOC_EXTS
    soup = BeautifulSoup(html, _parser())
    base_tag = soup.find("base", href=True)
    base = urljoin(base_url, base_tag["href"]) if base_tag else base_url

    raw: set[str] = set()
    for tag in soup.find_all(True):
        for attr in _LINK_ATTRS:
            val = tag.get(attr)
            if isinstance(val, str) and val:
                raw.add(val)
    # <meta http-equiv="refresh" content="0;URL=/document/Ext27/27143_0000.PDF">
    for meta in soup.find_all("meta", attrs={"http-equiv": re.compile("^refresh$", re.I)}):
        m = _META_REFRESH.search(meta.get("content") or "")
        if m:
            raw.add(m.group(1).strip("'\" "))
    # Document paths that only appear inside scripts, onclick handlers or viewer configs. Only
    # paths: a bare name ('1_22619.pdf') is an argument to some script, not a link.
    raw.update(m.group(1) for m in _doc_in_text(frozenset(doc_exts)).finditer(html) if "/" in m.group(1))
    for rule in (cfg.link_rules_rx if cfg else ()):
        raw.update(rule.links(html))
    raw.update(site.extra_links(html, soup))

    links = set()
    for href in raw:
        url = normalize(resolve(base, href, site))
        if not url:
            continue
        url = site.fix_url(url)
        links.add(url)
        # PDF viewers carry the real document in the query: viewer.html?file=/x/book.pdf
        for values in parse_qs(urlsplit(url).query).values():
            for v in values:
                if extension(v) in doc_exts and (doc := normalize(urljoin(url, v))):
                    links.add(doc)
    return links


def _parser() -> str:
    try:
        import lxml  # noqa: F401
    except ImportError:
        return "html.parser"
    return "lxml"
