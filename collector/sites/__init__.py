"""Site rules: the link quirks of one particular website, kept out of the generic crawler.

A config selects them with `site = "<name>"`, which loads collector/sites/<name>.py. That module
defines RULES, an instance of SiteRules (or of a subclass overriding only what the site needs).
Without `site`, the crawler uses SiteRules() itself, whose hooks change nothing.

Quirks that a regex can describe don't need a module: see `link_rules` in config.example.toml.
"""

from __future__ import annotations

import importlib
import re


class SiteRules:
    """The generic crawler's behaviour: every hook leaves things as they are."""

    name = ""

    def resolve_href(self, href: str) -> str:
        """An href as written on a page, before it is resolved against the page's URL."""
        return href

    def fix_url(self, url: str) -> str:
        """A link after it has been resolved and normalized."""
        return url

    def extra_links(self, html: str, soup) -> list[str]:
        """More links (relative or absolute) found in ways the generic extractor doesn't know,
        such as a JavaScript call that opens a file, or a pager drawn by a script."""
        return []


GENERIC = SiteRules()

_MODULE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load(name: str) -> SiteRules:
    if not name:
        return GENERIC
    if not _MODULE_NAME.match(name):
        raise ValueError(f"site must name a module in collector/sites/, got {name!r}")
    module_name = f"{__name__}.{name}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as e:
        if e.name == module_name:
            raise ValueError(f"unknown site {name!r}: there is no collector/sites/{name}.py") from None
        raise
    rules = getattr(module, "RULES", None)
    if not isinstance(rules, SiteRules):
        raise ValueError(f"collector/sites/{name}.py must define RULES = <a SiteRules instance>")
    return rules
