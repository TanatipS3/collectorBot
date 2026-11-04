"""Load OCR settings from a TOML file (ocr.toml)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from collector.config import tomllib


@dataclass
class OcrConfig:
    # What the crawl produced: its state database (the file -> URL record) and the files.
    collector_db: str = "state/collector.db"
    downloads_dir: str = "downloads"
    output_dir: str = "ocr_out"
    # The classification's final labels (doc_id, label), added to mapping.csv; not there yet = blank.
    labels_csv: str = "classify/final/doc_labels_final.csv"
    # Names an LLM gave each document (doc_id, alt_name), for mapping.csv's alt_file_name; a
    # document without one gets its first line of content.
    alt_names_csv: str = "classify/final/alt_names.csv"
    # The OCR engine: an OpenAI-compatible vision endpoint.
    primary_url: str = "http://localhost:8001/v1"
    primary_model: str = "ptm-llm-vision-3.0.0-preview"
    primary_key_env: str = ""  # name of the environment variable holding its API key, if any
    # Used for a page only when the primary is down or refuses it. Blank backup_url = no backup.
    backup_url: str = "https://tokenmind.pathumma.in.th/v1"
    backup_model: str = "ocr/olmocr"
    backup_key_env: str = "TOKENMIND_API_KEY"
    workers: int = 8  # OCR requests in flight at once to the primary
    backup_workers: int = 0  # pages the backup reads at once as a second engine; 0 = fallback only
    processes: int = 4  # processes reading and rendering PDFs
    render_px: int = 1600  # longer side of the page image sent to the model
    max_tokens: int = 4096
    timeout: float = 300.0  # seconds for one OCR request
    attempts: int = 3  # tries per engine for one page
    max_page_attempts: int = 3  # runs that retry a page that failed

    @property
    def out(self) -> Path:
        return Path(self.output_dir)

    @property
    def db_path(self) -> Path:
        return self.out / "ocr.db"

    @property
    def log_path(self) -> Path:
        return self.out / "ocr.log"

    @property
    def lock_path(self) -> Path:
        return self.out / "ocr.lock"

    @property
    def text_dir(self) -> Path:
        return self.out / "text"

    @property
    def mapping_path(self) -> Path:
        return self.out / "mapping.csv"


def load_config(path: str | Path) -> OcrConfig:
    data = {}
    if Path(path).is_file():
        with open(path, "rb") as f:
            data = tomllib.load(f)
    unknown = set(data) - set(OcrConfig.__dataclass_fields__)
    if unknown:
        raise ValueError(f"unknown config keys in {path}: {', '.join(sorted(unknown))}")
    cfg = OcrConfig(**data)
    # The names the other services on the server use.
    cfg.primary_url = os.environ.get("TOKENMIND_OCR_BASE_URL") or cfg.primary_url
    cfg.primary_model = os.environ.get("TOKENMIND_OCR_MODEL") or cfg.primary_model
    # Their containers reach the model as host.docker.internal; on the host itself it is localhost.
    if not Path("/.dockerenv").exists():
        cfg.primary_url = cfg.primary_url.replace("host.docker.internal", "localhost")
    return cfg


def api_key(env_name: str, env_file: Path = Path(".env")) -> str:
    """From the environment, or from a KEY=value line in .env (kept out of git)."""
    if not env_name:
        return ""
    if os.environ.get(env_name):
        return os.environ[env_name]
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{env_name}="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""
