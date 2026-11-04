import pymupdf

from ocr import pdf
from ocr.textlayer import decide, garbled, needs_another_look, normalize

THAI = "การประชุมวุฒิสภา ครั้งที่ 12 ปีที่ 2 วันพฤหัสบดีที่ 18 กันยายน 2568 ระเบียบวาระการประชุม"


def test_normalize_maps_thai_glyph_variants_and_sara_am():
    assert normalize("ป") == "ปิ"  # ปิ with a shifted sara i
    assert normalize("อ") == "อ้"  # อ้ with a lowered mai tho
    assert normalize("นํ้า") == "น้ำ"  # nikhahit, tone, sara aa
    assert normalize("น้ํา") == "น้ำ"
    assert normalize("a  \n\n\n\nb") == "a\n\nb"


def test_normalize_repairs_sara_am_split_by_a_space():
    # As extracted from real Senate PDFs made in Word.
    assert normalize("เลขาธิการวุฒิสภา ท าหนังสือ หรือยื่นแบบค าร้อง") == "เลขาธิการวุฒิสภา ทำหนังสือ หรือยื่นแบบคำร้อง"
    assert normalize("ส านักการคลัง ก าหนดไว้ จ าเป็น") == "สำนักการคลัง กำหนดไว้ จำเป็น"
    assert normalize("น้ าท่วม") == "น้ำท่วม"
    assert normalize("ประจ า ปี") == "ประจำ ปี"
    assert normalize("ต้องระวางโทษจ าคุกตั้งแต่ ๑ ปี ถึง ๕ ปี") == "ต้องระวางโทษจำคุกตั้งแต่ ๑ ปี ถึง ๕ ปี"
    assert normalize("๓ ส านัก   ทาน ๒") == "๓ สำนัก   ทาน ๒"
    # Letter-spaced headings and real spaces before other vowels are left alone.
    assert normalize("ก า ร ป ร ะ ชุ ม") == "ก า ร ป ร ะ ชุ ม"
    assert normalize("จุ ล นิ ติ") == "จุ ล นิ ติ"
    assert normalize("วุฒิสภา เป็น") == "วุฒิสภา เป็น"
    assert normalize("ดĞำเนินการ ในเรือนจĞำ") == "ดำเนินการ ในเรือนจำ"


def test_normalize_decodes_thai_stored_as_tis620_and_read_as_latin1():
    # A Legal Bureau byline as extracted: the author's name, post and office.
    assert normalize("¹Ò§ÊÒÇà¾ÅÔ¹μÒ ¹ÔμÔ¡Ã ÊÓ¹Ñ¡¡®ËÁÒÂ") == "นางสาวเพลินตา นิติกร สำนักกฎหมาย"
    # Ordinary Latin accents and signs are left alone.
    assert normalize("café au lait § 12 อุณหภูมิ 25°C") == "café au lait § 12 อุณหภูมิ 25°C"


def test_normalize_repairs_marks_stored_twice_and_their_junk():
    # As extracted from the typeset journals: every mark twice, a junk letter or U+FFFD after.
    assert normalize("สิิทธิิ วุุฒิิสภา ผู้้�บริโภค เรื่่Qอง ที่่Iเหมาะสม ทQำลาย สำำนััก") == \
        "สิทธิ วุฒิสภา ผู้บริโภค เรื่อง ที่เหมาะสม ทำลาย สำนัก"
    assert normalize("ระบบ IT และระบบIT") == "ระบบ IT และระบบIT"  # real Latin words stay
    assert normalize("ผู้Iด้รับการคัดเลือก") == "ผู้Iด้รับการคัดเลือก"  # another font's ไ, not junk


def test_normalize_maps_another_fonts_tone_marks_and_word_bullets():
    assert normalize("ใชขอมูล สงเสริม") == "ใช้ข้อมูล ส่งเสริม"
    assert normalize("ปญหา เปน ยุทธศาสตร ฝาย เฟซบุก ศิลปน") == \
        "ปัญหา เป็น ยุทธศาสตร์ ฝ่าย เฟซบุ๊ก ศิลปิน"
    assert normalize(" ข้อหนึ่ง  ผ่าน  ไม่ผ่าน") == "• ข้อหนึ่ง ✓ ผ่าน □ ไม่ผ่าน"


def test_a_font_drawing_sara_am_as_mai_tho_needs_ocr():
    # ทิ้งเศษอาหารลงท่อระบายน้ำ ... ทำน้ำยาหก ... ใช้น้ำยาทำความสะอาด, as that font stores it.
    raw = ("พบว่าทิ้งเศษอาหารลงท่อระบายน้้า พนักงานล้างแอร์ ท้าน้้ายาหก"
           " แม่บ้านใช้น้้ายาท้าความสะอาด")
    assert garbled(normalize(raw), raw)
    assert decide(normalize(raw), len(raw), 0.0, raw=raw)[0] == "ocr"
    # Even with a real ำ from a heading in another font, and only one stacked mark.
    mixed = THAI + " ผู้อำนวยการ สถานีจ้าหน่ายน้้ามันเชื้อเพลิง"
    assert garbled(normalize(mixed), mixed)
    # Real ้า (ข้าว, ล้าง) next to a real ำ, and a page whose marks are stored twice, are not it.
    assert not garbled(THAI + " ข้าวน้ำ ล้างจาน", THAI + " ข้าวน้ำ ล้างจาน")
    twice = THAI + " สิิทธิิ ป้้ายประกาศ ป้้ายชื่อ"
    assert not garbled(normalize(twice), twice)


def test_needs_another_look_finds_what_older_rules_left():
    assert not needs_another_look(THAI)
    for old in ("¹Ò§ÊÒÇ ÊÓ¹Ñ¡¡®ËÁÒÂ", "สิิทธิ", "ผู้�", "สQำหรับ", "ใช", "ท่อระบายน้้า"):
        assert needs_another_look(old), old


def test_garbled_text_layers():
    assert not garbled(THAI)
    assert not garbled("The Senate met on 18 September 2025 to consider the budget bill.")
    assert not garbled("1,234,567.00 2,345,678.00 3,456,789.00 4,567,890.00 5,678,901.00")
    # Thai in a legacy encoding read as Latin-1, and a font without a character map.
    assert garbled(THAI.encode("tis-620").decode("latin-1"))
    assert garbled("!\"#$%&'()*+,-./:;<=>?@[]^_`{|}~ !\"#$%&'()*+,-./:;<=>?@")
    assert not garbled("àé")  # too short to judge
    # A font that maps every sara aa to sara am.
    assert garbled("การละเมิดสิทธิตำมมำตรำ ๑๖/๑ กำหนดหน่วยงำนด้ำนสงเครำะห์และกำรคำนวณบำเหน็จ บำนำญรำยเดือน")


def test_decide():
    assert decide(THAI, len(THAI), 0.0) == ("text", "")
    assert decide("", 0, 0.95) == ("ocr", "scanned page")
    # A scanner's own OCR layer is invisible text over the image: read the image ourselves.
    assert decide(THAI * 5, 0, 0.95) == ("ocr", "scanned page")
    # A typed page on a full-page letterhead image is not a scan.
    assert decide(THAI * 5, 400, 0.95) == ("text", "")
    assert decide(THAI.encode("tis-620").decode("latin-1"), 90, 0.0)[0] == "ocr"
    assert decide("", 0, 0.3) == ("ocr", "little text, mostly image")
    assert decide("", 0, 0.0, drawings=500) == ("ocr", "text drawn as shapes")
    assert decide("", 0, 0.0) == ("text", "blank")


def _image(page, rect):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 80), 0)
    pix.clear_with(200)
    page.insert_image(rect, pixmap=pix)


def test_inspect_real_pdf(tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "The text layer of a typed page, long enough to keep.")
    _image(doc.new_page(), pymupdf.Rect(0, 0, 595, 842))  # a scan
    scanned_with_layer = doc.new_page()
    _image(scanned_with_layer, scanned_with_layer.rect)
    scanned_with_layer.insert_text((72, 72), "invisible OCR layer from the scanner " * 3, render_mode=3)
    letterhead = doc.new_page()
    _image(letterhead, letterhead.rect)
    letterhead.insert_textbox(pymupdf.Rect(50, 50, 545, 800), "Typed on a letterhead image. " * 20)
    doc.new_page()  # blank
    path = tmp_path / "t.pdf"
    doc.save(path)

    pages = pdf.inspect(str(path))
    assert [p["method"] for p in pages] == ["text", "ocr", "ocr", "text", "text"]
    assert pages[0]["text"].startswith("The text layer")
    assert pages[1]["reason"] == pages[2]["reason"] == "scanned page"
    assert pages[1]["text"] == ""  # OCR pages carry no text until they are read
    assert pages[4]["reason"] == "blank"

    image = pdf.render(str(path), 2, 400)
    assert image[:3] == b"\xff\xd8\xff"
    rendered = pymupdf.Pixmap(image)
    assert abs(max(rendered.width, rendered.height) - 400) <= 1


def test_a_page_with_content_but_no_text_layer_is_read(tmp_path):
    doc = pymupdf.open()
    drawn = doc.new_page()
    for i in range(30):  # lines of text arriving as filled shapes, the way outlined fonts do
        drawn.draw_rect(pymupdf.Rect(72, 100 + i * 20, 400, 108 + i * 20), color=None, fill=(0, 0, 0))
    doc.new_page()  # truly blank
    path = tmp_path / "drawn.pdf"
    doc.save(path)

    pages = pdf.inspect(str(path))
    assert [(p["method"], p["reason"]) for p in pages] == [("ocr", "content without a text layer"),
                                                           ("text", "blank")]
    image, ink = pdf.render_page(str(path), 1, 400)
    assert image[:3] == b"\xff\xd8\xff" and ink > 0.01
    assert pdf.render_page(str(path), 2, 400)[1] == 0
    top, bottom = (pymupdf.Pixmap(half) for half in pdf.render_halves(str(path), 1, 400))
    assert top.width > top.height and bottom.width > bottom.height  # halves of a portrait page


def test_strips_are_cut_between_lines(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for i in range(30):  # lines of text 26 pt apart, with a gap between each
        page.insert_text((72, 60 + i * 26), f"Line number {i} of a typed page", fontsize=16)
    pix = page.get_pixmap(dpi=144, colorspace=pymupdf.csGRAY)  # 2 px per point
    ink = lambda share: any(b < 128 for b in pix.samples[int(share * 842 * 2) * pix.stride:][:pix.width])
    path = tmp_path / "lines.pdf"
    doc.save(path)

    strips = pdf.render_strips(str(path), 1, 600, 3)
    spans = [span for _, span in strips]
    assert len(strips) == 3 and spans[0][0] == 0 and spans[-1][1] == 1
    for (_, bottom), (top, _) in zip(spans, spans[1:]):
        assert bottom == top and not ink(bottom)  # cut between lines: no overlap needed
    image = pymupdf.Pixmap(strips[0][0])
    assert image.width == 600 and image.height < 300  # a strip is wide and short: a higher resolution

    halves = pdf.render_strips(str(path), 1, 600, 2, spans[1])  # a strip cut again
    assert halves[0][1][0] == spans[1][0] and halves[-1][1][1] == spans[1][1]
    assert not ink(halves[0][1][1])
