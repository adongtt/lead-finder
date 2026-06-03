#!/usr/bin/env python3
"""
手套品牌种子域名生成器

原理：
  内置常见手套品牌列表（运动/工作/滑雪/骑行等），
  尝试通过 Bing 搜索验证官网域名，网络不通时使用预置域名。
  输出可直接导入 lead-finder "批量导入域名"。

用法：
  python generate_seed_domains.py
"""

import csv
import re
import sys
import time
import urllib.parse
from typing import Optional, Set

try:
    import requests
except ImportError:
    print("[ERROR] requests not installed. Run: pip install requests")
    sys.exit(1)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ---------------------------------------------------------------------------
# Pre-built brand list with known domains
# ---------------------------------------------------------------------------
SEED_BRANDS = {
    # Sports / Football / Baseball gloves
    "Nike": "nike.com",
    "Adidas": "adidas.com",
    "Under Armour": "underarmour.com",
    "Wilson Sporting Goods": "wilson.com",
    "Franklin Sports": "franklinsports.com",
    "Cutters Sports": "cuttersgloves.com",
    "Grip Boost": "gripboost.com",
    "Battle Sports Science": "battlesports.com",
    "Xenith": "xenith.com",
    "Riddell": "riddell.com",
    "Schutt Sports": "schuttsports.com",
    "Rawlings": "rawlings.com",
    "Easton Sports": "easton.com",
    "Louisville Slugger": "slugger.com",
    "Marucci Sports": "maruccisports.com",
    "Mizuno USA": "mizunousa.com",
    "Akadema": "akademapro.com",
    "All-Star Sporting Goods": "allstarequipment.com",
    "Diamond Sports": "diamond-sports.com",
    "EvoShield": "evoshield.com",
    "G-Form": "g-form.com",
    "McDavid": "mcdavidusa.com",
    "Shock Doctor": "shockdoctor.com",
    # Work / Safety gloves
    "Ansell": "ansell.com",
    "Mechanix Wear": "mechanix.com",
    "Ironclad Performance Wear": "ironclad.com",
    "Youngstown Glove": "youngstowntownglove.com",
    "Wells Lamont": "wellslamont.com",
    "Carhartt": "carhartt.com",
    "Kinco International": "kinco.com",
    "Atlas Glove": "atlasglove.com",
    "Showa Group": "showagroup.com",
    "HexArmor": "hexarmor.com",
    "Superior Glove Works": "superiorglove.com",
    "Majestic Glove": "majesticglove.com",
    "Cordova Safety Products": "cordovasafety.com",
    "MCR Safety": "mcrsafety.com",
    "PIP Global": "pipglobal.com",
    "Ergodyne": "ergodyne.com",
    "Impacto Protective Products": "impacto.ca",
    "Olympia Gloves": "olympiagloves.com",
    "Watson Gloves": "watsongloves.com",
    "Northern Safety": "northernsafety.com",
    # Ski / Winter sports gloves
    "Hestra Gloves": "hestragloves.com",
    "Gordini": "gordini.com",
    "Swany America": "swanyamerica.com",
    "Black Diamond Equipment": "blackdiamondequipment.com",
    "Outdoor Research": "outdoorresearch.com",
    "Arc'teryx": "arcteryx.com",
    "The North Face": "thenorthface.com",
    "Marmot": "marmot.com",
    "Burton Snowboards": "burton.com",
    "Dakine": "dakine.com",
    "686": "sixeightsix.com",
    "Pow Gloves": "powgloves.com",
    "Level Gloves": "levelgloves.com",
    "Reusch": "reusch.com",
    "Leki": "leki.com",
    # Cycling / Motorcycle gloves
    "Pearl Izumi": "pearlizumi.com",
    "Giro Sport Design": "giro.com",
    "Castelli Cycling": "castelli-cycling.com",
    "Rapha": "rapha.cc",
    "Fox Racing": "foxracing.com",
    "Troy Lee Designs": "troyleedesigns.com",
    "Five Ten": "fiveten.com",
    "100%": "100percent.com",
    "Alpinestars": "alpinestars.com",
    "Dainese": "dainese.com",
    "Rev'it": "revitsport.com",
    # Boxing / MMA gloves
    "Everlast": "everlast.com",
    "Title Boxing": "titleboxing.com",
    "Ringside": "ringside.com",
    "Venum": "venum.com",
    "Hayabusa Fight": "hayabusafight.com",
    "Cleto Reyes": "cletoreyesboxing.com",
    "Winning Boxing": "winning-usa.com",
    "Grant Boxing": "grantboxing.com",
    "Rival Boxing": "rivalboxing.com",
    "Sanabul": "sanabulsports.com",
    # Goalkeeper gloves
    "Reusch Soccer": "reusch.com",
    "Uhlsport": "uhlsport.de",
    "Sells Goalkeeper Products": "sellsports.com",
    "West Coast Goalkeeping": "westcoastgoalkeeping.com",
    "Storelli Sports": "storelli.com",
    "Aviata Sports": "aviata.com",
    "One Glove": "theoneglove.com",
    "Elite Sport": "elitesportgk.com",
}

# ---------------------------------------------------------------------------
# Domain finding via Bing (with fallback)
# ---------------------------------------------------------------------------

def find_domain_bing(brand_name: str) -> Optional[str]:
    query = f"{brand_name} official website"
    try:
        resp = requests.get(
            "https://www.bing.com/search",
            params={"q": query},
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        html = resp.text
        m = re.search(
            r'<li class="b_algo"[^>]*>.*?<h2[^>]*><a[^>]+href="([^"]+)"',
            html,
            re.DOTALL,
        )
        if not m:
            return None
        url = m.group(1)
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        blocked = {
            "facebook.com", "linkedin.com", "twitter.com", "x.com", "instagram.com",
            "youtube.com", "wikipedia.org", "amazon.com", "ebay.com", "alibaba.com",
            "zoominfo.com", "crunchbase.com", "bbb.org", "yellowpages.com", "yelp.com",
            "tripadvisor.com", "pinterest.com", "reddit.com", "quora.com",
            "etsy.com", "walmart.com", "target.com", "homedepot.com",
            "bestbuy.com", "costco.com", "wayfair.com", "macys.com",
        }
        if domain in blocked:
            return None
        return domain
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("[Seed Domain Generator]")
    print("=" * 60)
    print(f"Total seed brands: {len(SEED_BRANDS)}")
    print()

    results = []
    verified = 0
    fallback = 0

    for idx, (brand, known_domain) in enumerate(SEED_BRANDS.items(), 1):
        print(f"  [{idx}/{len(SEED_BRANDS)}] {brand} ...", end=" ", flush=True)
        # Skip Bing in China network (results are heavily polluted)
        # Use pre-built known domains directly
        domain = known_domain
        print(f"[known] {domain}")
        fallback += 1
        results.append((brand, domain))
        time.sleep(0.05)

    # Export CSV
    csv_path = "seed_brands.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["brand", "domain", "source"])
        for brand, domain in results:
            source = "bing" if any(r[0] == brand and r[1] != SEED_BRANDS[brand] for r in results) else "known"
            # Actually simpler: check if domain != known_domain
            source = "bing" if domain != SEED_BRANDS.get(brand, "") else "known"
            writer.writerow([brand, domain, source])
    print(f"\n[OK] CSV exported: {csv_path}")

    # Export domain list
    domain_path = "seed_domains.txt"
    domains = sorted({d for _, d in results if d})
    with open(domain_path, "w", encoding="utf-8") as f:
        for d in domains:
            f.write(d + "\n")
    print(f"[OK] Domain list exported: {domain_path}")

    # Summary
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    print(f"  Total brands    : {len(results)}")
    print(f"  Bing verified   : {verified}")
    print(f"  Known fallback  : {fallback}")
    print(f"  Unique domains  : {len(domains)}")
    print(f"  CSV file        : {csv_path}")
    print(f"  Domain list     : {domain_path}")
    print("=" * 60)
    print("\n  Copy the contents of seed_domains.txt into")
    print("  lead-finder's '批量导入域名' field to start email discovery.")


if __name__ == "__main__":
    main()
