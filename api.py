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
import io
import json
import os
import os
import psycopg2
from psycopg2.extras import RealDictCursor
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import bcrypt
from openpyxl import Workbook
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

app = FastAPI(title="B2B Lead Finder API", version="1.0")

# Serve static files (frontend)
app.mount("/static", StaticFiles(directory="static"), name="static")

DATA_DIR = Path(os.environ.get("DATA_DIR", "web_results"))
DATA_DIR.mkdir(exist_ok=True)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/leadfinder"
)
USERS_FILE = Path("users.json")

SESSION_SECRET = os.environ.get("SESSION_SECRET", "lead-finder-dev-secret-change-in-production")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=86400 * 7)


# ---------------------------------------------------------------------------
# DB init
# ---------------------------------------------------------------------------

def _get_conn():
    conn = psycopg2.connect(DATABASE_URL, sslmode="require", cursor_factory=RealDictCursor)
    return conn


def _init_db() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    conn = _get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS searches (
            id SERIAL PRIMARY KEY,
            job_id TEXT UNIQUE,
            keyword TEXT,
            pages INTEGER,
            total INTEGER,
            user_id TEXT,
            user_name TEXT,
            searched_at TEXT,
            deep INTEGER DEFAULT 0,
            csv_content TEXT
        )
    """)
    # Migrate existing searches table (add csv_content if missing)
    try:
        c.execute("ALTER TABLE searches ADD COLUMN IF NOT EXISTS csv_content TEXT")
    except Exception:
        pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE,
            domain TEXT,
            user_id TEXT,
            user_name TEXT,
            status TEXT DEFAULT '已发邮件',
            next_follow_up TEXT,
            created_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS followups (
            id SERIAL PRIMARY KEY,
            contact_email TEXT,
            action TEXT,
            notes TEXT,
            user_id TEXT,
            user_name TEXT,
            created_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS keywords (
            id SERIAL PRIMARY KEY,
            term TEXT UNIQUE,
            count INTEGER DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()


def _migrate_json_to_postgres() -> None:
    """One-time migration from JSON files to PostgreSQL."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM contacts")
    if c.fetchone()["count"] > 0:
        conn.close()
        return  # Already has data, skip migration
    conn.close()

    # Migrate searches
    searches_file = DATA_DIR / "searches.json"
    if searches_file.exists():
        with open(searches_file, "r", encoding="utf-8") as f:
            searches = json.load(f)
        conn = _get_conn()
        c = conn.cursor()
        for s in searches:
            c.execute("""
                INSERT INTO searches (job_id, keyword, pages, total, user_id, user_name, searched_at, deep)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(job_id) DO NOTHING
            """, (s.get("job_id"), s.get("keyword"), s.get("pages"), s.get("total"),
                  s.get("user_id"), s.get("user_name"), s.get("searched_at"), 1 if s.get("deep") else 0))
        conn.commit()
        conn.close()
        searches_file.rename(searches_file.with_suffix(".json.bak"))

    # Migrate contacts and followups
    contacted_file = DATA_DIR / "contacted.json"
    if contacted_file.exists():
        with open(contacted_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        conn = _get_conn()
        c = conn.cursor()
        for email, info in data.items():
            email = email.lower().strip()
            if "history" not in info:
                # Old flat format
                user_id = info.get("user_id", "")
                user_name = info.get("user_name", info.get("contacted_by", ""))
                created_at = info.get("contacted_at", datetime.now().isoformat())
                c.execute("""
                    INSERT INTO contacts (email, domain, user_id, user_name, status, next_follow_up, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(email) DO NOTHING
                """, (email, info.get("domain", ""), user_id, user_name, "已发邮件", "", created_at))
                c.execute("""
                    INSERT INTO followups (contact_email, action, notes, user_id, user_name, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (email, "已发邮件", info.get("notes", ""), user_id, user_name, created_at))
            else:
                # New CRM format
                c.execute("""
                    INSERT INTO contacts (email, domain, user_id, user_name, status, next_follow_up, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(email) DO NOTHING
                """, (email, info.get("domain", ""), info.get("user_id", ""), info.get("user_name", ""),
                      info.get("status", "已发邮件"), info.get("next_follow_up", ""),
                      info.get("created_at", datetime.now().isoformat())))
                for h in info.get("history", []):
                    c.execute("""
                        INSERT INTO followups (contact_email, action, notes, user_id, user_name, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (email, h.get("action"), h.get("notes", ""), h.get("user_id", ""), h.get("user_name", ""), h.get("at")))
        conn.commit()
        conn.close()
        contacted_file.rename(contacted_file.with_suffix(".json.bak"))

    # Migrate keywords
    keywords_file = DATA_DIR / "keywords.json"
    if keywords_file.exists():
        with open(keywords_file, "r", encoding="utf-8") as f:
            keywords = json.load(f)
        conn = _get_conn()
        c = conn.cursor()
        for term, count in keywords.items():
            c.execute("""
                INSERT INTO keywords (term, count)
                VALUES (%s, %s)
                ON CONFLICT(term) DO NOTHING
            """, (term, count))
        conn.commit()
        conn.close()
        keywords_file.rename(keywords_file.with_suffix(".json.bak"))


_init_db()
_migrate_json_to_postgres()


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
# DB helpers
# ---------------------------------------------------------------------------

def db_save_search(job_id: str, keyword: str, pages: int, total: int, user_id: str, user_name: str, deep: bool, csv_content: str = "") -> None:
    conn = _get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO searches (job_id, keyword, pages, total, user_id, user_name, searched_at, deep, csv_content)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (job_id, keyword, pages, total, user_id, user_name, datetime.now().isoformat(), 1 if deep else 0, csv_content))
    conn.commit()
    conn.close()


def db_list_searches(user_id: str, role: str, keyword_filter: str = "") -> list:
    conn = _get_conn()
    c = conn.cursor()
    if role == "admin":
        c.execute("SELECT * FROM searches ORDER BY searched_at DESC LIMIT 100")
    else:
        c.execute("SELECT * FROM searches WHERE user_id = %s ORDER BY searched_at DESC LIMIT 100", (user_id,))
    rows = c.fetchall()
    conn.close()
    results = []
    for row in rows:
        d = dict(row)
        d["deep"] = bool(d.get("deep"))
        if not keyword_filter or keyword_filter.lower() in (d.get("keyword") or "").lower():
            results.append(d)
    return results


def db_get_search(job_id: str) -> Optional[dict]:
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM searches WHERE job_id = %s", (job_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["deep"] = bool(d.get("deep"))
    return d


def db_create_contact(email: str, domain: str, user_id: str, user_name: str, notes: str = "") -> None:
    conn = _get_conn()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("""
        INSERT INTO contacts (email, domain, user_id, user_name, status, next_follow_up, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(email) DO UPDATE SET
            domain = EXCLUDED.domain,
            user_id = EXCLUDED.user_id,
            user_name = EXCLUDED.user_name,
            status = EXCLUDED.status,
            next_follow_up = EXCLUDED.next_follow_up,
            created_at = EXCLUDED.created_at
    """, (email.lower().strip(), domain, user_id, user_name, "已发邮件", "", now))
    c.execute("""
        INSERT INTO followups (contact_email, action, notes, user_id, user_name, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (email.lower().strip(), "已发邮件", notes, user_id, user_name, now))
    conn.commit()
    conn.close()


def db_list_contacts(user_id: str, role: str) -> list:
    conn = _get_conn()
    c = conn.cursor()
    if role == "admin":
        c.execute("SELECT * FROM contacts ORDER BY created_at DESC")
    else:
        c.execute("SELECT * FROM contacts WHERE user_id = %s ORDER BY created_at DESC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def db_get_contact(email: str) -> Optional[dict]:
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM contacts WHERE email = %s", (email.lower().strip(),))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    contact = dict(row)
    if not contact.get("email"):
        contact["email"] = email
    history = db_list_followups(email)
    for h in history:
        h.pop("contact_email", None)
    contact["history"] = history
    return contact


def db_update_contact_status(email: str, status: str) -> None:
    conn = _get_conn()
    c = conn.cursor()
    c.execute("UPDATE contacts SET status = %s WHERE email = %s", (status, email.lower().strip()))
    conn.commit()
    conn.close()


def db_add_followup(email: str, action: str, notes: str, next_follow_up: str, user_id: str, user_name: str) -> None:
    conn = _get_conn()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("""
        INSERT INTO followups (contact_email, action, notes, user_id, user_name, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (email.lower().strip(), action, notes, user_id, user_name, now))
    c.execute("UPDATE contacts SET status = %s WHERE email = %s", (action, email.lower().strip()))
    if next_follow_up:
        c.execute("UPDATE contacts SET next_follow_up = %s WHERE email = %s", (next_follow_up, email.lower().strip()))
    conn.commit()
    conn.close()


def db_list_followups(email: str) -> list:
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM followups WHERE contact_email = %s ORDER BY created_at ASC", (email.lower().strip(),))
    rows = c.fetchall()
    conn.close()
    results = []
    for r in rows:
        d = dict(r)
        d["at"] = d.pop("created_at")
        results.append(d)
    return results


def db_enrich_leads(leads: list) -> list:
    conn = _get_conn()
    c = conn.cursor()
    emails = [lead.get("email", "").lower().strip() for lead in leads if lead.get("email")]
    contact_map = {}
    if emails:
        placeholders = ",".join(["%s"] * len(emails))
        c.execute(f"SELECT email, user_name, status, created_at FROM contacts WHERE email IN ({placeholders})", emails)
        for r in c.fetchall():
            contact_map[r["email"]] = {"user_name": r["user_name"], "status": r["status"], "created_at": r["created_at"]}
    conn.close()
    for lead in leads:
        email = lead.get("email", "").lower().strip()
        info = contact_map.get(email)
        lead["contacted"] = bool(info)
        lead["contacted_at"] = info["created_at"] if info else ""
        lead["contacted_by"] = info["user_name"] if info else ""
        lead["status"] = info["status"] if info else ""
    return leads


# Position priority for sorting leads (higher priority = lower index)
_POSITION_PRIORITY = [
    "Buyer",
    "Senior Buyer",
    "Purchasing Manager",
    "Procurement Manager",
    "Sourcing Manager",
    "Global Sourcing Manager",
    "Merchandiser",
    "Merchandising Manager",
    "Category Manager",
    "Product Category Manager",
    "Commercial Director",
    "Sales & Commercial Director",
    "Sales and Commercial Director",
    "Product Manager",
    "Product Development Manager",
    "Brand Manager",
    "Marketing Manager",
    "Operations Manager",
    "Supply Chain Manager",
    "General Manager",
    "Managing Director",
]


def _position_priority(position: str) -> int:
    if not position:
        return 9999
    pos_lower = position.lower()
    for idx, title in enumerate(_POSITION_PRIORITY):
        if title.lower() in pos_lower:
            return idx
    return 9999


def _sort_leads_by_position(leads: list) -> list:
    return sorted(leads, key=lambda lead: (_position_priority(lead.get("position", "")), lead.get("email", "")))


def db_increment_keyword(term: str) -> None:
    conn = _get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO keywords (term, count) VALUES (%s, 1) ON CONFLICT(term) DO UPDATE SET count = keywords.count + 1", (term.strip().lower(),))
    conn.commit()
    conn.close()


def db_list_keywords() -> list:
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT term, count FROM keywords ORDER BY count DESC")
    rows = c.fetchall()
    conn.close()
    return [{"term": r["term"], "count": r["count"]} for r in rows]


# ---------------------------------------------------------------------------
# Page endpoints
# ---------------------------------------------------------------------------

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


@app.get("/result", response_class=HTMLResponse)
async def result_page(user: dict = Depends(require_user)):
    """Serve the search result detail page."""
    with open("static/result.html", "r", encoding="utf-8") as f:
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


# ---------------------------------------------------------------------------
# Search endpoints
# ---------------------------------------------------------------------------

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
    output_file = DATA_DIR / f"{job_id}.csv"

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
        db_increment_keyword(keyword)
        csv_content = output_file.read_text(encoding="utf-8")
        leads = []
        lines = csv_content.splitlines()
        if lines:
            lines[0] = lines[0].lstrip('﻿')
        reader = csv.DictReader(lines)
        for row in reader:
            leads.append({k: v for k, v in row.items()})
        leads = db_enrich_leads(leads)
        leads = _sort_leads_by_position(leads)
        db_save_search(job_id, keyword, pages, len(leads), user["user_id"], user["name"], deep, csv_content)

        return {
            "job_id": job_id,
            "status": "success",
            "total": len(leads),
            "download_url": f"/api/leads/download/{job_id}",
            "preview": leads[:5] if leads else [],
            "leads": leads,
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
    domains: str = "",
    target_tlds: str = "",
    user: dict = Depends(require_user),
):
    """Stream lead search progress via Server-Sent Events."""
    job_id = str(uuid.uuid4())[:8]
    output_file = DATA_DIR / f"{job_id}.csv"

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
    if domains:
        cmd.extend(["--domains", domains])
    if target_tlds:
        cmd.extend(["--target-tlds", target_tlds])

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
                db_increment_keyword(keyword)
                csv_content = output_file.read_text(encoding="utf-8")
                leads = []
                lines = csv_content.splitlines()
                if lines:
                    lines[0] = lines[0].lstrip('﻿')
                reader = csv.DictReader(lines)
                for row in reader:
                    leads.append({k: v for k, v in row.items()})
                leads = db_enrich_leads(leads)
                leads = _sort_leads_by_position(leads)
                db_save_search(job_id, keyword, pages, len(leads), user["user_id"], user["name"], deep, csv_content)

                payload = json.dumps({
                    "type": "done",
                    "status": "success",
                    "job_id": job_id,
                    "total": len(leads),
                    "download_url": f"/api/leads/download/{job_id}",
                    "preview": leads[:5] if leads else [],
                    "leads": leads,
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
    """Download the CSV result file from database."""
    search = db_get_search(job_id)
    if not search:
        raise HTTPException(status_code=404, detail="搜索记录不存在")
    if user["role"] != "admin" and search.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权下载")
    csv_content = search.get("csv_content", "")
    if not csv_content:
        # Fallback to local file for legacy searches
        file_path = DATA_DIR / f"{job_id}.csv"
        if file_path.exists():
            return FileResponse(
                file_path,
                filename=f"leads_{job_id}.csv",
                media_type="text/csv"
            )
        raise HTTPException(status_code=404, detail="CSV 内容不存在")
    from fastapi.responses import Response
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=leads_{job_id}.csv"}
    )


@app.get("/api/keywords")
async def get_keywords(user: dict = Depends(require_user)):
    """Return searched keywords sorted by frequency."""
    keywords = db_list_keywords()
    return {"keywords": keywords}


# ---------------------------------------------------------------------------
# Contacted CRM endpoints
# ---------------------------------------------------------------------------

@app.get("/api/contacted")
async def get_contacted(user: dict = Depends(require_user)):
    """Return contacted leads. Sales sees only their own; admin sees all."""
    contacts = db_list_contacts(user["user_id"], user["role"])
    return {"items": contacts}


@app.post("/api/contacted")
async def post_contacted(
    request: Request,
    email: str = Form(...),
    domain: str = Form(""),
    notes: str = Form(""),
    user: dict = Depends(require_user),
):
    """Mark an email as contacted (auto-attributes to current user)."""
    db_create_contact(email, domain, user["user_id"], user["name"], notes)
    return {"status": "ok", "email": email}


@app.get("/api/contacted/{email}")
async def get_contact_detail(email: str, user: dict = Depends(require_user)):
    """Get full follow-up history for a single contact."""
    contact = db_get_contact(email)
    if not contact:
        raise HTTPException(status_code=404, detail="客户不存在")
    if user["role"] != "admin" and contact.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权查看")
    return contact


@app.post("/api/contacted/{email}/followup")
async def post_followup(
    email: str,
    action: str = Form(...),
    notes: str = Form(""),
    next_follow_up: str = Form(""),
    user: dict = Depends(require_user),
):
    """Add a follow-up record and update status."""
    db_add_followup(email, action, notes, next_follow_up, user["user_id"], user["name"])
    return {"status": "ok"}


@app.put("/api/contacted/{email}/status")
async def put_status(
    email: str,
    status: str = Form(...),
    user: dict = Depends(require_user),
):
    """Directly update a contact's status."""
    contact = db_get_contact(email)
    if not contact:
        raise HTTPException(status_code=404, detail="客户不存在")
    if user["role"] != "admin" and contact.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权修改")
    db_update_contact_status(email, status)
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
    results = db_list_searches(user["user_id"], user["role"], keyword)
    return {"searches": results}


@app.get("/api/searches/{job_id}")
async def get_search_detail(job_id: str, user: dict = Depends(require_user)):
    """Re-load a past search result from database."""
    search = db_get_search(job_id)
    if not search:
        raise HTTPException(status_code=404, detail="搜索记录不存在")
    if user["role"] != "admin" and search.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权查看")

    csv_content = search.get("csv_content", "")
    if not csv_content:
        # Fallback to local file for legacy searches
        file_path = DATA_DIR / f"{job_id}.csv"
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="CSV 内容不存在")
        csv_content = file_path.read_text(encoding="utf-8")

    leads = []
    lines = csv_content.splitlines()
    if lines:
        lines[0] = lines[0].lstrip('﻿')
    reader = csv.DictReader(lines)
    for row in reader:
        leads.append({k: v for k, v in row.items()})
    leads = db_enrich_leads(leads)
    leads = _sort_leads_by_position(leads)

    return {
        "job_id": job_id,
        "keyword": search.get("keyword", ""),
        "total": len(leads),
        "download_url": f"/api/leads/download/{job_id}",
        "preview": leads[:5] if leads else [],
        "leads": leads,
    }


@app.get("/api/searches/{job_id}/excel")
async def download_search_excel(job_id: str, position: str = "", user: dict = Depends(require_user)):
    """Export a past search result as an Excel file. Optionally filter by position."""
    search = db_get_search(job_id)
    if not search:
        raise HTTPException(status_code=404, detail="搜索记录不存在")
    if user["role"] != "admin" and search.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权导出")

    csv_content = search.get("csv_content", "")
    if not csv_content:
        file_path = DATA_DIR / f"{job_id}.csv"
        if file_path.exists():
            csv_content = file_path.read_text(encoding="utf-8")
        else:
            raise HTTPException(status_code=404, detail="CSV 内容不存在")

    lines = csv_content.splitlines()
    if lines:
        lines[0] = lines[0].lstrip('﻿')
    reader = csv.DictReader(lines)
    rows = [row for row in reader]
    rows = db_enrich_leads(rows)
    rows = _sort_leads_by_position(rows)

    if position and position.strip():
        pos_lower = position.strip().lower()
        rows = [r for r in rows if pos_lower in (r.get("position") or "").lower()]

    header_map = {
        "email": "邮箱",
        "first_name": "名",
        "last_name": "姓",
        "position": "职位",
        "department": "部门",
        "company": "公司",
        "domain": "域名",
        "country": "国家",
        "confidence_score": "置信度",
        "email_type": "邮箱类型",
        "validation_status": "验证状态",
        "sources": "来源",
        "search_keyword": "搜索关键词",
        "found_at": "发现时间",
        "website_description": "网站简介",
        "relevance_score": "关联度",
        "linkedin_url": "LinkedIn",
        "status": "跟进状态",
        "contacted_by": "跟进人",
        "contacted_at": "跟进时间",
    }

    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"

    if rows:
        headers = []
        for key in rows[0].keys():
            headers.append(header_map.get(key, key))
        ws.append(headers)
        for row in rows:
            ws.append([row.get(k, "") for k in row.keys()])
    else:
        ws.append(["暂无数据"])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=leads_{job_id}.xlsx"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
