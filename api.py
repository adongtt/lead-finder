#!/usr/bin/env python3
"""
Lead Finder Web API — FastAPI wrapper around lead_finder.py

Run locally:
    uvicorn api:app --reload --host 0.0.0.0 --port 8000

Deploy (single worker — dev only):
    uvicorn api:app --host 0.0.0.0 --port $PORT

Deploy (multi-worker — production):
    gunicorn api:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT --timeout 120 --keep-alive 5
"""

import asyncio
import csv
import io
import json
import os
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool as psycopg2_pool
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import bcrypt
from openpyxl import Workbook
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
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
).strip()
USERS_FILE = Path("users.json")

SESSION_SECRET = os.environ.get("SESSION_SECRET", "lead-finder-dev-secret-change-in-production")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=86400 * 7)

CONFIG_PATH = Path("config.yaml")


def _load_config_status():
    """Return which API keys are configured without exposing their values."""
    key_names = [
        "serpapi_key",
        "google_maps_key",
        "hunter_key",
        "snov_key",
        "apollo_key",
        "norbert_key",
        "zerobounce_key",
    ]
    config = {}
    source = "env"
    if CONFIG_PATH.exists():
        try:
            import yaml
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            source = "config.yaml"
        except Exception:
            pass

    status = {}
    for key in key_names:
        file_val = config.get(key, "")
        env_val = os.environ.get(key.upper(), "")
        value = file_val or env_val
        status[key.replace("_key", "")] = {
            "configured": bool(value),
            "source": source if file_val else "env",
        }
    return status

# ---------------------------------------------------------------------------
# Simple in-memory TTL cache for read-heavy endpoints (stats, searches)
# ---------------------------------------------------------------------------
_cache = {}
_CACHE_TTL = 30  # seconds

def _cache_get(key):
    entry = _cache.get(key)
    if entry and (datetime.now() - entry["ts"]).total_seconds() < _CACHE_TTL:
        return entry["value"]
    return None

def _cache_set(key, value):
    _cache[key] = {"value": value, "ts": datetime.now()}


# ---------------------------------------------------------------------------
# DB init
# ---------------------------------------------------------------------------

# Connection pool (min 1, max 10) — avoids reconnect overhead on every request
_db_pool = None

def _get_pool():
    global _db_pool
    if _db_pool is None:
        kwargs = {"cursor_factory": RealDictCursor}
        # Render/Railway/Neon production DBs require SSL. Local dev may not.
        # Set DISABLE_DB_SSL=1 to skip sslmode when the DSN itself doesn't specify it.
        disable_ssl = os.environ.get("DISABLE_DB_SSL", "").lower() in ("1", "true", "yes")
        if "sslmode" not in DATABASE_URL and not disable_ssl:
            kwargs["sslmode"] = "require"
        _db_pool = psycopg2_pool.ThreadedConnectionPool(
            minconn=1, maxconn=10, dsn=DATABASE_URL, **kwargs
        )
    return _db_pool

def _get_conn():
    return _get_pool().getconn()

def _put_conn(conn):
    try:
        _get_pool().putconn(conn)
    except Exception:
        pass


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
            source_type TEXT,
            csv_content TEXT
        )
    """)
    # Migrate existing searches table (add missing columns)
    for col in ("csv_content", "source_type"):
        try:
            c.execute(f"ALTER TABLE searches ADD COLUMN IF NOT EXISTS {col} TEXT")
        except Exception:
            pass

    # Backfill source_type for legacy rows based on keyword prefix
    try:
        c.execute("UPDATE searches SET source_type = 'apollo' WHERE source_type IS NULL AND keyword LIKE 'Apollo:%'")
        c.execute("UPDATE searches SET source_type = 'supplier_portal' WHERE source_type IS NULL AND keyword LIKE 'Supplier Portal:%'")
        c.execute("UPDATE searches SET source_type = 'keyword' WHERE source_type IS NULL OR source_type = ''")
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
            created_at TEXT,
            domain_alive BOOLEAN DEFAULT NULL,
            email_valid BOOLEAN DEFAULT NULL,
            company_active BOOLEAN DEFAULT NULL,
            last_verified_at TEXT DEFAULT '',
            org_structure_type TEXT DEFAULT '',
            parent_company_name TEXT DEFAULT '',
            parent_company_country TEXT DEFAULT '',
            hq_country TEXT DEFAULT '',
            purchasing_authority TEXT DEFAULT '',
            purchasing_authority_reason TEXT DEFAULT '',
            parent_org_data_source TEXT DEFAULT ''
        )
    """)

    # Migrate existing contacts table (add verification columns)
    for col in ("domain_alive", "email_valid", "company_active", "last_verified_at"):
        try:
            if col == "last_verified_at":
                c.execute(f"ALTER TABLE contacts ADD COLUMN IF NOT EXISTS {col} TEXT DEFAULT ''")
            else:
                c.execute(f"ALTER TABLE contacts ADD COLUMN IF NOT EXISTS {col} BOOLEAN DEFAULT NULL")
        except Exception:
            pass

    # Migrate existing contacts table (add parent company / purchasing authority columns)
    for col in (
        "org_structure_type", "parent_company_name", "parent_company_country",
        "hq_country", "purchasing_authority", "purchasing_authority_reason", "parent_org_data_source"
    ):
        try:
            c.execute(f"ALTER TABLE contacts ADD COLUMN IF NOT EXISTS {col} TEXT DEFAULT ''")
        except Exception:
            pass

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

    # Performance indexes for Render / production
    c.execute("CREATE INDEX IF NOT EXISTS idx_searches_user_id ON searches(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_searches_searched_at ON searches(searched_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_searches_source_type ON searches(source_type)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_contacts_user_id ON contacts(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_followups_email ON followups(contact_email)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_followups_user_id ON followups(user_id)")

    # Functional indexes for dashboard date aggregations on TEXT timestamp columns
    try:
        c.execute("CREATE INDEX IF NOT EXISTS idx_searches_searched_at_date ON searches((searched_at::date))")
        c.execute("CREATE INDEX IF NOT EXISTS idx_contacts_created_at_date ON contacts((created_at::date))")
    except Exception:
        pass

    conn.commit()
    _put_conn(conn)


def _migrate_json_to_postgres() -> None:
    """One-time migration from JSON files to PostgreSQL."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM contacts")
    if c.fetchone()["count"] > 0:
        _put_conn(conn)
        return  # Already has data, skip migration
    _put_conn(conn)

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
        _put_conn(conn)
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
        _put_conn(conn)
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
        _put_conn(conn)
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


@app.get("/api/users")
async def list_users(user: dict = Depends(require_admin)):
    """Return all users (admin only) for history filtering."""
    users = _load_users()
    return {
        "users": [
            {"user_id": uid, "name": info.get("name", uid), "role": info.get("role", "sales")}
            for uid, info in users.items()
        ]
    }


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def db_save_search(job_id: str, keyword: str, pages: int, total: int, user_id: str, user_name: str, deep: bool, csv_content: str = "", source_type: str = "") -> None:
    conn = _get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO searches (job_id, keyword, pages, total, user_id, user_name, searched_at, deep, source_type, csv_content)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (job_id, keyword, pages, total, user_id, user_name, datetime.now().isoformat(), 1 if deep else 0, source_type, csv_content))
    conn.commit()
    _put_conn(conn)
    # Invalidate search history cache so new records appear immediately
    _cache.clear()


def db_list_searches(
    user_id: str,
    role: str,
    keyword_filter: str = "",
    date_from: str = "",
    date_to: str = "",
    source_type: str = "",
    filter_user_id: str = "",
) -> list:
    conn = _get_conn()
    c = conn.cursor()

    where = []
    params = []
    if role == "admin":
        if filter_user_id:
            where.append("user_id = %s")
            params.append(filter_user_id)
    else:
        where.append("user_id = %s")
        params.append(user_id)

    if keyword_filter:
        where.append("keyword ILIKE %s")
        params.append(f"%{keyword_filter}%")
    if date_from:
        where.append("searched_at >= %s")
        params.append(date_from)
    if date_to:
        # Include the full day if only a date was provided.
        if len(date_to) == 10:
            date_to = date_to + "T23:59:59"
        where.append("searched_at <= %s")
        params.append(date_to)
    if source_type:
        where.append("source_type = %s")
        params.append(source_type)

    sql = """
        SELECT job_id, keyword, searched_at, total, source_type, user_id, user_name
        FROM searches
    """.strip()
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY searched_at DESC LIMIT 200"

    c.execute(sql, tuple(params))
    rows = c.fetchall()
    _put_conn(conn)
    results = []
    for row in rows:
        d = dict(row)
        d["deep"] = bool(d.get("deep"))
        results.append(d)
    return results


def db_get_search(job_id: str) -> Optional[dict]:
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM searches WHERE job_id = %s", (job_id,))
    row = c.fetchone()
    _put_conn(conn)
    if not row:
        return None
    d = dict(row)
    d["deep"] = bool(d.get("deep"))
    return d


def db_create_contact(
    email: str,
    domain: str,
    user_id: str,
    user_name: str,
    notes: str = "",
    org_structure_type: str = "",
    parent_company_name: str = "",
    parent_company_country: str = "",
    hq_country: str = "",
    purchasing_authority: str = "",
    purchasing_authority_reason: str = "",
    parent_org_data_source: str = "",
) -> None:
    conn = _get_conn()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("""
        INSERT INTO contacts (
            email, domain, user_id, user_name, status, next_follow_up, created_at,
            org_structure_type, parent_company_name, parent_company_country, hq_country,
            purchasing_authority, purchasing_authority_reason, parent_org_data_source
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(email) DO UPDATE SET
            domain = EXCLUDED.domain,
            user_id = EXCLUDED.user_id,
            user_name = EXCLUDED.user_name,
            status = EXCLUDED.status,
            next_follow_up = EXCLUDED.next_follow_up,
            created_at = EXCLUDED.created_at,
            org_structure_type = EXCLUDED.org_structure_type,
            parent_company_name = EXCLUDED.parent_company_name,
            parent_company_country = EXCLUDED.parent_company_country,
            hq_country = EXCLUDED.hq_country,
            purchasing_authority = EXCLUDED.purchasing_authority,
            purchasing_authority_reason = EXCLUDED.purchasing_authority_reason,
            parent_org_data_source = EXCLUDED.parent_org_data_source
    """, (
        email.lower().strip(), domain, user_id, user_name, "已发邮件", "", now,
        org_structure_type, parent_company_name, parent_company_country, hq_country,
        purchasing_authority, purchasing_authority_reason, parent_org_data_source,
    ))
    c.execute("""
        INSERT INTO followups (contact_email, action, notes, user_id, user_name, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (email.lower().strip(), "已发邮件", notes, user_id, user_name, now))
    conn.commit()
    _put_conn(conn)


def db_list_contacts(user_id: str, role: str) -> list:
    conn = _get_conn()
    c = conn.cursor()
    if role == "admin":
        c.execute("SELECT * FROM contacts ORDER BY created_at DESC")
    else:
        c.execute("SELECT * FROM contacts WHERE user_id = %s ORDER BY created_at DESC", (user_id,))
    rows = c.fetchall()
    _put_conn(conn)
    return [dict(r) for r in rows]


def db_get_contact(email: str) -> Optional[dict]:
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM contacts WHERE email = %s", (email.lower().strip(),))
    row = c.fetchone()
    _put_conn(conn)
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
    _put_conn(conn)


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
    _put_conn(conn)


def db_list_followups(email: str) -> list:
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM followups WHERE contact_email = %s ORDER BY created_at ASC", (email.lower().strip(),))
    rows = c.fetchall()
    _put_conn(conn)
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
    _put_conn(conn)
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
    _put_conn(conn)


def db_list_keywords() -> list:
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT term, count FROM keywords ORDER BY count DESC")
    rows = c.fetchall()
    _put_conn(conn)
    return [{"term": r["term"], "count": r["count"]} for r in rows]


def db_get_stats(user_id: str, role: str) -> dict:
    """Return dashboard stats for the current user (or all if admin)."""
    conn = _get_conn()
    c = conn.cursor()

    # Weekly leads (searches created this week)
    c.execute("""
        SELECT COALESCE(SUM(total), 0) as total
        FROM searches
        WHERE searched_at::timestamp >= date_trunc('week', now()::timestamp)
        AND (%s = 'admin' OR user_id = %s)
    """, (role, user_id))
    total_leads = c.fetchone()["total"] or 0

    # Contacted count
    c.execute("""
        SELECT COUNT(*) as cnt FROM contacts
        WHERE (%s = 'admin' OR user_id = %s)
    """, (role, user_id))
    contacted = c.fetchone()["cnt"] or 0

    # Won count
    c.execute("""
        SELECT COUNT(*) as cnt FROM contacts
        WHERE status = '成交'
        AND (%s = 'admin' OR user_id = %s)
    """, (role, user_id))
    won = c.fetchone()["cnt"] or 0

    # Follow-up needed (has next_follow_up in the future or no follow-up)
    c.execute("""
        SELECT COUNT(*) as cnt FROM contacts
        WHERE (next_follow_up IS NULL OR next_follow_up = '' OR next_follow_up >= %s)
        AND status NOT IN ('成交', '放弃')
        AND (%s = 'admin' OR user_id = %s)
    """, (datetime.now().isoformat(), role, user_id))
    followup = c.fetchone()["cnt"] or 0

    _put_conn(conn)
    return {
        "total_leads": total_leads,
        "contacted": contacted,
        "won": won,
        "followup": followup,
    }


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


@app.get("/history", response_class=HTMLResponse)
async def history_page(user: dict = Depends(require_user)):
    """Serve the search history page."""
    with open("static/history.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(user: dict = Depends(require_user)):
    """Serve the data dashboard page."""
    with open("static/dashboard.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/contacts", response_class=HTMLResponse)
async def contacts_page(user: dict = Depends(require_user)):
    """Serve the contacted customers / CRM page."""
    with open("static/contacts.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/templates", response_class=HTMLResponse)
async def templates_page(user: dict = Depends(require_user)):
    """Serve the quick search templates page."""
    with open("static/templates.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/tools", response_class=HTMLResponse)
async def tools_page(user: dict = Depends(require_user)):
    """Serve the batch tools page."""
    with open("static/tools.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(user: dict = Depends(require_user)):
    """Serve the system status / settings page."""
    with open("static/settings.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/feedback", response_class=HTMLResponse)
async def feedback_page(user: dict = Depends(require_user)):
    """Serve the issue feedback page."""
    with open("static/feedback.html", "r", encoding="utf-8") as f:
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


@app.get("/api/config/status")
async def config_status(user: dict = Depends(require_user)):
    """Return non-sensitive configuration / connectivity status."""
    key_status = _load_config_status()
    db_ok = False
    try:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT 1")
        c.fetchone()
        _put_conn(conn)
        db_ok = True
    except Exception:
        db_ok = False

    return {
        "config_source": "config.yaml" if CONFIG_PATH.exists() else "env",
        "keys": key_status,
        "database_connected": db_ok,
        "session_secret_default": SESSION_SECRET == "lead-finder-dev-secret-change-in-production",
    }


# ---------------------------------------------------------------------------
# Streaming helper: subprocess with keep-alive pings and hard timeout
# ---------------------------------------------------------------------------

async def _stream_subprocess(
    cmd: list,
    output_file: Path,
    job_id: str,
    timeout_seconds: int = 180,
    on_success=None,
):
    """Run a subprocess and yield SSE events with keep-alive pings.

    Args:
        cmd: subprocess command list.
        output_file: expected CSV output path.
        job_id: unique job id.
        timeout_seconds: hard wall-clock limit for the subprocess.
        on_success: async callable() invoked when CSV exists. Must return the
            SSE 'done' payload dict (usually with status='success').
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    start_time = time.time()
    PING_INTERVAL = 5.0
    rate_limited = False

    async def _read_one():
        line = await proc.stdout.readline()
        if not line:
            return None
        return line.decode("utf-8", errors="replace").rstrip()

    try:
        while True:
            now = time.time()
            if now - start_time > timeout_seconds:
                raise asyncio.TimeoutError()

            try:
                text = await asyncio.wait_for(_read_one(), timeout=PING_INTERVAL)
            except asyncio.TimeoutError:
                # No output for a whole ping interval; emit keep-alive so proxies/browsers don't drop us.
                ping = json.dumps({"type": "ping", "elapsed": int(now - start_time)}, ensure_ascii=False)
                yield f"data: {ping}\n\n"
                continue

            if text is None:
                break

            if text:
                if "[RATE_LIMIT]" in text:
                    rate_limited = True
                payload = json.dumps({"type": "log", "content": text}, ensure_ascii=False)
                yield f"data: {payload}\n\n"

        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

        if proc.returncode != 0 and not output_file.exists():
            if rate_limited:
                payload = json.dumps({
                    "type": "done",
                    "status": "error",
                    "error_type": "rate_limit",
                    "message": "SerpAPI 请求过于频繁（429）。已自动降级到 DuckDuckGo，如果仍失败请稍后重试，或考虑升级 SerpAPI 套餐。",
                }, ensure_ascii=False)
            else:
                payload = json.dumps({
                    "type": "done",
                    "status": "error",
                    "error_type": "subprocess_error",
                    "message": f"搜索进程异常退出（返回码 {proc.returncode}），未生成结果文件。",
                }, ensure_ascii=False)
            yield f"data: {payload}\n\n"
            return

        if output_file.exists():
            if on_success is not None:
                payload = await on_success()
            else:
                payload = {"type": "done", "status": "success", "job_id": job_id}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        else:
            if rate_limited:
                payload = json.dumps({
                    "type": "done",
                    "status": "error",
                    "error_type": "rate_limit",
                    "message": "SerpAPI 请求过于频繁（429），且未找到其他可用结果。建议切换搜索引擎为 DuckDuckGo，或稍后重试。",
                }, ensure_ascii=False)
            else:
                payload = json.dumps({
                    "type": "done",
                    "status": "error",
                    "error_type": "no_output",
                    "message": "搜索已完成，但未生成结果文件（可能没有匹配结果）。",
                }, ensure_ascii=False)
            yield f"data: {payload}\n\n"

    except asyncio.TimeoutError:
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
        payload = json.dumps({
            "type": "done",
            "status": "error",
            "error_type": "timeout",
            "message": f"搜索超时（超过 {timeout_seconds} 秒）。建议减少 pages / max_results，或稍后重试。",
        }, ensure_ascii=False)
        yield f"data: {payload}\n\n"

    except Exception as exc:
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
        payload = json.dumps({
            "type": "done",
            "status": "error",
            "error_type": "server_error",
            "message": f"服务端错误: {exc}",
        }, ensure_ascii=False)
        yield f"data: {payload}\n\n"


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
    amazon: bool = Form(False),
    maps_region: str = Form(""),
    keep_no_email: bool = Form(False),
    exclude_big_platforms: bool = Form(False),
    advanced_syntax: bool = Form(False),
    strict_mode: bool = Form(False),
    min_relevance: int = Form(-50),
    scan_supplier_pages: bool = Form(False),
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
    if amazon:
        cmd.append("--amazon")
    if maps_region:
        cmd.extend(["--maps-region", maps_region])
    if keep_no_email:
        cmd.append("--keep-no-email")
    if exclude_big_platforms:
        cmd.append("--exclude-big-platforms")
    if advanced_syntax:
        cmd.append("--advanced-syntax")
    if strict_mode:
        cmd.append("--strict-mode")
    if min_relevance != -50:
        cmd.extend(["--min-relevance", str(min_relevance)])
    if scan_supplier_pages:
        cmd.append("--scan-supplier-pages")

    SYNC_SEARCH_TIMEOUT = int(os.environ.get("SYNC_SEARCH_TIMEOUT", "120"))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=".",
            encoding="utf-8",
            errors="replace",
            timeout=SYNC_SEARCH_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "job_id": job_id,
            "status": "error",
            "error_type": "timeout",
            "message": f"搜索超时（超过 {SYNC_SEARCH_TIMEOUT} 秒）。请改用流式搜索，或减少 pages / max_domains。",
            "log": (exc.stdout or "")[-500:] if exc.stdout else "",
        }
    except Exception as exc:
        return {
            "job_id": job_id,
            "status": "error",
            "error_type": "subprocess_error",
            "message": f"启动搜索进程失败: {exc}",
            "log": "",
        }

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
        source_type = "google_maps" if maps_region else "keyword"
        db_save_search(job_id, keyword, pages, len(leads), user["user_id"], user["name"], deep, csv_content, source_type=source_type)

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


@app.post("/api/leads/domains")
async def search_domains(
    domains: str = Form(...),
    keep_no_email: bool = Form(False),
    scan_supplier_pages: bool = Form(False),
    user: dict = Depends(require_user),
):
    """Run a bulk domain import search synchronously."""
    job_id = str(uuid.uuid4())[:8]
    output_file = DATA_DIR / f"{job_id}.csv"

    cmd = [
        sys.executable or "python", "lead_finder.py", "bulk-domain-search",
        "--domains", domains.replace("\n", ",").replace("\r", ""),
        "--output", str(output_file),
    ]
    if keep_no_email:
        cmd.append("--keep-no-email")
    if scan_supplier_pages:
        cmd.append("--scan-supplier-pages")

    SYNC_SEARCH_TIMEOUT = int(os.environ.get("SYNC_SEARCH_TIMEOUT", "120"))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=".",
            encoding="utf-8",
            errors="replace",
            timeout=SYNC_SEARCH_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "job_id": job_id,
            "status": "error",
            "error_type": "timeout",
            "message": f"批量域名搜索超时（超过 {SYNC_SEARCH_TIMEOUT} 秒）。请减少域名数量或使用流式搜索。",
            "log": (exc.stdout or "")[-500:] if exc.stdout else "",
        }
    except Exception as exc:
        return {
            "job_id": job_id,
            "status": "error",
            "error_type": "subprocess_error",
            "message": f"启动批量域名搜索失败: {exc}",
            "log": "",
        }

    if output_file.exists():
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
        db_save_search(job_id, f"Bulk domains ({len(domains.splitlines())} rows)", 0, len(leads), user["user_id"], user["name"], False, csv_content, source_type="domains")

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


@app.post("/api/leads/verify/file")
async def verify_file(
    file: UploadFile = File(...),
    verify_domain: bool = Form(True),
    verify_email: bool = Form(True),
    verify_company: bool = Form(True),
    user: dict = Depends(require_user),
):
    """Upload a CSV and run batch verification synchronously."""
    job_id = str(uuid.uuid4())[:8]
    input_file = DATA_DIR / f"{job_id}_verify_in.csv"
    output_file = DATA_DIR / f"{job_id}.csv"

    content = await file.read()
    input_file.write_bytes(content)

    cmd = [
        sys.executable or "python", "lead_finder.py",
        "--verify-csv", str(input_file),
        "--verify-output", str(output_file),
        "--verify-workers", "6",
    ]
    if verify_domain:
        cmd.append("--verify-domain")
    if verify_email:
        cmd.append("--verify-email")
    if verify_company:
        cmd.append("--verify-company")

    VERIFY_TIMEOUT = int(os.environ.get("SYNC_VERIFY_TIMEOUT", "300"))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=".",
            encoding="utf-8",
            errors="replace",
            timeout=VERIFY_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "job_id": job_id,
            "status": "error",
            "error_type": "timeout",
            "message": f"批量验证超时（超过 {VERIFY_TIMEOUT} 秒）。请减少 CSV 行数。",
            "log": (exc.stdout or "")[-500:] if exc.stdout else "",
        }
    except Exception as exc:
        return {
            "job_id": job_id,
            "status": "error",
            "error_type": "subprocess_error",
            "message": f"启动验证失败: {exc}",
            "log": "",
        }

    if output_file.exists():
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
        db_save_search(job_id, f"Verified: uploaded CSV", 0, len(leads), user["user_id"], user["name"], False, csv_content, source_type="verified")

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
    amazon: bool = False,
    maps_region: str = "",
    keep_no_email: bool = False,
    exclude_big_platforms: bool = False,
    advanced_syntax: bool = False,
    strict_mode: bool = False,
    min_relevance: int = -50,
    scan_supplier_pages: bool = False,
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
    if amazon:
        cmd.append("--amazon")
    if maps_region:
        cmd.extend(["--maps-region", maps_region])
    if keep_no_email:
        cmd.append("--keep-no-email")
    if exclude_big_platforms:
        cmd.append("--exclude-big-platforms")
    if advanced_syntax:
        cmd.append("--advanced-syntax")
    if strict_mode:
        cmd.append("--strict-mode")
    if min_relevance != -50:
        cmd.extend(["--min-relevance", str(min_relevance)])
    if scan_supplier_pages:
        cmd.append("--scan-supplier-pages")

    async def event_generator():
        async def on_success():
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
            if maps_region:
                source_type = "google_maps"
            elif domains:
                source_type = "domains"
            else:
                source_type = "keyword"
            db_save_search(job_id, keyword, pages, len(leads), user["user_id"], user["name"], deep, csv_content, source_type=source_type)
            db_increment_keyword(keyword)
            return {
                "type": "done",
                "status": "success",
                "job_id": job_id,
                "total": len(leads),
                "download_url": f"/api/leads/download/{job_id}",
                "preview": leads[:5] if leads else [],
                "leads": leads,
            }

        async for event in _stream_subprocess(
            cmd,
            output_file,
            job_id,
            timeout_seconds=int(os.environ.get("STREAM_SEARCH_TIMEOUT", "180")),
            on_success=on_success,
        ):
            yield event

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.get("/api/leads/apollo/stream")
async def stream_apollo_leads(
    request: Request,
    apollo_keywords: str = "",
    apollo_domains: str = "",
    apollo_titles: str = "Buyer,Purchasing Manager,Procurement Manager,Sourcing Manager",
    apollo_country: str = "",
    apollo_state: str = "",
    apollo_city: str = "",
    apollo_zip: str = "",
    max_results: int = 100,
    keep_no_email: bool = False,
    employee_range: str = "",
    scan_supplier_pages: bool = False,
    min_relevance: int = 0,
    strict_mode: bool = False,
    user: dict = Depends(require_user),
):
    """Stream Apollo.io People Search progress via Server-Sent Events."""
    if not apollo_keywords and not apollo_domains:
        raise HTTPException(status_code=400, detail="Either apollo_keywords or apollo_domains is required.")

    job_id = str(uuid.uuid4())[:8]
    output_file = DATA_DIR / f"{job_id}.csv"

    cmd = [
        sys.executable or "python", "lead_finder.py", "apollo-search",
        "--apollo-titles", apollo_titles,
        "--output", str(output_file),
    ]
    if apollo_keywords:
        cmd.extend(["--apollo-keywords", apollo_keywords])
    if apollo_domains:
        cmd.extend(["--apollo-domains", apollo_domains])
    if apollo_country:
        cmd.extend(["--apollo-country", apollo_country])
    if apollo_state:
        cmd.extend(["--apollo-state", apollo_state])
    if apollo_city:
        cmd.extend(["--apollo-city", apollo_city])
    if apollo_zip:
        cmd.extend(["--apollo-zip", apollo_zip])
    if employee_range:
        cmd.extend(["--apollo-employee-range", employee_range])
    if max_results:
        cmd.extend(["--max-domains", str(max_results)])
    if keep_no_email:
        cmd.append("--keep-no-email")
    if scan_supplier_pages:
        cmd.append("--scan-supplier-pages")
    if min_relevance is not None:
        cmd.extend(["--apollo-min-relevance", str(min_relevance)])
    if strict_mode:
        cmd.append("--apollo-strict-mode")

    async def event_generator():
        async def on_success():
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
            search_label = f"Apollo domains: {apollo_domains}" if apollo_domains else f"Apollo: {apollo_keywords}"
            db_save_search(job_id, search_label, 0, len(leads), user["user_id"], user["name"], False, csv_content, source_type="apollo")
            return {
                "type": "done",
                "status": "success",
                "job_id": job_id,
                "total": len(leads),
                "download_url": f"/api/leads/download/{job_id}",
                "preview": leads[:5] if leads else [],
                "leads": leads,
            }

        async for event in _stream_subprocess(
            cmd,
            output_file,
            job_id,
            timeout_seconds=int(os.environ.get("STREAM_APOLLO_TIMEOUT", "240")),
            on_success=on_success,
        ):
            yield event

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.get("/api/leads/supplier-portal/stream")
async def stream_supplier_portal_scan(
    request: Request,
    domains: str,
    user: dict = Depends(require_user),
):
    """Stream supplier portal page scan progress via Server-Sent Events."""
    if not domains:
        raise HTTPException(status_code=400, detail="domains parameter is required.")

    job_id = str(uuid.uuid4())[:8]
    output_file = DATA_DIR / f"{job_id}.csv"

    cmd = [
        sys.executable or "python", "lead_finder.py",
        "--supplier-portal-domains", domains,
        "--output", str(output_file),
    ]

    async def event_generator():
        async def on_success():
            csv_content = output_file.read_text(encoding="utf-8")
            leads = []
            lines = csv_content.splitlines()
            if lines:
                lines[0] = lines[0].lstrip('﻿')
            reader = csv.DictReader(lines)
            for row in reader:
                leads.append({k: v for k, v in row.items()})
            leads = db_enrich_leads(leads)
            db_save_search(job_id, f"Supplier Portal: {domains[:50]}", 0, len(leads), user["user_id"], user["name"], False, csv_content, source_type="supplier_portal")
            return {
                "type": "done",
                "status": "success",
                "job_id": job_id,
                "total": len(leads),
                "download_url": f"/api/leads/download/{job_id}",
                "preview": leads[:5] if leads else [],
                "leads": leads,
            }

        async for event in _stream_subprocess(
            cmd,
            output_file,
            job_id,
            timeout_seconds=int(os.environ.get("STREAM_SUPPLIER_TIMEOUT", "120")),
            on_success=on_success,
        ):
            yield event

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.get("/api/leads/verify/stream")
async def stream_verify_leads(
    request: Request,
    job_id: str,
    verify_domain: bool = True,
    verify_email: bool = True,
    verify_company: bool = True,
    user: dict = Depends(require_user),
):
    """Stream batch verification of an existing search result via SSE.

    Reads the CSV from the searches table, runs the selected verification checks,
    and returns an updated CSV as a new search record with source_type='verified'.
    """
    search = db_get_search(job_id)
    if not search:
        raise HTTPException(status_code=404, detail="搜索记录不存在")
    if user["role"] != "admin" and search.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权验证此记录")

    csv_content = search.get("csv_content", "")
    if not csv_content:
        raise HTTPException(status_code=400, detail="该搜索记录没有 CSV 内容可供验证")

    new_job_id = str(uuid.uuid4())[:8]
    input_file = DATA_DIR / f"{new_job_id}_verify_in.csv"
    output_file = DATA_DIR / f"{new_job_id}.csv"
    input_file.write_text(csv_content, encoding="utf-8")

    cmd = [
        sys.executable or "python", "lead_finder.py",
        "--verify-csv", str(input_file),
        "--verify-output", str(output_file),
        "--verify-workers", "6",
    ]
    if verify_domain:
        cmd.append("--verify-domain")
    if verify_email:
        cmd.append("--verify-email")
    if verify_company:
        cmd.append("--verify-company")

    async def event_generator():
        async def on_success():
            new_csv_content = output_file.read_text(encoding="utf-8")
            leads = []
            lines = new_csv_content.splitlines()
            if lines:
                lines[0] = lines[0].lstrip('﻿')
            reader = csv.DictReader(lines)
            for row in reader:
                leads.append({k: v for k, v in row.items()})
            leads = db_enrich_leads(leads)
            leads = _sort_leads_by_position(leads)

            original_keyword = search.get("keyword") or "verified"
            db_save_search(
                new_job_id,
                f"Verified: {original_keyword}"[:200],
                search.get("pages", 0) or 0,
                len(leads),
                user["user_id"],
                user["name"],
                bool(search.get("deep")),
                new_csv_content,
                source_type="verified",
            )
            return {
                "type": "done",
                "status": "success",
                "job_id": new_job_id,
                "total": len(leads),
                "download_url": f"/api/leads/download/{new_job_id}",
                "preview": leads[:5] if leads else [],
                "leads": leads,
            }

        async for event in _stream_subprocess(
            cmd,
            output_file,
            new_job_id,
            timeout_seconds=int(os.environ.get("STREAM_VERIFY_TIMEOUT", "300")),
            on_success=on_success,
        ):
            yield event

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


@app.get("/api/dashboard")
async def get_dashboard(user: dict = Depends(require_user)):
    """Return comprehensive dashboard analytics (admin = all, sales = own)."""
    cache_key = f"dashboard:{user['user_id']}:{user['role']}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    conn = _get_conn()
    c = conn.cursor()
    role = user["role"]
    uid = user["user_id"]
    now = datetime.now()
    now_iso = now.isoformat()
    week_start = now - timedelta(days=now.weekday())

    # KPI: weekly leads
    c.execute("""
        SELECT COALESCE(SUM(total), 0) AS total
        FROM searches
        WHERE searched_at >= %s
        AND (%s = 'admin' OR user_id = %s)
    """, (week_start.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(), role, uid))
    weekly_leads = int(c.fetchone()["total"] or 0)

    # KPI: contacted
    c.execute("""
        SELECT COUNT(*) AS cnt FROM contacts
        WHERE (%s = 'admin' OR user_id = %s)
    """, (role, uid))
    contacted = int(c.fetchone()["cnt"] or 0)

    # KPI: won
    c.execute("""
        SELECT COUNT(*) AS cnt FROM contacts
        WHERE status = '成交'
        AND (%s = 'admin' OR user_id = %s)
    """, (role, uid))
    won = int(c.fetchone()["cnt"] or 0)

    # KPI: follow-up needed (no next date or next date >= now, excluding 成交/放弃)
    c.execute("""
        SELECT COUNT(*) AS cnt FROM contacts
        WHERE status NOT IN ('成交', '放弃')
        AND (next_follow_up IS NULL OR next_follow_up = '' OR next_follow_up >= %s)
        AND (%s = 'admin' OR user_id = %s)
    """, (now_iso, role, uid))
    followup = int(c.fetchone()["cnt"] or 0)

    # Trend: last 30 days
    c.execute("""
        SELECT searched_at::date AS day, COALESCE(SUM(total), 0) AS leads
        FROM searches
        WHERE searched_at >= (now() - interval '30 days')::text
        AND (%s = 'admin' OR user_id = %s)
        GROUP BY searched_at::date
        ORDER BY day
    """, (role, uid))
    trend_rows = c.fetchall()

    c.execute("""
        SELECT created_at::date AS day, COUNT(*) AS cnt
        FROM contacts
        WHERE created_at >= (now() - interval '30 days')::text
        AND (%s = 'admin' OR user_id = %s)
        GROUP BY created_at::date
        ORDER BY day
    """, (role, uid))
    contacted_trend_rows = c.fetchall()

    # Source distribution
    c.execute("""
        SELECT source_type, COALESCE(SUM(total), 0) AS total
        FROM searches
        WHERE (%s = 'admin' OR user_id = %s)
        GROUP BY source_type
        ORDER BY total DESC
    """, (role, uid))
    source_rows = c.fetchall()

    # Conversion funnel by status
    c.execute("""
        SELECT status, COUNT(*) AS cnt
        FROM contacts
        WHERE (%s = 'admin' OR user_id = %s)
        GROUP BY status
        ORDER BY cnt DESC
    """, (role, uid))
    status_rows = c.fetchall()

    # Top keywords (global)
    c.execute("SELECT term, count FROM keywords ORDER BY count DESC LIMIT 10")
    keyword_rows = c.fetchall()

    # Follow-up calendar: next 14 days
    c.execute("""
        SELECT email, domain, status, next_follow_up
        FROM contacts
        WHERE next_follow_up IS NOT NULL AND next_follow_up != ''
        AND next_follow_up >= %s
        AND next_follow_up <= (now() + interval '14 days')::text
        AND status NOT IN ('成交', '放弃')
        AND (%s = 'admin' OR user_id = %s)
        ORDER BY next_follow_up
    """, (now_iso, role, uid))
    calendar_rows = c.fetchall()

    # Quality: searches & leads per source
    c.execute("""
        SELECT source_type, COUNT(*) AS searches, COALESCE(SUM(total), 0) AS leads
        FROM searches
        WHERE (%s = 'admin' OR user_id = %s)
        GROUP BY source_type
        ORDER BY leads DESC
    """, (role, uid))
    quality_rows = c.fetchall()

    # Top users (admin only)
    top_users = []
    if role == "admin":
        c.execute("""
            SELECT user_id, user_name, COUNT(*) AS searches, COALESCE(SUM(total), 0) AS leads
            FROM searches
            GROUP BY user_id, user_name
            ORDER BY leads DESC
            LIMIT 10
        """)
        top_users = [dict(r) for r in c.fetchall()]

    _put_conn(conn)

    # Build full 30-day date range with zeros for missing days
    today = now.date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
    leads_map = {str(r["day"]): int(r["leads"] or 0) for r in trend_rows}
    contacted_map = {str(r["day"]): int(r["cnt"] or 0) for r in contacted_trend_rows}

    payload = {
        "kpi": {
            "total_leads": weekly_leads,
            "contacted": contacted,
            "won": won,
            "followup": followup,
        },
        "trend": {
            "labels": days,
            "leads": [leads_map.get(d, 0) for d in days],
            "contacted": [contacted_map.get(d, 0) for d in days],
        },
        "sources": [
            {"type": r["source_type"] or "unknown", "total": int(r["total"] or 0)}
            for r in source_rows
        ],
        "funnel": [
            {"status": r["status"] or "未分类", "count": int(r["cnt"] or 0)}
            for r in status_rows
        ],
        "keywords": [
            {"term": r["term"], "count": int(r["count"] or 0)}
            for r in keyword_rows
        ],
        "calendar": [
            {
                "email": r["email"],
                "domain": r["domain"] or "",
                "status": r["status"] or "",
                "next": r["next_follow_up"],
            }
            for r in calendar_rows
        ],
        "quality": [
            {"source": r["source_type"] or "unknown", "searches": int(r["searches"]), "leads": int(r["leads"])}
            for r in quality_rows
        ],
        "top_users": top_users,
    }

    _cache_set(cache_key, payload)
    return payload


@app.get("/api/stats")
async def get_stats(user: dict = Depends(require_user)):
    """Return dashboard stats: weekly leads, contacted, won, follow-up."""
    cache_key = f"stats:{user['user_id']}:{user['role']}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    stats = db_get_stats(user["user_id"], user["role"])
    _cache_set(cache_key, stats)
    return stats


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
    org_structure_type: str = Form(""),
    parent_company_name: str = Form(""),
    parent_company_country: str = Form(""),
    hq_country: str = Form(""),
    purchasing_authority: str = Form(""),
    purchasing_authority_reason: str = Form(""),
    parent_org_data_source: str = Form(""),
    user: dict = Depends(require_user),
):
    """Mark an email as contacted (auto-attributes to current user)."""
    db_create_contact(
        email, domain, user["user_id"], user["name"], notes,
        org_structure_type=org_structure_type,
        parent_company_name=parent_company_name,
        parent_company_country=parent_company_country,
        hq_country=hq_country,
        purchasing_authority=purchasing_authority,
        purchasing_authority_reason=purchasing_authority_reason,
        parent_org_data_source=parent_org_data_source,
    )
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


@app.post("/api/feedback")
async def post_feedback(
    request: Request,
    feedback_type: str = Form(...),
    description: str = Form(...),
    log: str = Form(""),
    user: dict = Depends(require_user),
):
    """Save user feedback to a local JSON file."""
    feedback_dir = Path("feedback")
    feedback_dir.mkdir(exist_ok=True)
    now = datetime.now().isoformat()
    safe_time = now.replace(":", "-").replace(".", "-")
    filename = feedback_dir / f"feedback_{safe_time}_{user['user_id']}.json"
    payload = {
        "created_at": now,
        "user_id": user["user_id"],
        "user_name": user.get("name", ""),
        "type": feedback_type,
        "description": description,
        "log": log,
    }
    filename.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"status": "ok", "id": filename.name}


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
    date_from: str = "",
    date_to: str = "",
    source_type: str = "",
    user_id: str = "",
    user: dict = Depends(require_user),
):
    """Return search history (admin sees all, sales sees own)."""
    cache_key = f"searches:{user['user_id']}:{user['role']}:{keyword}:{date_from}:{date_to}:{source_type}:{user_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    results = db_list_searches(
        user["user_id"],
        user["role"],
        keyword_filter=keyword,
        date_from=date_from,
        date_to=date_to,
        source_type=source_type,
        filter_user_id=user_id if user["role"] == "admin" else "",
    )
    out = {"searches": results}
    _cache_set(cache_key, out)
    return out


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
