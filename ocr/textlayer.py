"""Is a PDF page's own text layer good enough to use, or does the page need OCR?

Pure functions, so the rules can be tested without a PDF.
"""

from __future__ import annotations

import re
import unicodedata

# Thai glyph variants some fonts keep in the Private Use Area: tone marks and vowels shifted
# up or left, and ฐ/ญ without their lower tails. Text copied out of those PDFs looks right on
# screen but matches nothing in a search, so they are mapped back to the real characters.
PUA_THAI = str.maketrans({
    # Word's Symbol and Wingdings characters (U+F020-F0FF): ticks, crosses and boxes keep their
    # meaning (they fill in checklists); every other one is a bullet.
    **{c: "•" for c in range(0xF020, 0xF100)},
    0xF050: "✓", 0xF0FC: "✓", 0xF04F: "✗", 0xF052: "☑", 0xF0FE: "☑", 0xF06F: "□", 0xF0A8: "□",
    0xF700: "\u0E10", 0xF70F: "\u0E0D",                       # ฐ ญ
    0xF701: "\u0E34", 0xF702: "\u0E35", 0xF703: "\u0E36", 0xF704: "\u0E37",  # ิ ี ึ ื
    0xF705: "\u0E48", 0xF706: "\u0E49", 0xF707: "\u0E4A", 0xF708: "\u0E4B", 0xF709: "\u0E4C",
    0xF70A: "\u0E48", 0xF70B: "\u0E49", 0xF70C: "\u0E4A", 0xF70D: "\u0E4B", 0xF70E: "\u0E4C",
    0xF713: "\u0E48", 0xF714: "\u0E49", 0xF715: "\u0E4A", 0xF716: "\u0E4B", 0xF717: "\u0E4C",
    0xF710: "\u0E31", 0xF711: "\u0E4D", 0xF712: "\u0E47",     # ั ํ ็
    0xF718: "\u0E38", 0xF719: "\u0E39", 0xF71A: "\u0E3A",     # ุ ู ฺ
    # Another font's layout: vowels at F884-F889, then three variants each of ่ ้ ๊ ๋ ์
    # (ป[F884]ญหา = ปัญหา, เป[F889]น = เป็น, ใช[F88E] = ใช้, ยุทธศาสตร[F897] = ยุทธศาสตร์).
    **dict(zip(range(0xF884, 0xF899), "\u0E31\u0E34\u0E35\u0E36\u0E37\u0E47"
               + "\u0E48" * 3 + "\u0E49" * 3 + "\u0E4A" * 3 + "\u0E4B" * 3 + "\u0E4C" * 3)),
})
# Thai stored in the old TIS-620 encoding and read as Latin-1: ¹Ò§ÊÒÇ is นางสาว, each byte
# 0xA1-0xFB is the Thai letter 0x0D60 higher (μ, U+03BC, stands in for 0xB5, ต). Only runs that
# hold a consonant are converted, and only on a page with a long run, which ordinary Latin
# accents (café, § 12, 25°) never make.
_TIS620_RUN = re.compile("[\u00A1-\u00DA\u00DF-\u00FB\u03BC]{2,}")
_TIS620_LONG = re.compile("[\u00A1-\u00DA\u00DF-\u00FB\u03BC]{4,}")
_TIS620_CONSONANT = re.compile("[\u00A1-\u00CE\u03BC]")
# Typesetting that stores every Thai mark twice (สิิทธิิ, วุุฒิิสภา), sometimes with U+FFFD or a
# junk capital after the copy (ผู้้�, เรื่่Qอง) or before ำ (ทQำลาย). Thai never repeats the same
# mark. A capital anywhere else can be a real letter in another font (ผู้Iด้รับ: I is ไ): it stays.
_DOUBLED_MARK = re.compile(r"([\u0E31\u0E33-\u0E3A\u0E47-\u0E4E])\1+(?:[QRIJ](?=[\u0E01-\u0E4E]))?")
_JUNK_LETTER = re.compile(r"(?<=[\u0E01-\u0E2E])[QRIJ](?=\u0E33)")
# A font that draws sara am as mai tho + sara aa: ท้า for ทำ, น้้า for น้ำ, จ้าหน่าย for จำหน่าย.
# Its tell is a tone mark stacked on mai tho before sara aa, with no other mark doubled (the
# typesetting above doubles every kind: เร่่ง, ปีี, กัับ). Which ้า are real (ข้าว) can't be
# told, so such a page needs OCR.
_TONE_ON_MAI_THO = re.compile("[\u0E48-\u0E4B]\u0E49\u0E32")
_DOUBLED_NOT_MAI_THO = re.compile(r"([\u0E31\u0E33-\u0E3A\u0E47\u0E48\u0E4A-\u0E4E])\1")
# Sara am stored as nikhahit + sara aa (ํา), sometimes with a tone mark in between.
_SARA_AM = re.compile("\u0E4D([\u0E48-\u0E4B]?)\u0E32")
# Sara am whose nikhahit came out as a space, as Word-made PDFs often do: ท าหนังสือ, ส านัก.
# Sara aa can never start a word, so a space before it is always this, except in letter-spaced
# headings (จุ ล นิ ติ), which are left alone. Followed by a letter it is never letter-spacing.
_SPLIT_SARA_AM = re.compile("([ก-ฮ][่-๋]?)[  ][าำ]")
_SPLIT_SARA_AM_IN_WORD = re.compile(_SPLIT_SARA_AM.pattern + "(?=[ก-๎])")
# A stray Latin letter inside a Thai word, from a broken font: ดĞำเนินการ.
_STRAY_LATIN = re.compile("(?<=[ก-๎])[Ā-ɏ](?=[ก-๎])")
_BLANK_LINES = re.compile(r"\n{3,}")

# Below this many characters a page is treated as blank or near blank.
MIN_TEXT = 20
# An image this share of the page or more makes it a scan, unless plenty of visible text sits on
# top of it (a typed page on a letterhead or background image is not a scan).
SCAN_COVERAGE = 0.5
TRUSTED_VISIBLE_TEXT = 200


def normalize(text: str) -> str:
    text = _from_tis620(unicodedata.normalize("NFC", text).translate(PUA_THAI))
    text = _DOUBLED_MARK.sub("\\1", text.replace("\ufffd", ""))
    text = _JUNK_LETTER.sub("", text)
    text = _STRAY_LATIN.sub("", _SARA_AM.sub("\\1\u0E33", text))
    text = "\n".join(_repair_line(line.rstrip()) for line in text.splitlines())
    return _BLANK_LINES.sub("\n\n", text).strip()


def _from_tis620(text: str) -> str:
    if not _TIS620_LONG.search(text):
        return text

    def thai(m: re.Match) -> str:
        run = m.group()
        if not _TIS620_CONSONANT.search(run):
            return run
        return "".join("\u0E15" if c == "\u03BC" else chr(ord(c) + 0x0D60) for c in run)

    return _TIS620_RUN.sub(thai, text)


def sara_am_as_mai_tho(raw: str) -> bool:
    """The page's font draws sara am as mai tho + sara aa (see _TONE_ON_MAI_THO)."""
    raw = unicodedata.normalize("NFC", raw).translate(PUA_THAI)
    return bool(_TONE_ON_MAI_THO.search(raw)) and not _DOUBLED_NOT_MAI_THO.search(raw)


def needs_another_look(text: str) -> bool:
    """Stored text-layer text that the rules above would now read differently."""
    return bool(_TIS620_LONG.search(text) or "\ufffd" in text or _DOUBLED_MARK.search(text)
                or _JUNK_LETTER.search(text) or _TONE_ON_MAI_THO.search(text)
                or any("\ue000" <= c <= "\uf8ff" for c in text))


def _repair_line(line: str) -> str:
    line = _SPLIT_SARA_AM_IN_WORD.sub("\\1\u0E33", line)
    tokens = line.split()
    if len(tokens) >= 4 and max(map(len, tokens)) <= 4:
        return line  # letter-spaced
    return _SPLIT_SARA_AM.sub("\\1\u0E33", line)


def garbled(text: str, raw: str | None = None) -> bool:
    """A text layer that decodes to junk: Thai stored in a legacy encoding comes out as Latin-1
    accents (àÃ×èͧ), a font without a character map comes out as punctuation and symbols.
    raw: the text before normalize(), for faults that normalizing hides."""
    chars = [c for c in text if not c.isspace()]
    if len(chars) < MIN_TEXT:
        return False
    if raw is not None and sara_am_as_mai_tho(raw):
        return True
    bad = sum(1 for c in chars
              if "\u0080" <= c <= "\u00ff" or "\ue000" <= c <= "\uf8ff" or c == "\ufffd" or c < " ")
    if bad / len(chars) > 0.1:
        return True
    # A font that maps sara aa to sara am (ตำมมำตรำ for ตามมาตรา). Real Thai has several า for
    # every ำ; which ำ are genuine can't be told, so the page needs OCR.
    sara_am = text.count("ำ")
    if sara_am >= 10 and text.count("า") < sara_am:
        return True
    letters = sum(c.isalpha() for c in chars)
    digits = sum(c.isdigit() for c in chars)
    # Tables of figures are mostly digits, and fine.
    return letters / len(chars) < 0.2 and digits / len(chars) < 0.5


def decide(text: str, visible_chars: int, coverage: float, drawings: int = 0,
           raw: str | None = None) -> tuple[str, str]:
    """('text' | 'ocr', reason).

    text: the page's normalized text layer. visible_chars: characters actually painted (a
    scanner's own OCR layer is invisible text over the image, and of unknown quality).
    coverage: share of the page covered by images. drawings: vector paths on a page without
    text or images, i.e. text converted to outlines. raw: the text layer before normalize()."""
    if garbled(text, raw):
        return "ocr", "unreadable text layer"
    if coverage >= SCAN_COVERAGE and visible_chars < TRUSTED_VISIBLE_TEXT:
        return "ocr", "scanned page"
    if len(text) >= MIN_TEXT:
        return "text", ""
    if coverage >= 0.1:
        return "ocr", "little text, mostly image"
    if drawings >= 200:
        return "ocr", "text drawn as shapes"
    return "text", "blank"
