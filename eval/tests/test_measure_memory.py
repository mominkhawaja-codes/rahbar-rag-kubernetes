"""The memory-measurement tool's parsing and summary, on realistic cgroup v2 text."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import measure_memory as mm  # noqa: E402

MIB = 1024 * 1024


def test_cgroup_v2_path_is_read_from_proc() -> None:
    text = "0::/system.slice/docker-3f2a9c.scope\n"
    assert mm.cgroup_path_of(text) == "/system.slice/docker-3f2a9c.scope"


def test_cgroup_v1_is_refused() -> None:
    with pytest.raises(ValueError):
        mm.cgroup_path_of("12:memory:/docker/3f2a9c\n11:cpu:/docker/3f2a9c\n")


def test_memory_stat_is_parsed() -> None:
    stat = mm.parse_kv("anon 104857600\nfile 52428800\ninactive_file 41943040\nbogus line\n")
    assert stat == {"anon": 104857600, "file": 52428800, "inactive_file": 41943040}


def test_process_rss_is_read_from_status() -> None:
    status = "Name:\ttesseract\nState:\tR (running)\nVmRSS:\t  245760 kB\n"
    assert mm.rss_kib(status) == ("tesseract", 245760)
    assert mm.rss_kib("Name:\tkworker\n") == ("kworker", 0)     # kernel thread, no RSS


def sample(current: int, anon: int, inactive: int, procs: dict[str, int]) -> dict:
    return {"t": 0.0, "current": current * MIB, "anon": anon * MIB, "file": 0,
            "inactive_file": inactive * MIB,
            "rss_tree": sum(procs.values()) * MIB,
            "by_name": {k: v * MIB for k, v in procs.items()}}


def test_summary_counts_child_processes_and_separates_cache() -> None:
    samples = [
        sample(300, 250, 40, {"python": 240}),
        sample(700, 560, 120, {"python": 260, "tesseract": 300}),   # OCR running
        sample(400, 300, 90, {"python": 280}),
    ]
    s = mm.summarise(samples, memory_peak=720 * MIB)
    assert s["memory_peak_mib"] == 720.0                  # kernel high-water mark
    assert s["working_set_max_mib"] == 580.0              # 700 - 120
    assert s["anon_max_mib"] == 560.0
    assert s["rss_tree_max_mib"] == 560.0                 # python + tesseract together
    assert s["processes_at_rss_peak"] == {"tesseract": 300.0, "python": 260.0}
    assert s["per_process_max_mib"]["python"] == 280.0    # its own peak, a later sample


def test_summary_of_no_samples_says_so() -> None:
    assert mm.summarise([], memory_peak=None) == {"samples": 0}
