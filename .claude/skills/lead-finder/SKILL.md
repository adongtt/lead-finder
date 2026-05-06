# /lead-finder

Run the B2B Lead Finder tool to discover decision-maker emails from a Google/DuckDuckGo keyword search.

## When to Use

Use this skill when the user wants to:
- Find B2B leads/emails from a keyword search
- Run a lead generation campaign
- Discover potential customer contact information
- Export leads to CSV for cold outreach

## Arguments

- `keyword` (required): Search keyword(s), e.g. `"football gloves manufacturer"`
- `--pages` (optional): Number of search result pages to scan. Default: 5
- `--max-domains` (optional): Limit number of domains to process. Useful for testing.
- `--validate` (optional): Enable ZeroBounce email validation.
- `--exclude` (optional): Extra domains to exclude, comma-separated.
- `--engine` (optional): Search engine to use. Choices: auto, duckduckgo, browser, serpapi. Default: auto.

## Workflow

### Step 1: Check Environment

1. Verify the working directory contains `lead_finder.py`, `config.yaml`, and `requirements.txt`. Do not assume an absolute path — look for these files in the current directory or ask the user where they placed the tool.
2. Check that `config.yaml` exists.
3. Read `config.yaml` to verify at least one email discovery key is configured:
   - `hunter_key` (not empty or placeholder)
   - `snov_key` (not empty or placeholder)
4. If **both** keys are missing/empty, warn the user and stop. Otherwise proceed.

### Step 2: Parse User Input

Extract keyword and any flags from the user's command. Common formats:
- `/lead-finder "football gloves manufacturer"`
- `/lead-finder "sports equipment distributor" --pages 3 --max-domains 10`

If the user did not quote the keyword, treat everything before the first `--flag` as the keyword.

### Step 3: Run Lead Finder

Execute the Python script via Bash:

```bash
python lead_finder.py "{{keyword}}" {{flags}}
```

Example:
```bash
python lead_finder.py "football gloves manufacturer" --pages 3 --max-domains 5
```

Wait for the script to complete and capture all stdout output.

### Step 4: Parse Results

1. Check if a CSV file was created (default: `leads.csv`, or the `--output` path).
2. If no leads were found, report this to the user.
3. If leads were found, read the CSV file and present a summary:
   - Total leads found
   - Top 10 leads (email, first_name, last_name, position, company, domain, confidence_score)
   - Breakdown by email source (hunter.io vs snov.io)
   - File path of the exported CSV

### Step 5: Handle Errors

If the script fails, inspect the error output:
- **"Config file not found"**: Ask the user to create `config.yaml` from the template.
- **"No search engine available"**: Tell the user to run `pip install ddgs` or `pip install playwright`.
- **"restricted_account" or "Rate limited"**: Inform the user their Hunter.io or Snov.io quota may be exhausted. Suggest waiting or upgrading.
- **"No leads found"**: Suggest trying different keywords or increasing `--pages`.

## Configuration

The tool reads API keys from `config.yaml` in the same directory:

```yaml
# Required: at least ONE of these two
hunter_key: "your_hunter_api_key"   # Primary source (25 free/month)
snov_key: "your_snov_access_token"   # Fallback source (50 free/month)

# Optional email validation
zerobounce_key: "your_zerobounce_key"
```

If the user needs to set up API keys, guide them to:
- Hunter.io: https://hunter.io/api (free: 25 searches/month)
- Snov.io: https://app.snov.io/api (free: 50 searches/month)
- ZeroBounce: https://www.zerobounce.net (free: 100 validations/month)

## Examples

### Quick test with small scope

```
/lead-finder "football gloves manufacturer" --pages 2 --max-domains 5
```

### Full run with validation

```
/lead-finder "sports gear wholesale Europe" --pages 10 --validate --output europe_leads.csv
```

### Exclude competitors

```
/lead-finder "football equipment" --pages 5 --exclude "nike.com,adidas.com"
```

### Force browser search engine

```
/lead-finder "american football gloves" --pages 3 --engine browser
```

## Important Notes

- **Compliance**: This tool finds publicly available emails. Always comply with GDPR, CAN-SPAM, and local email marketing laws when contacting leads.
- **Rate Limits**: Free API tiers are limited. Do not run massive batches without paid keys.
- **Big-brand Exclusion**: The tool automatically excludes 80+ major platforms (Amazon, Google, Facebook, etc.) to avoid wasting API calls on generic addresses.
- **Output Location**: CSV files are saved in the `lead-finder/` directory by default.

## Related Files

- `lead_finder.py` — Main script
- `config.yaml` — API key configuration
- `requirements.txt` — Python dependencies
