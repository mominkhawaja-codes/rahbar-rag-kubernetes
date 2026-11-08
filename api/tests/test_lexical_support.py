"""Evidence-based abstention: a passage that never mentions what the question is about
cannot support an answer, however high its cosine similarity.

The fixture checks use the real CI fixture documents and golden questions, so this is
the same situation the e2e gate measures, without any question-specific code."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from conftest import FixedRetriever, ScriptedProvider, grounded_answer, passage

from app.config import Settings
from app.schemas import Passage
from app.services.answer import answer_question
from app.services.grounding import content_terms, has_lexical_support

FIXTURES = Path(__file__).resolve().parents[2] / "eval" / "fixtures"


def fixture_passages(score: float = 0.6) -> list[Passage]:
    """Every '## ' section of every fixture document, as a retrieved passage that has
    cleared the relevance floor: the worst case, where similarity says yes to all."""
    out = []
    for doc in sorted(FIXTURES.glob("*.md")):
        if doc.name.lower().startswith("readme"):
            continue
        for i, block in enumerate(re.split(r"^## ", doc.read_text(encoding="utf-8"), flags=re.M)[1:]):
            heading, _, body = block.partition("\n")
            out.append(Passage(chunk_id=f"{doc.stem}-{i}", text=body.strip(), source=doc.name,
                               section=heading.strip(), page=1, score=score))
    return out


GOLDEN = yaml.safe_load((FIXTURES / "golden.fixture.yaml").read_text(encoding="utf-8"))["questions"]


@pytest.mark.parametrize("q", [q for q in GOLDEN if q.get("must_abstain")], ids=lambda q: q["id"])
def test_must_abstain_fixture_questions_have_no_lexical_support(q: dict) -> None:
    assert not has_lexical_support(q["question"], fixture_passages())


@pytest.mark.parametrize("q", [q for q in GOLDEN if not q.get("must_abstain")],
                         ids=lambda q: q["id"])
def test_answerable_fixture_questions_keep_their_support(q: dict) -> None:
    relevant = {r["section"] for r in q["relevant"]}
    supporting = [p for p in fixture_passages() if p.section in relevant]
    assert supporting, "the golden set names a section the fixtures do not have"
    assert has_lexical_support(q["question"], supporting)


def test_unsupported_question_is_declined_without_calling_the_model() -> None:
    passages = [passage(1, 0.62, section="1. Attendance [1.1]",
                        text="1.1 A student must attend at least 80 percent of the lectures.")]
    provider = ScriptedProvider(grounded_answer(passages[0]))
    r = answer_question("How do I apply for a hostel room?", FixedRetriever(passages), provider,
                        Settings(relevance_floor=0.45, cache_backend="none"))
    assert r.answered_from_context is False and r.citations == []
    assert provider.calls == []
    assert [p.chunk_id for p in r.retrieved] == ["c1"]      # still reported for recall


def test_word_forms_count_as_support() -> None:
    p = [passage(1, 0.6, section="Repeating a Course [5.8]",
                 text="5.9 A student may repeat any course to improve CGPA.")]
    assert has_lexical_support("If I repeated a course, is my grade capped?", p)
    assert has_lexical_support("What are the fees?", [passage(2, 0.6, text="The fee is Rs 300.")])


def test_generic_words_do_not_count_as_support() -> None:
    assert content_terms("How can students apply under the current university rules?") == set()
    p = [passage(1, 0.6, text="Students must apply under the university rules.")]
    assert not has_lexical_support("How do students apply for hostel accommodation?", p)
