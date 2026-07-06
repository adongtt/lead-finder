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
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote_plus, urlparse

import requests
import yaml

# Thread-local sessions avoid sharing urllib3 connection pools across threads.
# Sharing the global Session under ThreadPoolExecutor can trigger
# "SSL error: decryption failed or bad record mac" on concurrent HTTPS reuse.
_thread_local_sessions = threading.local()


def _get_session() -> requests.Session:
    """Return a thread-local requests Session with keep-alive disabled."""
    session = getattr(_thread_local_sessions, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"Connection": "close"})
        _thread_local_sessions.session = session
    return session


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
    "automotive", "motor vehicles", "electronics", "electrical",
    "appliances", "furniture", "home improvement", "plumbing",
}

APOLLO_POSITIVE_KEYWORDS = {
    "sport", "sports", "athletic", "athletics", "fitness", "outdoor",
    "equipment", "gear", "apparel", "uniform", "glove", "gloves",
    "football", "baseball", "cycling", "bike", "ski", "snowboard",
    "gym", "workout", "training", "team", "game", "player",
    "distributor", "wholesale", "importer", "supplier", "dealer",
    "retail", "merchant", "trading", "trade",
}

APOLLO_CHANNEL_ROLES = {
    "distributor", "distributors", "distributing", "distribution",
    "importer", "importers", "importing", "import", "imports",
    "exporter", "exporters", "exporting", "export", "exports",
    "supplier", "suppliers", "supplying", "supply", "supplies",
    "wholesaler", "wholesalers", "wholesale", "wholesaling",
    "dealer", "dealers", "dealing", "deal",
    "reseller", "resellers", "reselling",
    "buyer", "buyers", "buying",
    "retailer", "retailers", "retailing",
    "exporter", "exporters",
    "manufacturer", "manufacturers", "manufacturing", "manufacture",
    "oem", "odm",
    "vendor", "vendors",
    "merchant", "merchants",
    "trading", "trade", "trader", "traders",
}

APOLLO_PRODUCT_KEYWORDS = APOLLO_POSITIVE_KEYWORDS - APOLLO_CHANNEL_ROLES - {"retail", "merchant", "trading", "trade"} | {"sporting", "goods", "teams", "athletic"}

APOLLO_NEGATIVE_KEYWORDS = {
    "health", "medical", "dental", "hospital", "clinic", "patient",
    "software", "saas", "tech", "technology", "cloud", "ai", "data",
    "resort", "spa", "hotel", "hotels", "lodge", "lodges",
    "hospitality", "restaurant", "cafe", "bar", "pub",
    "food", "foods", "foodservice", "beverage", "beverages", "drink",
    "wine", "wines", "liquor", "alcohol", "floral", "flower", "flowers",
    "seafood", "fish", "meat", "dairy", "grocery", "groceries",
    "aviation", "aircraft", "airline", "plane", "maintenance",
    "government", "municipal", "county", "federal", "state",
    "nonprofit", "charity", "foundation", "ngo",
    "education", "school", "university", "college",
    "pharmaceutical", "drug", "biotech", "laboratory", "lab",
    "finance", "bank", "investment", "insurance", "mortgage",
    "legal", "law firm", "attorney", "lawyer",
    "construction", "contractor", "builder", "engineering",
    "oil", "gas", "energy", "petroleum", "mining",
    "real estate", "property", "rental", "rentals",
    "automotive", "car", "vehicle", "truck", "motor", "parts",
    "electronics", "semiconductor", "appliance", "appliances",
    "furniture", "plumbing", "hvac", "maintenance",
    "chemical", "chemicals", "solar", "tile", "tiles", "stone",
    "lighting", "pool", "fastener", "fasteners", "bearing", "bearings",
    # Ski/snow sports ecosystem that is NOT a B2B buyer: resorts, rental shops,
    # used-gear stores, schools/instructors, tour operators, hospitality.
    "ski resort", "ski resorts", "ski area", "ski areas",
    "ski mountain", "ski mountains", "snow resort", "snow resorts",
    "mountain resort", "mountain resorts", "ski lodge", "ski lodges",
    "ski valley", "ski basin", "ski basins", "ski and golf",
    "ski country",
    "ski school", "ski schools", "ski instructor", "ski instructors",
    "ski coach", "ski coaching", "ski lesson", "ski lessons",
    "ski rental", "ski rentals", "ski hire", "ski tour", "ski tours",
    "ski travel", "ski vacation", "ski trips",
    "snowboard school", "snowboard instructor", "snowboard rental",
    "pro shop", "proshop",
    "used ski", "used snowboard", "second hand", "second-hand",
    "pre-owned", "preowned", "thrift", "consignment",
    "outfitter", "outfitters",
    "tour operator", "tour operators", "travel agency", "vacation",
}

# ---------------------------------------------------------------------------
# Supplier portal page discovery
# ---------------------------------------------------------------------------

SUPPLIER_PAGE_PATHS = [
    "procurement", "purchasing", "sourcing",
    "become-a-supplier", "become-a-vendor", "become-a-distributor",
    "become-supplier", "become-vendor", "become-distributor",
    "supplier-registration", "vendor-registration", "distributor-registration",
    "supplier-portal", "vendor-portal", "partner-portal", "distributor-portal",
    "supplier", "vendors", "partners", "distributors", "wholesale", "reseller",
    "supplier-application", "vendor-application", "distributor-application",
    "supplier-onboarding", "vendor-onboarding",
    "sell-to-us", "work-with-us", "partner-with-us",
    "for-suppliers", "for-vendors", "for-distributors",
]

# Regex to find email addresses on supplier pages
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


def _scan_supplier_pages(domain: str, timeout: int = 6) -> dict:
    """Scan a domain for procurement/supplier-related pages.

    Returns dict with best matching page info, or empty dict if none found.
    Optimized: tries HTTPS first with a short timeout; falls back to HTTP only
    on connection errors.
    """
    found = []
    for path in SUPPLIER_PAGE_PATHS:
        for scheme in ("https://", "http://"):
            url = f"{scheme}{domain}/{path}"
            try:
                resp = _get_session().get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
                if resp.status_code == 200:
                    content_type = resp.headers.get("Content-Type", "")
                    if "text/html" not in content_type and "application/xhtml" not in content_type:
                        continue
                    text = resp.text
                    if len(text) < 200:
                        continue
                    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
                    title = title_match.group(1).strip() if title_match else ""
                    title = re.sub(r"\s+", " ", title)
                    # Extract first meaningful email (skip generic noreply/sales if possible)
                    emails = EMAIL_RE.findall(text)
                    supplier_emails = [
                        e for e in emails
                        if not is_generic_email(e) and "@" + domain in e.lower()
                    ]
                    if not supplier_emails:
                        supplier_emails = [e for e in emails if not is_generic_email(e)]
                    if not supplier_emails:
                        supplier_emails = emails
                    email = supplier_emails[0].lower().strip() if supplier_emails else ""
                    # Look for form action / registration link
                    form_match = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', text, re.IGNORECASE)
                    form_link = form_match.group(1) if form_match else ""
                    if form_link and form_link.startswith("/"):
                        form_link = f"{scheme}{domain}{form_link}"
                    elif form_link and not form_link.startswith("http"):
                        form_link = f"{scheme}{domain}/{form_link}"
                    # Simple keyword score for relevance
                    lower_text = text.lower()
                    score = sum(1 for kw in ["supplier", "vendor", "distributor", "procurement", "purchasing", "wholesale", "become", "register", "portal", "application"] if kw in lower_text)
                    found.append({
                        "url": url,
                        "title": title,
                        "email": email,
                        "form_link": form_link,
                        "score": score,
                    })
                    break
            except Exception:
                continue
    if not found:
        return {}
    found.sort(key=lambda x: x["score"], reverse=True)
    return found[0]


def _extract_supplier_notes(text: str) -> str:
    """Extract a short summary sentence from supplier page text."""
    # Remove scripts/styles
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    # Convert common block tags to spaces
    text = re.sub(r"</(p|div|li|h\d|br)\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Find sentences with procurement keywords
    keywords = ["supplier", "vendor", "distributor", "procurement", "purchasing", "wholesale", "register", "apply", "become"]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    for s in sentences[:30]:
        s = s.strip()
        if len(s) < 40 or len(s) > 250:
            continue
        if any(kw in s.lower() for kw in keywords):
            return s
    return ""


# Common country names used when extracting headquarters country from website text.
_COUNTRY_NAME_SET = {
    "Afghanistan", "Albania", "Algeria", "Argentina", "Armenia", "Australia", "Austria", "Azerbaijan",
    "Bahamas", "Bahrain", "Bangladesh", "Belarus", "Belgium", "Belize", "Bolivia", "Bosnia and Herzegovina",
    "Botswana", "Brazil", "Bulgaria", "Cambodia", "Cameroon", "Canada", "Chile", "China", "Colombia",
    "Costa Rica", "Croatia", "Cyprus", "Czech Republic", "Denmark", "Dominican Republic", "Ecuador", "Egypt",
    "El Salvador", "Estonia", "Ethiopia", "Finland", "France", "Georgia", "Germany", "Ghana", "Greece",
    "Guatemala", "Honduras", "Hong Kong", "Hungary", "Iceland", "India", "Indonesia", "Iran", "Iraq",
    "Ireland", "Israel", "Italy", "Jamaica", "Japan", "Jordan", "Kazakhstan", "Kenya", "Kuwait", "Laos",
    "Latvia", "Lebanon", "Libya", "Lithuania", "Luxembourg", "Madagascar", "Malaysia", "Maldives", "Malta",
    "Mexico", "Moldova", "Mongolia", "Montenegro", "Morocco", "Nepal", "Netherlands", "New Zealand",
    "Nicaragua", "Nigeria", "North Macedonia", "Norway", "Oman", "Pakistan", "Panama", "Paraguay", "Peru",
    "Philippines", "Poland", "Portugal", "Qatar", "Romania", "Russia", "Saudi Arabia", "Senegal", "Serbia",
    "Singapore", "Slovakia", "Slovenia", "South Africa", "South Korea", "Spain", "Sri Lanka", "Sudan",
    "Sweden", "Switzerland", "Syria", "Taiwan", "Tajikistan", "Tanzania", "Thailand", "Tunisia", "Turkey",
    "Turkmenistan", "Uganda", "Ukraine", "United Arab Emirates", "United Kingdom", "United States",
    "Uruguay", "Uzbekistan", "Venezuela", "Vietnam", "Yemen", "Zambia", "Zimbabwe",
}


def _extract_org_structure_from_text(text: str) -> dict:
    """Extract parent company / headquarters clues from website text.

    Returns a dict with keys:
      - org_structure_type: independent | subsidiary | division | branch | franchise | ""
      - parent_company_name: str
      - parent_company_country: str
      - source: apollo | website_heuristic | ""
    """
    result = {
        "org_structure_type": "",
        "parent_company_name": "",
        "parent_company_country": "",
        "source": "",
    }
    if not text:
        return result

    # Strip HTML if the caller passed raw HTML
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    lower = clean.lower()

    # Subsidiary / division / ownership patterns.  Capture the company name that
    # follows the signal phrase, stopping at punctuation or common conjunctions.
    sub_patterns = [
        r"(?:a\s+)?subsidiary\s+of\s+([A-Z][A-Za-z0-9\s&.,\-']+?)(?:\.|,|\s+and|\s+—|\s+–|\s+-|\Z)",
        r"(?:a\s+)?division\s+of\s+([A-Z][A-Za-z0-9\s&.,\-']+?)(?:\.|,|\s+and|\s+—|\s+–|\s+-|\Z)",
        r"(?:owned|operated)\s+by\s+([A-Z][A-Za-z0-9\s&.,\-']+?)(?:\.|,|\s+and|\s+—|\s+–|\s+-|\Z)",
        r"(?:part|member)\s+of\s+(?:the\s+)?([A-Z][A-Za-z0-9\s&.,\-']+?)(?:\.|,|\s+and|\s+—|\s+–|\s+-|\Z)",
        r"(?:brand|company)\s+of\s+([A-Z][A-Za-z0-9\s&.,\-']+?)(?:\.|,|\s+and|\s+—|\s+–|\s+-|\Z)",
        r"(?:wholly\s+owned\s+by)\s+([A-Z][A-Za-z0-9\s&.,\-']+?)(?:\.|,|\s+and|\s+—|\s+–|\s+-|\Z)",
    ]
    for pattern in sub_patterns:
        m = re.search(pattern, clean, re.IGNORECASE)
        if m:
            result["parent_company_name"] = m.group(1).strip()
            if "division" in pattern.lower():
                result["org_structure_type"] = "division"
            else:
                result["org_structure_type"] = "subsidiary"
            result["source"] = "website_heuristic"
            break

    # Franchise / authorized dealer
    if not result["org_structure_type"]:
        franchise_patterns = [
            r"\bfranchise(?:e|or)?\b",
            r"\bauthorized\s+dealer\b",
            r"\blicensed\s+dealer\b",
        ]
        for p in franchise_patterns:
            if re.search(p, lower):
                result["org_structure_type"] = "franchise"
                result["source"] = "website_heuristic"
                break

    # Independent signals
    if not result["org_structure_type"]:
        ind_signals = ["independent", "family-owned", "family owned", "privately owned", "founder-owned", "founder owned"]
        if any(s in lower for s in ind_signals):
            result["org_structure_type"] = "independent"
            result["source"] = "website_heuristic"

    # Try to extract HQ country when a parent company was found.
    if result["parent_company_name"]:
        country_patterns = [
            r"(?:headquartered|based|located)\s+in\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})",
            r"(?:headquarters|hq)\s*(?:in|:)\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})",
        ]
        for cp in country_patterns:
            m = re.search(cp, clean, re.IGNORECASE)
            if m:
                candidate = m.group(1).strip()
                if candidate in _COUNTRY_NAME_SET:
                    result["parent_company_country"] = candidate
                    break

    return result

def _apollo_extract_product_terms(user_keywords: List[str]) -> Set[str]:
    """Extract product terms from user-supplied Apollo keywords.

    Strips away channel-role words (distributor, wholesaler, etc.) and common
    stop words so that relevance scoring focuses on the actual product/category.
    Example: 'baseball equipment distributor' -> {'baseball', 'equipment'}.
    """
    stop_words = {
        "and", "or", "the", "of", "for", "in", "with", "by", "to", "a", "an",
        "company", "companies", "inc", "llc", "ltd", "co", "corp", "corporation",
        "usa", "us", "uk", "america", "american", "international", "global",
    }
    terms: Set[str] = set()
    for kw in user_keywords:
        for part in kw.lower().split():
            part = part.strip(".,;:!?()[]{}\"'")
            if not part or part in stop_words or part in APOLLO_CHANNEL_ROLES:
                continue
            terms.add(part)
    return terms


def _apollo_org_text(person: dict) -> str:
    """Extract searchable text from a person's organization data.

    Prefers enriched org data from /people/match, falls back to the free
    organization block returned by people_search, and finally to the raw
    organization_name string.
    """
    org = person.get("_enriched_org", {}) or {}
    if org:
        parts = [
            org.get("name", ""),
            org.get("industry", ""),
            " ".join(org.get("industries", [])),
            " ".join(org.get("keywords", [])),
            org.get("short_description", ""),
        ]
        return " ".join(parts).lower()

    free_org = person.get("_apollo_org", {}) or {}
    if free_org:
        return str(free_org.get("name", "")).lower()

    return str(person.get("organization_name", "")).lower()


def _score_apollo_contact_relevance(person: dict) -> int:
    """Score Apollo contact relevance based on org industry, keywords, description,
    and company name. Returns integer score. Negative = likely irrelevant.
    """
    score = 0
    org_text = _apollo_org_text(person)
    person_title = (person.get("position") or "").lower()

    # ------------------------------------------------------------------
    # Helper classifiers for B2B intent and low-value retail/rental forms.
    # ------------------------------------------------------------------
    b2b_roles = {
        "distributor", "distributors", "distributing", "distribution",
        "importer", "importers", "importing", "import", "imports",
        "exporter", "exporters", "exporting", "export", "exports",
        "wholesaler", "wholesalers", "wholesale", "wholesaling",
        "supplier", "suppliers", "supplying", "supply", "supplies",
        "dealer", "dealers", "dealing",
        "reseller", "resellers", "reselling",
        "vendor", "vendors",
        "manufacturer", "manufacturers", "manufacturing", "manufacture",
        "oem", "odm",
        "trading", "trade", "trader", "traders",
        "merchant", "merchants",
    }

    def _has_b2b_role(text: str) -> bool:
        return any(role in text for role in b2b_roles)

    def _is_large_sporting_retailer(text: str) -> bool:
        # Multi-location sporting goods chains: high-value accounts even if not
        # labeled as distributors.
        return any(sig in text for sig in {
            "sporting goods", "sports goods", "sports authority", "dick's",
            "dicks sporting", "academy sports", "big 5", "modell's", "modells",
            "olympia sport", "sports direct", "decathlon", "rei ", "mec ",
            "mountain equipment co-op",
        })

    def _has_weak_retail_signal(text: str) -> bool:
        # Single-location / rental / used-gear / consignment forms are poor B2B targets.
        return any(sig in text for sig in {
            "pro shop", "proshop", "rental", "rentals", "rent ", "hire ",
            "used ", "second hand", "second-hand", "pre-owned", "preowned",
            "thrift", "consignment", "outfitter", "outfitters",
        })

    def _has_resort_signal(text: str) -> bool:
        return any(sig in text for sig in {
            "resort", "resorts", "hotel", "hotels", "lodge", "lodges", "spa",
            "ski area", "ski mountain", "snow resort", "mountain resort",
            "ski resort", "ski lodge",
        })

    # Strong negative forms first.
    if _has_resort_signal(org_text):
        score -= 35
    if _has_weak_retail_signal(org_text):
        score -= 25

    org = person.get("_enriched_org", {}) or {}
    if org:
        industry = (org.get("industry") or "").lower()
        industries = [i.lower() for i in org.get("industries", [])]
        keywords = [k.lower() for k in org.get("keywords", [])]
        description = (org.get("short_description") or "").lower()

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

    # Company name scoring (works with or without enriched org data)
    if org_text:
        # Product/category signals are only meaningful when paired with a B2B
        # channel signal or a known large retailer. Otherwise 'Ski Resort' would
        # score too highly just because it contains 'ski'.
        product_match = any(pos in org_text for pos in APOLLO_PRODUCT_KEYWORDS)
        if product_match:
            if _has_b2b_role(org_text) or _is_large_sporting_retailer(org_text):
                score += 15
            else:
                score += 3  # tiny token bonus, not enough to survive on its own

        # High-value B2B channel roles
        if _has_b2b_role(org_text):
            score += 12

        # Large sporting goods chains are worth keeping even without a B2B label
        if _is_large_sporting_retailer(org_text):
            score += 8

        for neg in APOLLO_NEGATIVE_KEYWORDS:
            if neg in org_text:
                score -= 20

    # Title scoring (bonus for purchasing roles)
    if any(t in person_title for t in ["buyer", "purchasing", "procurement", "sourcing", "merchandising"]):
        score += 12
    elif any(t in person_title for t in ["manager", "director", "vp", "vice president", "head of"]):
        score += 3

    return score


def _score_apollo_organization_relevance(org: dict) -> int:
    """Score an Apollo organization for B2B relevance.

    Similar to _score_apollo_contact_relevance but works on organization-level
    data before any contacts are fetched. Returns integer score; negative means
    likely irrelevant.
    """
    score = 0
    org_name = (org.get("name") or "").lower()
    description = (org.get("short_description") or "").lower()
    keywords = " ".join(k.lower() for k in (org.get("keywords") or []))
    industry = (org.get("industry") or "").lower()
    industries = " ".join(i.lower() for i in (org.get("industries") or []))
    all_text = f"{org_name} {description} {keywords} {industry} {industries}".strip()

    b2b_roles = {
        "distributor", "distributors", "distributing", "distribution",
        "importer", "importers", "importing", "import", "imports",
        "exporter", "exporters", "exporting", "export", "exports",
        "wholesaler", "wholesalers", "wholesale", "wholesaling",
        "supplier", "suppliers", "supplying", "supply", "supplies",
        "dealer", "dealers", "dealing",
        "reseller", "resellers", "reselling",
        "vendor", "vendors",
        "manufacturer", "manufacturers", "manufacturing", "manufacture",
        "oem", "odm",
        "trading", "trade", "trader", "traders",
        "merchant", "merchants",
    }

    def _has_b2b_role(text: str) -> bool:
        return any(role in text for role in b2b_roles)

    def _is_large_sporting_retailer(text: str) -> bool:
        return any(sig in text for sig in {
            "sporting goods", "sports goods", "sports authority", "dick's",
            "dicks sporting", "academy sports", "big 5", "modell's", "modells",
            "olympia sport", "sports direct", "decathlon", "rei ", "mec ",
            "mountain equipment co-op",
        })

    def _has_weak_retail_signal(text: str) -> bool:
        return any(sig in text for sig in {
            "pro shop", "proshop", "rental", "rentals", "rent ", "hire ",
            "used ", "second hand", "second-hand", "pre-owned", "preowned",
            "thrift", "consignment", "outfitter", "outfitters",
        })

    def _has_resort_signal(text: str) -> bool:
        return any(sig in text for sig in {
            "resort", "resorts", "hotel", "hotels", "lodge", "lodges", "spa",
            "ski area", "ski mountain", "snow resort", "mountain resort",
            "ski resort", "ski lodge",
        })

    # Strong negative forms first.
    if _has_resort_signal(all_text):
        score -= 35
    if _has_weak_retail_signal(all_text):
        score -= 25

    # Industry scoring
    if industry:
        for pos in APOLLO_POSITIVE_INDUSTRIES:
            if pos in industry:
                score += 20
        for neg in APOLLO_NEGATIVE_INDUSTRIES:
            if neg in industry:
                score -= 15

    for ind in industries.split():
        for pos in APOLLO_POSITIVE_INDUSTRIES:
            if pos in ind:
                score += 12
        for neg in APOLLO_NEGATIVE_INDUSTRIES:
            if neg in ind:
                score -= 10

    # Keywords scoring
    for pos in APOLLO_POSITIVE_KEYWORDS:
        if pos in keywords:
            score += 10
    for neg in APOLLO_NEGATIVE_KEYWORDS:
        if neg in keywords:
            score -= 8

    # Company name scoring
    product_match = any(pos in org_name for pos in APOLLO_PRODUCT_KEYWORDS)
    if product_match:
        if _has_b2b_role(org_name) or _is_large_sporting_retailer(org_name):
            score += 15
        else:
            score += 3

    if _has_b2b_role(org_name):
        score += 12

    if _is_large_sporting_retailer(org_name):
        score += 8

    for neg in APOLLO_NEGATIVE_KEYWORDS:
        if neg in org_name:
            score -= 20

    return score


def _apollo_has_positive_signal(person: dict, user_keywords: List[str]) -> bool:
    """Return True if the contact is a plausible B2B lead.

    For product-keyword searches we now require BOTH product relevance AND
    B2B intent. A generic product word like 'ski' or 'glove' alone is not
    enough, because it matches ski resorts, rental shops, used-gear stores,
    schools, tour operators, etc.
    """
    org_text = _apollo_org_text(person)
    if not org_text:
        return False

    user_keywords_lower = [k.lower() for k in user_keywords if k.strip()]
    product_terms = _apollo_extract_product_terms(user_keywords)

    # Strong negative signal in company name is enough to reject immediately.
    for neg in APOLLO_NEGATIVE_KEYWORDS:
        if neg in org_text:
            return False

    b2b_roles = {
        "distributor", "distributors", "distributing", "distribution",
        "importer", "importers", "importing", "import", "imports",
        "exporter", "exporters", "exporting", "export", "exports",
        "wholesaler", "wholesalers", "wholesale", "wholesaling",
        "supplier", "suppliers", "supplying", "supply", "supplies",
        "dealer", "dealers", "dealing",
        "reseller", "resellers", "reselling",
        "vendor", "vendors",
        "manufacturer", "manufacturers", "manufacturing", "manufacture",
        "oem", "odm",
        "trading", "trade", "trader", "traders",
        "merchant", "merchants",
        "buying", "buyer", "buyers",
        "procurement", "purchasing", "sourcing",
    }

    def _has_b2b_role(text: str) -> bool:
        return any(role in text for role in b2b_roles)

    def _is_large_sporting_retailer(text: str) -> bool:
        return any(sig in text for sig in {
            "sporting goods", "sports goods", "sports authority", "dick's",
            "dicks sporting", "academy sports", "big 5", "modell's", "modells",
            "olympia sport", "sports direct", "decathlon", "rei ", "mec ",
            "mountain equipment co-op",
        })

    title = (person.get("position") or person.get("title") or "").lower()
    purchasing_title = any(t in title for t in [
        "buyer", "purchasing", "procurement", "sourcing", "merchandising", "merchandise"
    ])

    # Early guard: retail operations roles at generic ski/outdoor entities that
    # lack a B2B channel signal or known multi-location chain identity are
    # usually resort/single-shop staff (e.g. "Retail Buyer/artist" at Ski Santa Fe),
    # not the offline chain retailers we want. Reject before case-by-case logic
    # can accept them via the broad "buyer" keyword.
    if "retail" in title and any(pos in org_text for pos in APOLLO_PRODUCT_KEYWORDS):
        if not _has_b2b_role(org_text) and not _is_large_sporting_retailer(org_text):
            retailer_indicators = {"shop", "shops", "sports", "sport", "outdoor", "outdoors", "store", "stores"}
            if not any(ind in org_text for ind in retailer_indicators):
                return False

    # Case 1: exact user keyword match in org text
    if user_keywords_lower and any(kw in org_text for kw in user_keywords_lower):
        # If keyword already includes a channel role, accept.
        if any(_has_b2b_role(kw) for kw in user_keywords_lower):
            return True
        # Otherwise require B2B signal, purchasing title, or large retailer.
        if purchasing_title or _has_b2b_role(org_text) or _is_large_sporting_retailer(org_text):
            return True
        # fall through to stricter checks below

    # Case 2: product terms extracted from user keywords require B2B pairing
    if product_terms and any(term in org_text for term in product_terms):
        if purchasing_title or _has_b2b_role(org_text) or _is_large_sporting_retailer(org_text):
            return True

    # Case 3: generic product keyword match requires B2B channel signal
    if any(pos in org_text for pos in APOLLO_PRODUCT_KEYWORDS):
        if purchasing_title and _has_b2b_role(org_text):
            return True
        if _has_b2b_role(org_text):
            return True
        if _is_large_sporting_retailer(org_text):
            return True

    # Case 4: enriched-org positive industry only counts with B2B or purchasing signal
    org = person.get("_enriched_org", {}) or {}
    if org:
        industry = (org.get("industry") or "").lower()
        industries = [i.lower() for i in org.get("industries", [])]
        all_industries = " ".join([industry] + industries)
        if any(pos in all_industries for pos in APOLLO_POSITIVE_INDUSTRIES):
            if purchasing_title or _has_b2b_role(org_text) or _is_large_sporting_retailer(org_text):
                return True

    # Case 5: fallback for channel-role searches. The org must show product/industry
    # relevance; purchasing title alone is not enough because Apollo matches the
    # channel role broadly and returns distributors from unrelated industries.
    if user_keywords_lower:
        for kw in user_keywords_lower:
            if any(role in kw for role in APOLLO_CHANNEL_ROLES):
                if _has_b2b_role(org_text):
                    product_terms = _apollo_extract_product_terms(user_keywords)
                    product_indicators = set(product_terms) | APOLLO_PRODUCT_KEYWORDS
                    if any(term in org_text for term in product_indicators):
                        return True

    return False


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

    # Extract product core words (exclude generic B2B filler words).
    # Keep short product words like "ski" (3 chars) since they are meaningful.
    b2b_filler = {"wholesale", "supplier", "manufacturer", "distributor",
                  "importer", "dealer", "reseller", "oem", "odm", "factory"}
    kw_parts = [p for p in kw_lower.split() if len(p) >= 3 and p not in b2b_filler]

    # Signal lists (used both for scoring and for the strict-match override below).
    b2b_signals = ["distributor", "wholesaler", "importer", "dealer",
                   "reseller", "stockist", "agent", "supplier"]
    mfg_signals = ["manufacturer", "factory", "oem", "odm", "producer",
                   "private label", "custom", "bespoke", "bulk"]
    specialty_retail_signals = [
        "ski store", "ski shop", "snowboard shop", "snowboard store",
        "outdoor store", "outdoor shop", "outdoor retailer",
        "pro shop", "proshop", "specialty store",
    ]

    # CRITICAL relevance check:
    # If the search phrase has multiple product words (e.g. "football gloves"),
    # the title should contain the FULL phrase OR all product words.
    # A page titled "Basketball Training Gloves" should NOT match "football gloves".
    # EXCEPTION: pages with clear B2B or specialty-retail intent are category
    # matches and should not be penalized for missing one product word.
    title_lower = title.lower()
    has_category_intent = (
        any(s in text for s in b2b_signals + mfg_signals + specialty_retail_signals)
    )
    if kw_parts and not has_category_intent:
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
    for sig in b2b_signals:
        if sig in text:
            score += 15

    # Manufacturer / factory signals
    for sig in mfg_signals:
        if sig in text:
            score += 12

    # Negative: big platform / directory signals
    platform_signals = ["amazon", "alibaba", "ebay", "walmart", "shopify",
                        "directory", "marketplace", "list of", "top 10",
                        "review", "buying guide", "vs ", "compare"]
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

    # Established specialty retailers (multi-store pro shops, ski shops, etc.)
    # are B-tier customers even if they are not distributors.
    for sig in specialty_retail_signals:
        if sig in text:
            score += 8

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
    kw_parts = [p for p in keyword.lower().split() if len(p) >= 3]
    for part in kw_parts:
        if part in text:
            score += 8

    # Established specialty retailers are valuable B-tier leads even when they
    # do not label themselves as distributors/wholesalers.
    specialty_retail_signals = [
        "ski store", "ski shop", "snowboard shop", "snowboard store",
        "outdoor store", "outdoor shop", "outdoor retailer",
        "pro shop", "proshop", "specialty store",
    ]
    for sig in specialty_retail_signals:
        if sig in text:
            score += 12

    # Multi-store / established retailer signals (e.g. "come into one of the stores")
    multi_store_signals = [" stores", "locations", "since 19", "since 20", "established"]
    for sig in multi_store_signals:
        if sig in text:
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
    # Supplier portal discovery
    supplier_page_url: str = ""
    supplier_page_title: str = ""
    supplier_email: str = ""
    supplier_form_link: str = ""
    supplier_notes: str = ""
    # Lead maintenance / verification fields
    domain_alive: bool = False
    domain_check_error: str = ""
    email_valid: bool = False
    company_active: bool = True
    company_status_notes: str = ""
    last_verified_at: str = ""
    # Parent company / purchasing authority fields
    org_structure_type: str = ""          # independent | subsidiary | division | branch | franchise | unknown
    parent_company_name: str = ""          # Parent / headquarters company name
    parent_company_country: str = ""       # Parent / headquarters country
    hq_country: str = ""                   # Alias for parent_company_country
    purchasing_authority: str = ""         # local | headquarter | unknown
    purchasing_authority_reason: str = ""  # Human-readable explanation
    parent_org_data_source: str = ""       # apollo | website_heuristic | inferred
    # Customer value tier (A/B/C)
    tier: str = ""                         # A | B | C
    tier_reason: str = ""                  # Human-readable explanation for tier


def _classify_purchasing_authority(lead: Lead) -> Tuple[str, str]:
    """Classify where procurement decisions are likely made for this lead.

    Returns a tuple of (purchasing_authority, reason) where authority is one of:
      - headquarter: decisions made at parent / HQ level
      - local: decisions made by the local entity
      - unknown: not enough information
    """
    org_type = (lead.org_structure_type or "").lower()
    parent = (lead.parent_company_name or "").strip()
    parent_country = (lead.parent_company_country or lead.hq_country or "").strip()
    lead_country = (lead.country or "").strip()

    # Known subsidiary / division / branch
    if org_type in ("subsidiary", "division", "branch"):
        if parent and parent_country:
            if lead_country and lead_country != parent_country:
                return (
                    "headquarter",
                    f"{org_type.capitalize()} of {parent} (HQ in {parent_country}); "
                    f"local office in {lead_country}. Procurement typically centralized at HQ.",
                )
            return (
                "headquarter",
                f"{org_type.capitalize()} of {parent} (HQ in {parent_country}). "
                "Procurement typically centralized at HQ.",
            )
        return (
            "headquarter",
            f"Identified as {org_type}; procurement decisions typically sit at parent/HQ level.",
        )

    # Franchise
    if org_type == "franchise":
        return (
            "unknown",
            "Franchise location; purchasing may be local for some categories or dictated by the franchisor.",
        )

    # Independent
    if org_type == "independent":
        return ("local", "Independent company; procurement decisions are made locally.")

    # Infer from website description when Apollo has no explicit structure data
    if lead.website_description:
        desc = lead.website_description.lower()
        branch_signals = ["branch", "regional office", "local office", "representative office"]
        if any(s in desc for s in branch_signals):
            return (
                "headquarter",
                "Website indicates this is a branch/office; procurement likely centralized at HQ.",
            )
        local_signals = [
            "local distributor", "authorized dealer", "regional distributor",
            "local agent", "exclusive distributor for", "local partner",
            "independent distributor",
        ]
        if any(s in desc for s in local_signals):
            return (
                "local",
                "Website describes company as local/regional distributor; local purchasing authority likely.",
            )

    return (
        "unknown",
        "Insufficient data to determine purchasing authority. Recommend researching parent company structure.",
    )


# ---------------------------------------------------------------------------
# Customer value tier classification (A / B / C)
# ---------------------------------------------------------------------------

# Strong signals that a lead is a core/large customer (A tier)
TIER_A_SIGNALS = [
    "brand", "manufacturer", "oem", "odm", "original equipment",
    "wholesale", "wholesaler", "distributor", "importer", "exporter",
    "international", "global", "worldwide", "chain", "retail chain",
    "group", "corporate", "headquarter", "buying office", "holding",
    "enterprise", "public company", "listed company",
]

# Signals for medium-value potential customers (B tier)
TIER_B_SIGNALS = [
    "retailer", "retail store", "sport store", "sports shop", "dealer",
    "reseller", "regional", "local distributor", "authorized dealer",
    "multi-store", "outlet", "specialty store", "pro shop",
    # Ski / outdoor specialty retail
    "ski store", "ski shop", "snowboard shop", "snowboard store",
    "outdoor store", "outdoor shop", "outdoor retailer",
    "ski & sports", "ski and sports",
]

# Signals that demote a lead to low-value (C tier)
TIER_C_SIGNALS = [
    "resort", "ski resort", "ski area", "rental", "rentals", "ski rental",
    "ski school", "lesson", "lessons", "tour", "tours", "guide", "guides",
    "repair", "repairs", "used", "second-hand", "second hand", "pre-owned",
    "small", "single store", "single location", "local shop", "hobby",
    "club", "association", "charity", "non-profit", "community",
    "blog", "review", "news", "magazine", "forum",
]


def _classify_tier(lead: Lead) -> Tuple[str, str]:
    """Classify lead into A/B/C tier based on customer value signals.

    A = core large customers (brands, manufacturers, international distributors)
    B = potential customers (regional dealers, medium stores, local distributors)
    C = low-value leads (resorts, rentals, small shops, lessons/tours)

    Returns a tuple of (tier, reason).
    """
    text_parts = [
        (lead.company or "").lower(),
        (lead.website_description or "").lower(),
        (lead.position or "").lower(),
        (lead.domain or "").lower(),
    ]
    text = " ".join(text_parts)

    # Count signal hits
    a_hits = [s for s in TIER_A_SIGNALS if s in text]
    b_hits = [s for s in TIER_B_SIGNALS if s in text]
    c_hits = [s for s in TIER_C_SIGNALS if s in text]

    # "brand" is a strong A signal, but phrases like "best brands" / "top brands"
    # just mean a retailer carries multiple brands. Filter that false positive.
    if "brand" in a_hits:
        if re.search(r"\b(best|top|leading|popular|carried|carry|carries)\s+\w*\s*brands?\b", text):
            a_hits = [s for s in a_hits if s != "brand"]

    # Contact quality boosters
    has_personal_email = lead.email_type == "personal"
    has_direct_phone = lead.has_direct_phone
    high_confidence = lead.confidence_score >= 70
    hq_authority = lead.purchasing_authority == "headquarter"
    has_parent = bool(lead.parent_company_name)

    # Decide tier
    if c_hits and not (a_hits or b_hits):
        # Strong C-only signals -> C
        return "C", f"Low-value signals: {', '.join(c_hits[:3])}"

    if a_hits:
        # Strong A signals + reasonable quality/relevance -> A
        quality_ok = has_personal_email or high_confidence or has_direct_phone or hq_authority
        relevance_ok = lead.relevance_score >= 40 or hq_authority or has_parent
        # Brand/manufacturer + personal email is a strong A pattern regardless of relevance
        brand_or_maker = any(s in text for s in ["brand", "manufacturer", "oem", "odm"])
        if quality_ok and (relevance_ok or (brand_or_maker and has_personal_email)):
            return "A", f"Core customer signals: {', '.join(a_hits[:3])}"
        # A signals but weaker quality -> B
        return "B", f"Potential customer (A signals but lower contact quality): {', '.join(a_hits[:3])}"

    if b_hits:
        # Established specialty retailers / multi-store chains are valuable B-tier
        # customers even with modest or missing relevance scores.
        strong_b_signals = {
            "ski store", "ski shop", "snowboard shop", "snowboard store",
            "outdoor store", "outdoor shop", "outdoor retailer",
            "pro shop", "specialty store", "retail chain", "multi-store",
            "ski & sports", "ski and sports",
        }
        has_strong_b = any(s in strong_b_signals for s in b_hits)
        threshold = 0 if has_strong_b else 30
        if lead.relevance_score >= threshold:
            return "B", f"Potential customer signals: {', '.join(b_hits[:3])}"
        return "C", f"Weak relevance with retail signals: {', '.join(b_hits[:3])}"

    # No strong signals; use relevance and contact quality as tie-breakers
    if lead.relevance_score >= 70 and (has_personal_email or high_confidence or has_direct_phone):
        return "B", "High relevance but no explicit tier signals"

    return "C", "No strong value signals; low priority"


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
        # Try HTTPS first, then HTTP on SSL issues.
        for scheme in ("https", "http"):
            url = f"{scheme}://{domain}{path}"
            try:
                resp = _get_session().get(url, headers=headers, timeout=timeout, allow_redirects=True)
                if resp.status_code != 200:
                    break
                html = resp.text
                matches = re.findall(r"<p[^>]*>(.*?)</p>", html, flags=re.IGNORECASE | re.DOTALL)
                for m in matches:
                    text = _strip_html_tags(m).strip()
                    if len(text) >= 30 and not text.lower().startswith(("home", "menu", "contact", "about us", "copyright")):
                        if len(text) > 600:
                            text = text[:600].rsplit(".", 1)[0] + "."
                        return html, text
                break  # path exists but no usable paragraph; don't retry other scheme
            except requests.exceptions.SSLError:
                continue
            except Exception:
                break
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
            # Try HTTPS first; some sites have broken certificates, so fall back to HTTP.
            for scheme in ("https", "http"):
                try:
                    page.goto(f"{scheme}://{domain}", timeout=timeout * 1000, wait_until="domcontentloaded")
                    time.sleep(1.5)  # Allow JS frameworks to hydrate
                    html = page.content()
                    browser.close()
                    return html
                except Exception as e:
                    err = str(e).lower()
                    if "ssl" in err or "certificate" in err or "tls" in err:
                        continue
                    browser.close()
                    return ""
            browser.close()
            return ""
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
    # Try HTTPS first; on SSL errors fall back to HTTP so self-signed/misconfigured
    # certificates do not hang the entire pipeline.
    for url in (f"https://{domain}", f"http://{domain}"):
        try:
            resp = _get_session().get(url, headers=headers, timeout=timeout, allow_redirects=True)
            if resp.status_code == 200:
                homepage_html = resp.text
                break
        except requests.exceptions.SSLError:
            continue
        except Exception:
            break

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


def normalize_company_domain(domain: str) -> str:
    """Normalize known recruiting/HR subdomains to the main corporate domain."""
    if not domain:
        return domain
    replacements = {
        "dickssportinggoods.jobs": "dickssportinggoods.com",
    }
    if domain in replacements:
        return replacements[domain]
    # Strip common recruiting subdomains: careers.example.com -> example.com
    recruiting_prefixes = ("careers.", "jobs.", "workday.", "apply.")
    for prefix in recruiting_prefixes:
        if domain.startswith(prefix):
            return domain[len(prefix):]
    return domain


def extract_domain(url: str) -> Optional[str]:
    """Extract clean domain from a URL."""
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return normalize_company_domain(domain)
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
                resp = _get_session().get(self.base_url, params=params, timeout=30)
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
                err_msg = str(e)
                if "429" in err_msg or "Too Many Requests" in err_msg:
                    print(f"  [RATE_LIMIT] SerpAPI search rate limited (429). Try --engine duckduckgo or upgrade your SerpAPI plan.")
                else:
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

        last_error = ""
        for attempt in range(3):
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
                return results
            except Exception as e:
                last_error = str(e)
                print(f"  [DuckDuckGo ERROR] attempt {attempt + 1}/3: {e}")
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        print(f"  [DuckDuckGo] All attempts failed. Last error: {last_error}")
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
                resp = _get_session().get(url, params=params, timeout=15)
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
            resp = _get_session().get(url, params=params, timeout=15)
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
        self._match_cache: Dict[str, dict] = {}

    @staticmethod
    def _expand_keyword_tags(keywords: List[str]) -> List[str]:
        """Expand composite 'product + channel role' keywords into Apollo keyword tags.

        Apollo's q_organization_keyword_tags parameter matches against pre-existing
        keyword tags in its database. Long-tail phrases like 'football gloves distributor'
        rarely exist as a single tag, causing empty results. We preserve the original
        phrase and additionally emit shorter product/role tags to improve recall.
        The list is treated as OR by Apollo, so unrelated distributors may match
        the bare role tag; downstream relevance scoring removes them.
        """
        channel_roles = {
            "distributor", "distributors", "importer", "importers",
            "supplier", "suppliers", "wholesaler", "wholesalers", "wholesale",
            "dealer", "dealers", "reseller", "resellers", "buyer", "buyers",
            "retailer", "retailers", "exporter", "exporters", "manufacturer",
            "manufacturers", "oem", "odm", "vendor", "vendors",
        }
        tags: List[str] = []
        seen: Set[str] = set()
        for kw in keywords:
            kw = kw.strip()
            if not kw:
                continue
            original_lower = kw.lower()
            if original_lower not in seen:
                seen.add(original_lower)
                tags.append(original_lower)
            parts = original_lower.split()
            if len(parts) <= 1:
                continue
            role_indices = [i for i, p in enumerate(parts) if p in channel_roles]
            if role_indices:
                first_role = role_indices[0]
                product_phrase = " ".join(parts[:first_role]).strip()
                if product_phrase and product_phrase not in seen:
                    seen.add(product_phrase)
                    tags.append(product_phrase)
                # Emit the bare channel role as well for recall. Apollo treats
                # q_organization_keyword_tags as OR, so a lone "distributor" tag
                # can return distributors of unrelated products. We keep the tag
                # for recall and rely on downstream relevance scoring to drop
                # irrelevant distributors.
                for i in role_indices:
                    if parts[i] not in seen:
                        seen.add(parts[i])
                        tags.append(parts[i])
            else:
                # No recognized channel role: emit sliding-window sub-phrases.
                for length in range(len(parts) - 1, 0, -1):
                    for start in range(0, len(parts) - length + 1):
                        sub = " ".join(parts[start:start + length])
                        if sub not in seen:
                            seen.add(sub)
                            tags.append(sub)
        return tags

    def _merge_enriched_fields(self, person: dict, enriched: dict) -> dict:
        """Copy email/org fields from an Apollo enrichment response into person."""
        if enriched.get("email"):
            person["email"] = enriched["email"]
        if enriched.get("last_name"):
            person["last_name"] = enriched["last_name"]
        enriched_org = enriched.get("organization", {}) or {}
        if enriched_org:
            person["_enriched_org"] = enriched_org
            person["_parent_org_name"] = enriched_org.get("parent_organization_name", "")
            hq = enriched_org.get("headquarters", {}) or {}
            if isinstance(hq, dict):
                person["_hq_country"] = hq.get("country", "")
            else:
                person["_hq_country"] = ""
            if enriched_org.get("is_subsidiary") or person["_parent_org_name"]:
                person["_org_structure_type"] = "subsidiary"
            elif enriched_org.get("is_subsidiary") is False:
                person["_org_structure_type"] = "independent"
            else:
                person["_org_structure_type"] = ""
            if enriched_org.get("primary_domain"):
                person["organization_website"] = f"http://{enriched_org['primary_domain']}"
            elif enriched_org.get("website_url"):
                person["organization_website"] = enriched_org["website_url"]
        return person

    def _enrich_person(self, person: dict) -> dict:
        """Call /people/match to reveal full contact details."""
        pid = person.get("id")
        if not pid:
            return person
        if pid in self._match_cache:
            self._merge_enriched_fields(person, self._match_cache[pid])
            return person

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
        }
        try:
            match_resp = _get_session().post(
                f"{self.base_url}/people/match",
                headers=headers,
                json={
                    "id": pid,
                    "reveal_personal_emails": True,
                },
                timeout=8,
            )
            if match_resp.status_code == 200:
                match_data = match_resp.json()
                enriched = match_data.get("person", {})
                self._merge_enriched_fields(person, enriched)
                self._match_cache[pid] = {
                    "email": person.get("email"),
                    "last_name": person.get("last_name"),
                    "_enriched_org": person.get("_enriched_org"),
                    "_parent_org_name": person.get("_parent_org_name"),
                    "_hq_country": person.get("_hq_country"),
                    "_org_structure_type": person.get("_org_structure_type"),
                    "organization_website": person.get("organization_website"),
                }
        except Exception:
            pass
        return person

    def _bulk_enrich_people(self, people: List[dict]) -> List[dict]:
        """Enrich up to 10 people per call using Apollo bulk match.

        Apollo supports up to 10 details per request, so we chunk the input and
        only call the API for contacts that are missing an email. This reduces
        both API request overhead and reveal-credit consumption compared to 1:1
        /people/match calls.
        """
        if not people:
            return []

        indexed = [(i, p) for i, p in enumerate(people) if p.get("id")]
        results = [p.copy() for p in people]

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
        }
        credits_exhausted = False

        def _enrich_chunk(chunk: List[tuple]) -> None:
            nonlocal credits_exhausted
            if credits_exhausted:
                return
            payload = {
                "details": [{"id": p.get("id")} for _, p in chunk],
                "reveal_personal_emails": True,
            }
            try:
                resp = _get_session().post(
                    f"{self.base_url}/people/bulk_match",
                    headers=headers,
                    json=payload,
                    timeout=20,
                )
                if resp.status_code == 429:
                    print("    [Apollo] Bulk match rate limited. Sleeping 5s...")
                    time.sleep(5)
                    return
                if resp.status_code == 422:
                    err_text = resp.text or ""
                    if "insufficient credits" in err_text.lower():
                        credits_exhausted = True
                        print("    [Apollo WARNING] Bulk match failed: account has insufficient Apollo credits. Enrichment skipped for remaining contacts.")
                        return
                if resp.status_code != 200:
                    print(f"    [Apollo] Bulk match returned {resp.status_code}, falling back to single /people/match")
                    for idx, orig in chunk:
                        enriched_copy = self._enrich_person(orig.copy())
                        self._merge_enriched_fields(results[idx], enriched_copy)
                    return
                data = resp.json() if resp.text else {}
                people_list = data.get("people", []) if isinstance(data, dict) else []
                for enriched in people_list:
                    pid = enriched.get("id")
                    if not pid:
                        continue
                    for idx, orig in chunk:
                        if orig.get("id") == pid:
                            self._merge_enriched_fields(results[idx], enriched)
                            self._match_cache[pid] = {
                                "email": results[idx].get("email"),
                                "last_name": results[idx].get("last_name"),
                                "_enriched_org": results[idx].get("_enriched_org"),
                                "_parent_org_name": results[idx].get("_parent_org_name"),
                                "_hq_country": results[idx].get("_hq_country"),
                                "_org_structure_type": results[idx].get("_org_structure_type"),
                                "organization_website": results[idx].get("organization_website"),
                            }
                            break
            except Exception as e:
                print(f"    [Apollo ERROR] bulk enrich chunk: {e}")

        for i in range(0, len(indexed), 10):
            _enrich_chunk(indexed[i:i + 10])

        return results

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
            resp = _get_session().post(url, headers=headers, json=payload, timeout=20)
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
        organization_keywords: Optional[List[str]] = None,
        person_titles: Optional[List[str]] = None,
        person_locations: Optional[List[str]] = None,
        organization_num_employees: Optional[List[str]] = None,
        organization_domains: Optional[List[str]] = None,
        country: Optional[str] = None,
        state: Optional[str] = None,
        city: Optional[str] = None,
        zip_code: Optional[str] = None,
        per_page: int = 100,
        page: int = 1,
        enrich: bool = True,
    ) -> List[dict]:
        """Search people via Apollo.io People Search API.

        Supports keyword-based discovery, domain-based expansion, and
        multi-level location filters (country, state, city, zip).
        """
        url = f"{self.base_url}/mixed_people/api_search"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
        }
        payload: dict = {
            "per_page": min(per_page, 100),
            "page": page,
        }
        if organization_domains:
            payload["organization_domains"] = organization_domains
        elif organization_keywords:
            payload["q_organization_keyword_tags"] = self._expand_keyword_tags(organization_keywords)
        if person_titles:
            payload["person_titles"] = person_titles
        if person_locations:
            payload["person_locations"] = person_locations
        if country:
            payload["country"] = country
        if state:
            payload["state"] = state
        if city:
            payload["city"] = city
        if zip_code:
            payload["zip_codes"] = [zip_code]
        if any([person_locations, country, state, city, zip_code]):
            print(
                f"    [Apollo] Location filters: "
                f"person_locations={person_locations}, country={country}, state={state}, city={city}, zip_codes={[zip_code] if zip_code else None}"
            )
        if organization_num_employees:
            # Apollo expects a list of range strings, e.g. ["2,50"].
            if isinstance(organization_num_employees, list) and len(organization_num_employees) == 2:
                payload["organization_num_employees"] = [
                    f"{organization_num_employees[0]},{organization_num_employees[1]}"
                ]
            else:
                payload["organization_num_employees"] = organization_num_employees
            print(f"    [Apollo] Employee range filter: {payload['organization_num_employees']}")
        # Doc-recommended filter: avoid re-prospecting contacts already contacted by the team.
        payload["prospected_by_current_team"] = ["no"]
        # Note: person_departments and contact_email_status are intentionally omitted
        # because they severely reduce results for specific product keywords like "football gloves".
        # Quality is enforced by downstream relevance scoring + Hunter enrichment.

        try:
            last_error = ""
            for attempt in range(2):
                try:
                    resp = _get_session().post(url, headers=headers, json=payload, timeout=15)
                    break
                except requests.exceptions.Timeout as e:
                    last_error = str(e)
                    print(f"    [Apollo] people search timeout (attempt {attempt + 1}/2)")
                    if attempt == 1:
                        raise
                    time.sleep(1)
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
            # Bulk enrichment via /people/bulk_match for contacts missing email.
            # Skip contacts that already have an email from the free search.
            # ------------------------------------------------------------------
            if people and enrich:
                to_enrich = [p for p in people if not p.get("email")]
                if to_enrich:
                    enriched_list = self._bulk_enrich_people(to_enrich)
                    enriched_by_id = {p.get("id"): p for p in enriched_list if p.get("id")}
                    enriched_count = 0
                    for p in people:
                        ep = enriched_by_id.get(p.get("id"))
                        if not ep:
                            continue
                        if ep.get("email") and not p.get("email"):
                            p["email"] = ep["email"]
                            enriched_count += 1
                        if ep.get("last_name") and not p.get("last_name"):
                            p["last_name"] = ep["last_name"]
                        if ep.get("_enriched_org") and not p.get("_enriched_org"):
                            p["_enriched_org"] = ep["_enriched_org"]
                        if ep.get("_parent_org_name") and not p.get("_parent_org_name"):
                            p["_parent_org_name"] = ep["_parent_org_name"]
                        if ep.get("_hq_country") and not p.get("_hq_country"):
                            p["_hq_country"] = ep["_hq_country"]
                        if ep.get("_org_structure_type") and not p.get("_org_structure_type"):
                            p["_org_structure_type"] = ep["_org_structure_type"]
                        if ep.get("organization_website") and not p.get("organization_website"):
                            p["organization_website"] = ep["organization_website"]
                    if enriched_count:
                        print(f"    [Apollo] Enriched {enriched_count}/{len(to_enrich)} missing contacts via bulk match")

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
                    "id": p.get("id", ""),
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
                # Pass enriched org data (including parent company) for downstream use
                if p.get("_enriched_org"):
                    result_item["_enriched_org"] = p["_enriched_org"]
                if p.get("organization"):
                    result_item["_apollo_org"] = p["organization"]
                if p.get("_parent_org_name"):
                    result_item["_parent_org_name"] = p["_parent_org_name"]
                if p.get("_hq_country"):
                    result_item["_hq_country"] = p["_hq_country"]
                if p.get("_org_structure_type"):
                    result_item["_org_structure_type"] = p["_org_structure_type"]
                results.append(result_item)
            return results
        except requests.exceptions.HTTPError as e:
            print(f"    [Apollo ERROR] people search: {e}")
            return []
        except Exception as e:
            print(f"    [Apollo ERROR] people search: {e}")
            return []

    def search_organizations(
        self,
        keyword_tags: Optional[List[str]] = None,
        locations: Optional[List[str]] = None,
        organization_num_employees: Optional[List[str]] = None,
        country: Optional[str] = None,
        state: Optional[str] = None,
        city: Optional[str] = None,
        zip_code: Optional[str] = None,
        per_page: int = 100,
        page: int = 1,
    ) -> List[dict]:
        """Search organizations via Apollo.io Company Search API.

        Uses POST /mixed_companies/search. Returns normalized organization dicts.
        """
        url = f"{self.base_url}/mixed_companies/search"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
        }
        payload: dict = {
            "per_page": min(per_page, 100),
            "page": page,
        }
        if keyword_tags:
            payload["q_organization_keyword_tags"] = self._expand_keyword_tags(keyword_tags)
        if locations:
            payload["organization_locations"] = locations
        if country:
            payload["country"] = country
        if state:
            payload["state"] = state
        if city:
            payload["city"] = city
        if zip_code:
            payload["zip_codes"] = [zip_code]
        if any([locations, country, state, city, zip_code]):
            print(
                f"    [Apollo] Location filters: "
                f"organization_locations={locations}, country={country}, state={state}, city={city}, zip_codes={[zip_code] if zip_code else None}"
            )
        if organization_num_employees:
            # Apollo expects a list of range strings, e.g. ["2,50"].
            if isinstance(organization_num_employees, list) and len(organization_num_employees) == 2:
                payload["organization_num_employees"] = [
                    f"{organization_num_employees[0]},{organization_num_employees[1]}"
                ]
            else:
                payload["organization_num_employees"] = organization_num_employees
            print(f"    [Apollo] Employee range filter: {payload['organization_num_employees']}")

        try:
            last_error = ""
            for attempt in range(2):
                try:
                    resp = _get_session().post(url, headers=headers, json=payload, timeout=15)
                    break
                except requests.exceptions.Timeout as e:
                    last_error = str(e)
                    print(f"    [Apollo] organization search timeout (attempt {attempt + 1}/2)")
                    if attempt == 1:
                        raise
                    time.sleep(1)
            if resp.status_code == 429:
                print(f"    [Apollo] Rate limited on organization search. Sleeping 5s...")
                time.sleep(5)
                return []
            if resp.status_code == 403:
                data = resp.json() if resp.text else {}
                err = data.get("error", "")
                if "free plan" in err.lower():
                    print("    [Apollo] Free plan key cannot use Organization Search. Skipping.")
                else:
                    print(f"    [Apollo] Forbidden (403): {err}")
                return []
            if resp.status_code == 422:
                try:
                    err_body = resp.json()
                except Exception:
                    err_body = {"error": resp.text}
                err_msg = str(err_body.get("error", "")).lower()
                if "insufficient credits" in err_msg or "credit" in err_msg:
                    print("    [Apollo] Organization Search requires lead credits. Your account has insufficient credits.")
                else:
                    print(f"    [Apollo DEBUG] organization search payload: {payload}")
                    print(f"    [Apollo DEBUG] organization search response (422): {err_body}")
                return []
            if resp.status_code >= 400:
                try:
                    err_body = resp.json()
                except Exception:
                    err_body = resp.text
                print(f"    [Apollo DEBUG] organization search payload: {payload}")
                print(f"    [Apollo DEBUG] organization search response ({resp.status_code}): {err_body}")
            resp.raise_for_status()
            data = resp.json()
            organizations = data.get("organizations", []) if isinstance(data, dict) else []

            results = []
            for org in organizations:
                website_url = org.get("website_url", "")
                domain = org.get("domain", "")
                if not domain and website_url:
                    domain = extract_domain(website_url)
                result_item = {
                    "id": org.get("id", ""),
                    "name": org.get("name", ""),
                    "website_url": website_url,
                    "domain": domain,
                    "industry": org.get("industry", ""),
                    "industries": org.get("industries", []) or [],
                    "keywords": org.get("keywords", []) or [],
                    "short_description": org.get("short_description", ""),
                    "num_employees": org.get("num_employees", 0),
                    "country": org.get("country", ""),
                    "state": org.get("state", ""),
                    "city": org.get("city", ""),
                    "phone": org.get("phone", ""),
                    "linkedin_url": org.get("linkedin_url", ""),
                }
                results.append(result_item)
            return results
        except requests.exceptions.HTTPError as e:
            print(f"    [Apollo ERROR] organization search: {e}")
            return []
        except Exception as e:
            print(f"    [Apollo ERROR] organization search: {e}")
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
            resp = _get_session().get(url, headers=headers, params=params, timeout=20)
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
            resp = _get_session().get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"    [ZeroBounce ERROR] {email}: {e}")
            return {"status": "unknown", "error": str(e)}


# ---------------------------------------------------------------------------
# Lead Maintenance / Verification
# ---------------------------------------------------------------------------

# Negative signals used to guess whether a company is still operating.
_COMPANY_NEGATIVE_SIGNALS = [
    "closed permanently", "permanently closed", "temporarily closed",
    "out of business", "ceased trading", "company dissolved",
    "under construction", "coming soon", "domain for sale", "this domain is for sale",
    "website suspended", "account suspended", "service suspended",
    "page not found", "404 - not found",
]


def _normalize_bool(value: Any) -> bool:
    """Convert common CSV string representations to bool."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    s = str(value).strip().lower()
    return s in {"true", "1", "yes", "y"}


def _leads_from_csv(path: str) -> List[Lead]:
    """Reconstruct Lead objects from a previously exported CSV."""
    leads: List[Lead] = []
    if not os.path.exists(path):
        print(f"[ERROR] CSV file not found: {path}")
        return leads

    lead_fields = {f.name for f in fields(Lead)}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            mapped: Dict[str, Any] = {}
            for key, raw in row.items():
                if key not in lead_fields:
                    continue
                if raw is None:
                    continue
                # type coercion for known non-string fields
                if key in {"domain_alive", "email_valid", "company_active", "has_direct_phone"}:
                    mapped[key] = _normalize_bool(raw)
                elif key in {"confidence_score", "relevance_score", "google_reviews_count"}:
                    try:
                        mapped[key] = int(raw) if raw.strip() else 0
                    except Exception:
                        mapped[key] = 0
                elif key == "google_rating":
                    try:
                        mapped[key] = float(raw) if raw.strip() else 0.0
                    except Exception:
                        mapped[key] = 0.0
                elif key == "sources":
                    mapped[key] = [s.strip() for s in raw.split(";") if s.strip()]
                else:
                    mapped[key] = raw.strip()
            leads.append(Lead(**mapped))
    return leads


def check_domain_alive(domain: str, timeout: int = 10) -> Tuple[bool, str]:
    """
    Probe a domain's homepage to see if it currently resolves and responds.

    Returns:
        (is_alive, error_message)

    A 2xx/3xx/4xx response is considered alive because the domain resolves and a
    server responds. Only DNS failures, connection errors, timeouts and serious
    SSL errors are treated as "dead".
    """
    if not domain or "." not in domain:
        return False, "invalid domain"

    domain = domain.strip().lower()
    urls = [f"https://{domain}", f"http://{domain}"]

    for url in urls:
        try:
            resp = _get_session().head(
                url,
                headers=HEADERS,
                timeout=timeout,
                allow_redirects=True,
            )
            # Some servers don't support HEAD; treat method-not-allowed as alive
            # because the domain clearly responds. For other non-405 errors try
            # a GET fallback before deciding.
            if resp.status_code == 405:
                try:
                    resp = _get_session().get(
                        url,
                        headers=HEADERS,
                        timeout=timeout,
                        allow_redirects=True,
                    )
                except Exception as e:
                    return False, f"{url} GET error: {e}"
            if resp.status_code < 500:
                return True, ""
            # 5xx: server is reachable but misbehaving; still counts as alive.
            return True, f"server error {resp.status_code}"
        except requests.exceptions.SSLError as e:
            # HTTPS certificate issue: server may still respond over HTTP.
            if url == urls[-1]:
                return False, f"SSL error: {e}"
            continue
        except requests.exceptions.ConnectionError as e:
            # Only keep the first URL's error if both fail.
            if url == urls[-1]:
                return False, f"connection error: {e}"
            continue
        except requests.exceptions.Timeout:
            if url == urls[-1]:
                return False, "timeout"
            continue
        except Exception as e:
            if url == urls[-1]:
                return False, str(e)
            continue

    return False, "unreachable"


def validate_emails_concurrently(
    zerobounce: ZeroBounceClient,
    leads: List[Lead],
    max_workers: int = 6,
) -> None:
    """Validate emails in parallel using ZeroBounce. Mutates leads in place."""
    if not zerobounce:
        print("[Verify] No ZeroBounce key configured; skipping email validation.")
        return

    total = len(leads)
    lock = threading.Lock()
    completed = 0

    def _validate_one(lead: Lead) -> None:
        nonlocal completed
        if not lead.email:
            with lock:
                completed += 1
                print(f"  [{completed}/{total}] (no email) skipped")
            return

        result = zerobounce.validate(lead.email)
        status = result.get("status", "unknown")
        lead.validation_status = status
        lead.email_valid = status == "valid"

        with lock:
            completed += 1
            print(f"  [{completed}/{total}] {lead.email} -> {status}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        executor.map(_validate_one, leads)


def check_company_status(lead: Lead, timeout: int = 8) -> None:
    """
    Lightweight heuristic to guess whether a company is still operating.
    Mutates lead.company_active and lead.company_status_notes in place.
    """
    if not lead.domain:
        return

    notes: List[str] = []

    # Signal A: homepage negative keywords.
    try:
        meta = fetch_domain_meta(lead.domain, timeout=timeout)
        homepage_text = " ".join([
            meta.get("title", ""),
            meta.get("description", ""),
            meta.get("h1", ""),
            meta.get("about_text", ""),
        ]).lower()
        for signal in _COMPANY_NEGATIVE_SIGNALS:
            if signal in homepage_text:
                notes.append(f"homepage: '{signal}'")
                break
    except Exception:
        pass

    # Signal B: Google Maps business_status (only relevant for Maps-sourced leads).
    if lead.source_type == "google_maps" and lead.place_id:
        # The Maps search already skips CLOSED_* places, but if a lead was
        # exported earlier we have no cached business_status. We leave it
        # optimistic here; a future Maps re-check could populate it.
        pass

    # Signal C: LinkedIn company page 404.
    if lead.linkedin_url and "/company/" in lead.linkedin_url.lower():
        try:
            resp = _get_session().head(lead.linkedin_url, headers=HEADERS, timeout=timeout, allow_redirects=True)
            if resp.status_code == 404:
                notes.append("linkedin company page 404")
        except Exception:
            pass

    if notes:
        lead.company_active = False
        lead.company_status_notes = "; ".join(notes)
    else:
        lead.company_active = True
        lead.company_status_notes = ""


def check_companies_status_concurrently(
    leads: List[Lead],
    max_workers: int = 6,
) -> None:
    """Run check_company_status in parallel."""
    total = len(leads)
    lock = threading.Lock()
    completed = 0

    def _check_one(lead: Lead) -> None:
        nonlocal completed
        check_company_status(lead)
        with lock:
            completed += 1
            if completed % 10 == 0 or completed == total:
                print(f"  [Company check] {completed}/{total}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        executor.map(_check_one, leads)


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
                resp = _get_session().post(self.base_url, headers=headers, json=body, timeout=30)
                if resp.status_code != 200:
                    print(f"  [GoogleMaps ERROR] HTTP {resp.status_code}: {resp.text[:200]}")
                    break
                data = resp.json()
            except Exception as e:
                print(f"  [GoogleMaps ERROR] {e}")
                break

            places = data.get("places", [])
            for place in places:
                # Skip permanently or temporarily closed businesses
                status = place.get("businessStatus", "")
                if status in ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"):
                    continue
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
                if types:
                    snippet_parts.append(", ".join(types[:3]))

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
                        "business_status": status,
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
        # Social / video / marketplaces
        "facebook.com", "linkedin.com", "twitter.com", "x.com", "instagram.com",
        "youtube.com", "pinterest.com", "reddit.com", "quora.com", "tiktok.com",
        # Reference / directories
        "wikipedia.org", "amazon.com", "ebay.com", "alibaba.com", "aliexpress.com",
        "etsy.com", "walmart.com", "target.com", "homedepot.com", "bestbuy.com",
        "costco.com", "wayfair.com", "macys.com",
        # Search engines
        "google.com", "bing.com", "baidu.com", "duckduckgo.com", "yahoo.com",
        # Business directories / review sites
        "yellowpages.com", "yelp.com", "tripadvisor.com", "bbb.org", "manta.com",
        "thomasnet.com", "kompass.com", "europages.com", "dnb.com", "hoovers.com",
        "owler.com", "craft.co", "indeed.com", "glassdoor.com",
        # B2B contact data platforms (often outrank official sites in search)
        "zoominfo.com", "crunchbase.com", "apollo.io", "leadiq.com", "rocketreach.co",
        "seamless.ai", "lusha.com", "contactout.com", "adapt.io", "signalhire.com",
        "hunter.io", "snov.io", "voilanorbert.com", "clearbit.com", "fullcontact.com",
        "discoverorg.com", "insideview.com", "datanyze.com", "b2bdata.com",
        "email-format.com", "emailmatcher.com", "toofr.com", "anymailfinder.com",
        "findthat.email", "skrapp.io", "getprospect.com", "salesql.com", "wiza.io",
        "kaspr.io", "useartemis.com", "clay.earth", "phantombuster.com",
        # News / blog platforms that may mention the company
        "nj.com", "medium.com", "substack.com", "bloomberg.com", "reuters.com",
        "forbes.com", "inc.com", "entrepreneur.com", "businessinsider.com",
        "fastcompany.com", "techcrunch.com", "theguardian.com", "nytimes.com",
        "washingtonpost.com", "cnn.com", "foxnews.com", "nbcnews.com", "cbsnews.com",
        "abcnews.go.com", "usatoday.com", "latimes.com", "chicagotribune.com",
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
    except Exception as e:
        # DDGS can hang/fail behind certain networks; fail fast.
        try:
            print(f"    [Website resolve] DDGS failed for '{clean_name}': {e}")
        except UnicodeEncodeError:
            safe_name = clean_name.encode("ascii", "replace").decode("ascii")
            print(f"    [Website resolve] DDGS failed for '{safe_name}': {e}")

    # Fallback: simple heuristic guesses with very short probes
    simple = re.sub(r'[^\w\-]', '', clean_name.lower().replace(' ', '').replace('&', 'and'))
    candidates = [
        f"{simple}.com",
        f"{simple}.co.uk",
        f"{simple}.net",
    ]
    for domain in candidates:
        try:
            resp = _get_session().head(f"https://{domain}", timeout=3, allow_redirects=True)
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
                resp = _get_session().get(self.base_url, params=params, timeout=30)
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
                err_msg = str(e)
                if "429" in err_msg or "Too Many Requests" in err_msg:
                    print(f"  [RATE_LIMIT] SerpAPI LinkedIn discovery rate limited (429). Skipping LinkedIn enrichment.")
                else:
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
        resp = _get_session().get(url, headers=HEADERS, timeout=20)
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
            resp = _get_session().head(f"https://{domain}", timeout=5, allow_redirects=True)
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
        # Priority: duckduckgo > browser > serpapi
        # SerpAPI is paid and rate-limits heavily; use it only when free engines are unavailable.
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
        domains: Optional[List[str]] = None,
        target_tlds: Optional[List[str]] = None,
        amazon: bool = False,
        maps_region: str = "",
        keep_no_email: bool = False,
        exclude_big_platforms: bool = False,
        advanced_syntax: bool = False,
        strict_mode: bool = False,
        scan_supplier_pages: bool = False,
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

        # Apply search-result-level relevance scoring
        if not domains:
            before = len(raw_results)
            scored_results = []
            for r in raw_results:
                title = r.get("title", "")
                snippet = r.get("snippet", "")
                sr_score = _score_search_result_relevance(title, snippet, keyword)
                if engine == "google_maps":
                    # Maps places rarely include "wholesale/distributor" in the name,
                    # but Google has already filtered by business category. Use a
                    # lenient threshold so legitimate sporting-goods stores pass.
                    sr_score += 25
                    threshold = -60 if strict_mode else -90
                else:
                    threshold = 5 if strict_mode else -40
                if sr_score >= threshold:
                    scored_results.append(r)
            raw_results = scored_results
            after = len(raw_results)
            if after < before:
                print(f"      Filtered out {before - after} low-relevance search results (title/snippet score)")

        print("PROGRESS: 20")

        # 1b. Amazon brand search (optional, skip when explicit domains are provided)
        amazon_domains: Dict[str, int] = {}
        if amazon and not domains:
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

        # 1c. LinkedIn profile discovery (SerpAPI only, skip in Maps mode and when explicit domains are provided)
        linkedin_domains: Dict[str, int] = {}
        if self.linkedin_discovery and not use_maps and not domains:
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
                b2b_types = {
                    "wholesale", "store", "supplier", "manufacturer", "factory",
                    "distributor", "importer", "equipment_supplier", "export_company",
                    "import_company", "trading_company", "exporter", "importer",
                    "buying_office", "procurement", "sourcing", "oem", "odm",
                    "private_label", "brand", "retail", "ecommerce", "online_store",
                    "marketplace_seller", "wholesaler", "dealer", "reseller",
                    "stockist", "agent", "sales", "supply_store", "sporting_goods_store",
                }
                if any(t in b2b_types for t in meta.get("types", [])):
                    score += 15
                # Bonus for high-quality Maps signals
                if meta.get("rating", 0) >= 4.5:
                    score += 10
                elif meta.get("rating", 0) >= 4.0:
                    score += 5
                if meta.get("reviews_count", 0) >= 20:
                    score += 5
                elif meta.get("reviews_count", 0) >= 10:
                    score += 2
            else:
                score = _score_search_result_relevance(title, snippet, keyword)
            # Keep the highest score for each domain
            if domain not in domain_scores or score > domain_scores[domain]:
                domain_scores[domain] = score
            if domain not in domain_source_type:
                domain_source_type[domain] = "search"
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

        if engine == "google_maps":
            # Google Maps already provides rich business metadata; skip the slow
            # website crawl and derive description/relevance directly from it.
            keyword_parts = set(re.findall(r"[a-zA-Z0-9]+", keyword.lower()))
            for domain in domains:
                meta = domain_maps_meta.get(domain, {})
                parts = []
                primary_type = meta.get("primary_type", "")
                address = meta.get("address", "")
                types = meta.get("types", [])
                if primary_type:
                    parts.append(primary_type)
                if address:
                    parts.append(address)
                if types:
                    parts.append(", ".join(types[:3]))
                domain_descriptions[domain] = " — ".join(parts)
                domain_linkedin_links[domain] = []

                score = 25
                b2b_types = {
                    "wholesale", "store", "supplier", "manufacturer", "factory",
                    "distributor", "importer", "equipment_supplier", "export_company",
                    "import_company", "trading_company", "exporter", "importer",
                    "buying_office", "procurement", "sourcing", "oem", "odm",
                    "private_label", "brand", "retail", "ecommerce", "online_store",
                    "marketplace_seller", "wholesaler", "dealer", "reseller",
                    "stockist", "agent", "sales", "supply_store", "sporting_goods_store",
                }
                if any(t in b2b_types for t in types):
                    score += 15
                if meta.get("rating", 0) >= 4.5:
                    score += 10
                elif meta.get("rating", 0) >= 4.0:
                    score += 5
                if meta.get("reviews_count", 0) >= 20:
                    score += 5
                # Keyword overlap with Maps business type/address gives extra relevance.
                text = " ".join(parts).lower()
                matched = sum(1 for part in keyword_parts if part in text and len(part) > 2)
                score += matched * 5
                domain_relevance[domain] = score
            fetched = sum(1 for d in domain_descriptions.values() if d)
            print(f"      Used Google Maps metadata for {fetched}/{len(domains)} domains")
            print(f"      Relevance: {sum(1 for r in domain_relevance.values() if r >= 20)} high")
        else:
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

        # 3c. Extract org structure / parent company signals from website text
        org_structure_map: Dict[str, dict] = {}
        for domain in domains:
            desc = domain_descriptions.get(domain, "")
            if desc:
                org_data = _extract_org_structure_from_text(desc)
                if org_data.get("org_structure_type"):
                    org_structure_map[domain] = org_data
        if org_structure_map:
            print(f"      Parent-company signals found on {len(org_structure_map)} domains")

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
                    org_data = org_structure_map.get(domain, {})
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
                        org_structure_type=org_data.get("org_structure_type", ""),
                        parent_company_name=org_data.get("parent_company_name", ""),
                        parent_company_country=org_data.get("parent_company_country", ""),
                        hq_country=org_data.get("parent_company_country", ""),
                        parent_org_data_source=org_data.get("source", ""),
                    )
                    lead.purchasing_authority, lead.purchasing_authority_reason = _classify_purchasing_authority(lead)
                    lead.tier, lead.tier_reason = _classify_tier(lead)
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
                org_data = org_structure_map.get(domain, {})
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
                    org_structure_type=org_data.get("org_structure_type", ""),
                    parent_company_name=org_data.get("parent_company_name", ""),
                    parent_company_country=org_data.get("parent_company_country", ""),
                    hq_country=org_data.get("parent_company_country", ""),
                    parent_org_data_source=org_data.get("source", ""),
                )
                lead.purchasing_authority, lead.purchasing_authority_reason = _classify_purchasing_authority(lead)
                lead.tier, lead.tier_reason = _classify_tier(lead)
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

        # 6b. Optional supplier/procurement portal enrichment
        if scan_supplier_pages:
            unique_leads = self._enrich_leads_with_supplier_portal(unique_leads)

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
            "supplier_page_url", "supplier_page_title", "supplier_email",
            "supplier_form_link", "supplier_notes",
            "domain_alive", "domain_check_error", "email_valid",
            "company_active", "company_status_notes", "last_verified_at",
            # Parent company / purchasing authority
            "org_structure_type", "parent_company_name", "parent_company_country",
            "hq_country", "purchasing_authority", "purchasing_authority_reason",
            "parent_org_data_source",
            # Customer value tier
            "tier", "tier_reason",
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
            print(f"  No-email leads   : {no_email} (kept without email)")
        print(f"  Personal emails  : {personal}")
        print(f"  High confidence  : {high_conf}")
        if validated:
            print(f"  Validated (OK)   : {validated}")
        with_parent = sum(1 for l in leads if l.parent_company_name)
        hq_auth = sum(1 for l in leads if l.purchasing_authority == "headquarter")
        local_auth = sum(1 for l in leads if l.purchasing_authority == "local")
        unknown_auth = sum(1 for l in leads if l.purchasing_authority == "unknown")
        if with_parent:
            print(f"  With parent/HQ   : {with_parent}")
        if hq_auth or local_auth:
            print(f"  Purchasing auth  : {hq_auth} HQ, {local_auth} local, {unknown_auth} unknown")
        tier_a = sum(1 for l in leads if l.tier == "A")
        tier_b = sum(1 for l in leads if l.tier == "B")
        tier_c = sum(1 for l in leads if l.tier == "C")
        if tier_a or tier_b or tier_c:
            print(f"  Customer tiers   : A={tier_a}, B={tier_b}, C={tier_c}")
        print(f"{'='*60}\n")

    def _enrich_leads_with_supplier_portal(
        self,
        leads: List[Lead],
        max_workers: int = 6,
        timeout: int = 6,
    ) -> List[Lead]:
        """Scan each unique domain in leads for procurement/supplier pages.

        The scan is optional and runs after the main search pipeline. Results are
        written back into the supplier_* fields of each lead sharing the domain.
        """
        if not leads:
            return leads

        domains = list({lead.domain for lead in leads if lead.domain})
        if not domains:
            return leads

        print(f"\n[Supplier Portal] Scanning {len(domains)} domains for supplier/procurement pages...")
        print("PROGRESS: 92")

        portal_map: Dict[str, dict] = {}
        completed = 0
        total = len(domains)

        def _scan_one(domain: str) -> tuple:
            portal = _scan_supplier_pages(domain, timeout=timeout)
            return domain, portal

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_domain = {executor.submit(_scan_one, d): d for d in domains}
            for future in concurrent.futures.as_completed(future_to_domain):
                completed += 1
                pct = 92 + int((completed / total) * 7)
                print(f"PROGRESS: {min(pct, 99)}")
                try:
                    domain, portal = future.result()
                    if portal:
                        portal_map[domain] = portal
                except Exception as e:
                    print(f"    [Supplier Portal ERROR] {future_to_domain[future]}: {e}")

        enriched = 0
        for lead in leads:
            portal = portal_map.get(lead.domain)
            if not portal:
                continue
            lead.supplier_page_url = portal.get("url", "")
            lead.supplier_page_title = portal.get("title", "")
            lead.supplier_email = portal.get("email", "")
            lead.supplier_form_link = portal.get("form_link", "")
            # Extract a short summary from the page content
            if not lead.supplier_notes:
                try:
                    resp = _get_session().get(portal["url"], headers=HEADERS, timeout=timeout, allow_redirects=True)
                    lead.supplier_notes = _extract_supplier_notes(resp.text)
                except Exception:
                    lead.supplier_notes = ""
            enriched += 1

        print(f"  Enriched {enriched} leads with supplier portal data")
        return leads

    def run_apollo_search(
        self,
        organization_keywords: Optional[List[str]] = None,
        person_titles: Optional[List[str]] = None,
        person_locations: Optional[List[str]] = None,
        max_results: int = 100,
        output: str = "leads.csv",
        keep_no_email: bool = False,
        employee_range: Optional[List[str]] = None,
        organization_domains: Optional[List[str]] = None,
        country: Optional[str] = None,
        state: Optional[str] = None,
        city: Optional[str] = None,
        zip_code: Optional[str] = None,
        scan_supplier_pages: bool = False,
        min_relevance: int = 0,
        strict_mode: bool = False,
        max_enrich: int = 50,
        no_enrich: bool = False,
        source_type: str = "apollo",
        prefetched_people: Optional[List[dict]] = None,
    ) -> List[Lead]:
        """Run an Apollo.io People Search and export leads.

        Supports keyword discovery or domain-based expansion.
        Optimized with parallel domain resolution and Hunter enrichment.
        """
        if not self.apollo:
            print("[ERROR] Apollo client not initialized. Please set APOLLO_KEY in config.")
            return []

        if strict_mode:
            min_relevance = max(min_relevance, 10)
            # Tighten titles to purchasing roles only
            purchasing_titles = {"Buyer", "Purchasing Manager", "Procurement Manager", "Sourcing Manager"}
            if person_titles:
                person_titles = [t for t in person_titles if t in purchasing_titles] or list(purchasing_titles)
            else:
                person_titles = list(purchasing_titles)

        timestamp = datetime.now().isoformat()
        mode_label = "Domain Expansion" if organization_domains else "Keyword Search"
        mode_value = organization_domains or organization_keywords or []
        location_summary = ", ".join(filter(None, [country, state, city, zip_code])) or (person_locations or [])

        print(f"\n{'='*60}")
        print(f"  Apollo.io People Search - {mode_label}")
        print(f"  Input          : {mode_value}")
        print(f"  Titles         : {person_titles}")
        print(f"  Locations      : {location_summary}")
        print(f"  Employee Range : {employee_range or 'Any'}")
        print(f"  Min Relevance  : {min_relevance}{' (strict)' if strict_mode else ''}")
        print(f"  Max Results    : {max_results}")
        print(f"  Max Enrich     : {max_enrich}{' (skipped)' if no_enrich else ''}")
        print(f"  Output         : {output}")
        print(f"{'='*60}\n")

        per_page = min(max_results, 100)
        page = 1
        print("PROGRESS: 5")

        # -----------------------------------------------------------------------
        # Stage 1: Fetch all people from Apollo (may span multiple pages)
        # If prefetched_people is provided (e.g. from Organization Search), skip
        # the API call and use those contacts directly.
        # -----------------------------------------------------------------------
        all_people: List[dict] = []
        if prefetched_people is not None:
            all_people = prefetched_people
            print(f"[Apollo] Using {len(all_people)} prefetched contacts from organization search")
        else:
            while len(all_people) < max_results:
                print(f"[Apollo] Fetching page {page}...")
                people = self.apollo.people_search(
                    organization_keywords=organization_keywords,
                    person_titles=person_titles or None,
                    person_locations=person_locations or None,
                    organization_num_employees=employee_range or None,
                    organization_domains=organization_domains or None,
                    country=country or None,
                    state=state or None,
                    city=city or None,
                    zip_code=zip_code or None,
                    per_page=per_page,
                    page=page,
                    enrich=False,
                )
                if not people:
                    print("  No more results.")
                    break
                all_people.extend(people)
                print(f"  Page {page}: got {len(people)} contacts (total {len(all_people)})")
                if len(people) < per_page:
                    break
                page += 1
                time.sleep(0.2)

        print(f"\n[Apollo] Total contacts fetched: {len(all_people)}")
        if not all_people:
            print("[WARNING] No contacts returned by Apollo.")
            return []
        print("PROGRESS: 15")

        # -----------------------------------------------------------------------
        # Stage 1.5: Coarse pre-filter + bulk /people/bulk_match enrichment
        # Only enrich contacts that are missing an email; skip the rest to save
        # Apollo reveal credits. Bulk match handles up to 10 people per call.
        # -----------------------------------------------------------------------
        def _coarse_keep(person: dict) -> bool:
            org = person.get("organization", {}) or {}
            org_name = (org.get("name") or person.get("organization_name") or "").lower()
            org_website = org.get("website_url") or person.get("organization_website") or ""
            title = (person.get("title") or person.get("position") or "").lower()

            # Purchasing titles are strong B2B signals regardless of org name.
            if any(t in title for t in ["buyer", "purchasing", "procurement", "sourcing", "merchandising"]):
                return True

            # Exact user keyword match keeps the contact for enrichment.
            if organization_keywords:
                for kw in organization_keywords:
                    if kw.lower() in org_name:
                        return True

            # Without a user keyword match, only keep orgs that look like B2B channels
            # or large sporting goods chains. Do NOT keep a contact just because its
            # name contains a generic product word like 'ski' (ski resorts, rental shops).
            b2b_roles = {
                "distributor", "distributors", "distributing", "distribution",
                "importer", "importers", "importing", "import", "imports",
                "exporter", "exporters", "exporting", "export", "exports",
                "wholesaler", "wholesalers", "wholesale", "wholesaling",
                "supplier", "suppliers", "supplying", "supply", "supplies",
                "dealer", "dealers", "dealing",
                "reseller", "resellers", "reselling",
                "vendor", "vendors",
                "manufacturer", "manufacturers", "manufacturing", "manufacture",
                "oem", "odm",
                "trading", "trade", "trader", "traders",
                "merchant", "merchants",
            }
            if any(role in org_name for role in b2b_roles):
                return True

            # Keep well-known multi-location sporting goods chains.
            if any(sig in org_name for sig in {
                "sporting goods", "sports goods", "sports authority", "dick's",
                "dicks sporting", "academy sports", "big 5", "modell's", "modells",
                "olympia sport", "sports direct", "decathlon", "rei ", "mec ",
                "mountain equipment co-op",
            }):
                return True

            # Having a website alone is not enough for coarse keep; otherwise
            # irrelevant resorts and rental shops consume enrichment credits.
            return False

        if no_enrich:
            people_to_enrich = []
            print("\n[Apollo] Skipping /people/match enrichment (--apollo-no-enrich)")
        else:
            people_to_enrich = [p for p in all_people if _coarse_keep(p) and not p.get("email")][:max_enrich]
            skipped_coarse = len(all_people) - len(people_to_enrich)
            already_with_email = sum(1 for p in all_people if _coarse_keep(p) and p.get("email"))
            print(f"\n[Apollo] Coarse pre-filter: {len(people_to_enrich)} to enrich (max {max_enrich}), {skipped_coarse} skipped")
            if already_with_email:
                print(f"    [Apollo] {already_with_email} contacts already have a free email and will not be enriched")

        if people_to_enrich:
            enriched_count = 0
            print("  [Apollo] Starting bulk /people/bulk_match enrichment...")
            enriched_list = self.apollo._bulk_enrich_people(people_to_enrich)
            enriched_by_id = {p.get("id"): p for p in enriched_list if p.get("id")}
            for original in people_to_enrich:
                ep = enriched_by_id.get(original.get("id"))
                if not ep:
                    continue
                if ep.get("email"):
                    original["email"] = ep["email"]
                    enriched_count += 1
                if ep.get("last_name") and not original.get("last_name"):
                    original["last_name"] = ep["last_name"]
                if ep.get("_enriched_org") and not original.get("_enriched_org"):
                    original["_enriched_org"] = ep["_enriched_org"]
                if ep.get("_parent_org_name") and not original.get("_parent_org_name"):
                    original["_parent_org_name"] = ep["_parent_org_name"]
                if ep.get("_hq_country") and not original.get("_hq_country"):
                    original["_hq_country"] = ep["_hq_country"]
                if ep.get("_org_structure_type") and not original.get("_org_structure_type"):
                    original["_org_structure_type"] = ep["_org_structure_type"]
                if ep.get("organization_website") and not original.get("organization_website"):
                    original["organization_website"] = ep["organization_website"]
            print(f"  [Apollo] Bulk enriched {enriched_count}/{len(people_to_enrich)} contacts")
        print("PROGRESS: 25")

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

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            future_to_person = {executor.submit(_resolve_domain_for_person, p): p for p in all_people}
            completed = 0
            total = len(all_people)
            for future in concurrent.futures.as_completed(future_to_person):
                completed += 1
                if completed % max(1, total // 5) == 0 or completed == total:
                    pct = 25 + int((completed / total) * 25)
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
        print(f"\n[Stage 2.5/4] Scoring relevance for {len(people_with_domains)} contacts (min={min_relevance})...")
        scored_people: List[tuple] = []
        filtered_out = 0
        for person, domain in people_with_domains:
            score = _score_apollo_contact_relevance(person)
            has_org_data = bool(person.get("_enriched_org"))
            org_name = (person.get("organization_name") or "").lower()

            # Boost score when the company name directly contains the user's keyword.
            # This compensates for missing Apollo enriched org data on small retailers.
            if organization_keywords:
                for kw in organization_keywords:
                    if kw.lower() in org_name:
                        score += 15
                        break

            if not has_org_data:
                # Apollo did not return enriched org data. Infer relevance from the
                # company name so that obviously relevant names (e.g. "Bobs Sporting Goods")
                # are not discarded just because enrichment failed.
                if organization_keywords and _apollo_has_positive_signal(person, organization_keywords):
                    score = max(score, 0)
                elif any(pos in org_name for pos in APOLLO_PRODUCT_KEYWORDS):
                    score = max(score, 0)
                else:
                    score = min(score, -20)
            # For keyword discovery, require at least one positive signal
            # (user keyword, B2B keyword, or B2B industry) unless the user
            # explicitly lowered the relevance threshold below 0.
            # Channel-role-only matches are kept with a low score instead of
            # being hard-filtered, so relevant distributors/wholesalers whose
            # company name doesn't include the exact product term still appear.
            if organization_keywords and min_relevance >= 0 and not _apollo_has_positive_signal(person, organization_keywords):
                score = -25
            elif organization_keywords and min_relevance >= 0:
                # Positive signal present but may only be a weak channel role.
                # Clamp weak/negative scores up to 0 so they survive the default
                # threshold without pushing irrelevant results to the top.
                if score < 0:
                    score = 0
            if score >= min_relevance:
                scored_people.append((person, domain, score))
            else:
                filtered_out += 1
                if filtered_out <= 5:
                    filtered_org_name = person.get("organization_name", "")
                    try:
                        print(f"    [Filter] -'{filtered_org_name}' (score {score})")
                    except UnicodeEncodeError:
                        safe_name = filtered_org_name.encode("ascii", "replace").decode("ascii")
                        print(f"    [Filter] -'{safe_name}' (score {score})")
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

        # Contacts with full name -> Hunter email-finder
        email_finder_targets = [
            (person, domain) for person, domain in people_with_domains
            if not person.get("value") and person.get("first_name") and person.get("last_name")
        ]

        # Contacts without full name -> Hunter domain-search fallback (unique domains)
        domain_search_domains: List[str] = []
        domain_search_seen: Set[str] = set()
        for person, domain in people_with_domains:
            if person.get("value"):
                continue
            if person.get("first_name") and person.get("last_name"):
                continue  # handled by email-finder
            if domain and domain not in domain_search_seen:
                domain_search_seen.add(domain)
                domain_search_domains.append(domain)

        hunter_results: dict = {}          # id(person) -> (email, score)
        domain_search_results: dict = {}   # domain -> list of email dicts

        def _pick_best_hunter_email(emails: List[dict], first_name: str = "") -> Optional[dict]:
            """Pick the best email from Hunter domain_search results."""
            if not emails:
                return None
            # Drop generic department emails when possible
            candidates = [e for e in emails if not is_generic_email(e.get("value", ""))]
            if not candidates:
                candidates = list(emails)
            # Prefer personal type
            personal = [e for e in candidates if e.get("type") == "personal"]
            if personal:
                candidates = personal
            # If we have a first name, prefer prefix that contains it
            if first_name:
                fn_lower = first_name.lower().strip()
                matching = [
                    e for e in candidates
                    if fn_lower in e.get("value", "").split("@")[0].lower()
                ]
                if matching:
                    candidates = matching
            # Highest confidence first
            candidates.sort(key=lambda e: e.get("confidence", 0), reverse=True)
            return candidates[0]

        if self.hunter and (email_finder_targets or domain_search_domains):
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

            def _domain_search_one(domain: str) -> tuple:
                try:
                    emails = self.hunter.domain_search(domain, limit=10)
                    return (domain, emails)
                except Exception:
                    return (domain, [])

            total_tasks = len(email_finder_targets) + len(domain_search_domains)
            completed = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                future_to_item = {}
                for item in email_finder_targets:
                    future = executor.submit(_enrich_one, item)
                    future_to_item[future] = ("email_finder", item)
                for domain in domain_search_domains:
                    future = executor.submit(_domain_search_one, domain)
                    future_to_item[future] = ("domain_search", domain)

                for future in concurrent.futures.as_completed(future_to_item):
                    completed += 1
                    if completed % max(1, total_tasks // 4) == 0 or completed == total_tasks:
                        pct = 40 + int((completed / total_tasks) * 30)
                        print(f"PROGRESS: {min(pct, 70)}")

                    task_type, item = future_to_item[future]
                    if task_type == "email_finder":
                        pid, email, score = future.result()
                        hunter_results[pid] = (email, score)
                    else:
                        domain, emails = future.result()
                        domain_search_results[domain] = emails

            # Apply domain-search fallback to contacts that couldn't use email-finder
            domain_enriched_count = 0
            for person, domain in people_with_domains:
                if person.get("value"):
                    continue
                if id(person) in hunter_results and hunter_results[id(person)][0]:
                    continue  # already enriched via email-finder
                emails = domain_search_results.get(domain, [])
                best = _pick_best_hunter_email(emails, person.get("first_name", ""))
                if best and best.get("value"):
                    hunter_results[id(person)] = (best["value"], best.get("confidence", 50))
                    domain_enriched_count += 1

            email_finder_enriched = sum(
                1 for pid, (e, _) in hunter_results.items()
                if e and any(pid == id(p) for p, _ in email_finder_targets)
            )
            domains_with_emails = sum(1 for emails in domain_search_results.values() if emails)
            print(f"  Hunter email-finder enriched: {email_finder_enriched}/{len(email_finder_targets)}")
            print(f"  Hunter domain-search fallback: {domains_with_emails}/{len(domain_search_domains)} domains ({domain_enriched_count} contacts)")
        else:
            print("  No enrichment needed or Hunter not configured.")
        print("PROGRESS: 75")

        # -----------------------------------------------------------------------
        # Stage 4: Build Lead objects
        # -----------------------------------------------------------------------
        print(f"\n[Stage 4/4] Building leads...")
        all_leads: List[Lead] = []
        skipped_no_contact = 0
        apollo_has_email_but_empty = 0
        for person, domain in people_with_domains:
            email = (person.get("value") or "").lower().strip()
            has_email_flag = person.get("has_email", False)
            # Apollo may return strings like "Yes" or "Maybe: please request..."
            # Only treat a confirmed "Yes" as a real direct phone signal.
            raw_phone = person.get("has_direct_phone", False)
            if isinstance(raw_phone, bool):
                has_direct_phone = raw_phone
            elif isinstance(raw_phone, str):
                has_direct_phone = raw_phone.strip().lower() == "yes"
            else:
                has_direct_phone = bool(raw_phone)
            first_name = person.get("first_name", "")
            last_name = person.get("last_name", "")
            org_name = person.get("organization_name", "")
            enriched_org = person.get("_enriched_org", {}) or {}
            website_description = enriched_org.get("short_description") or ""
            if website_description:
                # Keep description concise: first 250 chars, trimmed at sentence boundary
                website_description = website_description.strip()
                if len(website_description) > 250:
                    truncated = website_description[:250]
                    last_period = truncated.rfind('.')
                    last_break = truncated.rfind('\n')
                    cut_at = max(last_period, last_break)
                    if cut_at > 150:
                        website_description = truncated[:cut_at + 1]
                    else:
                        website_description = truncated.rstrip() + '...'

            # Extract parent company / org structure from Apollo
            parent_name = person.get("_parent_org_name", "")
            hq_country = person.get("_hq_country", "")
            org_structure = person.get("_org_structure_type", "")
            parent_source = "apollo" if parent_name else ""

            # Apply Hunter enrichment
            hunter_confidence = 0
            if not email and id(person) in hunter_results:
                enriched_email, hunter_confidence = hunter_results[id(person)]
                if enriched_email:
                    email = enriched_email.lower().strip()

            # Track Apollo contacts where the API claims an email exists but
            # does not expose the actual address.
            if has_email_flag and not email:
                apollo_has_email_but_empty += 1

            # Skip contacts without an exposed email unless the user explicitly
            # asked to keep no-email leads. A direct phone alone is not enough
            # to keep a contact when keep_no_email is False; the checkbox
            # "保留无邮箱客户" must be checked for phone-only leads to be retained.
            if not email and not keep_no_email:
                skipped_no_contact += 1
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
                search_keyword=" | ".join(organization_keywords or organization_domains or []),
                found_at=timestamp,
                country=detect_country(domain, email),
                website_description=website_description,
                relevance_score=person.get("_relevance_score", 0),
                linkedin_url=person.get("linkedin_url", ""),
                source_type=source_type,
                has_direct_phone=has_direct_phone,
                # Parent company / purchasing authority from Apollo
                org_structure_type=org_structure,
                parent_company_name=parent_name,
                parent_company_country=hq_country,
                hq_country=hq_country,
                parent_org_data_source=parent_source,
            )
            lead.purchasing_authority, lead.purchasing_authority_reason = _classify_purchasing_authority(lead)
            lead.tier, lead.tier_reason = _classify_tier(lead)
            all_leads.append(lead)

        if apollo_has_email_but_empty:
            print(f"  [Apollo] {apollo_has_email_but_empty} contacts had has_email=True but no exposed email address")
        if skipped_no_contact:
            print(f"  [Apollo] Skipped {skipped_no_contact} contacts with no exposed email (keep_no_email=False)")

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

        # Optional supplier/procurement portal enrichment
        if scan_supplier_pages:
            unique_leads = self._enrich_leads_with_supplier_portal(unique_leads)

        self._export_csv(unique_leads, output)
        print("PROGRESS: 100")
        self._print_summary(unique_leads, "Apollo People Search", len(set(l.domain for l in unique_leads)))
        return unique_leads

    def run_apollo_organization_search(
        self,
        org_keywords: Optional[List[str]] = None,
        org_locations: Optional[List[str]] = None,
        employee_range: Optional[List[str]] = None,
        country: Optional[str] = None,
        state: Optional[str] = None,
        city: Optional[str] = None,
        zip_code: Optional[str] = None,
        person_titles: Optional[List[str]] = None,
        max_orgs: int = 50,
        max_people_per_org: int = 5,
        output: str = "leads.csv",
        keep_no_email: bool = False,
        min_relevance: int = 0,
        strict_mode: bool = False,
        max_enrich: int = 50,
        no_enrich: bool = False,
        scan_supplier_pages: bool = False,
    ) -> List[Lead]:
        """Run Apollo Organization Search: find companies, filter them, then drill into each for purchasing contacts."""
        if not self.apollo:
            print("[ERROR] Apollo client not initialized. Please set APOLLO_KEY in config.")
            return []

        if strict_mode:
            min_relevance = max(min_relevance, 10)
            purchasing_titles = {"Buyer", "Purchasing Manager", "Procurement Manager", "Sourcing Manager"}
            if person_titles:
                person_titles = [t for t in person_titles if t in purchasing_titles] or list(purchasing_titles)
            else:
                person_titles = list(purchasing_titles)

        timestamp = datetime.now().isoformat()
        location_summary = ", ".join(filter(None, [country, state, city, zip_code])) or (org_locations or [])

        print(f"\n{'='*60}")
        print(f"  Apollo.io Organization Search")
        print(f"  Input          : {org_keywords or []}")
        print(f"  Locations      : {location_summary}")
        print(f"  Employee Range : {employee_range or 'Any'}")
        print(f"  Min Relevance  : {min_relevance}{' (strict)' if strict_mode else ''}")
        print(f"  Max Orgs       : {max_orgs}")
        print(f"  People/Org     : {max_people_per_org}")
        print(f"  Max Enrich     : {max_enrich}{' (skipped)' if no_enrich else ''}")
        print(f"  Output         : {output}")
        print(f"{'='*60}\n")
        print("PROGRESS: 5")

        # -----------------------------------------------------------------------
        # Stage 1: Fetch organizations
        # -----------------------------------------------------------------------
        all_orgs: List[dict] = []
        per_page = min(max_orgs, 100)
        page = 1
        while len(all_orgs) < max_orgs:
            print(f"[Apollo] Fetching organization page {page}...")
            orgs = self.apollo.search_organizations(
                keyword_tags=org_keywords,
                locations=org_locations or None,
                organization_num_employees=employee_range or None,
                country=country or None,
                state=state or None,
                city=city or None,
                zip_code=zip_code or None,
                per_page=per_page,
                page=page,
            )
            if not orgs:
                print("  No more results.")
                break
            all_orgs.extend(orgs)
            print(f"  Page {page}: got {len(orgs)} organizations (total {len(all_orgs)})")
            if len(orgs) < per_page:
                break
            page += 1
            time.sleep(0.2)

        print(f"\n[Apollo] Total organizations fetched: {len(all_orgs)}")
        if not all_orgs:
            print("[WARNING] No organizations returned by Apollo.")
            return []
        print("PROGRESS: 15")

        # -----------------------------------------------------------------------
        # Stage 2: Filter/score organizations
        # -----------------------------------------------------------------------
        kept_orgs: List[dict] = []
        filtered_out = 0
        for org in all_orgs:
            score = _score_apollo_organization_relevance(org)
            org["_relevance_score"] = score
            if score >= min_relevance:
                kept_orgs.append(org)
            else:
                filtered_out += 1
                if filtered_out <= 5:
                    filtered_org_name = org.get("name", "")
                    try:
                        print(f"    [Filter] -'{filtered_org_name}' (score {score})")
                    except UnicodeEncodeError:
                        safe_name = filtered_org_name.encode("ascii", "replace").decode("ascii")
                        print(f"    [Filter] -'{safe_name}' (score {score})")

        print(f"\n[Apollo] Organizations kept after filtering: {len(kept_orgs)} (filtered {filtered_out})")
        if not kept_orgs:
            print("[WARNING] All organizations filtered out as irrelevant.")
            return []
        print("PROGRESS: 25")

        # -----------------------------------------------------------------------
        # Stage 3: Search people within each kept organization
        # -----------------------------------------------------------------------
        prefetched_people: List[dict] = []
        total_people_quota = max_orgs * max_people_per_org
        for idx, org in enumerate(kept_orgs, 1):
            if len(prefetched_people) >= total_people_quota:
                break
            domain = org.get("domain", "")
            if not domain and org.get("website_url"):
                domain = extract_domain(org["website_url"])
            if not domain:
                print(f"    [Apollo] Skipping org '{org.get('name', '')}' - no domain")
                continue

            print(f"    [Apollo] [{idx}/{len(kept_orgs)}] Searching people in '{org.get('name', '')}'...")
            people = self.apollo.people_search(
                organization_domains=[domain],
                person_titles=person_titles or None,
                person_locations=org_locations or None,
                organization_num_employees=employee_range or None,
                country=country or None,
                state=state or None,
                city=city or None,
                zip_code=zip_code or None,
                per_page=max_people_per_org,
                page=1,
                enrich=False,
            )
            if people:
                for p in people:
                    p["_apollo_org"] = org
                    p["_organization_name"] = org.get("name", "")
                    p["_organization_website"] = org.get("website_url", "")
                    # Patch organization_name/website for downstream consumers if missing
                    if not p.get("organization_name"):
                        p["organization_name"] = org.get("name", "")
                    if not p.get("organization_website"):
                        p["organization_website"] = org.get("website_url", "")
                prefetched_people.extend(people)
                print(f"      Found {len(people)} contacts")
            time.sleep(0.2)

        print(f"\n[Apollo] Total contacts fetched from organizations: {len(prefetched_people)}")
        if not prefetched_people:
            print("[WARNING] No contacts found in kept organizations.")
            return []
        print("PROGRESS: 40")

        # -----------------------------------------------------------------------
        # Stage 4: Hand off to the shared Apollo people pipeline
        # -----------------------------------------------------------------------
        return self.run_apollo_search(
            organization_keywords=org_keywords,
            person_titles=person_titles,
            person_locations=org_locations,
            max_results=total_people_quota,
            output=output,
            keep_no_email=keep_no_email,
            employee_range=employee_range,
            organization_domains=[org.get("domain", "") for org in kept_orgs if org.get("domain")],
            country=country,
            state=state,
            city=city,
            zip_code=zip_code,
            scan_supplier_pages=scan_supplier_pages,
            min_relevance=min_relevance,
            strict_mode=strict_mode,
            max_enrich=max_enrich,
            no_enrich=no_enrich,
            source_type="apollo_org",
            prefetched_people=prefetched_people,
        )

    def run_supplier_portal_scan(
        self,
        domains: List[str],
        output: str = "supplier_portals.csv",
        max_workers: int = 8,
    ) -> List[Lead]:
        """Scan a list of domains for procurement/supplier portal pages."""
        timestamp = datetime.now().isoformat()
        print(f"\n{'='*60}")
        print(f"  Supplier Portal Scan")
        print(f"  Domains: {len(domains)}")
        print(f"  Output : {output}")
        print(f"{'='*60}\n")
        print("PROGRESS: 5")

        results: List[Lead] = []
        completed = 0
        total = len(domains)

        def _scan_one(domain: str) -> Optional[Lead]:
            domain = domain.strip().lower()
            if not domain or "." not in domain:
                return None
            portal = _scan_supplier_pages(domain)
            if not portal:
                return None
            # Extract a short note from the page content
            notes = ""
            try:
                resp = _get_session().get(portal["url"], headers=HEADERS, timeout=8)
                notes = _extract_supplier_notes(resp.text)
            except Exception:
                pass
            lead = Lead(
                domain=domain,
                company=domain,
                search_keyword="supplier-portal-scan",
                found_at=timestamp,
                source_type="supplier_portal",
                supplier_page_url=portal["url"],
                supplier_page_title=portal["title"],
                supplier_email=portal["email"],
                supplier_form_link=portal["form_link"],
                supplier_notes=notes,
            )
            lead.tier, lead.tier_reason = _classify_tier(lead)
            return lead

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_domain = {executor.submit(_scan_one, d): d for d in domains}
            for future in concurrent.futures.as_completed(future_to_domain):
                completed += 1
                pct = 5 + int((completed / total) * 90)
                print(f"PROGRESS: {pct}")
                lead = future.result()
                if lead:
                    results.append(lead)

        print(f"\n[Supplier Portal] Found {len(results)} portals / {total} domains")
        print("PROGRESS: 100")
        self._export_csv(results, output)
        return results

    def run_verification(
        self,
        csv_path: str,
        check_domain: bool = False,
        check_email: bool = False,
        check_company: bool = False,
        output: str = "",
        max_workers: int = 8,
    ) -> List[Lead]:
        """Batch-verify existing leads from a CSV file.

        Updates domain_alive, email_valid, company_active and last_verified_at,
        then re-exports the CSV.
        """
        timestamp = datetime.now().isoformat()
        output_path = output or csv_path

        print(f"\n{'='*60}")
        print(f"  Lead Verification / Maintenance")
        print(f"  Input  : {csv_path}")
        print(f"  Output : {output_path}")
        print(f"{'='*60}\n")

        leads = _leads_from_csv(csv_path)
        if not leads:
            print("[WARNING] No leads loaded. Nothing to verify.")
            return []

        print(f"Loaded {len(leads)} leads")
        with_email = sum(1 for l in leads if l.email)
        unique_domains = sorted({l.domain for l in leads if l.domain})
        print(f"  with email: {with_email}, unique domains: {len(unique_domains)}")

        # 1. Domain alive check
        if check_domain:
            print(f"\n[Verify] Checking {len(unique_domains)} unique domains...")
            domain_results: Dict[str, Tuple[bool, str]] = {}
            lock = threading.Lock()
            completed = 0

            def _check_domain(domain: str) -> None:
                nonlocal completed
                alive, error = check_domain_alive(domain, timeout=10)
                domain_results[domain] = (alive, error)
                with lock:
                    completed += 1
                    if completed % 5 == 0 or completed == len(unique_domains):
                        print(f"  [Domain] {completed}/{len(unique_domains)} checked")

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                executor.map(_check_domain, unique_domains)

            for lead in leads:
                if lead.domain in domain_results:
                    alive, error = domain_results[lead.domain]
                    lead.domain_alive = alive
                    lead.domain_check_error = error

            alive_count = sum(1 for a, _ in domain_results.values() if a)
            print(f"  Domain alive: {alive_count}/{len(unique_domains)}")

        # 2. Company status heuristic
        if check_company:
            print("\n[Verify] Checking company status...")
            check_companies_status_concurrently(leads, max_workers=max_workers)
            active_count = sum(1 for l in leads if l.company_active)
            print(f"  Company active: {active_count}/{len(leads)}")

        # Update verification timestamp
        for lead in leads:
            lead.last_verified_at = timestamp

        self._export_csv(leads, output_path)
        print(f"\n[OK] Verified {len(leads)} leads exported to: {output_path}")

        # Summary
        print(f"\n{'='*60}")
        print(f"  VERIFICATION SUMMARY")
        print(f"{'='*60}")
        print(f"  Leads processed : {len(leads)}")
        if check_domain:
            alive = sum(1 for l in leads if l.domain_alive)
            dead = len(leads) - alive
            print(f"  Domains alive   : {alive}  |  dead: {dead}")
        if check_company:
            active = sum(1 for l in leads if l.company_active)
            inactive = len(leads) - active
            print(f"  Companies active: {active}  |  inactive: {inactive}")
        print(f"{'='*60}\n")

        return leads


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
    parser.add_argument(
        "--apollo-domains",
        type=str,
        default="",
        help="Apollo.io organization domains for expansion, comma-separated (e.g. 'example.com,sports.com')",
    )
    parser.add_argument(
        "--apollo-country",
        type=str,
        default="",
        help="Apollo.io country filter, e.g. 'United States'",
    )
    parser.add_argument(
        "--apollo-state",
        type=str,
        default="",
        help="Apollo.io state/province filter, e.g. 'California'",
    )
    parser.add_argument(
        "--apollo-city",
        type=str,
        default="",
        help="Apollo.io city filter, e.g. 'Los Angeles'",
    )
    parser.add_argument(
        "--apollo-zip",
        type=str,
        default="",
        help="Apollo.io zip/postal code filter, e.g. '90210'",
    )
    parser.add_argument(
        "--apollo-min-relevance",
        type=int,
        default=0,
        help="Minimum Apollo contact relevance score to keep (default: 0)",
    )
    parser.add_argument(
        "--apollo-strict-mode",
        action="store_true",
        help="Strict Apollo filtering: raises min relevance to 10 and limits titles to purchasing roles",
    )
    parser.add_argument(
        "--apollo-max-enrich",
        type=int,
        default=50,
        help="Maximum Apollo contacts to enrich via /people/match per search (default: 50)",
    )
    parser.add_argument(
        "--apollo-no-enrich",
        action="store_true",
        help="Skip Apollo /people/match enrichment; only use emails already present in search results",
    )
    parser.add_argument(
        "--apollo-org-keywords",
        type=str,
        default="",
        help="Apollo.io organization search keywords, comma-separated (enables Apollo Organization Search mode)",
    )
    parser.add_argument(
        "--apollo-org-locations",
        type=str,
        default="",
        help="Apollo.io organization search locations, comma-separated, e.g. 'United States,Germany'",
    )
    parser.add_argument(
        "--apollo-org-employee-range",
        type=str,
        default="",
        help="Apollo.io organization search company size range, e.g. '2,50' or '51,200'",
    )
    parser.add_argument(
        "--apollo-org-country",
        type=str,
        default="",
        help="Apollo.io organization search country filter, e.g. 'United States'",
    )
    parser.add_argument(
        "--apollo-org-state",
        type=str,
        default="",
        help="Apollo.io organization search state/province filter, e.g. 'California'",
    )
    parser.add_argument(
        "--apollo-org-city",
        type=str,
        default="",
        help="Apollo.io organization search city filter, e.g. 'Los Angeles'",
    )
    parser.add_argument(
        "--apollo-org-zip",
        type=str,
        default="",
        help="Apollo.io organization search zip/postal code filter, e.g. '90210'",
    )
    parser.add_argument(
        "--apollo-org-max-orgs",
        type=int,
        default=50,
        help="Maximum organizations to fetch from Apollo Organization Search (default: 50)",
    )
    parser.add_argument(
        "--apollo-org-max-people-per-org",
        type=int,
        default=5,
        help="Maximum people to fetch per organization in Apollo Organization Search (default: 5)",
    )
    parser.add_argument(
        "--supplier-portal-domains",
        type=str,
        default="",
        help="Comma-separated domains to scan for supplier/procurement portal pages",
    )
    parser.add_argument(
        "--scan-supplier-pages",
        action="store_true",
        help="After finding domains, also scan them for procurement/supplier portal pages",
    )
    parser.add_argument(
        "--verify-csv",
        type=str,
        default="",
        help="Maintenance mode: path to an existing CSV file to batch-verify",
    )
    parser.add_argument(
        "--verify-domain",
        action="store_true",
        help="Maintenance mode: check if each domain's homepage is alive",
    )
    parser.add_argument(
        "--verify-email",
        action="store_true",
        help="Maintenance mode: validate emails via ZeroBounce (requires API key)",
    )
    parser.add_argument(
        "--verify-company",
        action="store_true",
        help="Maintenance mode: check whether companies appear still operating",
    )
    parser.add_argument(
        "--verify-output",
        type=str,
        default="",
        help="Maintenance mode: output CSV path (default: overwrite input file)",
    )
    parser.add_argument(
        "--verify-workers",
        type=int,
        default=8,
        help="Maintenance mode: max concurrent workers for verification (default: 8)",
    )
    args = parser.parse_args()

    extra_excluded = set(d.strip().lower() for d in args.exclude.split(",") if d.strip())
    config = load_config()
    finder = LeadFinder(config, engine=args.engine, extra_excluded=extra_excluded)

    # Supplier Portal Scan mode
    if args.supplier_portal_domains:
        domains = [d.strip() for d in args.supplier_portal_domains.split(",") if d.strip()]
        finder.run_supplier_portal_scan(domains=domains, output=args.output)
        return

    # Lead Maintenance / Verification mode
    if args.verify_csv:
        if not any([args.verify_domain, args.verify_email, args.verify_company]):
            print("[ERROR] --verify-csv requires at least one of --verify-domain, --verify-email, --verify-company")
            sys.exit(1)
        finder.run_verification(
            csv_path=args.verify_csv,
            check_domain=args.verify_domain,
            check_email=args.verify_email,
            check_company=args.verify_company,
            output=args.verify_output,
            max_workers=args.verify_workers,
        )
        return

    # Apollo Organization Search mode
    if args.apollo_org_keywords:
        org_keywords = [k.strip() for k in args.apollo_org_keywords.split(",") if k.strip()]
        org_locations = [l.strip() for l in args.apollo_org_locations.split(",") if l.strip()]
        titles = [t.strip() for t in args.apollo_titles.split(",") if t.strip()]
        employee_range = None
        if args.apollo_org_employee_range:
            parts = [p.strip() for p in args.apollo_org_employee_range.split(",") if p.strip()]
            if len(parts) == 2:
                employee_range = parts
        finder.run_apollo_organization_search(
            org_keywords=org_keywords or None,
            org_locations=org_locations or None,
            employee_range=employee_range,
            country=args.apollo_org_country or None,
            state=args.apollo_org_state or None,
            city=args.apollo_org_city or None,
            zip_code=args.apollo_org_zip or None,
            person_titles=titles,
            max_orgs=args.apollo_org_max_orgs,
            max_people_per_org=args.apollo_org_max_people_per_org,
            output=args.output,
            keep_no_email=getattr(args, "keep_no_email", False),
            min_relevance=args.apollo_min_relevance,
            strict_mode=args.apollo_strict_mode,
            max_enrich=args.apollo_max_enrich,
            no_enrich=args.apollo_no_enrich,
            scan_supplier_pages=args.scan_supplier_pages,
        )
        return

    # Apollo People Search mode
    if args.apollo_keywords or args.apollo_domains:
        org_keywords = [k.strip() for k in args.apollo_keywords.split(",") if k.strip()]
        org_domains = [d.strip().lower() for d in args.apollo_domains.split(",") if d.strip()]
        titles = [t.strip() for t in args.apollo_titles.split(",") if t.strip()]
        locations = [l.strip() for l in args.apollo_locations.split(",") if l.strip()]
        employee_range = None
        if args.apollo_employee_range:
            parts = [p.strip() for p in args.apollo_employee_range.split(",") if p.strip()]
            if len(parts) == 2:
                employee_range = parts
        finder.run_apollo_search(
            organization_keywords=org_keywords or None,
            person_titles=titles,
            person_locations=locations,
            max_results=args.max_domains or 100,
            output=args.output,
            keep_no_email=getattr(args, "keep_no_email", False),
            employee_range=employee_range,
            organization_domains=org_domains or None,
            country=args.apollo_country or None,
            state=args.apollo_state or None,
            city=args.apollo_city or None,
            zip_code=args.apollo_zip or None,
            scan_supplier_pages=args.scan_supplier_pages,
            min_relevance=args.apollo_min_relevance,
            strict_mode=args.apollo_strict_mode,
            max_enrich=args.apollo_max_enrich,
            no_enrich=args.apollo_no_enrich,
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
        scan_supplier_pages=args.scan_supplier_pages,
    )


if __name__ == "__main__":
    main()
