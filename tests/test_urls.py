from collector.config import Config
from collector.urls import classify, extension, extract_links, normalize, resolve

CFG = Config(
    start_urls=[],
    crawl_hosts=["www.senate.go.th"],
    file_hosts=["*.senate.go.th", "pis.parliament.go.th"],
    exclude=["search_module", "[?&]key="],
    site="senate",
    crawl_host_download_patterns=[r"[?&]dl=\d"],
)


def test_normalize_unifies_raw_and_percent_encoded_thai():
    raw = "https://www.senate.go.th/view/1/หน้าแรก/TH-TH"
    enc = "https://www.senate.go.th/view/1/%e0%b8%ab%e0%b8%99%e0%b9%89%e0%b8%b2%e0%b9%81%e0%b8%a3%e0%b8%81/TH-TH"
    assert normalize(raw) == normalize(enc)
    assert "%E0%B8%AB" in normalize(raw)


def test_normalize_keeps_non_utf8_escapes():
    # TIS-620 escapes must survive untouched; re-encoding them would change the URL.
    assert normalize("https://www.senate.go.th/a/%CB%B9.pdf") == "https://www.senate.go.th/a/%CB%B9.pdf"


def test_normalize_drops_fragment_empty_params_and_default_port():
    assert normalize("HTTPS://WWW.Senate.go.th:443/x/?&page41=2#top") == "https://www.senate.go.th/x/?page41=2"


def test_normalize_rejects_junk():
    for bad in ("mailto:a@b.c", "javascript:void(0)", "https://www.senate.go.th/${arr.Link}", "#", "tel:02"):
        assert normalize(bad) is None


def test_resolve_root_query_pagination_quirk():
    base = "https://www.senate.go.th/view/1/news/TH-TH"
    assert resolve(base, "/?&amp;page41=2", CFG.rules) == "https://www.senate.go.th/view/1/news/TH-TH?&page41=2"
    assert resolve(base, "?dl=6094&type=4", CFG.rules) == "https://www.senate.go.th/view/1/news/TH-TH?dl=6094&type=4"


def test_extension_decodes_thai_names():
    assert extension("https://x/files/%E0%B8%82%E0%B9%89%E0%B8%AD.PDF?v=1") == "pdf"
    assert extension("https://x/view/1/TH-TH") == ""
    assert extension("https://x/book_shelf/.PDF") == ""  # no file name, just a suffix


def test_classify():
    assert classify("https://www.senate.go.th/view/1/x/TH-TH", CFG) == "page"
    assert classify("https://www.senate.go.th/assets/a/b.pdf", CFG) == "file"
    assert classify("https://lis.senate.go.th/files/b.docx", CFG) == "file"
    assert classify("https://lis.senate.go.th/topic.php", CFG) is None  # not a crawl host
    assert classify("https://pis.parliament.go.th/PARFileDownloadProxy/download?s=abc", CFG) == "file"
    # On a crawled host, but always a redirect to a document.
    assert classify("https://www.senate.go.th/view/1/news/TH-TH?dl=6094&type=4", CFG) == "file"
    assert classify("https://www.senate.go.th/view/1/download_center/TH-TH", CFG) == "page"
    assert classify("https://www.senate.go.th/assets/logo.png", CFG) is None
    assert classify("https://evil.example.com/a.pdf", CFG) is None
    assert classify("https://www.senate.go.th/search_module/x", CFG) is None
    assert classify("https://www.senate.go.th/view/1/p/TH-TH?page=1&key=abc", CFG) is None


def test_extract_links_finds_attrs_scripts_and_js_pagers():
    html = """
    <a href="/view/1/a/TH-TH">a</a>
    <a href="/?&amp;page41=3">3</a>
    <iframe src="/assets/viewer.html?file=/assets/x/book.pdf"></iframe>
    <script>var f = '/assets/portals/1/files/in-script.pdf';</script>
    <a href="?dl=12&type=4"><i></i></a>
    <ul class="pagination-sm purchase-pager" data-group="2" data-totalpages="3" data-perpage="20"></ul>
    """
    links = extract_links(html, "https://www.senate.go.th/view/1/p/TH-TH", CFG)
    p = "https://www.senate.go.th/view/1/p/TH-TH"
    assert "https://www.senate.go.th/view/1/a/TH-TH" in links
    assert f"{p}?page41=3" in links
    assert "https://www.senate.go.th/assets/x/book.pdf" in links
    assert "https://www.senate.go.th/assets/portals/1/files/in-script.pdf" in links
    assert f"{p}?dl=12&type=4" in links
    assert f"{p}?page_g2=2&perpage=20" in links
    assert f"{p}?page_g2=3&perpage=20" in links


def test_extract_links_news_attachments_and_links_missing_their_slash():
    # As on /view/1/Digital_Report/<title>/22619/TH-TH.
    html = """
    <a href="javascript:showFileName(22619,'1_22619.pdf',1,1,1)"><button>002.pdf</button></a>
    <a href="assets/portals/93/fileups/253/files/Ethics.pdf">ethics</a>
    <script>function showFileName(newsID,filename,fid,pid,lid){
        var urlfile = 'https://www.senate.go.th/assets/portals/'+1+'/news/'+newsID+'/'+filename;
    }</script>"""
    links = extract_links(html, "https://www.senate.go.th/view/1/Digital_Report/x/22619/TH-TH", CFG)
    assert "https://www.senate.go.th/assets/portals/1/news/22619/1_22619.pdf" in links
    assert "https://www.senate.go.th/assets/portals/93/fileups/253/files/Ethics.pdf" in links
    assert not [u for u in links if "/view/" in u and u.endswith(".pdf")]


def test_extract_links_ebook_viewer_pdf_and_paths_with_spaces():
    # As on /ebook/flipbook/5070/1/1.
    html = """
    <ul><li key="pdf_url">/assets/portals/1/fileups/380/files/6(1).2 รายงานงบดุล 66.pdf</li></ul>
    <a href='javascript:;' onclick="javascript:addclick_ebook('/assets/portals/1/files/a b.pdf')">pdf</a>
    <script>alert('Download the file.pdf');</script>"""
    links = extract_links(html, "https://www.senate.go.th/ebook/flipbook/5070/1/1", CFG)
    assert normalize("https://www.senate.go.th/assets/portals/1/fileups/380/files/6(1).2 รายงานงบดุล 66.pdf") in links
    assert "https://www.senate.go.th/assets/portals/1/files/a%20b.pdf" in links
    assert not [u for u in links if "Download" in u]  # a sentence, not a path


def test_extract_links_meta_refresh_and_bare_suffix_params():
    html = """<meta http-equiv="refresh" content="0;URL=/document/Ext27/27143_0000.PDF" />
    <a href='statpdf.php?doc=.PDF&folid=6016&docid=27143'>Open file</a>"""
    links = extract_links(html, "https://www.senate.go.th/book_shelf/", CFG)
    assert "https://www.senate.go.th/document/Ext27/27143_0000.PDF" in links
    assert "https://www.senate.go.th/book_shelf/.PDF" not in links
    assert not [u for u in links if "URL=" in u.upper()]  # content="0;URL=..." is not a path


def test_sniff_recognises_documents_by_their_first_bytes():
    from collector.crawler import _sniff
    assert _sniff(b"%PDF-1.6\n...") == "application/pdf"
    assert _sniff(b"PK\x03\x04....[Content_Types].xml....word/document.xml").endswith("wordprocessingml.document")
    assert _sniff(b"PK\x03\x04....xl/workbook.xml").endswith("spreadsheetml.sheet")
    assert _sniff(b"PK\x03\x04....data.csv") == "application/zip"
    assert _sniff(b"<html>") is None and _sniff(b"") is None


# ---------- generic behaviour (no site rules) ----------

GENERIC = Config(start_urls=[], crawl_hosts=["www.example.org"], file_hosts=["*.example.org"])


def test_generic_resolve_follows_the_browser():
    base = "https://www.example.org/news/list"
    assert resolve(base, "/?page=2") == "https://www.example.org/?page=2"
    assert classify("https://www.example.org/news?dl=5", GENERIC) == "page"  # no download pattern set


def test_generic_extraction_leaves_site_quirks_alone():
    html = """<a href="javascript:showFileName(22619,'1_22619.pdf',1,1,1)">x</a>
    <ul class="purchase-pager" data-group="2" data-totalpages="3"></ul>
    <a href="/files/a.pdf">a</a>"""
    links = extract_links(html, "https://www.example.org/view/1/p", GENERIC)
    assert links == {"https://www.example.org/files/a.pdf"}


def test_link_rules_turn_script_calls_into_links():
    cfg = Config(start_urls=[], crawl_hosts=["www.example.org"], file_hosts=["www.example.org"],
                 link_rules=[{"find": r"openDoc\((\d+),\s*'([^']+)'\)", "url": "/docs/{1}/{2}"}])
    html = """<button onclick="openDoc(42, 'report 2024.pdf')">open</button>"""
    links = extract_links(html, "https://www.example.org/list", cfg)
    assert "https://www.example.org/docs/42/report%202024.pdf" in links


def test_link_rule_template_must_fit_its_regex():
    import pytest
    with pytest.raises(ValueError):
        Config(start_urls=[], crawl_hosts=[], file_hosts=[], link_rules=[{"find": r"x(\d)", "url": "/{2}"}])
    with pytest.raises(ValueError):
        Config(start_urls=[], crawl_hosts=[], file_hosts=[], link_rules=[{"find": r"x", "link": "/"}])


def test_unknown_site_rules_are_an_error():
    import pytest
    with pytest.raises(ValueError, match="no collector/sites/nosuchsite.py"):
        Config(start_urls=[], crawl_hosts=[], file_hosts=[], site="nosuchsite")
    with pytest.raises(ValueError):
        Config(start_urls=[], crawl_hosts=[], file_hosts=[], site="../evil")


def test_extra_file_exts_and_wildcard_crawl_hosts():
    cfg = Config(start_urls=[], crawl_hosts=["*.example.org"], file_hosts=["*.example.org"],
                 extra_file_exts=["jpg", ".TXT"])
    assert classify("https://img.example.org/a/photo.jpg", cfg) == "file"
    assert classify("https://img.example.org/a/notes.txt", cfg) == "file"
    assert classify("https://img.example.org/a/logo.png", cfg) is None  # still skipped
    assert classify("https://docs.example.org/list", cfg) == "page"  # any subdomain is crawled
    assert classify("https://www.example.org/a/photo.jpg", GENERIC) is None
    html = "<script>var p = '/media/scan.jpg';</script>"
    assert "https://www.example.org/media/scan.jpg" in extract_links(html, "https://www.example.org/", cfg)
