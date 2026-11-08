# Image and dependency security

How images are scanned, what the scans found on 8 Nov 2026, what was fixed, and what
remains. Trivy's database changes daily, so a clean scan today is not a promise for
next month: CI re-scans on every push, and Dependabot proposes base-image, action and
pip updates weekly.

## The gate

`.github/workflows/ci.yml`, job `images`, builds each of the three images once and
runs Trivy twice on it:

1. a report of every vulnerability that has a fix, any severity (exit 0, for the log);
2. the **gate**: any **HIGH or CRITICAL vulnerability with a published fix** fails the
   job (`severity: CRITICAL,HIGH`, `ignore-unfixed: true`, `exit-code: 1`).

Unfixed CVEs do not fail the build, because there is nothing to upgrade to. They are
listed below when they matter. The end-to-end job deploys exactly the images that passed
the gate (saved as artifacts), and `cd.yml` scans each image again, as built there,
before pushing it to Docker Hub.

All images are pinned by tag **and digest**, and every action by commit SHA. Builds also
run `apt-get upgrade` / `apk upgrade`, so OS fixes published after the pinned digest are
included.

## What failed in CI, and why

| Image | CI result (run 42487659908) | Actual cause |
| --- | --- | --- |
| ui | **failed** at the Trivy step | `nginxinc/nginx-unprivileged:1.27.4-alpine` is Alpine 3.21.3, **end of life**: 67 fixable HIGH/CRITICAL (zlib CVE-2026-22184 and CVE-2026-85091, OpenSSL CVE-2026-31789 CRITICAL, brotli CVE-2026-111497 CRITICAL, curl, expat, libpng, libxml2, musl, pcre2, ...) |
| api | cancelled | Not checked: the matrix's default `fail-fast` cancelled it when ui failed. A local scan showed it would also have failed: base `python:3.12.10-slim-bookworm` had 54 fixable HIGH/CRITICAL (GnuTLS CRITICAL, apt, krb5, ...), and `starlette 0.46.2` (via FastAPI 0.115.12) had 3 HIGH. |
| indexer | cancelled | Same base image, plus `pypdf 5.4.0` (20 HIGH, 43 MEDIUM), `Pillow 12.2.0` (CRITICAL), `pdfminer.six 20250327` (HIGH). |

The first run (b219072) failed earlier, on a configuration error: the workflow named
`aquasecurity/trivy-action@0.28.0`, a version that does not exist. That was fixed in
60b4463.

## What changed

| Where | Before | After | Fixable HIGH/CRITICAL after (local Trivy 0.76.2) |
| --- | --- | --- | --- |
| `ui/Dockerfile` | nginx-unprivileged 1.27.4-alpine (Alpine 3.21, EOL) | 1.30.6-alpine3.24-slim + `apk upgrade` | base 3 before upgrade; all 3 have fixed packages in the Alpine 3.24 index (musl 1.2.6-r3, zlib 1.4-r0), which `apk upgrade` installs |
| `api/Dockerfile`, `indexer/Dockerfile` | python:3.12.10-slim-bookworm | python:3.12.16-slim-bookworm + `apt-get upgrade` | 0 (base) |
| `api/requirements.txt` | fastapi 0.115.12 (starlette 0.46.2) | fastapi 0.162.0, starlette 1.10.0 | 0 (all 47 resolved packages) |
| `indexer/requirements.txt` | pdfplumber 0.11.6, pypdf 5.4.0, Pillow 12.2.0 | pdfplumber 0.11.10 (pdfminer.six 20260107), pypdf 6.21.4, Pillow 13.0.0 | 0 (all 52 resolved packages) |
| `k8s/base/redis.yaml` | redis:7.4.2-alpine (Alpine 3.21, EOL) | redis:8.8.4-alpine3.23 | 0 (was 84, 60 of them in gosu's Go stdlib) |

How the local numbers were produced: Trivy 0.76.2 (checksum-verified binary), scanning
base images straight from the registry (`--image-src remote`), and scanning the
*complete* pip dependency set resolved for Linux/CPython 3.12 (`pip install --dry-run
--report`), not just the direct pins. The final images, with their OS packages for
Tesseract and poppler, can only be scanned after a build: the CI gate does that.

All 83 unit tests pass on the upgraded stack. Re-extracting the 2026 handbook with
pdfplumber 0.11.10 gives the same 73 chunks and section labels as before.

## Images we run but do not build

| Image | Status | Note |
| --- | --- | --- |
| `chromadb/chroma:1.0.12` | see below | Must match `chromadb-client==1.0.12` in both Python services. Upgrading it means upgrading the client too, and re-testing the API against the new server. |
| `ollama/ollama:0.5.7` | CI only | Used for embeddings inside the throwaway kind cluster. Never deployed by the dev or prod overlays. |

**Chroma 1.0.12: not fully scanned.** Trivy 0.76.2 timed out twice (20 and 8 minutes)
analysing the image's 113 MB Rust server binary (`usr/local/bin/chroma`), so we have no
result for it, and none for its OS layer either. This is an open item, not a pass. Its
exposure is limited: it is a ClusterIP Service reachable only from the api and indexer
pods (NetworkPolicy `chroma-ingress`), runs as a non-root user with all capabilities
dropped, and stores only the public-document index. Next step: scan it in CI with a
longer timeout, and move to a newer Chroma server *together with* `chromadb-client`.

**A Chroma configuration failure found by the CI e2e job:** Kubernetes injects
`CHROMA_PORT=tcp://<ip>:8000` into every pod for the `chroma` Service. Chroma 1.x reads
all `CHROMA_*` variables as configuration and panics on that value. All pods now set
`enableServiceLinks: false`; they find services through DNS.
