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

        # 2. Live Treasured Models Shopify endpoint
        api_url = "https://treasuredmodels.com/collections/trends-hobby/products.json?limit=250&page=1"
        pending.append({
            "source": "shopify_json",
            "url": api_url,
            "meta": {"page": 1}
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
                return [{
                    "source": "shopify_json",
                    "url": f"https://treasuredmodels.com/collections/trends-hobby/products.json?limit=250&page={next_page}",
                    "meta": {"page": next_page}
                }]
            except Exception as e:
                logger.error(f"Trends Hobby Shopify products page parse error: {e}")
                return None

        elif source == "shopify_detail":
            soup = BeautifulSoup(html_or_json, "lxml")
            
            title_tag = soup.find("h1", class_="product-single__title")
            product_name = title_tag.get_text(strip=True) if title_tag else ""
            if not product_name:
                title_tag = soup.find("title")
                if title_tag:
                    product_name = title_tag.get_text(strip=True).split("-")[0].strip()

            if not product_name:
                return None

            sku = ""
            sku_tag = soup.find(class_=re.compile(r"(sku|item-code|model-no)", re.I))
            if sku_tag:
                sku = sku_tag.get_text(strip=True)
            else:
                sku_match = re.search(r"\b(TH[0-9]+)\b", product_name, re.I)
                if sku_match:
                    sku = sku_match.group(1).upper()

            if not sku:
                id_match = re.search(r"id=(\d+)", url) or re.search(r"/products/([a-zA-Z0-9-]+)", url)
                if id_match:
                    sku = f"TH-{id_match.group(1).upper()}"
                else:
                    return None

            brand = "Trends Hobby"
            brand_match = re.search(r"^[A-Z0-9\s.-]+(?=\s-\s|\s//)", product_name, re.I)
            if brand_match:
                brand = brand_match.group(0).strip()

            img_urls = []
            for img in soup.find_all("img"):
                src = img.get("src") or img.get("data-src") or ""
                if src and "/products/" in src and not src.endswith(".gif") and "logo" not in src.lower():
                    img_urls.append(src.split("?")[0])

            year = None
            ym = re.search(r"\b(20\d{2})\b", product_name)
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

    def _parse_shopify_products(self, products: List[Dict]) -> None:
        for p in products:
            title = p.get("title", "").strip()
            item_number = p.get("variants", [{}])[0].get("sku", "") or ""
            if not item_number:
                sku_match = re.search(r"\b(TH[0-9]+)\b", title, re.I)
                if sku_match:
                    item_number = sku_match.group(1).upper()

            if not item_number:
                handle = p.get("handle", "")
                if handle:
                    item_number = f"TH-{handle.upper()}"
                else:
                    continue

            brand = "Trends Hobby"
            vendor = p.get("vendor", "")
            if vendor and vendor.lower() not in ("trends hobby", "trends"):
                brand = vendor

            product_name = title
            img_urls = []
            for img in p.get("images", []):
                src = img.get("src")
                if src:
                    img_urls.append(src.split("?")[0])

            year = None
            ym = re.search(r"\b(20\d{2})\b", title)
            if ym:
                year = int(ym.group(1))

            series = "Regular"
            tags = p.get("tags", [])
            for tag in tags:
                tag_lower = tag.lower()
                if "exclusive" in tag_lower:
                    series = "Exclusive"
                elif "dtm" in tag_lower:
                    series = "DTM Series"

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
                release_year_confidence="inferred" if year else None,
                status="Released",
                toy_brand="Trends Hobby",
                sub_series="Regular",
                attributes=attributes
            )
