"""Regression checks for the Knowledge Base scraper's generated Markdown.

Generated documents are chunked into fixed-size vectors, so the scraper must not
emit image alt text (one real blog image repeats ~10k characters of its article)
and must carry enough source, date, and lifecycle context that retrieval cannot
present an old post as current guidance.
"""

# Standard library
import importlib.util
import re
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRAPER = _REPO_ROOT / "scripts" / "infrastructure" / "scrape_aws_docs.py"
_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kb_scraper" / "blog_post_with_images.html"
_DOC = {"url": "https://example.invalid/blogs/post/", "output": "post.md", "title": "Synthetic Post"}

# Generated-output bounds. Real sources stay well inside these (longest line
# about 1.1k characters, largest document about 25k characters after cleanup).
_MAX_LINE_CHARS = 2_000
_MAX_DOCUMENT_CHARS = 60_000


@pytest.fixture(scope="module")
def scraper_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scrape_aws_docs", _SCRAPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(scraper_module: ModuleType, tmp_path: Path, html: str, doc: dict | None = None) -> str:
    doc = doc or _DOC
    scraper = scraper_module.DocScraper(tmp_path / "cache", tmp_path / "out")
    with patch.object(scraper, "_fetch_with_etag", return_value=html):
        assert scraper.scrape_doc("gamelift", doc)
    return (tmp_path / "out" / "gamelift" / doc["output"]).read_text(encoding="utf-8")


def test_images_and_alt_text_are_not_emitted(scraper_module, tmp_path):
    text = _render(scraper_module, tmp_path, _FIXTURE.read_text(encoding="utf-8"))
    assert "ALT-SENTINEL" not in text
    assert "diagram.png" not in text and "inline.png" not in text
    assert "[]" not in text  # no empty image-only links left behind


def test_article_text_and_real_links_are_kept_once(scraper_module, tmp_path):
    text = _render(scraper_module, tmp_path, _FIXTURE.read_text(encoding="utf-8"))
    assert text.count("Container fleets run your game server image on managed instances.") == 1
    assert "[developer guide](https://example.invalid/guide)" in text
    assert "Scaling uses target tracking." in text
    assert "Site navigation" not in text and "console.log" not in text


def test_generated_document_stays_within_line_size_and_duplication_bounds(scraper_module, tmp_path):
    text = _render(scraper_module, tmp_path, _FIXTURE.read_text(encoding="utf-8"))
    assert len(text) <= _MAX_DOCUMENT_CHARS
    assert max(len(line) for line in text.splitlines()) <= _MAX_LINE_CHARS
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if len(p.strip()) > 40]
    assert len(paragraphs) == len(set(paragraphs)), "a paragraph is repeated in the generated document"


def test_header_carries_source_date_and_status(scraper_module, tmp_path):
    text = _render(scraper_module, tmp_path, _FIXTURE.read_text(encoding="utf-8"))
    assert text.startswith(
        "# Synthetic Post\n\nSource: https://example.invalid/blogs/post/\n"
        "Published: 2025-03-10\nStatus: Current guidance.\n\n"
    )


def test_header_omits_date_when_page_declares_none(scraper_module, tmp_path):
    text = _render(scraper_module, tmp_path, "<html><body><main role='main'><p>Docs page.</p></main></body></html>")
    assert text.startswith(
        "# Synthetic Post\n\nSource: https://example.invalid/blogs/post/\nStatus: Current guidance.\n\n"
    )
    assert "Published:" not in text


def test_historical_sources_are_labelled_in_content(scraper_module, tmp_path):
    doc = {**_DOC, "lifecycle": "historical"}
    text = _render(scraper_module, tmp_path, _FIXTURE.read_text(encoding="utf-8"), doc)
    status = next(line for line in text.splitlines() if line.startswith("Status: "))
    assert status.startswith("Status: Historical.")
    assert "Current AWS documentation takes precedence." in status


def test_every_source_declares_owner_url_and_reviewed_lifecycle(scraper_module):
    for domain, docs in scraper_module.DOCS_CONFIG.items():
        assert domain in {"gamelift", "eks", "cost"}
        for doc in docs:
            assert doc["url"].startswith("https://"), doc
            assert doc["lifecycle"] in scraper_module.LIFECYCLE_NOTES, doc
            assert re.fullmatch(r"[a-z0-9-]+\.md", doc["output"]), doc
    outputs = [doc["output"] for docs in scraper_module.DOCS_CONFIG.values() for doc in docs]
    assert len(outputs) == len(set(outputs))


def test_beta_agones_adapter_post_is_not_a_kb_source(scraper_module):
    urls = [doc["url"] for docs in scraper_module.DOCS_CONFIG.values() for doc in docs]
    assert not any("fleetiq-adapter-for-agones" in url for url in urls)
