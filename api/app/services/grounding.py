"""Grounding guards around generation (section 3.4).

* `apply_relevance_floor`: passages below RELEVANCE_FLOOR never reach the model.
* `has_lexical_support`: at least one of those passages must mention something the
  question is about; otherwise the API declines without calling the model.
* `check_citations`: every citation must name a passage the model was actually given.
  Citations are matched on (source, section) after normalising case and spacing; a
  citation whose section is a prefix of exactly one retrieved section is mapped onto
  it (models often drop the "[5.4-5.6]" suffix). Anything else is dropped. An answer
  that claims to come from the context but keeps no valid citation is not trusted.
* `abstention` and `fallback`: the two honest "no" answers.
"""

from __future__ import annotations

import re
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


# Words that say nothing about *what* is asked: function words, question words, and
# terms that appear in nearly every passage of a university-rules corpus.
_STOPWORDS = frozenset("""
a an the and or but if then so of to in on at by for with from into about as is are was
were be been being do does did done can could should would will shall may might must
i me my mine we our you your he she they them their it its this that these those there
here what which who whom whose when where why how much many any some all each every per
under over after before between during within without than same other such more most
less least first last until upon via
not no yes please tell say said know want need get got give make let also just only very
apply applying applied ask asking allowed allow eligible required require
student students university fast nuces campus rule rules policy policies regulation
regulations current currently now new old previous instructions ignore document documents
""".split())


def content_terms(text: str) -> set[str]:
    """The words of `text` that carry its topic, lower-cased, at least 3 letters."""
    words = re.findall(r"[a-z][a-z0-9-]{2,}", text.lower())
    return {w.strip("-") for w in words if w not in _STOPWORDS}


def _same_word(a: str, b: str) -> bool:
    """Crude stemming: "fee"/"fees", "repeat"/"repeating" (prefix), "repeated"/"repeating"
    (same first five letters)."""
    return a.startswith(b) or b.startswith(a) or (len(a) >= 5 and a[:5] == b[:5])


def has_lexical_support(question: str, passages: list[Passage]) -> bool:
    """True when at least one passage mentions at least one of the question's content
    terms (allowing simple word forms, see `_same_word`).

    Dense similarity alone cannot tell "same subject area" from "answers the question":
    a hostel question scores about as high against attendance clauses as an attendance
    question does, because all of them are university rules. A passage that never
    mentions anything the question is about cannot be evidence for an answer, whatever
    its cosine score, so the API declines instead of asking the model. The test is
    deliberately lenient (one shared term is enough); deciding whether the evidence
    really answers the question stays the model's job."""
    terms = content_terms(question)
    if not terms:
        return True            # nothing to check against; leave it to the floor and the model
    for p in passages:
        words = content_terms(f"{p.section} {p.text}")
        if any(_same_word(t, w) for t in terms for w in words):
            return True
    return False


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
