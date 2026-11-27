"""The shapes passed between stages. Metadata fields are required (section 3.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SourceDoc:
    """One file in the corpus plus what its source says about it. A field the source does
    not state stays None: verification reports it as missing instead of inventing it."""

    path: Path
    doc_version: str | None
    superseded: bool
    title: str | None = None
    doc_id: str | None = None      # explicit id shared by the versions of one document
    sha256: str | None = None      # expected hash of the file, from _index.json
    size: int | None = None        # expected size in bytes, from _index.json


@dataclass
class Page:
    """Text of one page, as extraction produced it."""

    source: str              # file name, e.g. Academic_Rules_Handbook_2026.pdf
    page: int                # 1-based
    text: str
    ocr: bool = False        # True when this page had no text layer and was OCR'd


@dataclass
class Document:
    source: str
    doc_version: str         # e.g. "2026-06"; from corpus/sources.yaml
    superseded: bool         # True for an older version of a policy
    pages: list[Page] = field(default_factory=list)


@dataclass
class Chunk:
    id: str                  # stable: same document + same position gives the same id
    text: str
    source: str
    section: str             # the heading this chunk sits under, e.g. "5.4 Academic Warning"
    page: int
    doc_version: str
    superseded: bool
    doc_id: str = ""          # stable document id (Part A2); empty for the dummy index
    content_hash: str = ""    # sha256 of the source file this chunk came from

    def metadata(self) -> dict[str, str | int | bool]:
        """Chroma metadata values must be str, int, float or bool."""
        return {
            "source": self.source,
            "section": self.section,
            "page": self.page,
            "doc_version": self.doc_version,
            "superseded": self.superseded,
            **({"doc_id": self.doc_id} if self.doc_id else {}),
            **({"content_hash": self.content_hash} if self.content_hash else {}),
        }
