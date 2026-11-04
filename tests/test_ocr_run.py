import base64
import csv
import hashlib

import pymupdf
import pytest

from collector.store import Store
from ocr import engine
from ocr.config import OcrConfig
from ocr.engine import Engine, OcrClient
from ocr.runner import Runner, first_line_title
from ocr.store import OcrStore
from tests.ocr_fakes import FakeOcrServer


def _scan(page):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 80), 0)
    pix.clear_with(180)
    page.insert_image(page.rect, pixmap=pix)


@pytest.fixture
def site(tmp_path):
    """A finished crawl: three files on disk and the crawl's record of where each came from."""
    downloads = tmp_path / "downloads"
    (downloads / "www.senate.go.th" / "files").mkdir(parents=True)
    mixed = pymupdf.open()
    mixed.new_page().insert_text((72, 72), "Typed first page with a proper text layer.")
    _scan(mixed.new_page())
    mixed.new_page()
    mixed.save(downloads / "www.senate.go.th" / "files" / "รายงาน ประจำปี.pdf")
    scan = pymupdf.open()
    for _ in range(5):
        _scan(scan.new_page())
    scan.save(downloads / "www.senate.go.th" / "files" / "scan.pdf")
    (downloads / "www.senate.go.th" / "files" / "notes.docx").write_bytes(b"PK\x03\x04 not really")

    store = Store(tmp_path / "state" / "collector.db")
    base = "https://www.senate.go.th/files/"
    files = [("รายงาน ประจำปี.pdf", base + "report.pdf", None),
             ("รายงาน ประจำปี.pdf", base + "report-copy.pdf", "duplicate of " + base + "report.pdf"),
             ("scan.pdf", base + "scan.pdf", None),
             ("notes.docx", base + "notes.docx", None)]
    for name, url, note in files:
        path = f"www.senate.go.th/files/{name}"
        data = (downloads / path).read_bytes()
        store.add(url, "file", 1, "https://www.senate.go.th/view/1/")
        store.finish(url, "done", path=path, sha256=hashlib.sha256(data).hexdigest(),
                     size=len(data), final_url=url, note=note)
    store.commit()
    store.close()
    return tmp_path


def _runner(site, server, **overrides):
    cfg = OcrConfig(**{"collector_db": str(site / "state" / "collector.db"),
                       "downloads_dir": str(site / "downloads"), "output_dir": str(site / "ocr_out"),
                       "labels_csv": str(site / "labels.csv"), "alt_names_csv": str(site / "alt_names.csv"),
                       "primary_url": server.url,
                       "backup_url": "", "workers": 2, "processes": 1, "render_px": 300, **overrides})
    store = OcrStore(cfg.db_path)
    return Runner(cfg, store, OcrClient([Engine("primary", server.url, "fake-vision")], attempts=1)), store


def test_ocr_run_writes_text_and_a_mapping_back_to_every_source(site):
    server = FakeOcrServer()
    try:
        runner, store = _runner(site, server)
        runner.run()
        # Only the pages without a usable text layer were sent: page 2 of the report, and the scan.
        assert len(server.requests) == 6

        report = runner.text_file(store.db.execute(
            "SELECT doc_id FROM docs WHERE path LIKE '%รายงาน%'").fetchone()[0])
        text = report.read_text(encoding="utf-8")
        assert '\nog_filename: "รายงาน ประจำปี.pdf"\n' in text
        assert '\nog_url: "https://www.senate.go.th/files/report.pdf"\n' in text
        assert '\nother_urls: ["https://www.senate.go.th/files/report-copy.pdf"]\n' in text
        assert '\nfound_on: "https://www.senate.go.th/view/1/"\n' in text
        p1, p2, p3 = text.index("<!-- page 1/3 | text layer"), text.index("<!-- page 2/3 | ocr fake-vision"), \
            text.index("<!-- page 3/3 | text layer")
        assert p1 < text.index("Typed first page") < p2 < text.index("OCR text") < p3

        with open(runner.cfg.mapping_path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 4  # one per URL, the duplicate included
        by_url = {r["og_url"]: r for r in rows}
        dup = by_url["https://www.senate.go.th/files/report-copy.pdf"]
        assert dup["duplicate"] == "yes" and dup["doc_id"] == by_url[
            "https://www.senate.go.th/files/report.pdf"]["doc_id"]
        assert dup["og_filepath"].endswith("downloads/www.senate.go.th/files/รายงาน ประจำปี.pdf")
        assert dup["ocr_file"].endswith(f"{dup['doc_id']}.md") and dup["status"] == "done"
        assert (dup["pages"], dup["pages_text"], dup["pages_ocr"]) == ("3", "2", "1")
        assert by_url["https://www.senate.go.th/files/scan.pdf"]["pages_ocr"] == "5"
        docx = by_url["https://www.senate.go.th/files/notes.docx"]
        assert docx["status"] == "skipped" and docx["ocr_file"] == "" and "not a PDF" in docx["note"]
        assert dup["alt_file_name"] == "Typed first page with a proper text layer"
        assert docx["alt_file_name"] == ""
        store.close()

        # Run again: everything is done, nothing is sent.
        runner, store = _runner(site, server)
        runner.run()
        assert len(server.requests) == 6
        store.close()
    finally:
        server.close()


def test_mapping_carries_each_documents_label_in_english_and_thai(site):
    server = FakeOcrServer()
    try:
        runner, store = _runner(site, server)
        runner.run()

        def mapping():
            with open(runner.cfg.mapping_path, encoding="utf-8-sig") as f:
                return {r["og_url"]: r for r in csv.DictReader(f)}

        base = "https://www.senate.go.th/files/"
        assert mapping()[base + "report.pdf"]["label"] == ""  # not classified yet: blank
        report = mapping()[base + "report.pdf"]["doc_id"]
        with open(site / "labels.csv", "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerows([["doc_id", "label", "decided_by"], [report, "bill", "both_runs"]])
        with open(site / "alt_names.csv", "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerows([["doc_id", "alt_name", "filename"], [report, "รายงานประจำปี 2568", "x.pdf"]])
        runner.export()
        rows = mapping()
        assert rows[base + "report.pdf"]["alt_file_name"] == "รายงานประจำปี 2568"  # the LLM's name
        assert rows[base + "scan.pdf"]["alt_file_name"].startswith("OCR text")  # none: its first line
        for url in ("report.pdf", "report-copy.pdf"):  # the duplicate URL carries its file's label too
            assert (rows[base + url]["label"], rows[base + url]["label_en"], rows[base + url]["label_th"]) \
                == ("bill", "Bill", "ร่างกฎหมาย")
        assert rows[base + "scan.pdf"]["label"] == rows[base + "scan.pdf"]["label_th"] == ""
        store.close()
    finally:
        server.close()


def test_first_line_title_says_what_a_document_is():
    # First lines as read from real documents.
    assert first_line_title(["# ประกาศสำนักงานเลขาธิการวุฒิสภา\nเรื่อง ประกาศผู้ชนะการเสนอราคา ซื้อวัสดุการพิมพ์\n"
                             "ตามที่ สำนักงานเลขาธิการวุฒิสภา"]) == \
        "ประกาศสำนักงานเลขาธิการวุฒิสภา เรื่อง ประกาศผู้ชนะการเสนอราคา ซื้อวัสดุการพิมพ์"
    assert first_line_title(["ร่าง\nพระราชบัญญัติประกอบรัฐธรรมนูญ\nว่าด้วยคณะกรรมการสิทธิมนุษยชนแห่งชาติ\nพ.ศ. ...."]) == \
        "ร่าง พระราชบัญญัติประกอบรัฐธรรมนูญ ว่าด้วยคณะกรรมการสิทธิมนุษยชนแห่งชาติ"
    # A page number first, then a heading long enough on its own.
    assert first_line_title(["๑๑๓\n- 4 -\nการปฏิรูปกฎหมาย : LAW REFORM มาตรการกำกับดูแลโดรน\nบทนำ"]) == \
        "การปฏิรูปกฎหมาย : LAW REFORM มาตรการกำกับดูแลโดรน"
    # A blank first page, a table and an image description are passed over.
    assert first_line_title(["[หน้าว่าง]", "<figure>\nภาพอาคารรัฐสภา\n\n<table>\n<tr>\n<th>รายงานการตรวจสอบต้นไม้</th>"]) == \
        "รายงานการตรวจสอบต้นไม้"
    assert first_line_title(["", "12\n"]) == ""
    # A memo: form fields first, its subject a few lines down.
    memo = ("บันทึกข้อความ\nส่วนราชการ สำนักกำกับและตรวจสอบ กลุ่มงานติดตามผล โทร. ๐๒ ๘๓๑ ๙๓๒๘\n"
            "ที่ สว ๐๐๑๘/๑๒๓ วันที่ ๕ มกราคม ๒๕๖๘\nเรื่อง ขออนุมัติจัดซื้อวัสดุสำนักงาน\nเรียน เลขาธิการวุฒิสภา")
    assert first_line_title([memo]) == "บันทึกข้อความ เรื่อง ขออนุมัติจัดซื้อวัสดุสำนักงาน"


def _shape(payload) -> float:
    """Width over height of the request's JPEG, from its SOF header; the fake server answers in
    threads, where MuPDF must not be used."""
    data = base64.b64decode(payload["messages"][0]["content"][1]["image_url"]["url"].split(",", 1)[1])
    i = 2
    while data[i + 1] not in (0xC0, 0xC2):
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return int.from_bytes(data[i + 7:i + 9], "big") / int.from_bytes(data[i + 5:i + 7], "big")


def _landscape(payload) -> bool:
    """Wider than tall: a half page."""
    return _shape(payload) > 1


def test_a_page_still_cut_off_is_read_in_halves(site):
    def reply(n, payload):
        if _landscape(payload):
            return 200, f"ครึ่งหน้า {n}"
        return 200, "อ่านไม่จบ " + "๑๒๓ " * 40, "length"  # whole pages never fit

    server = FakeOcrServer(reply)
    try:
        runner, store = _runner(site, server)
        runner.run()
        rows = store.db.execute("SELECT text, suspect, notes FROM pages WHERE method='ocr'").fetchall()
        assert len(rows) == 6
        for r in rows:
            assert r["text"].startswith("ครึ่งหน้า") and "\n\nครึ่งหน้า" in r["text"]
            assert r["suspect"] == "" and "read_in_halves" in r["notes"]
        store.close()
    finally:
        server.close()


def test_fix_brings_earlier_output_up_to_the_current_rules(site):
    server = FakeOcrServer()
    try:
        runner, store = _runner(site, server)
        runner.run()
        report = store.db.execute("SELECT doc_id FROM docs WHERE path LIKE '%รายงาน%'").fetchone()[0]
        scan = store.db.execute("SELECT doc_id FROM docs WHERE path LIKE '%scan.pdf'").fetchone()[0]
        page = lambda d, n: store.db.execute("SELECT * FROM pages WHERE doc_id=? AND page=?", (d, n)).fetchone()
        # What an earlier run with older rules left behind:
        store.set_pages([
            (report, 1, {"text": "Typed first page with a proper text layer. สิิทธิ"}),
            (scan, 1, {"text": engine.PROMPT}),
            (scan, 2, {"text": "\n".join(["หัวเรื่อง"] + ["ลายน้ำสำนักงาน"] * 6), "suspect": "repetition_loop"}),
            (scan, 3, {"text": "ข้อความที่ถูกตัด", "suspect": "truncated"}),
        ])
        read_before = len(server.requests)

        expected = {"text layer repaired": 1, "OCR again: prompt echoed": 1,
                    "repeats collapsed (was a loop)": 1, "OCR again: truncated": 1}
        assert runner.fix(dry_run=True) == expected
        assert page(scan, 1)["text"] == engine.PROMPT  # a dry run writes nothing

        assert runner.fix() == expected
        assert page(report, 1)["text"] == "Typed first page with a proper text layer."
        assert "สิิทธิ" not in runner.text_file(report).read_text(encoding="utf-8")  # rewritten
        assert (page(scan, 2)["text"], page(scan, 2)["suspect"]) == ("หัวเรื่อง\nลายน้ำสำนักงาน", "")
        assert page(scan, 1)["status"] == page(scan, 3)["status"] == "pending"

        runner.run()
        assert len(server.requests) == read_before + 2  # only the two queued pages were read
        assert page(scan, 1)["text"].startswith("OCR text") and page(scan, 3)["suspect"] == ""
        assert "ลายน้ำสำนักงาน\nลายน้ำสำนักงาน" not in runner.text_file(scan).read_text(encoding="utf-8")
        assert runner.fix(dry_run=True) == {}  # nothing left to do
        store.close()
    finally:
        server.close()


def test_recheck_drops_loop_flags_that_are_a_tables_blank_rows(site):
    server = FakeOcrServer()
    try:
        runner, store = _runner(site, server)
        runner.run()
        scan = store.db.execute("SELECT doc_id FROM docs WHERE path LIKE '%scan.pdf'").fetchone()[0]
        page = lambda n: store.db.execute("SELECT * FROM pages WHERE doc_id=? AND page=?", (scan, n)).fetchone()
        blank_rows = ("# ใบลงทะเบียน\n<table>\n<tr>\n<td>ลำดับ</td>\n<td>ชื่อ</td>\n</tr>\n"
                      + "<tr>\n<td></td>\n<td></td>\n</tr>\n" * 14 + "</table>")
        store.set_pages([
            (scan, 1, {"text": blank_rows, "suspect": "repetition_loop"}),
            (scan, 2, {"text": blank_rows, "suspect": "repetition_loop,truncated"}),  # ran into the limit
            (scan, 3, {"text": "หัวเรื่อง\n" + "ข้อความซ้ำไปซ้ำมาไม่จบ\n" * 20, "suspect": "repetition_loop"}),
        ])
        runner.write_doc(scan)
        read_before = len(server.requests)

        expected = {"loop flag dropped: blank table rows": 1}
        assert runner.recheck(dry_run=True) == expected
        assert page(1)["suspect"] == "repetition_loop"  # a dry run writes nothing
        assert runner.recheck() == expected
        assert page(1)["suspect"] == "" and "blank_rows_not_a_loop" in page(1)["notes"]
        assert (page(2)["suspect"], page(3)["suspect"]) == ("repetition_loop,truncated", "repetition_loop")
        assert all(page(n)["status"] == "done" for n in (1, 2, 3))  # nothing queued
        assert len(server.requests) == read_before  # nothing read

        comments = [l for l in runner.text_file(scan).read_text(encoding="utf-8").splitlines() if l.startswith("<!-- page")]
        assert "suspect" not in comments[0] and "suspect: repetition_loop" in comments[2]
        with open(site / "ocr_out" / "flagged_pages.csv", encoding="utf-8-sig") as f:
            flagged = list(csv.DictReader(f))
        assert [(r["doc_id"], r["flagged_pages"], r["which_pages"]) for r in flagged] == [(scan, "2", "2,3")]
        with open(site / "ocr_out" / "mapping.csv", encoding="utf-8-sig") as f:
            assert {r["pages_suspect"] for r in csv.DictReader(f) if r["doc_id"] == scan} == {"2"}
        assert runner.recheck(dry_run=True) == {}  # nothing left to do
        store.close()
    finally:
        server.close()


def test_reread_reads_the_text_layer_pages_by_ocr_into_an_output_of_their_own(site):
    server = FakeOcrServer()
    try:
        runner, store = _runner(site, server)
        runner.run()
        before = [tuple(r) for r in store.db.execute("SELECT * FROM pages ORDER BY doc_id, page")]
        source = site / "ocr_out" / "ocr.db"
        with pytest.raises(ValueError):
            runner.reread_text_layer(source)  # never in the run's own state

        again, new = _runner(site, server, output_dir=str(site / "ocr_out_textlayer"))
        expected = {"text-layer pages to OCR": 1, "documents": 1}  # page 1 of the report
        assert again.reread_text_layer(source, dry_run=True) == expected
        assert new.db.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 0  # a dry run writes nothing
        assert again.reread_text_layer(source) == expected
        read_before = len(server.requests)
        again.run()
        assert len(server.requests) == read_before + 1
        assert again.reread_text_layer(source) == {}  # queued and read: nothing more to do

        report = new.db.execute("SELECT doc_id FROM docs WHERE path LIKE '%รายงาน%'").fetchone()[0]
        pages = new.db.execute("SELECT * FROM pages WHERE doc_id=? ORDER BY page", (report,)).fetchall()
        assert [(p["method"], p["engine"], p["reason"]) for p in pages] == [
            ("ocr", "primary", "text layer read again"), ("ocr", "primary", pages[1]["reason"]),
            ("text", "text-layer", "blank")]
        assert pages[0]["text"].startswith("OCR text")
        assert "<!-- page 1/3 | ocr fake-vision -->" in again.text_file(report).read_text(encoding="utf-8")
        assert str(again.text_file(report)).startswith(str(site / "ocr_out_textlayer"))
        assert new.db.execute("SELECT COUNT(*) FROM docs WHERE status='done'").fetchone()[0] == 2
        # The first run is as it was.
        assert [tuple(r) for r in store.db.execute("SELECT * FROM pages ORDER BY doc_id, page")] == before
        new.close()
        store.close()
    finally:
        server.close()


def _rescuer(runner, store, server):
    client = OcrClient([Engine("primary", server.url, "fake-vision")], attempts=1, watch=True, read_again=False)
    return Runner(runner.cfg, store, client)


def test_rescue_reads_pages_in_doubt_again_in_strips(site):
    server = FakeOcrServer(lambda n, p: (200, f"แถบ {n}" if _landscape(p) else f"OCR text {n}"))
    try:
        runner, store = _runner(site, server)
        runner.run()
        scan = store.db.execute("SELECT doc_id FROM docs WHERE path LIKE '%scan.pdf'").fetchone()[0]
        page = lambda n: store.db.execute("SELECT * FROM pages WHERE doc_id=? AND page=?", (scan, n)).fetchone()
        law = "\n".join(f"มาตรา {n} ผู้ซึ่งไม่สามารถอ่านและเขียนหนังสือได้ ให้ปฏิบัติตามข้อ {n * 3}" for n in range(1, 12))
        store.set_pages([(scan, 1, {"text": "ตัดกลางคัน", "suspect": "truncated"}),
                         (scan, 2, {"text": law, "suspect": "possible_refusal"})])
        rescuer = _rescuer(runner, store, server)
        assert rescuer.rescue(dry_run=True) == {"refusal flag dropped: it is the page's own text": 1,
                                                "to read in strips: truncated": 1}
        read_before = len(server.requests)

        rescuer.rescue()
        assert len(server.requests) == read_before + 3  # the one page, as three strips
        strips = page(1)["text"].split("\n\n")
        assert [s.split()[0] for s in strips] == ["แถบ"] * 3
        assert page(1)["suspect"] == "" and "read_in_strips" in page(1)["notes"]
        assert (page(2)["text"], page(2)["suspect"]) == (law, "")  # not read again
        assert "ตัดกลางคัน" not in runner.text_file(scan).read_text(encoding="utf-8")
        assert rescuer.rescue(dry_run=True) == {} and runner.fix(dry_run=True) == {}  # done with
        store.close()
    finally:
        server.close()


def test_rescue_reads_a_strip_the_model_is_stuck_on_in_halves(site):
    def reply(n, payload):
        if _shape(payload) > 3:  # a strip's half: wide and short
            return 200, f"ครึ่งแถบ {n}"
        return 200, "หัวเรื่อง\n" + "".join(f"{i}\n" for i in range(1, 500))  # whole strips: stuck
    server = FakeOcrServer(reply)
    try:
        runner, store = _runner(site, server)
        runner.run()
        scan = store.db.execute("SELECT doc_id FROM docs WHERE path LIKE '%scan.pdf'").fetchone()[0]
        store.set_pages([(scan, 1, {"text": "ตัดกลางคัน", "suspect": "truncated"})])
        _rescuer(runner, store, server).rescue()
        row = store.db.execute("SELECT * FROM pages WHERE doc_id=? AND page=1", (scan,)).fetchone()
        assert [s.split()[0] for s in row["text"].split("\n\n")] == ["ครึ่งแถบ"] * 6
        assert row["suspect"] == ""
        store.close()
    finally:
        server.close()


def test_first_pages_then_small_documents_first(site):
    store = OcrStore(site / "ocr.db")
    store.add_docs([("big", "big.pdf", None, 1, "new", None), ("small", "small.pdf", None, 1, "new", None)])
    page = lambda n: {"page": n, "method": "ocr", "reason": "scanned page", "text": ""}
    store.save_inspection("big", [page(n) for n in range(1, 6)])
    store.save_inspection("small", [page(1)])
    order = [(r["doc_id"], r["page"]) for r in store.claim(10)]
    assert order == [("small", 1), ("big", 1), ("big", 2), ("big", 3), ("big", 4), ("big", 5)]


def test_pages_wait_while_no_engine_answers(site, monkeypatch):
    def reply(n, payload):
        return (500, "") if n <= 2 else (200, f"OCR text {n}")

    server = FakeOcrServer(reply)
    try:
        runner, store = _runner(site, server, max_page_attempts=3)
        runner.client.engines[0].timeout = 5
        # The fake answers 500 to the first two requests: a busy server, so not the pages' fault.
        from ocr import engine, runner as runner_module
        monkeypatch.setattr(engine.time, "sleep", lambda s: None)
        monkeypatch.setattr(runner_module, "PAUSE_START", 0)
        runner.run()
        assert store.pending_pages() == 0
        failed = store.db.execute("SELECT COUNT(*) FROM pages WHERE status='failed'").fetchone()[0]
        assert failed == 0  # released and read again once the engine answered
    finally:
        server.close()
