#!/usr/bin/env python3
"""
海关进口商免费预览数据抓取工具

原理：
  ImportGenius、Panjiva、Zauba 等付费海关数据平台为了 SEO，
  会把部分进口商记录做成"免费预览"页面供搜索引擎索引。
  我们通过 DuckDuckGo 搜索这些碎片化的免费数据，提取进口商公司名。

注意：
  这是"边角料"数据，不完整，但完全免费。
  适合快速建立潜在客户名单，再导入 lead-finder 深挖邮箱。

用法示例：
  # 搜索 football gloves 进口商（多平台）
  python scrape_customs_teaser.py \
    --keyword "football gloves" \
    --pages 3 \
    --find-domains \
    --output customs_leads.csv

  # 搜索 baseball gloves，只查 ImportGenius
  python scrape_customs_teaser.py \
    --keyword "baseball gloves" \
    --sites importgenius \
    --find-domains

  # 同时搜多个关键词
  python scrape_customs_teaser.py \
    --keyword "football gloves" --keyword "baseball gloves" --keyword "work gloves" \
    --pages 5 \
    --find-domains
"""

import argparse
import csv
import re
import sys
import time
import urllib.parse
from typing import List, Optional, Set

try:
    import requests
except ImportError:
    print("[ERROR] requests not installed. Run: pip install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

GLOVE_KEYWORDS = [
    "glove", "gloves",
    "hand protection", "handwear",
    "baseball", "football", "softball", "lacrosse", "hockey",
    "soccer", "goalkeeper", "goalie",
    "ski", "snowboard", "winter sport",
    "cycling", "bike", "motorcycle",
    "work", "safety", "industrial", "construction",
    "gym", "fitness", "weightlifting",
    "boxing", "mma", "martial art",
    "batting", "fielding",
    "goal keeping", "goalkeeping",
]

CUSTOMS_SITES = {
    "importgenius": {
        "domain": "importgenius.com",
        "title_patterns": [
            r'^([A-Z][A-Za-z0-9\s\.\-&]{2,60})(?:\s+[-–—]\s+|\s*\|\s*)',
        ],
    },
    "panjiva": {
        "domain": "panjiva.com",
        "title_patterns": [
            r'^([A-Z][A-Za-z0-9\s\.\-&]{2,60})\s*\|\s*Panjiva',
        ],
    },
    "zauba": {
        "domain": "zauba.com",
        "title_patterns": [
            r'^([A-Z][A-Za-z0-9\s\.\-&]{2,60})(?:\s+[-–—]\s+|\s*\|\s*)',
        ],
    },
    "datamyne": {
        "domain": "datamyne.com",
        "title_patterns": [
            r'^([A-Z][A-Za-z0-9\s\.\-&]{2,60})(?:\s+[-–—]\s+|\s*\|\s*)',
        ],
    },
    "exportgenius": {
        "domain": "exportgenius.io",
        "title_patterns": [
            r'^([A-Z][A-Za-z0-9\s\.\-&]{2,60})(?:\s+[-–—]\s+|\s*\|\s*)',
        ],
    },
}

STOP_WORDS = {
    "home", "about", "contact", "products", "services", "news", "blog",
    "login", "register", "search", "menu", "cart", "home page", "about us",
    "contact us", "privacy policy", "terms of service", "sitemap",
    "importgenius", "panjiva", "zauba", "datamyne", "exportgenius",
    "facebook", "linkedin", "twitter", "instagram", "youtube",
    "wikipedia", "amazon", "alibaba", "made in china",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_company_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r"\s+(Inc\.?|LLC|Ltd\.?|Limited|Corp\.?|Corporation|GmbH|AG|S\.A\.?|B\.V\.?|Co\.?|Company)$", "", name, flags=re.IGNORECASE)
    return name.strip()


def is_glove_related(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in GLOVE_KEYWORDS)


def strip_html_tags(text: str) -> str:
    return re.sub(r'<[^>]+>', '', text).strip()


# ---------------------------------------------------------------------------
# Bing search (DDG blocked in China)
# ---------------------------------------------------------------------------

def search_bing(keyword: str, site_domain: str, page: int = 0) -> str:
    """Search Bing for keyword restricted to a site. Returns raw HTML."""
    query = f'{keyword} site:{site_domain}'
    params = {"q": query}
    if page > 0:
        params["first"] = page * 10
    try:
        resp = requests.get(
            "https://www.bing.com/search",
            params=params,
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"    [Bing ERROR] {query}: {e}")
        return ""


# ---------------------------------------------------------------------------
# Company extraction from Bing HTML
# ---------------------------------------------------------------------------

def extract_bing_blocks(html: str) -> List[str]:
    """Extract individual result blocks from Bing HTML."""
    blocks = re.findall(
        r'<li class="b_algo"[^>]*>(.*?)</li>',
        html,
        re.DOTALL,
    )
    return blocks


def extract_company_from_block(block: str, site_key: str) -> Optional[str]:
    """Try to extract a company name from a single Bing result block."""
    # Extract title (Bing: <h2><a href="...">Title</a></h2>)
    title_match = re.search(r'<h2[^>]*><a[^>]*>(.*?)</a></h2>', block, re.DOTALL)
    if not title_match:
        return None
    title = strip_html_tags(title_match.group(1))

    # Extract snippet (Bing: <p> inside block)
    snippet_match = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
    snippet = strip_html_tags(snippet_match.group(1)) if snippet_match else ""

    # Extract URL (Bing: <h2><a href="...">)
    url_match = re.search(r'<h2[^>]*><a[^>]+href="([^"]+)"', block)
    url = url_match.group(1) if url_match else ""

    company = None
    config = CUSTOMS_SITES.get(site_key, {})

    # Try site-specific title patterns
    for pattern in config.get("title_patterns", []):
        m = re.match(pattern, title)
        if m:
            company = m.group(1).strip()
            break

    # Fallback: extract ALL-CAPS company names from ImportGenius style titles
    if not company and site_key == "importgenius":
        m = re.search(r'^([A-Z][A-Z0-9\s\.&]{2,60})(?:\s+[-–—]\s+|\s*\|\s*)', title)
        if m:
            company = m.group(1).strip()

    # Fallback: look for "importer" / "supplier" in snippet and grab preceding words
    if not company:
        # "XYZ Corp is a leading importer of..."
        m = re.search(r'([A-Z][A-Za-z0-9\s\.&]{2,50})\s+(?:is\s+a|as\s+a)\s+(?:leading\s+)?(?:importer|supplier|manufacturer|distributor)', snippet)
        if m:
            company = m.group(1).strip()

    # Fallback: snippet contains "Company Name imported..."
    if not company:
        m = re.search(r'([A-Z][A-Za-z0-9\s\.&]{2,50})\s+(?:imported|exported|shipped|supplied)', snippet)
        if m:
            company = m.group(1).strip()

    # Clean up
    if company:
        company = normalize_company_name(company)
        # Filter stop words
        if company.lower() in STOP_WORDS:
            return None
        if len(company) < 3 or len(company) > 80:
            return None
        # Filter if mostly numbers
        if sum(c.isdigit() for c in company) > len(company) * 0.4:
            return None

    return company


# ---------------------------------------------------------------------------
# Domain finding
# ---------------------------------------------------------------------------

def find_domain_bing(company_name: str) -> Optional[str]:
    query = f"{company_name} official website"
    try:
        resp = requests.get(
            "https://www.bing.com/search",
            params={"q": query},
            headers=HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        html = resp.text

        # Bing result: <li class="b_algo"><h2><a href="URL">...</a></h2>
        m = re.search(r'<li class="b_algo"[^>]*>.*?<h2[^>]*><a[^>]+href="([^"]+)"', html, re.DOTALL)
        if not m:
            return None

        url = m.group(1)
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        blocked = {"facebook.com", "linkedin.com", "twitter.com", "x.com", "instagram.com",
                   "youtube.com", "wikipedia.org", "amazon.com", "ebay.com", "alibaba.com",
                   "zoominfo.com", "crunchbase.com", "bbb.org", "yellowpages.com", "yelp.com",
                   "tripadvisor.com", "pinterest.com", "reddit.com", "importgenius.com",
                   "panjiva.com", "zauba.com", "datamyne.com", "exportgenius.io"}
        if domain in blocked:
            return None
        return domain
    except Exception as e:
        print(f"    [Bing ERROR] {company_name}: {e}")
        return None


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_csv(companies: List[str], domains: dict, sources: dict, output_path: str):
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["company", "domain", "source_platform", "glove_related"])
        for name in companies:
            writer.writerow([
                name,
                domains.get(name, ""),
                sources.get(name, ""),
                "yes" if is_glove_related(name) else "no",
            ])
    print(f"\n[OK] Exported {len(companies)} rows to: {output_path}")


def export_domain_list(domains: List[str], output_path: str):
    clean = sorted(set(d for d in domains if d))
    with open(output_path, "w", encoding="utf-8") as f:
        for d in clean:
            f.write(d + "\n")
    print(f"[OK] Exported {len(clean)} domains to: {output_path}")
    print("  Copy these domains and paste into lead-finder's '批量导入域名' field.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Scrape customs/import data free teasers")
    parser.add_argument("--keyword", type=str, action="append", required=True,
                        help="Search keyword (can specify multiple)")
    parser.add_argument("--sites", type=str, default="importgenius,panjiva,zauba",
                        help="Comma-separated list of customs sites to search")
    parser.add_argument("--pages", type=int, default=3,
                        help="Number of search result pages per site (default: 3)")
    parser.add_argument("--find-domains", action="store_true",
                        help="Search for company websites via DuckDuckGo")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Delay between domain searches (seconds)")
    parser.add_argument("--all", action="store_true",
                        help="Include all companies, not just glove-related")
    parser.add_argument("--output", type=str, default="customs_leads.csv",
                        help="Output CSV path")
    parser.add_argument("--domain-list", type=str,
                        help="Also export plain domain list")
    args = parser.parse_args()

    site_keys = [s.strip().lower() for s in args.sites.split(",")]
    site_keys = [s for s in site_keys if s in CUSTOMS_SITES]
    if not site_keys:
        print(f"[ERROR] No valid sites specified. Available: {list(CUSTOMS_SITES.keys())}")
        sys.exit(1)

    all_companies: Set[str] = set()
    company_sources: dict = {}

    # Step 1: Search each keyword on each site
    for keyword in args.keyword:
        print(f"\n{'='*60}")
        print(f"[Keyword] {keyword}")
        print(f"{'='*60}")

        for site_key in site_keys:
            site_config = CUSTOMS_SITES[site_key]
            print(f"\n[Site] {site_key} ({site_config['domain']})")

            for page in range(args.pages):
                print(f"  [Page {page + 1}/{args.pages}] Searching...", end=" ", flush=True)
                html = search_bing(keyword, site_config["domain"], page)
                if not html:
                    print("failed")
                    continue

                blocks = extract_bing_blocks(html)
                print(f"found {len(blocks)} results")

                for block in blocks:
                    company = extract_company_from_block(block, site_key)
                    if company:
                        all_companies.add(company)
                        # Record source (if first time seeing this company)
                        if company not in company_sources:
                            company_sources[company] = site_key

                # Polite delay between pages
                time.sleep(0.5)

    print(f"\n{'='*60}")
    print(f"[Extract] Found {len(all_companies)} unique company names")
    print(f"{'='*60}")

    if not all_companies:
        print("[WARNING] No companies found. Try different keywords or more pages.")
        sys.exit(0)

    # Step 2: Filter glove-related
    companies = sorted(all_companies)
    if args.all:
        filtered = companies
    else:
        filtered = [c for c in companies if is_glove_related(c)]
        print(f"[Filter] {len(filtered)}/{len(companies)} companies match glove keywords")
        if filtered:
            print("  Matched:")
            for c in filtered[:10]:
                print(f"    - {c}")
            if len(filtered) > 10:
                print(f"    ... and {len(filtered) - 10} more")

    if not filtered:
        print("[WARNING] No glove-related companies found. Try --all to see all results.")
        sys.exit(0)

    # Step 3: Find domains
    domains = {}
    if args.find_domains:
        print(f"\n[Domain Search] Finding websites for {len(filtered)} companies...")
        for idx, name in enumerate(filtered, 1):
            print(f"  [{idx}/{len(filtered)}] {name} ...", end=" ", flush=True)
            domain = find_domain_bing(name)
            if domain:
                domains[name] = domain
                print(domain)
            else:
                print("not found")
            time.sleep(args.delay)
    else:
        print("\n[Tip] Pass --find-domains to automatically search for company websites")

    # Step 4: Export
    export_csv(filtered, domains, company_sources, args.output)
    if args.domain_list:
        export_domain_list([domains.get(c, "") for c in filtered], args.domain_list)

    # Summary
    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    print(f"  Total companies   : {len(companies)}")
    print(f"  Glove-related     : {len(filtered)}")
    print(f"  Domains found     : {sum(1 for d in domains.values() if d)}")
    print(f"  Output CSV        : {args.output}")
    if args.domain_list:
        print(f"  Domain list       : {args.domain_list}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
