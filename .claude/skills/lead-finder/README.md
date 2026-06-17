# Lead Finder Skill

A Claude Code skill for discovering B2B decision-maker emails and exporting them to CSV.

## What it does

This skill wraps the local `lead_finder.py` CLI and supports several lead-generation modes:

- **Keyword search** — find domains by keyword, then discover emails.
- **Batch domains** — provide a list of domains and find emails on those sites.
- **Apollo people search** — search Apollo.io by company keywords, titles, and locations.
- **Google Maps** — find local businesses and extract their emails.
- **Supplier portal scan** — scan procurement/supplier pages for contact emails.

## Installation

### Option 1: Project-level skill (recommended for this repo)

The skill folder is already included in this repository at `.claude/skills/lead-finder/`. Claude Code will detect it automatically when you open the project.

### Option 2: Global skill (available across all projects)

Copy the skill folder to your global Claude skills directory:

```bash
# macOS / Linux
cp -r .claude/skills/lead-finder ~/.claude/skills/lead-finder

# Windows (PowerShell)
Copy-Item -Recurse .claude\skills\lead-finder $env:USERPROFILE\.claude\skills\lead-finder
```

Restart Claude Code after copying.

## Configuration

1. Copy the example config to the project root:
   ```bash
   cp .claude/skills/lead-finder/config.yaml.example config.yaml
   ```
2. Open `config.yaml` and fill in at least one email-discovery key:
   - `hunter_key`
   - `snov_key`
   - `apollo_key`
   - `norbert_key`
3. Optional keys:
   - `serpapi_key` for Google Search
   - `google_maps_key` for Google Maps mode
   - `zerobounce_key` for email validation

Alternatively, set the corresponding environment variables (`HUNTER_KEY`, `SNOV_KEY`, etc.).

## Usage

In Claude Code, type:

```
/lead-finder "football gloves manufacturer" --pages 5 --output leads.csv
```

Other examples:

```
/lead-finder --domains "anthropic.com,openai.com" --output batch.csv
/lead-finder --apollo-keywords "sporting goods" --apollo-titles "Buyer,Purchasing Manager" --output apollo.csv
/lead-finder --engine google_maps --maps-region "USA" --keyword "football gloves" --output maps.csv
```

For the full list of flags, run:

```bash
python lead_finder.py --help
```

## Notes

- The skill is **stateless**: it generates a CSV file and does not write to the web UI's PostgreSQL database.
- Free API quotas are limited; large batches may require paid keys.
- Respect GDPR / CAN-SPAM when using exported emails.
