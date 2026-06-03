#!/usr/bin/env python3
"""
B2B Lead Finder - Find decision-maker emails from keyword searches.

Workflow:
1. Search Google via SerpAPI for keywords
2. Extract unique domains from top N pages
3. Query Hunter.io for emails at each domain
4. Filter out generic emails (info@, support@, etc.)
5. Validate emails via ZeroBounce (optional)
6. Export to CSV with enrichment data

Usage:
    python lead_finder.py "football gloves manufacturer" --pages 5 --output leads.csv
"""

import argparse
import concurrent.futures
import csv
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse

import requests
import yaml

# Optional: DuckDuckGo search (no API key needed)
try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None

# Optional: Browser automation via Playwright
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sync_playwright = None
    PWTimeout = None

# ---------------------------------------------------------------------------
# Config & Constants
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "config.yaml"

# Generic email prefixes to filter out
GENERIC_PREFIXES = {
    "info", "support", "sales", "contact", "hello", "admin", "noreply",
    "marketing", "help", "webmaster", "office", "service", "team", "general",
    "hr", "press", "media", "careers", "jobs", "abuse", "legal", "privacy",
    "security", "billing", "finance", "accounting", "orders", "feedback",
    "newsletter", "subscribe", "unsubscribe", "postmaster", "hostmaster",
    "root", "www", "ftp", "mail", "email", "customerservice", "enquiries",
    "inquiry", "request", "quote", "estimates", "reservations", "booking",
    "complaints", "returns", "shipping", "logistics", "procurement",
    "purchasing", "buyer", "vendors", "partners", "affiliates", "advertising",
    "events", "sponsorship", "donations", "foundation", "grants", "pr",
    "communications", "publicrelations", "community", "social", "media",
    "content", "editorial", "web", "it", "tech", "systems", "network",
    "operations", "facilities", "maintenance", "reception", "frontdesk",
    "concierge", "info2", "sales1", "sales2", "contactus", "reachus",
    "getintouch", "talktous", "askus", "questions", "faq", "helpdesk",
    "customersuccess", "client", "clients", "business", "corporate",
    "enterprise", "wholesale", "distributor", "retail", "store", "shop",
}

# Regex patterns that strongly suggest a personal/decision-maker email
PERSONAL_PATTERNS = [
    re.compile(r"^[a-z]+\.[a-z]+$"),           # john.smith
    re.compile(r"^[a-z]+_[a-z]+$"),            # john_smith
    re.compile(r"^[a-z]+-[a-z]+$"),            # john-smith
    re.compile(r"^[a-z]{2,20}[a-z]{2,20}$"),   # johnsmith (first+last concatenated)
]

# Country detection from TLD
def detect_country(domain: str, email: str = "") -> str:
    """Guess country from domain TLD or email domain TLD."""
    tld_map = {
        # Europe
        "co.uk": "UK", "uk": "UK",
        "de": "Germany",
        "fr": "France",
        "it": "Italy",
        "es": "Spain",
        "nl": "Netherlands",
        "be": "Belgium",
        "at": "Austria",
        "ch": "Switzerland",
        "se": "Sweden",
        "dk": "Denmark",
        "fi": "Finland",
        "no": "Norway",
        "pl": "Poland",
        "ie": "Ireland",
        "pt": "Portugal",
        "gr": "Greece",
        "cz": "Czech Republic",
        "hu": "Hungary",
        "ro": "Romania",
        "sk": "Slovakia",
        "si": "Slovenia",
        "hr": "Croatia",
        "bg": "Bulgaria",
        "lt": "Lithuania",
        "lv": "Latvia",
        "ee": "Estonia",
        "eu": "EU",
        # North America
        "ca": "Canada",
        "us": "USA",
        "mx": "Mexico",
        # Asia Pacific
        "au": "Australia",
        "nz": "New Zealand",
        "jp": "Japan",
        "kr": "South Korea",
        "cn": "China",
        "tw": "Taiwan",
        "hk": "Hong Kong",
        "sg": "Singapore",
        "my": "Malaysia",
        "th": "Thailand",
        "vn": "Vietnam",
        "id": "Indonesia",
        "ph": "Philippines",
        "in": "India",
        "bd": "Bangladesh",
        "pk": "Pakistan",
        # Middle East / Africa
        "ae": "UAE",
        "sa": "Saudi Arabia",
        "qa": "Qatar",
        "kw": "Kuwait",
        "bh": "Bahrain",
        "om": "Oman",
        "il": "Israel",
        "tr": "Turkey",
        "za": "South Africa",
        "eg": "Egypt",
        "ng": "Nigeria",
        "ke": "Kenya",
        # Latin America
        "br": "Brazil",
        "ar": "Argentina",
        "cl": "Chile",
        "co": "Colombia",
        "pe": "Peru",
        "uy": "Uruguay",
        # Russia / CIS
        "ru": "Russia",
        "ua": "Ukraine",
        "by": "Belarus",
        "kz": "Kazakhstan",
    }
    parts = domain.rsplit(".", 2)
    if len(parts) == 3 and parts[1] in ("co", "com", "org", "gov", "ac", "net"):
        # e.g. example.co.uk -> co.uk
        tld = f"{parts[1]}.{parts[2]}"
    elif len(parts) >= 2:
        tld = parts[-1]
    else:
        tld = ""
    if tld in tld_map:
        return tld_map[tld]
    # Fallback: check email domain
    if email and "@" in email:
        email_domain = email.split("@")[1]
        return detect_country(email_domain, "")
    return ""


# Big-brand / platform domains to exclude (no real decision-maker emails)
EXCLUDED_DOMAINS = {
    # E-commerce giants
    "amazon.com", "espn.com", "amazon.co.uk", "amazon.de", "amazon.fr", "amazon.ca", "amazon.in",
    "ebay.com", "ebay.co.uk", "ebay.de","nfl.com","rei.com",
    "walmart.com", "target.com", "bestbuy.com", "costco.com",
    "alibaba.com", "aliexpress.com", "taobao.com", "tmall.com", "jd.com",
    "etsy.com", "wayfair.com", "overstock.com", "newegg.com",
    # Social media
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "tiktok.com", "youtube.com", "pinterest.com", "snapchat.com", "reddit.com",
    "tumblr.com", "quora.com", "medium.com",
    # Search & tech
    "google.com", "bing.com", "yahoo.com", "duckduckgo.com",
    "apple.com", "microsoft.com", "ibm.com", "oracle.com", "sap.com",
    "adobe.com", "salesforce.com", "hubspot.com",
    # News & content
    "wikipedia.org", "wikimedia.org", "bbc.com", "bbc.co.uk", "cnn.com",
    "foxnews.com", "msn.com", "reuters.com", "bloomberg.com", "forbes.com",
    "nytimes.com", "washingtonpost.com", "guardian.com", "theguardian.com",
    "huffpost.com", "buzzfeed.com", "vice.com",
    # Hosting & platforms
    "wordpress.com", "wordpress.org", "wix.com", "squarespace.com",
    "shopify.com", "weebly.com", "godaddy.com", "namecheap.com",
    "cloudflare.com", "aws.amazon.com", "heroku.com", "vercel.com", "netlify.com",
    # Video & streaming
    "netflix.com", "hulu.com", "disneyplus.com", "spotify.com", "twitch.tv",
    # Travel
    "booking.com", "expedia.com", "tripadvisor.com", "airbnb.com", "uber.com",
    "lyft.com", "grab.com",
    # Finance
    "paypal.com", "stripe.com", "squareup.com", "chase.com", "bankofamerica.com",
    # File sharing
    "dropbox.com", "googleusercontent.com", "cloudfront.net",
    # Government & education
    "gov.uk", "usa.gov", "europa.eu", "un.org",
    "harvard.edu", "mit.edu", "stanford.edu", "ox.ac.uk", "cam.ac.uk",
    # Sports brands (big apparel/glove manufacturers, not distributors)
    "nike.com", "nike.cn",
    "adidas.com", "adidas.co.uk", "adidas.de",
    "underarmour.com", "underarmour.co.uk",
    "puma.com", "puma.de", "puma.co.uk",
    "newbalance.com", "newbalance.co.uk",
    "asics.com", "mizuno.com", "mizunousa.com",
    "rawlings.com", "wilson.com", "wilsonsports.com",
    "easton.com", "demarini.com", "louisville-slugger.com",
    "franklinsports.com", "schutt.com", "riiddell.com",
    "cuttersgloves.com", "gripboost.com", "battle.net",
    # Others
    "yelp.com", "trip.com", "glassdoor.com", "indeed.com", "monster.com",
    "ziprecruiter.com", "craigslist.org", " Gumtree.com",
}

# ---------------------------------------------------------------------------
# Search result scoring (title + snippet analysis)
# ---------------------------------------------------------------------------

# Positive signals: small distributors, importers, private-label buyers
POSITIVE_SIGNALS = [
    "distributor", "wholesale", "wholesaler", "importer", "import",
    "retailer", "dealer", "reseller", "resell",
    "private label", "private-label", "privatelabel",
    "oem", "odm", "custom", "bespoke",
    "family owned", "family-owned", "small business", "boutique",
    "specialty", "specialise", "specialize",
    "sports equipment", "sporting goods", "athletic",
    "baseball", "football", "softball", "lacrosse", "hockey",
    # B2B / manufacturing signals
    "manufacturer", "factory", "supplier", "export", "exporter", "bulk",
    "moq", "trade", "trading", "b2b", "commercial", "production",
    "processing", "mill", "plant", "workshop", "industrial",
    " sourcing", "procurement", "purchasing",
]

# Negative signals: news, blogs, reviews, jobs, investor pages
NEGATIVE_SIGNALS = [
    "news", "blog", "article", "magazine", "press release", "pressrelease",
    "review", "reviews", "top 10", "top10", "comparison", "compare",
    "guide", "tutorial", "how to", "howto", "tips", "advice",
    "jobs", "careers", "hiring", "internship", "work with us",
    "wikipedia", "wiki", "encyclopedia",
    "stock", "investor", "shareholder", "annual report", "annualreport",
    "fortune 500", "fortune500", "enterprise", "corporation", "holdings",
    "global leader", "worldwide", "leading brand", "official store",
    "billion", "million revenue",
    # Retail / consumer signals
    "shop", "store", "buy now", "add to cart", "online shop",
    "ecommerce", "e-commerce", "retail store", "consumer", "home delivery",
    "amazon seller", "marketplace", "directory", "listings",
    "yellow pages", "buyer's guide", "coupon", "discount", "deal",
    "shopping", "cart", "checkout", "wishlist", "personal use",
]

# Content-relevance scoring: used after we fetch the homepage
B2B_CONTENT_POSITIVE = [
    "manufacturer", "factory", "wholesale", "wholesaler", "supplier",
    "oem", "odm", "export", "exporter", "bulk", "b2b", "trade",
    "trading", "custom", "private label", "moq", "production",
    "mill", "plant", "workshop", "industrial", "processing",
]

B2B_CONTENT_NEGATIVE = [
    "shop", "store", "buy now", "add to cart", "retail", "consumer",
    "ecommerce", "e-commerce", "online store", "shopping", "reviews",
    "blog", "news", "directory", "marketplace", "amazon", "ebay",
    "personal use", "home use", "gift", "cart", "checkout",
]


def score_search_result(title: str = "", snippet: str = "") -> int:
    """Score a search result based on title and snippet text.

    Higher scores suggest small distributors / importers (good targets).
    Lower / negative scores suggest news, blogs, big brands (bad targets).
    """
    text = f"{title} {snippet}".lower()
    score = 0
    for signal in POSITIVE_SIGNALS:
        if signal in text:
            score += 10
    for signal in NEGATIVE_SIGNALS:
        if signal in text:
            score -= 20
    return score


def build_enhanced_query(keyword: str, b2b_focus: bool = True) -> str:
    """Enhance raw keyword with B2B qualifiers to improve result relevance.

    If the user already included B2B terms, return as-is.
    Otherwise append manufacturer/wholesale/etc. using OR syntax.
    """
    if not b2b_focus:
        return keyword
    lower = keyword.lower()
    b2b_terms = ["manufacturer", "factory", "wholesale", "supplier", "oem", "odm"]
    if any(t in lower for t in b2b_terms):
        return keyword
    # DuckDuckGo / Google compatible OR syntax
    return f'{keyword} (manufacturer OR wholesale OR supplier OR factory OR "OEM" OR "ODM")'


def calculate_content_relevance(meta: dict, keyword: str) -> int:
    """Score domain relevance based on homepage content (title, desc, keywords, h1, about).

    Returns an integer score. Positive = likely B2B/manufacturer.
    Negative = likely retail/blog/directory.
    """
    text = " ".join([
        meta.get("title", ""),
        meta.get("description", ""),
        meta.get("keywords", ""),
        meta.get("h1", ""),
        meta.get("about_text", ""),
    ]).lower()

    if not text.strip():
        return 0

    score = 0
    for term in B2B_CONTENT_POSITIVE:
        if term in text:
            score += 15
    for term in B2B_CONTENT_NEGATIVE:
        if term in text:
            score -= 30

    # Reward if the actual keyword products appear on the page
    kw_parts = [p for p in keyword.lower().split() if len(p) > 3]
    for part in kw_parts:
        if part in text:
            score += 8

    return score


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class Lead:
    domain: str
    company: str = ""
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    position: str = ""
    department: str = ""
    confidence_score: int = 0          # Hunter confidence (0-100)
    email_type: str = ""               # personal | generic
    sources: List[str] = field(default_factory=list)
    validation_status: str = ""        # valid | invalid | catch-all | unknown
    search_keyword: str = ""
    found_at: str = ""
    country: str = ""
    website_description: str = ""
    relevance_score: int = 0
    linkedin_url: str = ""


# ---------------------------------------------------------------------------
# Website Description Scraper
# ---------------------------------------------------------------------------

def _strip_html_tags(html: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = " ".join(text.split())
    return unescape(text)


def _extract_linkedin_links(html: str) -> List[str]:
    """Extract linkedin.com/in/ profile URLs from raw HTML."""
    if not html:
        return []
    pattern = r'https?://(?:www\.)?linkedin\.com/in/[a-zA-Z0-9\-_%]+'
    matches = re.findall(pattern, html, flags=re.IGNORECASE)
    seen: set = set()
    results: List[str] = []
    for m in matches:
        url = m.lower().replace("http://", "https://").replace("www.", "")
        clean = "https://www." + url.replace("https://", "")
        if clean not in seen:
            seen.add(clean)
            results.append(clean)
    return results


def _fetch_about_page(domain: str, headers: dict, timeout: int) -> tuple:
    """Try common about-page paths and return (html, extracted_text)."""
    about_paths = ["/about", "/about-us", "/aboutus", "/company", "/our-company", "/who-we-are", "/team", "/staff", "/people"]
    for path in about_paths:
        url = f"https://{domain}{path}"
        try:
            resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            if resp.status_code != 200:
                continue
            html = resp.text
            matches = re.findall(r"<p[^>]*>(.*?)</p>", html, flags=re.IGNORECASE | re.DOTALL)
            for m in matches:
                text = _strip_html_tags(m).strip()
                if len(text) >= 30 and not text.lower().startswith(("home", "menu", "contact", "about us", "copyright")):
                    if len(text) > 600:
                        text = text[:600].rsplit(".", 1)[0] + "."
                    return html, text
        except Exception:
            continue
    return "", ""


def _fetch_about_text(domain: str, headers: dict, timeout: int) -> str:
    """Try common about-page paths and return the first substantial paragraph."""
    _, text = _fetch_about_page(domain, headers, timeout)
    return text


def search_linkedin_ddg(name: str, company: str, ddg_client=None) -> str:
    """Search LinkedIn profile via DuckDuckGo. Returns URL or empty string."""
    if not name or not company:
        return ""
    query = f'"{name}" "{company}" site:linkedin.com/in'
    try:
        if ddg_client and hasattr(ddg_client, 'search'):
            results = ddg_client.search(query, max_results=5)
        else:
            if DDGS is None:
                return ""
            with DDGS() as ddgs:
                raw = ddgs.text(query, max_results=5)
                results = [{"url": r.get("href"), "title": r.get("title", ""), "snippet": r.get("body", "")}
                           for r in raw if r.get("href")]
        for r in results:
            url = r.get("url", "")
            if re.search(r'linkedin\.com/in/[a-zA-Z0-9\-_%]+', url, re.IGNORECASE):
                return url
    except Exception:
        pass
    return ""


def _match_linkedin_by_name(first_name: str, last_name: str, links: List[str]) -> str:
    """Try to match a LinkedIn URL to a person by name slug similarity."""
    if not first_name or not last_name or not links:
        return ""
    name_slug = re.sub(r'[^a-z0-9]', '-', f"{first_name}-{last_name}".lower())
    name_slug_rev = re.sub(r'[^a-z0-9]', '-', f"{last_name}-{first_name}".lower())
    for url in links:
        slug = url.rstrip("/").split("/")[-1].lower()
        # Exact or partial match
        if name_slug in slug or name_slug_rev in slug:
            return url
        # Fuzzy: first name OR last name in slug
        if first_name.lower() in slug and last_name.lower() in slug:
            return url
    return ""


def fetch_domain_meta(domain: str, timeout: int = 10) -> dict:
    """
    Fetch homepage metadata (title, description, keywords, h1) plus about-page text.

    Returns a dict with keys: title, description, keywords, h1, about_text, linkedin_links.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    result = {"title": "", "description": "", "keywords": "", "h1": "", "about_text": "", "linkedin_links": []}
    homepage_html = ""

    # --- Homepage metadata ---
    try:
        url = f"https://{domain}"
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200:
            homepage_html = resp.text
            # title
            m = re.search(r"<title>(.*?)</title>", homepage_html, re.IGNORECASE | re.DOTALL)
            result["title"] = unescape(m.group(1)).strip() if m else ""
            # meta keywords
            m = re.search(
                r'<meta[^>]+name=["\']keywords["\'][^>]+content=["\'](.*?)["\']',
                homepage_html, flags=re.IGNORECASE,
            )
            if not m:
                m = re.search(
                    r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']keywords["\']',
                    homepage_html, flags=re.IGNORECASE,
                )
            result["keywords"] = unescape(m.group(1)).strip() if m else ""
            # h1
            m = re.search(r"<h1[^>]*>(.*?)</h1>", homepage_html, flags=re.IGNORECASE | re.DOTALL)
            result["h1"] = _strip_html_tags(m.group(1)).strip() if m else ""
            # meta description
            m = re.search(
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
                homepage_html, flags=re.IGNORECASE,
            )
            if not m:
                m = re.search(
                    r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']',
                    homepage_html, flags=re.IGNORECASE,
                )
            result["description"] = unescape(m.group(1)).strip() if m else ""
    except Exception:
        pass

    # --- About-page text + LinkedIn links ---
    about_html, about_text = _fetch_about_page(domain, headers, timeout)
    result["about_text"] = about_text
    all_linkedin = _extract_linkedin_links(homepage_html) + _extract_linkedin_links(about_html)
    seen: set = set()
    for url in all_linkedin:
        if url not in seen:
            seen.add(url)
            result["linkedin_links"].append(url)
    return result


def fetch_website_description(domain: str, timeout: int = 10) -> str:
    """Convenience wrapper: return the best description string for a domain."""
    meta = fetch_domain_meta(domain, timeout)
    return meta["about_text"] or meta["description"] or ""


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load config from file or environment variables."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    # Fallback: read from environment variables (for cloud deployment)
    # HUNTER_KEY can be a comma-separated list of keys for rotation
    env_config = {
        "serpapi_key": os.environ.get("SERPAPI_KEY", ""),
        "hunter_key": os.environ.get("HUNTER_KEY", ""),
        "snov_key": os.environ.get("SNOV_KEY", ""),
        "apollo_key": os.environ.get("APOLLO_KEY", ""),
        "zerobounce_key": os.environ.get("ZEROBOUNCE_KEY", ""),
    }
    if not env_config["hunter_key"] and not env_config["snov_key"] and not env_config["apollo_key"]:
        print(f"[ERROR] Config file not found: {CONFIG_PATH}")
        print("Please copy config.yaml.example to config.yaml and fill in your API keys.")
        print("Or set HUNTER_KEY / SNOV_KEY / APOLLO_KEY environment variables.")
        sys.exit(1)
    return env_config


def is_generic_email(email: str) -> bool:
    """Return True if email looks like a generic/department email."""
    prefix = email.split("@")[0].lower().strip()
    # Direct prefix match
    if prefix in GENERIC_PREFIXES:
        return True
    # Contains generic substring
    for gen in GENERIC_PREFIXES:
        if prefix == gen or prefix.startswith(gen + "-") or prefix.startswith(gen + "_"):
            return True
        if prefix.startswith(gen) and prefix[len(gen):].isdigit():
            return True  # sales1, info2
    return False


def looks_personal(email: str) -> bool:
    """Heuristic: does the email prefix look like a person's name?"""
    prefix = email.split("@")[0].lower()
    for pattern in PERSONAL_PATTERNS:
        if pattern.match(prefix):
            return True
    # Single first name (short, alphabetic) - moderate confidence
    if 3 <= len(prefix) <= 10 and prefix.isalpha():
        return True
    return False


def extract_domain(url: str) -> Optional[str]:
    """Extract clean domain from a URL."""
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return None


# ---------------------------------------------------------------------------
# API Clients
# ---------------------------------------------------------------------------

class SerpAPIClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://serpapi.com/search"

    def search(self, query: str, num_results: int = 100, pages: int = 1, skip_pages: int = 0) -> List[dict]:
        """
        Search Google and return list of result dicts with url, title, snippet.
        SerpAPI returns 10 results per page by default.
        skip_pages: number of initial pages to skip (deep search mode).
        """
        results = []
        for page in range(skip_pages, skip_pages + pages):
            start = page * 10
            params = {
                "q": query,
                "engine": "google",
                "api_key": self.api_key,
                "num": min(10, num_results - start),
                "start": start,
            }
            try:
                resp = requests.get(self.base_url, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                organic = data.get("organic_results", [])
                for result in organic:
                    link = result.get("link")
                    if link:
                        results.append({
                            "url": link,
                            "title": result.get("title", ""),
                            "snippet": result.get("snippet") or result.get("description", ""),
                        })
                print(f"  [SerpAPI] Page {page + 1} (skipped first {skip_pages}): {len(organic)} results")
                if not organic:
                    break
            except requests.exceptions.RequestException as e:
                print(f"  [SerpAPI ERROR] Page {page + 1}: {e}")
                break
            time.sleep(1)  # Rate limit
        return results


class DuckDuckGoClient:
    """Free alternative to SerpAPI - no API key required."""

    def search(self, query: str, max_results: int = 100) -> List[dict]:
        """Search DuckDuckGo and return result dicts with url, title, snippet."""
        results = []
        if DDGS is None:
            print("  [ERROR] duckduckgo-search library not installed. Run: pip install duckduckgo-search")
            return results

        try:
            with DDGS() as ddgs:
                raw = ddgs.text(query, max_results=max_results)
                for r in raw:
                    link = r.get("href")
                    if link:
                        results.append({
                            "url": link,
                            "title": r.get("title", ""),
                            "snippet": r.get("body") or r.get("snippet", ""),
                        })
            print(f"  [DuckDuckGo] Found {len(results)} results")
        except Exception as e:
            print(f"  [DuckDuckGo ERROR] {e}")
        return results


class BrowserClient:
    """
    Browser automation search via Playwright.
    Opens a real Chromium browser, navigates to DuckDuckGo,
    performs the search, and extracts result URLs.
    More reliable than scraping but slower than API libraries.
    """

    def search(self, query: str, max_results: int = 100, skip_pages: int = 0) -> List[dict]:
        results = []
        if sync_playwright is None:
            print("  [ERROR] Playwright not installed. Run: pip install playwright && playwright install chromium")
            return results

        try:
            with sync_playwright() as p:
                # Launch headless browser
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                page = context.new_page()
                page.set_default_timeout(60000)

                # Use DuckDuckGo HTML version (no JS, faster, automation-friendly)
                print("  [Browser] Launching Chromium...")
                page.goto("https://html.duckduckgo.com/html/", timeout=60000)
                page.fill('input[name="q"]', query)
                page.press('input[name="q"]', "Enter")
                page.wait_for_load_state("networkidle", timeout=60000)

                # Skip initial pages for deep search
                for _ in range(skip_pages):
                    next_btn = page.query_selector("input[value='Next']")
                    if not next_btn:
                        break
                    next_btn.click()
                    page.wait_for_load_state("networkidle", timeout=60000)
                    time.sleep(1.5)

                collected = 0
                while collected < max_results:
                    # Extract results with title and snippet in one go
                    page_results = page.evaluate("""
                        () => Array.from(document.querySelectorAll('.result')).map(r => {
                            const a = r.querySelector('a.result__a');
                            const s = r.querySelector('.result__snippet');
                            return {
                                url: a ? a.href : '',
                                title: a ? a.innerText.trim() : '',
                                snippet: s ? s.innerText.trim() : ''
                            };
                        }).filter(r => r.url)
                    """)
                    for item in page_results:
                        if collected >= max_results:
                            break
                        results.append(item)
                        collected += 1

                    # Try to go to next page
                    if collected >= max_results:
                        break
                    next_btn = page.query_selector("input[value='Next']")
                    if not next_btn:
                        break
                    next_btn.click()
                    page.wait_for_load_state("networkidle", timeout=30000)
                    time.sleep(1.5)  # Be polite

                browser.close()
                print(f"  [Browser] Found {len(results)} results (skipped first {skip_pages} pages)")
        except Exception as e:
            print(f"  [Browser ERROR] {e}")
        return results


class HunterClient:
    def __init__(self, api_keys: str):
        """
        Accept a single key or comma-separated multiple keys.
        Example: "key1,key2,key3"
        """
        self.api_keys = [k.strip() for k in api_keys.split(",") if k.strip()]
        self._idx = 0
        self.base_url = "https://api.hunter.io/v2"
        self._dead_keys: set = set()  # keys that returned 429/403

    def _current_key(self) -> str:
        return self.api_keys[self._idx % len(self.api_keys)]

    def _rotate(self):
        """Move to next available key."""
        start = self._idx
        for _ in range(len(self.api_keys)):
            self._idx += 1
            if self._current_key() not in self._dead_keys:
                return True
        # All keys dead
        return False

    def _call(self, endpoint: str, params: dict) -> dict:
        """Make a Hunter API call with automatic key rotation on rate limit."""
        url = f"{self.base_url}/{endpoint}"
        attempts = 0
        max_attempts = len(self.api_keys) * 2

        while attempts < max_attempts:
            key = self._current_key()
            params["api_key"] = key
            try:
                resp = requests.get(url, params=params, timeout=15)
                if resp.status_code == 429:
                    print(f"    [Hunter] Key {self._idx + 1} rate limited. Rotating...")
                    self._dead_keys.add(key)
                    if not self._rotate():
                        print(f"    [Hunter] All keys exhausted.")
                        return {}
                    attempts += 1
                    time.sleep(2)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.HTTPError as e:
                if resp.status_code == 400:
                    try:
                        err_body = resp.json()
                        err_msg = err_body.get("errors", [{}])[0].get("details", "Bad Request")
                        print(f"    [Hunter ERROR]: {err_msg}")
                    except Exception:
                        print(f"    [Hunter ERROR]: {e}")
                else:
                    print(f"    [Hunter ERROR]: {e}")
                return {}
            except Exception as e:
                print(f"    [Hunter ERROR]: {e}")
                return {}
        return {}

    def domain_search(self, domain: str, limit: int = 10) -> List[dict]:
        """Return list of email dicts for a domain."""
        data = self._call("domain-search", {"domain": domain, "limit": limit})
        return data.get("data", {}).get("emails", [])

    def email_finder(self, domain: str, first_name: str, last_name: str) -> Optional[dict]:
        """Guess email format for a specific person."""
        data = self._call("email-finder", {
            "domain": domain,
            "first_name": first_name,
            "last_name": last_name,
        })
        result = data.get("data", {})
        return result if result.get("email") else None


class SnovClient:
    """Snov.io - Alternative email discovery API."""

    def __init__(self, access_token: str):
        self.access_token = access_token
        self.base_url = "https://api.snov.io/v1"

    def domain_search(self, domain: str, limit: int = 10) -> List[dict]:
        """Return list of email dicts for a domain."""
        url = f"{self.base_url}/get-domain-emails"
        params = {
            "domain": domain,
            "access_token": self.access_token,
            "type": "all",
            "limit": limit,
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("success"):
                print(f"    [Snov] API returned success=false for {domain}")
                return []
            # Normalize Snov response to match Hunter format
            results = []
            for item in data.get("result", []):
                results.append({
                    "value": item.get("email", ""),
                    "type": item.get("type", "personal").lower(),
                    "confidence": 80 if item.get("type", "").lower() == "personal" else 50,
                    "first_name": item.get("firstName", ""),
                    "last_name": item.get("lastName", ""),
                    "position": item.get("position", ""),
                    "department": "",
                    "sources": [{"domain": "snov.io"}],
                })
            return results
        except requests.exceptions.HTTPError as e:
            if resp.status_code == 429:
                print(f"    [Snov] Rate limited on {domain}. Sleeping 3s...")
                time.sleep(3)
            else:
                print(f"    [Snov ERROR] {domain}: {e}")
            return []
        except Exception as e:
            print(f"    [Snov ERROR] {domain}: {e}")
            return []


class ApolloClient:
    """Apollo.io - B2B contact database API (3rd fallback)."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.apollo.io/v1"

    def domain_search(self, domain: str, limit: int = 50) -> List[dict]:
        """Search contacts by domain. Returns list of email dicts."""
        url = f"{self.base_url}/mixed_people/search"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
        }
        payload = {
            "q_organization_domains": [domain],
            "per_page": min(limit, 100),
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=20)
            if resp.status_code == 429:
                print(f"    [Apollo] Rate limited on {domain}. Sleeping 5s...")
                time.sleep(5)
                return []
            if resp.status_code == 403:
                data = resp.json() if resp.text else {}
                err = data.get("error", "")
                if "free plan" in err.lower():
                    print("    [Apollo] Free plan key cannot use People Search. Skipping Apollo.")
                else:
                    print(f"    [Apollo] Forbidden (403): {err}")
                return []
            resp.raise_for_status()
            data = resp.json()
            people = data.get("people", []) if isinstance(data, dict) else []
            results = []
            for p in people:
                email = (p.get("email") or "").lower().strip()
                if not email:
                    continue
                name = p.get("name") or ""
                first_name = p.get("first_name") or ""
                last_name = p.get("last_name") or ""
                if not first_name and name:
                    parts = name.split()
                    first_name = parts[0]
                    last_name = " ".join(parts[1:]) if len(parts) > 1 else ""
                results.append({
                    "value": email,
                    "type": "personal",
                    "confidence": 70,
                    "first_name": first_name,
                    "last_name": last_name,
                    "position": p.get("title") or p.get("job_title") or "",
                    "department": p.get("department") or "",
                    "linkedin_url": p.get("linkedin_url") or "",
                    "sources": [{"domain": "apollo.io"}],
                })
            return results
        except requests.exceptions.HTTPError as e:
            print(f"    [Apollo ERROR] {domain}: {e}")
            return []
        except Exception as e:
            print(f"    [Apollo ERROR] {domain}: {e}")
            return []


class ZeroBounceClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.zerobounce.net/v2"

    def validate(self, email: str) -> dict:
        """Validate a single email. Returns status info."""
        url = f"{self.base_url}/validate"
        params = {
            "api_key": self.api_key,
            "email": email,
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"    [ZeroBounce ERROR] {email}: {e}")
            return {"status": "unknown", "error": str(e)}


# ---------------------------------------------------------------------------
# Core Pipeline
# ---------------------------------------------------------------------------

class LeadFinder:
    def __init__(self, config: dict, engine: str = "auto", extra_excluded: Optional[Set[str]] = None):
        self.config = config
        self.engine = engine
        self.excluded_domains = set(EXCLUDED_DOMAINS)
        if extra_excluded:
            self.excluded_domains.update(d.lower().strip() for d in extra_excluded)

        # Initialize all search clients
        self.serp = None
        self.ddg = DuckDuckGoClient()
        self.browser = BrowserClient()

        if config.get("serpapi_key") and config["serpapi_key"] not in ("", "YOUR_SERPAPI_KEY_HERE"):
            self.serp = SerpAPIClient(config["serpapi_key"])

        self.hunter = HunterClient(config["hunter_key"])
        self.snov = None
        if config.get("snov_key") and config["snov_key"] not in ("", "YOUR_SNOV_KEY_HERE"):
            self.snov = SnovClient(config["snov_key"])
        self.apollo = None
        if config.get("apollo_key") and config["apollo_key"] not in ("", "YOUR_APOLLO_KEY_HERE"):
            self.apollo = ApolloClient(config["apollo_key"])
        self.zerobounce = None
        if config.get("zerobounce_key"):
            self.zerobounce = ZeroBounceClient(config["zerobounce_key"])

    def _resolve_engine(self) -> str:
        """Pick the best available search engine."""
        if self.engine != "auto":
            return self.engine
        # Priority: duckduckgo > browser > serpapi
        if DDGS is not None:
            return "duckduckgo"
        if sync_playwright is not None:
            return "browser"
        if self.serp is not None:
            return "serpapi"
        print("[FATAL] No search engine available. Install duckduckgo-search or playwright.")
        sys.exit(1)

    def run(
        self,
        keyword: str,
        pages: int = 5,
        output: str = "leads.csv",
        validate: bool = False,
        max_domains: Optional[int] = None,
        deep: bool = False,
        b2b_focus: bool = True,
        min_relevance: int = -50,
    ) -> None:
        timestamp = datetime.now().isoformat()
        engine = self._resolve_engine()
        skip_pages = 5 if deep else 0
        search_query = build_enhanced_query(keyword, b2b_focus=b2b_focus)

        print(f"\n{'='*60}")
        print(f"  B2B Lead Finder")
        print(f"  Keyword : {keyword}")
        if b2b_focus and search_query != keyword:
            print(f"  Query   : {search_query}")
        print(f"  Engine  : {engine}")
        print(f"  Pages   : {pages}")
        if deep:
            print(f"  Mode    : DEEP (skip first {skip_pages} pages)")
        print(f"  Output  : {output}")
        print(f"{'='*60}\n")

        # 1. Search
        if engine == "serpapi" and self.serp:
            print("[1/5] Searching Google via SerpAPI...")
            raw_results = self.serp.search(search_query, pages=pages, skip_pages=skip_pages)
        elif engine == "browser":
            print("[1/5] Searching via Browser (Playwright + DuckDuckGo)...")
            raw_results = self.browser.search(search_query, max_results=pages * 10, skip_pages=skip_pages)
        else:
            print("[1/5] Searching via DuckDuckGo (free, no API key)...")
            raw_results = self.ddg.search(search_query, max_results=(skip_pages + pages) * 10)
            if deep:
                skip_count = skip_pages * 10
                if len(raw_results) > skip_count:
                    raw_results = raw_results[skip_count:]
                else:
                    print(f"  [DuckDuckGo] Only {len(raw_results)} results returned, keeping all (deep skip not applied).")
        print(f"      Total results found: {len(raw_results)}")

        # 2. Score, filter, and extract unique domains
        print("\n[2/5] Scoring and extracting domains...")
        domain_scores: Dict[str, int] = {}
        skipped = 0
        for item in raw_results:
            url = item.get("url") if isinstance(item, dict) else item
            title = item.get("title", "") if isinstance(item, dict) else ""
            snippet = item.get("snippet", "") if isinstance(item, dict) else ""
            domain = extract_domain(url)
            if not domain:
                continue
            if domain in self.excluded_domains:
                skipped += 1
                continue
            score = score_search_result(title, snippet)
            # Keep the highest score for each domain
            if domain not in domain_scores or score > domain_scores[domain]:
                domain_scores[domain] = score

        # Sort by score descending — small distributors bubble to the top
        sorted_domains = sorted(domain_scores.items(), key=lambda x: x[1], reverse=True)
        domains = [d for d, s in sorted_domains]
        if max_domains:
            domains = domains[:max_domains]
        print(f"      Unique domains: {len(domains)} (excluded {skipped} big-brand domains)")
        pos = sum(1 for _, s in sorted_domains if s > 0)
        neg = sum(1 for _, s in sorted_domains if s < 0)
        if pos or neg:
            print(f"      Score distribution: {pos} positive, {neg} negative")

        # 3. Fetch website descriptions and calculate content relevance
        print("\n[3/5] Fetching website metadata and scoring relevance...")
        domain_descriptions: Dict[str, str] = {}
        domain_relevance: Dict[str, int] = {}
        domain_linkedin_links: Dict[str, List[str]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            future_to_domain = {executor.submit(fetch_domain_meta, d): d for d in domains}
            for future in concurrent.futures.as_completed(future_to_domain):
                domain = future_to_domain[future]
                try:
                    meta = future.result()
                    desc = meta["about_text"] or meta["description"] or ""
                    domain_descriptions[domain] = desc
                    domain_relevance[domain] = calculate_content_relevance(meta, keyword)
                    domain_linkedin_links[domain] = meta.get("linkedin_links", [])
                except Exception:
                    domain_descriptions[domain] = ""
                    domain_relevance[domain] = 0
                    domain_linkedin_links[domain] = []
        fetched = sum(1 for d in domain_descriptions.values() if d)
        rel_high = sum(1 for r in domain_relevance.values() if r >= 20)
        rel_low = sum(1 for r in domain_relevance.values() if r < 0)
        linkedin_found = sum(1 for links in domain_linkedin_links.values() if links)
        print(f"      Fetched descriptions for {fetched}/{len(domains)} domains")
        print(f"      Relevance: {rel_high} high, {rel_low} low")
        print(f"      LinkedIn profiles found on websites: {linkedin_found}")

        # 3b. Filter out low-relevance domains
        before_filter = len(domains)
        domains = [d for d in domains if domain_relevance.get(d, 0) >= min_relevance]
        after_filter = len(domains)
        if after_filter < before_filter:
            print(f"      Filtered out {before_filter - after_filter} low-relevance domains (min={min_relevance})")

        # 4. Find emails via Hunter.io + Snov.io + Apollo.io (merge mode)
        sources_label = "Hunter.io"
        if self.snov:
            sources_label += " + Snov.io"
        if self.apollo:
            sources_label += " + Apollo.io"
        print(f"\n[4/5] Finding emails via {sources_label} (merge & enrich)...")
        all_leads: List[Lead] = []
        for idx, domain in enumerate(domains, 1):
            print(f"  [{idx}/{len(domains)}] {domain} ...", end=" ", flush=True)
            raw_emails: List[dict] = []
            source_name = "hunter.io"

            # Query all available sources and merge
            email_map: dict = {}
            source_tags: List[str] = []

            hunter_emails = self.hunter.domain_search(domain)
            if hunter_emails:
                source_tags.append("hunter.io")
                for e in hunter_emails:
                    email = (e.get("value") or "").lower().strip()
                    if email and "@" in email:
                        email_map[email] = dict(e)
                        email_map[email]["_sources"] = ["hunter.io"]

            if self.snov:
                snov_emails = self.snov.domain_search(domain)
                if snov_emails:
                    source_tags.append("snov.io")
                    for e in snov_emails:
                        email = (e.get("value") or "").lower().strip()
                        if email and "@" in email:
                            if email in email_map:
                                email_map[email]["_sources"].append("snov.io")
                                # Snov may have better confidence / names
                                if e.get("first_name") and not email_map[email].get("first_name"):
                                    email_map[email]["first_name"] = e["first_name"]
                                if e.get("last_name") and not email_map[email].get("last_name"):
                                    email_map[email]["last_name"] = e["last_name"]
                                if e.get("position") and not email_map[email].get("position"):
                                    email_map[email]["position"] = e["position"]
                            else:
                                email_map[email] = dict(e)
                                email_map[email]["_sources"] = ["snov.io"]

            if self.apollo:
                apollo_emails = self.apollo.domain_search(domain)
                if apollo_emails:
                    source_tags.append("apollo.io")
                    for e in apollo_emails:
                        email = (e.get("value") or "").lower().strip()
                        if email and "@" in email:
                            if email in email_map:
                                email_map[email]["_sources"].append("apollo.io")
                                # Apollo enrichment: linkedin_url, names, position
                                if e.get("linkedin_url") and not email_map[email].get("linkedin_url"):
                                    email_map[email]["linkedin_url"] = e["linkedin_url"]
                                if e.get("first_name") and not email_map[email].get("first_name"):
                                    email_map[email]["first_name"] = e["first_name"]
                                if e.get("last_name") and not email_map[email].get("last_name"):
                                    email_map[email]["last_name"] = e["last_name"]
                                if e.get("position") and not email_map[email].get("position"):
                                    email_map[email]["position"] = e["position"]
                                if e.get("department") and not email_map[email].get("department"):
                                    email_map[email]["department"] = e["department"]
                            else:
                                email_map[email] = dict(e)
                                email_map[email]["_sources"] = ["apollo.io"]

            raw_emails = list(email_map.values())
            source_name = " + ".join(source_tags) if source_tags else "none"

            if not raw_emails:
                print("0 found")
                time.sleep(0.7)
                continue

            kept = 0
            for e in raw_emails:
                email = (e.get("value") or "").lower().strip()
                if not email or "@" not in email:
                    continue
                if is_generic_email(email):
                    continue

                h_type = e.get("type", "").lower()
                confidence = e.get("confidence", 0) or 0

                # Prefer personal tag; fallback to regex heuristic
                is_personal = h_type == "personal" or looks_personal(email)
                if not is_personal and confidence < 50:
                    continue

                # Resolve LinkedIn URL: Apollo > website match > empty (DDG later)
                linkedin_url = e.get("linkedin_url", "")
                if not linkedin_url:
                    links = domain_linkedin_links.get(domain, [])
                    linkedin_url = _match_linkedin_by_name(
                        e.get("first_name", ""), e.get("last_name", ""), links
                    )

                lead = Lead(
                    domain=domain,
                    company=e.get("domain", domain),
                    email=email,
                    first_name=e.get("first_name", ""),
                    last_name=e.get("last_name", ""),
                    position=e.get("position", ""),
                    department=e.get("department", ""),
                    confidence_score=confidence,
                    email_type="personal" if is_personal else "generic",
                    sources=e.get("_sources", [source_name]),
                    search_keyword=keyword,
                    found_at=timestamp,
                    country=detect_country(domain, email),
                    website_description=domain_descriptions.get(domain, ""),
                    relevance_score=domain_relevance.get(domain, 0),
                    linkedin_url=linkedin_url,
                )
                all_leads.append(lead)
                kept += 1

            print(f"{kept} kept / {len(raw_emails)} total ({source_name})")
            time.sleep(0.7)

        print(f"\n      Total leads after filtering: {len(all_leads)}")

        # 4b. Supplement LinkedIn URLs via DuckDuckGo for leads still missing one
        leads_without_linkedin = [l for l in all_leads if not l.linkedin_url and l.first_name and l.last_name]
        if leads_without_linkedin:
            max_ddg_searches = min(len(leads_without_linkedin), 30)  # cap to avoid slowness
            print(f"\n[4b/5] Searching LinkedIn via DuckDuckGo for {max_ddg_searches} leads...")
            ddg_found = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                future_to_lead = {}
                for lead in leads_without_linkedin[:max_ddg_searches]:
                    name = f"{lead.first_name} {lead.last_name}"
                    future = executor.submit(search_linkedin_ddg, name, lead.company)
                    future_to_lead[future] = lead
                for future in concurrent.futures.as_completed(future_to_lead):
                    lead = future_to_lead[future]
                    try:
                        url = future.result()
                        if url:
                            lead.linkedin_url = url
                            ddg_found += 1
                    except Exception:
                        pass
            print(f"      Found {ddg_found} LinkedIn profiles via DuckDuckGo")

        # 6. Optional email validation
        if validate and self.zerobounce:
            print("\n[6/6] Validating emails via ZeroBounce...")
            for idx, lead in enumerate(all_leads, 1):
                print(f"  [{idx}/{len(all_leads)}] {lead.email} ...", end=" ", flush=True)
                result = self.zerobounce.validate(lead.email)
                status = result.get("status", "unknown")
                lead.validation_status = status
                print(status)
                time.sleep(0.5)
        else:
            print("\n[6/6] Skipping email validation (pass --validate to enable)")

        # 6. Deduplicate by email
        seen_emails: Set[str] = set()
        unique_leads: List[Lead] = []
        for lead in all_leads:
            if lead.email not in seen_emails:
                seen_emails.add(lead.email)
                unique_leads.append(lead)

        # 6. Export
        self._export_csv(unique_leads, output)
        self._print_summary(unique_leads, keyword, len(domains))

    def _export_csv(self, leads: List[Lead], path: str) -> None:
        if not leads:
            print("\n[WARNING] No leads found. Nothing to export.")
            return
        fieldnames = [
            "email", "first_name", "last_name", "position", "department",
            "company", "domain", "country", "confidence_score", "email_type",
            "validation_status", "sources", "search_keyword", "found_at",
            "website_description", "relevance_score", "linkedin_url",
        ]
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for lead in leads:
                row = asdict(lead)
                row["sources"] = "; ".join(row["sources"])
                writer.writerow(row)
        print(f"\n[OK] Exported {len(leads)} leads to: {path}")

    def _print_summary(self, leads: List[Lead], keyword: str, domains_searched: int) -> None:
        if not leads:
            return
        personal = sum(1 for l in leads if l.email_type == "personal")
        high_conf = sum(1 for l in leads if l.confidence_score >= 80)
        validated = sum(1 for l in leads if l.validation_status == "valid")
        print(f"\n{'='*60}")
        print(f"  SUMMARY")
        print(f"{'='*60}")
        print(f"  Keyword          : {keyword}")
        print(f"  Domains searched : {domains_searched}")
        print(f"  Unique leads     : {len(leads)}")
        print(f"  Personal emails  : {personal}")
        print(f"  High confidence  : {high_conf}")
        if validated:
            print(f"  Validated (OK)   : {validated}")
        print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="B2B Lead Finder - Discover decision-maker emails from Google searches."
    )
    parser.add_argument("keyword", help="Search keyword(s), e.g. 'football gloves manufacturer'")
    parser.add_argument("--pages", type=int, default=5, help="Number of Google result pages to scan (default: 5)")
    parser.add_argument("--output", type=str, default="leads.csv", help="Output CSV file path (default: leads.csv)")
    parser.add_argument("--validate", action="store_true", help="Validate emails via ZeroBounce (requires API key)")
    parser.add_argument("--max-domains", type=int, default=None, help="Limit number of domains to process")
    parser.add_argument(
        "--engine",
        type=str,
        default="auto",
        choices=["auto", "duckduckgo", "browser", "serpapi"],
        help="Search engine: auto (default), duckduckgo, browser (Playwright), or serpapi",
    )
    parser.add_argument(
        "--exclude",
        type=str,
        default="",
        help="Extra domains to exclude, comma-separated (e.g. 'nike.com,adidas.com')",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Deep search: skip first 5 pages to avoid big-brand results",
    )
    parser.add_argument(
        "--no-b2b-focus",
        action="store_true",
        help="Disable automatic B2B keyword enhancement (manufacturer/wholesale/etc.)",
    )
    parser.add_argument(
        "--min-relevance",
        type=int,
        default=-50,
        help="Minimum content-relevance score for a domain to be processed (default: -50, use 0 for stricter filtering)",
    )
    args = parser.parse_args()

    extra_excluded = set(d.strip().lower() for d in args.exclude.split(",") if d.strip())
    config = load_config()
    finder = LeadFinder(config, engine=args.engine, extra_excluded=extra_excluded)
    finder.run(
        keyword=args.keyword,
        pages=args.pages,
        output=args.output,
        validate=args.validate,
        max_domains=args.max_domains,
        deep=args.deep,
        b2b_focus=not args.no_b2b_focus,
        min_relevance=args.min_relevance,
    )


if __name__ == "__main__":
    main()
