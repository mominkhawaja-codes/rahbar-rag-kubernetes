# Formal native validation of Parts A1 and A2 (Batch A, then Batch B)

Run on 27 Nov 2026 on the development laptop (Windows, native Python 3.12), with real OCR
(Tesseract, Poppler 26.09.0), real embeddings (Ollama `nomic-embed-text`) and two isolated Chroma
1.0.12 servers started for this run. No Docker, no Kubernetes, no existing index touched.
`corpus/raw/` was only read.

## Reproduce

```
bash scripts/a3_batch_validation.sh /e/rahbar-a3-work     # an empty work directory
```

The script stages the lake, starts the two Chroma servers (ports 8801 and 8802, data under the
work directory), runs every step below and writes its command, log, exit code, reports and audit
into one folder per step here. It needs `corpus/raw/` (`corpus/fetch.py`), Ollama with
`nomic-embed-text`, Tesseract, Poppler (`POPPLER_BIN`) and a Chroma 1.0.12 binary (`CHROMA_BIN`).
The run took 7.5 minutes, 6 of them the first Batch A ingestion (OCR and 612 chunk embeddings).
The indexer's `HTTP Request` log lines are filtered out of each `log.txt`.

Each step folder holds `command.txt` (the exact command), `log.txt` (the indexer's JSON log),
`exit.txt`, the indexer's own reports (`verification-*.json`, `ingest-*.json`) and the audit made
afterwards by `eval/tools/a3_index_audit.py` (`audit.json`, which reads Chroma and the state file
directly; `audit-stdout.json` is the same text printed).

## Results

| Step | Command | Result |
| --- | --- | --- |
| 1 `step01-batch-a-ingest` | `--stage-a`, then `python -m pipeline.run` | **Full build**: 9 documents added, **612 chunks embedded**, 54 verification embeddings (9 documents x 6 windows), nothing quarantined, 361 s. OCR pages: 35 (2023 rules 23 of 25, rules handbook 2 of 55, student handbook 2 of 62, faculty policy 8 of 8), none left empty. The 2023 rules, declared superseded, are archived. The faculty policy already carries `doc_id` `faculty-entrepreneurship-policy`. Audit: consistent. |
| 2 `step02-batch-a-rerun` | same command, nothing changed | **Incremental, 9 unchanged: 0 chunks embedded, 0 verification embeddings**, 0 upserted, 0 deleted, `index_id` unchanged (`da2497c88c1a`), 2.7 s. All 612 chunks identical in id and embedding to step 1. |
| 3 `step03-baseline` | stop Chroma, copy the work area | **Baseline preserved** (below). |
| 4 `step04-batch-b-working-copy` | copy of the baseline, second Chroma on its copy | Audit: 612 chunks, `index_id da2497c88c1a`, **612 of 612 identical** to the baseline manifest. |
| 5 `step05-batch-b-ingest` | `--stage-b`, then `python -m pipeline.run` | **Incremental**: 5 documents added, 8 unchanged (their chunks reused), **5 rejected**, 1 newly archived. **30 chunks embedded** (6 + 6 + 6 + 7 + 5), 44 verification embeddings (new and rejected files only), 7 chunk flags updated, 0 deleted, 0 orphans removed, 642 chunks, new `index_id 6150bb31292d`, 30.9 s. |
| 6 `step06-batch-b-audit` | `a3_index_audit.py --compare --query` | Consistent: 642 chunks in the store = 642 in the state, **no orphans, no missing, no count or flag mismatch**. 612 baseline chunks kept with identical id and embedding. Only change to them: the 7 chunks of the 2022 faculty policy went `superseded False -> True`. Retrieval below. |
| 7a `step07a-restore-older-policy` | `--restore faculty-entrepreneurship-and-technical-start-ups-policy.pdf` | Older policy active, the 2026 revision archived; 14 chunk flags updated, **nothing embedded**, new `index_id 63de60605ea6`. Active-only retrieval now returns the 2022 policy. |
| 7b `step07b-restore-newer-policy` | `--restore faculty-entrepreneurship-policy-2026-revision.pdf` | Revision active, 2022 policy archived again; 14 flags updated, nothing embedded, `index_id 93481e3de295`. Active-only retrieval returns the revision. |
| 8 `step08-batch-b-rerun` | `python -m pipeline.run`, nothing changed | **14 unchanged: 0 chunks embedded, 0 verification embeddings**, `index_id` unchanged (`93481e3de295`), the restore choice kept, 2.5 s. Audit consistent. |

Quarantined in step 5 (also in `step06-batch-b-audit/quarantine-listing.txt`; reasons are in
`step05-batch-b-ingest/verification-*.json`):

| File | Check that failed | Reason recorded |
| --- | --- | --- |
| `fee-structure-page-2026-27-download.html` | uniqueness | exact duplicate of `fee-structure-page.html` |
| `annual-report-2019-20-mirror.pdf` | uniqueness | exact duplicate of `annual-report-2019-20.pdf` |
| `examination-appeals-procedure-2026-reissue.md` | uniqueness | near-duplicate of `examination-appeals-procedure-2026.md`, document cosine 0.9919 >= 0.98 |
| `student-clubs-and-societies-guide-2026.pdf` | quality, completeness | extraction failed (`Unexpected EOF`); file hash differs from the recorded sha256 |
| `campus-shuttle-service-notice.html` | completeness | title missing; version/date missing |

Accepted and indexed: the library, hostel, appeals and student-affairs documents (the last carries
the planted injection, which verification does not read; the retrieval guardrail is Part D) and
the 2026 faculty revision. The 2022 faculty policy was **superseded by the revision**
(`recency`, cosine 0.9416 between the two) and archived, not quarantined. Archive copies and
their sidecars are listed in `step06-batch-b-audit/archive-listing.txt`.

Retrieval for "For how many years can a faculty start-up use the university's intellectual
property at no initial cost?" (`audit.json` in steps 6, 7a and 7b, top 3):

| State | With `superseded == False` (what the API retrieves) | Without the filter |
| --- | --- | --- |
| After step 5 | all three from the 2026 revision | revision, then two chunks of the 2022 policy (flagged superseded) |
| After 7a | all three from the 2022 policy | the (now archived) revision first, then two 2022 chunks |
| After 7b | all three from the 2026 revision | revision, then two 2022 chunks (flagged superseded) |

## Baseline for later evaluation and drift measurement

`E:\rahbar-a3-work\baseline_batch_a\` (outside the repository): the Batch A Chroma data
(`chroma\`, 612 chunks, `index_id da2497c88c1a`), `state\index-state.json`, `archive\`, and the
lake with its `_index.json`. It was copied while Chroma was stopped and was not opened again;
`step03-baseline\baseline-sha256.txt` lists the hash of every file, and the script confirmed at
the end that the snapshot is unchanged. In this repository: `step03-baseline\index-state-batch-a.json`
(the state) and `chunk-manifest-batch-a.json` (every chunk id, source, flag and an md5 of its
embedding). The Batch B working copy is `E:\rahbar-a3-work\batch_b\` (Chroma data, state, archive,
quarantine, lake); to measure drift, serve the baseline copy with
`chroma run --path E:/rahbar-a3-work/baseline_batch_a/chroma --port <port>` and use
`CHROMA_COLLECTION=rahbar`.

## Discrepancies and observations

- **No outcome differed from the plan.** All ten Batch B files behaved as the README predicted,
  the earlier pre-check and this run agree, and the formal run did not need a repeat.
- **Exact duplicates are still embedded during verification.** Of the 44 verification embeddings in
  step 5, 12 were spent on the two exact duplicates (6 windows each), which the hash had already
  identified. It does not change any outcome; skipping the embedding for a hash duplicate would
  save it. Not changed, because it is outside the assignment.
- **The `--restore` steps write no `ingest-*.json`.** Their result (documents flipped, chunk flags
  updated, new `index_id`) is the last JSON object in `log.txt`.
- **Chroma 1.0.12 drops `hnsw:space` from collection metadata after a metadata update** (known,
  documented in `docs/PIPELINE.md`); the audit shows `index_id`, `embed_model` and `dimension`
  intact.
- The Ollama server and nothing else outlives the run; both Chroma servers were stopped by the
  script.
