# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

B2B Lead Finder is a Python tool that searches for domains by keyword, discovers decision-maker emails via Hunter.io/Snov.io/Apollo.io/VoilaNorbert, filters out generic addresses, and exports to CSV. It has three entry points over one core engine ([lead_finder.py](lead_finder.py)): the CLI itself, a FastAPI web app ([api.py](api.py)) with a static HTML frontend, and an MCP server ([lead_finder_mcp/server.py](lead_finder_mcp/server.py)).

There is no test suite, linter config, or build step — it runs directly with the Python interpreter.

## Common Commands

- **Install dependencies:** `pip install -r requirements.txt`
- **Run CLI search:** `python lead_finder.py "football gloves manufacturer" --pages 5 --output leads.csv`
- **Run API server:** `uvicorn api:app --reload --host 0.0.0.0 --port 8000`
- **Run MCP server (stdio):** `lead-finder-mcp` (after `pip install -e .`) or `python -m lead_finder_mcp.server`

There are no automated tests; the `test_*.csv` / `test_*.log` files in the repo root are manual run artifacts, not a test suite.

### CLI modes
`lead_finder.py` has no subparsers — the *mode* is selected by which flags are present, checked in this order in `main()`:
1. `--supplier-portal-domains` → supplier portal scan
2. `--verify-csv` (requires one of `--verify-domain` / `--verify-email` / `--verify-company`) → lead maintenance/verification of an existing CSV
3. `--apollo-keywords` or `--apollo-domains` → Apollo People Search
4. otherwise the positional `keyword` → normal keyword search (and `--engine google_maps` / `--domains` / `--amazon` switch behavior within it)

The MCP tool names (`search_leads`, `batch_domains`, `apollo_search`, `google_maps_search`, `supplier_portal_scan`, `validate_email`) are thin wrappers that shell out to these same flag combinations via `subprocess`.

## Architecture

### Single-file core
All business logic lives in [lead_finder.py](lead_finder.py): `Lead` dataclass, API clients (`SerpAPIClient`, `DuckDuckGoClient`, `BrowserClient`, `GoogleMapsClient`, `HunterClient`, `SnovClient`, `ApolloClient`, `NorbertClient`, `ZeroBounceClient`), filtering heuristics, verification, and CSV export. Both [api.py](api.py) and [lead_finder_mcp/server.py](lead_finder_mcp/server.py) reuse it (the API by `subprocess`, the MCP server by importing it as a module *and* shelling out). `pyproject.toml` exposes it as a top-level `py-modules` entry so the MCP package can `import lead_finder`.

### Search engine hierarchy
`LeadFinder._resolve_engine()` picks engines in this priority when `--engine auto` (default):
1. **DuckDuckGo** ([ddgs](https://github.com/deedy5/duckduckgo-search)) — free, no API key.
2. **Browser** (Playwright + DuckDuckGo HTML) — fallback when `ddgs` is unavailable.
3. **SerpAPI** — only if `serpapi_key` is configured in [config.yaml](config.yaml).

`--engine google_maps` (or `auto` when `google_maps_key` is set) uses `GoogleMapsClient` (Places API) instead, scoped by `--maps-region`.

### Search result scoring
Results are scored before domain extraction using `POSITIVE_SIGNALS` (distributor, wholesale, importer, OEM, etc. → +10) and `NEGATIVE_SIGNALS` (news, blog, review, job, investor, etc. → -20). Domains are sorted by score descending so small distributors bubble to the top.

### Email discovery pipeline
1. Search → extract unique domains (big-brand domains are excluded; see `EXCLUDED_DOMAINS`).
2. Query **Hunter.io** primary; if no results, fall back to **Snov.io** (if configured), then **Apollo.io** (3rd fallback). **VoilaNorbert** (`NorbertClient`) is also a configurable email-discovery provider.
3. Filter: drop generic prefixes (`info@`, `sales@`, etc. from `GENERIC_PREFIXES`).
4. Keep emails that are Hunter-tagged `personal` OR match `PERSONAL_PATTERNS` regex OR have confidence ≥ 50.
5. Optional ZeroBounce validation.
6. Deduplicate by email and export CSV with UTF-8 BOM.

### Hunter multi-key rotation
`HunterClient` accepts a comma-separated string of keys (`"key1,key2,key3"`). On HTTP 429 it marks the key dead and rotates automatically. Cloud deployments should pass a comma-separated `HUNTER_KEY` env var.

### Configuration
Config is loaded from [config.yaml](config.yaml) if present; otherwise falls back to environment variables:
- `SERPAPI_KEY`, `GOOGLE_MAPS_KEY`, `HUNTER_KEY`, `SNOV_KEY`, `APOLLO_KEY`, `NORBERT_KEY`, `ZEROBOUNCE_KEY`

At least one of `hunter_key` / `snov_key` / `apollo_key` / `norbert_key` is required or `load_config()` exits. Use env vars for cloud deployment. `config.yaml` is gitignored (see `config.yaml.example`).

### Deep search mode
Pass `--deep` to skip the first 5 pages of search results (avoids big-brand listings and Wikipedia). The CLI prints `Mode: DEEP (skip first 5 pages)` when active.

### Lead maintenance / verification mode
`--verify-csv <path>` re-checks an existing exported CSV in place (or to `--verify-output`), parallelized across `--verify-workers`. Each of the three checks is opt-in: `--verify-domain` (domain still resolves/serves), `--verify-email` (re-validate via ZeroBounce), `--verify-company` (`check_company_status()` probes the site for signs the company is still active). Results land in the `domain_alive` / `domain_check_error` / `email_valid` / `company_active` / `company_status_notes` / `last_verified_at` columns. Exposed in the web app via `GET /api/leads/verify/stream`.

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

### Customer tier classification (A/B/C)
Every exported lead is assigned a `tier` (A/B/C) plus a human-readable `tier_reason` that estimates customer value independent of keyword relevance:
- **A** = core large customers: professional sports brands, manufacturers, international distributors/wholesalers, large retail chains, HQ-level procurement.
- **B** = potential customers: regional distributors, medium offline stores, multi-store chains, independent dealers.
- **C** = low-value leads: ski resorts, rental shops, lesson/tour providers, small single-store retailers, blogs/news sites.

Classification is done by `_classify_tier()` in [lead_finder.py](lead_finder.py) using signal lists (`TIER_A_SIGNALS`, `TIER_B_SIGNALS`, `TIER_C_SIGNALS`) and factors such as `relevance_score`, `purchasing_authority`, `org_structure_type`, `parent_company_name`, `email_type`, `has_direct_phone`, and `confidence_score`. The tier flows through to the CSV, API JSON, frontend badge/filter, Excel export, and MCP summary.

### Lead enrichment
`db_enrich_leads()` cross-references search results against the `contacts` table to mark which leads have already been contacted, by whom, and their current status. This lets the frontend show contacted badges without re-querying.

### Streaming search endpoints
Each search mode has a Server-Sent Events variant that yields subprocess log lines in real time plus a final `done` payload: `GET /api/leads/stream` (keyword), `GET /api/leads/apollo/stream`, `GET /api/leads/supplier-portal/stream`, `GET /api/leads/verify/stream`. The synchronous `POST /api/leads/search` blocks until completion. Frontend pages: `index.html` (search), `result.html`, `history.html`, `dashboard.html` (backed by `GET /api/dashboard`).

## Key Data Model

`Lead` dataclass fields include: `email`, `first_name`, `last_name`, `position`, `department`, `company`, `domain`, `country`, `confidence_score`, `email_type` (`personal` | `generic`), `validation_status`, `sources`, `search_keyword`, `found_at`, `website_description`, `relevance_score`, `linkedin_url`, `phone`, `address`, Google Maps fields (`google_rating`, `google_reviews_count`, `google_maps_url`, `place_id`), `source_type`, `has_direct_phone`, supplier-scan fields (`supplier_page_url`, `supplier_email`, `supplier_form_link`, …), verification fields (`domain_alive`, `domain_check_error`, `email_valid`, `company_active`, `company_status_notes`, `last_verified_at`), and tier fields (`tier` and `tier_reason`). The authoritative field list and CSV column order is `_export_csv()` in [lead_finder.py](lead_finder.py).
