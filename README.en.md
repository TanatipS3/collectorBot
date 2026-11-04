# collectorBot

ภาษาไทย: [README.md](README.md)

Crawls a website and downloads every document it links to (PDF, Word, Excel,
PowerPoint, e-books, archives). `config.toml` is set up for the Thai Senate site,
https://www.senate.go.th, and its document portal https://document.senate.go.th.
For another site, copy `config.example.toml` and run with `-c` (see
[Another site](#another-site)).

## Setup

```powershell
pip install -r requirements.txt
```

Needs Python 3.10 or newer, on Windows or Linux (on Linux, type `python3` where this
says `python`). To run it on a server, see [Running on a Linux server](#running-on-a-linux-server).

## Use

```powershell
python -m collector check https://www.example.org/   # can this site be crawled? suggests a config
python -m collector crawl                 # start, or resume where the last run stopped
python -m collector crawl --limit 50      # quick test: stop after 50 requests
python -m collector status                # progress counts and recent errors
python -m collector manifest              # manifest.csv: every file, its URL and where it was found
python -m collector crawl --refresh       # later: re-crawl pages to pick up new documents
python -m collector crawl --retry-errors  # retry URLs that failed
python -m collector snapshot state.db     # one-file copy of the crawl state (to move the crawl)
```

Press **Ctrl+C** at any time. Requests already in flight finish and the state is
saved, so the next `crawl` picks up where this one stopped. Nothing gets
downloaded twice.

Files are saved under `downloads/`, mirroring the site's structure and keeping
the original Thai file names:

```
downloads/
  document.senate.go.th/approve/64.pdf
  www.senate.go.th/assets/portals/1/files/ebook-pdf/sum_c2569.pdf
  pis.parliament.go.th/PARFileDownloadProxy/download/หนังสือนัดประชุม ค 13.pdf
```

When two URLs serve identical bytes, the file is stored once and the second URL
is recorded as `duplicate of …`.

**Disk space:** document.senate.go.th alone has about 1,000 PDFs averaging around
5 MB each. Expect tens of GB for the whole site.

## What it handles on senate.go.th

The generic crawler handles the first item and the file-type, encoding and path ones. The
Senate-only link quirks live in `collector/sites/senate.py`, switched on by `site = "senate"`
in `config.toml`, and `?dl=` links by `crawl_host_download_patterns`.

- Links in `href`/`src`/`data-*` attributes, PDF paths inside scripts, and PDF-viewer
  URLs (`viewer.html?file=…pdf`).
- E-book viewer pages (`/ebook/flipbook/…`), which name their PDF in the page text
  (`<li key="pdf_url">`), and script paths with spaces in the file name.
- Files served with no `Content-Type` and a name that isn't a document's (the procurement
  PDFs arrive as `…_purchase_file.50`): recognised by their first bytes and saved as `.pdf`.
- News pagination written as `href="/?&page41=2"`, which literally points at the
  site root but means "page 2 of this list".
- Procurement tables paginated by JavaScript (`ul.purchase-pager` → `?page_gN=…`).
- News attachments opened by JavaScript (`showFileName(22619,'1_22619.pdf',…)` →
  `/assets/portals/1/news/22619/1_22619.pdf`), and file links written without their
  leading slash.
- Download links like `?dl=6094&type=4` that redirect to the real PDF. Whether a
  URL is a document is decided by its `Content-Type`, not just its extension.
- Thai URLs encoded as UTF-8 or TIS-620, and file names sent in `Content-Disposition`.
- Windows file name rules and paths longer than 260 characters.

## Configuration (`config.toml`)

| key | meaning |
|---|---|
| `start_urls` | where the crawl begins |
| `crawl_hosts` | hosts whose HTML pages are read for links (`*.example.org` = any subdomain) |
| `file_hosts` | hosts documents may be downloaded from (`*.senate.go.th` = any subdomain) |
| `exclude` | regexes for URLs never to fetch (search, login, click counters, …) |
| `output_dir`, `state_dir` | where files and the crawl state go (default `downloads`, `state`) |
| `site` | site rules in `collector/sites/<name>.py` for quirks that need code (`"senate"`; default none) |
| `link_rules` | links a regex finds in the HTML: `[{find = '<regex>', url = '/docs/{1}'}]` |
| `crawl_host_download_patterns` | regexes for crawl-host URLs that are always downloads (`'[?&]dl=\d'`) |
| `download_patterns` | regexes for extension-less download URLs on other file hosts |
| `extra_file_exts` | more file types to download (`["txt", "xml", "jpg"]`) |
| `user_agent`, `accept_language` | request headers (default Accept-Language `th,en;q=0.8`) |
| `workers` | download threads, across all hosts (pages get their own: `pages_per_host` per crawl host) |
| `delay` | normal seconds between requests to one host (default 1, config.toml: 0.25) |
| `max_per_host` | downloads open to one host at once (default 2, config.toml: 4) |
| `pages_per_host` | page requests open to one host at once, on top of the downloads (default 1, config.toml: 2) |
| `max_delay` | ceiling for the gap when a host is struggling (default 60 s) |
| `max_file_mb` | skip larger files (0 = no limit) |
| `respect_robots` | obey robots.txt (default `true`) |

### Another site

1. `python -m collector check https://www.example.org/ --write mysite.toml` reads the start page
   and a few pages it links to (10 by default, `--pages N`; 1 s apart; robots.txt obeyed; pages
   whose URL suggests documents first). It reports the documents linked, documents opened by
   JavaScript instead (with the code, for `link_rules`), a JavaScript application whose HTML is an
   empty shell, bot protection, robots.txt, `__doPostBack` paging, login forms and URL families
   worth excluding. Then a verdict, with the exit code: `READY` (0); `NEEDS LINK RULES` or
   `NO DOCUMENTS YET` (2); `NO LINKS`, `JAVASCRIPT APP`, `BLOCKED`, `NOT A PAGE` or
   `UNREACHABLE` (1). `--write` saves the suggested config, filled into `config.example.toml`
   (never over an existing file, nor for a site that can't be crawled). Without it, copy
   `config.example.toml` and set `start_urls`, `crawl_hosts`, `file_hosts`, `exclude`, and an
   `output_dir`/`state_dir` of its own.
2. `python -m collector -c mysite.toml crawl --limit 50`, then `status` and the log: add an
   `exclude` regex for every endless or useless URL family (calendars, sort orders, searches).
3. Documents opened by JavaScript: a `link_rules` entry. Anything a regex can't describe: a
   module in `collector/sites/` (copy `senate.py`), selected with `site`. Check the result with
   `python -m collector -c mysite.toml check <url>`, which uses the config's rules.

Sites that build their pages in the browser (React, Vue, Angular), need a login, or page with
POST forms are out of reach: the crawler reads the HTML the server sends and follows GET links.

### Politeness and back-off

The crawler is built not to strain the server, and to notice if it is being blocked:

- Each server gets at most 4 open downloads plus 2 page requests (6 in all, as
  many as a browser opens), started at least 0.25 s apart. If robots.txt sets a
  `Crawl-delay`, that is used when it is larger. Pages have their own slots and
  threads, so a page that takes a fraction of a second never waits behind a
  100 MB download. Download links without a file extension (`?dl=…`) wait for a
  download slot, not a page slot.
- On what a busy server sends (429, 502, 503, 504, a timeout or a dropped
  connection), the gap for that server doubles, up to 60 s, from the second one in
  a row. A single one between successes doesn't slow anything down. Successes
  bring the gap back down gradually. `Retry-After` is always obeyed.
- A 500 is a broken page, not a busy server (senate.go.th has many), and a 403 may
  be a block. Neither slows the crawl, but both count toward the health check.
- After 5 failures in a row, the server's robots.txt is fetched as a health check.
  If it answers normally and some request has succeeded in the last 30 seconds,
  the failures were broken links: the gap goes back to normal and crawling continues.
  Otherwise nothing is sent to that server for 5, 10, 20, then 30 minutes. If it
  is still failing after that, the crawler stops contacting it for the rest of the
  run, and its URLs stay queued.
- 429, 502, 503, 504 and timeouts are retried up to 3 times in the same run. A
  plain 500 is not: on senate.go.th it means a broken page, which fails the same
  way every time.
- On the next run, URLs that failed with timeouts, server errors or 403s are
  retried automatically. One that fails again with the same 403 or 500 doesn't count
  toward the health check: hundreds of them in a row at the start of a run say
  nothing about the server.
- Only one crawl can run at a time on the same state directory.

Other Senate subsystems (`lis.senate.go.th`, `library.senate.go.th`,
`datacatalog.senate.go.th`) are large separate sites and are not crawled by
default. Add them to `crawl_hosts` to include them.

### robots.txt

`www.senate.go.th/robots.txt` disallows `/document/`, `/v2/`, `/webstatic/` and
`/analytics/`. Two sets of documents sit behind those paths:

- the e-book flip-book shelf (`/book_shelf/` → `/document/ExtNN/….PDF`)
- committee reports (`/v2/files/commissionreport/….PDF`)

The e-book files are hosted on `library.senate.go.th/document/…`. That host's
robots.txt currently answers with HTTP 500, which RFC 9309 says to treat as
"disallow everything".

With `respect_robots = true` these URLs are listed as **blocked** in `status`
and the state database, and are never requested. Blocked URLs are checked again
at the start of every run. So if robots.txt changes, or the setting does, the
documents already found get picked up without a re-crawl. Whether to fetch them
is the operator's decision. The site owner has asked crawlers to stay out of
those paths.

## Running on a Linux server

A server can crawl around the clock on a faster connection. Set it up once:

```bash
cd ~/collectorBot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

(On Ubuntu, `python3 -m venv` needs the `python3-venv` package. Without it, use
`pip3 install --user -r requirements.txt` and type `python3` where this says `.venv/bin/python`.)

Run it inside tmux so it keeps going after you log out:

```bash
tmux new -s collector '.venv/bin/python -m collector crawl'   # Ctrl+B, then D, to leave it running
tmux attach -t collector                                       # watch it again
.venv/bin/python -m collector status                           # progress, from any shell
tail -f state/collector.log                                    # the full log
```

Without tmux: `nohup .venv/bin/python -m collector crawl > /dev/null 2>&1 &`.

To stop, use Ctrl+C in tmux, or `kill <pid>`. Either way the crawler finishes what is in flight
and saves its state. If the home disk is small, put the files on a bigger one:
`ln -s /data/collector-downloads downloads`.

## Moving a crawl to another machine

Only the state database moves. Files already downloaded stay where they are, and the
other machine downloads only what is left.

1. Stop the crawl, then run `python -m collector snapshot collector-state.db` (one file, about 50 MB).
2. Copy the code (everything except `downloads/` and `state/`) to the other machine, and put
   `collector-state.db` there as `state/collector.db`.
3. Start `crawl` there. Don't start the old machine again: two crawls would drift apart and
   double the load on the site.

At the end, copy the other machine's `downloads/` into this one's to have every file in one
place (names don't clash), along with its `state/collector.db` so `manifest` lists them all.
A file whose bytes match one on the first machine is stored again, since the second machine
can't see the first one's copy.

## Tests

```powershell
python -m pytest
```

`tests/test_crawl.py` runs the whole crawler against a local fake site that
reproduces the quirks listed above.

## OCR

`python -m ocr` turns the downloaded PDFs into text and keeps every text traceable to the
file and the links it came from. It runs next to the crawl's files and reads its state
database. Settings are in `ocr.toml`.

```bash
.venv/bin/python -m ocr probe          # send one page to each OCR engine; check both answer
.venv/bin/python -m ocr run --limit 20 # quick test: stop after OCR'ing 20 pages
tmux new -s ocr '.venv/bin/python -m ocr run'   # the full run (Ctrl+B, then D, to leave it)
.venv/bin/python -m ocr status         # progress, from any shell
tail -f ocr_out/ocr.log                # the full log
.venv/bin/python -m ocr export         # rewrite mapping.csv now (a run also does it every 30 min)
.venv/bin/python -m ocr fix --dry-run  # what the current rules would change in earlier output
.venv/bin/python -m ocr fix            # change it, then OCR the pages that need it
```

Like the crawl, it can be stopped at any time (Ctrl+C, or `kill`) and picks up where it
stopped. Finished pages are never read twice.

**How a run works:**

1. **Text layers first.** Every PDF is opened and each page's own text checked. A page typed
   on a computer keeps its text as is; reading it is exact and takes no OCR. What fonts get
   wrong in a predictable way is repaired: Thai glyph variants in the Private Use Area (two
   layouts), split ำ, Thai stored in TIS-620 and read as Latin-1 (`¹Ò§ÊÒÇ` is `นางสาว`), and
   typesetting that stores every mark twice (`สิิทธิิ`, `ผู้้�`). A page goes to OCR when it
   is a scan (an image covering most of the page with little visible text on it; a scanner's
   invisible OCR layer doesn't count), when its text layer decodes to junk (fonts without a
   character map, or one that draws ำ as ้ + า, which can't be told apart from real ้า), or
   when it has no text layer but visible content (mostly image, text drawn as shapes).
2. **OCR.** Those pages are rendered at 1600 px and read by two engines at once, each with
   its own share (`workers`, `backup_workers`): `ptm-llm-vision-3.0.0-preview` on pgx and
   TokenMind's `ocr/olmocr` (the same model, faster). Each stands in for the other when it
   is down or refuses a page. If neither answers, the run waits (30 s, doubling up to
   10 min) and carries on when one is back. A phrase repeated three or more times in a row
   (a watermark tiled over the page, a map's labels, a stuck model) is cut to one copy,
   outside tables. A reading still cut off or looping is tried again with a repetition
   penalty, then read in two halves, and the better reading is kept. A page answered with
   the prompt itself is blank when it has no ink. Pages with problems are flagged as
   `suspect`.
3. **Order.** The first three pages of every document are read before the rest, so the
   categorization step can start long before OCR finishes. After that, small documents go
   before large ones.

The backup engine needs `TOKENMIND_API_KEY`, in the environment or in a `.env` file here
(`TOKENMIND_API_KEY=...`, kept out of git). Without it the run uses the main engine only.

**What it writes (`ocr_out/`):**

- `text/<doc_id>.md`: one file per document, written when its last page is done. The
  header says where the document came from. Then come the pages in order, each one
  marked with how it was read:
  ```
  ---
  doc_id: "5aebe07f831ccd7f"
  og_filename: "รายงาน ประจำปี.pdf"
  og_filepath: "downloads/www.senate.go.th/files/รายงาน ประจำปี.pdf"
  og_url: "https://www.senate.go.th/files/report.pdf"
  found_on: "https://www.senate.go.th/view/1/"
  other_urls: ["https://www.senate.go.th/files/report-copy.pdf"]
  ...
  ---

  <!-- page 1/3 | text layer -->
  ...
  <!-- page 2/3 | ocr ptm-llm-vision-3.0.0-preview -->
  ...
  ```
  `doc_id` is the start of the file's SHA-256.
- `mapping.csv`: one row per URL the crawl saved a file from, including URLs that served
  an identical copy (`duplicate` = yes, same `doc_id`). Columns: `doc_id`, `og_filename`,
  `alt_file_name` (the document's first line of content, short heading lines joined, for
  files whose saved name says nothing), `og_filepath`, `og_url`, `final_url`, `found_on`,
  `label`, `label_en`, `label_th`,
  `duplicate`, `status`, `pages`, `pages_text`, `pages_ocr`, `pages_failed`, `pages_suspect`,
  `ocr_file`, `sha256`, `size`, `note`. Files that aren't PDFs (Word, Excel, PowerPoint) are
  listed as `skipped`. The labels come from the classification's final CSV (`labels_csv` in
  `ocr.toml`, default `classify/final/doc_labels_final.csv`) and stay blank until it exists.
- `ocr.db`: the state, with every page's text and how it was read: engine, model, prompt
  version, suspect flags, time taken.

A page that fails is retried on the next run, up to 3 runs.

**When the rules improve:** `fix` brings what earlier runs produced up to the current rules
without redoing what is fine. Text-layer pages the rules now read differently are inspected
again (repaired in place, or sent to OCR), OCR text with a repeated phrase is cut in place,
and pages read badly (cut off, looping, refused, the prompt echoed back) go back in the
queue. Then it OCRs the queue like `run`. Pages already read by OCR keep their reading
otherwise. `--dry-run` only counts.

**Text-layer pages read by OCR too:** a second run, kept apart from the first, OCRs the pages
the first run took from the PDFs' text layer:

```bash
.venv/bin/python -m ocr -c ocr_textlayer.toml reread --dry-run   # how many pages
tmux new -s ocr-textlayer '.venv/bin/python -m ocr -c ocr_textlayer.toml reread'
.venv/bin/python -m ocr -c ocr_textlayer.toml status
```

It copies the first run's state into `ocr_out_textlayer/` once and queues every text-layer
page there (`reason` = `text layer read again`). Pages already read by OCR, and blank pages, are
kept as they are, so `ocr_out_textlayer/` ends up a full set: every page by OCR, with its own
text files and mapping.csv. `ocr_out/` is only read. Stopped, it resumes with the same command
(or `run` with the same config).

**Flags rechecked after a run:** `recheck` looks at the flagged pages again from their stored
text, without reading anything again or queuing anything. It drops the flags the text shows
were wrong. So far that's one kind: a `repetition_loop` that is only empty table cells (a
sign-in sheet's or a form's blank rows) in a reading the model finished by itself. A reading cut
off at the token limit keeps its flags. Then it rewrites the changed documents' text files,
`flagged_pages.csv` and mapping.csv. It refuses to write while a run is going.

```bash
.venv/bin/python -m ocr recheck --dry-run                     # what it would change (ocr_out)
.venv/bin/python -m ocr recheck                               # change it
.venv/bin/python -m ocr -c ocr_textlayer.toml recheck         # the same for ocr_out_textlayer
```

## Files

- `collector/crawler.py`: crawl loop, rate limiting, robots, redirects, downloads
- `collector/urls.py`: URL normalization, scope rules, link extraction
- `collector/config.py`: the settings and their defaults (file types, download patterns)
- `collector/check.py`: `check`, the pre-crawl look at a site, and the config it suggests
- `collector/sites/`: site rules; `senate.py` holds senate.go.th's link quirks
- `collector/saver.py`: file names and on-disk layout
- `collector/store.py`: SQLite state (`state/collector.db`), which makes runs resumable
- `state/collector.log`: full log of every run
- `ocr/runner.py`: the OCR run: text layers, the OCR queue, text files and `mapping.csv`
- `ocr/textlayer.py`: when a page's own text is usable, and Thai text repair
- `ocr/pdf.py`: reading and rendering PDFs (PyMuPDF, in worker processes)
- `ocr/engine.py`: the OCR engines, the backup, retries and quality flags
- `ocr/store.py`: SQLite OCR state (`ocr_out/ocr.db`)
