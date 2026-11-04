# Document labels for the Senate collection

Every PDF in the collection (28,145) has one `doc_class` and one `collection` (the group it
belongs to), in `doc_labels.csv`. Join it to `ocr_out/mapping.csv` or the OCR text files on
`doc_id`. Use `doc_class` as metadata; use `collection` to split the documents into separate
RAG indexes.

## How the labels were made

1. **Grouped** every document by where the site published it: portal and section (613 groups).
2. **Read a stratified 11% sample** (3,118 documents, every group represented; up to 25 per group
   read: filename, section and the opening of page 1). The label set below came out of that
   reading, not from the site's section names, which often mislead: the section "PR" holds Green
   Office evidence, "ร่างกฎหมายน่ารู้" mixes radio scripts, cabinet summaries and articles.
3. **Wrote rules** (`labeler.py`), in priority order: unmistakable content (e.g. `กระทู้ถาม`,
   `ประกาศผู้ชนะการเสนอราคา`, `รายงานของคณะกรรมาธิการวิสามัญพิจารณาร่าง…`) → the section the file
   came from → weaker content cues → a fallback for committee pages.
4. **Verified twice** on random documents the rules had labelled: 236 in round 1 (~95% right),
   fixed the causes, then 198 fresh ones in round 2 (~95% right; ~98% weighted by class size,
   because the large classes were all correct). The press-release confusion found in round 2 was
   fixed afterwards.

## The labels

| collection | doc_class | ชื่อไทย | what it is | docs |
|---|---|---|---|---|
| **A_law** | `law_text` | ตัวบทกฎหมาย | Acts, constitutions, decrees, ministerial rules, rules of procedure (ข้อบังคับการประชุม), ระเบียบ/กฎ ก.ร., law compilations and indexes, Royal Gazette texts | 684 |
| | `court_decision` | คำวินิจฉัย/คำพิพากษา | Constitutional Court decisions and orders, court judgment summaries (จุลนิติ "สรุปคำพิพากษา") | 264 |
| | `legal_knowledge` | ความรู้กฎหมาย | Legal explainer columns (เกร็ดกฎหมายน่ารู้, สารพันปัญหากฎหมาย, แนะนำกฎหมายใหม่, ร่างกฎหมายที่น่าสนใจ, การปฏิรูปกฎหมาย, บทวิเคราะห์รัฐธรรมนูญ…), radio scripts "หนึ่งสัปดาห์กับวุฒิสภา", summaries of cabinet resolutions on legislation | 1,718 |
| **B_legislative** | `bill_documents` | เอกสารร่างกฎหมาย | Everything about one bill: bill texts sent from the House, bill briefings (document.senate.go.th), ad hoc committee scrutiny reports, amendment and comparison tables, legislative history and debate summaries (ความเป็นมาและประเด็นอภิปราย…), Act + legislative history (เอกสาร_กป) | 1,566 |
| | `plenary_sitting` | การประชุมวุฒิสภา | Plenary agenda papers (meeting shelf), meeting notices, minutes, resolution summaries, verbatim records, reports on ปรึกษาหารือ | 1,399 |
| | `interpellation` | กระทู้ถาม | Senators' questions to government, the Academic Office's briefs on them | 327 |
| | `committee_report` | รายงานการพิจารณาศึกษา | Committee and subcommittee study reports, policy briefs, reform/national-strategy follow-up reports, including those tabled in plenary | 603 |
| | `committee_news` | เอกสารข่าวคณะกรรมาธิการ | Press releases on committee and subcommittee meetings | 2,090 |
| | `committee_activity` | การดำเนินงานคณะกรรมาธิการ | Trip and site-visit reports, seminar reports, committee agendas, document-request lists, member lists, appointment orders, performance summaries | 436 |
| **C_knowledge** | `academic_brief` | เอกสารวิชาการ (สำนักวิชาการ) | Academic Office briefs: reviews of agencies' annual and financial reports, follow-ups on committee reports | 383 |
| | `academic_article` | บทความ/วารสารวิชาการ | Journal articles, interviews, seminar summaries, whole journal issues (จุลนิติ, เข็มทิศกฎหมาย), research summaries, theses | 808 |
| | `senate_publication` | สิ่งพิมพ์เผยแพร่ | Newsletters and magazines (สารวุฒิสภา, จันทรา, บอกเล่าข่าว), yearly Senate performance summaries, lists of senators, youth encyclopedia, foreign-language information | 223 |
| | `news_digest` | สรุปข่าวสื่อมวลชน | Daily media summaries: health news (most), energy news | 1,253 |
| | `external_reference` | เอกสารจากหน่วยงานภายนอก | Written by other agencies: national strategy and master plans, NESDC plans, HAII river-basin reports, agencies' slide decks to committees, Royal Institute rules | 112 |
| **D_admin** | `procurement` | จัดซื้อจัดจ้าง | Award notices, bid invitations, reference prices, procurement plans and monthly summaries, disposal auctions | 5,783 |
| | `budget_finance` | งบประมาณและการเงิน | Budget disbursement reports, auditor's and financial statements, budget documents | 146 |
| | `strategy_performance` | แผนและผลการดำเนินงาน | Strategic and action plans, performance agreements and reports, risk/BCP plans | 154 |
| | `hr` | บริหารทรัพยากรบุคคล | HR strategy and plans, career paths, recruitment and selection notices, training lists, discipline | 117 |
| | `governance_transparency` | ธรรมาภิบาลและข้อมูลข่าวสาร | ITA/OIT disclosures, anti-corruption measures, ethics, complaint handling, official-information statistics and manuals | 346 |
| | `internal_km` | คู่มือและการจัดการความรู้ | KM documents, work and service manuals, IT user guides, innovation presentations | 221 |
| | `general_notice` | ประกาศทั่วไป | Secretariat announcements: space leases, courses, network projects | 42 |
| **E_green_office** | `green_office` | หลักฐานสำนักงานสีเขียว | Evidence for the Green Office assessment, per bureau and หมวด 1-7: energy, water, paper and waste logs, maintenance checklists, policies, campaigns, contractor evaluations | 9,448 |
| **F_exclude** | `form_template` | แบบฟอร์ม | Blank forms: leave, motions, amendments, asset declaration | 20 |
| | `other` | อื่น ๆ | Junk (2 test files reading "Pentest") | 2 |

Other column in `doc_labels.csv`: `language` (th 27,992, en 130, fr 12, zh 8, unknown 3).
`rule` says how the label was decided (`strong`, `cluster`, `mixed`, `content`, `fallback`);
`fallback` and `content` are the least certain (about 3,000 documents).

## Suggested RAG split

| index | collections | why |
|---|---|---|
| Legislative & legal | A_law + B_legislative except `committee_news` | The core: laws, bills, plenary and committee work. Questions like "what does the Act say", "how did the Senate debate bill X", "what did committee Y recommend". |
| Committee news | `committee_news`, `committee_activity` | Short, date-heavy, numerous; they drown out reports if mixed in. |
| Knowledge & publications | C_knowledge except `news_digest` | Analysis and background. |
| Media digest | `news_digest` | Daily and time-bound; best kept apart and filtered by date. |
| Administration | D_admin | Mostly lookups (who won a tender, what the budget was); `procurement` is better served as a table than by RAG. |
| Leave out | E_green_office, F_exclude | 34% of the documents but internal compliance evidence; worth keeping out of general search. |

## Known weak spots

- **Committee report vs plenary agenda:** a committee report tabled in plenary can be labelled
  either way; an agenda cover page that only names the report is labelled `plenary_sitting`.
- **Committee news vs activity:** a press release about a site visit sometimes lands in activity.
- **Stray files in a section:** a few memos sit in sections they don't belong to (e.g. an HR memo
  under ร่างกฎหมายน่ารู้, a legal opinion on the publication shelf) and take the section's label.
- **One label per document**, from the first pages: a compiled volume gets the label of what it
  opens with.

## Re-running

On pgx in `~/senate`: `python3 classify/build_sample.py` (features and sample from the OCR state),
then `cd classify && python3 labeler.py` writes `labels_all.tsv`; `doc_labels.csv` is the export.
