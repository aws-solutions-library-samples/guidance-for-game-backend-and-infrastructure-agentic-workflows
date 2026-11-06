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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
import html2text

# Cache duration: 7 days
CACHE_DAYS = 7

# Bump when the generated document format changes; cached entries written by an
# older format are regenerated instead of kept.
OUTPUT_FORMAT_VERSION = 4

# Query parameters that only track a click; removed from link targets.
TRACKING_PARAMETERS = re.compile(r"^(utm_[a-z]+|trk|trkCampaign|sc_channel|sc_campaign|sc_medium|sc_content|sc_detail|sc_country|sc_geo|sc_outcome|sc_publisher|sc_category|sc_segment|sc_icampaign|sc_ichannel|sc_icontent|sc_iplace)$", re.IGNORECASE)

# Reviewed lifecycle status of each source. The rule:
#   current    - the recommended approach still applies, even if the service has
#                since been renamed (for example GameLift to GameLift Servers).
#   historical - the recommended approach, feature set, or pricing has since been
#                replaced; kept only as background.
#   sample     - walks through one specific sample, starter kit, or demo
#                implementation rather than the service's general guidance.
# Non-current documents repeat a short qualifier at every heading, at the start
# of every paragraph, list item, and table, on its own line before every code
# block (and after long ones), and at least every QUALIFIER_SPACING characters
# of prose. Knowledge Base chunking starts a chunk at a paragraph boundary when
# the next paragraph does not fit, so a short chunk is usually one or two whole
# paragraphs; the spacing rule covers chunks inside long paragraphs. Code lines
# and inline code are never changed.
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

    def _fetch_with_etag(
        self, url: str, cache_key: str, fingerprint: str, conditional: bool = True
    ) -> Tuple[str, Optional[str], Dict]:
        """Fetch a page. Returns (status, html, cache entry); status is "ok", "not_modified", or "failed".

        The cache entry is returned, not stored: the caller records it only
        after the generated file has been written.
        """
        headers = {}

        cached = self.cache.get(cache_key)
        # Conditional headers only when the existing file was generated for the
        # same fingerprint (and still exists); otherwise a 304 would keep an
        # outdated file or none at all.
        if conditional and cached and cached.get('fingerprint') == fingerprint:
            if cached.get('etag'):
                headers['If-None-Match'] = cached['etag']
            if cached.get('last_modified'):
                headers['If-Modified-Since'] = cached['last_modified']

        try:
            response = requests.get(url, headers=headers, timeout=30)
        except Exception as e:
            print(f"  ❌ Fetch failed: {e}")
            return "failed", None, {}

        if response.status_code == 304:
            print(f"  ✅ Not modified (using cache)")
            return "not_modified", None, {}

        if response.status_code == 200:
            entry = {
                'url': url,
                'fingerprint': fingerprint,
                'etag': response.headers.get('ETag'),
                'last_modified': response.headers.get('Last-Modified'),
                'cached_at': datetime.now().isoformat()
            }
            return "ok", response.text, entry

        print(f"  ⚠️  HTTP {response.status_code}")
        return "failed", None, {}

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

    @staticmethod
    def _strip_tracking(content) -> None:
        """Remove click-tracking query parameters from link targets."""
        for link in content.find_all('a', href=True):
            parts = urlsplit(link['href'])
            if not parts.query:
                continue
            kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAMETERS.match(k)]
            link['href'] = urlunsplit(parts._replace(query=urlencode(kept)))

    def _extract_content(self, html: str) -> Optional[str]:
        content = self._select_content(html)
        if content is None:
            return None
        self._drop_images(content)
        self._strip_tracking(content)
        return str(content)

    def _select_content(self, html: str):
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
            return blog_content

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
                return content

        return soup.find('body')

    def _discard_stale(self, output_file: Path, cache_key: str, reason: str) -> bool:
        """Remove a file that no longer matches its fingerprint, so it is never seeded."""
        if output_file.exists():
            output_file.unlink()
        self.cache.pop(cache_key, None)
        print(f"  ❌ {reason}; removed the outdated file")
        return False

    def scrape_doc(self, domain: str, doc_config: Dict) -> bool:
        """Generate one document. Returns False when no up-to-date file exists afterwards."""
        url = doc_config['url']
        output_file = self.output_dir / domain / doc_config['output']
        cache_key = f"{domain}/{doc_config['output']}"

        print(f"\n📄 {doc_config['title']}")
        print(f"   URL: {url}")

        fingerprint = self._fingerprint(doc_config)
        if self._is_cache_valid(cache_key, fingerprint) and output_file.exists():
            print(f"  ✅ Cache valid (age < {CACHE_DAYS} days)")
            return True

        # A file whose cache entry is missing or carries another fingerprint was
        # generated by an older format or for another title, URL, or lifecycle.
        # It may be kept only while it is still current.
        cached = self.cache.get(cache_key) or {}
        current = output_file.exists() and cached.get('fingerprint') == fingerprint

        status, html, entry = self._fetch_with_etag(url, cache_key, fingerprint, conditional=current)

        if status == "not_modified" and current:
            return True
        if html is None:
            if current:
                print(f"  ⚠️  Using existing file (fetch failed)")
                return True
            if output_file.exists():
                return self._discard_stale(output_file, cache_key, "Fetch failed and the existing file is outdated")
            print(f"  ❌ No cached file available")
            return False

        content_html = self._extract_content(html)
        if not content_html:
            if current:
                print(f"  ⚠️  Using existing file (extraction failed)")
                return True
            return self._discard_stale(output_file, cache_key, "Extraction failed")

        try:
            markdown = self.h2t.handle(content_html)
        except Exception as e:
            if current:
                print(f"  ⚠️  Using existing file (conversion failed: {e})")
                return True
            return self._discard_stale(output_file, cache_key, f"Conversion failed: {e}")

        # Source and date give retrieval enough context to tell a current
        # recommendation from an older post. Pages that declare no publication
        # date (the AWS documentation guides) get the retrieval date instead.
        header = f"# {doc_config['title']}\n\nSource: {url}\n"
        published = self._published_date(html)
        if published:
            header += f"Published: {published}\n"
        else:
            header += f"Retrieved: {datetime.now(timezone.utc).date().isoformat()}\n"
        lifecycle = doc_config['lifecycle']
        header += f"Status: {LIFECYCLE_NOTES[lifecycle]}\n"
        header += "\n"
        full_content = header + self._qualify(markdown, lifecycle, published)

        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(full_content)
        # Recorded only now, so a failure above never marks an old file as valid.
        self.cache[cache_key] = entry

        print(f"  ✅ Downloaded ({len(full_content)} chars)")
        return True

    @staticmethod
    def _is_indented_code(line: str) -> bool:
        return line.startswith(("    ", "\t")) and bool(line.strip())

    @staticmethod
    def _qualify(markdown: str, lifecycle: str, published: Optional[str]) -> str:
        """Repeat a short qualifier through a non-current document.

        Knowledge Base chunking splits a document into fixed-size pieces, and only
        the first piece holds the header. The qualifier is appended to every
        heading, starts every paragraph and list item (after the list or quote
        marker), is put on its own line directly above every table, on its own line before
        every code block, and
        after any code block longer than QUALIFIER_SPACING, and is inserted at a
        word boundary outside inline code at least every QUALIFIER_SPACING
        characters of prose. Code lines are never changed.
        """
        label = QUALIFIER_LABELS.get(lifecycle)
        if not label:
            return markdown
        qualifier = f"({label}, published {published}.)" if published else f"({label}.)"
        out: list = []
        since = QUALIFIER_SPACING  # qualify the opening text too, not just later chunks
        fenced = False
        indented = False
        block = 0
        previous_blank = True
        in_table = False

        def open_block() -> None:
            nonlocal since, block
            out.extend([qualifier, ""])
            since = 0
            block = 0

        def close_block() -> None:
            nonlocal since
            if block >= QUALIFIER_SPACING:
                out.extend(["", qualifier, ""])
                since = 0

        for line in markdown.split("\n"):
            fence = line.lstrip().startswith(("```", "~~~"))
            if fenced:
                out.append(line)
                since += len(line) + 1
                block += len(line) + 1
                if fence:
                    fenced = False
                    close_block()
                    previous_blank = True
                continue
            if indented:
                if not line.strip() or DocScraper._is_indented_code(line):
                    out.append(line)
                    since += len(line) + 1
                    block += len(line) + 1
                    continue
                indented = False
                close_block()
                previous_blank = True
            if fence:
                open_block()
                fenced = True
                out.append(line)
                block += len(line) + 1
                since += len(line) + 1
                continue
            if DocScraper._is_indented_code(line):
                open_block()
                indented = True
                out.append(line)
                block += len(line) + 1
                since += len(line) + 1
                continue
            if line.startswith("#"):
                out.append(f"{line} {qualifier}")
                since = 0
                previous_blank = in_table = False
                continue
            rebuilt = []
            ticks = 0
            table_row = "|" in line and ticks == 0 and not line.lstrip().startswith(">")
            if not line.strip():
                in_table = False
            elif table_row and not in_table:
                # On its own line directly above the table, with no blank line
                # between, so chunking keeps it with the table.
                out.append(qualifier)
                since = 0
                in_table = True
            elif previous_blank and not table_row:
                marker = re.match(r"\s*(?:>\s*)*(?:(?:[*+-]|\d+\.)\s+)?", line).group(0)
                rebuilt.append(f"{marker}{qualifier} ")
                line = line[len(marker):]
                since = 0
            previous_blank = not line.strip() and not rebuilt
            for piece in re.split(r"(\s+)", line):
                rebuilt.append(piece)
                ticks += piece.count("`")
                since += len(piece)
                # Only between words, and never inside an inline code span.
                if since >= QUALIFIER_SPACING and piece and piece.isspace() and ticks % 2 == 0:
                    rebuilt.append(f"{qualifier} ")
                    since = 0
            out.append("".join(rebuilt))
            since += 1
        if fenced or indented:
            close_block()
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

        # Seeding mirrors the generated set (s3 sync --delete), so a missing
        # source would silently leave the Knowledge Base. Fail instead, so both
        # deploy paths stop before seeding.
        missing = [
            f"{domain}/{doc['output']}"
            for domain, docs in DOCS_CONFIG.items()
            for doc in docs
            if not (self.output_dir / domain / doc['output']).exists()
        ]

        print(f"\n{'='*60}")
        print(f"{'✅' if not missing else '❌'} Complete: {success_count}/{total_count} documents")
        for name in missing:
            print(f"   missing: {name}")
        print(f"{'='*60}\n")

        return not missing


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
