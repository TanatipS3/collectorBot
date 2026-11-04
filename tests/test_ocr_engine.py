import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

from ocr import engine
from ocr.engine import (AllDown, Engine, OcrClient, assess, better, clean, collapse_repeats, cut_runaway,
                        empty_cell_loop)
from tests.ocr_fakes import FakeOcrServer, closed_port_url


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)


@pytest.fixture
def server():
    s = FakeOcrServer()
    yield s
    s.close()


def _client(*engines, attempts=2):
    return OcrClient(list(engines), attempts=attempts, max_tokens=100)


def test_reads_with_the_primary(server):
    result = _client(Engine("primary", server.url, "vision")).read_page(b"jpeg")
    assert result["status"] == "done"
    assert result["text"] == "OCR text 1"
    assert (result["engine"], result["model"]) == ("primary", "vision")
    sent = server.requests[0]
    assert sent["model"] == "vision" and sent["temperature"] == 0.0
    assert sent["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_backup_takes_over_when_the_primary_is_down(server):
    client = _client(Engine("primary", closed_port_url(), "vision"), Engine("backup", server.url, "olmocr"))
    result = client.read_page(b"jpeg")
    assert result["engine"] == "backup" and result["model"] == "olmocr"


def test_server_errors_are_retried_then_the_backup_is_used():
    primary = FakeOcrServer(lambda n, p: (503, ""))
    backup = FakeOcrServer()
    try:
        result = _client(Engine("primary", primary.url, "v"), Engine("backup", backup.url, "b"),
                         attempts=3).read_page(b"x")
        assert len(primary.requests) == 3
        assert result["engine"] == "backup"
    finally:
        primary.close()
        backup.close()


def test_a_refused_page_is_not_repeated_and_fails_without_a_backup():
    refuses = FakeOcrServer(lambda n, p: (400, ""))
    try:
        result = _client(Engine("primary", refuses.url, "v"), attempts=3).read_page(b"x")
        assert result["status"] == "failed" and "HTTP 400" in result["error"]
        assert len(refuses.requests) == 1
    finally:
        refuses.close()


def test_all_down_is_not_a_page_failure():
    client = _client(Engine("primary", closed_port_url(), "v"), Engine("backup", closed_port_url(), "b"))
    with pytest.raises(AllDown):
        client.read_page(b"x")


def test_an_engine_that_keeps_failing_is_skipped_for_a_while(server, monkeypatch):
    monkeypatch.setattr(engine, "DOWN_AFTER", 2)
    dead = Engine("primary", closed_port_url(), "v")
    client = _client(dead, Engine("backup", server.url, "b"), attempts=2)
    client.read_page(b"x")  # two failures: marked down
    calls = []
    monkeypatch.setattr(dead, "read", lambda *a: calls.append(a))
    assert client.read_page(b"x")["engine"] == "backup"
    assert calls == []  # not even tried


def _slow(n, payload):
    time.sleep(0.2)  # keeps pages in flight so the lanes fill up
    return 200, f"text {n}"


def test_a_backup_lane_takes_its_share_of_pages():
    primary, backup = FakeOcrServer(_slow), FakeOcrServer(_slow)
    try:
        client = OcrClient([Engine("primary", primary.url, "v"), Engine("backup", backup.url, "b")],
                           attempts=1, lanes={"primary": 2, "backup": 3})
        with ThreadPoolExecutor(5) as pool:
            results = list(pool.map(client.read_page, [b"x"] * 10))
        engines = Counter(r["engine"] for r in results)
        assert engines["primary"] >= 3 and engines["backup"] >= 3  # both read, at the same time
        assert client.busy == {"primary": 0, "backup": 0}  # every lane given back
    finally:
        primary.close()
        backup.close()


def test_without_lanes_the_backup_is_only_a_fallback(server):
    other = FakeOcrServer()
    try:
        client = _client(Engine("primary", server.url, "v"), Engine("backup", other.url, "b"))
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(client.read_page, [b"x"] * 8))
        assert len(server.requests) == 8 and other.requests == []
    finally:
        other.close()


def test_when_the_primary_is_down_the_backup_takes_all_pages(server, monkeypatch):
    monkeypatch.setattr(engine, "DOWN_AFTER", 1)
    client = OcrClient([Engine("primary", closed_port_url(), "v"), Engine("backup", server.url, "b")],
                       attempts=1, lanes={"primary": 4, "backup": 1})
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(client.read_page, [b"x"] * 8))
    assert all(r["engine"] == "backup" for r in results)


def test_a_phrase_repeated_over_and_over_is_cut_to_one_copy():
    s = FakeOcrServer(lambda n, p: (200, "หัวข้อเรื่อง\n" + "ซ้ำ ๆ วนไปมา " * 50))
    try:
        result = _client(Engine("primary", s.url, "v")).read_page(b"x")
        assert result["text"] == "หัวข้อเรื่อง\nซ้ำ ๆ วนไปมา"
        assert result["suspect"] == [] and "repeats_collapsed_1" in result["notes"]
        assert len(s.requests) == 1  # nothing to read again
    finally:
        s.close()


def test_a_page_cut_off_is_read_again_with_a_repetition_penalty():
    def reply(n, payload):
        if "repetition_penalty" in payload:
            return 200, "ข้อความที่ถูกต้องของหน้านี้"
        return 200, "ข้อความยาวมาก " + "๑๒๓ " * 50, "length"
    s = FakeOcrServer(reply)
    try:
        result = _client(Engine("primary", s.url, "v")).read_page(b"x")
        assert (result["text"], result["suspect"]) == ("ข้อความที่ถูกต้องของหน้านี้", [])
        retry = s.requests[1]
        assert (retry["temperature"], retry["repetition_penalty"]) == (engine.RETRY_TEMPERATURE,
                                                                       engine.RETRY_REPETITION_PENALTY)
    finally:
        s.close()


def test_a_page_answered_with_the_prompt_is_blank():
    s = FakeOcrServer(lambda n, p: (200, engine.PROMPT))
    try:
        client = _client(Engine("primary", s.url, "v"))
        empty = client.read_page(b"x", ink=0.0)  # nothing on the page: blank, no second try
        assert (empty["text"], empty["suspect"], len(s.requests)) == ("[หน้าว่าง]", [], 1)
        photo = client.read_page(b"x", ink=0.2)  # something there: tried again, then flagged
        assert (photo["text"], photo["suspect"], len(s.requests)) == ("[หน้าว่าง]", ["prompt_echo"], 3)
    finally:
        s.close()


def test_halves_are_joined_without_the_line_both_show():
    halves = ["บรรทัดแรก\nบรรทัดกลาง", "บรรทัดกลาง\nบรรทัดสุดท้าย"]
    s = FakeOcrServer(lambda n, p: (200, halves[n - 1]))
    try:
        result = _client(Engine("primary", s.url, "v")).read_parts([b"top", b"bottom"])
        assert result["text"] == "บรรทัดแรก\nบรรทัดกลาง\n\nบรรทัดสุดท้าย"
        assert "read_in_halves" in result["notes"]
    finally:
        s.close()


def test_collapse_repeats_keeps_layout_and_ordinary_text():
    watermark = ["ใช้บังคับต่อไป"] + ["สำนักงานคณะกรรมการกฤษฎีกา"] * 12 + ["ผู้รับสนองพระบรมราชโองการ"]
    assert collapse_repeats("\n".join(watermark)) == (
        "ใช้บังคับต่อไป\nสำนักงานคณะกรรมการกฤษฎีกา\nผู้รับสนองพระบรมราชโองการ", 1)
    cells = "<tr>" + "<td></td>" * 8 + "</tr>"  # empty table cells are layout
    assert collapse_repeats(cells) == (cells, 0)
    # Tables repeat cells for real: a pass/fail pair under every week, the same columns per year.
    weeks = "<tr>\n" + "<td>ผ่าน</td>\n<td>ไม่ผ่าน</td>\n" * 4 + "</tr>"
    assert collapse_repeats(weeks) == (weeks, 0)
    columns = "| ปี | " + "โครงการ | จำนวนเงิน | " * 3
    assert collapse_repeats(columns) == (columns, 0)
    numbers = "๑๒๓ " * 30  # no words
    assert collapse_repeats(numbers)[1] == 0
    law = "ตามมาตรา ๑ และมาตรา ๒ และมาตรา ๓ แห่งพระราชบัญญัตินี้"
    assert collapse_repeats(law) == (law, 0)
    board = "ประธานกรรมการ\nกรรมการ\nกรรมการ\nกรรมการ\nกรรมการ"  # a column of short labels
    assert collapse_repeats(board) == (board, 0)


def test_cut_runaway_drops_the_block_a_stuck_model_kept_writing():
    page = "# แบบสอบถาม\n<table>\n<tr><td>ข้อ ๑</td></tr>\n</table>"
    dots = page + "\n\n" + "....................\n\n" * 40 + "......"  # the last line is cut short
    assert cut_runaway(dots) == (page + "\n\n....................", 39)
    counter = "ปิดการรับฟัง\n" + "\n".join(str(i) for i in range(1, 300)) + "\n30"
    assert cut_runaway(counter) == ("ปิดการรับฟัง\n1", 298)
    listed = "\n".join(f"{n}. รายงานสรุปผลการรับฟังความคิดเห็น ร่างพระราชบัญญัติ" for n in "๑๒๓๔๕๖๗๘๙") + "\n๑๐. ราย"
    assert cut_runaway(listed) == ("๑. รายงานสรุปผลการรับฟังความคิดเห็น ร่างพระราชบัญญัติ", 8)
    row = "<tr>\n<td></td>\n<td>☐ อนุญาต (ลงชื่อ)</td>\n</tr>"
    rows = "<table>\n" + "\n".join([row] * 10) + "\n<tr>\n<td>"
    assert cut_runaway(rows) == ("<table>\n" + row, 9)


def test_cut_runaway_keeps_real_content():
    figures = "\n".join(f"<tr><td>{n}</td><td>{n * 37 % 1000:,}</td></tr>" for n in range(1, 40)) + "\n<tr><td>4"
    assert cut_runaway(figures) == (figures, 0)  # rows of a table of figures differ by more than one
    few = "ลงชื่อ ....................\n" * 5 + "ลง"  # under RUNAWAY_COPIES
    assert cut_runaway(few) == (few, 0)
    assert cut_runaway("") == ("", 0)


def test_a_watched_reading_stops_once_the_model_is_stuck():
    counting = "# รายงาน\nเนื้อหาจริงของหน้า\n" + "".join(f"{n}\n" for n in range(1, 3000))
    s = FakeOcrServer(lambda n, p: (200, counting, "length"))
    try:
        client = OcrClient([Engine("primary", s.url, "v")], max_tokens=100, watch=True, read_again=False)
        result = client.read_page(b"x")
        assert s.requests[0]["stream"] is True
        assert result["text"] == "# รายงาน\nเนื้อหาจริงของหน้า\n1"  # stopped, the counting cut
        assert result["suspect"] == ["truncated"] and "stopped_stuck" in result["notes"]
        assert len(s.requests) == 1  # not read again: rescue reads it in smaller pieces instead
    finally:
        s.close()


def test_a_watched_reading_that_is_fine_comes_back_whole():
    page = "\n".join(f"มาตรา {n} ข้อความของมาตรานี้แตกต่างกันไป เรื่องที่ {n * 7}" for n in range(1, 60))
    s = FakeOcrServer(lambda n, p: (200, page))
    try:
        result = OcrClient([Engine("primary", s.url, "v")], watch=True).read_page(b"x")
        assert (result["text"], result["suspect"], result["tokens_out"]) == (page, [], 7)
    finally:
        s.close()


def test_stuck():
    assert engine.stuck("หัวข้อ\n" + "\n".join(str(i) for i in range(100, 113)) + "\n11")
    assert engine.stuck("เนื้อหา " + "สำนักงานคณะกรรมการกฤษฎีกา " * 13 + "สำนัก")  # one long line
    assert not engine.stuck("กรรมการ\n" * 11 + "ประ")  # fewer than STUCK_COPIES
    assert not engine.stuck("สารบัญ\nบทที่ ๑ บทนำ " + "." * 300 + " ๑")  # a dot leader has no words


def test_better_prefers_fewer_doubts_then_more_text():
    cut = {"status": "done", "text": "short", "suspect": ["truncated"]}
    whole = {"status": "done", "text": "a longer reading", "suspect": []}
    assert better(cut, whole) is whole and better(whole, cut) is whole
    assert better(whole, {"status": "failed", "error": "x"}) is whole


def test_assess():
    assert assess({"text": ""}) == (["empty"], [])
    assert assess({"text": "[หน้าว่าง]"}) == ([], ["blank"])
    assert "repetition_loop" in assess({"text": "ทดสอบการวนซ้ำ " * 40})[0]
    assert "truncated" in assess({"text": "ข้อความภาษาไทย", "finish": "length"})[0]
    assert assess({"text": "Annual report of the Senate"})[1] == ["little_thai"]
    # Boilerplate repeated a few times inside a real page is not a loop.
    page = "บทนำของรายงาน " * 3 + "เนื้อหาหลักที่แตกต่างกันไปในแต่ละย่อหน้า " * 1 + "สรุปผลการดำเนินงานประจำปี" * 2
    assert assess({"text": page})[0] == []
    assert assess({"text": "I'm sorry, I cannot read this image."})[0] == ["possible_refusal"]
    law = ("มาตรา ๘๖ บุคคลผู้มีลักษณะดังต่อไปนี้ในวันเลือกตั้ง เป็นบุคคลต้องห้ามมิให้ใช้สิทธิเลือกตั้ง คือ\n"
           "(๑) วิกลจริต หรือจิตฟั่นเฟือนไม่สมประกอบ\n(๒) หูหนวกและเป็นใบ้ ซึ่งไม่สามารถอ่านและเขียนหนังสือได้\n"
           "(๓) ภิกษุ สามเณร นักพรต หรือนักบวช\n(๔) ต้องคุมขังอยู่โดยหมายของศาลหรือโดยคำสั่งที่ชอบด้วยกฎหมาย\n"
           "มาตรา ๘๗ ผู้มีสิทธิเลือกตั้งซึ่งไม่ไปใช้สิทธิโดยไม่แจ้งเหตุอันสมควรที่ทำให้ไม่อาจไปใช้สิทธิได้ "
           "ย่อมเสียสิทธิตามที่กฎหมายบัญญัติ ทั้งนี้ ให้เป็นไปตามระเบียบที่คณะกรรมการการเลือกตั้งกำหนด")
    assert assess({"text": law})[0] == []  # the words of a refusal inside a real page


def test_clean():
    assert clean("<think>hmm</think>\n```markdown\n# หัวข้อ\n```") == "# หัวข้อ"
    assert clean("---\nprimary_language: th\nis_table: false\n---\nข้อความ") == "ข้อความ"
    assert clean("---\ntitle: x\n---\nkeep") == "---\ntitle: x\n---\nkeep"


BLANK_ROWS = ("# รายชื่อผู้เข้าร่วม\n<table>\n<tr>\n<td>ลำดับ</td>\n<td>ชื่อ - สกุล</td>\n</tr>\n"
              + "<tr>\n<td></td>\n<td></td>\n</tr>\n" * 14 + "</table>")


def test_a_loop_of_empty_table_cells_is_told_apart_from_a_stuck_model():
    md = "# ใบลงทะเบียน\n| ลำดับ | ชื่อ |\n|---|---|\n" + "|    |    |\n" * 20
    words = "หัวเรื่อง\n" + "ข้อความซ้ำไปซ้ำมาไม่จบ\n" * 20
    zeros = BLANK_ROWS.replace("<td></td>\n<td></td>", "<td>0</td>\n<td></td>")
    for text in (BLANK_ROWS, md):  # a sign-in sheet's blank rows, as HTML and as Markdown
        assert "repetition_loop" in assess({"text": text})[0]  # what the run flags
        assert empty_cell_loop(text)
    for text in (words, zeros):  # words or figures repeated: still a loop
        assert "repetition_loop" in assess({"text": text})[0]
        assert not empty_cell_loop(text)
    assert not empty_cell_loop("<table>\n<tr>\n<td></td>\n</tr>\n</table>")  # no loop at all
