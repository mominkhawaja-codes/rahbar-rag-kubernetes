# Live FAST-NUCES corpus deployment

On 26 November 2026 (PKT), Minikube namespace `rahbar` switched its API to the nine-document public corpus at `http://rahbar.local`.

## Import and cutover

1. Read 612 records from `rahbar` in the isolated `rahbar-a02-final-chroma` container, backed by Docker volume `rahbar-a02-final-public-index`. Export the IDs, text, metadata and stored embeddings together. Both Chroma servers use `1.0.12`.
2. Use a localhost-only `kubectl --context minikube -n rahbar port-forward svc/chroma` connection to create `rahbar-fast-public` on the live server and add the exported records with their embeddings. Chroma builds the search graph from the stored vectors. OCR, extraction and embedding generation are unnecessary.
3. Read back all 612 records. The IDs, text, record metadata and collection settings matched. The destination has 768-dimensional `nomic-embed-text` vectors, cosine distance and index ID `10d4b233f95f`. The maximum vector component difference was `2e-8`. The live fixture collection `rahbar` kept its original collection ID and 11 records.
4. Build `rahbar-api:fast-public-citations-20261126` with `docker build --tag rahbar-api:fast-public-citations-20261126 ./api` and load it with `& 'C:/minikube/minikube.exe' image load rahbar-api:fast-public-citations-20261126`. A single strategic patch on Deployment `api` changed the `api` container image and added `CHROMA_COLLECTION=rahbar-fast-public`. Revision 11 reached 3/3 Ready. The shared ConfigMap, fixture CronJob and CI configuration stayed on their existing settings. Both live PVCs kept their original bindings.

The UI returned HTTP 200 at `rahbar.local`. A fresh hosted request to `/api/ask` returned the undergraduate minimum CGPA of **2.00** with inline citations from the 2026 Academic Rules Handbook and other public sources. The request ID was `fast-public-migration-smoke-20261126`.

The export, import and verification scripts, `verified-import.json`, `api-public-patch.json`, and `live-smoke.json` are under `%TEMP%\rahbar-fast-migration-20261126-170055` on the deployment workstation. The first copy script's strict float equality assertion failed after the import completed. The read-only `verify-fast-collection.py` check confirmed exact text and metadata matches and verified vectors within float32 precision. Keep the source volume or export available for reproducing the import.

## Reproduce the development deployment

The [default dev setup](../README.md#from-a-clean-clone-to-an-answered-question) is for a fresh cluster. It stages the original documents in the indexer image and applies `k8s/overlays/dev`. The initial Job and weekly CronJob use the same image and command, read `/corpus`, and write `rahbar-fast-public`. Both dev overlays configure the API to read `rahbar-fast-public`. The shared ConfigMap keeps `CHROMA_COLLECTION=rahbar` as the fixture rollback default.

To reproduce the imported deployment, follow the [optional snapshot setup](../README.md#optional-snapshot-setup) using `k8s/overlays/dev-snapshot`. It omits the initial Job and suspends the CronJob. Use this overlay for a deployment that must retain its seeded index. Apply without `--prune` to preserve existing resources.

If a teammate needs the seed file, export it from the existing public collection. Start a port-forward in its own terminal and leave it running for the export:

```powershell
kubectl --context minikube -n rahbar port-forward --address 127.0.0.1 svc/chroma 18000:8000
```

From the repository root on that workstation:

```powershell
python scripts/fast_public_index.py export ../fast-public-index.json
```

Use the same port-forward on the destination, then run `python scripts/fast_public_index.py seed ../fast-public-index.json`. The earlier `source-export.json` also works as the seed file. Share the reviewed public snapshot with the team outside the repository. The helper uploads to staging, reads back IDs, text, metadata, vectors and settings, then publishes the verified collection. An existing matching public collection is verified in place, and a conflicting collection is left intact. A failed import can leave an unpublished staging collection. The API only serves the final name. See [Chroma's stored-embeddings API](https://docs.trychroma.com/docs/collections/add-data).

## Rollback

```powershell
kubectl --context minikube -n rahbar rollout undo deployment/api --to-revision=10
kubectl --context minikube -n rahbar rollout status deployment/api --timeout=360s
```

Revision 10 restores `rahbar-api:cache-guard-v2-20261125` and inherits `CHROMA_COLLECTION=rahbar` from the shared ConfigMap. For a dev deployment from the repository, or when revision 10 has aged out, remove the override to switch the current API image back to the retained fixtures:

```powershell
kubectl --context minikube -n rahbar set env deployment/api CHROMA_COLLECTION-
kubectl --context minikube -n rahbar rollout status deployment/api --timeout=360s
```

Verify an answer cites `academic-rules-2026.md`. This rollback requires an existing `rahbar` fixture collection, as on the original Minikube deployment. A newly seeded dev profile can add a separate fixture collection if it needs that rollback target. Both collections can coexist on the Chroma PVC, and their index IDs isolate Redis cache entries. To return to the seeded public collection using the current API image, run `kubectl --context minikube -n rahbar set env deployment/api CHROMA_COLLECTION=rahbar-fast-public` after verifying the seed.

The original live deployment still uses its locally loaded `rahbar-api:fast-public-citations-20261126` and the API-only override from the cutover. The old live fixture CronJob was suspended separately. The optional snapshot overlay is the repository counterpart for subsequent applies that should keep the seeded index.
