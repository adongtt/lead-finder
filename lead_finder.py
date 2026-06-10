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
from urllib.parse import quote_plus, urlparse

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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

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

# ---------------------------------------------------------------------------
# Apollo relevance scoring
# ---------------------------------------------------------------------------

APOLLO_POSITIVE_INDUSTRIES = {
    "sporting goods", "sports", "retail", "wholesale", "apparel", "fashion",
    "manufacturing", "consumer goods", "consumer products", "outdoor",
    "recreation", "leisure", "fitness", "athletic", "footwear", "textiles",
    "distribution", "import and export", "import/export",
}

APOLLO_NEGATIVE_INDUSTRIES = {
    "healthcare", "health", "medical", "dental", "hospital", "clinic",
    "software", "technology", "information technology", "it services",
    "computer software", "internet", "telecommunications",
    "finance", "financial services", "banking", "insurance", "investment",
    "real estate", "construction", "hospitality", "hotel", "resort",
    "restaurant", "food", "aviation", "aerospace", "airlines",
    "government", "non-profit", "nonprofit", "education", "logistics",
    "transportation", "oil", "energy", "mining", "pharmaceutical",
    "biotechnology", "legal", "law practice", "accounting",
}

APOLLO_POSITIVE_KEYWORDS = {
    "sport", "sports", "athletic", "athletics", "fitness", "outdoor",
    "equipment", "gear", "apparel", "uniform", "glove", "gloves",
    "football", "baseball", "cycling", "bike", "ski", "snowboard",
    "gym", "workout", "training", "team", "game", "player",
    "distributor", "wholesale", "importer", "supplier", "dealer",
    "retail", "merchant", "trading", "trade",
}

APOLLO_NEGATIVE_KEYWORDS = {
    "health", "medical", "dental", "hospital", "clinic", "patient",
    "software", "saas", "tech", "technology", "cloud", "ai", "data",
    "resort", "spa", "hotel", "hospitality", "restaurant", "cafe",
    "aviation", "aircraft", "airline", "plane", "maintenance",
    "government", "municipal", "county", "federal", "state",
    "nonprofit", "charity", "foundation", "ngo",
    "education", "school", "university", "college",
    "pharmaceutical", "drug", "biotech", "laboratory", "lab",
    "finance", "bank", "investment", "insurance", "mortgage",
    "legal", "law firm", "attorney", "lawyer",
    "construction", "contractor", "builder", "engineering",
    "oil", "gas", "energy", "petroleum", "mining",
    "real estate", "property", "rental",
}

def _score_apollo_contact_relevance(person: dict) -> int:
    """Score Apollo contact relevance based on org industry, keywords, description.
    Returns integer score. Negative = likely irrelevant.
    """
    score = 0
    org = person.get("_enriched_org", {}) or {}
    if not org:
        return 0

    org_name = (org.get("name") or "").lower()
    industry = (org.get("industry") or "").lower()
    industries = [i.lower() for i in org.get("industries", [])]
    keywords = [k.lower() for k in org.get("keywords", [])]
    description = (org.get("short_description") or "").lower()
    person_title = (person.get("position") or "").lower()

    # Industry scoring (soft penalties to avoid over-filtering)
    if industry:
        for pos in APOLLO_POSITIVE_INDUSTRIES:
            if pos in industry:
                score += 20
        for neg in APOLLO_NEGATIVE_INDUSTRIES:
            if neg in industry:
                score -= 15
    for ind in industries:
        for pos in APOLLO_POSITIVE_INDUSTRIES:
            if pos in ind:
                score += 12
        for neg in APOLLO_NEGATIVE_INDUSTRIES:
            if neg in ind:
                score -= 10

    # Keywords scoring
    all_text = " ".join(keywords)
    for pos in APOLLO_POSITIVE_KEYWORDS:
        if pos in all_text:
            score += 10
    for neg in APOLLO_NEGATIVE_KEYWORDS:
        if neg in all_text:
            score -= 8

    # Description scoring
    if description:
        for pos in APOLLO_POSITIVE_KEYWORDS:
            if pos in description:
                score += 8
        for neg in APOLLO_NEGATIVE_KEYWORDS:
            if neg in description:
                score -= 6

    # Company name scoring
    if org_name:
        for pos in APOLLO_POSITIVE_KEYWORDS:
            if pos in org_name:
                score += 10
        for neg in APOLLO_NEGATIVE_KEYWORDS:
            if neg in org_name:
                score -= 10

    # Title scoring (bonus for purchasing roles)
    if any(t in person_title for t in ["buyer", "purchasing", "procurement", "sourcing"]):
        score += 5

    return score


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
    # E-commerce giants / big retailers
    "made-in-china.com", "amazon.com", "espn.com",
    "ebay.com", "ebay.co.uk", "ebay.de","nfl.com","rei.com",
    "walmart.com", "target.com", "bestbuy.com", "costco.com",
    "alibaba.com", "aliexpress.com", "taobao.com", "tmall.com", "jd.com",
    "etsy.com", "wayfair.com", "overstock.com", "newegg.com",
    "prodirectsport.com", "sportsdirect.com", "decathlon.com",
    # B2B wholesale platforms / directories (not independent distributors)
    "tradeindia.com", "indiamart.com", "globalsources.com",
    "dhgate.com", "1688.com", "ec21.com", "ecplaza.net",
    "b2bmit.com", "toboc.com", "impexlb.com", "matchory.com",
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
    # Software review / B2B directory platforms
    "g2.com", "g2crowd.com", "capterra.com", "trustpilot.com",
    "getapp.com", "softwareadvice.com", "crows.com",
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
    # Additional wholesale / marketplace platforms
    "thomasnet.com", "kompass.com", " europages.com",
    "yellowpages.com", "yellowpages.ca", "yelp.com",
    "bbb.org", "manta.com", "zoominfo.com",
    # Big generic manufacturers / OEM directories
    "panjiva.com", "volza.com", "exportgenius.in",
    # More social / content platforms
    "crunchbase.com", "owler.com", "glassdoor.com",
    "indeed.com", "simplyhired.com", "monster.com",

    "puma.com", "puma.de", "puma.co.uk",
    "newbalance.com", "newbalance.co.uk",
    "asics.com", "mizuno.com", "mizunousa.com",
    "rawlings.com", "wilson.com", "wilsonsports.com",
    "easton.com", "demarini.com", "louisville-slugger.com",
    "franklinsports.com", "schutt.com", "riiddell.com",
    "cuttersgloves.com", "cutterssports.com", "gripboost.com", "battle.net",
    # Work-safety glove brands
    "ironclad.com", "ironcladperformancewear.com",
    # Others
    "yelp.com", "trip.com", "glassdoor.com", "indeed.com", "monster.com",
    "ziprecruiter.com", "craigslist.org", "gumtree.com",
    # Unrelated / false positives seen in tests
    "the-north-pole.com", "flighttothenorthpole.org",
    # Automotive (motorcycle brands with riding gear/gloves)
    "bmw.com", "bmwmotorcycles.com",
}

# Major B2B directories / wholesale platforms that flood results.
# When "exclude big platforms" is enabled, these are appended as `-site:` exclusions.
BIG_PLATFORMS = {
    "alibaba.com", "aliexpress.com", "made-in-china.com",
    "tradeindia.com", "indiamart.com", "globalsources.com",
    "dhgate.com", "1688.com", "ec21.com", "ecplaza.net",
    "thomasnet.com", "kompass.com", "europages.com",
    "yellowpages.com", "yelp.com", "bbb.org",
    "panjiva.com", "volza.com", "exportgenius.in",
    "amazon.com", "ebay.com", "walmart.com",
    "g2.com", "capterra.com", "trustpilot.com",
    "faire.com", "faire.co.uk", "ankorstore.com",
    "aboundwholesale.com", "tundra.com",
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
    # Distributor / importer signals (higher priority for lead finding)
    "distributor", "wholesale", "wholesaler", "importer", "import",
    "dealer", "reseller", "retailer", "stockist", "agent",
    "distribution", "trading", "trading company",
    # Manufacturing signals (still relevant but secondary)
    "manufacturer", "factory", "supplier", "oem", "odm",
    "export", "exporter", "bulk", "b2b", "trade",
    "custom", "private label", "moq",
]

B2B_CONTENT_NEGATIVE = [
    "news", "blog", "article", "magazine", "press release",
    "review", "top 10", "comparison", "guide", "tutorial",
    "jobs", "careers", "hiring", "wikipedia",
    "shop", "buy now", "add to cart", "online shop",
    "ecommerce", "e-commerce", "consumer", "home delivery",
    "marketplace", "directory", "listings", "yellow pages",
    "coupon", "discount", "deal", "shopping", "cart", "checkout",
]


def build_enhanced_query(
    keyword: str,
    b2b_focus: bool = True,
    exclude_big_platforms: bool = False,
    advanced_syntax: bool = False,
) -> str:
    """Enhance raw keyword with B2B qualifiers and optional exclusions.

    Prioritizes distributor/importer/dealer terms since those are the
    actual buyers/decision-makers for lead generation.
    """
    query = keyword.strip()
    if not query:
        return query

    # Advanced syntax: wrap bare keyword in intitle for tighter matching
    if advanced_syntax and not query.lower().startswith("intitle:"):
        core = query.strip('"')
        query = f'intitle:"{core}" {core}'

    if b2b_focus:
        lower = query.lower()
        b2b_terms = ["manufacturer", "factory", "wholesale", "supplier", "oem", "odm",
                     "distributor", "importer", "dealer", "reseller"]
        if not any(t in lower for t in b2b_terms):
            query = f"{query} (distributor OR importer OR wholesaler OR dealer)"

    if exclude_big_platforms:
        for site in sorted(BIG_PLATFORMS):
            query += f" -site:{site}"

    return query


def _score_search_result_relevance(title: str, snippet: str, keyword: str) -> int:
    """Score a single search result by its title + snippet.

    Higher = more likely a small distributor/manufacturer.
    Lower = generic directory, big brand, or unrelated content.
    """
    text = (title + " " + snippet).lower()
    if not text.strip():
        return 0

    score = 0
    kw_lower = keyword.lower().strip('"')

    # Extract product core words (exclude generic B2B filler words)
    b2b_filler = {"wholesale", "supplier", "manufacturer", "distributor",
                  "importer", "dealer", "reseller", "oem", "odm", "factory"}
    kw_parts = [p for p in kw_lower.split() if len(p) > 3 and p not in b2b_filler]

    # CRITICAL relevance check:
    # If the search phrase has multiple product words (e.g. "football gloves"),
    # the title must contain the FULL phrase OR all product words.
    # A page titled "Basketball Training Gloves" should NOT match "football gloves".
    title_lower = title.lower()
    if kw_parts:
        has_full_phrase = kw_lower in title_lower
        matched_parts = sum(1 for p in kw_parts if p in title_lower)
        if len(kw_parts) >= 2:
            # Multi-word product: require full phrase or ALL product words
            if not has_full_phrase and matched_parts < len(kw_parts):
                score -= 80
        else:
            # Single product word: must appear in title
            if matched_parts == 0:
                score -= 80

    # Title contains exact product keyword → strong signal
    if kw_lower in title_lower:
        score += 20

    # Distributor / importer / wholesale signals
    b2b_signals = ["distributor", "wholesaler", "importer", "dealer",
                   "reseller", "stockist", "agent", "supplier"]
    for sig in b2b_signals:
        if sig in text:
            score += 15

    # Manufacturer / factory signals
    mfg_signals = ["manufacturer", "factory", "oem", "odm", "producer",
                   "private label", "custom", "bespoke", "bulk"]
    for sig in mfg_signals:
        if sig in text:
            score += 12

    # Negative: big platform / directory signals
    platform_signals = ["amazon", "alibaba", "ebay", "walmart", "shopify",
                        "directory", "marketplace", "list of", "top 10",
                        "best ", "review", "buying guide", "vs ", "compare"]
    for sig in platform_signals:
        if sig in text:
            score -= 25

    # Negative: news / blog / content farm
    content_signals = ["news", "blog", "article", "magazine", "how to",
                       "tips", "ultimate guide", "wikipedia"]
    for sig in content_signals:
        if sig in text:
            score -= 20

    # Small-business signals (great for finding small sites)
    small_signals = ["family owned", "family-owned", "small business",
                     "boutique", "specialty", "since 19", "established"]
    for sig in small_signals:
        if sig in text:
            score += 10

    return score


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
    # Distributor/importer terms get highest weight — these are our target buyers
    buyer_terms = ["distributor", "wholesale", "wholesaler", "importer", "import",
                   "dealer", "reseller", "retailer", "stockist", "agent",
                   "distribution", "trading company"]
    for term in buyer_terms:
        if term in text:
            score += 25
    # Other B2B terms (manufacturer, supplier, etc.)
    for term in B2B_CONTENT_POSITIVE:
        if term in text and term not in buyer_terms:
            score += 15
    # Negative signals: retail, news, blogs
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
    # Google Maps enrichment
    phone: str = ""
    address: str = ""
    google_rating: float = 0.0
    google_reviews_count: int = 0
    google_maps_url: str = ""
    place_id: str = ""
    source_type: str = "search"        # search | google_maps | linkedin_discovery | apollo
    has_direct_phone: bool = False


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


def _extract_og_description(html: str) -> str:
    """Extract Open Graph or Twitter Card description."""
    for pattern in [
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:description["\']',
        r'<meta[^>]+name=["\']twitter:description["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']twitter:description["\']',
    ]:
        m = re.search(pattern, html, flags=re.IGNORECASE)
        if m:
            return unescape(m.group(1)).strip()
    return ""


def _search_engine_snippet(domain: str) -> str:
    """Use DuckDuckGo to get a snippet for a domain as fallback."""
    if DDGS is None:
        return ""
    try:
        with DDGS() as ddgs:
            raw = ddgs.text(f"{domain}", max_results=3)
            for r in raw:
                snippet = r.get("body", "")
                if snippet and len(snippet) > 20:
                    return snippet
    except Exception:
        pass
    return ""


def _fetch_with_browser(domain: str, timeout: int = 15) -> str:
    """Fallback: use Playwright to render JS-heavy pages."""
    if sync_playwright is None:
        return ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page.goto(f"https://{domain}", timeout=timeout * 1000, wait_until="domcontentloaded")
            time.sleep(1.5)  # Allow JS frameworks to hydrate
            html = page.content()
            browser.close()
            return html
    except Exception:
        return ""


def fetch_domain_meta(domain: str, timeout: int = 10) -> dict:
    """
    Fetch homepage metadata (title, description, keywords, h1) plus about-page text.
    Falls back to Open Graph, browser rendering, and search engine snippets.

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
    used_browser = False

    # --- Homepage metadata ---
    try:
        url = f"https://{domain}"
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200:
            homepage_html = resp.text
    except Exception:
        pass

    # --- Playwright fallback if page looks blocked or too short (SPA/CF) ---
    if len(homepage_html) < 800 or "challenge-platform" in homepage_html or "cf-browser-verification" in homepage_html:
        browser_html = _fetch_with_browser(domain, timeout=timeout)
        if browser_html and len(browser_html) > len(homepage_html):
            homepage_html = browser_html
            used_browser = True

    if homepage_html:
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

        # Open Graph / Twitter fallback
        if not result["description"]:
            result["description"] = _extract_og_description(homepage_html)

    # --- About-page text + LinkedIn links ---
    about_html, about_text = _fetch_about_page(domain, headers, timeout)
    result["about_text"] = about_text
    all_linkedin = _extract_linkedin_links(homepage_html) + _extract_linkedin_links(about_html)
    seen: set = set()
    for url in all_linkedin:
        if url not in seen:
            seen.add(url)
            result["linkedin_links"].append(url)

    # --- Search engine snippet as last resort ---
    best_desc = result["about_text"] or result["description"] or result["h1"] or ""
    if len(best_desc) < 30:
        snippet = _search_engine_snippet(domain)
        if snippet:
            result["description"] = snippet

    if used_browser:
        print(f"      [Browser fallback] {domain}")

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
        "google_maps_key": os.environ.get("GOOGLE_MAPS_KEY", ""),
        "hunter_key": os.environ.get("HUNTER_KEY", ""),
        "snov_key": os.environ.get("SNOV_KEY", ""),
        "apollo_key": os.environ.get("APOLLO_KEY", ""),
        "norbert_key": os.environ.get("NORBERT_KEY", ""),
        "zerobounce_key": os.environ.get("ZEROBOUNCE_KEY", ""),
    }
    if not env_config["hunter_key"] and not env_config["snov_key"] and not env_config["apollo_key"] and not env_config["norbert_key"]:
        print(f"[ERROR] Config file not found: {CONFIG_PATH}")
        print("Please copy config.yaml.example to config.yaml and fill in your API keys.")
        print("Or set HUNTER_KEY / SNOV_KEY / APOLLO_KEY / NORBERT_KEY environment variables.")
        sys.exit(1)
    return env_config


def is_generic_email(email: str) -> bool:
    """Return True if email looks like a generic/department email."""
    prefix = email.split("@")[0].lower().strip()
    # Direct prefix match
    if prefix in GENERIC_PREFIXES:
        return True
    # Separator-based variants: sales-1, info_2, us.customer.service
    for gen in GENERIC_PREFIXES:
        if prefix == gen or prefix.startswith(gen + "-") or prefix.startswith(gen + "_"):
            return True
        if prefix.startswith(gen) and prefix[len(gen):].isdigit():
            return True  # sales1, info2
    # Dot-separated parts: e.g. us.customer.service -> service is generic
    for part in prefix.split("."):
        if part in GENERIC_PREFIXES:
            return True
    # Substring match for longer generic prefixes (>=5 chars) to avoid false positives
    # Catches hostadmin, webmasterhost, etc.
    for gen in GENERIC_PREFIXES:
        if len(gen) >= 5 and gen in prefix:
            return True
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


def _is_excluded_domain(domain: str, excluded: Set[str]) -> bool:
    """Check if domain or any of its parent domains are in the excluded set."""
    if domain in excluded:
        return True
    parts = domain.split(".")
    # Check progressively shorter suffixes: a.b.c.d -> b.c.d -> c.d
    for i in range(1, len(parts) - 1):
        parent = ".".join(parts[i:])
        if parent in excluded:
            return True
    return False


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
        self._dead_keys: set = set()
        self._email_finder_cache: dict = {}

    def _email_finder_cached(self, domain: str, first_name: str, last_name: str) -> Optional[dict]:
        """Cached wrapper around email-finder to avoid repeated API calls."""
        cache_key = f"{domain}:{first_name.lower()}:{last_name.lower()}"
        if cache_key in self._email_finder_cache:
            return self._email_finder_cache[cache_key]
        result = self.email_finder(domain, first_name, last_name)
        self._email_finder_cache[cache_key] = result
        return result  # keys that returned 429/403

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
        url = f"{self.base_url}/mixed_people/api_search"
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
                    "sources": ["apollo.io"],
                })
            return results
        except requests.exceptions.HTTPError as e:
            print(f"    [Apollo ERROR] {domain}: {e}")
            return []
        except Exception as e:
            print(f"    [Apollo ERROR] {domain}: {e}")
            return []

    def people_search(
        self,
        organization_keywords: List[str],
        person_titles: Optional[List[str]] = None,
        person_locations: Optional[List[str]] = None,
        organization_num_employees: Optional[List[str]] = None,
        per_page: int = 100,
        page: int = 1,
    ) -> List[dict]:
        """Search people via Apollo.io People Search API.

        Returns list of standardized contact dicts with organization info.
        """
        url = f"{self.base_url}/mixed_people/api_search"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
        }
        payload: dict = {
            "q_organization_keyword_tags": organization_keywords,
            "per_page": min(per_page, 100),
            "page": page,
        }
        if person_titles:
            payload["person_titles"] = person_titles
        if person_locations:
            payload["person_locations"] = person_locations
        if organization_num_employees:
            payload["organization_num_employees"] = organization_num_employees

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            if resp.status_code == 429:
                print(f"    [Apollo] Rate limited on people search. Sleeping 5s...")
                time.sleep(5)
                return []
            if resp.status_code == 403:
                data = resp.json() if resp.text else {}
                err = data.get("error", "")
                if "free plan" in err.lower():
                    print("    [Apollo] Free plan key cannot use People Search. Skipping.")
                else:
                    print(f"    [Apollo] Forbidden (403): {err}")
                return []
            resp.raise_for_status()
            data = resp.json()
            people = data.get("people", []) if isinstance(data, dict) else []

            # ------------------------------------------------------------------
            # Parallel enrichment via /people/match to get real email + last_name
            # ------------------------------------------------------------------
            def _enrich_one(person: dict) -> dict:
                """Call /people/match to reveal full contact details."""
                pid = person.get("id")
                if not pid:
                    return person
                try:
                    match_resp = requests.post(
                        f"{self.base_url}/people/match",
                        headers=headers,
                        json={"id": pid},
                        timeout=15,
                    )
                    if match_resp.status_code == 200:
                        match_data = match_resp.json()
                        enriched = match_data.get("person", {})
                        if enriched.get("email"):
                            person["email"] = enriched["email"]
                        if enriched.get("last_name"):
                            person["last_name"] = enriched["last_name"]
                        # Save enriched org data for downstream relevance scoring + domain resolution
                        enriched_org = enriched.get("organization", {})
                        if enriched_org:
                            person["_enriched_org"] = enriched_org
                            if enriched_org.get("primary_domain"):
                                person["organization_website"] = f"http://{enriched_org['primary_domain']}"
                            elif enriched_org.get("website_url"):
                                person["organization_website"] = enriched_org["website_url"]
                except Exception:
                    pass
                return person

            if people:
                enriched_count = 0
                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    future_to_p = {executor.submit(_enrich_one, p.copy()): p for p in people}
                    for future in concurrent.futures.as_completed(future_to_p):
                        p_enriched = future.result()
                        original = future_to_p[future]
                        if p_enriched.get("email") and not original.get("email"):
                            original["email"] = p_enriched["email"]
                            enriched_count += 1
                        if p_enriched.get("last_name") and not original.get("last_name"):
                            original["last_name"] = p_enriched["last_name"]
                        if p_enriched.get("_enriched_org") and not original.get("_enriched_org"):
                            original["_enriched_org"] = p_enriched["_enriched_org"]
                        if p_enriched.get("organization_website") and not original.get("organization_website"):
                            original["organization_website"] = p_enriched["organization_website"]
                if enriched_count:
                    print(f"    [Apollo] Enriched {enriched_count}/{len(people)} contacts via /people/match")

            results = []
            for p in people:
                email = (p.get("email") or "").lower().strip()
                name = p.get("name") or ""
                first_name = p.get("first_name") or ""
                last_name = p.get("last_name") or ""
                # Fallback: parse full name whenever parts are missing
                if name and (not first_name or not last_name):
                    parts = name.split()
                    if not first_name and parts:
                        first_name = parts[0]
                    if not last_name and len(parts) > 1:
                        last_name = " ".join(parts[1:])

                org = p.get("organization", {}) or {}
                # Prefer enriched website_url set by /people/match, fallback to original org data
                org_website = p.get("organization_website") or org.get("website_url", "")
                # Normalize sources to string list for downstream consumers
                raw_sources = p.get("sources", [])
                if raw_sources and isinstance(raw_sources[0], dict):
                    source_strs = [s.get("domain", "apollo.io") for s in raw_sources if isinstance(s, dict)]
                else:
                    source_strs = [str(s) for s in raw_sources] if raw_sources else ["apollo.io"]
                result_item = {
                    "value": email,
                    "type": "personal",
                    "confidence": 70,
                    "first_name": first_name,
                    "last_name": last_name,
                    "position": p.get("title") or p.get("job_title") or "",
                    "department": p.get("department") or "",
                    "linkedin_url": p.get("linkedin_url") or "",
                    "organization_name": org.get("name", ""),
                    "organization_website": org_website,
                    "has_email": p.get("has_email", False),
                    "has_direct_phone": p.get("has_direct_phone", False),
                    "sources": source_strs,
                }
                # Pass enriched org data for downstream relevance scoring
                if p.get("_enriched_org"):
                    result_item["_enriched_org"] = p["_enriched_org"]
                results.append(result_item)
            return results
        except requests.exceptions.HTTPError as e:
            print(f"    [Apollo ERROR] people search: {e}")
            return []
        except Exception as e:
            print(f"    [Apollo ERROR] people search: {e}")
            return []


class NorbertClient:
    """VoilaNorbert - Simple domain email enrichment (4th fallback)."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.voilanorbert.com/2018-01-08"

    def domain_search(self, domain: str, limit: int = 50) -> List[dict]:
        """Search emails by domain. Returns list of email dicts."""
        url = f"{self.base_url}/enrich/domain"
        headers = {"Authorization": f"Token {self.api_key}"}
        params = {"domain": domain}
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=20)
            if resp.status_code == 429:
                print(f"    [Norbert] Rate limited on {domain}. Sleeping 3s...")
                time.sleep(3)
                return []
            if resp.status_code == 401:
                print("    [Norbert] Invalid API key.")
                return []
            resp.raise_for_status()
            data = resp.json()
            emails = data.get("emails", []) if isinstance(data, dict) else []
            results = []
            for e in emails:
                email = (e.get("email") or "").lower().strip()
                if not email:
                    continue
                results.append({
                    "value": email,
                    "type": "personal",
                    "confidence": e.get("confidence", 80),
                    "first_name": e.get("first_name", ""),
                    "last_name": e.get("last_name", ""),
                    "position": e.get("title", ""),
                    "department": "",
                    "linkedin_url": "",
                    "sources": [{"domain": "voilanorbert.com"}],
                })
            return results
        except requests.exceptions.HTTPError as e:
            print(f"    [Norbert ERROR] {domain}: {e}")
            return []
        except Exception as e:
            print(f"    [Norbert ERROR] {domain}: {e}")
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


class GoogleMapsClient:
    """Google Places API (New) — search businesses via Google Maps."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://places.googleapis.com/v1/places:searchText"

    def search(self, query: str, region: str = "", max_results: int = 60) -> List[dict]:
        """
        Search businesses via Google Maps Text Search.
        Returns result dicts with url, title, snippet, plus _maps_meta.
        """
        results = []
        page_token = None
        count = 0
        text_query = query
        if region and region.lower() not in query.lower():
            text_query = f"{query} in {region}"

        while count < max_results:
            body: dict = {"textQuery": text_query, "pageSize": min(20, max_results - count)}
            if page_token:
                body["pageToken"] = page_token

            headers = {
                "Content-Type": "application/json",
                "X-Goog-Api-Key": self.api_key,
                "X-Goog-FieldMask": (
                    "places.displayName,places.formattedAddress,places.websiteUri,"
                    "places.internationalPhoneNumber,places.nationalPhoneNumber,"
                    "places.rating,places.userRatingCount,places.types,"
                    "places.googleMapsUri,places.location,places.businessStatus,"
                    "places.id,places.primaryTypeDisplayName"
                ),
            }

            try:
                resp = requests.post(self.base_url, headers=headers, json=body, timeout=30)
                if resp.status_code != 200:
                    print(f"  [GoogleMaps ERROR] HTTP {resp.status_code}: {resp.text[:200]}")
                    break
                data = resp.json()
            except Exception as e:
                print(f"  [GoogleMaps ERROR] {e}")
                break

            places = data.get("places", [])
            for place in places:
                website = place.get("websiteUri", "")
                if not website:
                    continue
                domain = extract_domain(website)
                if not domain:
                    continue

                # Build a search-result-compatible dict
                display_name = place.get("displayName", {}).get("text", "")
                primary_type = place.get("primaryTypeDisplayName", {}).get("text", "")
                types = place.get("types", [])
                address = place.get("formattedAddress", "")
                snippet_parts = [address]
                if primary_type:
                    snippet_parts.insert(0, primary_type)

                result = {
                    "url": website,
                    "title": display_name or domain,
                    "snippet": " — ".join(snippet_parts),
                    "_maps_meta": {
                        "place_id": place.get("id", ""),
                        "phone": place.get("internationalPhoneNumber", place.get("nationalPhoneNumber", "")),
                        "address": address,
                        "rating": place.get("rating", 0) or 0,
                        "reviews_count": place.get("userRatingCount", 0) or 0,
                        "types": types,
                        "google_maps_url": place.get("googleMapsUri", ""),
                        "location": place.get("location", {}),
                        "business_status": place.get("businessStatus", ""),
                        "primary_type": primary_type,
                    },
                }
                results.append(result)

            count += len(places)
            print(f"  [GoogleMaps] Fetched {len(places)} places (total {count})")

            page_token = data.get("nextPageToken")
            if not page_token:
                break
            time.sleep(1.5)  # Be polite between pages

        print(f"  [GoogleMaps] Total usable results: {len(results)}")
        return results


# ---------------------------------------------------------------------------
# LinkedIn Discovery Helpers
# ---------------------------------------------------------------------------

def _is_valid_company_name(name: str) -> bool:
    """Filter out obviously wrong company name extractions."""
    name_lower = name.lower().strip()
    invalid = {
        "linkedin member", "self-employed", "freelance", "independent consultant",
        "consultant", "looking for new opportunities", "open to work",
        "student", "intern", "unemployed",
    }
    if name_lower in invalid:
        return False
    if len(name) < 3:
        return False
    # If it's all lowercase and no spaces, probably a parsing error
    if " " not in name and not name[0].isupper():
        return False
    # Heuristic: filter out common job titles mistaken for company names
    job_title_signals = [
        "sales manager", "purchasing manager", "buyer", "procurement manager",
        "account manager", "business development", "marketing manager",
        "general manager", "managing director", "ceo", "cfo", "cto",
        "director of", "head of", "vice president", "vp ",
    ]
    lowered = name_lower
    for signal in job_title_signals:
        if signal in lowered:
            return False
    return True


def _extract_company_from_linkedin_result(title: str, snippet: str, link: str = "") -> Optional[str]:
    """Extract company name from LinkedIn search result title/snippet/link."""
    # For /company/ pages, the title IS the company name (e.g. "Keeper Grip | LinkedIn")
    if "/company/" in link:
        company = re.sub(r'\s*\|\s*LinkedIn\s*$', '', title, flags=re.IGNORECASE).strip()
        if _is_valid_company_name(company):
            return company

    # For /in/ personal profiles, try multiple patterns
    # Pattern A: "Name - Title at Company Name | LinkedIn"
    m = re.search(r'(?:at|–)\s+([A-Z][A-Za-z0-9\s&.,\-]+?)(?:\s*\||\s*$)', title)
    if m:
        candidate = m.group(1).strip()
        if _is_valid_company_name(candidate):
            return candidate

    # Pattern B: "Name - Company Name" (single dash, no "at")
    parts = title.split(' - ', 1)
    if len(parts) == 2:
        candidate = parts[1].strip()
        # Remove trailing "..." or "| LinkedIn"
        candidate = re.sub(r'\s*\.\.\.\s*$', '', candidate)
        candidate = re.sub(r'\s*\|\s*LinkedIn\s*$', '', candidate, flags=re.IGNORECASE)
        if _is_valid_company_name(candidate):
            return candidate

    # Pattern C: snippet like "... at Company Name. ..."
    m = re.search(
        r'(?:is|was|works?|working)\s+(?:as\s+[^.]+?)?at\s+([A-Z][A-Za-z0-9\s&.,\-]+?)'
        r'(?:\.|,|\s+\||\s+-\s+|\s+and\s+|\s+Location|\s+Connections|\s+Experience|\Z)',
        snippet, re.IGNORECASE,
    )
    if m:
        candidate = m.group(1).strip()
        if _is_valid_company_name(candidate):
            return candidate

    # Pattern D: snippet starts with "Company Name. manufacturer/supplier..."
    m = re.search(r'^([A-Z][A-Za-z0-9\s&.,\-]+?)(?:\s+manufacturer|\s+supplier|\s+distributor)', snippet, re.IGNORECASE)
    if m:
        candidate = m.group(1).strip()
        if _is_valid_company_name(candidate):
            return candidate

    return None


# In-memory cache for company website lookups (avoids repeated DDG searches)
_company_website_cache: dict = {}


def _resolve_company_website(company_name: str) -> Optional[str]:
    """Find official website for a company name via DuckDuckGo (free, no key needed).

    Results are cached to avoid repeated network requests for the same company.
    """
    if not company_name or len(company_name) < 2:
        return None

    cache_key = company_name.strip().lower()
    if cache_key in _company_website_cache:
        return _company_website_cache[cache_key]

    blocked = {
        "facebook.com", "linkedin.com", "twitter.com", "x.com", "instagram.com",
        "youtube.com", "wikipedia.org", "amazon.com", "ebay.com", "alibaba.com",
        "aliexpress.com", "zoominfo.com", "crunchbase.com", "bbb.org",
        "yellowpages.com", "yelp.com", "tripadvisor.com", "pinterest.com",
        "reddit.com", "quora.com", "etsy.com", "walmart.com", "target.com",
        "homedepot.com", "bestbuy.com", "costco.com", "wayfair.com", "macys.com",
        "google.com", "bing.com", "baidu.com",
    }

    clean_name = company_name.strip().strip('"').strip("'")

    # Try DuckDuckGo first (free, no API key)
    try:
        if DDGS is not None:
            with DDGS() as ddgs:
                query = f'"{clean_name}" official website'
                for r in ddgs.text(query, max_results=5):
                    href = r.get("href", "")
                    domain = extract_domain(href)
                    if domain and domain not in blocked:
                        _company_website_cache[cache_key] = domain
                        return domain
    except Exception:
        pass

    # Fallback: simple heuristic guesses
    simple = re.sub(r'[^\w\-]', '', clean_name.lower().replace(' ', '').replace('&', 'and'))
    candidates = [
        f"{simple}.com",
        f"{simple}.co.uk",
        f"{simple}.net",
    ]
    for domain in candidates:
        try:
            resp = requests.head(f"https://{domain}", timeout=5, allow_redirects=True)
            if resp.status_code < 400:
                _company_website_cache[cache_key] = domain
                return domain
        except Exception:
            continue

    _company_website_cache[cache_key] = None
    return None


def _extract_product_keyword(keyword: str) -> str:
    """Strip B2B suffixes to get the core product term for LinkedIn search."""
    b2b_terms = {
        "manufacturer", "factory", "wholesale", "supplier", "oem", "odm",
        "distributor", "importer", "dealer", "reseller", "exporter",
        "company", "co", "ltd", "inc", "llc", "limited",
    }
    words = keyword.lower().split()
    filtered = [w for w in words if w not in b2b_terms]
    return " ".join(filtered) if filtered else keyword


class LinkedInDiscoveryClient:
    """Discover B2B companies by searching LinkedIn profiles via SerpAPI."""

    def __init__(self, serpapi_key: str):
        self.serpapi_key = serpapi_key
        self.base_url = "https://serpapi.com/search"

    def search(self, keyword: str, pages: int = 2) -> List[dict]:
        """
        Search LinkedIn profiles and return list of dicts with:
        linkedin_url, name, company, domain
        """
        # Use product-only keyword (strip "manufacturer", "supplier", etc.)
        # so we match profiles that mention the product but not the full B2B phrase.
        product_kw = _extract_product_keyword(keyword)
        # Broader query: any LinkedIn page (company or profile) matching keyword + B2B roles
        # -pulse filters out LinkedIn Pulse articles which dominate results
        query = (
            f'site:linkedin.com "{product_kw}" '
            f'-pulse'
        )

        results: List[dict] = []
        for page in range(pages):
            start = page * 10
            params = {
                "q": query,
                "engine": "google",
                "api_key": self.serpapi_key,
                "num": 10,
                "start": start,
            }
            try:
                resp = requests.get(self.base_url, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                organic = data.get("organic_results", [])
                for r in organic:
                    title = r.get("title", "")
                    snippet = r.get("snippet") or r.get("description", "")
                    link = r.get("link", "")

                    if not link or "linkedin.com/" not in link:
                        continue
                    # Skip Pulse articles and posts that slip through
                    if "/pulse/" in link or "/posts/" in link or "/activities/" in link:
                        continue

                    company = _extract_company_from_linkedin_result(title, snippet, link)
                    if not company:
                        continue

                    # Resolve company website via DuckDuckGo
                    domain = _resolve_company_website(company)
                    if not domain:
                        continue

                    name = title.split("-")[0].strip() if "-" in title else ""
                    results.append({
                        "linkedin_url": link,
                        "name": name,
                        "company": company,
                        "domain": domain,
                    })
                    time.sleep(0.5)  # Be polite to Bing

                page_companies = len({r["domain"] for r in results})
                print(f"  [LinkedInDiscovery] Page {page + 1}: {len(organic)} results, {page_companies} companies resolved so far")
                if not organic:
                    break
            except requests.exceptions.RequestException as e:
                print(f"  [LinkedInDiscovery ERROR] Page {page + 1}: {e}")
                break
            time.sleep(1)
        return results


# ---------------------------------------------------------------------------
# Amazon brand search helpers
# ---------------------------------------------------------------------------

def _search_amazon_brands(keyword: str) -> Set[str]:
    """Search Amazon and extract brand names from product titles."""
    brands: Set[str] = set()
    try:
        url = f"https://www.amazon.com/s?k={quote_plus(keyword)}"
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            return brands
        html = resp.text
        if "captcha" in html.lower() or "Enter the characters you see below" in html:
            print("      [Amazon] CAPTCHA page returned — try with a VPN/proxy")
            return brands

        # Extract product titles via multiple patterns
        titles: Set[str] = set()
        for pattern in [
            r'<span[^>]*class="[^"]*a-size-base-plus[^"]*"[^>]*>([^<]{10,120})</span>',
            r'<span[^>]*class="[^"]*a-size-medium[^"]*"[^>]*>([^<]{10,120})</span>',
            r'<span[^>]*class="[^"]*a-color-base[^"]*a-text-normal[^"]*"[^>]*>([^<]{10,120})</span>',
            r'<h2[^>]*>.*?<span[^>]*>([^<]{10,120})</span>.*?</h2>',
        ]:
            for m in re.finditer(pattern, html, re.DOTALL):
                titles.add(re.sub(r'<[^>]+>', '', m.group(1)).strip())

        for title in titles:
            brand = _extract_brand_from_title(title)
            if brand:
                brands.add(brand)
    except Exception as e:
        print(f"      [Amazon] Error: {e}")
    return brands


def _extract_brand_from_title(title: str) -> Optional[str]:
    """Extract brand name from an Amazon product title."""
    title = title.strip()
    if len(title) < 5 or len(title) > 120:
        return None
    words = title.split()
    brand_words = []
    for word in words:
        clean = re.sub(r'[^\w\-\&]', '', word)
        if not clean:
            continue
        # Brand words start with uppercase and are not common words
        if clean[0].isupper() and clean.lower() not in {
            "the", "and", "for", "with", "new", "best", "top", "original", "official",
            "premium", "pro", "plus", "max", "mini", "ultra", "super", "genuine",
        }:
            brand_words.append(clean)
        else:
            break
    if brand_words:
        return ' '.join(brand_words)
    return None


def _find_brand_domain(brand: str) -> Optional[str]:
    """Find official website for a brand via DuckDuckGo (free, no key needed)."""
    blocked = {
        "facebook.com", "linkedin.com", "twitter.com", "x.com", "instagram.com",
        "youtube.com", "wikipedia.org", "amazon.com", "ebay.com", "alibaba.com",
        "zoominfo.com", "crunchbase.com", "bbb.org", "yellowpages.com", "yelp.com",
        "tripadvisor.com", "pinterest.com", "reddit.com", "quora.com",
        "etsy.com", "walmart.com", "target.com", "homedepot.com",
        "bestbuy.com", "costco.com", "wayfair.com", "macys.com",
    }

    # Try DuckDuckGo first (free)
    try:
        if DDGS is not None:
            with DDGS() as ddgs:
                query = f'"{brand}" official website'
                for r in ddgs.text(query, max_results=5):
                    href = r.get("href", "")
                    domain = extract_domain(href)
                    if domain and domain not in blocked:
                        return domain
    except Exception:
        pass

    # Fallback: heuristic guesses
    simple = re.sub(r'[^\w\-]', '', brand.lower().replace(' ', '').replace('&', 'and'))
    candidates = [f"{simple}.com", f"{simple}.co.uk", f"{simple}.net"]
    for domain in candidates:
        try:
            resp = requests.head(f"https://{domain}", timeout=5, allow_redirects=True)
            if resp.status_code < 400:
                return domain
        except Exception:
            continue

    return None


# ---------------------------------------------------------------------------
# Core Pipeline
# ---------------------------------------------------------------------------

class LeadFinder:
    def __init__(self, config: dict, engine: str = "auto", extra_excluded: Optional[Set[str]] = None):
        self.config = config
        self.engine = engine
        self.excluded_domains = set(EXCLUDED_DOMAINS) | BIG_PLATFORMS
        if extra_excluded:
            self.excluded_domains.update(d.lower().strip() for d in extra_excluded)

        # Initialize all search clients
        self.serp = None
        self.ddg = DuckDuckGoClient()
        self.browser = BrowserClient()
        self.google_maps = None

        self.linkedin_discovery = None
        if config.get("serpapi_key") and config["serpapi_key"] not in ("", "YOUR_SERPAPI_KEY_HERE"):
            self.serp = SerpAPIClient(config["serpapi_key"])
            self.linkedin_discovery = LinkedInDiscoveryClient(config["serpapi_key"])

        if config.get("google_maps_key") and config["google_maps_key"] not in ("", "YOUR_GOOGLE_MAPS_KEY_HERE"):
            self.google_maps = GoogleMapsClient(config["google_maps_key"])

        self.hunter = HunterClient(config["hunter_key"])
        self.snov = None
        if config.get("snov_key") and config["snov_key"] not in ("", "YOUR_SNOV_KEY_HERE"):
            self.snov = SnovClient(config["snov_key"])
        self.apollo = None
        if config.get("apollo_key") and config["apollo_key"] not in ("", "YOUR_APOLLO_KEY_HERE"):
            self.apollo = ApolloClient(config["apollo_key"])
        self.norbert = None
        if config.get("norbert_key") and config["norbert_key"] not in ("", "YOUR_NORBERT_KEY_HERE"):
            self.norbert = NorbertClient(config["norbert_key"])
        self.zerobounce = None
        if config.get("zerobounce_key"):
            self.zerobounce = ZeroBounceClient(config["zerobounce_key"])

    def _resolve_engine(self, force_maps: bool = False) -> str:
        """Pick the best available search engine."""
        if force_maps and self.google_maps is not None:
            return "google_maps"
        if self.engine != "auto":
            return self.engine
        # Priority: serpapi > duckduckgo > browser
        if self.serp is not None:
            return "serpapi"
        if DDGS is not None:
            return "duckduckgo"
        if sync_playwright is not None:
            return "browser"
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
        domains: Optional[List[str]] = None,
        target_tlds: Optional[List[str]] = None,
        amazon: bool = False,
        maps_region: str = "",
        keep_no_email: bool = False,
        exclude_big_platforms: bool = False,
        advanced_syntax: bool = False,
        strict_mode: bool = False,
    ) -> None:
        timestamp = datetime.now().isoformat()
        domain_source_type: Dict[str, str] = {}
        use_maps = bool(maps_region) or self.engine == "google_maps"
        engine = self._resolve_engine(force_maps=use_maps)
        skip_pages = 5 if deep else 0

        # Strict mode tightens relevance threshold and search strategy
        if strict_mode:
            min_relevance = max(min_relevance, 10)
            exclude_big_platforms = True
            advanced_syntax = True

        # Google Maps should use the exact raw keyword without enhancement
        if engine == "google_maps":
            search_query = keyword
        else:
            search_query = build_enhanced_query(
                keyword,
                b2b_focus=b2b_focus,
                exclude_big_platforms=exclude_big_platforms,
                advanced_syntax=advanced_syntax,
            )

        print(f"\n{'='*60}")
        print(f"  B2B Lead Finder")
        print(f"  Keyword : {keyword}")
        if b2b_focus and search_query != keyword:
            print(f"  Query   : {search_query}")
        print(f"  Engine  : {engine}")
        print(f"  Pages   : {pages}")
        if deep:
            print(f"  Mode    : DEEP (skip first {skip_pages} pages)")
        if domains:
            print(f"  Mode    : DIRECT (using {len(domains)} provided domains)")
        if target_tlds:
            print(f"  Filter  : TLDs {target_tlds}")
        if engine == "google_maps":
            print(f"  Region  : {maps_region or 'Global'}")
        print(f"  Output  : {output}")
        print(f"{'='*60}\n")

        # Validate Google Maps availability early
        if engine == "google_maps" and self.google_maps is None:
            print("[ERROR] Google Maps engine selected but no API key configured.")
            print("        Set google_maps_key in config.yaml or GOOGLE_MAPS_KEY env var.")
            sys.exit(1)

        # 1. Search (skip if domains provided)
        print("PROGRESS: 5")
        domain_maps_meta: Dict[str, dict] = {}
        if domains:
            print(f"[1/5] Using {len(domains)} provided domains, skipping search engine...")
            raw_results = [{"url": f"https://{d}", "title": "", "snippet": ""} for d in domains]
        elif engine == "google_maps" and self.google_maps:
            print("[1/5] Searching via Google Maps (Places API)...")
            raw_results = self.google_maps.search(keyword, region=maps_region, max_results=pages * 10)
            # Store maps metadata keyed by domain for later enrichment
            for r in raw_results:
                d = extract_domain(r.get("url", ""))
                if d and "_maps_meta" in r:
                    domain_maps_meta[d] = r["_maps_meta"]
                    domain_source_type[d] = "google_maps"
        elif engine == "serpapi" and self.serp:
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

        # Apply search-result-level relevance scoring (skip for direct domains or maps)
        if not domains and engine != "google_maps":
            before = len(raw_results)
            scored_results = []
            for r in raw_results:
                title = r.get("title", "")
                snippet = r.get("snippet", "")
                sr_score = _score_search_result_relevance(title, snippet, keyword)
                # Normal mode: filter obviously irrelevant results (title missing product keywords)
                # Strict mode: require a modest positive score
                threshold = 5 if strict_mode else -40
                if sr_score >= threshold:
                    scored_results.append(r)
            raw_results = scored_results
            after = len(raw_results)
            if after < before:
                print(f"      Filtered out {before - after} low-relevance search results (title/snippet score)")

        print("PROGRESS: 20")

        # 1b. Amazon brand search (optional)
        amazon_domains: Dict[str, int] = {}
        if amazon:
            print("PROGRESS: 15")
            print("\n[1b/5] Searching Amazon for brands...")
            amazon_brands = _search_amazon_brands(keyword)
            if amazon_brands:
                print(f"      Found {len(amazon_brands)} brand candidates")
                found_domains = 0
                for brand in amazon_brands:
                    domain = _find_brand_domain(brand)
                    if domain and not _is_excluded_domain(domain, self.excluded_domains) and domain not in amazon_domains:
                        amazon_domains[domain] = 5  # Positive score for Amazon brands
                        found_domains += 1
                if found_domains:
                    print(f"      Resolved {found_domains} domains from Amazon brands")
            else:
                print("      No brands found (Amazon may require a VPN/proxy or is blocking automated access)")

        # 1c. LinkedIn profile discovery (SerpAPI only, skip in Maps mode)
        linkedin_domains: Dict[str, int] = {}
        if self.linkedin_discovery and not use_maps:
            print("\n[1c/5] Discovering companies via LinkedIn profiles...")
            linkedin_results = self.linkedin_discovery.search(keyword, pages=min(pages, 3))
            if linkedin_results:
                unique_domains = {r["domain"] for r in linkedin_results}
                for domain in unique_domains:
                    if not _is_excluded_domain(domain, self.excluded_domains):
                        linkedin_domains[domain] = 18  # Strong positive score
                        domain_source_type[domain] = "linkedin_discovery"
                print(f"      Found {len(linkedin_results)} LinkedIn profiles, resolved {len(linkedin_domains)} unique domains")
            else:
                print("      No LinkedIn profiles found")

        # 2. Score, filter, and extract unique domains
        print("PROGRESS: 25")
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
            if _is_excluded_domain(domain, self.excluded_domains):
                skipped += 1
                continue
            # TLD filter
            if target_tlds:
                domain_tld = "." + domain.rsplit(".", 1)[-1] if "." in domain else ""
                if domain_tld and domain_tld not in target_tlds:
                    skipped += 1
                    continue
            if engine == "google_maps":
                meta = item.get("_maps_meta", {})
                score = 20
                if meta.get("rating", 0) >= 4.0:
                    score += 10
                if meta.get("reviews_count", 0) >= 10:
                    score += 5
                b2b_types = {"wholesale", "store", "supplier", "manufacturer", "factory", "distributor", "importer", "equipment_supplier"}
                if any(t in b2b_types for t in meta.get("types", [])):
                    score += 15
            else:
                score = _score_search_result_relevance(title, snippet, keyword)
            # Keep the highest score for each domain
            if domain not in domain_scores or score > domain_scores[domain]:
                domain_scores[domain] = score
            if domain not in domain_source_type:
                domain_source_type[domain] = "search"

        # Merge Amazon domains
        for domain, score in amazon_domains.items():
            if domain not in domain_scores:
                domain_scores[domain] = score
            else:
                domain_scores[domain] = max(domain_scores[domain], score)

        # Merge LinkedIn discovered domains
        for domain, score in linkedin_domains.items():
            if domain not in domain_scores:
                domain_scores[domain] = score
            else:
                domain_scores[domain] = max(domain_scores[domain], score)

        # Sort by score descending — small distributors bubble to the top
        sorted_domains = sorted(domain_scores.items(), key=lambda x: x[1], reverse=True)
        domains = [d for d, s in sorted_domains]
        if max_domains:
            domains = domains[:max_domains]
        print(f"      Unique domains: {len(domains)} (excluded {skipped} big-brand domains)")
        if amazon_domains:
            print(f"      Amazon-sourced : {len(amazon_domains)} domains")
        if linkedin_domains:
            print(f"      LinkedIn-sourced: {len(linkedin_domains)} domains")
        pos = sum(1 for _, s in sorted_domains if s > 0)
        neg = sum(1 for _, s in sorted_domains if s < 0)
        if pos or neg:
            print(f"      Score distribution: {pos} positive, {neg} negative")

        # 3. Fetch website descriptions and calculate content relevance
        print("PROGRESS: 30")
        print("\n[3/5] Fetching website metadata and scoring relevance...")
        domain_descriptions: Dict[str, str] = {}
        domain_relevance: Dict[str, int] = {}
        domain_linkedin_links: Dict[str, List[str]] = {}
        total_domains = len(domains)
        completed_domains = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            future_to_domain = {executor.submit(fetch_domain_meta, d): d for d in domains}
            for future in concurrent.futures.as_completed(future_to_domain):
                domain = future_to_domain[future]
                completed_domains += 1
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
                if completed_domains % max(1, total_domains // 5) == 0 or completed_domains == total_domains:
                    pct = 30 + int((completed_domains / total_domains) * 15)
                    print(f"PROGRESS: {min(pct, 45)}")
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

        # 4. Find emails via Hunter.io + Snov.io + Apollo.io + Norbert (merge mode)
        sources_label = "Hunter.io"
        if self.snov:
            sources_label += " + Snov.io"
        if self.apollo:
            sources_label += " + Apollo.io"
        if self.norbert:
            sources_label += " + Norbert"
        print("PROGRESS: 50")
        print(f"\n[4/5] Finding emails via {sources_label} (merge & enrich)...")
        all_leads: List[Lead] = []
        total_domains_step4 = len(domains)
        for idx, domain in enumerate(domains, 1):
            pct = 50 + int((idx / total_domains_step4) * 25)
            print(f"PROGRESS: {min(pct, 75)}")
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

            if self.norbert:
                norbert_emails = self.norbert.domain_search(domain)
                if norbert_emails:
                    source_tags.append("norbert.io")
                    for e in norbert_emails:
                        email = (e.get("value") or "").lower().strip()
                        if email and "@" in email:
                            if email in email_map:
                                email_map[email]["_sources"].append("norbert.io")
                                if e.get("first_name") and not email_map[email].get("first_name"):
                                    email_map[email]["first_name"] = e["first_name"]
                                if e.get("last_name") and not email_map[email].get("last_name"):
                                    email_map[email]["last_name"] = e["last_name"]
                                if e.get("position") and not email_map[email].get("position"):
                                    email_map[email]["position"] = e["position"]
                            else:
                                email_map[email] = dict(e)
                                email_map[email]["_sources"] = ["norbert.io"]

            raw_emails = list(email_map.values())
            source_name = " + ".join(source_tags) if source_tags else "none"

            if not raw_emails:
                if keep_no_email:
                    # Preserve domain info even when no email is found
                    maps_meta = domain_maps_meta.get(domain, {})
                    lead = Lead(
                        domain=domain,
                        company=domain,
                        email="",
                        first_name="",
                        last_name="",
                        position="",
                        department="",
                        confidence_score=0,
                        email_type="",
                        sources=["none"],
                        search_keyword=keyword,
                        found_at=timestamp,
                        country=detect_country(domain, ""),
                        website_description=domain_descriptions.get(domain, ""),
                        relevance_score=domain_relevance.get(domain, 0),
                        linkedin_url="",
                        phone=maps_meta.get("phone", ""),
                        address=maps_meta.get("address", ""),
                        google_rating=maps_meta.get("rating", 0.0),
                        google_reviews_count=maps_meta.get("reviews_count", 0),
                        google_maps_url=maps_meta.get("google_maps_url", ""),
                        place_id=maps_meta.get("place_id", ""),
                        source_type="google_maps" if maps_meta else domain_source_type.get(domain, "search"),
                    )
                    all_leads.append(lead)
                    print("0 email, kept domain")
                else:
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

                maps_meta = domain_maps_meta.get(domain, {})
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
                    phone=maps_meta.get("phone", ""),
                    address=maps_meta.get("address", ""),
                    google_rating=maps_meta.get("rating", 0.0),
                    google_reviews_count=maps_meta.get("reviews_count", 0),
                    google_maps_url=maps_meta.get("google_maps_url", ""),
                    place_id=maps_meta.get("place_id", ""),
                    source_type="google_maps" if maps_meta else domain_source_type.get(domain, "search"),
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
            print("PROGRESS: 80")
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
        print("PROGRESS: 90")
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

        # 6. Deduplicate by email (for leads with email) or domain (for no-email leads)
        seen_keys: Set[str] = set()
        unique_leads: List[Lead] = []
        for lead in all_leads:
            key = lead.email.lower().strip() if lead.email else f"__no_email__:{lead.domain}"
            if key not in seen_keys:
                seen_keys.add(key)
                unique_leads.append(lead)

        # 6. Export
        print("PROGRESS: 100")
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
            "phone", "address", "google_rating", "google_reviews_count",
            "google_maps_url", "place_id", "source_type", "has_direct_phone",
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
        no_email = sum(1 for l in leads if not l.email)
        print(f"\n{'='*60}")
        print(f"  SUMMARY")
        print(f"{'='*60}")
        print(f"  Keyword          : {keyword}")
        print(f"  Domains searched : {domains_searched}")
        print(f"  Unique leads     : {len(leads)}")
        if no_email:
            print(f"  No-email leads   : {no_email} (kept for phone/address)")
        print(f"  Personal emails  : {personal}")
        print(f"  High confidence  : {high_conf}")
        if validated:
            print(f"  Validated (OK)   : {validated}")
        print(f"{'='*60}\n")

    def run_apollo_search(
        self,
        organization_keywords: List[str],
        person_titles: List[str],
        person_locations: List[str],
        max_results: int = 100,
        output: str = "leads.csv",
        keep_no_email: bool = False,
        employee_range: Optional[List[str]] = None,
    ) -> List[Lead]:
        """Run an Apollo.io People Search and export leads.

        Optimized with parallel domain resolution and Hunter enrichment to minimize wall-clock time.
        """
        if not self.apollo:
            print("[ERROR] Apollo client not initialized. Please set APOLLO_KEY in config.")
            return []

        timestamp = datetime.now().isoformat()

        print(f"\n{'='*60}")
        print(f"  Apollo.io People Search")
        print(f"  Org Keywords : {organization_keywords}")
        print(f"  Titles       : {person_titles}")
        print(f"  Locations    : {person_locations}")
        print(f"  Employee Range: {employee_range or 'Any'}")
        print(f"  Max Results  : {max_results}")
        print(f"  Output       : {output}")
        print(f"{'='*60}\n")

        per_page = min(max_results, 100)
        page = 1
        print("PROGRESS: 5")

        # -----------------------------------------------------------------------
        # Stage 1: Fetch all people from Apollo (may span multiple pages)
        # -----------------------------------------------------------------------
        all_people: List[dict] = []
        while len(all_people) < max_results:
            print(f"[Apollo] Fetching page {page}...")
            people = self.apollo.people_search(
                organization_keywords=organization_keywords,
                person_titles=person_titles or None,
                person_locations=person_locations or None,
                organization_num_employees=employee_range or None,
                per_page=per_page,
                page=page,
            )
            if not people:
                print("  No more results.")
                break
            all_people.extend(people)
            print(f"  Page {page}: got {len(people)} contacts (total {len(all_people)})")
            if len(people) < per_page:
                break
            page += 1
            time.sleep(1)

        print(f"\n[Apollo] Total contacts fetched: {len(all_people)}")
        if not all_people:
            print("[WARNING] No contacts returned by Apollo.")
            return []
        print("PROGRESS: 15")

        # -----------------------------------------------------------------------
        # Stage 2: Parallel domain resolution
        # -----------------------------------------------------------------------
        print(f"\n[Stage 2/4] Resolving domains for {len(all_people)} contacts (parallel)...")
        people_with_domains: List[tuple] = []
        skipped_excluded = 0
        skipped_no_domain = 0

        def _resolve_domain_for_person(person: dict) -> Optional[tuple]:
            """Return (person, domain) or None if excluded/unresolvable."""
            org_name = person.get("organization_name", "")
            org_website = person.get("organization_website", "")
            domain = ""
            if org_website:
                domain = extract_domain(org_website)
            if not domain and org_name:
                domain = _resolve_company_website(org_name)
            if not domain:
                return None
            if _is_excluded_domain(domain, self.excluded_domains):
                return None
            return (person, domain)

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            future_to_person = {executor.submit(_resolve_domain_for_person, p): p for p in all_people}
            completed = 0
            total = len(all_people)
            for future in concurrent.futures.as_completed(future_to_person):
                completed += 1
                if completed % max(1, total // 5) == 0 or completed == total:
                    pct = 15 + int((completed / total) * 25)
                    print(f"PROGRESS: {pct}")
                result = future.result()
                if result:
                    people_with_domains.append(result)
                else:
                    skipped_no_domain += 1

        print(f"  Resolved: {len(people_with_domains)} domains, skipped: {skipped_no_domain}")
        if not people_with_domains:
            print("[WARNING] No valid domains resolved.")
            return []
        print("PROGRESS: 40")

        # -----------------------------------------------------------------------
        # Stage 2.5: Relevance filtering based on enriched org data
        # -----------------------------------------------------------------------
        print(f"\n[Stage 2.5/4] Scoring relevance for {len(people_with_domains)} contacts...")
        scored_people: List[tuple] = []
        filtered_out = 0
        for person, domain in people_with_domains:
            score = _score_apollo_contact_relevance(person)
            has_org_data = bool(person.get("_enriched_org"))
            if not has_org_data:
                score = 0
            if score >= -50:
                scored_people.append((person, domain, score))
            else:
                filtered_out += 1
                if filtered_out <= 5:
                    org_name = person.get("organization_name", "")
                    print(f"    [Filter] -'{org_name}' (score {score})")
        # Sort by relevance score descending and attach score to person dict
        scored_people.sort(key=lambda x: x[2], reverse=True)
        people_with_domains = []
        for p, d, s in scored_people:
            p["_relevance_score"] = s
            people_with_domains.append((p, d))
        print(f"  Kept: {len(people_with_domains)}, Filtered out: {filtered_out}")
        if not people_with_domains:
            print("[WARNING] All contacts filtered out as irrelevant.")
            return []
        print("PROGRESS: 42")

        # -----------------------------------------------------------------------
        # Stage 3: Parallel Hunter enrichment for contacts missing email
        # -----------------------------------------------------------------------
        print(f"\n[Stage 3/4] Hunter enrichment for missing emails (parallel)...")
        hunter_targets = [
            (person, domain) for person, domain in people_with_domains
            if not person.get("value") and person.get("first_name") and person.get("last_name")
        ]
        hunter_results: dict = {}

        if hunter_targets and self.hunter:
            def _enrich_one(args: tuple) -> tuple:
                person, domain = args
                fn = person.get("first_name", "")
                ln = person.get("last_name", "")
                try:
                    res = self.hunter._email_finder_cached(domain, fn, ln)
                    if res and res.get("email"):
                        return (id(person), res.get("email"), res.get("score", 0) or res.get("confidence", 0) or 50)
                except Exception:
                    pass
                return (id(person), None, 0)

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                future_to_idx = {executor.submit(_enrich_one, item): item for item in hunter_targets}
                completed = 0
                total_h = len(hunter_targets)
                for future in concurrent.futures.as_completed(future_to_idx):
                    completed += 1
                    if completed % max(1, total_h // 4) == 0 or completed == total_h:
                        pct = 40 + int((completed / total_h) * 30)
                        print(f"PROGRESS: {min(pct, 70)}")
                    pid, email, score = future.result()
                    hunter_results[pid] = (email, score)

            enriched_count = sum(1 for e, _ in hunter_results.values() if e)
            print(f"  Hunter enriched: {enriched_count}/{len(hunter_targets)}")
        else:
            print("  No enrichment needed or Hunter not configured.")
        print("PROGRESS: 75")

        # -----------------------------------------------------------------------
        # Stage 4: Build Lead objects
        # -----------------------------------------------------------------------
        print(f"\n[Stage 4/4] Building leads...")
        all_leads: List[Lead] = []
        for person, domain in people_with_domains:
            email = (person.get("value") or "").lower().strip()
            has_email_flag = person.get("has_email", False)
            has_direct_phone = person.get("has_direct_phone", False)
            first_name = person.get("first_name", "")
            last_name = person.get("last_name", "")
            org_name = person.get("organization_name", "")

            # Apply Hunter enrichment
            hunter_confidence = 0
            if not email and id(person) in hunter_results:
                enriched_email, hunter_confidence = hunter_results[id(person)]
                if enriched_email:
                    email = enriched_email.lower().strip()

            # Skip if no contact signal and not keeping no-email leads
            has_contact_signal = bool(email) or has_email_flag or has_direct_phone
            if not has_contact_signal and not keep_no_email:
                continue

            if email and is_generic_email(email):
                continue

            sources = person.get("sources", ["apollo.io"])
            if hunter_confidence > 0:
                sources = sources + ["hunter.io (enriched)"]

            lead = Lead(
                domain=domain,
                company=org_name or domain,
                email=email,
                first_name=first_name,
                last_name=last_name,
                position=person.get("position", ""),
                department=person.get("department", ""),
                confidence_score=hunter_confidence if hunter_confidence > 0 else (person.get("confidence", 70) if email else 0),
                email_type="personal" if email else "",
                sources=sources,
                search_keyword=" | ".join(organization_keywords),
                found_at=timestamp,
                country=detect_country(domain, email),
                website_description="",
                relevance_score=person.get("_relevance_score", 0),
                linkedin_url=person.get("linkedin_url", ""),
                source_type="apollo",
                has_direct_phone=has_direct_phone,
            )
            all_leads.append(lead)

        print(f"  Built {len(all_leads)} leads")
        print("PROGRESS: 85")

        # Deduplicate
        seen: Set[str] = set()
        unique_leads: List[Lead] = []
        for lead in all_leads:
            key = lead.email.lower().strip() if lead.email else f"__no_email__:{lead.domain}"
            if key not in seen:
                seen.add(key)
                unique_leads.append(lead)

        print(f"\n[Apollo] Total unique leads: {len(unique_leads)}")
        print("PROGRESS: 95")
        self._export_csv(unique_leads, output)
        print("PROGRESS: 100")
        self._print_summary(unique_leads, "Apollo People Search", len(set(l.domain for l in unique_leads)))
        return unique_leads


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="B2B Lead Finder - Discover decision-maker emails from Google searches."
    )
    parser.add_argument("keyword", nargs="?", default="", help="Search keyword(s), e.g. 'football gloves manufacturer'")
    parser.add_argument("--pages", type=int, default=5, help="Number of Google result pages to scan (default: 5)")
    parser.add_argument("--output", type=str, default="leads.csv", help="Output CSV file path (default: leads.csv)")
    parser.add_argument("--validate", action="store_true", help="Validate emails via ZeroBounce (requires API key)")
    parser.add_argument("--max-domains", type=int, default=None, help="Limit number of domains to process")
    parser.add_argument(
        "--engine",
        type=str,
        default="auto",
        choices=["auto", "duckduckgo", "browser", "serpapi", "google_maps"],
        help="Search engine: auto (default), duckduckgo, browser (Playwright), serpapi, or google_maps",
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
    parser.add_argument(
        "--domains",
        type=str,
        default="",
        help="Comma-separated list of domains to process directly (skip search engine)",
    )
    parser.add_argument(
        "--target-tlds",
        type=str,
        default="",
        help="Comma-separated TLDs to keep, e.g. '.de,.fr,.uk' (default: all)",
    )
    parser.add_argument(
        "--amazon",
        action="store_true",
        help="Also search Amazon for product brands and find their official websites",
    )
    parser.add_argument(
        "--maps-region",
        type=str,
        default="",
        help="Google Maps search region, e.g. 'USA', 'Germany', 'Southeast Asia' (requires --engine google_maps or auto with google_maps_key configured)",
    )
    parser.add_argument(
        "--keep-no-email",
        action="store_true",
        help="Keep leads even when no email is found (useful for phone-based outreach)",
    )
    parser.add_argument(
        "--exclude-big-platforms",
        action="store_true",
        help="Exclude major B2B directory / marketplace platforms from search results (Alibaba, ThomasNet, etc.)",
    )
    parser.add_argument(
        "--advanced-syntax",
        action="store_true",
        help="Use advanced search syntax (intitle:) for tighter keyword matching",
    )
    parser.add_argument(
        "--strict-mode",
        action="store_true",
        help="Strict filtering: enables --exclude-big-platforms, --advanced-syntax, and raises min-relevance to 10",
    )
    parser.add_argument(
        "--apollo-keywords",
        type=str,
        default="",
        help="Apollo.io organization keywords, comma-separated (enables Apollo People Search mode)",
    )
    parser.add_argument(
        "--apollo-titles",
        type=str,
        default="Buyer,Purchasing Manager,Procurement Manager,Sourcing Manager",
        help="Apollo.io person titles, comma-separated",
    )
    parser.add_argument(
        "--apollo-locations",
        type=str,
        default="",
        help="Apollo.io person locations, comma-separated, e.g. 'United States,Germany'",
    )
    parser.add_argument(
        "--apollo-employee-range",
        type=str,
        default="",
        help="Apollo.io company size range, e.g. '2,50' or '51,200'",
    )
    args = parser.parse_args()

    extra_excluded = set(d.strip().lower() for d in args.exclude.split(",") if d.strip())
    config = load_config()
    finder = LeadFinder(config, engine=args.engine, extra_excluded=extra_excluded)

    # Apollo People Search mode
    if args.apollo_keywords:
        org_keywords = [k.strip() for k in args.apollo_keywords.split(",") if k.strip()]
        titles = [t.strip() for t in args.apollo_titles.split(",") if t.strip()]
        locations = [l.strip() for l in args.apollo_locations.split(",") if l.strip()]
        employee_range = None
        if args.apollo_employee_range:
            parts = [p.strip() for p in args.apollo_employee_range.split(",") if p.strip()]
            if len(parts) == 2:
                employee_range = parts
        finder.run_apollo_search(
            organization_keywords=org_keywords,
            person_titles=titles,
            person_locations=locations,
            max_results=args.max_domains or 100,
            output=args.output,
            keep_no_email=getattr(args, "keep_no_email", False),
            employee_range=employee_range,
        )
        return

    if not args.keyword:
        parser.print_help()
        sys.exit(1)

    domain_list = [d.strip() for d in args.domains.split(",") if d.strip()] if args.domains else None
    tld_list = [t.strip() for t in args.target_tlds.split(",") if t.strip()] if args.target_tlds else None
    finder.run(
        keyword=args.keyword,
        pages=args.pages,
        output=args.output,
        validate=args.validate,
        max_domains=args.max_domains,
        deep=args.deep,
        b2b_focus=not args.no_b2b_focus,
        min_relevance=args.min_relevance,
        domains=domain_list,
        target_tlds=tld_list,
        amazon=args.amazon,
        maps_region=getattr(args, "maps_region", ""),
        keep_no_email=getattr(args, "keep_no_email", False),
        exclude_big_platforms=getattr(args, "exclude_big_platforms", False),
        advanced_syntax=getattr(args, "advanced_syntax", False),
        strict_mode=getattr(args, "strict_mode", False),
    )


if __name__ == "__main__":
    main()
