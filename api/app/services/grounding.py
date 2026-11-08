"""Grounding guards around generation (section 3.4).

* `apply_relevance_floor`: passages below RELEVANCE_FLOOR never reach the model.
* `check_citations`: every citation must name a passage the model was actually given.
  Citations are matched on (source, section) after normalising case and spacing; a
  citation whose section is a prefix of exactly one retrieved section is mapped onto
  it (models often drop the "[5.4-5.6]" suffix). Anything else is dropped. An answer
  that claims to come from the context but keeps no valid citation is not trusted.
* `abstention` and `fallback`: the two honest "no" answers.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..schemas import Answer, Citation, Passage

ABSTAIN_TEXT = ("I could not find this in the university documents I have, so I can't "
                "answer it reliably. Please check with the relevant office.")
FALLBACK_TEXT = ("I couldn't produce a reliable answer right now. Please try again in a "
                 "moment.")


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def apply_relevance_floor(passages: list[Passage], floor: float) -> list[Passage]:
    return [p for p in passages if p.score >= floor]


@dataclass
class CitationCheck:
    valid: list[Citation]
    dropped: int


def check_citations(citations: list[Citation], passages: list[Passage]) -> CitationCheck:
    by_key = {(_norm(p.source), _norm(p.section)): p for p in passages}
    valid: list[Citation] = []
    seen: set[tuple[str, str]] = set()
    dropped = 0
    for c in citations:
        key = (_norm(c.source), _norm(c.section))
        match = by_key.get(key)
        if match is None and key[1]:
            prefixed = [p for (src, sec), p in by_key.items()
                        if src == key[0] and sec.startswith(key[1])]
            match = prefixed[0] if len(prefixed) == 1 else None
        if match is None:
            dropped += 1
            continue
        canonical = (match.source, match.section)
        if canonical in seen:
            continue
        seen.add(canonical)
        valid.append(Citation(source=match.source, section=match.section, page=match.page))
    return CitationCheck(valid=valid, dropped=dropped)


def abstention(reason: str) -> Answer:
    return Answer(reasoning=f"declined: {reason}", answer=ABSTAIN_TEXT, citations=[],
                  answered_from_context=False, confidence="low")


def fallback(reason: str) -> Answer:
    return Answer(reasoning=f"fallback: {reason}", answer=FALLBACK_TEXT, citations=[],
                  answered_from_context=False, confidence="low")
