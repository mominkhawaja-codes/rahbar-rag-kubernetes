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

## Verification

`verify.py` runs after clean and before chunking (Assignment 03, Part A1; paper 4.2.2).
Each document gets a pass or fail with a reason for four checks, and one outcome:
`index` (all pass), `quarantine` (quality, completeness or uniqueness failed: the file is
moved to `QUARANTINE_DIR` and is not indexed) or `supersede` (only recency failed: a newer
version exists, so the document is kept and indexed with `superseded=True`). Each run writes
`verification-<UTC timestamp>.json` to `REPORT_DIR`. `python -m pipeline.run --verify-only`
runs this stage alone (`--no-quarantine` reports without moving files).

| Check | Fails when | Threshold |
| --- | --- | --- |
| Quality | extraction raised an error; fewer than `MIN_CHARS` of text; replacement or control characters above 2 percent; letters below 40 percent of non-space characters | `MIN_CHARS=200` |
| Completeness | title, version/date or page count missing; the file's sha256 or size differs from `_index.json`; a PDF without `%%EOF`; HTML without `</html>` | none |
| Recency | an older version of a document that shares an explicit `doc_id` with a newer valid one | none |
| Uniqueness | same sha256 of the normalised text as an earlier document; or document-embedding cosine at or above the threshold with a document that is not a version of it | `NEAR_DUP_THRESHOLD=0.98` |

Nothing is invented: a title or version the source does not state is reported as missing.
Titles come from `_index.json`, front matter, or a Markdown file's own first heading.

**Weak hints are warnings.** Text that ends without punctuation is a hint of truncation, but
many valid files end that way, so it goes into the verdict's `warnings` and never fails a
document by itself. Only structural evidence (hash or size mismatch, no `%%EOF`, no
`</html>`) fails completeness.

**Current and superseded documents are told apart in the report.** Every verdict has
`version_status` (`current` or `superseded`) and `superseded_by` (the newer file, when one
was found). A source that marks itself `superseded: true` is `superseded` with no
`superseded_by`. The summary counts `indexed_versions` (current against superseded, among
documents that are not quarantined) and `documents_with_warnings`. Archiving and restoring
superseded versions belong to A2.

**A newer version is not a near-duplicate.** Two documents are treated as versions, and so
skip the near-duplicate test, when they share a `doc_id`, or when their versions differ and
the source marks one as superseded. Without either signal, no versions are guessed (the
2019-20 and 2023-24 annual reports are different documents).

### Calibration (27 Nov 2026, `nomic-embed-text`, real Ollama)

A document is embedded as the mean of up to six evenly spaced 1,500-character windows.
Embedding only the first page would score two handbooks with the same cover text as copies.

Two measurements exist and they are kept apart:

- **Final (OCR on):** the complete Batch A verification with Poppler 26.09.0 and OCR enabled.
  Report: `eval/results/verification/batch-a/verification-20261127T121224Z.json` (log beside
  it). Nine documents, 275 pages. All nine passed; nothing was quarantined.
- **Earlier (OCR off):** a first pass on the same nine files before Poppler was installed.
  Four PDFs contributed less text or failed extraction. Its report stayed in a temporary scratch folder outside the repository, so its
  numbers are not backed by a committed file.

Batch A cosines. The report stores each document's nearest other document and the highest
score against earlier retained documents, not a full matrix, so a pair appears only where one
of those fields recorded it; "not recorded" is not a zero.

| Pair (different documents) | Final, OCR on | Earlier, OCR off |
| --- | --- | --- |
| Student handbook vs academic rules handbook | **0.9576** (highest) | 0.9643 |
| Rules Aug 2023 vs prospectus 2026 | 0.9540 | not recorded |
| Annual report 2019-20 vs 2023-24 | 0.9475 | 0.9475 |
| Annual report 2019-20 vs student handbook | 0.9140 | not recorded |
| Prospectus vs academic rules handbook | 0.9016 | 0.9095 |
| Grading page vs prospectus | 0.8979 | 0.8979 |
| Faculty policy vs student handbook | 0.8510 | not extracted (scanned) |
| Fee page vs rules Aug 2023 | 0.8385 | not recorded |
| Fee page vs prospectus | 0.8289 | 0.8289 |

Edited-copy and version scores were measured earlier, on small documents that involve no OCR
(two HTML pages and the CI fixtures), so the OCR change does not affect them:

| Pair | Cosine |
| --- | --- |
| Grading page vs a lightly edited copy (6 numbers, 3 words, 1 sentence changed) | 0.9981 |
| Fee page vs the same kind of edit | 0.9930 |
| Fixture rules 2023 vs 2026 (two versions of one document) | 0.9907 |

My first threshold, 0.95, wrongly quarantined the student handbook in the OCR-off pass
(0.9643). **0.98** sits above the highest different-document pair (0.9576 with OCR, 0.9643
without) and below the lightest edited copy (0.993). The last row shows why versions must be
exempt: they score above the threshold.

Quality figures from the final run: letters were 92 to 96 percent of characters in the nine
PDFs and 88 percent in the two HTML pages (lowest 87.5 percent, the fee page); the shortest
document had 3,706 characters. In the earlier OCR-off pass the 2023 rules had only 221
characters (77.8 percent letters) and the faculty policy 0, which is what OCR fixed.

Limits of this calibration:

- Edited copies were generated by me from two small documents, not found in the wild.
  A heavier edit that scores between 0.96 and 0.98 is not caught.
- The margin to the highest different-document pair is small (0.9576 against 0.98). A new
  document from the same domain could land closer; Batch B will test this.
- `MIN_CHARS=200` and the 40 percent letter ratio are chosen limits, not fitted ones: every
  real document passes with room to spare, and no failing real document exists to calibrate
  against until Batch B.
- Reports written from now on carry `ocr_pages` and `ocr_pages_empty` per document. The
  Batch A report above predates them and does not have them. The formal ingestion of 27 Nov
  (`eval/results/a3-batch-validation/step01-batch-a-ingest/verification-*.json`) has them: 35
  OCR pages in total (2023 rules 23 of 25, rules handbook 2 of 55, student handbook 2 of 62,
  faculty policy 8 of 8), none left empty, matching the 35 in the Assignment 02 index.

## Incremental update and document lifecycle

`incremental.py` (Assignment 03, Part A2; paper 4.2.3 and 4.2.4). With verification on (the
default) the Job ingests incrementally; `VERIFY_ENABLED=false` keeps the Assignment 02 full
rebuild unchanged, and `--full-rebuild` forces it.

**State.** `index-state.json` in `STATE_DIR` records, for every indexed document: the stable
document id (the explicit `doc_id`, else the file name), the file sha256, the text hash, the
document vector, the version, the chunk ids and the status (`active` or `archived`). Chunks
carry `doc_id` and `content_hash` in their metadata. The file is written atomically and only
after the store has been checked.

**Full or incremental.** The first run, a missing collection, or a changed collection name,
embedding model, chunk size or overlap builds the whole index through the staging swap of
`store.py` (ADR 0001). Otherwise only what changed is touched:

| Case | What happens | Embeds |
| --- | --- | --- |
| Unchanged file (same sha256) | Not extracted, verified or embedded again. Only changed metadata is updated in place. | 0 chunks, 0 verification texts |
| Added file | Verified, chunked and embedded; chunks upserted. | its chunks |
| Modified file | Its chunks are upserted and its old chunks that no longer exist are deleted. Other documents are untouched. | its chunks |
| Deleted file | All chunks with that `source` are deleted, then counted per document. | 0 |
| Rejected by verification | Quarantined (A1) and never indexed. A known document replaced by a rejected version loses its chunks. | 0 |
| Superseded version | Chunks stay with `superseded=True`, which retrieval already excludes; a copy of the file goes to `ARCHIVE_DIR/<doc id>/<version>/`. | 0 |

Newly added files are compared against documents already accepted, never the other way
round, so a new arrival cannot displace an indexed document.

**Proof of no orphans.** After applying changes the Job counts chunks per document in the
store (`count_chunks`) against the state and stops with an error if any differ, or if the
store holds a source the state does not know. A failed earlier run that left stray chunks is
healed the same way on the next run.

**Archive and restore.** `python -m pipeline.run --restore FILE` makes an archived version
active and archives the other versions that share its `doc_id` (a rollback);
`--archive FILE` archives one document. While the chunks are still indexed both only flip
`superseded` on them, so nothing is embedded again, and both are locked: later runs keep the
choice until the file changes. Every archived version is also copied to `ARCHIVE_DIR` with a
`<file>.meta.json` sidecar (its title, version and `doc_id`). If the file has since left the
lake, its chunks were deleted with it; `--restore` then copies the archived file back into the
lake, lists it in `_index.json` when the lake has one, ingests it again (verified and
embedded like a new document) and makes it the active version. A document deleted while
active has no archive copy and cannot be restored; archive first.

**Cache invalidation.** The API's semantic cache keys on the collection's `index_id` (ADR
0003). Any run that changes what is indexed, including archive and restore, gives the
collection a new `index_id`; a run that changes nothing keeps it. `ChromaRetriever.index_id()`
reads the collection metadata on every call (one small request to Chroma per question), not
through the 30 second `METADATA_TTL` used for the pin check, so a cached answer is never served
after the index changed. The cost is that one request, including on cache hits.

**After an interrupted run.** The state file is written only after the store has been checked,
so a run that dies halfway leaves the old state and a store that is partly ahead of it. The
next run redoes the changes idempotently (upserts, deletes by source) and ends consistent;
until it does, a changed `index_id` may not yet have been published, so cached answers from
before the interrupted run can still be served.

**Counts.** Every run writes `ingest-<UTC timestamp>.json` to `REPORT_DIR` with the number
and names of documents added, changed, unchanged, deleted, rejected, newly archived and
restored, and the chunks embedded, upserted, deleted and metadata-updated, the verification
texts embedded, the orphans removed, the per-document chunk counts and the `index_id`.

**Collection metadata.** Updating a collection's metadata in Chroma drops the legacy
`hnsw:space` key from it. The distance function lives in the collection's configuration and
stays cosine (checked against a real Chroma 1.0.12 server: a vector scaled by two had
distance 0); the API's pin check reads only `embed_model` and `dimension`, which are kept.

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

`api/app/services/cache.py` (Redis on the cluster, ADR 0003) finds exact normalised
matches by key. Semantic reuse requires an explicitly recognised equivalent intent and
canonical embeddings with cosine similarity ≥ `CACHE_THRESHOLD` (0.95). The reviewed
academic-warning phrasings preserve the distinction between triggering a warning and
the minimum needed to avoid one. Other phrasings use exact matching. The generation
includes the index id, embedding model, provider and model, prompt version, cache policy
version and relevance floor. Abstentions, fallbacks, uncited answers and follow-ups
are excluded.

The expanded experiment found zero false hits with the guard at 0.95 across 32 labelled
pairs (30 lookups and two follow-up bypass checks). See ADR 0003 for the comparison.

**Original cosine-only measurement.** `eval/cache_threshold.py` embedded 25 labelled question pairs
(`eval/cache_pairs.yaml`, written from the corpus before measuring) with the API's own
`OllamaEmbedder`, i.e. `nomic-embed-text` with the `search_query:` prefix, and counts what
each threshold would do. Run `cache-threshold`
[42517013627](https://github.com/mominkhawaja-codes/rahbar-rag-kubernetes/actions/runs/42517013627)
(commit c4efade, Ollama 0.5.7, Redis 8.8.4). A false hit is a hit on a pair whose answers
differ (near misses, version pairs, must-abstain topics; 13 such pairs). The 2 follow-up
pairs are not measured: they never reach the cache.

| Threshold | True hits | False hits | Misses | False-hit rate | Pairs that hit falsely |
| --- | --- | --- | --- | --- | --- |
| 0.80 | 9 | 8 | 1 | 0.615 | n1 n2 n3 n6 n8 v1 v2 a2 |
| 0.85 | 7 | 7 | 3 | 0.538 | n2 n3 n6 n8 v1 v2 a2 |
| 0.88 | 7 | 7 | 3 | 0.538 | n2 n3 n6 n8 v1 v2 a2 |
| 0.90 | 7 | 4 | 3 | 0.308 | n2 n3 n6 v2 |
| 0.92 | 4 | 2 | 6 | 0.154 | n2 n6 |
| 0.93 | 3 | 1 | 7 | 0.077 | n2 |
| 0.94 | 2 | 0 | 8 | 0 | |
| **0.95** | **2** | **0** | **8** | **0** | |
| 0.96 to 0.99 | 2 | 0 | 8 | 0 | |

- **The false hit that rules out 0.93:** n2, "What SGPA is needed for the Dean's List of
  Honors?" vs "... for the Rector's List of Honors?", similarity **0.933**. One word apart,
  different answers (3.50 to 3.99 vs 4.00). At 0.92, n6 (B+ vs B- grade points, 0.928)
  also hits.
- **Chosen: 0.95.** The lecture's rule (lowest threshold with zero false hits) gives 0.94.
  0.95 produces exactly the same measured result on this set and keeps 0.017 rather than
  0.007 above the worst near miss, so we keep it.
- **What this costs:** at any zero-false-hit threshold only the 2 exact or normalised repeats
  hit; all 8 genuine paraphrases miss (their similarities are 0.735 to 0.939). With this
  model, same-answer paraphrases and different-answer near misses overlap, so a similarity
  threshold cannot separate them. The later above/below regression confirmed that even
  highly similar questions can need different answers.
- **Checked against a real `RedisCache`** at 0.94 and 0.95: 23 of 23 lookups match the
  computed decision; an entry stored under one index generation is a miss under the next
  (re-index invalidation); an abstention and both follow-ups are refused by
  `should_cache`/`is_followup`.
- **Limits:** 25 pairs is a small sample, and a margin of 0.017 is thin; a new phrasing could
  cross it. The pairs are ours, not real user traffic. Version pairs were measured without
  the generation key distinguishing current from historical questions: a history question
  that scored above 0.95 against a cached current-rules answer would be served that answer.
- **Reproduce:** run the `cache-threshold` workflow, or locally with Ollama and Redis:
  `OLLAMA_URL=http://localhost:11434 REDIS_URL=redis://localhost:6379/0 python eval/cache_threshold.py`.

## Generation temperature

**Shipped: `TEMPERATURE=0.2`, measured.** `eval/temperature_experiment.py` calls the real
`OllamaProvider` (`llama3.2:3b` on Ollama 0.5.7, CPU, the Answer schema as `format`, one
retry) at temperatures 0.0, 0.2 and 0.8. It uses 9 questions (1 ordinary, 2 version,
3 exact-term, 2 must-abstain, 1 injection) with 3 repeats each, so 27 generations per
temperature. One must-abstain question ("late fee for paying hostel charges") passes the
lexical evidence check, so the *model* has to decline it. Run
`llm-temperature`
[42870465199](https://github.com/mominkhawaja-codes/rahbar-rag-kubernetes/actions/runs/42870465199)
(commit f5c8953).

**Retrieval is held fixed.** Every question gets the same 6 passages, the current-version
sections of the fixture documents (the superseded 2023 edition excluded, as the retriever
would). The script asserts that each answerable question's relevant section is among them.
So every wrong answer below is a **generation** failure, not a retrieval failure.

| | 0.0 | **0.2** | 0.8 |
| --- | --- | --- | --- |
| Correct (expected fact in an answer marked from context) | 0.741 | 0.704 | 0.630 |
| Raw citation validity (before our citation check) | 0.875 | **0.941** | 0.722 |
| Mean distinct answers per question over 3 repeats | 1.44 | 1.78 | 2.56 |
| Questions whose answered/declined flag changed between repeats | 1 | 2 | 4 |
| Must-abstain correct (2 questions × 3) | 1.0 | 1.0 | 1.0 |
| Invalid JSON on the first attempt (retries) / fallbacks | 0 / 0 | 0 / 0 | 0 / 0 |
| Injection question scored correct | 0 / 3 | 0 / 3 | 0 / 3 |
| Median / max latency (s) | 13.4 / 92.8 | 12.6 / 17.8 | 14.4 / 23.9 |
| Mean tokens in / out (provider-reported) | 1065 / 77 | 1065 / 77 | 1065 / 84 |

**Why 0.2 is kept:**
- 0.8 is worse on everything: correctness, citation validity and stability. At 0.8 the
  ordinary warning question (f01) was wrong in all 3 repeats.
- Between 0.0 and 0.2 the correctness difference is one generation in 27 (the refund
  question x2, 2/3 vs 1/3), which this sample cannot separate from noise.
- 0.2 cited valid passages most often, and an invalid citation is what costs marks.
- 0.0 would be an equally defensible choice on these numbers. The 92.8 s maximum at 0.0 is
  most likely the model loading on the experiment's very first call; it was not repeated.

**What temperature does not fix, reported as measured:**
- **Right fact, wrong flag.** The dominant failure at every temperature: the model writes
  the correct fact but sets `answered_from_context: false`.
  - "If I repeat a course, is my grade capped?" → "No, your grade is not capped." (0/3
    at 0.0 and at 0.2).
  - The refund question → "50%".
  - In the pipeline such a reply becomes a declined answer with no citations (`_ground`
    in `services/answer.py`). The user sees the text, but it is not presented as grounded.
  - This is a 3B model's calibration, not a temperature effect.
- **The injection question is scored wrong at every temperature.** Where we saw the
  replies (temperature 0.0, all 3 repeats) the model *did not* obey the planted
  instruction: it answered "No, attendance is not optional now." But it flagged the answer
  as not from context and did not state the 80% figure, so our strict criterion counts it as
  incorrect. Replies at 0.2 and 0.8 for this question were not captured in the published
  summary.
- **The "correct" metric is a substring match** on the expected fact ("2.00", "80",
  "most recent", ...) and also requires the answer to be marked from context. It undercounts
  answers that are right but worded differently (f03 above).

**Limits:** 9 questions over the small fictional fixture corpus, 3 repeats, one model, CPU.
Not the real FAST-NUCES corpus, and not a hosted model. The temperature result is a
direction, not a significance test.

**Reproduce:** run the `llm-temperature` workflow, or locally with Ollama and the model:
`OLLAMA_URL=http://localhost:11434 python eval/temperature_experiment.py --out temperature.json`.

## Real-model evaluation on the real corpus

**Setup:**
- Workflow `rag-eval`, run
  [43068217988](https://github.com/mominkhawaja-codes/rahbar-rag-kubernetes/actions/runs/43068217988)
  (commit 5e05ff3).
- The real API process, Chroma 1.0.12, Redis 8.8.4, Ollama 0.5.7 with `llama3.2:3b`
  (temperature 0.2) and `nomic-embed-text`.
- The real FAST-NUCES corpus indexed by the indexer image: 612 chunks.
- The unchanged 19-question `eval/golden.yaml`.
- Semantic cache on at 0.95. 19 misses and 0 hits, so no answer leaked between questions.
- `LLM_TIMEOUT` 300 s. **These are real-corpus results**; the CI gate's fixture results
  use stub generation.

**Measured (baseline, before the Task 12 fixes):**

| | Ollama 8 GiB | Ollama 2 GiB |
| --- | --- | --- |
| Answerable correct (of 17) | **6** | 0 |
| Incorrect (answered, expected fact missing) | 2 | 0 |
| False abstentions | **9** (5 model flag false, 4 all citations rejected) | 17 (all fallbacks) |
| Must-abstain correctly declined (of 2) | 2 | 2 |
| recall@3 / recall@5 | 0.941 / 0.941 | 0.941 / 0.941 |
| Citation validity (citations kept in answers) | 1.0 (8 citations; 3 dropped) | n/a |
| HTTP errors / timeouts / fallbacks | 0 / 0 / 0 | 0 / 0 / 18 |
| Duration (19 questions) | 1111 s, 37 to 99 s per question | 76 s |

**Verified causes:**
- **2 GiB is too little for `llama3.2:3b`.** Docker reports `OOMKilled: true`, Ollama
  logs "llm server error", and every generation fails with HTTP 500. The API returns its
  safe fallback, never a 500. The CI overlay's Ollama limit is now 8Gi (commit 3dc697f).
  The earlier run's HTTP **503s were not reproduced**; their exact cause is still
  unverified.
- **False abstentions have two causes:**
  - the model writes a correct fact but sets `answered_from_context: false` (5);
  - every citation the model gave fails the citation check (4).

**Fixes in 3dc697f:**
- Prompt rule 7 defines when `answered_from_context` is true, including answers like "No"
  or "50%". Rules 1–2 are unchanged. `PROMPT_VERSION` is v4.
- Rejected citations are now logged next to the passages offered. Citation matching is
  unchanged.

**The after-fix evaluation was started (run 43074432806) but not recorded before
submission.** So there is no measured before/after comparison yet, and the fix's effect
on the 6/17 is unknown.

**Production finding, not changed:** answers took 37–99 s on a CPU runner. The ConfigMap's
`LLM_TIMEOUT=60` would time out about half of them, so it needs to fit the hardware.

## Isolated evaluation on Windows

For the stabilization check on 25 Nov 2026, a temporary Docker network supplied its own
ChromaDB and API. The API used the existing `nomic-embed-text` model on the Windows host,
and cached answers were disabled in the temporary API. The Kubernetes fixture deployment
and its storage were separate from both runs. The nine public downloads matched the
committed corpus manifest, which the `eval-corpus` workflow now checks after fetching.

The CI fixture golden set passed in this setup with the stub provider. All six questions
met the gate, with recall@3, recall@5, citation validity and abstention correctness of
100%, plus zero request errors. This checks the fixture evaluator and retrieval path.
The GitHub kind and image-scanning jobs run separately.

The public FAST run indexed 612 chunks from 275 extracted pages, including 35 OCR pages,
at the shipped chunk setting of 400 words with 50 words of overlap. Groq used
`openai/gpt-oss-20b` for generation. Recall@3 and recall@5 were both **94.12%**. The
clause 1.21 query (`q02`) remained the retrieval miss. All eight structured citations
matched retrieved passages.

The hosted run failed the abstention gate at **50%**. Groq returned HTTP 429 for 13 of
18 provider calls. One was `q17`, which returned a generation fallback and counted as an
error. The API declined `q18` before calling Groq. The burst provides incomplete
generation-quality evidence. For a hosted rerun, set `--min-request-interval` in seconds
according to the available provider quota and keep the temporary index for related runs.
The flag is optional and leaves the stub CI run and API behavior unchanged. The gated
baseline JSON remains unchanged.

The evaluator reports `inline_citation_answer_rate` separately from citation validity.
The former counts answers that contain at least one bracketed `[source / section]`
reference backed by their citation list and retrieved passages. A positive result alone
does not establish that every factual sentence is cited. The fixture stub returned
structured citations in five responses and no bracketed inline references. The four
grounded answers in that Groq run also had zero bracketed inline references.

Prompt v6 uses one factual sentence per line with numbered passage markers. The API
resolves those markers to canonical `[source / section]` references after the existing
source and section validation. A single unmarked sentence can use its structured
references when every supplied citation passes validation. Multi-sentence answers
require explicit verified markers. The structured Sources list is still available.
The prompt version changes the cache namespace so cached answers follow the same format.

The paced Groq run on 26 Nov 2026 passed the required gates at a 31-second minimum
request interval. Recall@5 was 94.12%, both required abstentions passed, and all 29
emitted citations were valid. All 14 grounded answers had inline citations. Two
requests failed inline citation validation (`q13`, `q19`). Clause 1.21 (`q02`) remained
the only top-five retrieval miss. The expected text matched in 11 of 17 answerable
questions. Three other grounded answers had no expected-text match.
The full measured result is in [`eval/results.json`](../eval/results.json).

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
