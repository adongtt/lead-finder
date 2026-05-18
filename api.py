#!/usr/bin/env python3
"""
Lead Finder Web API — FastAPI wrapper around lead_finder.py

Run locally:
    uvicorn api:app --reload --host 0.0.0.0 --port 8000

Deploy:
    pip install -r requirements.txt
    uvicorn api:app --host 0.0.0.0 --port $PORT
"""

import asyncio
import csv
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="B2B Lead Finder API", version="1.0")

# Serve static files (frontend)
app.mount("/static", StaticFiles(directory="static"), name="static")

RESULTS_DIR = Path("web_results")
RESULTS_DIR.mkdir(exist_ok=True)

KEYWORDS_FILE = RESULTS_DIR / "keywords.json"


def _load_keywords() -> dict:
    if KEYWORDS_FILE.exists():
        with open(KEYWORDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_keyword(keyword: str) -> None:
    keywords = _load_keywords()
    k = keyword.strip().lower()
    if k:
        keywords[k] = keywords.get(k, 0) + 1
        with open(KEYWORDS_FILE, "w", encoding="utf-8") as f:
            json.dump(keywords, f, ensure_ascii=False, indent=2)


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the web UI."""
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.post("/api/leads/search")
async def search_leads(
    keyword: str = Form(...),
    pages: int = Form(20),
    max_domains: Optional[int] = Form(None),
    exclude: str = Form(""),
    deep: bool = Form(False),
):
    """
    Run a lead search and return results.
    Synchronous — reasonable defaults keep it under ~15s.
    """
    job_id = str(uuid.uuid4())[:8]
    output_file = RESULTS_DIR / f"{job_id}.csv"

    cmd = [
        "python", "lead_finder.py", keyword,
        "--pages", str(pages),
        "--output", str(output_file),
    ]
    if max_domains is not None:
        cmd.extend(["--max-domains", str(max_domains)])
    if exclude:
        cmd.extend(["--exclude", exclude])
    if deep:
        cmd.append("--deep")

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=".", encoding="utf-8", errors="replace")

    if output_file.exists():
        _save_keyword(keyword)
        leads = []
        with open(output_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                leads.append({k: v for k, v in row.items()})

        return {
            "job_id": job_id,
            "status": "success",
            "total": len(leads),
            "download_url": f"/api/leads/download/{job_id}",
            "preview": leads[:5] if leads else [],
            "log": result.stdout[-500:] if result.stdout else "",
        }
    else:
        return {
            "job_id": job_id,
            "status": "error",
            "message": (result.stdout + "\n" + result.stderr)[-1000:],
        }


@app.get("/api/leads/stream")
async def stream_leads(
    keyword: str,
    pages: int = 20,
    max_domains: Optional[int] = None,
    exclude: str = "",
    deep: bool = False,
):
    """Stream lead search progress via Server-Sent Events."""
    job_id = str(uuid.uuid4())[:8]
    output_file = RESULTS_DIR / f"{job_id}.csv"

    cmd = [
        sys.executable or "python", "lead_finder.py", keyword,
        "--pages", str(pages),
        "--output", str(output_file),
    ]
    if max_domains is not None:
        cmd.extend(["--max-domains", str(max_domains)])
    if exclude:
        cmd.extend(["--exclude", exclude])
    if deep:
        cmd.append("--deep")

    async def event_generator():
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    payload = json.dumps({"type": "log", "content": text}, ensure_ascii=False)
                    yield f"data: {payload}\n\n"

            await proc.wait()

            if output_file.exists():
                _save_keyword(keyword)
                leads = []
                with open(output_file, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        leads.append({k: v for k, v in row.items()})

                payload = json.dumps({
                    "type": "done",
                    "status": "success",
                    "job_id": job_id,
                    "total": len(leads),
                    "download_url": f"/api/leads/download/{job_id}",
                    "preview": leads[:5] if leads else [],
                }, ensure_ascii=False)
                yield f"data: {payload}\n\n"
            else:
                payload = json.dumps({
                    "type": "done",
                    "status": "error",
                    "message": "Search completed but no output file was generated.",
                }, ensure_ascii=False)
                yield f"data: {payload}\n\n"
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.get("/api/leads/download/{job_id}")
async def download_leads(job_id: str):
    """Download the CSV result file."""
    file_path = RESULTS_DIR / f"{job_id}.csv"
    if file_path.exists():
        return FileResponse(
            file_path,
            filename=f"leads_{job_id}.csv",
            media_type="text/csv"
        )
    return {"error": "File not found"}


@app.get("/api/keywords")
async def get_keywords():
    """Return searched keywords sorted by frequency."""
    keywords = _load_keywords()
    sorted_keywords = sorted(keywords.items(), key=lambda x: x[1], reverse=True)
    return {"keywords": [{"term": k, "count": v} for k, v in sorted_keywords]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
