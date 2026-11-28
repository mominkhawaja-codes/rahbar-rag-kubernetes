# Assignment 03 A1/A2 Kubernetes evidence

Recorded on 28 November 2026 in Minikube, namespace and Chroma collection `rahbar-a3`.
[Job events](job-events.json) contains the terminal JSON events from the completed Jobs.
The audit links compare live Chroma with the persisted lake state and the relevant prior run.

| Job | Observed result | Chunks | Index ID after |
| --- | --- | ---: | --- |
| [`batch-a-retry-01`](batch-a-retry-01/audit.json) | Nine accepted, zero quarantined, 612 new chunk embeddings. | 0 to 612 | `fe1e42ada090` |
| [`batch-a-unchanged-01`](batch-a-unchanged-01/audit.json) | Nine unchanged and zero new chunk or verification embeddings. State, chunk IDs, vectors and checked metadata matched. | 612 | `fe1e42ada090` |
| [`batch-b-incremental-01`](batch-b-incremental-01/audit.json) | Five accepted, five quarantined, 30 new chunk embeddings. All 612 Batch A vectors retained. | 612 to 642 | `6e665ec86456` |
| [`batch-b-replace-01`](a2-lifecycle/batch-b-replace-01/audit.json) | Only the six synthetic hostel chunks were upserted. Curfew text changed to 10:30 p.m. | 642 | `e5743feb6e9f` |
| [`batch-b-restore-older-01`](a2-lifecycle/batch-b-restore-older-01/audit.json) | 2022 faculty version active. 14 flags updated with zero new embeddings. | 642 | `2dbda966579d` |
| [`batch-b-delete-2026-01`](a2-lifecycle/batch-b-delete-2026-01/audit.json) | Seven chunks removed when the archived synthetic 2026 source left the lake. Archive checksum matched. | 642 to 635 | `ff628f3bb684` |
| [`batch-b-restore-2026-01`](a2-lifecycle/batch-b-restore-2026-01/audit.json) | Restored the synthetic revision from its archive. Seven regenerated vectors matched their pre-deletion fingerprints. | 635 to 642 | `0b5fe89165a4` |

Chroma and persisted state agreed in the final audit. Active-only Chroma
retrieval returned only the 2026 faculty revision. The CronJob stayed suspended.
The original initial Job hit its 60-minute deadline during control-plane and
host memory pressure. Exit code 137 was observed without confirmation of OOM.
The successful retry is linked above.

The committed manifests retain SHA-256 fingerprints, chunk IDs, and metadata
for [initial Batch A](batch-a-retry-01/chunk-manifest.json),
[completed Batch B](a2-lifecycle/batch-b-chunk-manifest.json), and the
[final lifecycle state](a2-lifecycle/batch-b-restore-2026-01/chunk-manifest.json).
Full logs, raw documents, state files with vectors, and local recovery artifacts
remain outside the commit. Native evidence is in
[the native validation directory](../a3-batch-validation/README.md).
