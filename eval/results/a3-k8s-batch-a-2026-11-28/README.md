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

## Cache invalidation, recovery, and persistence

| Check | Observed result |
| --- | --- |
| [Cache invalidation](a2-cache-invalidation/summary.json) | The same single-worker API process returned `miss, hit` before and after reindexing the synthetic hostel policy. All four answers used stub generation and cited the policy. Restoring its original 11:00 p.m. curfew re-embedded six chunks and changed the index from `0b5fe89165a4` to `e15c5f0ab556`. |
| [Isolated physical restore](a2-reliability/isolated-restore/physical-restore.json) | All 642 records, metadata, text hashes, and vectors matched exactly. Vector and full-text queries passed on the restored copy. |
| [SQLite backup checks](a2-reliability/isolated-restore/sqlite-checks.json) | The Chroma-matched SQLite 3.46.0 passed on both the archive and the read-only online snapshot. The same Windows SQLite 3.50.4 FTS5 warning appeared on both copies. |
| [Isolated logical restore](a2-reliability/isolated-restore/logical-restore.json) | All 642 IDs, metadata records, and texts matched, and representative retrieval passed. Of the vectors, 570 matched exactly and 72 differed by at most one float32 step. |
| [Interrupted write](a2-reliability/interrupted-job/partial-write-audit.json) | The test writer exited deliberately with code 86 after writing three synthetic chunks. Chroma temporarily held 645 records. The prior state and all original records were preserved. |
| [Recovery](a2-reliability/recovery-job/audit.json) | One normal ingestion run removed the three orphan chunks with zero embeddings and restored 642 consistent records at index `a7ea00b43ec0`. The parsed indexing state matched its earlier values. |
| [Chroma pod replacement](a2-reliability/chroma-persistence/after.json) | A new Ready pod from the same StatefulSet used the same PVC. All 642 records and the index ID were preserved, and the state file was byte-identical across the replacement. |

[Terminal Job events](a2-reliability/job-events.json) are excerpts of the
actual structured logs. The [backup inventory](a2-reliability/backup-artifacts.json)
records checksums for the local archives. No database copies or logical
export records are stored here. The A3 CronJob remained suspended throughout.

## A3 API and observability

The [API and observability evidence](a3-api-observability/README.md) records
hosted generation, cache provenance, a retrieved injection, matching stdout
and PVC traces, trace-tool output, Prometheus metrics, and resource samples.
The API rollout preserved all three recorded traces and read the existing
642-chunk collection.
