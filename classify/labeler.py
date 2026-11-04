"""Rule-based labeler built from reading the 11% sample. Content rules first (strong signals that
override where a file was published), then source-section rules, then weak content fallbacks."""
import csv
import re
import sys
from collections import Counter, defaultdict

GROUP = {
    "law_text": "A_law", "court_decision": "A_law", "legal_knowledge": "A_law",
    "bill_documents": "B_legislative", "plenary_sitting": "B_legislative", "interpellation": "B_legislative",
    "committee_report": "B_legislative", "committee_news": "B_legislative", "committee_activity": "B_legislative",
    "academic_brief": "C_knowledge", "academic_article": "C_knowledge", "senate_publication": "C_knowledge",
    "news_digest": "C_knowledge", "external_reference": "C_knowledge",
    "procurement": "D_admin", "budget_finance": "D_admin", "strategy_performance": "D_admin", "hr": "D_admin",
    "governance_transparency": "D_admin", "internal_km": "D_admin", "general_notice": "D_admin",
    "green_office": "E_green_office",
    "form_template": "F_exclude", "other": "F_exclude",
}

R = lambda p: re.compile(p)
# Green Office portals: one per Secretariat bureau (346-364) and the central one (291).
GREEN_PORTAL = R(r"^p(29[01]|34[6-9]|35\d|36[0-4])/")
GREEN_SECTION = R(r"(หมวด|Category|Green|measuresandguidelines|policy|Fuel|Water|electricity|Paper|Waste|Greenhouse|ทะเบียนกฎหมาย|"
                  r"การใช้ของเสีย|รายงานการปล่อย|publishing|บันทึกการประชุมปี|Events|HRD|DetailPRmedia|คณะทำงาน|สำนักงานสีเขียว|"
                  r"ภาพกิจกรรม|รายละเอียดภาพ|แนวทางปฏิบัติ|PR$|law$|MinutesAndShorts|\(no section\)|ประจำปี)")

# (pattern on snippet/filename, label). Applied to every document, first match wins.
STRONG = [
    (R(r"Pentest"), "other"),
    (R(r"สรุปมติการประชุมวุฒิสภา|บันทึกการประชุมวุฒิสภา|หนังสือนัดประชุมวุฒิสภา|งดการประชุมวุฒิสภา|ปรึกษาหารือ ?ก่อนเข้าสู่ ?ระเบียบวาระ"), "plenary_sitting"),
    (R(r"กระทู้ถาม"), "interpellation"),
    (R(r"รายงาน ?ของ ?คณะกรรม(า)?ธิการ ?วิสามัญ ?พิจารณาร่าง|รายนาม ?ของ ?คณะกรรม(า)?ธิการ ?วิสามัญ ?พิจารณาร่าง|"
       r"ความเป็นมา ?และประเด็น ?อภิปราย|สรุปประเด็นอภิปราย(การพิจารณา)?ร่างพระราชบัญญัติ|ต้องพิจารณาให้เสร็จภายใน|"
       r"เอกสารประกอบการพิจารณา(เบื้องต้น)?.{0,40}ร่าง|บรรจุระเบียบวาระ|ตารางเปรียบเทียบ.{0,30}(พระราชบัญญัติ|ร่าง)|"
       r"คำแปรญัตติ ?ร่าง|พร้อมทั้ง ?สรุปสาระสำคัญ ?ประวัติ|รายงาน ?(ผล)?การพิจารณา ?ศึกษา ?ร่าง ?พระราชบัญญัติ"), "bill_documents"),
    (R(r"ประกาศผู้ชนะการเสนอราคา|ประกวดราคา|ขายทอดตลาด|สรุปผลการดำเนินการจัดซื้อจัดจ้าง|เผยแพร่แผนการจัดซื้อจัดจ้าง|"
       r"แบบ บก\.? ?0?6|ตารางแสดงวงเงินงบประมาณที่ได้รับจัดสรร"), "procurement"),
]

# (pattern on cluster = "p<portal>/<section>", label). First match wins.
CLUSTER = [
    (R(r"^p1/ประกาศจัดซื้อจัดจ้าง|^p1/ประกาศร่างเอกสารประกวดราคา|^p1/ราคากลาง|^p1/แผนการจัดซื้อจัดจ้าง|^p1/รายงานผลการจัดซื้อ|"
       r"^p1/ขายทอดตลาด|^p13/สรุปผลจัดซื้อจัดจ้าง|^p13/ประกาศประกวดราคา"), "procurement"),
    (R(r"^p1/meeting_bookshelf|^p1/การประชุมสภา|^p1/หน้าแรก$|^p1/Digital_Discuss|^p1/Digital_Media_Data|^p1/การประชุมอดีตสภา|"
       r"^p93/เจตนารมณ์รัฐธรรมนูญ|^pis\.parliament"), "plenary_sitting"),
    (R(r"^document/|^p93/เอกสาร_กป|^p412/Infoร่าง|^p440/|^p4[5-9]\d/main|^p5\d\d/main|^p461/|^p466/|^p469/|^p451/เอกสารอื่น|"
       r"^p456/เอกสารประกอบ|^p452/สรุปประเด็น|^p483/"), "bill_documents"),
    (R(r"^p1/Digital_Questions|^p1/เชื่อมโยงเว็บไซต์"), "interpellation"),
    (R(r"^p1/Digital_Report|^p185/\(no section\)|^p185/SenatePublication|^p185/ebook|^p1/บทความ_รายงานการพิจารณาศึกษา"), "academic_brief"),
    (R(r"สรุปข่าวด้านสาธารณสุข|^p127/หน้าหลัก"), "news_digest"),
    (R(r"^p93/(สรุปคำพิพากษา|ภาค[กขคง])"), "court_decision"),
    (R(r"^p93/(บทความทางวิชาการ|บทสัมภาษณ์|สรุปการสัมมนาทางวิชาการ|วารสาร|EbookClip)|^p185/(Journal|ชวนค้นคว้า|main_page)|"
       r"^p1/ResearchSummary"), "academic_article"),
    (R(r"^p93/(เกร็ดกฎหมาย|แนะนำกฎหมายใหม่|ร่างกฎหมายที่น่าสนใจ|สารพันปัญหา|กฎหมายน่ารู้|หน้าต่างโลก|มุมสะท้อน|การปฏิรูปกฎหมาย|"
       r"บทวิเคราะห์รัฐธรรมนูญ|คมความคิด|สรุปมติคณะรัฐมนตรี|ร่างกฎหมายน่ารู้|หน้าแรกศูนย์ข้อมูลด้านกฎหมาย$)"), "legal_knowledge"),
    (R(r"^p93/(รัฐธรรมนูญ|ดัชนี|รวมกฎหมาย|ข้อบังคับ|\(no section\))|^p424/กฎหมาย|^p28/กฎหมาย|^p28/ระเบียบ|^p141/|^p144/กฎหมาย|"
       r"^p178/หน้าหลัก|^p181/กฎหมาย|^p126/Law$|^p128/|^p13/มาตรา_๗_๔|^p13/มาตรา_๙_๕|^p13/กฎหมายที่เกี่ยวข้อง|^p13/ประกาศ_แต่งตั้ง|"
       r"^p1/ebook_law|^p1/Home$|^p1/AccesstoParliament|^p404/|^p412/(main|ข้อบังคับ)|^p421/|^p42[34]/main|^p439/|^p467/|^p472/|^p515/|"
       r"^p360/[1-6]\.|^p347/law|^p349/law|^p358/detaillaw"), "law_text"),
    (R(r"^p1/bookshelf_law|^p1/main$|^p1/Senate_Homepage|^p1/หน้าหลักวุฒิสภา|^p1/หน้าที่และอำนาจ|^p1/领导层|"
       r"^p1/แผนภูมิ|^p13/รายชื่อสมาชิก|^p93/สิทธิประโยชน์"), "senate_publication"),
    (R(r"^p181/(แผนแม่บท|หน้าหลัก)|^p143/(รายงาน25|อนุโครงสร้าง|เอกสารต่างๆ|หน้าหลัก)|^p126/PTT|^p442/|^p185/Home"), "external_reference"),
    (R(r"^p1/แผนการใช้จ่ายงบประมาณ"), "budget_finance"),
    (R(r"^p1/การบริหารและพัฒนาทรัพยากรบุคคล|^p1/JobAnnouncement|^p13/มาตรา_๙_๘"), "hr"),
    (R(r"^p1/(ReportCorruption|มาตรการภายใน|ITA|OIT|การประเมินความเสี่ยง|เจตจำนง)|^p13/(สถิติการขอ|ผลการพิจารณา|เอกสารเผยแพร่|"
       r"รายงานผลการดำเนินงานด้านข้อมูล|ศูนย์ข้อมูลข่าวสาร|ช่องทาง|มาตรา_๙_๔|อำนาจหน้าที่|ข้อมูลข่าวสาร|รายงานผลมาตรฐาน|InformationCenter)|^p92/"),
     "governance_transparency"),
    (R(r"^p13/(แผนดำเนินงาน|มาตรา_๙_๓|คำรับรอง)|^p1/(คำรับรอง|HumanRightsWork|นโยบายของสำนักงาน|SupportEducation|BCM|Link-Ebook)"),
     "strategy_performance"),
    (R(r"^book_shelf/KMBookshelf|^p49/|^p1/(OperationsServices|Webex|แอปพลิเคชัน|application_senate|AnnouncementsAndPolicies|"
       r"หน้าหลักสำนักงานเลขาธิการ|committee$)|^p181/form_report"), "internal_km"),
    (R(r"^p1/(ข่าวสารสำนักงานเลขาธิการ|ข่าวสารวุฒิสภา|ภาพ-ข่าววุฒิสภา|office_of_the_secretariat|MeetTheSenator)"), "general_notice"),
    (R(r"^p1/ดาวน์โหลดแบบฟอร์ม|^p2/TopicOfSenator|ใบลาการประชุม|^p13/(ตรวจสอบทรัพย์สิน|ขั้นตอนการขอ)"), "form_template"),
]

# Weak content fallbacks, for committee pages and anything the rules above did not reach.
WEAK = [
    (R(r"รายงาน ?(ผล)?การพิจารณา ?ศึกษา|POLICY BRIEF|รายงานสรุปผลการศึกษาวิเคราะห์|รายงานความก้าวหน้า.{0,30}ปฏิรูป|"
       r"สรุปความคืบหน้าในการตราอนุบัญญัติ|สถานการณ์รายไตรมาส|ฐานข้อมูลทางวิชาการ|ข้อมูลประกอบการพิจารณา คณะกรรมาธิการ"), "committee_report"),
    (R(r"เอกสาร ?ข่าว|จดหมายข่าว|ข่าว ?กมธ|สรุปข่าวการประชุม|กรรมาธิการเล่าข่าว|ข่าวประชาสัมพันธ์"), "committee_news"),
    (R(r"สรุปผลการเดินทาง|ศึกษาดูงาน|รายงาน ?การเดินทาง|สรุปผล ?การสัมมนา|รายงานสรุปผลการสัมมนา|สรุปผลการเสวนา|"
       r"ระเบียบวาระการประชุม ?คณะกรรมาธิการ|ขอเอกสารเพิ่มเติม|สรุปผล ?การดำเนินงาน|ผลการดำเนินงานของ ?คณะ|คำสั่งคณะกรรมาธิการ|"
       r"รายนาม ?คณะกรรมาธิการ|ฝ่ายเลขานุการคณะกรรมาธิการ|กรอบการพิจารณา|แผนผังที่นั่ง|ตารางการพิจารณา|สรุปผลการประชุม|"
       r"ข้อมูลการอภิปราย|กำหนดการสัมมนา|เอกสารประกอบการสัมมนา|กรอบยุทธศาสตร์"), "committee_activity"),
    (R(r"ราชกิจจานุเบกษา|สำนักงานคณะกรรมการกฤษฎีกา|รัฐธรรมนูญแห่งราชอาณาจักรไทย"), "law_text"),
    (R(r"คำวินิจฉัยศาลรัฐธรรมนูญ|คำพิพากษา"), "court_decision"),
    (R(r"เอกสารวิชาการ"), "academic_brief"),
    (R(r"บทความ ?(ทาง)?วิชาการ"), "academic_article"),
]

NEWS_FIRST = R(r"ข่าว|จดหมายข่าว|^ฉบับที่ ?\d")
NEWS_SECTION = R(r"ข่าว|ช่าว|news|ประชาสัมพันธ์")
# (cluster pattern, content pattern, label): sections that hold more than one kind of document.
MIXED = [
    (R(r"^p93/หน้าแรกศูนย์ข้อมูลด้านกฎหมาย"), R(r"^.{0,80}ราชกิจจานุเบกษา|แก้ไขเพิ่มเติมถึงปัจจุบัน"), "law_text"),
    (R(r"^p93/หน้าแรกศูนย์ข้อมูลด้านกฎหมาย"), R(r"สรุปการสัมมนา|สรุปสัมมนา|บทความทางวิชาการ|บทสัมภาษณ์"), "academic_article"),
    (R(r"^p93/หน้าแรกศูนย์ข้อมูลด้านกฎหมาย"), R(r"เจ้าหน้าที่ผู้ให้บริการ|สถิติ"), "governance_transparency"),
    (R(r"^p1/ebook_law"), R(r"คู่มือ"), "internal_km"),
    (R(r"^p185/ebook"), R(r"^(?!.*เอกสารวิชาการ)"), "academic_article"),
    (R(r"^p1/(ข่าวสาร|ภาพ-ข่าว)"), R(r"นักกฎหมายนิติบัญญัติ|รับสมัคร|การดำเนินการทางวินัย|วินัย คือ"), "hr"),
    (R(r"^p13/"), R(r"^.{0,40}งบประมาณรายจ่าย ?ประจำปีงบประมาณ"), "budget_finance"),
    (R(r"^p13/ขั้นตอนการขอ"), R(r"แผนผังกระบวนการ"), "governance_transparency"),
]

COMMITTEE_PORTAL = R(r"^p(1[2-9]\d|2[0-8]\d|3[89]\d|4[0-4]\d|1\d\d)/|^p(2[0-9]|4[0-9]?)/")  # committee & subcommittee portals


def label(r: dict) -> tuple[str, str]:
    text = (r["filename"] + " | " + r["snippet"])[:600]
    c = r["cluster"]
    if GREEN_PORTAL.match(c) and GREEN_SECTION.search(c.split("/", 1)[1]):
        # Content wins over a mis-filed document; the laws inside green-office law registers are laws.
        for pat, lab in STRONG[:2]:
            if pat.search(text):
                return lab, "strong"
        if re.match(r"^p(360/[1-6]\.|347/law|349/law|358/detaillaw)", c):
            return "law_text", "cluster"
        return "green_office", "cluster"
    if c.startswith("p1/meeting_bookshelf"):
        if R(r"กระทู้ถาม").search(text):
            return "interpellation", "strong"
        if R(r"รายงาน ?(ผล)?การพิจารณา ?ศึกษา|เรื่องที่คณะกรรมาธิการ ?พิจารณาเสร็จแล้ว").search(text):
            return "committee_report", "content"
        if R(r"เรื่องด่วน.{0,20}ร่างพระราชบัญญัติ").search(text):
            return "bill_documents", "content"
        return "plenary_sitting", "cluster"
    for pat, lab in STRONG:
        if pat.search(text):
            return lab, "strong"
    for cpat, tpat, lab in MIXED:
        if cpat.search(c) and tpat.search(text):
            return lab, "mixed"
    for pat, lab in CLUSTER:
        if pat.search(c):
            return lab, "cluster"
    in_committee = not re.match(r"^p(1|2|13|49|93|185)/", c)
    if NEWS_FIRST.search(r["filename"]) or NEWS_FIRST.search(r["snippet"][:80]) or (
            in_committee and NEWS_SECTION.search(c.split("/", 1)[1])):
        return "committee_news", "content"
    for pat, lab in WEAK:
        if pat.search(text):
            return lab, "content"
    if R(r"ผลการเลือกตำแหน่ง").search(text):
        return "committee_activity", "content"
    if c.startswith("p93/") and R(r"สถิติ").search(c + text):
        return "governance_transparency", "cluster"  # legal-information service statistics
    if not re.match(r"^p(1|2|13|49|93|185)/", c):
        # Committee and subcommittee pages: a meeting write-up is press news; else go by section name.
        if R(r"รายงาน|สรุปผลการศึกษา").search(text[:120]):
            return "committee_report", "fallback"
        if R(r"ได้มีการประชุม|ประชุม ?ครั้งที่|^.{0,40}เมื่อวัน|^.{0,60}ฉบับที่ ?[0-9๐-๙]+ ?/|กมธ\.").search(text.split(" | ", 1)[-1]):
            return "committee_news", "fallback"
        section = c.split("/", 1)[1]
        if R(r"รายงาน|บทความ").search(section):
            return "committee_report", "fallback"
        if R(r"เดินทาง|tour|articles|สัมมนา|คำสั่ง|ข้อมูลประกอบ|เอกสารนำเสนอ").search(section):
            return "committee_activity", "fallback"
        return "committee_news", "fallback"
    if R(r"เรื่องที่เสนอใหม่|ระเบียบวาระ").search(text):
        return "plenary_sitting", "content"
    return "unlabeled", "-"


def language(snippet: str) -> str:
    s = snippet[:300]
    thai = len(re.findall(r"[฀-๿]", s))
    if thai >= 0.2 * max(len(s.replace(" ", "")), 1):
        return "th"
    if re.search(r"[一-鿿]", s):
        return "zh"
    if re.search(r"[éèàçêù]|\b(le|la|les|du|des)\b", s):
        return "fr"
    return "en" if s.strip() else "unknown"


if __name__ == "__main__":
    rows = list(csv.DictReader(open("features.tsv", encoding="utf-8"), delimiter="\t"))
    out, by_label, unl = [], Counter(), Counter()
    for r in rows:
        lab, how = label(r)
        r.update(doc_class=lab, collection=GROUP.get(lab, "?"), rule=how, language=language(r["snippet"]))
        out.append(r)
        by_label[lab] += 1
        if lab == "unlabeled":
            unl[r["cluster"]] += 1
    with open("labels_all.tsv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, list(out[0].keys()), delimiter="\t")
        w.writeheader()
        w.writerows(out)
    total = len(out)
    for lab, n in sorted(by_label.items(), key=lambda x: -x[1]):
        print(f"{n:6} {100*n/total:5.1f}%  {GROUP.get(lab,'?'):15} {lab}")
    print("\nunlabeled clusters:", len(unl))
    for c, n in unl.most_common(int(sys.argv[1]) if len(sys.argv) > 1 else 40):
        print(f"  {n:4} {c}")
