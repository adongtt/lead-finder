# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

B2B Lead Finder is a Python tool that searches for domains by keyword, discovers decision-maker emails via Hunter.io/Snov.io, filters out generic addresses, and exports to CSV. It consists of a CLI script ([lead_finder.py](lead_finder.py)) and a FastAPI web wrapper ([api.py](api.py)) with a static HTML frontend.

## Common Commands

- **Install dependencies:** `pip install -r requirements.txt`
- **Run CLI search:** `python lead_finder.py "football gloves manufacturer" --pages 5 --output leads.csv`
- **Run API server:** `uvicorn api:app --reload --host 0.0.0.0 --port 8000`

CLI flags: `--pages`, `--output`, `--validate` (ZeroBounce), `--max-domains`, `--engine {auto,duckduckgo,browser,serpapi}`, `--exclude domain1,domain2`.

## Architecture

### Single-file core
All business logic lives in [lead_finder.py](lead_finder.py): `Lead` dataclass, API clients (`SerpAPIClient`, `DuckDuckGoClient`, `BrowserClient`, `HunterClient`, `SnovClient`, `ZeroBounceClient`), filtering heuristics, and CSV export. There is no separate test suite or package structure.

### Search engine hierarchy
`LeadFinder._resolve_engine()` picks engines in this priority when `--engine auto` (default):
1. **DuckDuckGo** ([ddgs](https://github.com/deedy5/duckduckgo-search)) — free, no API key.
2. **Browser** (Playwright + DuckDuckGo HTML) — fallback when `ddgs` is unavailable.
3. **SerpAPI** — only if `serpapi_key` is configured in [config.yaml](config.yaml).

### Email discovery pipeline
1. Search → extract unique domains (big-brand domains are excluded; see `EXCLUDED_DOMAINS`).
2. Query **Hunter.io** primary; if no results, fall back to **Snov.io** (if configured).
3. Filter: drop generic prefixes (`info@`, `sales@`, etc. from `GENERIC_PREFIXES`).
4. Keep emails that are Hunter-tagged `personal` OR match `PERSONAL_PATTERNS` regex OR have confidence ≥ 50.
5. Optional ZeroBounce validation.
6. Deduplicate by email and export CSV with UTF-8 BOM.

### Hunter multi-key rotation
`HunterClient` accepts a comma-separated string of keys (`"key1,key2,key3"`). On HTTP 429 it marks the key dead and rotates automatically. Cloud deployments should pass a comma-separated `HUNTER_KEY` env var.

### Configuration
Config is loaded from [config.yaml](config.yaml) if present; otherwise falls back to environment variables:
- `SERPAPI_KEY`, `HUNTER_KEY`, `SNOV_KEY`, `ZEROBOUNCE_KEY`

Use env vars for cloud deployment. `config.yaml` is gitignored.

### API layer
[api.py](api.py) is a thin FastAPI wrapper that spawns `lead_finder.py` via `subprocess.run`, reads the resulting CSV from `web_results/`, and returns JSON with a download URL. The frontend is a single static HTML file at [static/index.html](static/index.html).

## Key Data Model

`Lead` dataclass fields: `email`, `first_name`, `last_name`, `position`, `department`, `company`, `domain`, `confidence_score`, `email_type` (`personal` | `generic`), `validation_status`, `sources`, `search_keyword`, `found_at`.
