#!/usr/bin/env python3
"""Finish migrating the last 58 external detail images."""

import io
import json
import os
import sys
import time
from urllib.parse import urlparse

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


def download_image(url: str) -> tuple[bytes, str, str]:
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "image/jpeg")
    parsed = urlparse(url)
    filename = parsed.path.split("/")[-1] or "image.jpg"
    if "." not in filename:
        ext = content_type.split("/")[-1].replace("jpeg", "jpg")
        filename += f".{ext}"
    return resp.content, filename, content_type


def upload_to_r2(token: str, content: bytes, filename: str, content_type: str) -> str:
    url = f"{API_BASE}/admin/cta/image"
    files = {"image": (filename, io.BytesIO(content), content_type)}
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(url, files=files, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.json()["url"]


def update_product_images(token: str, product_id: str, new_images: list[str]) -> dict:
    url = f"{API_BASE}/products/{product_id}"
    data = {"images": new_images}
    files = {"data": (None, json.dumps(data), "application/json")}
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.put(url, files=files, headers=headers, timeout=30)
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

    # Only products with remaining external images
    todo = [p for p in products if any("sellgloves.com" in img for img in p.get("images", []))]
    print(f"[LOAD] {len(todo)} products still need migration\n")

    total_migrated = 0
    total_failed = 0

    for i, p in enumerate(todo, 1):
        product_id = p["id"]
        old_images = p.get("images", [])
        main_image = p.get("image", "")

        new_images = []
        migrated = 0
        failed = 0

        for img_url in old_images:
            if "daleimarble" in img_url or "r2.dev" in img_url:
                new_images.append(img_url)
                continue

            try:
                content, filename, content_type = download_image(img_url)
                r2_url = upload_to_r2(token, content, filename, content_type)
                new_images.append(r2_url)
                migrated += 1
                print(f"    OK: {img_url[:60]}...")
            except Exception as exc:
                print(f"    FAIL: {img_url[:60]}... -> {exc}")
                new_images.append(img_url)
                failed += 1

        # Avoid duplicate storage: use main_image R2 URL for first slot
        if main_image and "daleimarble" in main_image and new_images:
            new_images[0] = main_image

        try:
            update_product_images(token, product_id, new_images)
            print(f"  [{i}/{len(todo)}] {p['name'][:45]}... migrated={migrated} failed={failed}\n")
            total_migrated += migrated
            total_failed += failed
        except Exception as exc:
            print(f"  [{i}/{len(todo)}] UPDATE FAILED: {p['name'][:45]}... -> {exc}\n")
            total_failed += migrated

        time.sleep(1)

    print(f"\n[DONE] Migrated: {total_migrated}, Failed: {total_failed}")


if __name__ == "__main__":
    main()
