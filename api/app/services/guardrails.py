"""Part D4: detection rails whose actions are written into the trace.

The Assignment 02 injection defence is unchanged and still does the protecting: passages are
wrapped as numbered <passage> data, tag-like text that could close the element is
neutralised (prompt.py), and the system message forbids following instructions found in
passages. This module adds what that defence did not have: it *notices* injection attempts,
in the question and in retrieved passages, and checks whether the answer repeats an injected
instruction, so each case is visible in the trace and countable by the monitoring script.
Nothing here blocks an answer.
"""

from __future__ import annotations

import re

from ..providers.llm.prompt import TAG_PATTERN

# Phrases that address the model rather than a student. Kept specific to avoid flagging
# ordinary policy text such as "students must follow the instructions of the invigilator".
INJECTION_PATTERNS = [
    r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|your|all|the)\b[^.\n]{0,20}\b(instructions?|rules|prompt|guidelines)\b",
    r"\bnote to (any |the )?(ai|llm|assistant|chatbot|language model)\b",
    r"\b(you are now|act as|pretend to be)\b[^.\n]{0,40}\b(assistant|ai|model|dan)\b",
    r"\b(system prompt|developer message|jailbreak)\b",
    r"\b(tell|say to|inform) (every|all|each|the) (students?|users?)\b[^.\n]{0,80}\b(waived|free|optional|cancelled|not required)\b",
]
_COMPILED = [re.compile(p, re.I) for p in INJECTION_PATTERNS]


def scan_injection(text: str) -> list[str]:
    """The injection phrases found in the text (empty when none)."""
    found = []
    for pattern in _COMPILED:
        for match in pattern.finditer(text or ""):
            found.append(match.group(0).strip())
    return found


def injected_sentence(text: str, match: str) -> str:
    """The sentence that holds the match, used to check whether the answer repeats it."""
    start = (text or "").lower().find(match.lower())
    if start < 0:
        return match
    left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start)) + 1
    right = min([i for i in (text.find(".", start), text.find("\n", start)) if i >= 0]
                or [len(text)])
    return text[left:right + 1].strip()


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def echoes(answer: str, injected: str, n: int = 5) -> bool:
    """True when the answer repeats any n consecutive words of the injected sentence."""
    grams = {tuple(_words(injected)[i:i + n]) for i in range(max(0, len(_words(injected)) - n + 1))}
    words = _words(answer)
    return any(tuple(words[i:i + n]) in grams for i in range(max(0, len(words) - n + 1)))


def neutralised_tags(text: str) -> int:
    """How many tag-like strings prompt.py will replace in this passage."""
    return len(TAG_PATTERN.findall(text or ""))
