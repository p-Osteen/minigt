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

                    # Series line detection — match all official Tarmac Works product lines
                    series = "Regular"
                    tags = p.get("tags", [])
                    product_type = p.get("product_type", "")
                    for t in tags:
                        tl = t.lower()
                        if "collab64" in tl:
                            series = "COLLAB64"
                        elif "global64" in tl:
                            series = "GLOBAL64"
                        elif "hobby64+" in tl:
                            series = "HOBBY64+"
                        elif "hobby64" in tl:
                            series = "HOBBY64"
                        elif "road64" in tl:
                            series = "ROAD64"
                        elif "parts64" in tl:
                            series = "PARTS64"
                        elif "truck64" in tl or "trucks64" in tl:
                            series = "TRUCKS64"
                        elif "special" in tl or "limited" in tl:
                            if series == "Regular":
                                series = "Special Edition"

                    img_urls = []
                    for img in p.get("images", []):
                        src = img.get("src")
                        if src:
                            img_urls.append(src.split("?")[0])

                    year = None
                    ym = re.search(r"\b(20\d{2})\b", title)
                    if ym:
                        year = int(ym.group(1))

                    attributes = {}
                    if tags:
                        attributes["tags"] = tags
                    if product_type:
                        attributes["product_type"] = product_type

                    # Clean SKU
                    sku = re.sub(r"\s*\((?:bundle|set)\)", "", sku, flags=re.I).strip()

                    # Clean product name
                    product_name = title
                    product_name = re.sub(r"^\s*1[:/]64\s+", "", product_name, flags=re.I)
                    product_name = re.sub(r"\s*-\s*Tarmac Works.*$", "", product_name, flags=re.I)
                    product_name = re.sub(r"\s*-\s*Tarmac Cards.*$", "", product_name, flags=re.I)
                    product_name = product_name.strip(" -–") or title

                    # Extract car brand
                    brand = self._detect_car_brand(product_name)
                    if brand == "Tarmac Works":
                        vendor = p.get("vendor", "")
                        if vendor and vendor.lower() not in ("tarmac works", "tarmac"):
                            brand = vendor

                    # Series line detection — match all official Tarmac Works product lines
                    series = "Regular"
                    tags = p.get("tags", [])
                    product_type = p.get("product_type", "")
                    for t in tags:
                        tl = t.lower()
                        if "collab64" in tl:
                            series = "COLLAB64"
                        elif "global64" in tl:
                            series = "GLOBAL64"
                        elif "hobby64+" in tl:
                            series = "HOBBY64+"
                        elif "hobby64" in tl:
                            series = "HOBBY64"
                        elif "road64" in tl:
                            series = "ROAD64"
                        elif "parts64" in tl:
                            series = "PARTS64"
                        elif "truck64" in tl or "trucks64" in tl:
                            series = "TRUCKS64"
                        elif "special" in tl or "limited" in tl:
                            if series == "Regular":
                                series = "Special Edition"

                    if series == "Regular":
                        if sku.startswith("T64G-"):
                            series = "GLOBAL64"
                        elif sku.startswith("T64R-"):
                            series = "ROAD64"
                        elif sku.startswith("T64-"):
                            series = "HOBBY64"

                    img_urls = []
                    for img in p.get("images", []):
                        src = img.get("src")
                        if src:
                            img_urls.append(src.split("?")[0])

                    year = None
                    ym = re.search(r"\b(20\d{2})\b", title)
                    if ym:
                        year = int(ym.group(1))
                    if not year:
                        pub = p.get("published_at") or p.get("created_at") or ""
                        ym = re.search(r"^(20[12]\d)", pub)
                        if ym:
                            year = int(ym.group(1))

                    attributes = {}
                    if tags:
                        attributes["tags"] = tags
                    if product_type:
                        attributes["product_type"] = product_type

                    self.crawler._save_or_merge_product(
                        item_number=sku,
                        product_name=product_name,
                        brand=brand,
                        scale="1:64",
                        series=series,
                        img_urls=img_urls,
                        source="shopify",
                        release_year=year,
                        release_year_confidence="confirmed" if year else None,
                        status="Released",
                        toy_brand="Tarmac Works",
                        attributes=attributes
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
            page_year = int(page_name) if page_name.isdigit() and 2015 <= int(page_name) <= 2030 else None
            car_brand_from_page = self._detect_car_brand(page_name)

            for table in soup.find_all("table"):
                rows = table.find_all("tr")
                if not rows:
                    continue

                headers = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th", "td"])]
                code_idx = name_idx = desc_idx = brand_idx = series_idx = photo_idx = release_idx = -1

                for idx, h in enumerate(headers):
                    if "model #" in h or h in ("code", "item", "sku"):
                        code_idx = idx
                    elif any(k in h for k in ("name", "model")):
                        name_idx = idx
                    elif any(k in h for k in ("description", "livery", "color", "variation")):
                        desc_idx = idx
                    elif any(k in h for k in ("brand", "marque", "make")):
                        brand_idx = idx
                    elif any(k in h for k in ("series", "line")):
                        series_idx = idx
                    elif any(k in h for k in ("photo", "image", "pic")):
                        photo_idx = idx
                    elif any(k in h for k in ("release", "date", "year")):
                        release_idx = idx

                if code_idx == -1 and name_idx == -1 and desc_idx == -1:
                    continue

                last_series = "Regular"
                last_year = page_year

                for row in rows[1:]:
                    cells = row.find_all(["td", "th"])
                    if not cells:
                        continue

                    # 1. SKU extraction
                    item_number = ""
                    if code_idx != -1 and code_idx < len(cells):
                        item_number = cells[code_idx].get_text(strip=True)
                    if not item_number:
                        for c in cells:
                            txt = c.get_text(strip=True)
                            m = re.search(r"\b(T64[A-Z0-9-]*|T43[A-Z0-9-]*|T18[A-Z0-9-]*)\b", txt)
                            if m:
                                item_number = m.group(1).upper()
                                break

                    if not item_number or item_number in ("-", "", "N/A", "TBD"):
                        continue

                    # 2. Product Name
                    raw_name = ""
                    if name_idx != -1 and name_idx < len(cells):
                        raw_name = cells[name_idx].get_text(strip=True)
                    if not raw_name and desc_idx != -1 and desc_idx < len(cells):
                        raw_name = cells[desc_idx].get_text(strip=True)

                    if not raw_name:
                        raw_name = page_name

                    # Combine page name and description if description doesn't include the car name
                    clean_page = re.sub(r"\s*\([^)]*\)", "", page_name).strip()
                    if clean_page and clean_page.lower() not in raw_name.lower() and not page_name.isdigit():
                        product_name = f"{page_name} - {raw_name}".strip(" -–")
                    else:
                        product_name = raw_name

                    # 3. Automotive Brand
                    brand = "Tarmac Works"
                    if brand_idx != -1 and brand_idx < len(cells):
                        b_txt = cells[brand_idx].get_text(strip=True)
                        if b_txt:
                            brand = b_txt
                    if brand == "Tarmac Works":
                        brand = self._detect_car_brand(product_name)
                    if brand == "Tarmac Works" and car_brand_from_page != "Tarmac Works":
                        brand = car_brand_from_page

                    # 4. Series
                    series = last_series
                    if series_idx != -1 and series_idx < len(cells):
                        s_txt = cells[series_idx].get_text(strip=True)
                        if s_txt:
                            series = s_txt
                            last_series = s_txt
                    if series in ("Regular", "", "-"):
                        if item_number.startswith("T64G-"):
                            series = "GLOBAL64"
                        elif item_number.startswith("T64R-"):
                            series = "ROAD64"
                        elif item_number.startswith("T64-"):
                            series = "HOBBY64"

                    # 5. Release Year
                    release_year = last_year
                    release_year_confidence = "confirmed" if release_year else None
                    if release_idx != -1 and release_idx < len(cells):
                        rel_val = cells[release_idx].get_text(strip=True)
                        ym = re.search(r"(20\d{2})", rel_val)
                        if ym:
                            y = int(ym.group(1))
                            if 2015 <= y <= 2030:
                                release_year = y
                                release_year_confidence = "confirmed"
                                last_year = y

                    if release_year is None:
                        ym = re.search(r"\b(20\d{2})\b", product_name)
                        if ym:
                            y = int(ym.group(1))
                            if 2015 <= y <= 2030:
                                release_year = y
                                release_year_confidence = "inferred"

                    # 6. Images
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
                        series=series,
                        img_urls=img_urls,
                        source="fandom",
                        release_year=release_year,
                        release_year_confidence=release_year_confidence,
                        status="Released",
                        toy_brand="Tarmac Works"
                    )

        return None

    @staticmethod
    def _detect_car_brand(text: str) -> str:
        """Detect real automotive manufacturer brand from text."""
        car_brands = [
            "Porsche", "Ferrari", "Mercedes-Benz", "Mercedes-AMG", "Mercedes",
            "Audi", "BMW", "Nissan", "Toyota", "Honda", "Mitsubishi", "Mazda",
            "Subaru", "Ford", "Chevrolet", "McLaren", "Aston Martin", "Lamborghini",
            "Dodge", "Volkswagen", "Lexus", "Pagani", "RWB", "Koenigsegg"
        ]
        for cb in car_brands:
            if re.search(r"\b" + re.escape(cb) + r"\b", text, re.I):
                if cb in ("Mercedes", "Mercedes-AMG"):
                    return "Mercedes-Benz"
                return cb
        return "Tarmac Works"

