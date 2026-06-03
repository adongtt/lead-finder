# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

B2B Lead Finder is a Python tool that searches for domains by keyword, discovers decision-maker emails via Hunter.io/Snov.io, filters out generic addresses, and exports to CSV. It consists of a CLI script ([lead_finder.py](lead_finder.py)) and a FastAPI web wrapper ([api.py](api.py)) with a static HTML frontend.

## Common Commands

- **Install dependencies:** `pip install -r requirements.txt`
- **Run CLI search:** `python lead_finder.py "football gloves manufacturer" --pages 5 --output leads.csv`
- **Run API server:** `uvicorn api:app --reload --host 0.0.0.0 --port 8000`

CLI flags: `--pages`, `--output`, `--validate` (ZeroBounce), `--max-domains`, `--engine {auto,duckduckgo,browser,serpapi}`, `--exclude domain1,domain2`, `--deep`.

## Architecture

### Single-file core
All business logic lives in [lead_finder.py](lead_finder.py): `Lead` dataclass, API clients (`SerpAPIClient`, `DuckDuckGoClient`, `BrowserClient`, `HunterClient`, `SnovClient`, `ApolloClient`, `ZeroBounceClient`), filtering heuristics, and CSV export. There is no separate test suite or package structure.

### Search engine hierarchy
`LeadFinder._resolve_engine()` picks engines in this priority when `--engine auto` (default):
1. **DuckDuckGo** ([ddgs](https://github.com/deedy5/duckduckgo-search)) — free, no API key.
2. **Browser** (Playwright + DuckDuckGo HTML) — fallback when `ddgs` is unavailable.
3. **SerpAPI** — only if `serpapi_key` is configured in [config.yaml](config.yaml).

### Search result scoring
Results are scored before domain extraction using `POSITIVE_SIGNALS` (distributor, wholesale, importer, OEM, etc. → +10) and `NEGATIVE_SIGNALS` (news, blog, review, job, investor, etc. → -20). Domains are sorted by score descending so small distributors bubble to the top.

### Email discovery pipeline
1. Search → extract unique domains (big-brand domains are excluded; see `EXCLUDED_DOMAINS`).
2. Query **Hunter.io** primary; if no results, fall back to **Snov.io** (if configured), then **Apollo.io** (3rd fallback).
3. Filter: drop generic prefixes (`info@`, `sales@`, etc. from `GENERIC_PREFIXES`).
4. Keep emails that are Hunter-tagged `personal` OR match `PERSONAL_PATTERNS` regex OR have confidence ≥ 50.
5. Optional ZeroBounce validation.
6. Deduplicate by email and export CSV with UTF-8 BOM.

### Hunter multi-key rotation
`HunterClient` accepts a comma-separated string of keys (`"key1,key2,key3"`). On HTTP 429 it marks the key dead and rotates automatically. Cloud deployments should pass a comma-separated `HUNTER_KEY` env var.

### Configuration
Config is loaded from [config.yaml](config.yaml) if present; otherwise falls back to environment variables:
- `SERPAPI_KEY`, `HUNTER_KEY`, `SNOV_KEY`, `APOLLO_KEY`, `ZEROBOUNCE_KEY`

Use env vars for cloud deployment. `config.yaml` is gitignored.

### Deep search mode
Pass `--deep` to skip the first 5 pages of search results (avoids big-brand listings and Wikipedia). The CLI prints `Mode: DEEP (skip first 5 pages)` when active.

### API layer
[api.py](api.py) is a thin FastAPI wrapper that spawns `lead_finder.py` via `subprocess.run`, reads the resulting CSV from `web_results/`, and returns JSON with a download URL. The frontend is a single static HTML file at [static/index.html](static/index.html).

### Database
PostgreSQL is used in production (local dev falls back to `postgresql://postgres:postgres@localhost:5432/leadfinder`). Tables:
- `searches` — job history, keyword, CSV content, user attribution
- `contacts` — CRM records with status and next follow-up date
- `followups` — action history per contact
- `keywords` — search term frequency counter

On startup, `_migrate_json_to_postgres()` moves legacy JSON file data (`searches.json`, `contacted.json`, `keywords.json`) into Postgres and renames the files to `.bak`.

### Auth & roles
Session-based auth via `SessionMiddleware` + `users.json` (plain JSON with bcrypt hashes). Roles are `admin` and `sales` (default). Admins see all searches/contacts; sales users see only their own. Endpoints use `require_user` and `require_admin` dependencies.

### Position priority sorting
Leads are sorted by position importance before returning to the frontend. The priority list (higher = earlier) includes: Buyer, Purchasing Manager, Procurement Manager, Sourcing Manager, Merchandiser, Product Manager, Brand Manager, Marketing Manager, General Manager, Managing Director. Unknown positions sort to the end.

### Lead enrichment
`db_enrich_leads()` cross-references search results against the `contacts` table to mark which leads have already been contacted, by whom, and their current status. This lets the frontend show contacted badges without re-querying.

### Streaming search endpoint
`POST /api/leads/stream` returns Server-Sent Events while the subprocess runs, yielding log lines in real time and a final `done` payload with results. The synchronous `POST /api/leads/search` blocks until completion.

## Key Data Model

`Lead` dataclass fields: `email`, `first_name`, `last_name`, `position`, `department`, `company`, `domain`, `confidence_score`, `email_type` (`personal` | `generic`), `validation_status`, `sources`, `search_keyword`, `found_at`, `country`.
