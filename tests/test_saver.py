from pathlib import Path

from collector.saver import disposition_filename, safe_component, target_path


def test_safe_component_windows_rules():
    assert safe_component('a:b*c?.pdf') == "a_b_c_.pdf"
    assert safe_component("CON.pdf") == "_CON.pdf"
    assert safe_component("name. ") == "name"
    long = "ก" * 300 + ".pdf"
    out = safe_component(long)
    assert len(out) <= 120 and out.endswith(".pdf")
    assert len(out.encode("utf-8")) <= 255  # Linux's limit is in bytes: 3 per Thai character
    assert safe_component(long) == out  # deterministic
    assert safe_component("ก" * 100 + ".pdf") != "ก" * 100 + ".pdf"  # 100 characters, 304 bytes


def test_disposition_rfc5987():
    v = "attachment; filename*=UTF-8''%E0%B8%A3%E0%B8%B2%E0%B8%A2%E0%B8%87%E0%B8%B2%E0%B8%99.pdf"
    assert disposition_filename(v) == "รายงาน.pdf"


def test_disposition_raw_utf8_bytes_seen_as_latin1():
    # requests decodes header bytes as latin-1; recover the UTF-8 name.
    garbled = "รายงาน.pdf".encode("utf-8").decode("latin-1")
    assert disposition_filename(f'attachment; filename="{garbled}"') == "รายงาน.pdf"


def test_disposition_percent_encoded_with_plus_for_space():
    # Exactly what pis.parliament.go.th sends.
    v = "inline; filename=%E0%B8%AB%E0%B8%99%E0%B8%B1%E0%B8%87%E0%B8%AA%E0%B8%B7%E0%B8%AD+%E0%B8%8412.pdf"
    assert disposition_filename(v) == "หนังสือ ค12.pdf"


def test_disposition_strips_paths():
    assert disposition_filename('attachment; filename="..\\..\\evil.pdf"') == "evil.pdf"
    assert disposition_filename(None) is None


def test_target_path_mirrors_url_and_decodes_thai():
    p = target_path(
        "https://www.senate.go.th/assets/portals/1/files/%E0%B8%A3%E0%B8%B2%E0%B8%A2%E0%B8%87%E0%B8%B2%E0%B8%99.pdf",
        "application/pdf", None,
    )
    assert p == Path("www.senate.go.th", "assets", "portals", "1", "files", "รายงาน.pdf")


def test_target_path_tis620_escapes():
    p = target_path("https://www.senate.go.th/files/%C3%D2%C2%A7%D2%B9.pdf", "application/pdf", None)
    assert p.name == "รายงาน.pdf"


def test_target_path_query_and_missing_extension():
    a = target_path("https://pis.parliament.go.th/Proxy/download?s=1", "application/pdf", None)
    b = target_path("https://pis.parliament.go.th/Proxy/download?s=2", "application/pdf", None)
    assert a != b and a.suffix == ".pdf" and a.parent == Path("pis.parliament.go.th", "Proxy", "download")
    c = target_path("https://x.go.th/get?id=1", "application/pdf", 'attachment; filename="r.pdf"')
    assert c == Path("x.go.th", "get", "r.pdf")


def test_target_path_adds_the_real_extension():
    # senate.go.th procurement files: a PDF named "..._purchase_file.50"
    p = target_path("https://www.senate.go.th/assets/purchase/9_purchase_file.50", "application/pdf", None)
    assert p.name == "9_purchase_file.50.pdf"
    assert target_path("https://x.go.th/a/report.PDF", "application/octet-stream", None).name == "report.PDF"


def test_target_path_does_not_double_an_extension_it_already_has():
    p = target_path("https://x.org/a/photo.jpg", "image/jpeg", None, frozenset({"jpg"}))
    assert p.name == "photo.jpg"
    p = target_path("https://x.org/a/photo.JPG", "image/jpeg", None)  # jpg not a document type here
    assert p.name == "photo.JPG"
