import os
import re
import json
import logging
from typing import List, Dict, Set, Optional
from bs4 import BeautifulSoup

logger = logging.getLogger("crawler")

class TrendsHobbyBrandHandler:
    """Crawls Trends Hobby models from Treasured Models Shopify store and local reference HTML."""
    def __init__(self, crawler):
        self.crawler = crawler

    def discover_sources(self) -> List[Dict]:
        pending = []
        
        # 1. Local HTML reference page
        ref_dir = os.path.join(os.path.dirname(__file__), "..", "reference_htmls")
        local_shop = os.path.join(ref_dir, "5. Trends Hobby – Treasured Models.html")
        if os.path.exists(local_shop):
            try:
                with open(local_shop, "r", encoding="utf-8") as f:
                    html_content = f.read()
                soup = BeautifulSoup(html_content, "lxml")
                
                # Discover products from local HTML
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if "/products/" in href:
                        pending.append({
                            "source": "shopify_detail",
                            "url": href,
                            "meta": {}
                        })
            except Exception as e:
                logger.error(f"Failed parsing Trends Hobby local HTML: {e}")

        # 2. Live Shopify endpoints (Treasured Models, Mobile Garage HK, Downskale)
        endpoints = [
            "https://treasuredmodels.com/collections/trends-hobby/products.json?limit=250&page=1",
            "https://www.mobilegaragehk.com/collections/trends-hobby/products.json?limit=250&page=1",
            "https://downskale.com/collections/trends-hobby/products.json?limit=250&page=1",
        ]
        for ep in endpoints:
            pending.append({
                "source": "shopify_json",
                "url": ep,
                "meta": {"page": 1, "base_url": ep}
            })

        return pending

    def parse_task(self, html_or_json: str, task: Dict) -> Optional[List[Dict]]:
        source = task["source"]
        meta = task["meta"]
        url = task["url"]

        if source == "shopify_json":
            try:
                data = json.loads(html_or_json)
                products = data.get("products", [])
                if not products:
                    return None

                self._parse_shopify_products(products)

                # Next Shopify page
                next_page = meta.get("page", 1) + 1
                base_url = meta.get("base_url", url)
                next_url = re.sub(r"page=\d+", f"page={next_page}", base_url)
                return [{
                    "source": "shopify_json",
                    "url": next_url,
                    "meta": {"page": next_page, "base_url": base_url}
                }]
            except Exception as e:
                logger.error(f"Trends Hobby Shopify products page parse error: {e}")
                return None

        elif source == "shopify_detail":
            soup = BeautifulSoup(html_or_json, "lxml")
            
            title_tag = soup.find("h1", class_="product-single__title")
            raw_title = title_tag.get_text(strip=True) if title_tag else ""
            if not raw_title:
                title_tag = soup.find("title")
                if title_tag:
                    raw_title = title_tag.get_text(strip=True).split("-")[0].strip()

            if not raw_title:
                return None

            sku = ""
            sku_tag = soup.find(class_=re.compile(r"(sku|item-code|model-no)", re.I))
            if sku_tag:
                sku = sku_tag.get_text(strip=True)
            if not sku:
                sku_match = re.search(r"\b(24\d{4,5}[A-Z0-9\-]*(?:\([A-Z0-9]+\))?)\b", raw_title, re.I)
                if sku_match:
                    sku = sku_match.group(1).upper().replace("(", "").replace(")", "")
                else:
                    sku_match = re.search(r"\b(TH[\-]?[0-9A-Z]+)\b", raw_title, re.I)
                    if sku_match:
                        sku = sku_match.group(1).upper()

            if not sku:
                id_match = re.search(r"id=(\d+)", url) or re.search(r"/products/([a-zA-Z0-9-]+)", url)
                if id_match:
                    sku = f"TH-{id_match.group(1).upper()}"
                else:
                    return None

            # Clean product name
            product_name = self._clean_product_name(raw_title, sku)
            brand = self._detect_car_brand(raw_title)

            img_urls = []
            for img in soup.find_all("img"):
                src = img.get("src") or img.get("data-src") or ""
                if src and "/products/" in src and not src.endswith(".gif") and "logo" not in src.lower():
                    img_urls.append(src.split("?")[0])

            year = None
            ym = re.search(r"\b(20\d{2})\b", raw_title)
            if ym:
                year = int(ym.group(1))

            self.crawler._save_or_merge_product(
                item_number=sku,
                product_name=product_name,
                brand=brand,
                scale="1:64",
                series="Regular",
                img_urls=img_urls,
                source="shopify_detail",
                release_year=year,
                release_year_confidence="inferred" if year else None,
                status="Released",
                toy_brand="Trends Hobby"
            )

        return None

    @staticmethod
    def _clean_product_name(title: str, sku: str = "") -> str:
        """Strip prefixes, scales, and redundant codes from title."""
        name = title
        name = re.sub(r"^\s*\[?(?:pre-?order)\]?\s*:?\s*", "", name, flags=re.I)
        name = re.sub(r"^\s*trends\s+hobby\s+(?:x\s+[^\-–]+[\-–]\s*)?", "", name, flags=re.I)
        name = re.sub(r"^\s*th\s+", "", name, flags=re.I)
        name = re.sub(r"\s*\(?1[:/]64(?:\s+diecast)?\)?\s*$", "", name, flags=re.I)
        name = re.sub(r"\s+1[:/]64\s+", " ", name, flags=re.I)
        if sku:
            name = re.sub(re.escape(sku), "", name, flags=re.I)
        name = re.sub(r"\s*\(?\b24\d{4,5}[A-Z0-9\-]*\)?\s*", " ", name)
        name = re.sub(r"\s+", " ", name).strip(" -–")
        return name if name else title

    @staticmethod
    def _detect_car_brand(text: str) -> str:
        """Detect real automotive manufacturer brand from text."""
        car_brands = [
            "Lamborghini", "Porsche", "Bugatti", "Ferrari", "McLaren",
            "BMW", "Mercedes-Benz", "Mercedes", "Audi", "Nissan",
            "Toyota", "Honda", "Ford", "Chevrolet", "Aston Martin", "Dodge"
        ]
        for cb in car_brands:
            if re.search(r"\b" + re.escape(cb) + r"\b", text, re.I):
                return "Mercedes-Benz" if cb == "Mercedes" else cb
        return "Trends Hobby"

    def _parse_shopify_products(self, products: List[Dict]) -> None:
        for p in products:
            title = p.get("title", "").strip()
            handle = p.get("handle", "")
            tags = p.get("tags", [])
            variant_sku = p.get("variants", [{}])[0].get("sku") or ""

            # 1. High-precision SKU extraction
            item_number = ""
            if variant_sku and not variant_sku.upper().startswith("PRE-ORDER") and not variant_sku.upper().startswith("PREORDER"):
                item_number = variant_sku.strip()

            if not item_number:
                # E.g. 241083E, 241084J, 241082HI-2G, 241098B, 241084(F)
                sku_match = re.search(r"\b(24\d{4,5}[A-Z0-9\-]*(?:\([A-Z0-9]+\))?)\b", title, re.I)
                if sku_match:
                    item_number = sku_match.group(1).upper().replace("(", "").replace(")", "")
                else:
                    sku_match = re.search(r"\b(TH[\-]?[0-9A-Z]+(?:-[0-9A-Z]+)?)\b", title, re.I)
                    if sku_match:
                        item_number = sku_match.group(1).upper()
                    else:
                        sku_match = re.search(r"\b(\d{5,7}[A-Z]*(?:-[0-9A-Z]+)?)\b", title)
                        if sku_match:
                            item_number = sku_match.group(1).upper()

            if not item_number and handle:
                sku_from_handle = re.search(r"(24\d{4,5}[a-z0-9\-]*)", handle, re.I)
                if sku_from_handle:
                    item_number = sku_from_handle.group(1).upper()
                else:
                    clean_handle = re.sub(r"^(pre-?order-?|trends-?hobby-?)", "", handle, flags=re.I)
                    clean_handle = clean_handle.strip("-")
                    item_number = re.sub(r"[^A-Z0-9-]", "", clean_handle.upper())
                    if len(item_number) > 30:
                        item_number = item_number[:30]

            if not item_number:
                continue

            # 2. Car Brand Detection
            brand = self._detect_car_brand(title)
            if brand == "Trends Hobby":
                for tag in tags:
                    tag_brand = self._detect_car_brand(tag)
                    if tag_brand != "Trends Hobby":
                        brand = tag_brand
                        break

            # 3. Clean Product Name
            product_name = self._clean_product_name(title, item_number)

            img_urls = []
            for img in p.get("images", []):
                src = img.get("src")
                if src:
                    img_urls.append(src.split("?")[0])

            # 4. Release Year Detection
            year = None
            for tag in tags:
                if re.match(r"^(202[0-9])$", tag.strip()):
                    year = int(tag.strip())
                    break
            if not year:
                ym = re.search(r"\b(202[0-9])\b", title)
                if ym:
                    year = int(ym.group(1))
            if not year:
                pub = p.get("published_at") or p.get("created_at") or ""
                ym = re.search(r"^(202[0-9])", pub)
                if ym:
                    year = int(ym.group(1))

            # 5. Series Classification
            series = "Regular"
            product_type = p.get("product_type", "")
            for tag in tags:
                tag_lower = tag.lower()
                if "exclusive" in tag_lower or "anniversary" in tag_lower:
                    series = "Exclusive"
                elif "dtm" in tag_lower:
                    series = "DTM Series"
                elif "gt3" in tag_lower or "gt world" in tag_lower:
                    series = "GT Series"
                elif "super gt" in tag_lower:
                    series = "Super GT Series"
                elif "formula" in tag_lower or "f1" in tag_lower:
                    series = "Formula Series"
                elif "wrc" in tag_lower or "rally" in tag_lower:
                    series = "Rally Series"
                elif "lemans" in tag_lower or "le mans" in tag_lower:
                    series = "Le Mans Series"

            attributes = {
                "tags": tags,
                "description": p.get("body_html", "")
            }

            self.crawler._save_or_merge_product(
                item_number=item_number,
                product_name=product_name,
                brand=brand,
                scale="1:64",
                series=series,
                img_urls=img_urls,
                source="shopify",
                release_year=year,
                release_year_confidence="confirmed" if year else None,
                status="Released",
                toy_brand="Trends Hobby",
                sub_series="Regular",
                attributes=attributes
            )
