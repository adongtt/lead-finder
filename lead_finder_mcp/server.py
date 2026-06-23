"""MCP server wrapping lead_finder.py for B2B lead discovery."""

import asyncio
import csv
import os
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP

# Reuse the core script as a module. It is included as a top-level module in the package.
import lead_finder
from lead_finder import ZeroBounceClient

mcp = FastMCP("lead-finder")


def _config_safe() -> dict:
    """Load config without killing the server when keys are missing.

    Mirrors lead_finder.load_config() but never calls sys.exit().
    """
    try:
        if lead_finder.CONFIG_PATH.exists():
            with open(lead_finder.CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
                if isinstance(cfg, dict):
                    return cfg
    except Exception:
        pass

    return {
        "serpapi_key": os.environ.get("SERPAPI_KEY", ""),
        "google_maps_key": os.environ.get("GOOGLE_MAPS_KEY", ""),
        "hunter_key": os.environ.get("HUNTER_KEY", ""),
        "snov_key": os.environ.get("SNOV_KEY", ""),
        "apollo_key": os.environ.get("APOLLO_KEY", ""),
        "norbert_key": os.environ.get("NORBERT_KEY", ""),
        "zerobounce_key": os.environ.get("ZEROBOUNCE_KEY", ""),
    }


def _has_email_discovery_key(config: dict) -> bool:
    return bool(
        config.get("hunter_key")
        or config.get("snov_key")
        or config.get("apollo_key")
        or config.get("norbert_key")
    )


def _default_output(prefix: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return str(Path.cwd() / f"{prefix}_{ts}.csv")


async def _run_cli(args: list[str]) -> tuple[int, str, str]:
    """Run lead_finder.py as a subprocess and return (returncode, stdout, stderr)."""
    script = Path(lead_finder.__file__)
    cmd = [sys.executable, str(script), *args]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(Path.cwd()),
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    return proc.returncode or 0, stdout_bytes.decode("utf-8", errors="replace"), stderr_bytes.decode("utf-8", errors="replace")


def _summarize_csv(csv_path: str) -> dict[str, Any]:
    """Read a generated CSV and produce summary statistics."""
    path = Path(csv_path)
    if not path.exists():
        return {"exists": False, "count": 0}

    rows: list[dict[str, str]] = []
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    total = len(rows)
    personal = sum(1 for r in rows if r.get("email_type", "").lower() == "personal")
    generic = total - personal
    positions = Counter(r.get("position", "Unknown") for r in rows if r.get("position"))
    sources: Counter[str] = Counter()
    for r in rows:
        for src in (r.get("sources") or "").split(" / "):
            src = src.strip()
            if src:
                sources[src] += 1

    authority_counts = Counter(r.get("purchasing_authority", "") for r in rows if r.get("purchasing_authority"))
    with_parent = sum(1 for r in rows if r.get("parent_company_name"))

    return {
        "exists": True,
        "count": total,
        "personal": personal,
        "generic": generic,
        "top_positions": positions.most_common(3),
        "sources": sources.most_common(),
        "authority_counts": authority_counts.most_common(),
        "with_parent_company": with_parent,
    }


def _format_result(tool_name: str, csv_path: str, returncode: int, stdout: str, stderr: str) -> str:
    if returncode != 0:
        return f"## {tool_name} failed\n\nExit code: {returncode}\n\n```\n{stderr or stdout}\n```"

    summary = _summarize_csv(csv_path)
    lines = [
        f"## {tool_name} complete",
        f"",
        f"**CSV:** `{csv_path}`",
        f"**Total leads:** {summary.get('count', 0)}",
    ]
    if summary.get("exists"):
        lines.extend([
            f"**Personal emails:** {summary['personal']}",
            f"**Generic emails:** {summary['generic']}",
        ])
        if summary["top_positions"]:
            lines.append("**Top positions:**")
            for pos, cnt in summary["top_positions"]:
                lines.append(f"- {pos}: {cnt}")
        if summary["sources"]:
            lines.append("**Sources:**")
            for src, cnt in summary["sources"]:
                lines.append(f"- {src}: {cnt}")
        if summary.get("with_parent_company"):
            lines.append(f"**With parent company:** {summary['with_parent_company']}")
        if summary.get("authority_counts"):
            lines.append("**Purchasing authority:**")
            for auth, cnt in summary["authority_counts"]:
                lines.append(f"- {auth}: {cnt}")

    if stderr:
        lines.extend(["", "**Warnings / logs:**", f"```\n{stderr[-2000:]}\n```"])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_leads(
    keyword: str,
    pages: int = 2,
    max_domains: int = 10,
    deep: bool = False,
    engine: str = "auto",
    output: str | None = None,
    validate: bool = False,
) -> str:
    """Search B2B leads by keyword and export decision-maker emails to CSV.

    Args:
        keyword: Search term, e.g. "football gloves manufacturer".
        pages: Number of search pages (default 2).
        max_domains: Maximum domains to process (default 10).
        deep: Skip first 5 pages to avoid big brands.
        engine: Search engine: auto, duckduckgo, browser, serpapi, google_maps.
        output: Output CSV path. Defaults to mcp_search_*.csv in the working directory.
        validate: Validate emails via ZeroBounce (requires key).
    """
    config = _config_safe()
    if not _has_email_discovery_key(config):
        return "[ERROR] No email-discovery key found. Set HUNTER_KEY, SNOV_KEY, APOLLO_KEY, or NORBERT_KEY in config.yaml or environment."

    csv_path = output or _default_output("mcp_search")
    args = [keyword, "--pages", str(pages), "--max-domains", str(max_domains), "--engine", engine, "--output", csv_path]
    if deep:
        args.append("--deep")
    if validate:
        args.append("--validate")

    code, out, err = await _run_cli(args)
    return _format_result("search_leads", csv_path, code, out, err)


@mcp.tool()
async def batch_domains(
    domains: str,
    max_domains: int = 10,
    output: str | None = None,
    validate: bool = False,
) -> str:
    """Find emails for a specific comma-separated list of domains.

    Args:
        domains: Comma-separated domains, e.g. "anthropic.com,openai.com".
        max_domains: Maximum domains to process (default 10).
        output: Output CSV path. Defaults to mcp_batch_*.csv.
        validate: Validate emails via ZeroBounce (requires key).
    """
    config = _config_safe()
    if not _has_email_discovery_key(config):
        return "[ERROR] No email-discovery key found. Set HUNTER_KEY, SNOV_KEY, APOLLO_KEY, or NORBERT_KEY in config.yaml or environment."

    csv_path = output or _default_output("mcp_batch")
    args = ["batch-import", "--domains", domains, "--max-domains", str(max_domains), "--output", csv_path]
    if validate:
        args.append("--validate")

    code, out, err = await _run_cli(args)
    return _format_result("batch_domains", csv_path, code, out, err)


@mcp.tool()
async def apollo_search(
    keywords: str,
    titles: str = "Buyer,Purchasing Manager,Procurement Manager,Sourcing Manager",
    locations: str = "",
    max_results: int = 20,
    output: str | None = None,
) -> str:
    """Search Apollo.io for people by organization keywords and titles.

    Args:
        keywords: Organization keywords, comma-separated, e.g. "sporting goods".
        titles: Person titles, comma-separated.
        locations: Person locations, comma-separated, e.g. "United States".
        max_results: Maximum results (default 20).
        output: Output CSV path. Defaults to mcp_apollo_*.csv.
    """
    config = _config_safe()
    if not config.get("apollo_key"):
        return "[ERROR] apollo_key is required for Apollo People Search."

    csv_path = output or _default_output("mcp_apollo")
    args = [
        "apollo-search",
        "--apollo-keywords", keywords,
        "--apollo-titles", titles,
        "--max-domains", str(max_results),
        "--output", csv_path,
    ]
    if locations:
        args.extend(["--apollo-locations", locations])

    code, out, err = await _run_cli(args)
    return _format_result("apollo_search", csv_path, code, out, err)


@mcp.tool()
async def google_maps_search(
    keyword: str,
    region: str = "",
    max_results: int = 20,
    output: str | None = None,
) -> str:
    """Find local businesses via Google Maps and extract emails.

    Args:
        keyword: Business keyword, e.g. "football gloves".
        region: Region, e.g. "USA" or "Germany".
        max_results: Maximum businesses to process (default 20).
        output: Output CSV path. Defaults to mcp_maps_*.csv.
    """
    config = _config_safe()
    if not config.get("google_maps_key"):
        return "[ERROR] google_maps_key is required for Google Maps mode."
    if not _has_email_discovery_key(config):
        return "[ERROR] No email-discovery key found. Set HUNTER_KEY, SNOV_KEY, APOLLO_KEY, or NORBERT_KEY in config.yaml or environment."

    csv_path = output or _default_output("mcp_maps")
    args = [keyword, "--engine", "google_maps", "--max-domains", str(max_results), "--output", csv_path]
    if region:
        args.extend(["--maps-region", region])

    code, out, err = await _run_cli(args)
    return _format_result("google_maps_search", csv_path, code, out, err)


@mcp.tool()
async def supplier_portal_scan(
    keyword: str,
    domains: str = "",
    output: str | None = None,
) -> str:
    """Scan domains for supplier/procurement portal pages and extract contacts.

    Args:
        keyword: Search keyword used to discover domains when domains is empty.
        domains: Optional comma-separated domains to scan directly.
        output: Output CSV path. Defaults to mcp_supplier_*.csv.
    """
    config = _config_safe()
    if not _has_email_discovery_key(config):
        return "[ERROR] No email-discovery key found. Set HUNTER_KEY, SNOV_KEY, APOLLO_KEY, or NORBERT_KEY in config.yaml or environment."

    csv_path = output or _default_output("mcp_supplier")
    if domains:
        args = ["supplier-scan", "--supplier-portal-domains", domains, "--output", csv_path]
    else:
        args = [keyword, "--scan-supplier-pages", "--output", csv_path]

    code, out, err = await _run_cli(args)
    return _format_result("supplier_portal_scan", csv_path, code, out, err)


@mcp.tool()
async def validate_email(email: str) -> str:
    """Validate a single email address using ZeroBounce.

    Args:
        email: The email address to validate.
    """
    config = _config_safe()
    key = config.get("zerobounce_key")
    if not key:
        return "[ERROR] zerobounce_key is required for email validation."

    client = ZeroBounceClient(key)
    try:
        result = await asyncio.to_thread(client.validate, email)
    except Exception as e:
        return f"[ERROR] Validation failed: {e}"

    status = result.get("status", "unknown")
    sub_status = result.get("sub_status", "")
    details = []
    for k, v in result.items():
        if k not in ("status", "sub_status") and v:
            details.append(f"{k}: {v}")

    lines = [f"**Status:** {status}"]
    if sub_status:
        lines.append(f"**Sub-status:** {sub_status}")
    if details:
        lines.append("**Details:**")
        lines.extend(f"- {d}" for d in details[:10])
    return "\n".join(lines)


def main() -> None:
    """Entry point for the lead-finder-mcp console script."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
