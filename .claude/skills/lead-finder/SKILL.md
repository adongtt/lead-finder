# /lead-finder

Search for B2B leads by keyword and export decision-maker emails to CSV.

## When to Use

- Find emails from a keyword search (e.g. industry + role/region)
- Build a cold outreach list
- Discover distributors, buyers, or procurement contacts

## Arguments

- `keyword` (required): Search term, e.g. `"ski gloves importer usa"`
- `--pages` (optional): Number of search pages. Default: 20
- `--max-domains` (optional): Limit how many domains to process
- `--deep` (optional): Skip first 5 pages to avoid big brands
- `--exclude` (optional): Comma-separated domains to skip
- `--validate` (optional): Enable ZeroBounce email validation
- `--output` (optional): CSV filename. Default: `leads.csv`

## Before You Run

1. Ensure `lead_finder.py` and `config.yaml` exist in the working directory.
2. Check `config.yaml` for at least one email-discovery key:
   ```yaml
   hunter_key: "your_hunter_key"
   snov_key: "your_snov_key"
   apollo_key: "your_apollo_key"
   ```
   At least one of the three is required.
3. If no key is configured, guide the user to get a free key from:
   - Hunter.io: https://hunter.io/api (25 free/month)
   - Snov.io: https://app.snov.io/api (50 free/month)
   - Apollo.io: https://apollo.io/api (free tier available)

## Workflow

1. Run the search:
   ```bash
   python lead_finder.py "{{keyword}}" {{flags}}
   ```
2. Wait for completion.
3. Return the CSV file path and a brief summary (total leads, top contacts by position).

## Example

```
/lead-finder "ski gloves importer usa" --pages 10 --deep --output ski_leads.csv
```

## Output

A CSV file containing: email, first_name, last_name, position, company, domain, confidence_score, sources (hunter.io / snov.io / apollo.io).

## Notes

- Free API quotas are limited; large batches may require paid keys.
- The tool auto-excludes 80+ big-brand domains (Amazon, Google, etc.).
- Respect GDPR / CAN-SPAM when using the exported emails.
