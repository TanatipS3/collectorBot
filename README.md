# collectorBot

English: [README.en.md](README.en.md)

collectorBot คือบอทเก็บเอกสารจากเว็บไซต์ มันเปิดหน้าเว็บไล่ตามลิงก์ไปทีละหน้า แล้วดาวน์โหลดเอกสารทุกไฟล์ที่เจอ
(PDF, Word, Excel, PowerPoint, e-book, ไฟล์บีบอัด) มาเก็บไว้ในเครื่อง โดยคงโครงสร้างโฟลเดอร์ตาม URL และชื่อไฟล์ภาษาไทยเดิมไว้

- **หยุดแล้วทำต่อได้**: กด Ctrl+C เมื่อไหร่ก็ได้ รอบถัดไปจะทำต่อจากจุดเดิม ไม่โหลดไฟล์ซ้ำ
- **ไม่เก็บไฟล์ซ้ำ**: ไฟล์ที่เนื้อหาเหมือนกันทุกไบต์ เก็บไว้ชุดเดียว
- **สุภาพต่อเซิร์ฟเวอร์**: จำกัดจำนวนการเชื่อมต่อ เว้นระยะระหว่างคำขอ ชะลอเองเมื่อเซิร์ฟเวอร์เริ่มรับไม่ไหว และทำตาม robots.txt
- **ใช้กับเว็บไหนก็ได้**: สิ่งที่เป็นของเว็บใดเว็บหนึ่งอยู่ในไฟล์ตั้งค่าทั้งหมด
  `config.toml` คือค่าของเว็บวุฒิสภา (www.senate.go.th) ส่วนเว็บอื่นให้เริ่มจาก `config.example.toml`

โปรเจกต์นี้มีอีกสองส่วนที่ทำงานต่อจากการเก็บไฟล์ คือ `ocr/` (อ่าน PDF ออกมาเป็นข้อความ) และ `classify/` (จัดหมวดเอกสาร)
ดู[ด้านล่าง](#ocr-และการจัดหมวดเอกสาร)

## สารบัญ

- [ติดตั้ง](#ติดตั้ง)
- [คำสั่งที่ใช้](#คำสั่งที่ใช้)
- [ใช้กับเว็บไซต์อื่น](#ใช้กับเว็บไซต์อื่น)
- [หลักการทำงาน](#หลักการทำงาน)
- [การตั้งค่า](#การตั้งค่า)
- [ผลลัพธ์ที่ได้](#ผลลัพธ์ที่ได้)
- [รันบนเซิร์ฟเวอร์ Linux](#รันบนเซิร์ฟเวอร์-linux)
- [ข้อจำกัด](#ข้อจำกัด)
- [โครงสร้างโค้ดและการทดสอบ](#โครงสร้างโค้ดและการทดสอบ)

## ติดตั้ง

ต้องใช้ Python 3.10 ขึ้นไป บน Windows หรือ Linux

```bash
git clone https://github.com/TanatipS3/collectorBot.git
cd collectorBot
pip install -r requirements.txt
```

บน Linux ให้พิมพ์ `python3` แทน `python` ในทุกคำสั่ง

## คำสั่งที่ใช้

| คำสั่ง | ทำอะไร |
|---|---|
| `python -m collector crawl` | เริ่มเก็บ หรือทำต่อจากรอบที่แล้ว |
| `python -m collector crawl --limit 50` | ทดลอง: หยุดหลังส่งคำขอครบ 50 ครั้ง |
| `python -m collector status` | ดูความคืบหน้าและข้อผิดพลาดล่าสุด (เปิดดูได้ขณะบอทกำลังทำงาน) |
| `python -m collector manifest` | สร้าง `manifest.csv`: รายการไฟล์ทั้งหมด มาจาก URL ไหน เจอลิงก์ที่หน้าไหน |
| `python -m collector crawl --refresh` | ภายหลัง: เปิดทุกหน้าใหม่ เพื่อหาเอกสารที่เว็บเพิ่มเข้ามา |
| `python -m collector crawl --retry-errors` | ลอง URL ที่ล้มเหลวอีกครั้ง |
| `python -m collector snapshot state.db` | คัดลอกสถานะการเก็บเป็นไฟล์เดียว (ไว้ย้ายไปทำต่อที่เครื่องอื่น) |

ถ้าไม่ระบุ ทุกคำสั่งจะใช้ `config.toml` ถ้าจะใช้ไฟล์ตั้งค่าอื่นให้ใส่ `-c` **ก่อน**ชื่อคำสั่ง:

```bash
python -m collector -c mysite.toml crawl
python -m collector -c mysite.toml status
python -m collector -c mysite.toml manifest -o mysite.csv
```

`crawl` รับ `--workers N` และ `--delay วินาที` เพื่อใช้แทนค่าในไฟล์ตั้งค่าเฉพาะรอบนั้นได้

กด **Ctrl+C** (หรือสั่ง `kill`) ได้ทุกเมื่อ คำขอที่กำลังทำอยู่จะทำต่อจนเสร็จ แล้วสถานะจะถูกบันทึกไว้
ถ้าสั่งเก็บสองตัวพร้อมกันบนสถานะเดียวกัน ตัวที่สองจะไม่ยอมทำงาน เพราะจะทำให้เว็บรับภาระเป็นสองเท่า

## ใช้กับเว็บไซต์อื่น

### 0. ตรวจก่อนว่าเว็บนั้นเก็บได้ไหม

บอทอ่าน HTML ที่เซิร์ฟเวอร์ส่งมาให้ ไม่ได้รัน JavaScript แบบเบราว์เซอร์ ให้เปิดหน้าที่มีเอกสารในเบราว์เซอร์
กด **Ctrl+U** (ดูซอร์สของหน้า) แล้วค้นคำว่า `.pdf`

- **เจอลิงก์เอกสาร** เช่น `<a href="/files/report.pdf">`: เก็บได้ ทำขั้นต่อไปได้เลย
- **เจอเป็นการเรียก JavaScript** เช่น `onclick="openDoc(42,'report.pdf')"`: เก็บได้ โดยเพิ่ม `link_rules` (ขั้นที่ 4)
- **ไม่เจออะไรเลย** หน้าว่างแล้วเนื้อหาค่อยโหลดตามมา: เว็บสร้างหน้าด้วย JavaScript เก็บตรง ๆ ไม่ได้ (ดู[ข้อจำกัด](#ข้อจำกัด))

### 1. สร้างไฟล์ตั้งค่า

```bash
cp config.example.toml mysite.toml        # Windows: copy config.example.toml mysite.toml
```

### 2. แก้ค่าหลัก

ค่าที่ต้องแก้มีแค่นี้ ในไฟล์มีเครื่องหมาย `← แก้` กำกับไว้:

```toml
start_urls  = ["https://www.example.go.th/", "https://www.example.go.th/download"]
crawl_hosts = ["www.example.go.th"]                   # หน้าเว็บที่จะเปิดอ่านหาลิงก์
file_hosts  = ["*.example.go.th", "cdn.example.com"]  # ที่ที่อนุญาตให้ดาวน์โหลดเอกสาร
exclude     = ['/search', '[?&]q=', '/login', '/calendar/']
output_dir  = "downloads_mysite"                      # แยกโฟลเดอร์ต่อเว็บ
state_dir   = "state_mysite"
```

### 3. ทดลองรันสั้น ๆ แล้วดูผล

```bash
python -m collector -c mysite.toml crawl --limit 50
python -m collector -c mysite.toml status
```

ดูใน `state_mysite/collector.log` ว่าบอทเข้าหน้าไหนบ้าง ถ้าเจอ URL ที่วนไม่รู้จบหรือไม่มีเอกสาร
(ปฏิทินที่กดเดือนถัดไปได้เรื่อย ๆ, การเรียงลำดับ `?sort=`, ตัวกรอง, หน้าสำหรับพิมพ์) ให้เพิ่ม regex ใน `exclude` แล้วรันใหม่
URL ที่ค้างอยู่ในคิวจะถูกจัดใหม่ตามค่าใหม่เอง ไม่ต้องเริ่มใหม่

ถ้าอยากเริ่มใหม่ทั้งหมด ให้ลบโฟลเดอร์ `state_dir` (และ `output_dir` ถ้าไม่ต้องการไฟล์เดิม)

### 4. ถ้าเอกสารถูกเปิดด้วย JavaScript

ถ้าเขียนรูปแบบเป็น regex ได้ ให้ใช้ `link_rules` ในไฟล์ตั้งค่า ไม่ต้องแก้โค้ด:

```toml
link_rules = [
    # <button onclick="openDoc(42, 'report.pdf')">  →  /docs/42/report.pdf
    { find = '''openDoc\((\d+),\s*'([^']+)'\)''', url = "/docs/{1}/{2}" },
]
```

`find` คือ regex ที่จะหาในซอร์ส HTML ของหน้า ส่วน `url` คือแม่แบบ URL โดย `{1}`, `{2}` คือข้อความที่จับได้ในวงเล็บกลุ่มที่ 1, 2
ของ regex (`{0}` คือข้อความที่ตรงทั้งหมด) URL ที่ได้จะต่อกับ URL ของหน้าเหมือนลิงก์ปกติ ถ้าแม่แบบอ้างกลุ่มที่ regex ไม่มี
บอทจะแจ้งข้อผิดพลาดตั้งแต่ตอนเริ่ม

ถ้าซับซ้อนเกินกว่า regex เช่น ต้องอ่านค่าจากอีกส่วนของหน้า หรือต้องสร้างเลขหน้าเอง ให้เขียน[กฎเฉพาะเว็บ](#กฎเฉพาะเว็บ-collectorsites)เป็น Python

### 5. รันจริง

```bash
python -m collector -c mysite.toml crawl
```

ถ้ารันบนเซิร์ฟเวอร์ ให้รันใน tmux (ดู[รันบนเซิร์ฟเวอร์ Linux](#รันบนเซิร์ฟเวอร์-linux))

## หลักการทำงาน

```
start_urls ──► คิว URL (state_dir/collector.db)
                  │
                  ▼
           1. แยกประเภท URL
                  ├─ ข้าม  : ตรง exclude, เป็นรูป/css/js, หรืออยู่นอกโฮสต์ที่กำหนด
                  ├─ file : นามสกุลเอกสาร และโฮสต์อยู่ใน file_hosts
                  └─ page : โฮสต์อยู่ใน crawl_hosts
                  ▼
           2. ส่งคำขอ (คุมจังหวะต่อโฮสต์ + ตรวจ robots.txt)
                  ▼
           3. ดูว่าได้อะไรกลับมาจริง (Content-Type, ไบต์แรกของไฟล์)
                  ├─ หน้า HTML : ดึงลิงก์ทั้งหมด แล้วใส่กลับเข้าคิว (กลับไปข้อ 1)
                  ├─ เอกสาร   : ดาวน์โหลด → ตรวจซ้ำด้วย SHA-256 → บันทึกลง output_dir
                  └─ อย่างอื่น  : ข้าม พร้อมบันทึกเหตุผล
```

### 1. แยกประเภท URL (`classify()` ใน `collector/urls.py`)

ทุก URL ถูกแปลงเป็นรูปมาตรฐานก่อน (ตัดส่วน `#...` ออก, ชื่อโฮสต์เป็นตัวพิมพ์เล็ก, ภาษาไทยถูก encode แบบเดียวกัน)
URL เดียวกันที่เขียนต่างกันจึงเข้าคิวแค่ครั้งเดียว จากนั้นตัดสินตามลำดับนี้:

1. ตรง regex ใน `exclude` → ไม่เข้า
2. นามสกุลเป็นรูป วิดีโอ ฟอนต์ css js → ไม่เข้า
3. นามสกุลเป็นเอกสาร (pdf, docx, xlsx, pptx, zip, ...) → ดาวน์โหลด ถ้าโฮสต์อยู่ใน `file_hosts`
4. โฮสต์อยู่ใน `crawl_hosts` → เปิดเป็นหน้าเว็บ (ถ้าตรง `crawl_host_download_patterns` จะเข้าคิวดาวน์โหลดแทน)
5. โฮสต์อยู่ใน `file_hosts` และ URL ตรง `download_patterns` → ดาวน์โหลด
6. นอกนั้น → ไม่เข้า

### 2. ส่งคำขอ (`collector/crawler.py`)

หน้าเว็บกับไฟล์ดาวน์โหลดใช้เธรดและคิวแยกกัน ไฟล์ขนาด 100 MB จึงไม่ขวางการเปิดหน้าเว็บ ซึ่งเป็นทางไปสู่เอกสารอื่น ๆ
ตัวคุมจังหวะ (`HostGovernor`) จำกัดจำนวนการเชื่อมต่อต่อโฮสต์และเว้นระยะระหว่างคำขอ
(ดู[ความสุภาพต่อเซิร์ฟเวอร์](#ความสุภาพต่อเซิร์ฟเวอร์)) บอทตาม redirect ทีละขั้นเอง เพื่อตรวจ robots.txt และคุมจังหวะได้ทุกขั้น

### 3. ตัดสินจากคำตอบจริง ไม่ใช่แค่นามสกุลใน URL

- `Content-Type` เป็น HTML → อ่านหาลิงก์ (เฉพาะหน้าที่อยู่ใน `crawl_hosts`)
- `Content-Type` เป็นชนิดเอกสาร หรือ `Content-Disposition` บอกชื่อไฟล์เอกสาร → ดาวน์โหลด
  ลิงก์อย่าง `?dl=6094` ที่ redirect ไปหา PDF จึงได้ไฟล์มาเสมอ
- ไม่บอกชนิด (ว่าง หรือ `application/octet-stream`) → ดูไบต์แรกของไฟล์ `%PDF-` คือ PDF ส่วน `PK..` คือไฟล์ Office หรือ zip

**ลิงก์ถูกดึงมาจากที่ไหนบ้าง** (`extract_links()`):

- แอตทริบิวต์ `href`, `src`, `data`, `data-src`, `data-href`, `data-url`, `data-file` ของทุกแท็ก
- `<meta http-equiv="refresh">` และ `<base href>`
- path ของเอกสารที่อยู่ในสคริปต์หรือ `onclick` เช่น `'/files/a.pdf'`
- ตัวเปิด PDF ที่ส่งชื่อไฟล์ผ่าน query เช่น `viewer.html?file=/x/book.pdf`
- `link_rules` จากไฟล์ตั้งค่า
- กฎเฉพาะเว็บ (`site`)

### 4. สถานะ ทำให้หยุดแล้วทำต่อได้

ทุก URL ที่เจออยู่ในฐานข้อมูล SQLite (`state_dir/collector.db`) พร้อมสถานะของมัน เมื่อเริ่มรอบใหม่ บอทจะ:

- ลอง URL ที่ล้มเหลวชั่วคราวในรอบก่อนอีกครั้ง (timeout, 5xx, 403)
- ตรวจ URL ที่ถูก robots.txt บล็อกซ้ำ เผื่อ robots.txt หรือการตั้งค่าเปลี่ยนไป (ไม่ต้องส่งคำขอเพิ่ม)
- จัด URL ในคิวใหม่ตาม `exclude` และโฮสต์ล่าสุด ถ้าแก้ไฟล์ตั้งค่าไว้

## การตั้งค่า

### ต้องแก้อะไรเมื่อเปลี่ยนเว็บ

| ค่า | ความหมาย | เปลี่ยนเว็บต้องแก้ไหม |
|---|---|---|
| `start_urls` | หน้าเริ่มต้น | **ต้องแก้** |
| `crawl_hosts` | โฮสต์ที่เปิดหน้า HTML อ่านหาลิงก์ (`*.example.go.th` = ทุกโดเมนย่อย) | **ต้องแก้** |
| `file_hosts` | โฮสต์ที่ดาวน์โหลดเอกสารได้ ควรรวมโฮสต์ใน `crawl_hosts` ด้วย | **ต้องแก้** |
| `exclude` | regex ของ URL ที่ไม่เข้าเลย | **ต้องแก้** ตามเว็บ |
| `output_dir` | โฟลเดอร์เก็บไฟล์ (ค่าเริ่มต้น `downloads`) | **แยกต่อเว็บ** |
| `state_dir` | โฟลเดอร์เก็บสถานะและ log (ค่าเริ่มต้น `state`) | **แยกต่อเว็บ** |
| `link_rules` | สร้างลิงก์จาก regex ที่หาใน HTML | ถ้าเว็บเปิดไฟล์ด้วย JavaScript |
| `site` | กฎเฉพาะเว็บเป็น Python (`collector/sites/<ชื่อ>.py`) | ถ้า regex ไม่พอ |
| `crawl_host_download_patterns` | URL บน `crawl_hosts` ที่รู้แน่ว่าเป็นลิงก์ดาวน์โหลด | ไม่จำเป็น |
| `download_patterns` | URL ดาวน์โหลดที่ไม่มีนามสกุล บนโฮสต์ไฟล์ที่ไม่ได้อยู่ใน `crawl_hosts` | ปกติไม่ต้อง |
| `extra_file_exts` | ชนิดไฟล์ที่เก็บเพิ่ม เช่น `["txt", "xml", "jpg"]` | ถ้าต้องการไฟล์ชนิดอื่น |
| `accept_language` | ภาษาที่ขอจากเว็บ (ค่าเริ่มต้น `th,en;q=0.8`) | ถ้าเว็บมีหลายภาษา |
| `user_agent` | ชื่อที่บอทแจ้งกับเซิร์ฟเวอร์ | ปกติไม่ต้อง |
| `workers`, `delay`, `max_per_host`, `pages_per_host`, `max_delay`, `timeout`, `max_retries` | ความเร็วและความสุภาพ | ปกติไม่ต้อง |
| `max_file_mb` | ข้ามไฟล์ที่ใหญ่กว่านี้ (0 = ไม่จำกัด) | ตามพื้นที่ดิสก์ |
| `respect_robots` | ทำตาม robots.txt (ค่าเริ่มต้น `true`) | ไม่ควรปิด |

ถ้าพิมพ์ชื่อค่าผิด บอทจะหยุดทันทีและบอกชื่อที่ไม่รู้จัก ในไฟล์ TOML ให้เขียน regex ด้วย `'...'` จะได้ไม่ต้องเขียน `\` สองตัว
regex ใน `exclude` และ `*_patterns` ไม่สนตัวพิมพ์เล็ก/ใหญ่ ส่วน `find` ของ `link_rules` สนใจตัวพิมพ์

### เลือก `exclude` อย่างไร

regex ถูกเทียบกับ URL ทั้งเส้น ตรงส่วนไหนก็ได้ ข้อควรระวังคือ URL ถูกเก็บในรูป encode แล้ว ภาษาไทยใน URL จึงเป็น
`%E0%B8%...` ถ้าจะตัด path ภาษาไทย ให้ใช้รูป encode (ดูได้จาก log หรือ `state_dir/collector.db`)

| ปัญหาที่เจอบ่อย | regex |
|---|---|
| ช่องค้นหา | `'/search'`, `'[?&](q\|keyword)='` |
| ปฏิทินที่วนไม่จบ | `'/calendar/'`, `'[?&](month\|year)='` |
| การเรียงลำดับและตัวกรอง | `'[?&](sort\|order\|filter)='` |
| หน้าสำหรับพิมพ์หรือแชร์ | `'/print/'`, `'share'` |
| ล็อกอินและระบบภายใน | `'/login'`, `'logout'`, `'webmail'` |
| ภาษาอื่นที่ไม่ต้องการ | `'/en/'`, `'[?&]lang=en'` |

`config.toml` ของวุฒิสภามีตัวอย่างจริง พร้อมคำอธิบายว่าตัดแต่ละอันเพราะอะไร

### กฎเฉพาะเว็บ (`collector/sites/`)

สำหรับลักษณะเฉพาะที่ regex ทำไม่ได้ ให้สร้างไฟล์ `collector/sites/<ชื่อ>.py` แล้วใส่ `site = "<ชื่อ>"` ในไฟล์ตั้งค่า
เขียนเฉพาะส่วนที่ต้องเปลี่ยน ส่วนที่ไม่เขียนจะทำงานแบบปกติ:

```python
# collector/sites/mysite.py
import re

from . import SiteRules

_PAGER = re.compile(r'data-total-pages="(\d+)"')


class MySiteRules(SiteRules):
    name = "mysite"

    def resolve_href(self, href):        # href ดิบ ก่อนต่อเข้ากับ URL ของหน้า
        return href

    def fix_url(self, url):              # URL หลังต่อและแปลงเป็นรูปมาตรฐานแล้ว
        return url

    def extra_links(self, html, soup):   # ลิงก์เพิ่ม (relative หรือ absolute ก็ได้)
        m = _PAGER.search(html)          # ตัวอย่าง: ตัวแบ่งหน้าที่สร้างด้วย JavaScript
        total = int(m.group(1)) if m else 1
        return [f"?page={n}" for n in range(2, total + 1)]


RULES = MySiteRules()
```

`soup` คือหน้าเว็บที่ BeautifulSoup แยกส่วนไว้แล้ว ใช้ `soup.select(...)` หาแท็กได้

ตัวอย่างจริงอยู่ที่ `collector/sites/senate.py` ซึ่งจัดการลักษณะเฉพาะของเว็บวุฒิสภา:

- เลขหน้าเขียนเป็น `href="/?&page41=2"` (เบราว์เซอร์จะพาไปหน้าแรก แต่เว็บหมายถึงหน้าปัจจุบัน)
- ไฟล์แนบข่าวเปิดด้วย `showFileName(22619,'1_22619.pdf',…)`
- ตัวอ่าน e-book บอกไฟล์ไว้ใน `<li key="pdf_url">`
- ตารางจัดซื้อจัดจ้างแบ่งหน้าด้วย JavaScript (`ul.purchase-pager`)
- ลิงก์ไฟล์ที่ลืมใส่ `/` นำหน้า

เมื่อเพิ่มกฎใหม่ ควรเพิ่มเทสต์ด้วย `tests/test_crawl.py` สร้างเว็บปลอมที่มีลักษณะแปลก ๆ เหล่านี้ แล้วรันบอทจริงกับมัน

### ความสุภาพต่อเซิร์ฟเวอร์

- แต่ละโฮสต์เปิดดาวน์โหลดพร้อมกันได้ไม่เกิน `max_per_host` และเปิดหน้าเว็บได้อีก `pages_per_host` คำขอแต่ละครั้งเริ่มห่างกันอย่างน้อย
  `delay` วินาที ถ้า robots.txt กำหนด `Crawl-delay` ที่มากกว่าก็ใช้ค่านั้น
- เมื่อเจอสัญญาณว่าเซิร์ฟเวอร์รับไม่ไหว (429, 502, 503, 504, timeout, การเชื่อมต่อหลุด) ติดกันตั้งแต่ครั้งที่สอง ระยะห่างจะเพิ่มเป็นสองเท่า
  สูงสุดถึง `max_delay` เมื่อกลับมาสำเร็จก็ค่อย ๆ ลดลง และทำตาม `Retry-After` เสมอ
- 500 (หน้าเสีย) และ 403 (อาจถูกบล็อก) ไม่ทำให้ช้าลง แต่นับรวมในการตรวจสุขภาพ
- ล้มเหลวติดกัน 5 ครั้ง บอทจะลองโหลด robots.txt ของโฮสต์นั้น ถ้าตอบปกติและมีคำขอสำเร็จใน 30 วินาทีที่ผ่านมา แปลว่าแค่ลิงก์เสีย จึงทำต่อ
  ถ้าไม่ใช่ จะหยุดส่งคำขอไปโฮสต์นั้น 5, 10, 20 แล้ว 30 นาที ถ้ายังไม่ดีขึ้นจะเลิกติดต่อโฮสต์นั้นในรอบนี้ (URL ยังอยู่ในคิวรอรอบหน้า)
- ค่าเริ่มต้น (ดาวน์โหลด 2 + หน้าเว็บ 1 การเชื่อมต่อ ห่างกัน 1 วินาที) ปลอดภัย `config.toml` ของวุฒิสภาใช้ 4 + 2 ห่างกัน 0.25 วินาที
  ซึ่งค่อนข้างเร็ว อย่าเพิ่มค่าเหล่านี้กับเว็บที่ไม่รู้ว่าเซิร์ฟเวอร์รับได้แค่ไหน

### robots.txt

เมื่อ `respect_robots = true` (ค่าเริ่มต้น) URL ที่ robots.txt ห้ามจะถูกบันทึกเป็น `blocked` และไม่ถูกเข้าเลย ดูจำนวนได้จาก `status`
URL เหล่านี้ถูกตรวจซ้ำทุกรอบ ถ้า robots.txt เปลี่ยนก็จะถูกเก็บโดยไม่ต้องไล่เว็บใหม่ ถ้า robots.txt ของโฮสต์ตอบ 5xx
บอทถือว่าห้ามทั้งโฮสต์ (ตาม RFC 9309) การปิด `respect_robots` เป็นการตัดสินใจของผู้ใช้ เพราะเจ้าของเว็บขอให้บอทอยู่ห่างจาก path เหล่านั้น

## ผลลัพธ์ที่ได้

```
<output_dir>/              ไฟล์ที่ดาวน์โหลด
<state_dir>/collector.db   สถานะของทุก URL (SQLite)
<state_dir>/collector.log  log ของทุกรอบ
manifest.csv               รายการไฟล์ (สร้างด้วยคำสั่ง manifest)
```

### ไฟล์ที่ดาวน์โหลด (`output_dir`)

```
downloads/
├── document.senate.go.th/
│   └── approve/64.pdf
├── www.senate.go.th/
│   └── assets/portals/1/files/ebook-pdf/sum_c2569.pdf
└── pis.parliament.go.th/
    └── PARFileDownloadProxy/download/หนังสือนัดประชุม ค 13.pdf
```

- ชั้นแรกคือชื่อโฮสต์ ถัดมาคือ path ของ URL ชื่อไฟล์ภาษาไทยถูกถอดกลับเป็นตัวอักษรไทย (ได้ทั้ง UTF-8 และ TIS-620)
- ถ้าเซิร์ฟเวอร์ส่งชื่อไฟล์มาใน `Content-Disposition` จะใช้ชื่อนั้น
- URL ที่ไม่มีชื่อไฟล์จะได้ชื่อ `download` และ URL ที่ต่างกันแค่ query (`?id=1`, `?id=2`) จะได้ `__<hash 8 ตัว>` ต่อท้ายชื่อ ไม่ให้ชนกัน
- ไฟล์ที่นามสกุลไม่ใช่ของเอกสาร (เช่น `…_purchase_file.50` ที่จริงเป็น PDF) จะถูกเติมนามสกุลจริงให้: `…_purchase_file.50.pdf`
- ไฟล์ที่เนื้อหาเหมือนกันทุกไบต์จะเก็บไว้ชุดเดียว URL ที่สองถูกบันทึกว่า `duplicate of <path>`
- คนละไฟล์แต่ชื่อชนกัน ไฟล์หลังจะได้ชื่อ `ชื่อ_<sha256 8 ตัว>.pdf`
- ชื่อไฟล์ถูกปรับตามกฎของ Windows (ตัดอักขระต้องห้าม, จำกัดความยาว) และรองรับ path ที่ยาวเกิน 260 ตัวอักษร
- ระหว่างดาวน์โหลด ไฟล์อยู่ใน `output_dir/.partial/` และย้ายเข้าที่เมื่อโหลดครบแล้วเท่านั้น ไฟล์ใน `output_dir` จึงสมบูรณ์เสมอ

### `manifest.csv`

สร้างด้วย `python -m collector manifest` (ใส่ `-o ชื่อไฟล์.csv` เพื่อเปลี่ยนชื่อ) หนึ่งแถวต่อหนึ่ง URL ที่ได้ไฟล์มา รวม URL ที่ได้ไฟล์ซ้ำ
บันทึกเป็น UTF-8 แบบมี BOM เปิดใน Excel แล้วภาษาไทยไม่เพี้ยน

| คอลัมน์ | ความหมาย |
|---|---|
| `path` | ไฟล์ใน `output_dir` |
| `url` | URL ที่เจอในหน้าเว็บ |
| `final_url` | URL สุดท้ายหลัง redirect |
| `size` | ขนาดไฟล์ (ไบต์) |
| `sha256` | ลายนิ้วมือของไฟล์ |
| `content_type` | ชนิดไฟล์ |
| `found_on` | หน้าที่มีลิงก์นี้ |
| `note` | หมายเหตุ เช่น `duplicate of …` |
| `downloaded_at` | เวลาที่โหลด (UTC) |

```csv
path,url,final_url,size,sha256,content_type,found_on,note,downloaded_at
document.senate.go.th/approve/64.pdf,https://document.senate.go.th/approve/64.pdf,https://document.senate.go.th/approve/64.pdf,5242880,9f2c41…,application/pdf,https://document.senate.go.th/,,2026-09-12T03:14:07+00:00
```

### `status`

```
            done   pending  fetching   skipped   blocked     error
page        4212       318         2       905        11        64
file        9876      1204         4        37       420        52

downloaded: 48213.7 MB (excluding duplicates)

recent errors:
  [file] HTTP 404  https://www.example.go.th/files/old.pdf
```

| สถานะ | ความหมาย |
|---|---|
| `done` | เสร็จแล้ว (หน้าเว็บอ่านลิงก์แล้ว หรือไฟล์ดาวน์โหลดแล้ว) |
| `pending` | รอในคิว |
| `fetching` | กำลังทำอยู่ |
| `skipped` | ไม่ใช่เอกสาร, ถูก `exclude`, redirect ออกนอกขอบเขต หรือใหญ่เกิน `max_file_mb` |
| `blocked` | robots.txt ห้าม |
| `error` | ล้มเหลว (404, 500, timeout, ...) ลองใหม่ด้วย `--retry-errors` |

### log (`state_dir/collector.log`)

```
2026-11-04 14:02:11 INFO    crawling https://www.example.go.th/ -> downloads_mysite (site rules=generic, workers=4, delay=1.00s, robots=True)
2026-11-04 14:02:13 INFO    saved www.example.go.th/files/รายงานประจำปี 2567.pdf (2.3 MB)
2026-11-04 14:02:14 INFO    duplicate https://www.example.go.th/news?file=12 (same bytes as www.example.go.th/files/รายงานประจำปี 2567.pdf)
2026-11-04 14:02:20 WARNING error https://www.example.go.th/files/old.pdf: HTTP 404
2026-11-04 14:02:26 INFO    pages 12 done, 140 queued | files 9 saved (31.2 MB), 57 queued | skipped 3 | robots-blocked 0 | errors 1
```

### ฐานข้อมูลสถานะ (`state_dir/collector.db`)

ไฟล์ SQLite มีตาราง `urls` หนึ่งแถวต่อทุก URL ที่บอทเจอ คอลัมน์: `url`, `kind` (page/file), `status`, `depth`, `referrer`
(หน้าที่เจอลิงก์), `http_status`, `content_type`, `final_url`, `path`, `sha256`, `size`, `note`, `attempts`, `updated_at`

```bash
sqlite3 state/collector.db "SELECT status, COUNT(*) FROM urls WHERE kind='file' GROUP BY status"
sqlite3 state/collector.db "SELECT url, note FROM urls WHERE status='error' LIMIT 20"
```

## รันบนเซิร์ฟเวอร์ Linux

ติดตั้งครั้งแรก:

```bash
cd ~/collectorBot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

(บน Ubuntu, `python3 -m venv` ต้องมีแพ็กเกจ `python3-venv` ถ้าไม่มี ให้ใช้ `pip3 install --user -r requirements.txt` แล้วพิมพ์ `python3`
แทน `.venv/bin/python`)

รันใน tmux เพื่อให้ทำงานต่อหลังออกจากระบบ:

```bash
tmux new -s collector '.venv/bin/python -m collector crawl'   # กด Ctrl+B แล้ว D เพื่อปล่อยให้รันต่อ
tmux attach -t collector                                       # กลับไปดู
.venv/bin/python -m collector status                           # ดูความคืบหน้าจาก shell ไหนก็ได้
tail -f state/collector.log                                    # ดู log
```

ถ้าดิสก์ home เล็ก ให้ชี้ไฟล์ไปดิสก์ใหญ่: `ln -s /data/collector-downloads downloads`

**ย้ายไปทำต่อที่เครื่องอื่น**: หยุดบอท แล้วสั่ง `python -m collector snapshot collector-state.db` คัดลอกโค้ด (ยกเว้น `downloads/`
และ `state/`) ไปเครื่องใหม่ วาง `collector-state.db` เป็น `state/collector.db` แล้วสั่ง `crawl` ที่นั่น เครื่องใหม่จะโหลดเฉพาะส่วนที่เหลือ
ห้ามรันเครื่องเดิมต่อ ไม่อย่างนั้นสองเครื่องจะทำงานซ้ำกันและเว็บรับภาระเป็นสองเท่า เมื่อเสร็จให้รวม `downloads/` ของสองเครื่องเข้าด้วยกัน
(ชื่อไม่ชนกัน)

## ข้อจำกัด

- **เว็บที่สร้างหน้าด้วย JavaScript** (React, Vue, Angular, หรือหน้าที่โหลดข้อมูลผ่าน API) บอทจะเห็นแค่โครงหน้าว่าง ทางแก้คือเรียก API
  ของเว็บโดยตรง หรือเพิ่มเบราว์เซอร์อัตโนมัติ (เช่น Playwright) ซึ่งยังไม่มีในโปรเจกต์นี้
- **เว็บที่ต้องล็อกอิน** ยังไม่รองรับ (ต้องเพิ่ม cookie หรือ token ใน `_session()` ของ `collector/crawler.py`)
- **การแบ่งหน้าแบบส่งฟอร์ม POST** (เช่น `__doPostBack` ของ ASP.NET) ตามไม่ได้ บอทตามเฉพาะลิงก์แบบ GET
- เก็บเฉพาะไฟล์ ไม่เก็บเนื้อหาหน้า HTML (หน้าเว็บถูกอ่านเพื่อหาลิงก์เท่านั้น) และอ่านหน้า HTML ได้ไม่เกิน 20 MB แรก
- ชื่อไฟล์ที่ encode ด้วยรหัสอื่นนอกจาก UTF-8 และ TIS-620 (เช่น Shift-JIS) อาจถูกถอดเป็นตัวอักษรผิด แต่เนื้อไฟล์ยังถูกต้อง

## โครงสร้างโค้ดและการทดสอบ

| ไฟล์ | หน้าที่ |
|---|---|
| `collector/__main__.py` | คำสั่ง `crawl`, `status`, `manifest`, `snapshot` |
| `collector/config.py` | อ่านไฟล์ตั้งค่า, ค่าเริ่มต้น, ชนิดไฟล์ที่เก็บ |
| `collector/urls.py` | แปลง URL เป็นรูปมาตรฐาน, แยกประเภท URL, ดึงลิงก์จากหน้า |
| `collector/crawler.py` | วงรอบการเก็บ, คุมจังหวะ, robots.txt, redirect, ดาวน์โหลด |
| `collector/saver.py` | ตั้งชื่อไฟล์และโครงสร้างโฟลเดอร์ |
| `collector/store.py` | ฐานข้อมูลสถานะ SQLite |
| `collector/sites/` | กฎเฉพาะเว็บ (`senate.py` = เว็บวุฒิสภา) |
| `config.toml` | ค่าตั้งของเว็บวุฒิสภา |
| `config.example.toml` | แม่แบบสำหรับเว็บใหม่ |

```bash
python -m pytest
```

`tests/test_crawl.py` รันบอทจริงกับเว็บปลอมในเครื่องที่จำลองลักษณะแปลก ๆ ของเว็บจริง (redirect, ไฟล์ไม่บอกชนิด, robots.txt,
เซิร์ฟเวอร์ล่ม, ถูกบล็อก) ส่วน `tests/test_urls.py` และ `tests/test_config.py` ทดสอบการแยก URL การดึงลิงก์ และไฟล์ตั้งค่า

## OCR และการจัดหมวดเอกสาร

สองส่วนนี้ทำงานต่อจากการเก็บไฟล์ รายละเอียดเต็มอยู่ใน [README.en.md](README.en.md#ocr)

- **`python -m ocr`** แปลง PDF ที่เก็บมาเป็นข้อความ หน้าที่มีข้อความในไฟล์อยู่แล้วจะใช้ข้อความนั้น (พร้อมซ่อมฟอนต์ไทยที่เพี้ยน)
  หน้าที่เป็นภาพสแกนจะส่งให้โมเดล vision อ่านผ่าน API แบบ OpenAI (`/v1/chat/completions`) ผลลัพธ์อยู่ใน `ocr_out/`:
  ไฟล์ข้อความหนึ่งไฟล์ต่อเอกสาร และ `mapping.csv` ที่โยงข้อความกลับไปหาไฟล์และ URL ต้นทาง ตั้งค่าใน `ocr.toml`
  ถ้าใช้กับเว็บอื่น ให้แก้ `collector_db` และ `downloads_dir` ใน `ocr.toml` ให้ชี้ไปที่ `state_dir/collector.db` และ `output_dir` ของเว็บนั้น
  และแก้ `primary_url` ให้ชี้ไปที่เซิร์ฟเวอร์ OCR ของคุณ
- **`classify/`** จัดหมวดเอกสารด้วยกฎและโมเดลภาษา หมวดและกฎทั้งหมดออกแบบมาสำหรับเอกสารวุฒิสภา (`classify/labels.md`)
  ถ้าใช้กับเว็บอื่นต้องออกแบบหมวดใหม่
