# A3 API and observability validation

Observed on Minikube on 28 November 2026 using the
[A3 API overlay](../../../../k8s/overlays/a3-api/kustomization.yaml).
One API replica and worker served the `rahbar-a3` collection and generated
answers with Groq `openai/gpt-oss-20b`. Readiness returned 642 chunks and
confirmed the `nomic-embed-text` embedding pin. The API reused the
tracing-capable image already loaded in Minikube under a new local tag.

| Request | Cache | Observed result |
| --- | --- | --- |
| `a3-f4-1aab29a3-miss` | Miss | Cited the synthetic hostel policy for the 11:00 p.m. curfew. |
| `a3-f4-1aab29a3-hit` | Hit | Returned the same answer and linked to the miss trace. |
| `a3-f4-1aab29a3-injection` | Miss | Retrieved and flagged the injected FAQ passage, passed the output-echo check, and answered Rs. 500 with the student affairs FAQ citation. The provider also included an extraneous library-policy citation. |

The [trace verification](trace-verification.json) matched all three stdout
JSON lines with the [PVC trace sample](traces/traces-2026-11-28-a3-cache-api-694f5646b9-fp4x9.jsonl).
The Minikube hostpath root retained its
provisioner permissions. The non-root API wrote trace files as UID/GID 10001.
[The trace reconstruction](trace-show-cache-hit.txt) followed the hit to its source
and rendered the exact context and prompt with the recorded SHA-256.
[The trace summary](trace-summary.json) found three answers,
one cache hit, zero abstentions, one retrieved injection, and zero injection
echoes. Total latency was 2.06 s p50 and 5.66 s p95 across these three queries.
The [Prometheus port-forward sample](metrics-sample.prom) showed three
successful asks, two retrievals, two generations, and one cache hit.

The API CPU request was raised to 200m using the measured smoke workload.
That rollout replaced the API pod. The [persistence check](trace-persistence.json)
verified the same trace PVC and byte-identical JSONL on the new Ready pod.

| Component | Observed CPU / working set | Requests | Limits |
| --- | --- | --- | --- |
| A3 API | 159m / 94.8 MiB | 200m / 192 MiB | 500m / 512 MiB |
| A3 Chroma | 31.5m / 69.3 MiB | 100m / 256 MiB | 1 CPU / 1 GiB |
| A3 indexer | Completed pods had no Metrics API samples | 250m / 512 MiB | 1 CPU / 1536 MiB |

The [resource evidence](resource-evidence.json) records the Metrics API
timestamps and sample windows. The indexer limit uses the earlier
1129.8 MiB full-corpus Docker measurement as a reference. The API and
Chroma resource samples cover the smoke workload only.
