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
import os
import subprocess
import sys
import uuid
from pathlib import Path
import bcrypt
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

app = FastAPI(title="B2B Lead Finder API", version="1.0")

# Serve static files (frontend)
app.mount("/static", StaticFiles(directory="static"), name="static")

RESULTS_DIR = Path("web_results")
RESULTS_DIR.mkdir(exist_ok=True)

KEYWORDS_FILE = RESULTS_DIR / "keywords.json"
CONTACTED_FILE = RESULTS_DIR / "contacted.json"
SEARCHES_FILE = RESULTS_DIR / "searches.json"
USERS_FILE = Path("users.json")

SESSION_SECRET = os.environ.get("SESSION_SECRET", "lead-finder-dev-secret-change-in-production")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=86400 * 7)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _load_users() -> dict:
    if USERS_FILE.exists():
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def _get_current_user(request: Request) -> Optional[dict]:
    """Return user dict from session, or None."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    users = _load_users()
    user = users.get(user_id)
    if user:
        return {"user_id": user_id, "name": user.get("name", user_id), "role": user.get("role", "sales")}
    return None


def require_user(request: Request) -> dict:
    user = _get_current_user(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _load_contacted() -> dict:
    if CONTACTED_FILE.exists():
        with open(CONTACTED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _mark_contacted(email: str, domain: str, user_id: str, user_name: str, notes: str = "") -> None:
    contacted = _load_contacted()
    email = email.lower().strip()
    contacted[email] = {
        "domain": domain,
        "contacted_at": datetime.now().isoformat(),
        "user_id": user_id,
        "user_name": user_name,
        "notes": notes,
    }
    with open(CONTACTED_FILE, "w", encoding="utf-8") as f:
        json.dump(contacted, f, ensure_ascii=False, indent=2)


def _enrich_with_contacted(leads: list) -> list:
    """Add 'contacted' and 'contacted_at' keys to each lead dict."""
    contacted = _load_contacted()
    for lead in leads:
        email = lead.get("email", "").lower().strip()
        info = contacted.get(email)
        lead["contacted"] = bool(info)
        lead["contacted_at"] = info.get("contacted_at", "") if info else ""
        lead["contacted_by"] = info.get("user_name", "") if info else ""
    return leads


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


# ---------------------------------------------------------------------------
# Searches history
# ---------------------------------------------------------------------------

def _load_searches() -> list:
    if SEARCHES_FILE.exists():
        with open(SEARCHES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_search(job_id: str, keyword: str, pages: int, total: int, user_id: str, user_name: str, deep: bool) -> None:
    searches = _load_searches()
    searches.insert(0, {
        "job_id": job_id,
        "keyword": keyword,
        "pages": pages,
        "total": total,
        "user_id": user_id,
        "user_name": user_name,
        "searched_at": datetime.now().isoformat(),
        "deep": deep,
    })
    with open(SEARCHES_FILE, "w", encoding="utf-8") as f:
        json.dump(searches, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Contacted CRM (upgraded with status & history)
# ---------------------------------------------------------------------------

def _load_contacted() -> dict:
    if not CONTACTED_FILE.exists():
        return {}
    with open(CONTACTED_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Migrate old flat format to new CRM format
    migrated = {}
    for email, info in data.items():
        if "history" not in info:
            migrated[email] = {
                "domain": info.get("domain", ""),
                "user_id": info.get("user_id", ""),
                "user_name": info.get("user_name", info.get("contacted_by", "")),
                "status": "已发邮件",
                "history": [
                    {
                        "action": "已发邮件",
                        "at": info.get("contacted_at", datetime.now().isoformat()),
                        "notes": info.get("notes", ""),
                        "user_id": info.get("user_id", ""),
                        "user_name": info.get("user_name", info.get("contacted_by", "")),
                    }
                ],
                "next_follow_up": "",
                "created_at": info.get("contacted_at", datetime.now().isoformat()),
            }
        else:
            migrated[email] = info
    return migrated


def _mark_contacted(email: str, domain: str, user_id: str, user_name: str, notes: str = "") -> None:
    contacted = _load_contacted()
    email = email.lower().strip()
    now = datetime.now().isoformat()
    contacted[email] = {
        "domain": domain,
        "user_id": user_id,
        "user_name": user_name,
        "status": "已发邮件",
        "history": [
            {
                "action": "已发邮件",
                "at": now,
                "notes": notes,
                "user_id": user_id,
                "user_name": user_name,
            }
        ],
        "next_follow_up": "",
        "created_at": now,
    }
    with open(CONTACTED_FILE, "w", encoding="utf-8") as f:
        json.dump(contacted, f, ensure_ascii=False, indent=2)


def _add_followup(email: str, action: str, notes: str, next_follow_up: str, user_id: str, user_name: str) -> None:
    contacted = _load_contacted()
    email = email.lower().strip()
    if email not in contacted:
        raise HTTPException(status_code=404, detail="客户不存在")
    record = contacted[email]
    record["status"] = action
    record["history"].append({
        "action": action,
        "at": datetime.now().isoformat(),
        "notes": notes,
        "user_id": user_id,
        "user_name": user_name,
    })
    if next_follow_up:
        record["next_follow_up"] = next_follow_up
    with open(CONTACTED_FILE, "w", encoding="utf-8") as f:
        json.dump(contacted, f, ensure_ascii=False, indent=2)


def _enrich_with_contacted(leads: list) -> list:
    """Add 'contacted', 'contacted_at', 'contacted_by', 'status' keys to each lead dict."""
    contacted = _load_contacted()
    for lead in leads:
        email = lead.get("email", "").lower().strip()
        info = contacted.get(email)
        lead["contacted"] = bool(info)
        lead["contacted_at"] = info.get("created_at", "") if info else ""
        lead["contacted_by"] = info.get("user_name", "") if info else ""
        lead["status"] = info.get("status", "") if info else ""
    return leads


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the web UI (redirect to login if not authenticated)."""
    if not _get_current_user(request):
        return RedirectResponse(url="/login", status_code=302)
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """Serve the login page."""
    with open("static/login.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    """Authenticate and set session cookie."""
    users = _load_users()
    user = users.get(username.strip())
    if not user or not _verify_password(password, user.get("password_hash", "")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    request.session["user_id"] = username.strip()
    return {"status": "ok"}


@app.post("/logout")
async def logout(request: Request):
    """Clear session."""
    request.session.clear()
    return {"status": "ok"}


@app.get("/api/me")
async def me(request: Request):
    """Return current authenticated user."""
    user = _get_current_user(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    return user


@app.post("/api/leads/search")
async def search_leads(
    request: Request,
    keyword: str = Form(...),
    pages: int = Form(20),
    max_domains: Optional[int] = Form(None),
    exclude: str = Form(""),
    deep: bool = Form(False),
    user: dict = Depends(require_user),
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
        leads = _enrich_with_contacted(leads)
        _save_search(job_id, keyword, pages, len(leads), user["user_id"], user["name"], deep)

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
    request: Request,
    keyword: str,
    pages: int = 20,
    max_domains: Optional[int] = None,
    exclude: str = "",
    deep: bool = False,
    user: dict = Depends(require_user),
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
                leads = _enrich_with_contacted(leads)
                _save_search(job_id, keyword, pages, len(leads), user["user_id"], user["name"], deep)

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
async def download_leads(job_id: str, user: dict = Depends(require_user)):
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
async def get_keywords(user: dict = Depends(require_user)):
    """Return searched keywords sorted by frequency."""
    keywords = _load_keywords()
    sorted_keywords = sorted(keywords.items(), key=lambda x: x[1], reverse=True)
    return {"keywords": [{"term": k, "count": v} for k, v in sorted_keywords]}


# ---------------------------------------------------------------------------
# Contacted CRM endpoints
# ---------------------------------------------------------------------------

@app.get("/api/contacted")
async def get_contacted(user: dict = Depends(require_user)):
    """Return contacted leads. Sales sees only their own; admin sees all."""
    contacted = _load_contacted()
    items = []
    for email, info in sorted(contacted.items(), key=lambda x: x[1].get("created_at", ""), reverse=True):
        if user["role"] == "admin" or info.get("user_id") == user["user_id"]:
            items.append({"email": email, **info})
    return {"items": items}


@app.post("/api/contacted")
async def post_contacted(
    request: Request,
    email: str = Form(...),
    domain: str = Form(""),
    notes: str = Form(""),
    user: dict = Depends(require_user),
):
    """Mark an email as contacted (auto-attributes to current user)."""
    _mark_contacted(email, domain, user["user_id"], user["name"], notes)
    return {"status": "ok", "email": email}


@app.get("/api/contacted/{email}")
async def get_contact_detail(email: str, user: dict = Depends(require_user)):
    """Get full follow-up history for a single contact."""
    contacted = _load_contacted()
    email = email.lower().strip()
    info = contacted.get(email)
    if not info:
        raise HTTPException(status_code=404, detail="客户不存在")
    if user["role"] != "admin" and info.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权查看")
    return {"email": email, **info}


@app.post("/api/contacted/{email}/followup")
async def post_followup(
    email: str,
    action: str = Form(...),
    notes: str = Form(""),
    next_follow_up: str = Form(""),
    user: dict = Depends(require_user),
):
    """Add a follow-up record and update status."""
    _add_followup(email, action, notes, next_follow_up, user["user_id"], user["name"])
    return {"status": "ok"}


@app.put("/api/contacted/{email}/status")
async def put_status(
    email: str,
    status: str = Form(...),
    user: dict = Depends(require_user),
):
    """Directly update a contact's status."""
    contacted = _load_contacted()
    email = email.lower().strip()
    if email not in contacted:
        raise HTTPException(status_code=404, detail="客户不存在")
    record = contacted[email]
    if user["role"] != "admin" and record.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权修改")
    record["status"] = status
    with open(CONTACTED_FILE, "w", encoding="utf-8") as f:
        json.dump(contacted, f, ensure_ascii=False, indent=2)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Search history endpoints
# ---------------------------------------------------------------------------

@app.get("/api/searches")
async def get_searches(
    keyword: str = "",
    user: dict = Depends(require_user),
):
    """Return search history (admin sees all, sales sees own)."""
    searches = _load_searches()
    results = []
    for s in searches:
        if user["role"] == "admin" or s.get("user_id") == user["user_id"]:
            if not keyword or keyword.lower() in s.get("keyword", "").lower():
                results.append(s)
    return {"searches": results[:100]}


@app.get("/api/searches/{job_id}")
async def get_search_detail(job_id: str, user: dict = Depends(require_user)):
    """Re-load a past search result from its CSV."""
    searches = _load_searches()
    search = next((s for s in searches if s.get("job_id") == job_id), None)
    if not search:
        raise HTTPException(status_code=404, detail="搜索记录不存在")
    if user["role"] != "admin" and search.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权查看")

    file_path = RESULTS_DIR / f"{job_id}.csv"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="CSV 文件已删除")

    leads = []
    with open(file_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            leads.append({k: v for k, v in row.items()})
    leads = _enrich_with_contacted(leads)

    return {
        "job_id": job_id,
        "keyword": search.get("keyword", ""),
        "total": len(leads),
        "download_url": f"/api/leads/download/{job_id}",
        "preview": leads[:5] if leads else [],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
