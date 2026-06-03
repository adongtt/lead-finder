#!/usr/bin/env python3
"""Migrate detail images (images array) to Cloudflare R2 via backend API."""

import os
import sys
import time

import requests

API_BASE = "https://fbgloves.onrender.com/api"


def login(email: str, password: str) -> str:
    resp = requests.post(
        f"{API_BASE}/auth/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["token"]


def fetch_products(token: str) -> list[dict]:
    resp = requests.get(
        f"{API_BASE}/products",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("products", [])


def migrate_product(token: str, product_id: str) -> dict:
    resp = requests.post(
        f"{API_BASE}/admin/migrate-product-images/{product_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


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
    print(f"[LOAD] {len(products)} products found\n")

    if os.environ.get("AUTO_IMPORT") != "1":
        confirm = input(f"Migrate detail images for {len(products)} products? (yes/no): ").strip().lower()
        if confirm not in ("yes", "y"):
            print("Aborted")
            sys.exit(0)

    total_migrated = 0
    total_skipped = 0
    total_failed = 0
    product_failures = 0

    for i, p in enumerate(products, 1):
        try:
            result = migrate_product(token, p["id"])
            m = result.get("migrated", 0)
            s = result.get("skipped", 0)
            f = result.get("failed", 0)
            total_migrated += m
            total_skipped += s
            total_failed += f
            status = "OK" if f == 0 else "PARTIAL_FAIL"
            print(
                f"  [{i}/{len(products)}] {status}: {result['name'][:45]}... "
                f"migrated={m} skipped={s} failed={f}"
            )
            if f > 0:
                product_failures += 1
        except Exception as exc:
            print(f"  [{i}/{len(products)}] FAILED: {p['name'][:45]}... -> {exc}")
            product_failures += 1
        time.sleep(0.4)

    print(
        f"\n[DONE] Products: {len(products)}, "
        f"Migrated: {total_migrated}, Skipped: {total_skipped}, Failed: {total_failed}, "
        f"Products with failures: {product_failures}"
    )


if __name__ == "__main__":
    main()
