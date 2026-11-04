"""Reading PDFs with PyMuPDF. These run in worker processes: MuPDF is not thread-safe, and a
malformed file that crashes it then takes down one worker instead of the whole run."""

from __future__ import annotations

import signal

import pymupdf

from .textlayer import decide, normalize

pymupdf.TOOLS.mupdf_display_errors(False)  # malformed-PDF warnings would flood the log

_open: dict[str, pymupdf.Document] = {}

# Ink: the share of a page's pixels darker than mid-grey, at a low resolution. Text pages have
# 0.3% or more; a page with none is empty. A page with no text layer and no image but some ink
# carries text or a chart drawn some other way (outlines, fonts without a character map).
_INK_LUT = bytes(1 if b < 128 else 0 for b in range(256))
INK_CONTENT = 0.001
HALF_OVERLAP = 0.02  # each half reaches this share of the page past the middle: no line is cut


def worker_init() -> None:
    # Ctrl+C reaches every process in the terminal. Only the main one should act on it: it
    # stops handing out work and lets what is running finish.
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _document(path: str) -> pymupdf.Document:
    # Pages of one document are queued together, so keeping the last one open saves re-parsing
    # a 300 MB file for each of its pages.
    if path not in _open:
        for doc in _open.values():
            doc.close()
        _open.clear()
        _open[path] = pymupdf.open(path)
    return _open[path]


def inspect(path: str) -> list[dict]:
    """One record per page: whether its text layer is usable, and the text if it is."""
    pages = []
    with pymupdf.open(path) as doc:
        if doc.needs_pass and not doc.authenticate(""):
            raise ValueError("password-protected")
        for page in doc:
            try:
                pages.append(_inspect_page(page))
            except Exception as exc:  # one odd page must not lose the document
                pages.append({"page": page.number + 1, "method": "ocr",
                              "reason": f"unreadable page ({type(exc).__name__})", "text": ""})
    return pages


def _inspect_page(page: pymupdf.Page) -> dict:
    raw = page.get_text()
    text = normalize(raw)
    area = abs(page.rect) or 1.0
    coverage = 0.0
    for image in page.get_image_info():
        box = pymupdf.Rect(image["bbox"]) & page.rect
        if not box.is_empty:
            coverage += abs(box) / area  # summed: some scans are split into strips
    coverage = min(coverage, 1.0)
    try:
        visible = sum(len(span["chars"]) for span in page.get_texttrace()
                      if span["type"] != 3 and span.get("opacity", 1) > 0)
    except Exception:
        visible = len(text)
    drawings = len(page.get_cdrawings()) if len(text) < 20 and coverage < 0.1 else 0
    method, reason = decide(text, visible, coverage, drawings, raw)
    if reason == "blank" and ink(page) >= INK_CONTENT:
        method, reason = "ocr", "content without a text layer"
    return {"page": page.number + 1, "method": method, "reason": reason,
            "text": text if method == "text" else ""}


def ink(page: pymupdf.Page) -> float:
    samples = page.get_pixmap(dpi=50, colorspace=pymupdf.csGRAY, alpha=False).samples
    return samples.translate(_INK_LUT).count(1) / max(len(samples), 1)


def _jpeg(page: pymupdf.Page, longest_px: int, clip: pymupdf.Rect | None = None) -> bytes:
    area = clip or page.rect
    zoom = longest_px / (max(area.width, area.height) or 1)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
    return pix.tobytes("jpeg", jpg_quality=85)


def render(path: str, page_no: int, longest_px: int) -> bytes:
    """The page as a JPEG whose longer side is longest_px pixels."""
    return _jpeg(_document(path)[page_no - 1], longest_px)


def render_page(path: str, page_no: int, longest_px: int) -> tuple[bytes, float]:
    """The page as a JPEG, and its ink (an empty page needs no reading)."""
    page = _document(path)[page_no - 1]
    return _jpeg(page, longest_px), ink(page)


def render_halves(path: str, page_no: int, longest_px: int) -> list[bytes]:
    """The top and bottom halves of the page, slightly overlapping, each as its own JPEG: half
    as much to read per request, at a higher resolution."""
    page = _document(path)[page_no - 1]
    r = page.rect
    middle, overlap = r.y0 + r.height / 2, r.height * HALF_OVERLAP
    return [_jpeg(page, longest_px, pymupdf.Rect(r.x0, r.y0, r.x1, middle + overlap)),
            _jpeg(page, longest_px, pymupdf.Rect(r.x0, middle - overlap, r.x1, r.y1))]


# Strips: a page cut across where there is no ink, between lines of text, so no line is cut in two.
# Each cut is looked for within this share of a strip's height around its even position; a row with
# ink below BLANK_ROW of its pixels counts as blank (scanner specks), and one at least GAP_PX from
# any ink is preferred (the middle of the gap between two lines, not the edge of one).
STRIP_SEARCH = 0.25
BLANK_ROW = 0.002
GAP_PX = 4


def strip_cuts(page: pymupdf.Page, n: int, band: pymupdf.Rect) -> list[tuple[float, bool]]:
    """Where to cut that band of the page into n strips, top to bottom (ends included): (y in
    points, whether the cut falls between lines). A cut through ink needs the strips on both
    sides to overlap."""
    pix = page.get_pixmap(dpi=72, colorspace=pymupdf.csGRAY, alpha=False, clip=band)
    w, h = pix.width, pix.height
    rows = pix.samples
    inked = [rows[y * pix.stride:y * pix.stride + w].translate(_INK_LUT).count(1) > w * BLANK_ROW
             for y in range(h)]
    clear, last = [0] * h, -GAP_PX  # rows to the nearest ink, capped at GAP_PX
    for y in range(h):
        last = y if inked[y] else last
        clear[y] = min(y - last, GAP_PX)
    last = h + GAP_PX
    for y in reversed(range(h)):
        last = y if inked[y] else last
        clear[y] = min(clear[y], last - y)
    cuts = [(0, True)]
    for k in range(1, n):
        target, reach = h * k / n, h * STRIP_SEARCH / n
        window = range(max(cuts[-1][0] + 1, int(target - reach)), min(h - 1, int(target + reach)) + 1)
        y = min(window, key=lambda y: (-clear[y], abs(y - target)))
        cuts.append((y, clear[y] > 0))
    cuts.append((h, True))
    return [(band.y0 + y * band.height / h, between) for y, between in cuts]


def render_strips(path: str, page_no: int, longest_px: int, n: int, part: tuple[float, float] = (0.0, 1.0)
                  ) -> list[tuple[bytes, tuple[float, float]]]:
    """The page, or the part of it between those shares of its height, cut across into n strips,
    each its own JPEG at a higher resolution: [(image, (top, bottom) as shares of the page)]."""
    page = _document(path)[page_no - 1]
    r = page.rect
    band = pymupdf.Rect(r.x0, r.y0 + part[0] * r.height, r.x1, r.y0 + part[1] * r.height)
    overlap = r.height * HALF_OVERLAP
    cuts = strip_cuts(page, n, band)
    out = []
    for (y0, between0), (y1, between1) in zip(cuts, cuts[1:]):
        a = y0 if between0 else max(band.y0, y0 - overlap)
        b = y1 if between1 else min(band.y1, y1 + overlap)
        out.append((_jpeg(page, longest_px, pymupdf.Rect(r.x0, a, r.x1, b)),
                    ((a - r.y0) / r.height, (b - r.y0) / r.height)))
    return out
