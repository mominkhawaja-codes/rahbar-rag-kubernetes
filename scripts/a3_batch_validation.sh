#!/usr/bin/env bash
# Formal native validation of Assignment 03 Parts A1 and A2 (no Docker, no Kubernetes).
#
#   bash scripts/a3_batch_validation.sh [WORK_DIR]
#
# Needs: the repo's Python environment (indexer dependencies), a Chroma 1.0.12 server binary
# (CHROMA_BIN), Ollama running with nomic-embed-text, Tesseract and Poppler (POPPLER_BIN), and
# corpus/raw/ (run corpus/fetch.py). It starts two isolated Chroma servers on ports 8801 and
# 8802 with data under WORK_DIR, never touching any other index, and writes its logs, reports
# and audits to eval/results/a3-batch-validation/. corpus/raw is only read.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
W="${1:-/e/rahbar-a3-work}"
EV="$REPO/eval/results/a3-batch-validation"
CHROMA_BIN="${CHROMA_BIN:-/e/tools/chroma-venv/Scripts/chroma.exe}"
POPPLER_BIN="${POPPLER_BIN:-}"        # folder holding pdftoppm, if it is not already on PATH
PORT_A=8801
PORT_B=8802
QUESTION="For how many years can a faculty start-up use the university's intellectual property at no initial cost?"
OLD_POLICY="faculty-entrepreneurship-and-technical-start-ups-policy.pdf"
NEW_POLICY="faculty-entrepreneurship-policy-2026-revision.pdf"

[ -n "$POPPLER_BIN" ] && export PATH="$PATH:$POPPLER_BIN"
export OLLAMA_URL="http://127.0.0.1:11434" OCR_ENABLED=true CHROMA_HOST=localhost
mkdir -p "$EV" "$W"
say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$EV/run.log"; }

start_chroma() {   # PATH PORT
  mkdir -p "$1"
  (nohup "$CHROMA_BIN" run --path "$1" --port "$2" > "$W/chroma-$2.log" 2>&1 &)
  for _ in $(seq 1 90); do
    curl -s -m 2 "http://localhost:$2/api/v2/heartbeat" > /dev/null && return 0
    sleep 2
  done
  say "Chroma on port $2 did not start"; exit 1
}
stop_chroma() {    # PORT
  local pid
  pid="$(netstat -ano | grep ":$1 " | grep LISTENING | awk '{print $5}' | head -1)"
  [ -n "$pid" ] && taskkill //PID "$pid" //F > /dev/null 2>&1
  sleep 2
}
use_batch() {      # DIR PORT: point the indexer at one isolated working area
  export CORPUS_DIR="$1/lake" STATE_DIR="$1/state" ARCHIVE_DIR="$1/archive" \
         QUARANTINE_DIR="$1/quarantine" CHROMA_PORT="$2"
}
indexer() {        # STEP ARGS...: run the indexer, keeping command, log and reports
  local step="$1"; shift
  mkdir -p "$EV/$step"
  export REPORT_DIR="$EV/$step"
  echo "cd indexer && python -m pipeline.run $*   (CORPUS_DIR=$CORPUS_DIR CHROMA_PORT=$CHROMA_PORT)" > "$EV/$step/command.txt"
  say "$step: python -m pipeline.run $*"
  local start=$SECONDS
  (cd "$REPO/indexer" && python -m pipeline.run "$@") 2>&1 | grep -v "HTTP Request" > "$EV/$step/log.txt"
  echo "exit code ${PIPESTATUS[0]}, $((SECONDS - start)) s" | tee "$EV/$step/exit.txt" | tee -a "$EV/run.log"
}
audit() {          # STEP PORT STATEDIR [extra args]
  local step="$1" port="$2" state="$3"; shift 3
  mkdir -p "$EV/$step"
  echo "python eval/tools/a3_index_audit.py --port $port --state-dir $state $*" >> "$EV/$step/command.txt"
  python "$REPO/eval/tools/a3_index_audit.py" --port "$port" --state-dir "$state" "$@" \
    > "$EV/$step/audit-stdout.json" 2> "$EV/$step/audit-stderr.txt" \
    && say "$step: audit consistent" || say "$step: AUDIT REPORTED A PROBLEM (see $EV/$step)"
}

: > "$EV/run.log"
say "work dir $W; evidence $EV"
[ -d "$W/batch_a" ] && { say "refusing to reuse $W/batch_a: choose an empty WORK_DIR"; exit 1; }

# ---- 1. stage and ingest all nine Batch A documents
use_batch "$W/batch_a" $PORT_A
python "$REPO/corpus/make_batch_b.py" --stage-a "$W/batch_a/lake" | tee -a "$EV/run.log"
start_chroma "$W/batch_a/chroma" $PORT_A
indexer step01-batch-a-ingest
audit step01-batch-a-ingest $PORT_A "$STATE_DIR" --label batch-a-first-ingest \
  --out "$EV/step01-batch-a-ingest/audit.json" --manifest "$W/manifest-batch-a.json"

# ---- 2. repeat unchanged: zero chunk embeddings and zero verification embeddings
indexer step02-batch-a-rerun
audit step02-batch-a-rerun $PORT_A "$STATE_DIR" --label batch-a-rerun \
  --out "$EV/step02-batch-a-rerun/audit.json" --compare "$W/manifest-batch-a.json"

# ---- 3. preserve the Batch A collection, state and results as the baseline
stop_chroma $PORT_A
cp -r "$W/batch_a" "$W/baseline_batch_a"
mkdir -p "$EV/step03-baseline"
cp "$W/manifest-batch-a.json" "$EV/step03-baseline/chunk-manifest-batch-a.json"
cp "$STATE_DIR/index-state.json" "$EV/step03-baseline/index-state-batch-a.json"
( cd "$W/baseline_batch_a" && find . -type f | sort | xargs sha256sum ) > "$EV/step03-baseline/baseline-sha256.txt"
say "baseline preserved at $W/baseline_batch_a ($(wc -l < "$EV/step03-baseline/baseline-sha256.txt") files hashed)"

# ---- 4. an isolated equivalent working index for Batch B, reusing every Batch A embedding
cp -r "$W/baseline_batch_a" "$W/batch_b"
rm -rf "$W/batch_b/reports"
use_batch "$W/batch_b" $PORT_B
start_chroma "$W/batch_b/chroma" $PORT_B
audit step04-batch-b-working-copy $PORT_B "$STATE_DIR" --label batch-b-before \
  --out "$EV/step04-batch-b-working-copy/audit.json" --compare "$W/manifest-batch-a.json"

# ---- 5. stage Batch B and ingest incrementally
python "$REPO/corpus/make_batch_b.py" --stage-b "$W/batch_b/lake" | tee -a "$EV/run.log"
ls "$CORPUS_DIR" > "$W/lake-before-step05.txt"
indexer step05-batch-b-ingest

# ---- 6. check the outcome in the store itself
mkdir -p "$EV/step06-batch-b-audit"
ls "$QUARANTINE_DIR" > "$EV/step06-batch-b-audit/quarantine-listing.txt"
ls "$ARCHIVE_DIR"/*/* > "$EV/step06-batch-b-audit/archive-listing.txt" 2>&1
ls "$CORPUS_DIR" > "$EV/step06-batch-b-audit/lake-listing.txt"
audit step06-batch-b-audit $PORT_B "$STATE_DIR" --label batch-b-after-ingest \
  --out "$EV/step06-batch-b-audit/audit.json" --compare "$W/manifest-batch-a.json" \
  --query "$QUESTION"

# ---- 7. restore the older faculty policy, then the newer version
indexer step07a-restore-older-policy --restore "$OLD_POLICY"
audit step07a-restore-older-policy $PORT_B "$STATE_DIR" --label older-restored \
  --out "$EV/step07a-restore-older-policy/audit.json" --compare "$W/manifest-batch-a.json" \
  --query "$QUESTION"
indexer step07b-restore-newer-policy --restore "$NEW_POLICY"
audit step07b-restore-newer-policy $PORT_B "$STATE_DIR" --label newer-restored \
  --out "$EV/step07b-restore-newer-policy/audit.json" --compare "$W/manifest-batch-a.json" \
  --query "$QUESTION"

# ---- 8. repeat unchanged: zero new embeddings
indexer step08-batch-b-rerun
audit step08-batch-b-rerun $PORT_B "$STATE_DIR" --label batch-b-rerun \
  --out "$EV/step08-batch-b-rerun/audit.json" --compare "$W/manifest-batch-a.json"

stop_chroma $PORT_B
( cd "$W/baseline_batch_a" && find . -type f | sort | xargs sha256sum ) | diff - "$EV/step03-baseline/baseline-sha256.txt" \
  && say "baseline snapshot unchanged after all steps" || say "BASELINE SNAPSHOT CHANGED"
say "done"
