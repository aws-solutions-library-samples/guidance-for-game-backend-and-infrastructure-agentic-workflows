"""Regression checks for the Knowledge Base scraper's generated Markdown.

Generated documents are chunked into fixed-size vectors (256 tokens), so the
scraper must not emit image alt text (one real blog image repeats ~10k
characters of its article), every chunk of a non-current source must carry its
lifecycle qualifier, and regenerated or removed sources must not leave stale
files behind.
"""

# Standard library
import importlib.util
import json
import re
from datetime import datetime
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRAPER = _REPO_ROOT / "scripts" / "infrastructure" / "scrape_aws_docs.py"
_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kb_scraper" / "blog_post_with_images.html"
_DOC = {
    "url": "https://example.invalid/blogs/post/",
    "output": "post.md",
    "title": "Synthetic Post",
    "lifecycle": "current",
}

# Generated-output bounds. Current real sources measure a longest line of
# about 1.7k characters and a largest document of about 21k characters.
_MAX_LINE_CHARS = 2_000
_MAX_DOCUMENT_CHARS = 60_000
_QUALIFIER_WINDOW = 800

# Reviewed non-current sources. Changing a label must change this pin.
_EXPECTED_NON_CURRENT = {
    ("gamelift", "blog-unreal-under-1-dollar.md"): "historical",
    ("gamelift", "blog-gamelift-container-fleets.md"): "sample",
    ("eks", "blog-optimize-containers.md"): "historical",
    ("cost", "blog-global-compute-strategy.md"): "historical",
    ("cost", "blog-cost-optimize-minecraft-ec2.md"): "sample",
}


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


def _long_article(paragraphs: int = 40, published: str | None = "2018-02-15") -> str:
    meta = f'<meta property="article:published_time" content="{published}T09:00:00-08:00">' if published else ""
    body = "".join(
        (f"<h2>Section {i}</h2>" if i % 7 == 0 else "")
        + f"<p>Paragraph {i}. "
        + "Spot capacity and FleetIQ placement details are described here at length. " * 6
        + "</p>"
        for i in range(paragraphs)
    )
    return f"<html><head>{meta}</head><body><article><section class='blog-post-content'>{body}</section></article></body></html>"


# ---------------------------------------------------------------------------
# Content cleanup
# ---------------------------------------------------------------------------


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


def test_generated_fixture_passes_the_public_content_scan(scraper_module, tmp_path):
    text = _render(scraper_module, tmp_path, _FIXTURE.read_text(encoding="utf-8"))
    path = tmp_path / "generated.md"
    path.write_text(text, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        "check_public_content", _REPO_ROOT / "scripts" / "check_public_content.py"
    )
    assert spec is not None and spec.loader is not None
    checker = importlib.util.module_from_spec(spec)
    # Standard library
    import sys

    sys.modules.setdefault("check_public_content", checker)  # dataclasses resolve via sys.modules
    spec.loader.exec_module(checker)
    assert checker.scan_text(str(path), text) == []


# ---------------------------------------------------------------------------
# Header and lifecycle
# ---------------------------------------------------------------------------


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


def test_published_date_falls_back_to_time_element(scraper_module, tmp_path):
    html = "<html><body><article><time datetime='2024-08-21T10:00:00Z'>21 AUG 2024</time><p>Body.</p></article></body></html>"
    assert "Published: 2024-08-21\n" in _render(scraper_module, tmp_path, html)


@pytest.mark.parametrize("lifecycle, label", [("historical", "Historical source"), ("sample", "Sample walkthrough")])
def test_every_window_of_a_non_current_document_carries_its_qualifier(scraper_module, tmp_path, lifecycle, label):
    text = _render(scraper_module, tmp_path, _long_article(), {**_DOC, "lifecycle": lifecycle})
    qualifier = f"({label}, published 2018-02-15.)"
    assert len(text) > 5 * _QUALIFIER_WINDOW
    for start in range(0, len(text) - _QUALIFIER_WINDOW, 50):
        assert qualifier in text[start : start + _QUALIFIER_WINDOW], f"no qualifier in window at {start}"
    for line in text.splitlines():
        if line.startswith("## "):
            assert line.endswith(qualifier), line


def test_current_documents_get_no_inline_qualifier(scraper_module, tmp_path):
    text = _render(scraper_module, tmp_path, _long_article())
    assert "(Historical source" not in text and "(Sample walkthrough" not in text


def test_every_source_declares_owner_url_and_a_reviewed_lifecycle(scraper_module):
    labels = {}
    for domain, docs in scraper_module.DOCS_CONFIG.items():
        assert domain in {"gamelift", "eks", "cost"}
        for doc in docs:
            assert doc["url"].startswith("https://"), doc
            assert doc["lifecycle"] in scraper_module.LIFECYCLE_NOTES, doc
            assert re.fullmatch(r"[a-z0-9-]+\.md", doc["output"]), doc
            labels[(domain, doc["output"])] = doc["lifecycle"]
    assert {key: value for key, value in labels.items() if value != "current"} == _EXPECTED_NON_CURRENT
    outputs = [doc["output"] for docs in scraper_module.DOCS_CONFIG.values() for doc in docs]
    assert len(outputs) == len(set(outputs))


def test_a_source_without_a_lifecycle_fails_instead_of_defaulting_to_current(scraper_module, tmp_path):
    doc = {key: value for key, value in _DOC.items() if key != "lifecycle"}
    with pytest.raises(KeyError):
        _render(scraper_module, tmp_path, _FIXTURE.read_text(encoding="utf-8"), doc)


def test_removed_and_moved_sources_stay_out(scraper_module):
    urls = [doc["url"] for docs in scraper_module.DOCS_CONFIG.values() for doc in docs]
    assert not any("fleetiq-adapter-for-agones" in url for url in urls)
    assert not any("fleetiq-and-spot-instances" in url for url in urls)
    assert not any("aws.github.io/aws-eks-best-practices" in url for url in urls)
    assert not any("docs.aws.amazon.com/gamelift/latest/" in url for url in urls)


# ---------------------------------------------------------------------------
# Cache refresh and orphan cleanup
# ---------------------------------------------------------------------------


def _response(status: int, text: str = "", headers: dict | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.text = text
    response.headers = headers or {}
    return response


def test_old_format_cache_entry_is_regenerated_without_conditional_headers(scraper_module, tmp_path):
    cache_dir, out_dir = tmp_path / "cache", tmp_path / "out"
    cache_dir.mkdir()
    old_file = out_dir / "gamelift" / "post.md"
    old_file.parent.mkdir(parents=True)
    old_file.write_text("# Synthetic Post\n\nOld body without a status line.\n", encoding="utf-8")
    # A fresh entry from the previous format: no fingerprint, but an ETag.
    (cache_dir / "metadata.json").write_text(
        json.dumps(
            {"gamelift/post.md": {"url": _DOC["url"], "etag": '"abc"', "cached_at": datetime.now().isoformat()}}
        ),
        encoding="utf-8",
    )
    scraper = scraper_module.DocScraper(cache_dir, out_dir)
    html = _FIXTURE.read_text(encoding="utf-8")
    with patch.object(scraper_module.requests, "get", return_value=_response(200, html, {"ETag": '"def"'})) as get:
        assert scraper.scrape_doc("gamelift", _DOC)
    assert "If-None-Match" not in get.call_args.kwargs["headers"]
    assert "Status: Current guidance." in old_file.read_text(encoding="utf-8")
    assert scraper.cache["gamelift/post.md"]["fingerprint"] == scraper_module.DocScraper._fingerprint(_DOC)


def test_matching_fingerprint_reuses_the_cache(scraper_module, tmp_path):
    scraper = scraper_module.DocScraper(tmp_path / "cache", tmp_path / "out")
    html = _FIXTURE.read_text(encoding="utf-8")
    with patch.object(scraper_module.requests, "get", return_value=_response(200, html)) as get:
        assert scraper.scrape_doc("gamelift", _DOC)
        assert scraper.scrape_doc("gamelift", _DOC)
    assert get.call_count == 1


def test_lifecycle_change_invalidates_the_cache(scraper_module, tmp_path):
    scraper = scraper_module.DocScraper(tmp_path / "cache", tmp_path / "out")
    html = _long_article()
    with patch.object(scraper_module.requests, "get", return_value=_response(200, html)) as get:
        assert scraper.scrape_doc("gamelift", _DOC)
        assert scraper.scrape_doc("gamelift", {**_DOC, "lifecycle": "historical"})
    assert get.call_count == 2
    text = (tmp_path / "out" / "gamelift" / "post.md").read_text(encoding="utf-8")
    assert "Status: Historical." in text


def test_orphaned_files_and_cache_entries_are_pruned(scraper_module, tmp_path):
    out_dir = tmp_path / "out"
    (out_dir / "gamelift").mkdir(parents=True)
    orphan = out_dir / "gamelift" / "blog-gamelift-agones-fleetiq-adapter.md"
    orphan.write_text("old", encoding="utf-8")
    kept = out_dir / "gamelift" / scraper_module.DOCS_CONFIG["gamelift"][0]["output"]
    kept.write_text("kept", encoding="utf-8")
    scraper = scraper_module.DocScraper(tmp_path / "cache", out_dir)
    scraper.cache = {"gamelift/blog-gamelift-agones-fleetiq-adapter.md": {}, f"gamelift/{kept.name}": {}}
    removed = scraper.prune_orphans()
    assert removed == ["gamelift/blog-gamelift-agones-fleetiq-adapter.md"]
    assert not orphan.exists() and kept.exists()
    assert list(scraper.cache) == [f"gamelift/{kept.name}"]


@pytest.mark.parametrize(
    "path",
    [
        "scripts/infrastructure/seed-kb-gamelift.sh",
        "scripts/infrastructure/seed-kb-eks.sh",
        "scripts/infrastructure/seed-kb-cost.sh",
        "scripts/powershell/Private/Invoke-GameAgentKBSeed.ps1",
    ],
)
def test_seeding_removes_documents_that_are_no_longer_generated(path):
    text = (_REPO_ROOT / path).read_text(encoding="utf-8")
    sync = next(line for line in text.splitlines() if "s3 sync" in line)
    block = text[text.index(sync) : text.index(sync) + 300]
    assert "--delete" in block, path
