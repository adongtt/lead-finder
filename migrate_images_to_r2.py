#!/usr/bin/env python3
"""Download external product images and re-upload to Cloudflare R2 via backend API."""

import io
import os
import sys
import time
from urllib.parse import urljoin, urlparse

import requests

API_BASE = "https://fbgloves.onrender.com/api"


def login(email: str, password: str) -> str:
    url = urljoin(API_BASE + "/", "auth/login")
    print(f"[LOGIN] POST {url}")
    resp = requests.post(url, json={"email": email, "password": password}, timeout=30)
    resp.raise_for_status()
    token = resp.json().get("token")
    if not token:
        raise RuntimeError("Login response missing token")
    print("[LOGIN] OK")
    return token


def fetch_products(token: str) -> list[dict]:
    url = urljoin(API_BASE + "/", "products")
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("products", [])


def download_image(url: str) -> tuple[bytes, str, str]:
    """Download image and return (content, filename, content_type)."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "image/jpeg")
    # Guess filename from URL
    parsed = urlparse(url)
    path = parsed.path
    filename = path.split("/")[-1] if "/" in path else "image.jpg"
    if "." not in filename:
        ext = content_type.split("/")[-1]
        if ext in ("jpeg", "jpg", "png", "gif", "webp"):
            filename += f".{ext}"
        else:
            filename += ".jpg"
    return resp.content, filename, content_type


def migrate_product_image(token: str, product: dict, index: int) -> bool:
    product_id = product["id"]
    image_url = product.get("image", "")

    if not image_url:
        print(f"  [{index}] SKIP (no image): {product['name'][:50]}...")
        return True

    # Skip if already R2 URL
    if "r2.dev" in image_url or "cloudflare" in image_url:
        print(f"  [{index}] SKIP (already R2): {product['name'][:50]}...")
        return True

    try:
        content, filename, content_type = download_image(image_url)
    except Exception as exc:
        print(f"  [{index}] DOWNLOAD FAILED: {product['name'][:50]}... -> {exc}")
        return False

    url = urljoin(API_BASE + "/", f"products/{product_id}")
    files = {
        "image": (filename, io.BytesIO(content), content_type),
    }
    # Send empty data JSON so only image gets updated
    files["data"] = (None, "{}", "application/json")

    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = requests.put(url, files=files, headers=headers, timeout=60)
        if resp.status_code in (200, 201):
            new_url = resp.json().get("product", {}).get("image", "")
            print(f"  [{index}] MIGRATED: {product['name'][:50]}... -> {new_url[:60]}...")
            return True
        else:
            print(f"  [{index}] API FAILED ({resp.status_code}): {product['name'][:50]}...")
            try:
                print(f"       -> {resp.json()}")
            except Exception:
                print(f"       -> {resp.text[:200]}")
            return False
    except Exception as exc:
        print(f"  [{index}] UPLOAD ERROR: {exc}")
        return False


def main():
    email = os.environ.get("ADMIN_EMAIL", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "").strip()
    if not email:
        email = input("Admin email: ").strip()
    if not password:
        password = input("Admin password: ").strip()
    if not email or not password:
        print("Email and password required")
        sys.exit(1)

    token = login(email, password)
    products = fetch_products(token)
    print(f"[LOAD] {len(products)} products found on cloud\n")

    if os.environ.get("AUTO_IMPORT") != "1":
        confirm = input(f"Migrate main images for {len(products)} products? (yes/no): ").strip().lower()
        if confirm not in ("yes", "y"):
            print("Aborted")
            sys.exit(0)

    success = 0
    failed = 0
    skipped = 0
    for i, p in enumerate(products, 1):
        result = migrate_product_image(token, p, i)
        if result:
            # Distinguish skip vs success
            if "r2.dev" in p.get("image", "") or "cloudflare" in p.get("image", ""):
                skipped += 1
            elif not p.get("image"):
                skipped += 1
            else:
                success += 1
        else:
            failed += 1
        time.sleep(0.5)  # Be gentle to Render + R2

    print(f"\n[DONE] Migrated: {success}, Skipped: {skipped}, Failed: {failed}, Total: {len(products)}")


if __name__ == "__main__":
    main()
