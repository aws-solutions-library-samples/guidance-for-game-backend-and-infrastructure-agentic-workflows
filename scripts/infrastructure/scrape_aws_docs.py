#!/usr/bin/env python3
"""
AWS Documentation Scraper with ETag-based caching.

Downloads AWS documentation and converts to markdown for Knowledge Base ingestion.
Uses HTTP ETags and Last-Modified headers to avoid unnecessary downloads.
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

import requests
from bs4 import BeautifulSoup
import html2text

# Cache duration: 7 days
CACHE_DAYS = 7

# Bump when the generated document format changes; cached entries written by an
# older format are regenerated instead of kept.
OUTPUT_FORMAT_VERSION = 2

# Reviewed lifecycle status of each source. The rule:
#   current    - the recommended approach still applies, even if the service has
#                since been renamed (for example GameLift to GameLift Servers).
#   historical - the recommended approach, feature set, or pricing has since been
#                replaced; kept only as background.
#   sample     - walks through one specific sample, starter kit, or demo
#                implementation rather than the service's general guidance.
# Non-current documents repeat a short qualifier at every heading and at least
# every QUALIFIER_SPACING characters, so every Knowledge Base chunk carries it.
LIFECYCLE_NOTES = {
    "current": "Current guidance.",
    "historical": (
        "Historical. Kept for background; details may be out of date. "
        "Current AWS documentation takes precedence."
    ),
    "sample": (
        "Sample walkthrough. Shows one example implementation; adapt it rather than treating "
        "it as the general recommendation."
    ),
}
QUALIFIER_LABELS = {"historical": "Historical source", "sample": "Sample walkthrough"}
QUALIFIER_SPACING = 600

# AWS Documentation URLs (HTML only - S3 Vectors has size limits).
# The domain key is the owning specialist; every entry declares a lifecycle.
DOCS_CONFIG = {
    "gamelift": [
        {
            "url": "https://docs.aws.amazon.com/gameliftservers/latest/developerguide/gamelift-intro.html",
            "output": "developer-guide.md",
            "title": "GameLift Developer Guide",
            "lifecycle": "current"
        },
        {
            "url": "https://aws.amazon.com/blogs/gametech/how-to-host-your-unreal-engine-game-for-under-1-per-player-with-amazon-gamelift/",
            "output": "blog-unreal-under-1-dollar.md",
            "title": "How to Host Your Unreal Engine Game for Under $1 per Player with Amazon GameLift",
            # 2023 post; its EC2 fleet setup and per-player cost math predate container fleets.
            "lifecycle": "historical"
        },
        {
            "url": "https://aws.amazon.com/blogs/gametech/faster-multiplayer-hosting-with-containers-on-amazon-gamelift-servers/",
            "output": "blog-gamelift-container-fleets.md",
            "title": "Faster Multiplayer Hosting with Containers on Amazon GameLift Servers",
            # Walkthrough of the Containers Starter Kit sample.
            "lifecycle": "sample"
        },
        {
            "url": "https://aws.amazon.com/blogs/gametech/leverage-fully-managed-containers-to-host-multiplayer-games-at-global-scale-on-amazon-gamelift/",
            "output": "blog-gamelift-managed-containers.md",
            "title": "Leverage Fully-Managed Containers to Host Multiplayer Games at Global Scale on Amazon GameLift",
            "lifecycle": "current"
        },
        {
            "url": "https://aws.amazon.com/blogs/gametech/hybrid-game-server-hosting-with-amazon-gamelift-anywhere/",
            "output": "blog-gamelift-anywhere-hybrid.md",
            "title": "Hybrid Game Server Hosting with Amazon GameLift Anywhere",
            "lifecycle": "current"
        },
        {
            "url": "https://aws.amazon.com/blogs/gametech/development-phase-steps-for-successful-launches-on-amazon-gamelift-servers/",
            "output": "blog-gamelift-dev-phase.md",
            "title": "Development Phase Steps for Successful Launches on Amazon GameLift Servers",
            "lifecycle": "current"
        },
        {
            "url": "https://aws.amazon.com/blogs/gametech/launch-phase-steps-for-successful-launches-on-amazon-gamelift-servers/",
            "output": "blog-gamelift-launch-phase.md",
            "title": "Launch Phase Steps for Successful Launches on Amazon GameLift Servers",
            "lifecycle": "current"
        },
        {
            "url": "https://aws.amazon.com/blogs/gametech/apex-legends-migrates-to-amazon-gamelift-servers-in-just-10-days/",
            "output": "blog-gamelift-apex-migration.md",
            "title": "Apex Legends Migrates to Amazon GameLift Servers in Just 10 Days",
            "lifecycle": "current"
        }
    ],
    "eks": [
        {
            "url": "https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html",
            "output": "user-guide.md",
            "title": "EKS User Guide",
            "lifecycle": "current"
        },
        {
            "url": "https://docs.aws.amazon.com/eks/latest/best-practices/introduction.html",
            "output": "best-practices.md",
            "title": "EKS Best Practices",
            "lifecycle": "current"
        },
        {
            "url": "https://aws.amazon.com/blogs/gametech/developers-guide-to-operate-game-servers-on-kubernetes-part-1/",
            "output": "blog-game-servers-kubernetes.md",
            "title": "Developer's Guide to Operate Game Servers on Kubernetes (Part 1)",
            "lifecycle": "current"
        },
        {
            "url": "https://aws.amazon.com/blogs/gametech/optimize-game-servers-hosting-with-containers/",
            "output": "blog-optimize-containers.md",
            "title": "Optimize Game Servers Hosting with Containers",
            # 2022 post; predates GameLift Servers container fleets.
            "lifecycle": "historical"
        },
        {
            "url": "https://aws.amazon.com/blogs/gametech/new-solution-guidance-for-building-scalable-cross-platform-game-backends-on-aws/",
            "output": "blog-game-backend-framework.md",
            "title": "Guidance for Building Scalable Cross-Platform Game Backends on AWS (Game Backend Framework)",
            "lifecycle": "current"
        },
        {
            "url": "https://aws.amazon.com/blogs/gametech/modernize-game-backend-services-with-aws-global-accelerator/",
            "output": "blog-modernize-aga.md",
            "title": "Modernize Game Backend Services with AWS Global Accelerator",
            "lifecycle": "current"
        },
        {
            "url": "https://aws.amazon.com/blogs/gametech/building-resilient-and-secure-game-backends-with-amazon-cloudfront/",
            "output": "blog-resilient-cloudfront.md",
            "title": "Building Resilient and Secure Game Backends with Amazon CloudFront",
            "lifecycle": "current"
        }
    ],
    "cost": [
        {
            "url": "https://docs.aws.amazon.com/cost-management/latest/userguide/what-is-costmanagement.html",
            "output": "cost-management.md",
            "title": "Cost Management Guide",
            "lifecycle": "current"
        },
        {
            "url": "https://aws.amazon.com/blogs/gametech/choose-the-right-compute-strategy-for-your-global-game-servers/",
            "output": "blog-global-compute-strategy.md",
            "title": "Choose the Right Compute Strategy for Your Global Game Servers",
            # 2020 post; instance families and pricing guidance have changed.
            "lifecycle": "historical"
        },
        {
            "url": "https://aws.amazon.com/blogs/gametech/cost-optimize-your-minecraft-java-ec2-server/",
            "output": "blog-cost-optimize-minecraft-ec2.md",
            "title": "Cost Optimize Your Minecraft Java EC2 Server",
            # Walkthrough for one specific self-managed EC2 game server.
            "lifecycle": "sample"
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
        # Images carry no retrievable text. Their alt attributes can be huge (one
        # blog image repeats ~10k characters of the article), which the KB's
        # fixed-size chunking turns into many duplicate, context-poor vectors.
        self.h2t.ignore_images = True

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

    @staticmethod
    def _fingerprint(doc_config: Dict) -> str:
        """Identify what a generated file depends on besides the page itself."""
        material = {
            "format": OUTPUT_FORMAT_VERSION,
            "title": doc_config["title"],
            "url": doc_config["url"],
            "lifecycle": doc_config["lifecycle"],
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()

    def _is_cache_valid(self, cache_key: str, fingerprint: str) -> bool:
        if cache_key not in self.cache:
            return False

        cached = self.cache[cache_key]
        if cached.get('fingerprint') != fingerprint:
            # Written by an older output format, or for a different title,
            # URL, or lifecycle: regenerate.
            return False
        cached_date = datetime.fromisoformat(cached.get('cached_at', '2000-01-01'))
        age_days = (datetime.now() - cached_date).days

        return age_days < CACHE_DAYS

    def _fetch_with_etag(self, url: str, cache_key: str, fingerprint: str) -> Optional[str]:
        headers = {}

        cached = self.cache.get(cache_key)
        # Conditional headers only when the existing file was generated for the
        # same fingerprint; otherwise a 304 would keep an outdated file.
        if cached and cached.get('fingerprint') == fingerprint:
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
                    'fingerprint': fingerprint,
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

    @staticmethod
    def _published_date(html: str) -> Optional[str]:
        """Return the page's publication date (YYYY-MM-DD) when it declares one."""
        soup = BeautifulSoup(html, 'html.parser')
        meta = soup.find('meta', {'property': 'article:published_time'})
        value = meta.get('content') if meta else None
        if not value:
            time_tag = soup.find('time')
            value = time_tag.get('datetime') if time_tag else None
        match = re.match(r'(\d{4}-\d{2}-\d{2})', value or '')
        return match.group(1) if match else None

    @staticmethod
    def _drop_images(content) -> None:
        """Remove images, and links whose only content was an image."""
        for img in content.find_all('img'):
            link = img.find_parent('a')
            img.decompose()
            if link is not None and not link.get_text(strip=True) and not link.find('img'):
                link.decompose()

    def _extract_content(self, html: str) -> Optional[str]:
        soup = BeautifulSoup(html, 'html.parser')

        # Blog posts (aws.amazon.com/blogs/...) use a dedicated article body.
        # Try these first so we get clean article text instead of page chrome.
        blog_content = (
            soup.find('section', {'class': 'blog-post-content'})
            or soup.find('article')
        )
        if blog_content:
            for tag in blog_content.find_all(['nav', 'footer', 'script', 'style']):
                tag.decompose()
            self._drop_images(blog_content)
            return str(blog_content)

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
                self._drop_images(content)
                return str(content)

        body = soup.find('body')
        if body:
            self._drop_images(body)
            return str(body)

        return None

    def scrape_doc(self, domain: str, doc_config: Dict) -> bool:
        url = doc_config['url']
        output_file = self.output_dir / domain / doc_config['output']
        cache_key = f"{domain}/{doc_config['output']}"

        print(f"\n📄 {doc_config['title']}")
        print(f"   URL: {url}")

        fingerprint = self._fingerprint(doc_config)
        if self._is_cache_valid(cache_key, fingerprint) and output_file.exists():
            print(f"  ✅ Cache valid (age < {CACHE_DAYS} days)")
            return True

        html = self._fetch_with_etag(url, cache_key, fingerprint)

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

        # Source and publication date give retrieval enough context to tell a
        # current recommendation from an older post.
        header = f"# {doc_config['title']}\n\nSource: {url}\n"
        published = self._published_date(html)
        if published:
            header += f"Published: {published}\n"
        lifecycle = doc_config['lifecycle']
        header += f"Status: {LIFECYCLE_NOTES[lifecycle]}\n"
        header += "\n"
        full_content = header + self._qualify(markdown, lifecycle, published)

        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(full_content)

        print(f"  ✅ Downloaded ({len(full_content)} chars)")
        return True

    @staticmethod
    def _qualify(markdown: str, lifecycle: str, published: Optional[str]) -> str:
        """Repeat a short qualifier through a non-current document.

        Knowledge Base chunking splits a document into fixed-size pieces, and only
        the first piece holds the header. The qualifier is appended to every
        heading and inserted at a word boundary at least every
        QUALIFIER_SPACING characters, so each chunk carries it.
        """
        label = QUALIFIER_LABELS.get(lifecycle)
        if not label:
            return markdown
        qualifier = f"({label}, published {published}.)" if published else f"({label}.)"
        out: list = []
        since = QUALIFIER_SPACING  # qualify the opening text too, not just later chunks
        for line in markdown.split("\n"):
            if line.startswith("#"):
                out.append(f"{line} {qualifier}")
                since = 0
                continue
            pieces = re.split(r"(\s+)", line)
            rebuilt = []
            for piece in pieces:
                rebuilt.append(piece)
                since += len(piece)
                if since >= QUALIFIER_SPACING and piece and piece.isspace():
                    rebuilt.append(f"{qualifier} ")
                    since = 0
            out.append("".join(rebuilt))
            since += 1
        return "\n".join(out)

    def prune_orphans(self) -> list:
        """Delete generated files and cache entries for sources no longer configured."""
        removed = []
        configured = {domain: {doc["output"] for doc in docs} for domain, docs in DOCS_CONFIG.items()}
        for domain, outputs in configured.items():
            domain_dir = self.output_dir / domain
            if not domain_dir.is_dir():
                continue
            for path in sorted(domain_dir.glob("*.md")):
                if path.name not in outputs:
                    path.unlink()
                    removed.append(f"{domain}/{path.name}")
        for key in list(self.cache):
            domain, _, name = key.partition("/")
            if domain in configured and name not in configured[domain]:
                del self.cache[key]
        for name in removed:
            print(f"  🗑️  Removed {name} (no longer a configured source)")
        return removed

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

        self.prune_orphans()
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
