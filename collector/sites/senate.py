"""Link quirks of www.senate.go.th (Thai Senate). Selected with `site = "senate"` in config.toml.

Also a worked example of site rules: each hook handles one thing the site does that the generic
crawler can't know about.
"""

from __future__ import annotations

import re

from . import SiteRules

# News attachments: <a href="javascript:showFileName(22619,'1_22619.pdf',1,1,1)"> opens
# /assets/portals/<portal>/news/<newsID>/<file>. The portal is written into the page's own
# showFileName function; the call's 4th argument is the fallback.
_SHOW_FILE = re.compile(r"showFileName\(\s*(\d+)\s*,\s*'([^']+)'\s*,\s*\d+\s*,\s*(\d+)")
_SHOW_FILE_PORTAL = re.compile(r"""/assets/portals/['"]\s*\+\s*(\d+)\s*\+\s*['"]/news/""")
# File links written without the leading slash ("assets/portals/93/..."), which resolve under the
# /view/... page. The files are at the site root.
_MISROOTED = re.compile(r"^(https?://[^/]+)/view/[^?]*?/(assets/portals/.*)$")


class SenateRules(SiteRules):
    name = "senate"

    def resolve_href(self, href: str) -> str:
        # Pagination is written href="/?&page41=2", which a browser would resolve to the site
        # root. The server means the current page, so resolve it like "?&page41=2".
        return href[1:] if href.startswith("/?") else href

    def fix_url(self, url: str) -> str:
        return _MISROOTED.sub(r"\1/\2", url)

    def extra_links(self, html: str, soup) -> list[str]:
        # The e-book viewer (/ebook/flipbook/...) names its PDF in <li key="pdf_url">/assets/...pdf</li>.
        links = [li.get_text(strip=True) for li in soup.find_all(attrs={"key": "pdf_url"})]
        links += _show_file_links(html)
        links += _expand_purchase_pagers(soup)
        return links


def _show_file_links(html: str) -> list[str]:
    m = _SHOW_FILE_PORTAL.search(html)
    return [f"/assets/portals/{m.group(1) if m else portal}/news/{news_id}/{name}"
            for news_id, name, portal in _SHOW_FILE.findall(html)]


def _expand_purchase_pagers(soup) -> list[str]:
    # The procurement tables page with JavaScript (twbsPagination): the pager is an empty
    # <ul data-group=N data-totalpages=M> and clicking sets ?page_gN=<page>&perpage=<n>.
    hrefs = []
    for ul in soup.select("ul.purchase-pager[data-group][data-totalpages]"):
        try:
            group = int(ul["data-group"])
            total = int(ul["data-totalpages"])
            perpage = int(ul.get("data-perpage") or 20)
        except ValueError:
            continue
        hrefs.extend(f"?page_g{group}={n}&perpage={perpage}" for n in range(2, total + 1))
    return hrefs


RULES = SenateRules()
