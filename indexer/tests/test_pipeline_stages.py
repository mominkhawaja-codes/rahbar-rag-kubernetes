"""The five pipeline stages, without a Chroma server or a model."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from pipeline import embed as embed_module
from pipeline.chunk import chunk, count_tokens, split_sections
from pipeline.clean import clean
from pipeline.config import get_settings
from pipeline.embed import EmbeddingError, embed, embedding_text
from pipeline.extract import extract, find_columns, html_to_text, is_real_table, table_to_markdown
from pipeline.models import Chunk, Document, Page
from pipeline.store import store

FIXTURES = Path(__file__).resolve().parents[2] / "eval" / "fixtures"


def doc(*pages: str, source: str = "rules.pdf") -> Document:
    return Document(source=source, doc_version="2026", superseded=False,
                    pages=[Page(source, i + 1, t) for i, t in enumerate(pages)])


# ---------------------------------------------------------------- extract
def test_markdown_front_matter_is_stripped_and_metadata_kept() -> None:
    d = extract(str(FIXTURES / "academic-rules-2023.md"), "2023", True)
    assert d.superseded is True and d.doc_version == "2023"
    assert not d.pages[0].text.startswith("---")
    assert "## 1. Attendance" in d.pages[0].text


def test_html_keeps_headings_and_tables_and_drops_navigation() -> None:
    raw = """<html><header><nav><a>Home</a><a>Admissions</a></nav></header>
    <h2 class="captions">Fee Structure</h2><ol class="breadcrumb"><li>Home</li></ol>
    <h2>Tuition Fee</h2><p>Payable in   full.</p>
    <table><tr><th>Program</th><th>Fee</th></tr><tr><td>BS</td><td>Rs. 12,000</td></tr></table>
    <script>var x = 1;</script><footer>Copyright</footer></html>"""
    text = html_to_text(raw)
    assert "Home" not in text and "Copyright" not in text and "var x" not in text
    assert "## Tuition Fee" in text and "Payable in full." in text
    assert "| BS | Rs. 12,000 |" in text and "| --- | --- |" in text


def test_tables_become_pipe_tables_without_empty_columns() -> None:
    rows = [["Grade", "", "Points"], ["A+", None, "4.00"], ["B+", "", "3.33"]]
    assert table_to_markdown(rows) == ["| Grade | Points |", "| --- | --- |",
                                       "| A+ | 4.00 |", "| B+ | 3.33 |"]


def test_decorative_boxes_are_not_tables() -> None:
    assert is_real_table([["Grade", "Points"], ["A", "4.00"], ["B", "3.00"]])
    assert not is_real_table([["A whole paragraph of running text " * 4]])   # one column
    assert not is_real_table([["", ""], ["", "x"], ["", ""]])                  # mostly empty


def _words(x0: float, x1: float, n: int) -> list[dict[str, Any]]:
    return [{"x0": x0, "x1": x1, "top": i * 10.0} for i in range(n)]


def test_column_gutters_are_found_and_cut_at_the_emptiest_point() -> None:
    words = _words(40, 200, 30) + _words(224, 230, 3) + _words(240, 400, 30)
    cols = find_columns(words, 0, 450)
    assert len(cols) == 2
    assert 200 < cols[0][1] < 224          # the hanging "4." at x=224 stays in column two


def test_a_single_column_page_is_not_split() -> None:
    assert find_columns(_words(40, 400, 60), 0, 450) == [(0, 450)]


def test_unsupported_types_fail_loudly(tmp_path: Path) -> None:
    bad = tmp_path / "x.docx"
    bad.write_text("x")
    with pytest.raises(RuntimeError):
        extract(str(bad), "1", False)


# ---------------------------------------------------------------- clean
def test_running_headers_footers_and_page_numbers_are_removed() -> None:
    pages = [f"NATIONAL UNIVERSITY OF COMPUTER\n5.{i} Rule number {i}.\n"
             f"Undergraduate Rules 2026 {i}\n{i}" for i in range(1, 6)]
    cleaned = clean(doc(*pages))
    text = "\n".join(p.text for p in cleaned.pages)
    assert "NATIONAL UNIVERSITY" not in text and "Undergraduate Rules" not in text
    assert "5.3 Rule number 3." in text                    # clause numbers survive
    assert not any(line.strip().isdigit() for line in text.splitlines())


def test_cleaning_keeps_headings_and_joins_hyphenated_words() -> None:
    cleaned = clean(doc("## Academic Warning\n5.4 The regis-\ntration rule ’quoted’."))
    text = cleaned.pages[0].text
    assert text.startswith("## Academic Warning")
    assert "registration rule 'quoted'." in text


# ---------------------------------------------------------------- chunk
def test_fixture_chunks_carry_section_page_version_and_superseded() -> None:
    d = clean(extract(str(FIXTURES / "academic-rules-2026.md"), "2026", False))
    chunks = chunk(d, 400, 50)
    sections = [c.section for c in chunks]
    assert "1. Attendance [1.1-1.2]" in sections
    assert "2. Academic warning [2.1-2.2]" in sections
    assert all(c.doc_version == "2026" and c.superseded is False and c.page == 1
               for c in chunks)
    assert len({c.id for c in chunks}) == len(chunks)


def test_chunks_never_cross_a_section_boundary() -> None:
    d = doc("## A\n1.1 Alpha rule.\n## B\n2.1 Beta rule.")
    chunks = chunk(d, 400, 50)
    assert [c.section for c in chunks] == ["A [1.1]", "B [2.1]"]
    assert "Beta" not in chunks[0].text


def test_neighbouring_chunks_overlap() -> None:
    clauses = "\n".join(f"7.{i} " + " ".join(f"w{i}x{j}" for j in range(30)) for i in range(1, 9))
    chunks = chunk(doc("## Long section\n" + clauses), 100, 20)
    assert len(chunks) > 2
    for prev, nxt in zip(chunks, chunks[1:]):
        tail = prev.text.split()[-20:]
        assert nxt.text.split()[:20] == tail
    assert all(count_tokens(c.text) <= 100 for c in chunks)


def test_a_long_clause_is_split_on_sentences_within_the_size() -> None:
    sentence = " ".join(["word"] * 40) + "."
    d = doc("## Huge\n9.1 Every " + " ".join([sentence] * 6))
    chunks = chunk(d, 100, 10)
    assert len(chunks) >= 3
    assert sum(count_tokens(c.text) for c in chunks) >= 242     # nothing was lost
    assert all(count_tokens(c.text) <= 100 for c in chunks)
    assert all(c.section == "Huge [9.1]" for c in chunks)


def test_a_split_table_repeats_its_header_row() -> None:
    rows = "\n".join(f"| Item {i} | Rs. {i},000 |" for i in range(60))
    d = doc("## Fees\n| Charge | Amount |\n| --- | --- |\n" + rows)
    chunks = chunk(d, 80, 10)
    assert len(chunks) > 1
    assert all(c.text.startswith("| Charge | Amount |\n| --- | --- |") for c in chunks)


def test_a_sentence_longer_than_a_chunk_is_cut_on_words() -> None:
    from pipeline.chunk import _split_text

    pieces = _split_text(" ".join(f"w{i}" for i in range(250)), 100, 10)
    assert [len(p.split()) for p in pieces] == [100, 100, 70]
    assert pieces[1].split()[:10] == pieces[0].split()[-10:]
    # a short sentence, then one longer than a chunk: no piece may exceed the size
    mixed = _split_text("Short one here. " + " ".join(f"w{i}" for i in range(180)), 100, 10)
    assert all(len(p.split()) <= 100 for p in mixed)
    assert mixed[0] == "Short one here."


def test_sentences_starting_with_numbers_are_not_clauses() -> None:
    sections = split_sections(doc("## Dean's List\nStudents with an SGPA of 3.50 to\n"
                                  "3.99 would be included in the list."))
    assert sections[0].units[0].clause is None


def test_invalid_sizes_are_rejected() -> None:
    with pytest.raises(ValueError):
        chunk(doc("x"), 50, 50)


# ---------------------------------------------------------------- embed
def test_embed_batches_prefixes_and_checks_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = httpx.Request("POST", "/", content=request.content).read()
        import json
        payload = json.loads(body)
        seen.append(payload)
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3]] * len(payload["input"])})

    real_client = httpx.Client
    monkeypatch.setattr(embed_module.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    vectors = embed([f"text {i}" for i in range(70)], get_settings())
    assert len(vectors) == 70
    assert [len(p["input"]) for p in seen] == [32, 32, 6]
    assert all(p["model"] == "nomic-embed-text" for p in seen)
    assert seen[0]["input"][0] == "search_document: text 0"


def test_embed_fails_when_the_model_returns_too_few_vectors(
        monkeypatch: pytest.MonkeyPatch) -> None:
    real_client = httpx.Client
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"embeddings": [[0.1]]}))
    monkeypatch.setattr(embed_module.httpx, "Client",
                        lambda **kw: real_client(transport=transport))
    with pytest.raises(EmbeddingError):
        embed(["a", "b"], get_settings())


def test_embedding_text_includes_document_and_section() -> None:
    c = Chunk(id="x", text="5.4 Minimum CGPA is 2.00.", source="academic-rules-2026.pdf",
              section="Academic Warning [5.4]", page=24, doc_version="2026", superseded=False)
    assert embedding_text(c) == "academic rules 2026 | Academic Warning [5.4]\n5.4 Minimum CGPA is 2.00."


# ---------------------------------------------------------------- store
class Col:
    def __init__(self, name: str, metadata: dict) -> None:
        self.name, self.metadata, self.rows = name, metadata, {}
        self.client: SwapClient | None = None

    def add(self, ids, documents, embeddings, metadatas) -> None:   # type: ignore[no-untyped-def]
        for i, d, e, m in zip(ids, documents, embeddings, metadatas):
            self.rows[i] = (d, e, m)

    def count(self) -> int:
        return len(self.rows)

    def modify(self, name: str) -> None:
        assert self.client is not None
        del self.client.cols[self.name]
        self.name = name
        self.client.cols[name] = self


class SwapClient:
    def __init__(self) -> None:
        self.cols: dict[str, Col] = {}

    def create_collection(self, name: str, metadata: dict) -> Col:
        col = Col(name, metadata)
        col.client = self
        self.cols[name] = col
        return col

    def get_collection(self, name: str) -> Col:
        return self.cols[name]

    def delete_collection(self, name: str) -> None:
        if name not in self.cols:
            raise ValueError(name)
        del self.cols[name]


def _chunks(n: int) -> list[Chunk]:
    return [Chunk(id=f"d-{i:04d}", text=f"t{i}", source="d.pdf", section="S", page=1,
                  doc_version="2026", superseded=i % 2 == 0) for i in range(n)]


def test_store_records_the_pin_and_swaps_the_new_index_in() -> None:
    client = SwapClient()
    settings = get_settings()
    assert store(_chunks(3), [[0.1, 0.2]] * 3, settings, client=client) == 3
    first_id = client.cols["rahbar"].metadata["index_id"]
    assert store(_chunks(5), [[0.1, 0.2]] * 5, settings, client=client) == 5
    assert set(client.cols) == {"rahbar"}                 # no staging or previous left over
    live = client.cols["rahbar"]
    assert live.count() == 5
    assert live.metadata["embed_model"] == settings.embed_model
    assert live.metadata["dimension"] == 2
    assert live.metadata["hnsw:space"] == "cosine"
    assert live.metadata["index_id"] != first_id
    _, _, meta = live.rows["d-0000"]
    assert set(meta) == {"source", "section", "page", "doc_version", "superseded"}


def test_store_refuses_mismatched_inputs_and_keeps_the_live_index() -> None:
    client = SwapClient()
    settings = get_settings()
    store(_chunks(2), [[0.1]] * 2, settings, client=client)
    with pytest.raises(ValueError):
        store(_chunks(3), [[0.1]] * 2, settings, client=client)
    with pytest.raises(ValueError):
        store([], [], settings, client=client)
    assert client.cols["rahbar"].count() == 2
