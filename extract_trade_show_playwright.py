#!/usr/bin/env python3
"""
展会参展商提取工具 — Playwright 版本

解决普通 requests 无法抓取 JS 渲染页面（如 React/Vue 展会网站）的问题。
用 Playwright 打开真实 Chrome 浏览器，等页面完全渲染后再提取参展商名单。

用法示例：
  # ISPO 参展商（自动识别加载更多）
  python extract_trade_show_playwright.py \
    --url "https://www.ispo.com/exhibitors-2026-brands" \
    --find-domains \
    --output ispo_leads.csv \
    --domain-list ispo_domains.txt

  # 自定义选择器（针对特定展会网站）
  python extract_trade_show_playwright.py \
    --url "https://example-trade-show.com/exhibitors" \
    --selector ".exhibitor-name" \
    --load-more-selector "button.load-more" \
    --find-domains

  # 只导出 glove 相关公司（默认）
  python extract_trade_show_playwright.py \
    --url "..." \
    --find-domains

  # 导出所有公司不过滤
  python extract_trade_show_playwright.py \
    --url "..." \
    --all \
    --find-domains
"""

import argparse
import csv
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import List, Optional, Set

try:
    import requests
except ImportError:
    requests = None

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

# Common CSS selectors that might contain exhibitor/company names
DEFAULT_SELECTORS = [
    "[class*='exhibitor'] h2", "[class*='exhibitor'] h3", "[class*='exhibitor'] .name",
    "[class*='company'] h2", "[class*='company'] h3", "[class*='company'] .name",
    "[class*='participant'] h2", "[class*='participant'] h3",
    "[class*='vendor'] h2", "[class*='vendor'] h3",
    "[class*='brand'] h2", "[class*='brand'] h3",
    ".exhibitor-item .title", ".exhibitor-item .name",
    ".company-item .title", ".company-item .name",
    "[data-name]",
    ".m-exhibitor-item__name",  # ISPO style
    ".exhibitor-name",
    ".exhibitor-title",
    ".company-name",
    ".company-title",
    ".brand-name",
    ".brand-title",
    "h2 a", "h3 a", "h4 a",
    ".title", ".name",
]

# Common "Load More" button selectors
LOAD_MORE_SELECTORS = [
    "button:has-text('Load more')", "button:has-text('Show more')",
    "button:has-text('Load More')", "button:has-text('Show More')",
    "button:has-text('load more')", "button:has-text('show more')",
    "a:has-text('Load more')", "a:has-text('Show more')",
    "[class*='load-more']", "[class*='loadmore']", "[class*='show-more']", "[class*='showmore']",
    "[class*='loadMore']", "[class*='showMore']",
    "button[class*='more']", "a[class*='more']",
    "button:has-text('Mehr laden')", "button:has-text('Weitere')",  # German
    "button:has-text('查看更多')", "button:has-text('加载更多')",  # Chinese
    "[aria-label*='load more' i]", "[aria-label*='show more' i]",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_company_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r"\s+(Inc\.?|LLC|Ltd\.?|Limited|Corp\.?|Corporation|GmbH|AG|S\.A\.?|B\.V\.?)$", "", name, flags=re.IGNORECASE)
    return name.strip()


def is_glove_related(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in GLOVE_KEYWORDS)


def find_domain_ddg(company_name: str) -> Optional[str]:
    if requests is None:
        return None
    query = f"{company_name} official website"
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        html = resp.text
        m = re.search(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', html)
        if not m:
            m = re.search(r'<a[^>]+href="(https?://[^"]+)"', html)
        if not m:
            return None
        url = m.group(1)
        if "duckduckgo.com/l/" in url or "duckduckgo.com/d.js" in url:
            ru = re.search(r'uddg=([^&]+)', url)
            if ru:
                url = urllib.parse.unquote(ru.group(1))
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        blocked = {"facebook.com", "linkedin.com", "twitter.com", "x.com", "instagram.com",
                   "youtube.com", "wikipedia.org", "amazon.com", "ebay.com", "alibaba.com",
                   "zoominfo.com", "crunchbase.com", "bbb.org", "yellowpages.com", "yelp.com",
                   "tripadvisor.com", "pinterest.com", "reddit.com"}
        if domain in blocked:
            return None
        return domain
    except Exception as e:
        print(f"    [DDG ERROR] {company_name}: {e}")
        return None


def export_csv(companies: List[str], domains: dict, output_path: str):
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["company", "domain", "glove_related"])
        for name in companies:
            domain = domains.get(name, "")
            writer.writerow([name, domain, "yes" if is_glove_related(name) else "no"])
    print(f"\n[OK] Exported {len(companies)} rows to: {output_path}")


def export_domain_list(domains: List[str], output_path: str):
    clean = sorted(set(d for d in domains if d))
    with open(output_path, "w", encoding="utf-8") as f:
        for d in clean:
            f.write(d + "\n")
    print(f"[OK] Exported {len(clean)} domains to: {output_path}")
    print("  Copy these domains and paste into lead-finder's '批量导入域名' field.")


# ---------------------------------------------------------------------------
# Playwright scraping
# ---------------------------------------------------------------------------

def scrape_with_playwright(
    url: str,
    selector: Optional[str] = None,
    load_more_selector: Optional[str] = None,
    max_clicks: int = 20,
    initial_wait: float = 3.0,
    scroll: bool = True,
) -> List[str]:
    """
    Scrape exhibitor/company names from a JS-rendered page using Playwright.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("[ERROR] Playwright not installed. Run: pip install playwright && playwright install chromium")
        sys.exit(1)

    companies: Set[str] = set()

    with sync_playwright() as p:
        print(f"[Browser] Launching Chromium...")
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=HEADERS["User-Agent"],
        )
        page = context.new_page()

        print(f"[Browser] Navigating to {url} ...")
        page.goto(url, wait_until="networkidle", timeout=60000)
        time.sleep(initial_wait)

        # Scroll to bottom to trigger lazy loading
        if scroll:
            print("[Browser] Scrolling to trigger lazy loading...")
            for _ in range(3):
                page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                time.sleep(1)

        # Click "Load More" buttons repeatedly
        click_count = 0
        while click_count < max_clicks:
            btn_selector = None
            # Try to find load-more button
            selectors_to_try = [load_more_selector] if load_more_selector else LOAD_MORE_SELECTORS
            for sel in selectors_to_try:
                if not sel:
                    continue
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=2000):
                        btn_selector = sel
                        break
                except Exception:
                    continue

            if not btn_selector:
                print(f"[Browser] No more 'Load More' buttons found after {click_count} clicks")
                break

            print(f"[Browser] Clicking 'Load More' ({click_count + 1}/{max_clicks})...")
            try:
                page.locator(btn_selector).first.click(timeout=5000)
                # Wait for new content
                time.sleep(2)
                # Scroll again after load
                if scroll:
                    page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                    time.sleep(1)
                click_count += 1
            except PWTimeout:
                print("[Browser] Click timed out, assuming no more content")
                break
            except Exception as e:
                print(f"[Browser] Click failed: {e}")
                break

        # Extract company names
        print("[Browser] Extracting company names...")
        selectors_to_try = [selector] if selector else DEFAULT_SELECTORS
        for sel in selectors_to_try:
            elements = page.locator(sel).all()
            for elem in elements:
                try:
                    text = elem.text_content().strip()
                    if text and 3 < len(text) < 80:
                        # Skip obvious non-companies
                        lower = text.lower()
                        if lower in {"home", "about", "contact", "products", "services", "news", "blog",
                                     "login", "register", "search", "menu", "cart", "home page",
                                     "about us", "contact us", "load more", "show more"}:
                            continue
                        companies.add(text)
                except Exception:
                    continue

        browser.close()

    return sorted(companies)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Extract trade show exhibitors with Playwright")
    parser.add_argument("--url", type=str, required=True, help="Exhibitor list page URL")
    parser.add_argument("--selector", type=str, help="CSS selector for company name elements (optional)")
    parser.add_argument("--load-more-selector", type=str, help="CSS selector for 'Load More' button (optional)")
    parser.add_argument("--max-clicks", type=int, default=20, help="Max 'Load More' clicks (default: 20)")
    parser.add_argument("--wait", type=float, default=3.0, help="Initial wait after page load (seconds)")
    parser.add_argument("--no-scroll", action="store_true", help="Disable auto-scroll")
    parser.add_argument("--find-domains", action="store_true", help="Search for company websites via DuckDuckGo")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between domain searches")
    parser.add_argument("--all", action="store_true", help="Include all companies, not just glove-related")
    parser.add_argument("--output", type=str, default="trade_show_leads.csv", help="Output CSV path")
    parser.add_argument("--domain-list", type=str, help="Also export plain domain list")
    args = parser.parse_args()

    # Step 1: Scrape
    companies = scrape_with_playwright(
        url=args.url,
        selector=args.selector,
        load_more_selector=args.load_more_selector,
        max_clicks=args.max_clicks,
        initial_wait=args.wait,
        scroll=not args.no_scroll,
    )

    print(f"[Extract] Found {len(companies)} potential company names")
    if not companies:
        print("[WARNING] No company names found. Try adjusting --selector or --load-more-selector.")
        sys.exit(0)

    # Step 2: Deduplicate
    companies = sorted(set(normalize_company_name(c) for c in companies if c.strip()))
    print(f"[Deduplicate] {len(companies)} unique companies")

    # Step 3: Filter
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
        print("[WARNING] No glove-related companies found. Try --all to see all companies.")
        sys.exit(0)

    # Step 4: Find domains
    domains = {}
    if args.find_domains:
        if requests is None:
            print("[ERROR] requests not installed. Run: pip install requests")
        else:
            print(f"\n[Domain Search] Finding websites for {len(filtered)} companies...")
            for idx, name in enumerate(filtered, 1):
                print(f"  [{idx}/{len(filtered)}] {name} ...", end=" ", flush=True)
                domain = find_domain_ddg(name)
                if domain:
                    domains[name] = domain
                    print(domain)
                else:
                    print("not found")
                time.sleep(args.delay)
    else:
        print("\n[Tip] Pass --find-domains to automatically search for company websites")

    # Step 5: Export
    export_csv(filtered, domains, args.output)
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
