# Changelog

All notable changes to this project will be documented in this file.

## 2026-07-07

### Added
- **Serper.dev search engine support**
  - New `SerperClient` in `lead_finder.py` calling `POST https://google.serper.dev/search` with `X-API-KEY` header.
  - Supports pagination and `--deep` skip-pages semantics.
  - New `serper_key` config option and `SERPER_KEY` environment variable.
  - `--engine serper` CLI choice added; `--engine auto` now prefers Serper when configured, then DuckDuckGo, Browser, and SerpAPI.
  - API config status and MCP server env fallback now recognize `serper_key`.

### Added
- **Business operating attribute classification**
  - New `business_type` and `business_type_reason` fields on the `Lead` dataclass in `lead_finder.py`.
  - New `_classify_business_type()` helper with signal-based rules for `wholesaler` (批发商/分销商), `retailer` (线下零售商), `brand` (自有品牌方), `end_buyer` (终端采购方), and `unknown` (未分类).
  - Classification is computed for every lead produced by keyword search, Apollo People/Org Search, Google Maps, and supplier-portal scan pipelines.
  - Guard rules avoid false positives: "best brands" does not make a retailer a brand, dropshipping is not a wholesaler, pure marketplace listings are not retailers.
  - `business_type` is exported to CSV and loaded back by `_leads_from_csv()`.
  - `result.html` shows a "客户类型" column with color-coded badges and a filter dropdown.
  - `index.html` preview table also shows business-type badges.
  - Excel export maps `business_type` to "客户类型" and `business_type_reason` to "客户类型原因".

## 2026-07-03

### Added
- **A/B/C Customer Tier Classification**
  - New `tier` and `tier_reason` fields on the `Lead` dataclass in `lead_finder.py`.
  - New `_classify_tier()` helper with generic signal-based rules (A = core/large customers, B = potential customers, C = low-value leads).
  - Tier is computed for every lead produced by keyword search, Apollo People Search, Apollo Organization Search, Google Maps, and supplier-portal scan pipelines.
  - Tier columns exported to CSV and loaded back by `_leads_from_csv()`.
  - API/frontend sorting now prioritizes tier (A → B → C), then position priority, then relevance.
  - `result.html` shows a "客户分层" column with color-coded badges (A green, B blue, C gray) and a tier filter dropdown.
  - `index.html` preview table also shows tier badges.
  - MCP tool output now includes tier distribution counts.
  - Excel export maps `tier` to "客户分层" and `tier_reason` to "分层原因".

## 2026-06-30

### Added
- **Apollo Organization Search** (separate from Apollo People Search)
  - New `ApolloClient.search_organizations()` in `lead_finder.py` calling `POST /mixed_companies/search`.
  - New `LeadFinder.run_apollo_organization_search()` pipeline: search companies → score/filter at organization level → drill into each kept company for purchasing contacts.
  - New CLI flags: `--apollo-org-keywords`, `--apollo-org-locations`, `--apollo-org-employee-range`, `--apollo-org-country`, `--apollo-org-state`, `--apollo-org-city`, `--apollo-org-zip`, `--apollo-org-max-orgs`, `--apollo-org-max-people-per-org`.
  - New API streaming endpoint: `GET /api/leads/apollo/organization/stream`.
  - New MCP tool: `apollo_organization_search`.
  - New frontend mode: "Apollo 企业搜索" with org keywords, locations, employee range, max orgs, people/org, titles, relevance slider, strict mode, and supplier-page scan options.

### Fixed
- Apollo Organization Search 422 error: changed `q_keyword_tags` to `q_organization_keyword_tags` in the `/mixed_companies/search` payload.
- Apollo Organization Search now clearly reports when the account has insufficient lead credits instead of dumping a raw 422 error.

### Improved
- Tightened Apollo People Search relevance scoring for product+channel-role queries (e.g. "ski gloves distributor") to filter out ski resorts, rental shops, used-gear stores, and unrelated cross-industry distributors.
- Retail/resort guard now runs before positive-signal case returns, eliminating false positives like ski resorts.

## Earlier

See `git log` for previous changes.
