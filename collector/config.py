"""Load crawler settings from a TOML file."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import sites

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.10, e.g. Ubuntu 22.04's
    import tomli as tomllib

# File types downloaded by default. `extra_file_exts` adds more.
DOC_EXTS = frozenset({
    "pdf", "doc", "docx", "docm", "dot", "dotx", "xls", "xlsx", "xlsm", "csv",
    "ppt", "pptx", "pps", "ppsx", "odt", "ods", "odp", "rtf", "epub", "mobi",
    "azw3", "djvu", "zip", "rar", "7z", "tar", "gz",
})
# Never fetched by default (page furniture, media, fonts), unless named in `extra_file_exts`.
SKIP_EXTS = frozenset({
    "jpg", "jpeg", "png", "gif", "bmp", "webp", "svg", "ico", "tif", "tiff",
    "css", "js", "map", "json", "woff", "woff2", "ttf", "eot", "otf",
    "mp3", "mp4", "m4a", "m4v", "wav", "ogg", "webm", "avi", "mov", "wmv", "flv",
    "swf", "exe", "msi", "apk", "dmg", "iso",
})
# Download endpoints with no file extension (".../FileDownloadProxy/download?s=...") on file hosts
# that aren't crawled.
DEFAULT_DOWNLOAD_PATTERNS = ("download", "[?&]dl=", "getfile", "fileid=")


@dataclass
class Config:
    start_urls: list[str]
    # Hosts whose HTML pages are fetched and mined for links. "*.example.com" matches any subdomain.
    crawl_hosts: list[str]
    # Hosts documents may be downloaded from. "*.example.com" matches any subdomain.
    file_hosts: list[str]
    # Regexes; any URL matching one is never fetched.
    exclude: list[str] = field(default_factory=list)
    # Site rules: a module in collector/sites/ for one site's link quirks ("senate"). "" = none.
    site: str = ""
    # Links a regex can find in a page's HTML: [{find = "<regex>", url = "<template>"}], where the
    # template's {1}, {2}... are the regex's groups ({0} is the whole match).
    link_rules: list[dict] = field(default_factory=list)
    # Regexes for URLs on a crawl host that always lead to a document (e.g. "[?&]dl=\d"): queued as
    # downloads instead of pages. Others are fetched as pages and saved if they turn out to be files.
    crawl_host_download_patterns: list[str] = field(default_factory=list)
    # Regexes for extension-less download URLs on file hosts that aren't crawled.
    download_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_DOWNLOAD_PATTERNS))
    # More file types to download, e.g. ["txt", "xml", "jpg"].
    extra_file_exts: list[str] = field(default_factory=list)
    output_dir: str = "downloads"
    state_dir: str = "state"
    workers: int = 4  # download threads (pages have their own: pages_per_host per crawl host)
    delay: float = 1.0  # normal seconds between two requests to the same host
    max_per_host: int = 2  # downloads open at once to one host
    pages_per_host: int = 1  # page requests open at once to one host, on top of the downloads
    max_delay: float = 60.0  # ceiling for the spacing when a host is struggling
    timeout: float = 60.0
    max_retries: int = 3
    max_file_mb: float = 0.0  # 0 = no limit
    respect_robots: bool = True
    user_agent: str = "Mozilla/5.0 (compatible; collectorBot/0.1; document archiver)"
    accept_language: str = "th,en;q=0.8"

    def __post_init__(self) -> None:
        self.crawl_hosts = [h.lower() for h in self.crawl_hosts]
        self.file_hosts = [h.lower() for h in self.file_hosts]
        self.exclude_rx = [re.compile(p, re.I) for p in self.exclude]
        self.crawl_host_download_rx = [re.compile(p, re.I) for p in self.crawl_host_download_patterns]
        self.download_rx = [re.compile(p, re.I) for p in self.download_patterns]
        extra = {e.lower().lstrip(".") for e in self.extra_file_exts}
        self.doc_exts = DOC_EXTS | extra
        self.skip_exts = SKIP_EXTS - extra
        self.link_rules_rx = [LinkRule.parse(r) for r in self.link_rules]
        self.rules = sites.load(self.site)

    @property
    def db_path(self) -> Path:
        return Path(self.state_dir) / "collector.db"

    @property
    def log_path(self) -> Path:
        return Path(self.state_dir) / "collector.log"

    @property
    def lock_path(self) -> Path:
        return Path(self.state_dir) / "crawl.lock"


@dataclass
class LinkRule:
    find: re.Pattern
    url: str

    @classmethod
    def parse(cls, rule: dict) -> LinkRule:
        if not isinstance(rule, dict) or set(rule) != {"find", "url"}:
            raise ValueError(f"a link rule needs exactly `find` and `url`, got {rule!r}")
        find = re.compile(rule["find"])
        try:  # a template naming a group the regex doesn't have fails now, not mid-crawl
            rule["url"].format(*[""] * (find.groups + 1))
        except (IndexError, KeyError, ValueError) as e:
            raise ValueError(f"link rule url {rule['url']!r} doesn't fit {rule['find']!r}: {e}") from None
        return cls(find, rule["url"])

    def links(self, html: str) -> list[str]:
        return [self.url.format(m.group(0), *(g or "" for g in m.groups())) for m in self.find.finditer(html)]


def load_config(path: str | Path) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    known = Config.__dataclass_fields__.keys()
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"unknown config keys in {path}: {', '.join(sorted(unknown))}")
    return Config(**data)
