# ADR 0002: Structure-aware chunking on clauses, 400 words with 50 overlap

## Status
Accepted · 8 Nov 2026 (size to be re-confirmed by the recall@5 measurements in `docs/PIPELINE.md`)

## Context
Most questions are answered by one numbered clause ("5.4 Minimum CGPA required to avoid
academic warning is 2.00…"), and the golden set and citations name clauses. But the
corpus also has tables that only make sense with their header row, multi-page policies
without clause numbers (the Generative AI policy), and annual-report prose. Fixed-size
windows would cut clauses in half and separate table rows from their column names.

## Decision
`indexer/pipeline/chunk.py`:
- split at headings first (font-size headings from PDFs, `<h2>` from HTML, Markdown `#`,
  numbered chapter titles), and never let a chunk cross one;
- inside a section, pack whole clauses, paragraphs and tables into chunks of at most
  **400 whitespace words**, with **50 words** of overlap between neighbours in the same
  section;
- split an oversized unit on sentences, then on words; split a table on rows and repeat
  its header in every piece;
- label every chunk `Heading [first-last clause]`, which is what citations and recall@k
  match on.

## Options considered
- **Fixed windows** (e.g. 300 words, 50 overlap, ignoring structure): simplest, but cuts
  clauses and tables, and the section label becomes a guess.
- **One chunk per clause:** precise labels, but many clauses are a single line ("4.22 Fee
  is not refunded for a withdrawn course."). Alone, those lines lack the context their
  neighbours give, and the chunk count triples.
- **Smaller packing (150/30 or 250/40):** measured at 760 and 632 chunks against 588, with
  amplification 1.100 and 1.047 against 1.021. Smaller chunks rank more precisely but give
  the model less context per passage. The choice between them is the recall@5 column in
  `docs/PIPELINE.md`, still to be measured on the cluster.

## Consequences
- Easier: citations name real clauses, tables arrive intact, and the overlap costs only
  2% storage because most sections fit in one chunk.
- Harder: quality now depends on heading and clause detection. OCR'd pages have no font
  sizes, so they fall back on clause numbers and numbered chapter titles. A sentence that
  starts with a number ("3.99 would be included…") is not taken as a clause, because a
  clause must continue with a capital letter (there is a test for this).
- Watch: chunks labelled `Page N` mean no heading was detected for them. Their count in
  `--dry-run --show` is a quick quality signal after any extraction change.
