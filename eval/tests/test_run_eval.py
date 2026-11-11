"""The evaluation's own logic: section matching, recall@k, citation validity, the gate."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_eval as ev  # noqa: E402


def ref(source: str, section: str, score: float = 0.8) -> dict:
    return {"chunk_id": f"{source}-{section}", "source": source, "section": section,
            "score": score}


@pytest.mark.parametrize("golden,retrieved,expected", [
    ("5.4", "Academic Warning/Probation [5.4-5.7]", True),
    ("5.10", "Repeating a Course [5.8-5.11]", True),        # 10 > 8 numerically, not as text
    ("5.12", "Repeating a Course [5.8-5.11]", False),
    ("2.3", "Grade Points [2.3]", True),
    ("1. Attendance", "1. Attendance [1.1-1.2]", True),
    ("Tuition Fee", "Tuition Fee (w.e.f. Fall 2026 semester)", True),
    ("1. Attendance", "2. Academic warning [2.1-2.2]", False),
    ("", "anything", False),
])
def test_section_matching(golden: str, retrieved: str, expected: bool) -> None:
    assert ev.section_matches(golden, retrieved) is expected


def test_recall_at_k_counts_only_the_top_k() -> None:
    relevant = [{"source": "a.pdf", "section": "5.4"}, {"source": "b.html", "section": "Fees"}]
    retrieved = [ref("a.pdf", "Warning [5.4-5.7]"), ref("x.pdf", "X"), ref("x.pdf", "Y"),
                 ref("b.html", "Fees")]
    assert ev.recall_at_k(relevant, retrieved, 3) == 0.5
    assert ev.recall_at_k(relevant, retrieved, 5) == 1.0
    assert ev.recall_at_k([], retrieved, 5) is None


def test_recall_requires_the_right_source() -> None:
    relevant = [{"source": "academic-rules-2026.md", "section": "1. Attendance"}]
    superseded = [ref("academic-rules-2023.md", "1. Attendance [1.1-1.2]")]
    assert ev.recall_at_k(relevant, superseded, 5) == 0.0


def test_citation_validity() -> None:
    resp = {"retrieved": [ref("a.pdf", "S [1.1]")],
            "citations": [{"source": "a.pdf", "section": "S [1.1]"},
                          {"source": "made-up.pdf", "section": "Z"}]}
    assert ev.citation_validity(resp) == 0.5
    assert ev.citation_validity({"retrieved": [], "citations": []}) is None


def _row(qid: str, qtype: str, answered: bool, retrieved: list[dict], relevant: list[dict],
         citations: list[dict] | None = None, must_abstain: bool = False,
         expect: str | None = None, answer: str = "") -> dict:
    return {"id": qid, "type": qtype, "must_abstain": must_abstain, "relevant": relevant,
            "expect": expect, "seconds": 0.1,
            "response": {"answer": answer, "answered_from_context": answered,
                         "retrieved": retrieved, "citations": citations or []}}


def test_summarise_and_gate_pass_on_a_good_run() -> None:
    hit = ref("a.pdf", "Fees [1.1]")
    rows = [
        _row("q1", "exact", True, [hit], [{"source": "a.pdf", "section": "1.1"}],
             [{"source": "a.pdf", "section": "Fees [1.1]"}], expect="12,000",
             answer="It is Rs. 12000."),
        _row("q2", "abstain", False, [ref("a.pdf", "Other", 0.2)], [], must_abstain=True),
    ]
    results = ev.summarise(rows)
    assert results["recall@5"] == 1.0 and results["recall@5_by_type"] == {"exact": 1.0}
    assert results["citation_validity"] == 1.0
    assert results["abstention_correctness"] == 1.0
    assert results["expect_found_rate"] == 1.0
    assert ev.gate(results, {"recall@5": 0.98}) == []


def test_gate_fails_on_recall_drop_bad_citations_and_missed_abstention() -> None:
    rows = [
        _row("q1", "ordinary", True, [ref("x.pdf", "Wrong")],
             [{"source": "a.pdf", "section": "1.1"}],
             [{"source": "invented.pdf", "section": "9"}]),
        _row("q2", "abstain", True, [ref("a.pdf", "Hostel?")], [], must_abstain=True,
             citations=[{"source": "a.pdf", "section": "Hostel?"}]),
    ]
    failures = ev.gate(ev.summarise(rows), {"recall@5": 0.8})
    assert len(failures) == 3
    assert any("recall@5" in f for f in failures)
    assert any("citation validity" in f for f in failures)
    assert any("q2" in f for f in failures)


def test_a_failed_request_is_neither_a_correct_nor_a_false_abstention() -> None:
    error = {"answer": "", "citations": [], "retrieved": [], "answered_from_context": False,
             "error": "HTTPStatusError: 503"}
    rows = [
        {"id": "q17", "type": "abstain", "must_abstain": True, "relevant": [], "expect": None,
         "seconds": 0.5, "response": dict(error)},
        {"id": "q01", "type": "ordinary", "must_abstain": False, "expect": "2.00",
         "relevant": [{"source": "a.pdf", "section": "5.4"}], "seconds": 0.5,
         "response": dict(error)},
    ]
    results = ev.summarise(rows)
    assert results["abstention_correctness"] == 0.0       # an outage is not an honest refusal
    assert results["false_abstention_rate"] == 0.0        # nor a false abstention
    assert results["errors"] == 2
    assert any("q17" in f for f in ev.gate(results, None))


def test_recall_within_five_points_of_baseline_passes() -> None:
    assert ev.gate({"recall@5": 0.76, "citation_validity": None,
                    "abstention_correctness": None}, {"recall@5": 0.80}) == []


def test_the_real_golden_set_has_the_required_mix() -> None:
    questions = ev.load_golden(Path(__file__).resolve().parents[1] / "golden.yaml")
    types = [q.get("type") for q in questions]
    assert len(questions) >= 15
    assert types.count("abstain") >= 2 and types.count("injection") >= 1
    assert types.count("version") >= 2 and types.count("exact") >= 2
    assert all(q.get("must_abstain") for q in questions if q.get("type") == "abstain")
    assert len({q["id"] for q in questions}) == len(questions)
