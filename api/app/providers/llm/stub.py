"""A provider with no model and no network.

Use it for every unit test and for CI (section 6): your tests must never call a
real model. It is deterministic: the same input always gives the same output.
"""

from __future__ import annotations

from ...schemas import Answer, Citation, Passage
from ...services.grounding import SENTENCE_BREAK


class StubProvider:
    name = "stub"

    def generate(self, question: str, passages: list[Passage]) -> Answer:
        if not passages:
            return Answer(
                reasoning="stub: nothing was retrieved",
                answer="(stub) No passages were retrieved for this question.",
                citations=[],
                answered_from_context=False,
                confidence="low",
            )
        top = passages[0]
        excerpt = " ".join(top.text.replace("[", "(").replace("]", ")").split())
        return Answer(
            reasoning="stub: cites the first retrieved passage",
            answer="\n".join(f"{sentence} [1]" for sentence in SENTENCE_BREAK.split(excerpt)),
            citations=[Citation(source=top.source, section=top.section, page=top.page)],
            answered_from_context=True,
            confidence="low",
        )
