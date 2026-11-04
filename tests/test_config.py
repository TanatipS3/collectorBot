from pathlib import Path

from collector.config import load_config, tomllib
from collector.urls import extract_links

ROOT = Path(__file__).resolve().parent.parent


def test_senate_config_loads_with_its_site_rules():
    cfg = load_config(ROOT / "config.toml")
    assert cfg.site == "senate" and cfg.rules.name == "senate"
    assert cfg.crawl_host_download_rx


def test_example_config_loads_and_is_generic():
    cfg = load_config(ROOT / "config.example.toml")
    assert cfg.site == "" and cfg.link_rules == []
    assert cfg.output_dir != "downloads" and cfg.state_dir != "state"  # never mixed with the Senate crawl


def test_example_link_rule_works_once_uncommented():
    text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.strip().startswith("# { find ="))
    data = tomllib.loads("link_rules = [" + line.strip()[2:] + "]")
    from collector.config import Config
    cfg = Config(start_urls=[], crawl_hosts=["www.example.go.th"], file_hosts=["www.example.go.th"], **data)
    html = """<button onclick="openDoc(42, 'report.pdf')">open</button>"""
    assert "https://www.example.go.th/docs/42/report.pdf" in extract_links(html, "https://www.example.go.th/", cfg)
