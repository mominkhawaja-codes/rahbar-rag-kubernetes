# Corpus

Source: FAST-NUCES public website · Documents: 9 (7 PDF, 2 HTML) · Pages: 281 (279 PDF pages + 2 web pages)

Fetched with `corpus/fetch.py` on 8 November 2026 from the URLs in `corpus/sources.yaml`.
`corpus/MANIFEST.md` records each URL, size and SHA-256 (`python corpus/fetch.py --verify`
re-checks them). The files themselves are never committed (`corpus/raw/` is gitignored).

| # | File | Pages | doc_version | superseded | Text layer |
| --- | --- | --- | --- | --- | --- |
| 1 | `undergraduate-academic-rules-aug-2023.pdf` | 26 | 2023-08 | **yes** | 2 of 26 pages; 24 scanned |
| 2 | `academic-rules-handbook-jun-2026.pdf` | 58 | 2026-06 | no | yes (5 blank/cover pages) |
| 3 | `prospectus-2026-academic-rules-and-regulations.pdf` | 5 | 2026 | no | yes, 3 columns |
| 4 | `student-handbook-jun-2026.pdf` | 64 | 2026-06 | no | yes, 2 columns (4 blank) |
| 5 | `annual-report-2019-20.pdf` | 33 | 2019-20 | no | yes |
| 6 | `annual-report-2023-24.pdf` | 85 | 2023-24 | no | yes |
| 7 | `fee-structure-page.html` | 1 | 2026-27 | no | HTML |
| 8 | `grading-page.html` | 1 | 2026 | no | HTML |
| 9 | `faculty-entrepreneurship-and-technical-start-ups-policy.pdf` | 8 | 2022-05 | no | **none: image-only** |

| # | Required property | Document(s) that provide it | Evidence or notes |
| --- | --- | --- | --- |
| 1 | At least 6 documents, 40 pages | All nine | 9 documents, 281 pages. Page counts from `pypdf` (table above). |
| 2 | Two versions of one policy | #1 Undergraduate Academic Rules (Aug 2023) is superseded by #2 Academic Rules Handbook (Jun 2026) | `corpus/sources.yaml` sets `superseded: true` on #1, `fetch.py` copies it into `corpus/raw/_index.json`, and the indexer writes `doc_version` and `superseded` into every chunk's metadata. The retriever queries with `where={"superseded": False}` unless the question explicitly asks about an older version (`api/app/retrieval/chroma.py`, `wants_history`). The 2026 handbook says on its own clauses which rules apply now (for example 5.4: minimum CGPA 2.00). #3, the 2026 prospectus, states the same rules with different clause numbers (attendance is clause 1 there, 1.21 in the handbook) and is **not** superseded: it is a second, current wording. |
| 3 | Scanned, image-only PDF | #9 Faculty Entrepreneurship policy (all 8 pages) and #1 the 2023 rules (24 of 26 pages) | `pdffonts` is not installed on our Windows machines, so we checked the same thing with `pypdf`: #9 has **0 font resources and 8 image XObjects on 8 pages**, and no extractable text on any page. #1 has 3 font resources (used only by the cover and one inner page) and 92 images, and 24 pages yield no text. Both go through Tesseract OCR in the indexer image (`indexer/pipeline/extract.py`, `ocr_pdf_pages`; `tesseract-ocr` and `poppler-utils` in `indexer/Dockerfile`), and each OCR'd page is marked `ocr=True`. |
| 4 | A table with real information | #2 handbook clause 2.3 (grade points) and 2.2 (letter-grade interpretation); #7 fee page (tuition per credit hour, miscellaneous fees, refund timeline); #8 grading page; #4 library loan table; #5/#6 publication counts by campus | Tables are written as Markdown pipe tables and kept whole in one chunk, or split by rows with the header repeated. Before/after example in `docs/PIPELINE.md`. |
| 5 | Long definitions | #2 chapter 10, "Policy on Generative AI Based Tools" (pages 40-55: acceptable and unacceptable uses, assessments); #2 chapter 8, Academic Honesty (8.1-8.13, definitions of plagiarism, cheating and other violations); #4 Code of Conduct | These sections run over several pages without clause numbers, so they test sentence-level splitting with overlap rather than clause-level chunking. |
| 6 | A deliberately absent topic | Hostel accommodation: how to apply, hostel rules, hostel fees | We left the prospectus section "Transport and Hostel Facilities" out of `sources.yaml` on purpose. What remains only *mentions* hostels: the annual reports say the Chiniot-Faisalabad campus offers boys' and girls' hostels, and another campus description mentions "a hostel for boys". No document says how to apply, what the rules are, or what a room costs, and the fee page has no hostel row (checked by searching every extracted page). A new student would plausibly ask, and a model would happily invent a fee, which is exactly why `golden.yaml` q17 and q18 must be declined. |
| 7 | Exact-term content | #2 clause numbers (1.21 attendance 100% with up to 20% condonable; 5.4 minimum CGPA 2.00; 7.3 Rector's List SGPA 4.00 with 30 credit hours; 7.6 Dean's List 3.50-3.99); #7 fees (Rs. 12,000 per credit hour, admission fee Rs 30,000, security deposit Rs 20,000); #2 clause 2.3 grade points | Golden questions q02, q11 and q12 are exact-term questions. Clause numbers survive cleaning and become part of every chunk's `section`, for example `Attendance Requirements [1.21-1.22]`. |

## What we checked by hand, and what still needs checking

- Every `expect` string in `eval/golden.yaml` was read in the extracted text of the named
  document (indexer dry run, 8 Nov 2026).
- The 2023 rules (#1) are scanned. Its text layer confirms the title page ("UNDERGRADUATE
  ACADEMIC RULES AND REGULATIONS 2023"), but its clauses can only be read after OCR, which
  runs in the indexer image (Tesseract is not installed on our laptops). **After the first
  real index build, read the OCR'd text of #1 and record here which numbers differ from
  2026** (attendance, warning threshold, repeat policy) so the version questions can name them.
- #5 and #6 contain the same campus descriptions word for word. That duplication is real,
  not an extraction error. It lowers the precision of campus questions, since both copies
  are retrieved.
