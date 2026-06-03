#!/usr/bin/env python3
"""Generate SQL seed file for importing sellgloves products into Render SQLite."""

import csv
import json
import re
import uuid
from datetime import datetime, timezone

CSV_FILE = "sellgloves_football_gloves_clean.csv"
SQL_FILE = "seed-products.sql"

def parse_price(val: str) -> float | None:
    if not val:
        return None
    # Extract number from strings like "$7.9", "23.2$", "$16.9"
    m = re.search(r"[\d.]+", val.replace(",", ""))
    return float(m.group()) if m else None

def build_colors(csv_row: dict) -> list[dict]:
    """Build colors array from color/size/material heuristics."""
    colors = []
    color_val = csv_row.get("color", "")
    if color_val:
        for c in re.split(r"[,/]", color_val):
            c = c.strip()
            if c:
                colors.append({"name": c, "value": "#000000", "image": ""})
    return colors

def build_sizes(csv_row: dict) -> list[str]:
    sizes = []
    size_val = csv_row.get("size", "")
    if size_val:
        for s in re.split(r"[,/]", size_val):
            s = s.strip()
            if s:
                sizes.append(s)
    return sizes or ["S", "M", "L", "XL"]

def build_features(csv_row: dict) -> list[str]:
    features = []
    feature_val = csv_row.get("feature", "") or csv_row.get("features", "")
    if feature_val:
        for f in re.split(r"[,;]", feature_val):
            f = f.strip()
            if f:
                features.append(f)
    return features

def build_specifications(csv_row: dict) -> dict[str, str]:
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
        val = csv_row.get(key, "")
        if val:
            specs[label] = val
    return specs

def build_images(csv_row: dict) -> list[str]:
    imgs = []
    if csv_row.get("list_image"):
        imgs.append(csv_row["list_image"])
    if csv_row.get("detail_images"):
        for img in csv_row["detail_images"].split(","):
            img = img.strip()
            if img and img not in imgs:
                imgs.append(img)
    return imgs

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def escape_sql(val):
    if val is None:
        return "NULL"
    if isinstance(val, (list, dict)):
        val = json.dumps(val, ensure_ascii=False)
    val = str(val).replace("'", "''")
    return f"'{val}'"

def main():
    rows = list(csv.DictReader(open(CSV_FILE, "r", encoding="utf-8-sig")))
    print(f"Loaded {len(rows)} products from CSV")

    statements = []
    statements.append("BEGIN TRANSACTION;")

    for row in rows:
        price = parse_price(row.get("sale_price", ""))
        original_price = parse_price(row.get("original_price", ""))
        if price is None:
            price = 0.0

        images = build_images(row)
        features = build_features(row)
        sizes = build_sizes(row)
        colors = build_colors(row)
        specs = build_specifications(row)

        product = {
            "id": str(uuid.uuid4()),
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
            "inStock": 1,
            "isBestseller": 0,
            "isNew": 0,
            "createdAt": now_iso(),
            "updatedAt": now_iso(),
        }

        cols = ", ".join(product.keys())
        vals = ", ".join(escape_sql(v) for v in product.values())
        statements.append(f"INSERT INTO products ({cols}) VALUES ({vals});")

    statements.append("COMMIT;")

    with open(SQL_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(statements))

    print(f"Generated {SQL_FILE} with {len(rows)} INSERT statements")

if __name__ == "__main__":
    main()
