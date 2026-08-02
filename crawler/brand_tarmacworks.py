import os
import re
import json
import logging
import urllib.parse
from typing import List, Dict, Set, Optional
from bs4 import BeautifulSoup

from crawler.utils import clean_fandom_image_url, get_row_product_images

logger = logging.getLogger("crawler")

class TarmacWorksBrandHandler:
    """Crawls Tarmac Works models from the official Shopify API and Fandom Wiki."""
    def __init__(self, crawler):
        self.crawler = crawler

    def discover_sources(self) -> List[Dict]:
        pending = []
        # 1. Official Shopify products.json
        api_url = "https://www.tarmacworks.com/products.json?limit=250&page=1"
        pending.append({
            "source": "shopify_json",
            "url": api_url,
            "meta": {"page": 1}
        })

        # 2. Exhaustive Fandom Wiki crawl
        apcontinue = ""
        while True:
            api_url = ("https://tarmacworks.fandom.com/api.php?action=query&list=allpages"
                       f"&apnamespace=0&aplimit=500&format=json")
            if apcontinue:
                api_url += f"&apcontinue={urllib.parse.quote(apcontinue)}"
            
            res_json = self.crawler.fetch_url(api_url, use_cache=True)
            if not res_json:
                break
            try:
                data = json.loads(res_json)
                pages = data.get("query", {}).get("allpages", [])
                for p in pages:
                    page_name = p["title"]
                    if any(x in page_name.lower() for x in [":", "main page", "list of"]):
                        continue
                    page_api = (f"https://tarmacworks.fandom.com/api.php?action=parse"
                                f"&page={urllib.parse.quote(page_name)}&format=json&prop=text")
                    pending.append({
                        "source": "fandom",
                        "url": page_api,
                        "meta": {"page_name": page_name}
                    })
                apcontinue = data.get("continue", {}).get("apcontinue", "")
                if not apcontinue:
                    break
            except Exception as e:
                logger.error(f"Error fetching Tarmac Works allpages: {e}")
                break

        return pending

    def parse_task(self, html_or_json: str, task: Dict) -> Optional[List[Dict]]:
        source = task["source"]
        meta = task["meta"]
        if source == "shopify_json":
            try:
                data = json.loads(html_or_json)
                products = data.get("products", [])
                if not products:
                    return None

                for p in products:
                    title = p.get("title", "").strip()
                    # Tarmac SKU code format e.g. T64-001-WH or T64R-002-RE
                    sku = p.get("variants", [{}])[0].get("sku", "") or ""
                    if not sku:
                        sku_match = re.search(r"\b(T64R?-[A-Z0-9-]+)\b", title, re.I)
                        if sku_match:
                            sku = sku_match.group(1).upper()
                    
                    if not sku:
                        continue

                    # Extract brand / maker
                    brand = "Tarmac Works"
                    vendor = p.get("vendor", "")
                    if vendor and vendor.lower() not in ("tarmac works", "tarmac"):
                        brand = vendor
                    else:
                        brand_match = re.search(r"^[A-Z0-9\s.-]+(?=\s-\s|\s//)", title, re.I)
                        if brand_match:
                            brand = brand_match.group(0).strip()

                    # Series line detection
                    series = "Regular"
                    tags = p.get("tags", [])
                    for t in tags:
                        tl = t.lower()
                        if "collab64" in tl:
                            series = "COLLAB64"
                        elif "global64" in tl:
                            series = "GLOBAL64"
                        elif "hobby64" in tl:
                            series = "HOBBY64"
                        elif "road64" in tl:
                            series = "ROAD64"

                    img_urls = []
                    for img in p.get("images", []):
                        src = img.get("src")
                        if src:
                            img_urls.append(src.split("?")[0])

                    year = None
                    ym = re.search(r"\b(20\d{2})\b", title)
                    if ym:
                        year = int(ym.group(1))

                    self.crawler._save_or_merge_product(
                        item_number=sku,
                        product_name=title,
                        brand=brand,
                        scale="1:64",
                        series=series,
                        img_urls=img_urls,
                        source="shopify",
                        release_year=year,
                        release_year_confidence="inferred" if year else None,
                        status="Released",
                        toy_brand="Tarmac Works"
                    )

                # Next Shopify page
                next_page = meta.get("page", 1) + 1
                return [{
                    "source": "shopify_json",
                    "url": f"https://www.tarmacworks.com/products.json?limit=250&page={next_page}",
                    "meta": {"page": next_page}
                }]
            except Exception as e:
                logger.error(f"Tarmac Works Shopify products page parse error: {e}")
                return None

        elif source == "fandom":
            try:
                res_data = json.loads(html_or_json)
                if "parse" not in res_data or "text" not in res_data["parse"]:
                    return None
                html_content = res_data["parse"]["text"]["*"]
                soup = BeautifulSoup(html_content, "lxml")
            except Exception as e:
                logger.error(f"Tarmac Works Fandom parse error for {meta.get('page_name','?')}: {e}")
                return None

            page_name = meta.get("page_name", "")
            default_series = "Regular"
            
            for table in soup.find_all("table"):
                headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
                code_idx = name_idx = brand_idx = photo_idx = release_idx = -1

                for idx, h in enumerate(headers):
                    if "model #" in h or h in ("code", "item", "sku"):
                        code_idx = idx
                    elif any(k in h for k in ("name", "model")):
                        name_idx = idx
                    elif any(k in h for k in ("brand", "marque")):
                        brand_idx = idx
                    elif any(k in h for k in ("photo", "image", "pic")):
                        photo_idx = idx
                    elif "release" in h or "date" in h:
                        release_idx = idx

                if code_idx == -1 or name_idx == -1:
                    continue

                for row in table.find_all("tr")[1:]:
                    cells = row.find_all(["td", "th"])
                    if len(cells) <= max(code_idx, name_idx):
                        continue

                    item_number = cells[code_idx].get_text(strip=True)
                    product_name = cells[name_idx].get_text(strip=True)

                    if not item_number or not product_name or item_number.strip() in ("-", ""):
                        continue

                    brand = "Tarmac Works"
                    if brand_idx != -1 and brand_idx < len(cells):
                        brand = cells[brand_idx].get_text(strip=True) or "Tarmac Works"

                    release_year = None
                    release_year_confidence = None
                    if release_idx != -1 and release_idx < len(cells):
                        rel_val = cells[release_idx].get_text(strip=True)
                        ym = re.search(r"\b(20\d{2})\b", rel_val)
                        if ym:
                            release_year = int(ym.group(1))
                            release_year_confidence = "confirmed"

                    img_urls = get_row_product_images(row)
                    if not img_urls and photo_idx != -1 and photo_idx < len(cells):
                        img_tag = cells[photo_idx].find("img")
                        if img_tag:
                            img_url = img_tag.get("data-src") or img_tag.get("src", "")
                            if img_url and "data:image" not in img_url:
                                img_urls = [clean_fandom_image_url(img_url)]

                    self.crawler._save_or_merge_product(
                        item_number=item_number,
                        product_name=product_name,
                        brand=brand,
                        scale="1:64",
                        series=default_series,
                        img_urls=img_urls,
                        source="fandom",
                        release_year=release_year,
                        release_year_confidence=release_year_confidence,
                        status="Released",
                        toy_brand="Tarmac Works"
                    )

        return None
