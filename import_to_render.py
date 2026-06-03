#!/usr/bin/env python3
"""Import sellgloves products into Render backend via API."""

import csv
import json
import os
import re
import sys
import time
from urllib.parse import urljoin

import requests

API_BASE = "https://fbgloves.onrender.com/api"
CSV_FILE = "sellgloves_football_gloves_clean.csv"


def parse_price(val: str) -> float | None:
    if not val:
        return None
    m = re.search(r"[\d.]+", val.replace(",", ""))
    return float(m.group()) if m else None


def build_product(row: dict) -> dict:
    """Convert CSV row to backend Product JSON."""
    price = parse_price(row.get("sale_price", ""))
    original_price = parse_price(row.get("original_price", ""))
    if price is None:
        price = 0.0

    # Images
    images = []
    if row.get("list_image"):
        images.append(row["list_image"].strip())
    if row.get("detail_images"):
        for img in row["detail_images"].split(","):
            img = img.strip()
            if img and img not in images:
                images.append(img)

    # Features
    features = []
    feature_val = row.get("feature", "") or row.get("features", "")
    if feature_val:
        for f in re.split(r"[,;]", feature_val):
            f = f.strip()
            if f:
                features.append(f)

    # Sizes
    sizes = []
    size_val = row.get("size", "")
    if size_val:
        for s in re.split(r"[,/]", size_val):
            s = s.strip()
            if s:
                sizes.append(s)
    if not sizes:
        sizes = ["S", "M", "L", "XL"]

    # Colors (heuristic)
    colors = []
    color_val = row.get("color", "")
    if color_val:
        for c in re.split(r"[,/]", color_val):
            c = c.strip()
            if c:
                colors.append({"name": c, "value": "#000000", "image": ""})

    # Specifications
    specs = {}
    mapping = {
        "material": "Material",
        "size": "Size",
        "color": "Color",
        "logo": "Logo",
        "packaging": "Packaging",
        "payment": "Payment Terms",
        "sample_time": "Sample Time",
        "production_time": "Production Time",
        "moq": "MOQ",
        "oem": "OEM",
        "gender": "Gender",
        "age": "Age",
        "sport": "Sport",
        "style": "Style",
        "type": "Type",
        "pad": "Pad",
        "design": "Design",
        "function": "Function",
        "application": "Application",
        "item": "Item",
        "season": "Season",
        "lead_time": "Lead Time",
        "model_no": "Model No.",
        "sample": "Sample",
        "delivery": "Delivery",
    }
    for key, label in mapping.items():
        val = row.get(key, "")
        if val:
            specs[label] = val

    return {
        "name": row.get("title", "").strip() or "Untitled Product",
        "category": "Football Gloves",
        "categoryId": "football-gloves",
        "price": price,
        "originalPrice": original_price,
        "rating": 0,
        "reviewCount": 0,
        "image": images[0] if images else "",
        "images": images,
        "description": row.get("description", "").strip(),
        "features": features,
        "specifications": specs,
        "sizes": sizes,
        "colors": colors,
        "inStock": True,
        "isBestseller": False,
        "isNew": False,
    }


def login(email: str, password: str) -> str:
    url = urljoin(API_BASE + "/", "auth/login")
    print(f"[LOGIN] POST {url}")
    resp = requests.post(url, json={"email": email, "password": password}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    token = data.get("token")
    if not token:
        raise RuntimeError(f"Login response missing token: {data}")
    print(f"[LOGIN] OK, got token")
    return token


def create_product(token: str, product: dict, index: int) -> bool:
    url = urljoin(API_BASE + "/", "products")
    headers = {"Authorization": f"Bearer {token}"}

    # API expects multipart/form-data with 'data' field containing JSON string
    # We don't upload files; image URLs are already inside the JSON
    files = {"data": (None, json.dumps(product, ensure_ascii=False), "application/json")}

    try:
        resp = requests.post(url, files=files, headers=headers, timeout=30)
        if resp.status_code == 201:
            print(f"  [{index}] Created: {product['name'][:50]}...")
            return True
        elif resp.status_code == 409:
            print(f"  [{index}] Already exists: {product['name'][:50]}...")
            return True
        else:
            print(f"  [{index}] FAILED ({resp.status_code}): {product['name'][:50]}...")
            try:
                print(f"       -> {resp.json()}")
            except Exception:
                print(f"       -> {resp.text[:200]}")
            return False
    except Exception as exc:
        print(f"  [{index}] ERROR: {exc}")
        return False


def main():
    email = os.environ.get("ADMIN_EMAIL", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "").strip()

    if not email:
        email = input("Admin email: ").strip()
    if not password:
        password = input("Admin password: ").strip()

    if not email or not password:
        print("Email and password are required.")
        sys.exit(1)

    # 1. Login
    try:
        token = login(email, password)
    except Exception as exc:
        print(f"[LOGIN] FAILED: {exc}")
        sys.exit(1)

    # 2. Load CSV
    print(f"\n[LOAD] Reading {CSV_FILE}...")
    rows = list(csv.DictReader(open(CSV_FILE, "r", encoding="utf-8-sig")))
    print(f"[LOAD] {len(rows)} products ready to import")

    # 3. Confirm
    if os.environ.get("AUTO_IMPORT") == "1":
        confirm = "yes"
        print(f"\n[AUTO] Importing {len(rows)} products to {API_BASE}...")
    else:
        confirm = input(f"\nImport {len(rows)} products to {API_BASE}? (yes/no): ").strip().lower()
    if confirm not in ("yes", "y"):
        print("Aborted.")
        sys.exit(0)

    # 4. Import
    print("\n[IMPORT] Starting...")
    success = 0
    failed = 0
    for i, row in enumerate(rows, 1):
        product = build_product(row)
        if create_product(token, product, i):
            success += 1
        else:
            failed += 1
        # Small delay to avoid rate limiting
        time.sleep(0.3)

    print(f"\n[DONE] Success: {success}, Failed: {failed}, Total: {len(rows)}")


if __name__ == "__main__":
    main()
