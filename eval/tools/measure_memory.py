#!/usr/bin/env python3
"""Run one container and measure its memory from the host's cgroup v2 accounting.

Used by .github/workflows/indexer-memory.yml to measure the real indexer Job image on the
real corpus. Everything the container runs (Python, tesseract, pdftoppm) is in its cgroup,
so the cgroup figures include child processes.

    python eval/tools/measure_memory.py --image rahbar-indexer:mem --name idx \
        --memory 1536m --cpus 1 --network rahbar-net -e CORPUS_DIR=/corpus ... \
        --out memory.json

Recorded, all in MiB:
  memory_peak        memory.peak: the kernel's exact high-water mark of memory.current.
                     Includes page cache, which the kernel reclaims before OOM-killing.
  working_set_max    max over samples of memory.current - inactive_file: what kubelet
                     and `kubectl top` report.
  anon_max           max over samples of anonymous memory: not reclaimable without swap,
                     the part that actually gets a container OOM-killed.
  rss_tree_max       max over samples of the summed VmRSS of every process in the cgroup
                     (overcounts pages shared between processes), and the largest single
                     process by name.
Sampled values can miss a spike shorter than the interval; memory.peak cannot.

Linux, cgroup v2 and Docker only. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

MIB = 1024 * 1024
CGROUP_ROOT = Path("/sys/fs/cgroup")


def parse_kv(text: str) -> dict[str, int]:
    """memory.stat style: one 'key value' per line."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def cgroup_path_of(proc_cgroup_text: str) -> str:
    """The cgroup v2 path from /proc/<pid>/cgroup ('0::/system.slice/docker-x.scope')."""
    for line in proc_cgroup_text.splitlines():
        if line.startswith("0::"):
            return line[3:]
    raise ValueError("no cgroup v2 entry (is this cgroup v1?)")


def rss_kib(status_text: str) -> tuple[str, int]:
    """(process name, VmRSS in KiB) from /proc/<pid>/status; RSS 0 for kernel threads."""
    name, rss = "?", 0
    for line in status_text.splitlines():
        if line.startswith("Name:"):
            name = line.split(None, 1)[1].strip()
        elif line.startswith("VmRSS:"):
            rss = int(line.split()[1])
    return name, rss


def summarise(samples: list[dict[str, Any]], memory_peak: int | None) -> dict[str, Any]:
    if not samples:
        return {"samples": 0}
    biggest = max(samples, key=lambda s: s["rss_tree"])
    per_process: dict[str, int] = {}
    for s in samples:
        for name, rss in s["by_name"].items():
            per_process[name] = max(per_process.get(name, 0), rss)
    mib = lambda b: round(b / MIB, 1)  # noqa: E731
    return {
        "samples": len(samples),
        "memory_peak_mib": mib(memory_peak) if memory_peak is not None else None,
        "current_max_mib": mib(max(s["current"] for s in samples)),
        "working_set_max_mib": mib(max(s["current"] - s["inactive_file"] for s in samples)),
        "anon_max_mib": mib(max(s["anon"] for s in samples)),
        "file_cache_max_mib": mib(max(s["file"] for s in samples)),
        "rss_tree_max_mib": mib(biggest["rss_tree"]),
        "processes_at_rss_peak": {k: mib(v) for k, v in sorted(
            biggest["by_name"].items(), key=lambda kv: -kv[1])[:5]},
        "per_process_max_mib": {k: mib(v) for k, v in sorted(
            per_process.items(), key=lambda kv: -kv[1])[:8]},
    }


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def sample(cg: Path) -> dict[str, Any] | None:
    try:
        current = int((cg / "memory.current").read_text())
        stat = parse_kv((cg / "memory.stat").read_text())
        pids = (cg / "cgroup.procs").read_text().split()
    except (FileNotFoundError, ProcessLookupError, OSError):
        return None                                  # the container has exited
    if not pids:
        return None                                  # cgroup lingers, nothing left in it
    by_name: dict[str, int] = {}
    for pid in pids:
        try:
            name, kib = rss_kib(Path(f"/proc/{pid}/status").read_text())
        except OSError:
            continue
        by_name[name] = by_name.get(name, 0) + kib * 1024
    return {"t": time.monotonic(), "current": current, "anon": stat.get("anon", 0),
            "file": stat.get("file", 0), "inactive_file": stat.get("inactive_file", 0),
            "rss_tree": sum(by_name.values()), "by_name": by_name}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--memory", required=True, help="docker --memory, e.g. 1536m")
    ap.add_argument("--cpus", default="1")
    ap.add_argument("--network", default="bridge")
    ap.add_argument("--tmp-volume", default=None, help="named volume mounted at /tmp")
    ap.add_argument("-e", "--env", action="append", default=[])
    ap.add_argument("--interval", type=float, default=0.2)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    run = ["docker", "run", "-d", "--name", args.name, "--memory", args.memory,
           "--memory-swap", args.memory, "--cpus", args.cpus, "--network", args.network,
           "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
    if args.tmp_volume:
        run += ["-v", f"{args.tmp_volume}:/tmp"]
    for e in args.env:
        run += ["-e", e]
    container = sh(*run, args.image)
    started = time.monotonic()
    pid = sh("docker", "inspect", "-f", "{{.State.Pid}}", container)
    samples: list[dict[str, Any]] = []
    memory_peak: int | None = None
    cg = Path("unknown")
    try:
        cg = CGROUP_ROOT / cgroup_path_of(Path(f"/proc/{pid}/cgroup").read_text()).lstrip("/")
    except (OSError, ValueError) as exc:             # exited before we could look
        print(f"could not find the container's cgroup: {exc}", file=sys.stderr)
    while cg.exists():
        s = sample(cg)
        if s is None:
            break
        samples.append(s)
        try:
            memory_peak = int((cg / "memory.peak").read_text())
        except (FileNotFoundError, OSError):
            pass
        time.sleep(args.interval)

    sh("docker", "wait", container)
    duration = round(time.monotonic() - started, 1)
    state = json.loads(sh("docker", "inspect", "-f", "{{json .State}}", container))
    logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True)
    log_lines = [ln for ln in (logs.stdout + logs.stderr).splitlines() if ln.strip()]
    stages = []
    for ln in log_lines:
        try:
            msg = json.loads(ln).get("msg")
        except ValueError:
            continue
        if msg in ("indexer starting", "corpus chunked", "embedded", "indexer finished"):
            stages.append(json.loads(ln))
    result = {
        "image": args.image, "memory_limit": args.memory, "cpus": args.cpus,
        "interval_s": args.interval, "duration_s": duration,
        "exit_code": state.get("ExitCode"), "oom_killed": state.get("OOMKilled"),
        "cgroup": str(cg), **summarise(samples, memory_peak),
        "stages": stages, "last_log_lines": log_lines[-5:],
        "series": [{"t": round(s["t"] - started, 1), "current_mib": round(s["current"] / MIB, 1),
                    "anon_mib": round(s["anon"] / MIB, 1)} for s in samples[::25]],
    }
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("series", "stages")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
