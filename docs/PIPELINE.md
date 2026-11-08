# Indexing and retrieval pipeline

Code: `indexer/pipeline/{extract,clean,chunk,embed,store}.py`, run by
`python -m pipeline.run` (the Job) or `python -m pipeline.run --dry-run` (local, no
network: extract, clean and chunk only, then print statistics). All numbers below marked
*measured* came from dry runs on 8 Nov 2026 over the nine documents in `docs/CORPUS.md`.

## How the documents reach the Job

They are baked into the indexer image. `make stage-corpus` (run by `make images`) copies
`corpus/raw/*` into `indexer/corpus/` and the CI fixtures into `indexer/fixtures/`, both
gitignored, and the Dockerfile copies them to `/corpus` and `/fixtures`. `CORPUS_DIR`
selects one.

- **Why:** the image *is* the corpus version. An index can always be rebuilt from an image
  tag, the Job needs no network access to the university site (forty teams share it), and
  there is no extra PVC to populate by hand.
- **Cost:** changing a document means rebuilding the indexer image, and the image is about
  35 MB larger. Images pushed by CD are built in GitHub Actions, where `corpus/raw/` does
  not exist, so they contain only the fictional fixtures: the university's PDFs are never
  republished on Docker Hub.

## Extraction

`extract.py` turns each document into page texts with two kinds of markup that the later
stages rely on: headings as `## ...` lines, and tables as Markdown pipe tables.

- **PDF:** `pdfplumber`, page by page. Three problems found on this corpus:
  1. **Off-page text.** The 2026 handbook's pages also carry the *neighbouring* page's text
     at negative x coordinates. Plain `extract_text()` interleaved two pages line by line
     (clause 4.5 appeared between 4.12 and 4.13). Every page is now cropped to its own
     bounding box (`page.within_bbox(page.bbox)`).
  2. **Columns.** The prospectus has three columns and the student handbook two. Gutters
     are found from word positions (`find_columns`): a vertical band crossed by at most 3%
     of the words. The cut goes at the band's emptiest point, because hanging clause
     numbers ("4. Semester Course Load") sit inside the band. A "column" with under 8% of
     the words is merged into its neighbour.
  3. **Headings** are lines whose font size is at least 1.15 times the page's body size
     (the handbook uses 18 pt for chapters, 12 pt for sub-headings, 10 pt for text).
- **OCR:** a page with fewer than 25 visible characters is rendered at 300 dpi
  (`pdf2image`/poppler) and read by Tesseract (`pytesseract`), then marked `ocr=True`.
  Needed for all 8 pages of the start-ups policy and 24 of 26 pages of the 2023 rules. If
  OCR fails, the Job fails (`ExtractionError`) rather than silently indexing less.
- **HTML:** a small `html.parser` subclass keeps `h1`-`h6`, paragraphs, list items and
  `<table>`s, and drops `header`, `nav`, `footer`, `script`, `style`, forms and the
  breadcrumb. Content starts at the first heading, so the site-wide menu (every campus,
  every department) never reaches a chunk.
- **Markdown/text:** read as is, without the front-matter block.

**Before and after, a table** (handbook p. 12, clause 2.3). Plain `pdfplumber` text:

```
NATIONAL UNIVERSITY OF COMPUTER AND EMERGING SCIENCES
Grade Points
2.3 The points for each letter grade are given as follows:
Grade Points Grade Points
A+ 4.00 C+ 2.33
A 4.00 C 2.00
...
```

What our extract stage writes. The rows keep their column names, and the running header
is later removed by `clean`:

```
## Grade Points
2.3 The points for each letter grade are given as follows:

| Grade | Points | Grade | Points |
| --- | --- | --- | --- |
| A+ | 4.00 | C+ | 2.33 |
| A | 4.00 | C | 2.00 |
| A- | 3.67 | C- | 1.67 |
| B+ | 3.33 | D+ | 1.33 |
| B | 3.00 | D | 1.00 |
| B- | 2.67 | F | 0.00 |
```

pdfplumber reports decorative frames and text boxes as tables too. `is_real_table` keeps
only tables with at least 2 rows and 2 columns, at least half the cells filled, and cells
averaging under 60 characters.

**What could not be recovered:**

- The prospectus's grade lists are laid out with spaces, not ruled lines, so they stay
  text ("A+ 4.00 C+ 2.33").
- A few prospectus lines are set letter-spaced ("S e m e s t e r s t a r t s i n") and
  stay that way.
- The OCR quality of the two scanned documents is not yet measured: it has to be checked
  after the first index build in the cluster, because Tesseract is not installed on our
  laptops.

## Cleaning

`clean.py`, in this order:

1. Unicode NFKC; curly quotes to straight ones; a replacement character between letters
   becomes an apostrophe ("Rector�s" → "Rector's").
2. **Running headers and footers** are removed: a line that appears on at least half the
   pages (and on 3 or more) after its page-number tokens are dropped. This removes
   "NATIONAL UNIVERSITY OF COMPUTER AND EMERGING SCIENCES" and "Undergraduate Academic
   Rules and Regulations 2026 15". Our first version masked *every* digit, which made
   "5.1 Rule ..." and "5.2 Rule ..." the same line and deleted real clauses. A unit test
   now guards that.
3. Page numbers go, including letter-spaced ones ("1 0", "I V"), and so do
   table-of-contents lines with dot leaders.
4. Words hyphenated across a line break are joined again.
5. Runs of spaces and blank lines are squeezed.

**Deliberately not stripped:** clause numbers at the start of a line ("5.4 Minimum CGPA
..."). They look like list noise, but chunking splits on them, every chunk's `section`
records them, and the exact-term questions ask for them. Heading markers and table pipes
are kept for the same reason.

## Chunking

`chunk.py`. Unit: a **token is a whitespace-separated word**.

Structure first: a chunk never crosses a heading. Inside a section, units are clauses
(`5.4 ...`), paragraphs or whole tables, packed greedily up to the size. The next chunk
starts with the last `overlap` words of the previous one. A unit larger than the size is
split on sentences, then on words, and a table on rows with its header repeated. Each
chunk's `section` is its heading plus the clauses it holds:
`Academic Warning/Probation [5.4-5.7]`.

**Shipped: 400 words, overlap 50.** Storage amplification (stored words / original
words, headings excluded since they are stored as metadata) is **1.021** at that setting.

| Size (tokens) | Overlap (tokens) | recall@5 | Chunks stored | Amplification |
| --- | --- | --- | --- | --- |
| **400** | **50** | **0.941** | 612 | 1.028 |
| 250 | 40 | 0.941 | 671 | 1.058 |
| 150 | 30 | 0.941 | 830 | 1.115 |

*Measured* by the `eval-corpus` workflow, run
[42498760820](https://github.com/mominkhawaja-codes/rahbar-rag-kubernetes/actions/runs/42498760820)
(commit 5fd631a): the real corpus indexed in a kind cluster with OCR (275 pages, 35 of them
OCR'd, 64,273 source words), `nomic-embed-text`, and the 19-question `eval/golden.yaml`.
Recall is identical at all three sizes: 16 of the 17 answerable questions find their clause
in the top 5, and the same one misses each time (q02, "What does clause 1.21 require?"; the
clause number alone is a weak signal for a dense embedding). Smaller chunks cost 10% and 37%
more vectors for no recall gain on this golden set, so **400/50 is kept**. With every
setting at the same recall, the golden set cannot separate them; a harder set, with questions
whose answers span two clauses, is what would.

Amplification stays low even with overlap because most clauses are far shorter than a
chunk: overlap is only added between chunks *inside one section*, and most sections fit
in one chunk.

## Embedding

- **Model:** `nomic-embed-text` through Ollama `POST /api/embed`, batches of 32, three
  attempts with backoff. Its vectors have 768 dimensions; the indexer records the
  dimension it actually received.
- **Prefixes:** documents are embedded as
  `search_document: <file name words> | <section>\n<text>`, questions as
  `search_query: <question>` (nomic's task prefixes). The heading goes into the
  embedding because the clause text often omits the subject ("5.4 Minimum CGPA ...") that
  the heading names ("Academic Warning").
- **The pin:** `EMBED_MODEL` lives in one ConfigMap key, read by both the indexer and the
  API. The indexer writes `embed_model` and `dimension` into the collection metadata
  (`store.py`). The API checks both (`api/app/retrieval/chroma.py`, `pin_problem`):
  1. at start-up, a mismatch is logged as an error;
  2. on every `/readyz`, the pod reports not ready with `embed_pin` named;
  3. on every query, `/api/ask` answers 503 instead of searching with incompatible vectors.

## Semantic cache

`api/app/services/cache.py` (Redis on the cluster, ADR 0003). A hit is a stored question
whose embedding has cosine similarity ≥ `CACHE_THRESHOLD` with the new one. An exact
normalised match is found first, by key. Entries live under a generation made of the
index id, the embedding model, the provider and model, the prompt version and the floor,
so a re-index starts a fresh cache. Never cached: abstentions, fallbacks, answers without
a valid citation, and follow-ups ("what about MS?").

**Thresholds:** 0.95 is the shipped default, chosen before measurement because a false hit
returns a *confident wrong answer*, while a miss only costs latency. To confirm or lower
it, try 0.90, 0.93 and 0.95 on paraphrase pairs from the golden set, and look for the
classic false hit: questions differing in one number or degree level ("BS fee per credit
hour" vs "MS fee per credit hour"). **Not measured yet**: record the pairs and
similarities here when the cluster runs.

## Evaluation method

`eval/run_eval.py`, tested in `eval/tests/test_run_eval.py`.

- **recall@k** matches golden `(source, section)` pairs against the top-k `retrieved`
  list: same source file, and a section that matches by `section_matches`:
  - a golden **clause number** ("5.4") matches a retrieved section whose bracketed clause
    range contains it numerically (`[5.4-5.7]` contains 5.4; `[5.8-5.11]` contains 5.10
    but not 5.12);
  - a golden **heading** ("1. Attendance") matches a retrieved section equal to it or
    starting with it ("1. Attendance [1.1-1.2]").

  We match on sections, not chunk ids, because chunk ids change whenever the chunk size
  changes, and the three configurations above must be scored on the same golden set.
- **Citation validity:** cited `(source, section)` pairs that appear exactly in
  `retrieved`, as a fraction of all citations.
- **Abstention correctness:** must-abstain questions answered with
  `answered_from_context: false`. We also report the false-abstention rate on answerable
  questions, and how often the `expect` string appears in the answer. Both are reported,
  not gated.
- **Gate** (unchanged from the kit): recall@5 at most 0.05 below the baseline, citation
  validity ≥ 0.95, abstention correctness = 1.0. Exit code 1 fails CI.
