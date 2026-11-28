# Run Assignment 03 ingestion on Minikube

Use `k8s/overlays/a3` for A1 and A2. It deploys into `rahbar-a3`, with its own
Chroma 1.0.12 StatefulSet and claim and the collection `rahbar-a3`. The
`ragops-data` PVC backs `/data/lake`, `/data/quarantine`, `/data/archive`,
`/data/state` and `/data/reports`. The initial `indexer` Job and weekly
`reindex` CronJob use the same pod template. Both start suspended. The overlay
deploys ingestion resources only. Assignment 02 keeps its own namespace,
configuration and `rahbar.local` Ingress.

These PowerShell commands are for the live validation pass. They assume an
existing running `minikube` profile with a default dynamic StorageClass and
host Ollama reachable from the cluster at `host.minikube.internal:11434`.
Run from the repository root with the nine verified files and `_index.json`
already in `corpus/raw`. The [Batch B notes](../corpus/batch_b/README.md) explain
the generated samples and the shared faculty-policy document ID.

## Prepare the namespace and seed Batch A

Build and load a distinct A3 tag when live work is approved. Use the existing
indexer Dockerfile. The lake will be uploaded from the host.

```powershell
$ErrorActionPreference = 'Stop'
$context = 'minikube'
$minikube = (Get-Command minikube -ErrorAction SilentlyContinue).Source
if (!$minikube) { throw 'Add the Minikube executable to PATH before continuing' }
function Invoke-A3 {
    & kubectl --context $context --namespace rahbar-a3 @args
    if ($LASTEXITCODE -ne 0) { throw "kubectl failed: $($args -join ' ')" }
}

kubectl --context $context get storageclass
ollama list
# If nomic-embed-text is missing, run: ollama pull nomic-embed-text
& $minikube -p $context ssh -- curl -fsS http://host.minikube.internal:11434/api/tags
if ($LASTEXITCODE -ne 0) { throw 'Host Ollama is unreachable from Minikube' }
docker build -t rahbar-indexer:a3 ./indexer
if ($LASTEXITCODE -ne 0) { throw 'Indexer image build failed' }
& $minikube -p $context image load rahbar-indexer:a3
if ($LASTEXITCODE -ne 0) { throw 'Indexer image load failed' }

Invoke-A3 apply -k k8s/overlays/a3
Invoke-A3 rollout status statefulset/chroma --timeout=300s
Invoke-A3 apply -f k8s/overlays/a3/lake-tools.yaml
Invoke-A3 wait --for=condition=Ready pod/lake-tools --timeout=180s
Invoke-A3 get pvc
Invoke-A3 exec lake-tools '--' mkdir -p /data/lake /data/quarantine /data/archive /data/state /data/reports
Invoke-A3 exec lake-tools '--' python -c "from pathlib import Path; assert not any(Path('/data/lake').iterdir())"
```

The last command checks that this is a fresh lake. For later uploads, pause
the schedule and wait for all indexer Jobs to finish. `Forbid` covers Jobs
created by the CronJob controller. It does not serialize manually started Jobs.
Use one writer at a time, including file uploads.

```powershell
$work = Join-Path ([IO.Path]::GetTempPath()) ('rahbar-a3-' + [guid]::NewGuid().ToString('N'))
$stageA = Join-Path $work 'batch-a'
python corpus/make_batch_b.py --stage-a $stageA
if ($LASTEXITCODE -ne 0) { throw 'Batch A preparation failed' }

function Publish-A3LakeFiles($directory) {
    Push-Location $directory
    try {
        foreach ($file in Get-ChildItem -File | Where-Object Name -ne '_index.json') {
            Invoke-A3 cp --no-preserve $file.Name "lake-tools:/data/lake/$($file.Name)"
        }
        Invoke-A3 cp --no-preserve _index.json lake-tools:/data/lake/_index.incoming.json
        Invoke-A3 exec lake-tools '--' mv /data/lake/_index.incoming.json /data/lake/_index.json
    } finally { Pop-Location }
}

Publish-A3LakeFiles $stageA
$resume = Join-Path $work 'resume-job.yaml'
Set-Content -LiteralPath $resume -Encoding Ascii -Value @('spec:', '  suspend: false')
Invoke-A3 patch job/indexer --type=merge --patch-file $resume
Invoke-A3 wait --for=condition=complete job/indexer --timeout=65m
Invoke-A3 logs job/indexer -c indexer
```

Apply the overlay before the initial resume. Reapplying its suspended Job
while it is running would stop that Job's pods. The initial run should produce
612 chunks from nine documents, with zero quarantines. Confirm that the
`ingest finished` log and `/data/state/index-state.json` agree.

## Verify the no-op and add Batch B

In a second terminal, forward only the A3 Chroma Service on localhost.

```powershell
kubectl --context minikube -n rahbar-a3 port-forward --address 127.0.0.1 svc/chroma 18003:8000
```

Back in the first terminal, use a Python environment with the existing
indexer dependencies, including `chromadb-client==1.0.12`, for the audit.

```powershell
function Save-A3State($label) {
    $directory = New-Item -ItemType Directory -Path (Join-Path $work $label)
    Push-Location $directory.FullName
    try {
        Invoke-A3 cp lake-tools:/data/state/index-state.json ./index-state.json
    } finally { Pop-Location }
}

Save-A3State 'state-a'
python eval/tools/a3_index_audit.py --port 18003 --collection rahbar-a3 --state-dir "$work/state-a" --out "$work/audit-a.json" --manifest "$work/chunks-a.json"
if ($LASTEXITCODE -ne 0) { throw 'Batch A audit failed' }

Invoke-A3 create job batch-a-rerun --from=cronjob/reindex
Invoke-A3 wait --for=condition=complete job/batch-a-rerun --timeout=65m
Invoke-A3 logs job/batch-a-rerun -c indexer
```

Confirm that the rerun embeds zero chunks and zero verification texts and
keeps the same `index_id`. For Batch B, start from a fresh copy of the live
manifest so the preparation script preserves the Batch A metadata. Upload
only the Batch B files and publish the merged manifest last.

```powershell
$stageB = New-Item -ItemType Directory -Path (Join-Path $work 'batch-b')
Push-Location $stageB.FullName
try {
    Invoke-A3 cp lake-tools:/data/lake/_index.json ./_index.json
} finally { Pop-Location }
python corpus/make_batch_b.py --stage-b $stageB.FullName
if ($LASTEXITCODE -ne 0) { throw 'Batch B preparation failed' }
Publish-A3LakeFiles $stageB.FullName

Invoke-A3 create job batch-b --from=cronjob/reindex
Invoke-A3 wait --for=condition=complete job/batch-b --timeout=65m
Invoke-A3 logs job/batch-b -c indexer
Save-A3State 'state-b'
python eval/tools/a3_index_audit.py --port 18003 --collection rahbar-a3 --state-dir "$work/state-b" --compare "$work/chunks-a.json" --out "$work/audit-b.json"
if ($LASTEXITCODE -ne 0) { throw 'Batch B audit failed' }
Invoke-A3 exec lake-tools '--' ls -l /data/quarantine /data/archive /data/reports
Push-Location $work
try {
    Invoke-A3 cp lake-tools:/data/reports ./reports
} finally { Pop-Location }
```

The [native reference run](../eval/results/a3-batch-validation/README.md)
accepted five Batch B files, quarantined five, embedded 30 new chunks and
reached 642 total. All 612 original vectors and chunk IDs were retained, with
the seven old faculty-policy chunks marked superseded. Check the audit's
comparison as well as its consistency result.

## Restore and check persistence

Generate lifecycle Jobs from the same suspended CronJob template so they use
the same collection, mounts, permissions and limits. Use unique Job names.

```powershell
function Invoke-A3Lifecycle($name, $action, $file) {
    $job = Invoke-A3 create job $name --from=cronjob/reindex --dry-run=client -o json | ConvertFrom-Json
    $container = $job.spec.template.spec.containers | Where-Object name -eq 'indexer'
    $container | Add-Member -NotePropertyName command -NotePropertyValue @('python', '-m', 'pipeline.run') -Force
    $container | Add-Member -NotePropertyName args -NotePropertyValue @($action, $file) -Force
    $filePath = Join-Path $work "$name.json"
    [IO.File]::WriteAllText($filePath, ($job | ConvertTo-Json -Depth 30))
    Invoke-A3 create -f $filePath
    Invoke-A3 wait --for=condition=complete "job/$name" --timeout=65m
    Invoke-A3 logs "job/$name" -c indexer
}

Invoke-A3Lifecycle 'restore-older-policy' '--restore' 'faculty-entrepreneurship-and-technical-start-ups-policy.pdf'
Save-A3State 'state-restored-older'
python eval/tools/a3_index_audit.py --port 18003 --collection rahbar-a3 --state-dir "$work/state-restored-older" --query "For how many years can a faculty start-up use the university's intellectual property at no initial cost?"
if ($LASTEXITCODE -ne 0) { throw 'Older policy audit failed' }
Invoke-A3Lifecycle 'restore-newer-policy' '--restore' 'faculty-entrepreneurship-policy-2026-revision.pdf'
Save-A3State 'state-restored-newer'
python eval/tools/a3_index_audit.py --port 18003 --collection rahbar-a3 --state-dir "$work/state-restored-newer" --query "For how many years can a faculty start-up use the university's intellectual property at no initial cost?"
if ($LASTEXITCODE -ne 0) { throw 'Newer policy audit failed' }
```

The active-only search should switch between versions. With both versions
still indexed, each restore should flip 14 chunk flags and embed zero chunks.
The same helper supports `--archive`. To finish A2 validation, use a synthetic
document to demonstrate one-file replacement and deletion. Update its stored
hash and size in the lake manifest when editing, and remove its manifest entry
as well as its file when deleting. Run a new Job for each change and compare
its state and chunks to the previous audit. Then recreate the A3 Chroma pod
and run an unchanged Job to check persistence.

```powershell
Invoke-A3 delete pod chroma-0
Invoke-A3 rollout status statefulset/chroma --timeout=300s
Invoke-A3 create job persistence-rerun --from=cronjob/reindex
Invoke-A3 wait --for=condition=complete job/persistence-rerun --timeout=65m
Invoke-A3 logs job/persistence-rerun -c indexer
Invoke-A3 exec lake-tools '--' ls -l /data/lake /data/quarantine /data/archive /data/state /data/reports
```

Restart the A3 port-forward if it closed with the old Chroma pod. The rerun
should report zero embeddings and keep the previous `index_id`. Capture CPU and memory with
`Invoke-A3 top pod -l app=indexer --containers` while a Job is active. The
[integration checklist](../docs/K8S-INTEGRATION-CHECKLIST.md) lists the
additional retry and later API acceptance checks.

When file transfer and evidence collection are finished, close the
port-forward and run `Invoke-A3 delete pod lake-tools`. The PVCs remain. Keep
the CronJob suspended until the controlled validation is finished.

An A3 API can later use the same namespaced `rahbar-config` and Chroma Service.
Give it a writable trace PVC with `TRACE_DIR` set to its mount and retain
stdout traces. An evaluation Job can then reach the separate API Service via
an explicit NetworkPolicy and mount a results PVC. Use port-forwarding or a
separate hostname for the API so Assignment 02 keeps its current Ingress.

## Live validation checkpoint

[The Kubernetes evidence](../eval/results/a3-k8s-batch-a-2026-11-28/README.md)
records the Batch A retry, no-op rerun, Batch B ingestion and document lifecycle
Jobs, same-process cache invalidation, isolated backup restoration,
interrupted-Job recovery, and Chroma pod replacement. After recovery and
replacement, the collection held 642 chunks at index ID `a7ea00b43ec0`, with
the original synthetic hostel policy and the 2026 faculty revision active.
The weekly CronJob remained suspended.
The local Assignment 02 API and UI were temporarily paused during validation.
Keep the restoration settings in the local recovery notes.

## Isolated API for cache invalidation

Use one API pod and one worker in `rahbar-a3`, with `RETRIEVER=chroma`,
`CHROMA_HOST=chroma`, `CHROMA_PORT=8000`, `CHROMA_COLLECTION=rahbar-a3`,
`EMBED_MODEL=nomic-embed-text`, and the same reachable Ollama endpoint as
the indexer. Pin `CACHE_BACKEND=memory` for this single-process proof.
`LLM_PROVIDER=stub` can serve a grounded quote for a suitable question
without credentials. Use the configured hosted provider with a namespaced
Secret if the chosen demonstration question needs real generation.
Allow API egress to DNS, Chroma and Ollama. The existing Chroma ingress
policy admits pods labelled `app: api`. Port-forward the A3 API to localhost.

With the same API process and question, record a cited answer with
`X-Cache: miss`, then `hit`. After a controlled index update, confirm the
new index ID and the next `miss`, followed by `hit` on the new answer.
The retriever refreshes the index ID before cache lookup, and the cache
generation changes with that ID. A restarted pod would also have an empty
in-memory cache, so keep the pod and worker running throughout the check.
Use a dedicated A3 Redis service if the acceptance check needs Redis or
multiple API replicas. Full tracing and evaluation can be configured separately.
