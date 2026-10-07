"""Regression checks for the KB docs scraper's raw-code (template) support."""

# Standard library
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_SCRIPT = Path(__file__).resolve().parents[1] / "infrastructure" / "scrape_aws_docs.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("scrape_aws_docs", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScrapeCodeFormatTests(unittest.TestCase):
    def setUp(self):
        try:
            self.mod = _load_module()
        except ImportError as exc:  # scraper deps (bs4/html2text) not installed in this env
            self.skipTest(f"scraper dependencies unavailable: {exc}")

    def test_code_format_is_stored_as_fenced_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            scraper = self.mod.DocScraper(cache_dir=tmp / "cache", output_dir=tmp / "out")
            doc = {
                "url": "https://example.invalid/template.yml",
                "output": "template.md",
                "title": "Reference Template",
                "format": "code",
                "language": "yaml",
            }
            with patch.object(scraper, "_fetch_with_etag", return_value="Resources:\n  A:\n    Type: AWS::S3::Bucket\n"):
                self.assertTrue(scraper.scrape_doc("gamelift", doc))
            text = (tmp / "out" / "gamelift" / "template.md").read_text(encoding="utf-8")
            self.assertTrue(text.startswith("# Reference Template\n"))
            self.assertIn("```yaml\nResources:\n  A:\n    Type: AWS::S3::Bucket\n```", text)

    def test_gamelift_kb_includes_container_fleet_cloudformation_reference(self):
        outputs = {doc["output"] for doc in self.mod.DOCS_CONFIG["gamelift"]}
        self.assertTrue({"cfn-containerfleet.md", "cfn-containergroupdefinition.md"} <= outputs)
        code_docs = [d for d in self.mod.DOCS_CONFIG["gamelift"] if d.get("format") == "code"]
        self.assertTrue(all(d.get("language") for d in code_docs))

    def test_gamelift_kb_includes_unreal_engine_integration_docs(self):
        outputs = {doc["output"] for doc in self.mod.DOCS_CONFIG["gamelift"]}
        self.assertTrue({"unreal-plugin.md", "unreal-integration.md", "unreal-plugin-container.md"} <= outputs)


if __name__ == "__main__":
    unittest.main()
