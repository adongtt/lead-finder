---
name: lead-finder
description: Search for B2B leads by keyword, domains, Apollo people search, Google Maps, or supplier portals and export decision-maker emails to CSV.
user-invocable: true
argument-hint: <keyword or mode> [options]
---

# /lead-finder

Search for B2B decision-maker emails using the local `lead_finder.py` CLI and export results to CSV.

## When to use

- Build a cold outreach list from a keyword.
- Find emails for a specific list of domains.
- Search Apollo.io for people by title, location, or company keywords.
- Find local businesses via Google Maps and extract their emails.
- Scan supplier/procurement portal pages for contact emails.

## Before you run

1. Ensure dependencies are installed in the project environment:
   ```bash
   pip install -r requirements.txt
   ```
2. Ensure API keys are configured. The tool needs **at least one** email-discovery key:
   - `hunter_key` (Hunter.io)
   - `snov_key` (Snov.io)
   - `apollo_key` (Apollo.io)
   - `norbert_key` (VoilaNorbert)
3. Configure keys in one of two ways:
   - Copy `.claude/skills/lead-finder/config.yaml.example` to `config.yaml` in the project root and fill in values.
   - Set environment variables: `HUNTER_KEY`, `SNOV_KEY`, `APOLLO_KEY`, `NORBERT_KEY`.
4. Optional keys:
   - `serpapi_key` for Google Search via SerpAPI.
   - `google_maps_key` for Google Maps mode.
   - `zerobounce_key` to validate discovered emails.

If no email-discovery key is present, stop and guide the user to get a free key:
- Hunter.io: https://hunter.io/api (25 free/month)
- Snov.io: https://app.snov.io/api (50 free/month)
- Apollo.io: https://apollo.io/api (free tier available)

## Arguments

Treat everything after `/lead-finder` as arguments to `python lead_finder.py`.

### Common modes

| Mode | Example command |
|------|-----------------|
| Keyword search | `/lead-finder "football gloves manufacturer" --pages 5 --output leads.csv` |
| Batch domains | `/lead-finder --domains "anthropic.com,openai.com" --output batch.csv` |
| Apollo people search | `/lead-finder --apollo-keywords "sporting goods" --apollo-titles "Buyer,Purchasing Manager" --output apollo.csv` |
| Google Maps | `/lead-finder --engine google_maps --maps-region "USA" --keyword "football gloves" --output maps.csv` |
| Supplier portal scan | `/lead-finder --scan-supplier-pages --keyword "baseball gloves distributor" --output suppliers.csv` |

### Frequently used flags

- `--pages N` — search result pages (default: 5)
- `--max-domains N` — limit domains to process
- `--output FILE.csv` — output CSV path (default: `leads.csv`)
- `--validate` — validate emails with ZeroBounce
- `--deep` — skip first 5 pages to avoid big brands
- `--engine {auto,duckduckgo,browser,serpapi,google_maps}` — search engine
- `--exclude domain1,domain2` — extra domains to skip
- `--domains domain1,domain2` — process specific domains directly
- `--target-tlds ".com,.us"` — keep only certain TLDs
- `--min-relevance N` — minimum content-relevance score
- `--strict-mode` — tighter filtering (excludes big platforms + advanced syntax)
- `--keep-no-email` — keep leads even when no email is found

### Apollo-specific flags

- `--apollo-keywords KEYWORDS`
- `--apollo-titles "Buyer,Purchasing Manager"`
- `--apollo-locations "United States"`
- `--apollo-employee-range "2,50"`
- `--apollo-domains "domain1,domain2"`
- `--apollo-country / --apollo-state / --apollo-city / --apollo-zip`

### Supplier portal flags

- `--scan-supplier-pages`
- `--supplier-portal-domains "domain1,domain2"`

For the full list, run `python lead_finder.py --help`.

## Workflow

1. Parse the user's command after `/lead-finder`.
2. If the user did not provide a keyword or mode flag, ask for one.
3. Verify that at least one email-discovery key is available. If not, guide the user to configure keys.
4. Assemble the full command:
   ```bash
   python lead_finder.py <arguments>
   ```
5. Run the command from the project root directory. Stream output when possible.
6. After completion, read the generated CSV and report:
   - CSV file path.
   - Total number of leads.
   - Number of personal vs generic emails.
   - Top 3 positions.
   - Source breakdown (hunter.io / snov.io / apollo.io / norbert.io).
7. If the command fails, surface the error and suggest fixes (missing key, quota exhausted, network issue, bad arguments).

## Examples

```
/lead-finder "football gloves manufacturer" --pages 5 --output leads.csv
/lead-finder "ski gloves distributor" --pages 10 --deep --output ski_leads.csv
/lead-finder --domains "anthropic.com,openai.com" --max-domains 2 --output batch.csv
/lead-finder --apollo-keywords "sporting goods" --apollo-titles "Buyer,Purchasing Manager" --output apollo.csv
/lead-finder --engine google_maps --maps-region "USA" --keyword "football gloves" --output maps.csv
/lead-finder --scan-supplier-pages --keyword "baseball gloves distributor" --output suppliers.csv
```

## Output

A CSV file containing: `email`, `first_name`, `last_name`, `position`, `company`, `domain`, `confidence_score`, `email_type`, `validation_status`, `sources`, `search_keyword`, `found_at`, `country`.

## Notes

- Free API quotas are limited; large batches may require paid keys.
- The tool auto-excludes 80+ big-brand domains (Amazon, Google, LinkedIn, etc.).
- This skill is **stateless**: it generates a CSV file and does **not** write to the web UI's PostgreSQL database.
- Respect GDPR / CAN-SPAM when using exported emails.
