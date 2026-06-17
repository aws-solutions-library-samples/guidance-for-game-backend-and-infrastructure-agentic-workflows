#!/usr/bin/env python3
"""
AWS Documentation Scraper with ETag-based caching.

Downloads AWS documentation and converts to markdown for Knowledge Base ingestion.
Uses HTTP ETags and Last-Modified headers to avoid unnecessary downloads.
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

import requests
from bs4 import BeautifulSoup
import html2text

# Cache duration: 7 days
CACHE_DAYS = 7

# AWS Documentation URLs (HTML only - S3 Vectors has size limits)
DOCS_CONFIG = {
    "gamelift": [
        {
            "url": "https://docs.aws.amazon.com/gamelift/latest/developerguide/gamelift-intro.html",
            "output": "developer-guide.md",
            "title": "GameLift Developer Guide"
        }
    ],
    "eks": [
        {
            "url": "https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html",
            "output": "user-guide.md",
            "title": "EKS User Guide"
        },
        {
            "url": "https://aws.github.io/aws-eks-best-practices/",
            "output": "best-practices.md",
            "title": "EKS Best Practices"
        }
    ],
    "cost": [
        {
            "url": "https://docs.aws.amazon.com/cost-management/latest/userguide/what-is-costmanagement.html",
            "output": "cost-management.md",
            "title": "Cost Management Guide"
        }
    ]
}


class DocScraper:
    """Scrapes AWS documentation with ETag-based caching."""

    def __init__(self, cache_dir: Path, output_dir: Path):
        self.cache_dir = cache_dir
        self.output_dir = output_dir
        self.cache_file = cache_dir / "metadata.json"
        self.cache = self._load_cache()

        # Configure html2text
        self.h2t = html2text.HTML2Text()
        self.h2t.ignore_links = False
        self.h2t.body_width = 0
        self.h2t.ignore_images = False

    def _load_cache(self) -> Dict:
        if self.cache_file.exists():
            try:
                with open(self.cache_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                print(f"⚠️  Failed to load cache: {e}")
        return {}

    def _save_cache(self):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with open(self.cache_file, 'w') as f:
            json.dump(self.cache, f, indent=2)

    def _is_cache_valid(self, cache_key: str) -> bool:
        if cache_key not in self.cache:
            return False

        cached = self.cache[cache_key]
        cached_date = datetime.fromisoformat(cached.get('cached_at', '2000-01-01'))
        age_days = (datetime.now() - cached_date).days

        return age_days < CACHE_DAYS

    def _fetch_with_etag(self, url: str, cache_key: str) -> Optional[str]:
        headers = {}

        if cache_key in self.cache:
            cached = self.cache[cache_key]
            if 'etag' in cached:
                headers['If-None-Match'] = cached['etag']
            if 'last_modified' in cached:
                headers['If-Modified-Since'] = cached['last_modified']

        try:
            response = requests.get(url, headers=headers, timeout=30)

            if response.status_code == 304:
                print(f"  ✅ Not modified (using cache)")
                return None

            if response.status_code == 200:
                self.cache[cache_key] = {
                    'url': url,
                    'etag': response.headers.get('ETag'),
                    'last_modified': response.headers.get('Last-Modified'),
                    'cached_at': datetime.now().isoformat()
                }
                return response.text

            print(f"  ⚠️  HTTP {response.status_code}")
            return None

        except Exception as e:
            print(f"  ❌ Fetch failed: {e}")
            return None

    def _extract_content(self, html: str) -> Optional[str]:
        soup = BeautifulSoup(html, 'html.parser')

        selectors = [
            {'id': 'main-content'},
            {'id': 'main-col-body'},
            {'class': 'awsui-util-container'},
            {'role': 'main'},
            {'class': 'main-content'}
        ]

        for selector in selectors:
            content = soup.find('div', selector) or soup.find('main', selector)
            if content:
                for tag in content.find_all(['nav', 'footer', 'script', 'style']):
                    tag.decompose()
                return str(content)

        body = soup.find('body')
        if body:
            return str(body)

        return None

    def scrape_doc(self, domain: str, doc_config: Dict) -> bool:
        url = doc_config['url']
        output_file = self.output_dir / domain / doc_config['output']
        cache_key = f"{domain}/{doc_config['output']}"

        print(f"\n📄 {doc_config['title']}")
        print(f"   URL: {url}")

        if self._is_cache_valid(cache_key) and output_file.exists():
            print(f"  ✅ Cache valid (age < {CACHE_DAYS} days)")
            return True

        html = self._fetch_with_etag(url, cache_key)

        if html is None and output_file.exists():
            return True

        if html is None:
            if output_file.exists():
                print(f"  ⚠️  Using existing file (fetch failed)")
                return True
            else:
                print(f"  ❌ No cached file available")
                return False

        content_html = self._extract_content(html)
        if not content_html:
            if output_file.exists():
                print(f"  ⚠️  Using existing file (extraction failed)")
                return True
            return False

        try:
            markdown = self.h2t.handle(content_html)
        except Exception as e:
            print(f"  ❌ Conversion failed: {e}")
            if output_file.exists():
                print(f"  ⚠️  Using existing file")
                return True
            return False

        header = f"# {doc_config['title']}\n\n"
        full_content = header + markdown

        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(full_content)

        print(f"  ✅ Downloaded ({len(full_content)} chars)")
        return True

    def scrape_all(self) -> bool:
        success_count = 0
        total_count = sum(len(docs) for docs in DOCS_CONFIG.values())

        for domain, docs in DOCS_CONFIG.items():
            print(f"\n{'='*60}")
            print(f"📚 {domain.upper()} Documentation")
            print(f"{'='*60}")

            for doc in docs:
                if self.scrape_doc(domain, doc):
                    success_count += 1

        self._save_cache()

        print(f"\n{'='*60}")
        print(f"✅ Complete: {success_count}/{total_count} documents")
        print(f"{'='*60}\n")

        return success_count > 0


def main():
    script_dir = Path(__file__).parent
    project_root = script_dir.parent.parent

    cache_dir = project_root / "docs" / ".kb-cache"
    output_dir = project_root / "docs" / "kb-sources"

    print("="*60)
    print("🌐 AWS Documentation Scraper")
    print("="*60)
    print(f"Cache dir: {cache_dir}")
    print(f"Output dir: {output_dir}")
    print(f"Cache duration: {CACHE_DAYS} days")

    scraper = DocScraper(cache_dir, output_dir)
    success = scraper.scrape_all()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
