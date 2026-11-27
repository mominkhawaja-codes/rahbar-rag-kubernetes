# Kubernetes integration checklist for Parts A1 and A2

For whoever wires the verification stage and the incremental update into the cluster. It
lists what the code needs; the manifests are not changed yet. Where a statement was
measured, the source is named. **Nothing here was run on Kubernetes**: the code was tested
natively on Windows with a stand-in for Chroma, plus one smoke run against a real Chroma
1.0.12 server (`eval/results/a2-incremental/chroma-smoke-20261127.txt`). Section 6 is the
test plan that still has to be carried out on the cluster.

Behaviour is described in `docs/PIPELINE.md` ("Verification" and "Incremental update and
document lifecycle").

## 1. Environment variables

All are read by `indexer/pipeline/config.py`. The existing ones already come from the
`rahbar-config` ConfigMap through `envFrom`; the new ones need keys added there (or set on the
Job) and must point at the volumes in section 2.

| Variable | Default | Meaning | Set in the cluster to |
| --- | --- | --- | --- |
| `CORPUS_DIR` | `/corpus` | The data lake: documents and, for PDFs, `_index.json` | the lake volume mount |
| `QUARANTINE_DIR` | `<tmp>/rahbar/quarantine` | Where rejected files are moved | quarantine mount |
| `REPORT_DIR` | `<tmp>/rahbar/reports` | `verification-*.json` and `ingest-*.json` | reports mount |
| `STATE_DIR` | `<tmp>/rahbar/state` | `index-state.json` (what is indexed) | **persistent** state mount |
| `ARCHIVE_DIR` | `<tmp>/rahbar/archive` | Copies of superseded versions | archive mount |
| `NEAR_DUP_THRESHOLD` | `0.98` | Document cosine at or above this is a near-duplicate | keep (calibrated) |
| `MIN_CHARS` | `200` | Less extracted text than this counts as empty | keep |
| `VERIFY_ENABLED` | `true` | `false` restores the Assignment 02 full rebuild, no verification | keep `true` |
| `EMBED_MODEL`, `CHUNK_SIZE`, `CHUNK_OVERLAP`, `CHROMA_COLLECTION` | existing | A change of any of these forces one full rebuild | unchanged |
| `OLLAMA_URL`, `CHROMA_HOST`, `CHROMA_PORT`, `OCR_ENABLED` | existing | unchanged | unchanged |

`<tmp>` is the pod's temp directory. The Job's root filesystem is read-only and only `/tmp`
is an `emptyDir`, so **leaving the four directories at their defaults loses the state, the
quarantine, the archive and the reports when the pod ends**: the next run would rebuild
everything. They must be set to persistent mounts.

## 2. Persistent volumes

One volume per kind of data is the simplest layout; one PVC with four sub-paths also works.
Sizes below are rough: the real corpus is about 34 MB of files, the state file is under 1 MB
for nine documents (each document stores a 768-number vector), and reports are small JSON.

| Mount path (suggested) | Env var | Written by | Must survive pod restarts | Notes |
| --- | --- | --- | --- | --- |
| `/data/lake` | `CORPUS_DIR` | the Job (quarantine removes files from it) and whoever adds documents | yes | See "Lake contents" below |
| `/data/quarantine` | `QUARANTINE_DIR` | the Job | yes | Rejected files, for review |
| `/data/archive` | `ARCHIVE_DIR` | the Job | **yes** | Needed to restore a version whose file left the lake |
| `/data/state` | `STATE_DIR` | the Job | **yes** | Losing it means one full rebuild, nothing else |
| `/data/reports` | `REPORT_DIR` | the Job | yes | Evidence for the report; also read by the report scripts |

- Run the container as the existing user (uid 10001, `runAsNonRoot`); set `fsGroup: 10001` so
  the volumes are writable. Keep `readOnlyRootFilesystem: true`.
- The Job and the CronJob must mount the **same** volumes, and `concurrencyPolicy: Forbid`
  (already set on the CronJob) must stay: two runs at once would race on the state file.
  With a ReadWriteOnce volume (minikube's default) a second pod on another node cannot mount
  it anyway.
- The API does not need these volumes. The Job stays the only writer to Chroma.

**Lake contents.** Today the documents are baked into the image at `/corpus` and are
read-only. For the lake to take new documents it has to be a volume that is seeded once:

- PDFs and HTML need a `_index.json` next to them giving each file's `name` (its title),
  `file`, `doc_version`, optionally `doc_id`, `superseded`, `sha256` and `bytes` (the format
  `corpus/fetch.py` already writes). Without an entry, a PDF has no title or version and is
  quarantined as incomplete. Markdown and text files carry the same fields as front matter
  (`doc_version`, `superseded`, `doc_id`, `title`; the first `#` heading is used as the title).
- If `_index.json` exists it is the only list of files: a file that is not listed is ignored.
- Seed the volume from the image (an `initContainer` that copies `/corpus` when the volume is
  empty) or with `kubectl cp`. Batch B is added the same way.
- `doc_id` ties the versions of one document together. Without it no versions are guessed: an
  older file is only treated as superseded when its source says `superseded: true`.

## 3. Required dependencies

Nothing new has to be installed. `incremental.py` and `verify.py` use only the standard
library plus what the indexer already has (`chromadb-client==1.0.12`, `httpx`, `pdfplumber`,
`pypdf`, `pytesseract`, `pdf2image`). The image already contains `tesseract-ocr` and
`poppler-utils` (`indexer/Dockerfile`).

- Chroma server image must stay at 1.0.12, the version the client is pinned to.
- Ollama with `nomic-embed-text` must be reachable from the Job at `OLLAMA_URL`. The
  verification stage also needs it (near-duplicate check), not only the chunk embedding, but
  an unchanged rerun calls it zero times.
- The NetworkPolicy `indexer-egress` already allows Chroma and Ollama; no change is needed.
- Resources: the Job's current limit is 1536 Mi (peak measured for a full real-corpus build:
  1129.8 MiB, `docs/ENGINEERING-NOTES.md`). The incremental path was **not** measured on the
  cluster; unchanged documents are not extracted, so it should need no more. Re-measure when
  Part F5 is done.

## 4. Job behaviour

Command stays `python -m pipeline.run` (the image default). Extra flags:

| Command | What it does |
| --- | --- |
| `python -m pipeline.run` | Verify, then full or incremental ingest (see below). Exit 0 when the index and state are consistent |
| `python -m pipeline.run --full-rebuild` | Ignore the state; rebuild through the staging swap |
| `python -m pipeline.run --verify-only [--no-quarantine]` | Verification and its report only; needs Ollama but not Chroma |
| `python -m pipeline.run --restore FILE` | Make an archived version active (from the archive if its file left the lake); archives sibling versions |
| `python -m pipeline.run --archive FILE` | Exclude one document from retrieval, keep its chunks |
| `python -m pipeline.run --dry-run` | Chunk statistics only; writes nothing |

Normal run:

1. Waits for Chroma (the existing `wait-for-chroma` init container).
2. Reads the lake and `STATE_DIR/index-state.json`. No state, a missing collection or a
   changed model, chunk size, overlap or collection name means a **full rebuild** through the
   existing staging swap; otherwise it is **incremental**.
3. Verifies new and changed files; unchanged files are skipped (no extraction, no embeddings).
   Rejected files are moved to `QUARANTINE_DIR`; a verification report is written.
4. Embeds and writes only new or changed documents, removes deleted or rejected ones, flips
   `superseded` where versions changed, then checks per-document chunk counts against the
   state. A mismatch fails the Job (non-zero exit) **before** the state file is written.
5. Gives the collection a new `index_id` if anything changed, writes `index-state.json` and
   `ingest-<timestamp>.json`, and logs `ingest finished` with the counts.

Behaviour to rely on:

- **Retry safe.** If a pod dies mid-update, the state file still describes the previous run,
  and the retry (`backoffLimit`) redoes the work idempotently. Covered by
  `test_interrupted_update_is_completed_by_the_next_run`.
- **Exit status.** Non-zero when the run cannot finish: no documents, every document rejected,
  an embedding-dimension mismatch, or the store disagreeing with the state. Rejecting some
  documents is not a failure; read the report for that.
- **Quarantine moves files out of the lake.** A lake mounted read-only turns that into a
  recorded `quarantine_error`; the document is still not indexed. Mount it read-write.
- **Cache invalidation.** A change gives a new `index_id`; the API reads it on every request,
  so a changed index never serves cached answers (api change in section 5).
- **Output to collect:** `REPORT_DIR/verification-*.json` and `ingest-*.json`.

## 5. Deployment changes

1. **PVCs and mounts** from section 2 on `k8s/base/indexer-job.yaml` and
   `indexer-cronjob.yaml` (both), plus `fsGroup`.
2. **ConfigMap keys** `QUARANTINE_DIR`, `REPORT_DIR`, `STATE_DIR`, `ARCHIVE_DIR` and
   `CORPUS_DIR` (now the lake mount) in `k8s/base/configmap.yaml`.
3. **Seeding** of the lake from the image or with `kubectl cp`, including `_index.json`.
4. **Overlays.** The `ci` overlay's indexer uses the three fictional fixtures in `/fixtures`.
   Verification passes them (checked with `--verify-only`: 3 accepted, none quarantined), so
   the e2e evaluation does not need changes, but the CI Job also needs writable state and
   report directories: an `emptyDir` is enough there.
5. **API image.** One line changed: `ChromaRetriever.index_id()` now reads the collection
   metadata on every call instead of every 30 seconds. This adds one small request to Chroma
   per `/api/ask`, including cache hits. Rebuild and roll the API image so it carries this.
6. **No change** to the Chroma StatefulSet, Redis, Services, Ingress or NetworkPolicies.

## 6. Acceptance tests on the cluster

Run in this order on a fresh cluster. "Run the Job" means `kubectl create job
--from=cronjob/indexer` (or the Job manifest) and `kubectl logs job/<name>`.

| # | Action | Expected |
| --- | --- | --- |
| 1 | Apply manifests; seed the lake with the nine Batch A documents | Pods ready; lake lists nine files and `_index.json` |
| 2 | Run the Job | `mode: full`; no document quarantined; report in `REPORT_DIR`; `index-state.json` exists |
| 3 | Run the Job again, nothing changed | `mode: incremental`; `chunks.embedded 0`; `verification_embeddings 0`; `index_id_changed false` |
| 4 | Delete the Job pod while it is updating (after step 6) | Retry succeeds; counts consistent |
| 5 | Copy Batch B into the lake and run the Job | Duplicates, near-duplicate, truncated, missing-metadata and injected files reported with reasons; failures in `QUARANTINE_DIR`; newer version indexed, older one archived |
| 6 | Modify one file | Only that document's chunks embedded; `documents.changed` lists it |
| 7 | Delete one file | `documents.deleted` lists it; per-document chunk count is 0 (also by `kubectl exec` into a debug pod querying Chroma) |
| 8 | Ask the same question before and after step 6 or 7 | Second answer is not served from cache (`X-Cache: miss`); `index_id` differs |
| 9 | `--restore` a superseded version, then a version whose file was deleted from the lake | Chunk flags flip with no embedding; the deleted one is copied back from `ARCHIVE_DIR` and re-embedded |
| 10 | Restart the Job pod and the Chroma pod | State, archive, quarantine and reports are still there; the next run is a no-op |
| 11 | Edit a threshold in the ConfigMap (`NEAR_DUP_THRESHOLD`) and rerun | Changes the verification outcome without rebuilding the image |

Keep screenshots of the commands and outputs for the report appendix (Parts A and F).

## 7. Known limits to keep in mind

- Collection-metadata updates in Chroma drop the legacy `hnsw:space` key. The distance
  function stays cosine in the collection configuration (checked on 1.0.12); nothing reads the
  old key.
- A document deleted from the lake while **active** loses its chunks and has no archive copy.
  Archive first (`--archive`) if it must be restorable. Only archived versions are restorable.
- Restoring from the archive after `_index.json` changed re-lists the file from its sidecar
  (`<file>.meta.json`); archive copies made before that sidecar existed have none.
- Near-duplicate calibration margin is small (highest different-document pair 0.9576 against
  the 0.98 threshold) and was measured on nine documents only.
- The real nine-document corpus has not been through the incremental path with real
  embeddings; only the verification stage has (Batch A report).
