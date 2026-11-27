"""The verification stage with mocked embeddings: no model, no Chroma, no network."""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.models import Document, Page, SourceDoc
from pipeline.verify import (
    check_quality,
    quarantine,
    verify_documents,
    version_key,
    write_report,
)

BODY = ("Students must attend at least eighty percent of the lectures in every course. "
        "A student who falls below this limit is not allowed to sit the final examination. ") * 4


def make_source(tmp: Path, name: str, text: str = BODY, version: str | None = "2026",
                title: str | None = "Rules", doc_id: str | None = None,
                superseded: bool = False, body: bytes | None = None) -> SourceDoc:
    path = tmp / name
    path.write_bytes(body if body is not None else (text + "\n").encode())
    return SourceDoc(path=path, doc_version=version, superseded=superseded, title=title,
                     doc_id=doc_id)


def extract_stub(texts: dict[str, str]):
    def extract(src: SourceDoc) -> Document:
        text = texts.get(src.path.name, BODY)
        if text == "BOOM":
            raise RuntimeError("cannot parse file")
        return Document(src.path.name, src.doc_version or "unknown", src.superseded,
                        [Page(src.path.name, 1, text)])
    return extract


def fake_embed(table: dict[str, list[float]]):
    """Vector chosen by a marker word in the text, so similarity is under the test's control."""
    def embed(texts: list[str]) -> list[list[float]]:
        return [next((v for k, v in table.items() if k in t), [0.0, 0.0, 1.0]) for t in texts]
    return embed


def run(sources, texts=None, embed=None, **kw):
    return verify_documents(sources, extract_stub(texts or {}), lambda d: d, embed, **kw)


def verdict(result, name):
    return next(v for v in result.verdicts if v.source == name)


def test_clean_corpus_passes_all_four_checks(tmp_path: Path) -> None:
    result = run([make_source(tmp_path, "a.md")])
    v = verdict(result, "a.md")
    assert v.action == "index" and all(c.ok for c in v.checks.values())
    assert set(v.checks) == {"quality", "completeness", "recency", "uniqueness"}
    assert "a.md" in result.documents


def test_extraction_failure_is_reported_and_quarantined(tmp_path: Path) -> None:
    result = run([make_source(tmp_path, "bad.md")], texts={"bad.md": "BOOM"})
    v = verdict(result, "bad.md")
    assert v.action == "quarantine" and not v.checks["quality"].ok
    assert "extraction failed" in v.checks["quality"].reason
    assert "cannot parse" in v.checks["quality"].reason
    assert result.documents == {}


def test_empty_and_garbled_text_fail_quality() -> None:
    assert not check_quality("  short ", None, 200)[0].ok
    garbled = "�" * 50 + "abc " * 100
    assert "garbled" in check_quality(garbled, None, 20)[0].reason
    assert "garbled" in check_quality("1234 5678 " * 100, None, 20)[0].reason
    assert check_quality(BODY, None, 200)[0].ok


def test_missing_metadata_is_reported_not_invented(tmp_path: Path) -> None:
    result = run([make_source(tmp_path, "nometa.md", version=None, title=None)])
    v = verdict(result, "nometa.md")
    reason = v.checks["completeness"].reason
    assert v.action == "quarantine"
    assert "title missing" in reason and "version/date missing" in reason
    assert v.title is None and v.doc_version is None      # still empty in the report
    unknown = run([make_source(tmp_path, "unk.md", version="unknown")])
    assert "version/date missing" in verdict(unknown, "unk.md").checks["completeness"].reason


def test_truncated_files_fail_completeness(tmp_path: Path) -> None:
    cut_text = BODY + "and the final examination for"
    sources = [
        make_source(tmp_path, "cut.md", text=cut_text),
        make_source(tmp_path, "cut.pdf", body=b"%PDF-1.4 " + b"x" * 500),
        make_source(tmp_path, "cut.html", body=b"<html><body><p>" + BODY.encode()),
        make_source(tmp_path, "ok.pdf", body=b"%PDF-1.4 " + b"x" * 500 + b"\n%%EOF\n"),
    ]
    result = run(sources, texts={"cut.md": cut_text})
    cut = verdict(result, "cut.md")         # a weak hint alone must not quarantine a document
    assert cut.checks["completeness"].ok and cut.action == "index"
    assert any("mid-sentence" in w for w in cut.warnings)
    assert "%%EOF" in verdict(result, "cut.pdf").checks["completeness"].reason
    assert "</html>" in verdict(result, "cut.html").checks["completeness"].reason
    assert verdict(result, "ok.pdf").checks["completeness"].ok


def test_weak_hint_is_a_warning_but_a_hard_sign_still_fails(tmp_path: Path) -> None:
    cut_text = BODY + "and the final examination for"
    soft = make_source(tmp_path, "soft.md", text=cut_text)
    hard = make_source(tmp_path, "hard.md", text=cut_text)
    hard.size = 5                                          # recorded size says the file was cut
    result = run([soft, hard], texts={"soft.md": cut_text, "hard.md": cut_text})
    assert verdict(result, "soft.md").action == "index"
    assert verdict(result, "hard.md").action == "quarantine"
    assert verdict(result, "hard.md").warnings                # the hint is still reported


def test_recorded_hash_or_size_mismatch_fails_completeness(tmp_path: Path) -> None:
    src = make_source(tmp_path, "a.md")
    src.sha256, src.size = "0" * 64, None
    assert "sha256" in verdict(run([src]), "a.md").checks["completeness"].reason
    src.sha256, src.size = None, 5
    assert "truncated" in verdict(run([src]), "a.md").checks["completeness"].reason


def test_exact_duplicate_detected_by_hash_first_copy_kept(tmp_path: Path) -> None:
    a, b = make_source(tmp_path, "a.md"), make_source(tmp_path, "b.md")
    result = run([a, b], texts={"b.md": BODY.upper()})   # case and whitespace do not matter
    assert verdict(result, "a.md").action == "index"
    v = verdict(result, "b.md")
    assert v.action == "quarantine"
    assert "exact duplicate of a.md" in v.checks["uniqueness"].reason


def test_near_duplicate_detected_by_document_embedding(tmp_path: Path) -> None:
    embed = fake_embed({"alpha": [1.0, 0.0, 0.0], "alpine": [0.999, 0.04, 0.0],
                        "unrelated": [0.0, 1.0, 0.0]})
    sources = [make_source(tmp_path, n) for n in ("a.md", "b.md", "c.md")]
    texts = {"a.md": BODY + " alpha.", "b.md": BODY + " alpine.", "c.md": BODY + " unrelated."}
    result = run(sources, texts=texts, embed=embed, near_dup_threshold=0.95)
    assert verdict(result, "a.md").action == "index"
    assert verdict(result, "c.md").action == "index"
    b = verdict(result, "b.md")
    assert b.action == "quarantine" and "near-duplicate of a.md" in b.checks["uniqueness"].reason
    assert b.nearest["source"] == "a.md"


def test_near_duplicate_check_skipped_without_embeddings_says_so(tmp_path: Path) -> None:
    result = run([make_source(tmp_path, "a.md")], embed=None)
    assert "skipped" in result.near_duplicate_check
    assert "skipped" in verdict(result, "a.md").checks["uniqueness"].reason


def test_newer_version_is_not_a_near_duplicate_and_old_one_is_kept(tmp_path: Path) -> None:
    embed = fake_embed({"alpha": [1.0, 0.0, 0.0]})          # both versions embed identically
    old = make_source(tmp_path, "rules-2023.md", version="2023-08", doc_id="rules")
    new = make_source(tmp_path, "rules-2026.md", version="2026-06", doc_id="rules")
    texts = {"rules-2023.md": BODY + " alpha 75 percent.",
             "rules-2026.md": BODY + " alpha 80 percent."}
    result = run([old, new], texts=texts, embed=embed)
    new_v, old_v = verdict(result, "rules-2026.md"), verdict(result, "rules-2023.md")
    assert new_v.action == "index" and new_v.checks["recency"].ok
    assert old_v.checks["uniqueness"].ok                    # not rejected as a near-duplicate
    assert "superseded by rules-2026.md" in old_v.checks["recency"].reason
    assert not old_v.checks["recency"].ok and old_v.action == "supersede"
    assert result.documents["rules-2023.md"].superseded is True      # kept for A2
    assert (old_v.version_status, old_v.superseded_by) == ("superseded", "rules-2026.md")
    assert (new_v.version_status, new_v.superseded_by) == ("current", None)
    assert result.documents["rules-2026.md"].superseded is False


def test_uniqueness_reason_says_what_was_compared(tmp_path: Path) -> None:
    embed = fake_embed({"alpha": [1.0, 0.0, 0.0], "beta": [0.6, 0.8, 0.0]})
    sources = [make_source(tmp_path, n) for n in ("a.md", "b.md")]
    texts = {"a.md": BODY + " alpha.", "b.md": BODY + " beta."}
    first, second = run(sources, texts=texts, embed=embed).verdicts
    assert "no earlier retained document to compare with" in first.checks["uniqueness"].reason
    assert "nearest document overall: b.md at 0.6000" in first.checks["uniqueness"].reason
    assert "highest similarity to an earlier retained document: a.md at 0.6000" in (
        second.checks["uniqueness"].reason)
    assert "no other document to compare" not in first.checks["uniqueness"].reason


def test_report_counts_ocr_pages_per_document(tmp_path: Path) -> None:
    sources = [make_source(tmp_path, "scan.md"), make_source(tmp_path, "text.md"),
               make_source(tmp_path, "bad.md")]

    def extract(src: SourceDoc) -> Document:
        if src.path.name == "bad.md":
            raise RuntimeError("cannot parse file")
        pages = [Page(src.path.name, 1, BODY), Page(src.path.name, 2, "", ocr=True),
                 Page(src.path.name, 3, BODY, ocr=True)] if src.path.name == "scan.md" \
            else [Page(src.path.name, 1, BODY + " again.")]
        return Document(src.path.name, "2026", False, pages)

    result = verify_documents(sources, extract, lambda d: d, None)
    scan, text, bad = (verdict(result, n) for n in ("scan.md", "text.md", "bad.md"))
    assert (scan.ocr_pages, scan.ocr_pages_empty) == (2, 1)
    assert (text.ocr_pages, text.ocr_pages_empty) == (0, 0)
    assert bad.ocr_pages is None                  # extraction failed: nothing to count
    path = write_report(result, str(tmp_path / "reports"), {})
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["summary"]["ocr_pages"] == 2
    assert next(d for d in report["documents"] if d["source"] == "scan.md")["ocr_pages"] == 2


def test_declared_old_and_new_versions_without_doc_id_are_not_near_duplicates(tmp_path: Path) -> None:
    embed = fake_embed({"alpha": [1.0, 0.0, 0.0]})
    old = make_source(tmp_path, "rules-2023.md", version="2023", superseded=True)
    new = make_source(tmp_path, "rules-2026.md", version="2026")
    texts = {"rules-2023.md": BODY + " alpha 75.", "rules-2026.md": BODY + " alpha 80."}
    assert [v.action for v in run([old, new], texts=texts, embed=embed).verdicts] == ["index", "index"]
    # the same two files with the same version and no superseded flag are near-duplicates
    twin = make_source(tmp_path, "rules-copy.md", version="2026")
    result = run([new, twin], texts={**texts, "rules-copy.md": BODY + " alpha 80!"}, embed=embed)
    assert verdict(result, "rules-copy.md").action == "quarantine"


def test_declared_superseded_document_is_kept_and_passes(tmp_path: Path) -> None:
    src = make_source(tmp_path, "old.md", version="2023", superseded=True)
    v = verdict(run([src]), "old.md")
    assert v.action == "index" and "kept for archival" in v.checks["recency"].reason
    assert v.version_status == "superseded" and v.superseded_by is None   # source-declared


def test_a_failed_newer_version_does_not_supersede_the_older_one(tmp_path: Path) -> None:
    old = make_source(tmp_path, "r-2023.md", version="2023", doc_id="r")
    broken = make_source(tmp_path, "r-2026.md", version="2026", doc_id="r")
    result = run([old, broken], texts={"r-2026.md": "BOOM"})
    assert verdict(result, "r-2023.md").action == "index"
    assert verdict(result, "r-2026.md").action == "quarantine"


def test_documents_without_doc_id_are_never_grouped_as_versions(tmp_path: Path) -> None:
    a = make_source(tmp_path, "annual-report-2019.md", version="2019")
    b = make_source(tmp_path, "annual-report-2023.md", version="2023")
    texts = {"annual-report-2019.md": BODY + " one.", "annual-report-2023.md": BODY + " two."}
    result = run([a, b], texts=texts)
    assert [v.action for v in result.verdicts] == ["index", "index"]


def test_version_key_orders_and_refuses_to_guess() -> None:
    assert version_key("2023-08") < version_key("2026-06") < version_key("2026-06-15")
    assert version_key("2019-20") == (2019, 0, 0)
    assert version_key("unknown") is None and version_key(None) is None


def test_quarantine_moves_failed_files_only_and_report_lists_every_check(tmp_path: Path) -> None:
    lake, qdir = tmp_path / "lake", tmp_path / "quarantine"
    lake.mkdir()
    good, bad = make_source(lake, "good.md"), make_source(lake, "bad.md", title=None)
    result = run([good, bad])
    quarantine(result, str(qdir))
    assert (lake / "good.md").exists() and not (lake / "bad.md").exists()
    assert (qdir / "bad.md").exists()
    assert verdict(result, "bad.md").quarantined_to == str(qdir / "bad.md")

    path = write_report(result, str(tmp_path / "reports"), {"corpus_dir": str(lake)})
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["summary"]["actions"] == {"index": 1, "supersede": 0, "quarantine": 1}
    assert report["summary"]["indexed_versions"] == {"current": 1, "superseded": 0}
    good_doc = next(d for d in report["documents"] if d["source"] == "good.md")
    assert good_doc["version_status"] == "current"
    bad_doc = next(d for d in report["documents"] if d["source"] == "bad.md")
    assert set(bad_doc["checks"]) == {"quality", "completeness", "recency", "uniqueness"}
    assert bad_doc["checks"]["completeness"] == {"status": "fail", "reason": "title missing"}
