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

**Limit:** 1536 Mi, request 512 Mi (`k8s/base/indexer-job.yaml:66`,
`indexer-cronjob.yaml:71`). It was sized for the worst stage: rasterising one A4 page at
300 dpi for Tesseract (about 2480 × 3508 px; around 25 MB as RGB, several times that
while pdf2image and Tesseract hold copies). We OCR one page at a time
(`indexer/pipeline/extract.py`, `ocr_pdf_pages`) so peak memory does not grow with the
document.

**Peak: not measured yet.** Take it with `kubectl top pod -n rahbar -l app=indexer
--containers` every few seconds while `make index` runs (metrics-server samples every
~15 s, so also read `container_memory_working_set_bytes` if Prometheus is installed). Then
record the peak and the margin (limit ÷ peak) here.

**When the limit is wrong:** the kernel OOM-kills the container. The pod shows
`OOMKilled`, exit code **137** (128 + SIGKILL), and Kubernetes restarts it under
`restartPolicy: OnFailure` up to `backoffLimit: 3`, after which the Job is `Failed`. The
live index is untouched, because the swap only happens at the end (ADR 0001). Check with
`kubectl -n rahbar describe pod -l app=indexer | grep -A3 "Last State"`.

## 5. recall@5 by question type: where is it worst, and why?

**Not measured yet**: it needs Chroma, the embedding model and the real index. Run
`make eval` and read `recall@5_by_type` in `eval/results.json` (types: ordinary, exact,
version, table, injection). Our prediction, to be confirmed or refuted by those numbers:

- **table** (q16, B+ grade points) is likely the weakest. A pipe table embeds poorly next
  to prose, and the same grade table appears in the handbook, the prospectus and the
  grading page, so the top 5 may hold the other two copies instead of the handbook's
  clause 2.3.
- **exact** questions that quote a clause number ("clause 1.21") depend on the number
  being in the embedded text. It is: the section label `[1.21-1.22]` goes into
  `embedding_text`, `indexer/pipeline/embed.py`. But dense embeddings match numbers weakly.

Write the measured table and the actual worst type here.

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
