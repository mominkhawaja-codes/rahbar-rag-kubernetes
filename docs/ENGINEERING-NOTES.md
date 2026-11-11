# Engineering notes

Answers to the seven questions in section 7, each tied to our own files. Questions 4
and 5 need numbers from the running cluster; where a number is not measured yet, the
note says so and gives the exact command that produces it.

## 1. Indexing in API startup: what breaks with 3 replicas and one Chroma volume?

Three writers, one collection. Every replica would delete and rebuild `rahbar` on start,
so the replicas race: one deletes the collection while another is adding to it, and the
survivor holds a mix of two builds, or an empty set if the last delete wins. Every restart
and every HPA scale-up (3 → 8 in `k8s/base/api.yaml:112-129`) would trigger another full
re-index, OCR included, while pods are *alive* but not ready. The startup probe
(`api.yaml`, 60 s budget) would kill pods mid-index, and they would restart into the same
loop.

What we did instead: the API only reads. `api/app/main.py:34` (`lifespan`) builds clients
and checks the pin; it adds nothing. The only `.add()` in the system is in
`indexer/pipeline/store.py:59-98`, run by a Job. It builds `rahbar__staging` and renames it
to live only when it is complete (`store.py:96`), so the API never sees a half-written
index (ADR 0001).

## 2. Readiness and liveness swapped, Chroma slow for 90 seconds: what happens?

Our liveness, `/healthz` (`api/app/routes/health.py:22`), touches nothing. Readiness,
`/readyz` (`health.py:44`), calls Chroma's heartbeat with a 2 s timeout and counts the
collection (`api/app/retrieval/chroma.py:151`).

**Swapped:** liveness would then run the Chroma checks. With `periodSeconds: 10,
failureThreshold: 3` (`k8s/base/api.yaml:65-69`), all three API pods fail liveness after
about 30 s of a slow Chroma, and the kubelet restarts every one of them. Restarting does
not make Chroma faster: the new pods start, their probes fail again, and the restarts back
off (CrashLoopBackOff). When Chroma recovers at 90 s, the API is still sitting in restart
back-off, so a 90-second dependency stall becomes a longer full outage. Meanwhile
readiness, now on `/healthz`, keeps saying "ready", so the Service keeps sending traffic to
pods that are about to be killed.

**As configured:** after about 15 s (3 × 5 s) the pods leave the Service endpoints, the
Ingress returns 503 quickly instead of hanging, nothing restarts, and the pods rejoin by
themselves seconds after Chroma recovers. The checker's rule "liveness never touches a
dependency" exists for exactly this case. Evidence to capture on the cluster:
`kubectl -n rahbar get endpoints api -w` while scaling Chroma to 0 for 90 s.

## 3. What does a CPU-based HPA measure on a generation service, and what should scale it?

It measures the API pods' own CPU (`api.yaml:112-129`, target 70% of a 100m request). But
an `/api/ask` request spends most of its time *waiting*: on Ollama or the hosted LLM
(seconds), then on Chroma and Redis (milliseconds). Waiting costs almost no CPU, so 50
requests stuck behind a slow model can show the same low CPU as an idle pod, and the HPA
will not scale. Even if it did, more API pods would only send more load to the same single
model, which is the real bottleneck.

What should drive it is a signal of *demand on the model*:

- requests in flight per pod;
- p95 of `rahbar_generation_seconds`;
- queue depth in front of the LLM.

All three come from our `/metrics` (`api/app/routes/metrics.py`) through Prometheus and a
custom-metrics adapter (prometheus-adapter or KEDA), which minikube does not have by
default. With a hosted model, the provider's rate limit is the real ceiling, and scaling
pods beyond it only produces more 429s. CPU is therefore the floor we can run today, not
the right signal; the semantic cache is our cheapest real capacity gain.

## 4. The indexer's peak memory, its limit, the margin, and the exit code when it is wrong

**Request 512 Mi, limit 1536 Mi** (`k8s/base/indexer-job.yaml:66,69`,
`indexer-cronjob.yaml:71,74`).

**Measured peak: 1129.8 MiB.** Workflow `indexer-memory`, run
[43062518890](https://github.com/mominkhawaja-codes/rahbar-rag-kubernetes/actions/runs/43062518890),
commit 07e78ef.
- **Workload:** the real indexer image (`indexer/Dockerfile`, Tesseract and poppler) on the
  real corpus, fetched with `corpus/fetch.py` and verified against `corpus/MANIFEST.md`.
  Every stage ran: 9 documents, 275 pages, **35 pages OCR'd**, 612 chunks, 612 vectors of
  768 dimensions from `nomic-embed-text` (Ollama 0.5.7), stored in Chroma 1.0.12. 473 s.
- **Limits as the Job sets them:** 1536 MiB with no swap, 1 CPU, read-only root, `/tmp` on
  a disk volume, all capabilities dropped, ConfigMap environment (400/50 chunks, OCR on).
- **Method** (`eval/tools/measure_memory.py`): the container's cgroup v2 accounting, read
  from the host. It covers every process in the container, not only the Python parent.
  `memory.peak` is the kernel's exact high-water mark. The other figures are sampled every
  0.2 s (2365 samples), so a spike shorter than that can be missed.

| Measure | MiB |
| --- | --- |
| `memory.peak` (high-water mark, includes page cache) | **1129.8** |
| Working set, `memory.current − inactive_file` (what `kubectl top` reports) | 1117.3 |
| Anonymous memory (not reclaimable; what gets a container OOM-killed) | 1112.4 |
| Page cache, at most | 24.9 |
| Summed RSS of all processes, at its peak | 1156.0 (python 1071, tesseract 85) |
| Largest single process | python 1093.2 · tesseract 130.5 · pdftoppm 68.1 |

- **Margin:** limit ÷ peak = 1536 ÷ 1129.8 = **1.36×**, 406 MiB (26%) of headroom.
- **Against the request:** the peak is **2.2× the 512 MiB request**.
- **Our first sizing argument was wrong.** We had sized the limit for one 300-dpi page in
  Tesseract (a 2480 × 3508 image, about 25 MB). Tesseract never used more than 131 MiB. The
  Python process itself reached 1.07 GiB. This run did not record which stage inside Python
  holds it; the per-sample series is in the run's `indexer-memory` artifact. Plausible
  causes, not measured: pdfplumber's per-page objects for the image-heavy scanned PDF, and
  every chunk and vector held in memory until `store` (`indexer/pipeline/run.py:108-111`
  embeds the whole corpus before storing).

**Are the numbers right?**
- **The limit is adequate but tight.** 1.36× on one run of this corpus; a larger scanned
  document would cross it.
- **The request is too low.** The scheduler reserves 512 MiB for a pod that uses 1.1 GiB, so
  on a busy node the indexer is the first candidate for eviction under memory pressure
  (Burstable QoS, usage above request).
- **Proposed, not applied:** request about **1152 Mi** (the measured peak) and limit about
  **2048 Mi** (1.8×). This needs agreement before changing the manifests, and the minikube
  node (6144 MB in `make cluster`) has room for it.

**When the limit is wrong: OOMKilled, exit code 137.**
- **What happens:** when a container's anonymous memory would exceed its cgroup limit and
  nothing can be reclaimed, the kernel's OOM killer sends SIGKILL. The container exits with
  **137** (128 + 9), and Kubernetes records `Last State: Terminated, Reason: OOMKilled,
  Exit Code: 137`.
- **We reproduced it** (Docker, not Kubernetes): the same workflow ran the indexer again with
  its limit cut to **556 MiB**, half the measured anonymous peak. The kernel killed it after
  15.3 s: exit code 137, `OOMKilled: true`, at `memory.peak` 553.6 MiB. Only `indexer
  starting` had been logged, so it died while extracting the first document in
  `_index.json` order, the scanned 2023 rules PDF.
- **Retries:** with `restartPolicy: OnFailure` (`indexer-job.yaml:20`), the kubelet restarts
  the container in the same pod with an increasing back-off (10 s, 20 s, 40 s, … up to 5
  minutes). Failures count against `backoffLimit: 3` (`:11`); once that is exceeded the Job
  is marked `Failed` (`BackoffLimitExceeded`) and its pod is terminated. `activeDeadlineSeconds:
  3600` (`:12`) caps the whole Job at an hour. An OOM is deterministic for a given corpus
  and limit, so the retries fail the same way: the fix is the limit, not a retry. This
  paragraph is Kubernetes' documented behaviour; we have not observed it on a cluster yet.
- **The existing Chroma index survives a failed run.** The indexer embeds everything before
  it writes anything (`run.py:108-111`), then builds `rahbar__staging` and only renames it
  to `rahbar` at the end (`store.py:73-97`). Measured in the same workflow
  (`eval/tools/index_state.py`), before and after the OOM-killed run: collections
  `["rahbar"]`, 612 chunks, `index_id 6d1adb3f2630`, both times. No staging collection was
  left behind, because the kill came before `store`.
  - A kill *during* `store` would leave a partial `rahbar__staging`, which the next run
    drops first (`store.py:75`).
  - The one unsafe moment is between the two renames (`store.py:93-96`), when no collection
    is named `rahbar`. A kill there makes the API not ready until the next successful run.
  - The window lasts as long as two HTTP calls, and we have not provoked it.

**Still pending (needs the live cluster, not yet done):**
- `kubectl top pod -n rahbar -l app=indexer --containers` during `make index`. Note that
  metrics-server samples about every 15 s and will usually read below the kernel peak above.
- `kubectl describe pod` showing a real `OOMKilled` / `137`.
- The Job's restarts and `BackoffLimitExceeded` on minikube.

## 5. recall@5 by question type: where is it worst, and why?

Measured on the real corpus (`eval/results.json`, eval-corpus run 42498760820, 400/50):

| Type | recall@5 |
| --- | --- |
| ordinary (10) | 1.00 |
| version (2) | 1.00 |
| table (1) | 1.00 |
| injection (1) | 1.00 |
| **exact (3)** | **0.67** |

**Worst: exact.** The miss is q02, "What does clause 1.21 of the academic rules require?",
at every chunk size. The question names the clause only by its number, while the passage
talks about attendance, and a dense embedding barely represents "1.21". (Which passages
it retrieved instead was not recorded by this run.) The number is in the
embedded text (the section label `[1.21-1.22]` goes into `embedding_text`,
`indexer/pipeline/embed.py`), but one token among hundreds does not move the vector. The
other two exact questions (q11 tuition per credit hour, q12 security deposit) succeed,
because they name the thing as well as the number. The fix would be hybrid retrieval: a
keyword or metadata match on clause numbers (a `where_document` / `$contains` filter in
Chroma) alongside the vector search.

Our first prediction, that the table question would be weakest, was wrong: q16 (B+ grade
points) found handbook clause 2.3 in every configuration.

## 6. EMBED_MODEL changed on the API only: what does your system do?

It refuses, loudly, in three places. The collection's metadata records the model it was
built with (`indexer/pipeline/store.py`, `collection_metadata`).

1. At start-up, `api/app/main.py:47` logs `embedding pin check failed` with the reason
   from `pin_problem` (`api/app/retrieval/chroma.py:52`): "embedding model mismatch:
   index built with 'nomic-embed-text', API configured with EMBED_MODEL='…'".
2. `/readyz` returns 503 with `embed_pin` in `failed` (`chroma.py:151`,
   `routes/health.py:44`), so the pods never join the Service. During a rolling update
   with `maxUnavailable: 0`, the old pods keep serving and the rollout stalls instead of
   completing.
3. Should a request reach such a pod anyway, `retrieve` raises `EmbeddingPinMismatch`
   (`chroma.py:116`), and the API answers 503 naming `embed_pin`. If the model *name*
   matches but the vector *dimension* differs, `chroma.py:120` catches that too.

Without these checks, the query vectors would come from a different space than the stored
ones: Chroma would still return the "nearest" chunks, the scores would look plausible, and
the answers would be confidently wrong with nothing in any log. Tests:
`test_embedding_model_mismatch_is_refused` and `test_readyz_fails_when_the_pin_does_not_match`.
The demonstration from STARTER.md: index with `--dummy` (records `dummy-768`), set the real
model in the ConfigMap, do not re-index, and watch readiness fail.

## 7. The failure that cost us more than an hour

**The development environment, not the code.** Docker Desktop would not start:
"The virtual machine or container JSON document is invalid"
(`Wsl/Service/RegisterDistro/CreateVm/HCS/HCS_E_INVALID_JSON`). We first suspected
Docker, then a bad `.wslconfig` (there was none), and then downgraded WSL from 3.0.3 to
2.0.14. That did not help: the same `HCS/0x8037010d` error appeared when importing a plain
Alpine distro into WSL with no Docker involved, which proved the cause was the Windows
build (10.0.21996, a 2021 preview build whose virtualisation service cannot parse the
VM configuration newer WSL sends). Docker Desktop 4.98 also refuses WSL versions old
enough to suit that build, so no WSL version could work. Fix: upgrade Windows to a release
build. Meanwhile we built and tested everything that does not need a cluster: unit
tests against stubs and fakes, and the indexer's `--dry-run` on the real corpus.

**Lesson:** test the smallest thing that can fail (one VM, no Docker) before changing
versions. One `wsl --import` of a 3 MB distro answered the question that two reinstalls
did not.

A second, smaller one, in the code: our first header/footer rule masked every digit, so
"5.1 Rule…" and "5.2 Rule…" became the same "repeated line" and real clauses were deleted
as boilerplate. A unit test with five numbered clauses caught it
(`docs/FAILURE-ANALYSIS.md`, stage 2).
