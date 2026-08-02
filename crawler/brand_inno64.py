import os
import re
import json
import logging
import urllib.parse
from typing import List, Dict, Set, Optional
from bs4 import BeautifulSoup

from crawler.utils import (
    clean_fandom_image_url,
    get_row_product_images,
    parse_my64_list,
    parse_my64_detail
)

logger = logging.getLogger("crawler")

class Inno64BrandHandler:
    """Crawls INNO64 models from local HTML files, official WooCommerce site, and my64.com.my."""
    def __init__(self, crawler):
        self.crawler = crawler

    def discover_sources(self) -> List[Dict]:
        pending = []
        
        # 1. Discover from local reference HTML files
        ref_dir = os.path.join(os.path.dirname(__file__), "..", "reference_htmls")
        inno_shop_local = os.path.join(ref_dir, "1. Shop All Collectible Model Cars Online _ Inno Models.html")
        inno64_local = os.path.join(ref_dir, "2. Model Cars Online Malaysia __ INNO64.html")
        inno18r_local = os.path.join(ref_dir, "3.Model Cars Online Malaysia __ INNO18-R.html")

        if os.path.exists(inno_shop_local):
            try:
                with open(inno_shop_local, "r", encoding="utf-8") as f:
                    html_content = f.read()
                soup = BeautifulSoup(html_content, "lxml")
                
                # WooCommerce pagination and details discovery
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if "/product/" in href:
                        pending.append({
                            "source": "woocommerce_detail",
                            "url": href,
                            "meta": {"page_url": href}
                        })
                    elif "/shop/" in href or "paged=" in href:
                        pending.append({
                            "source": "woocommerce_list",
                            "url": href,
                            "meta": {}
                        })
            except Exception as e:
                logger.error(f"Failed parsing WooCommerce local shop: {e}")

        # 2. Local my64 INNO64 & INNO18-R
        if os.path.exists(inno64_local):
            try:
                with open(inno64_local, "r", encoding="utf-8") as f:
                    html = f.read()
                pending.extend(parse_my64_list(self.crawler, html, "26", "INNO64"))
            except Exception as e:
                logger.error(f"Failed parsing local my64 INNO64: {e}")

        if os.path.exists(inno18r_local):
            try:
                with open(inno18r_local, "r", encoding="utf-8") as f:
                    html = f.read()
                pending.extend(parse_my64_list(self.crawler, html, "27", "INNO64"))
            except Exception as e:
                logger.error(f"Failed parsing local my64 INNO18-R: {e}")

        # 3. Live WooCommerce and my64 queues
        pending.append({
            "source": "woocommerce_list",
            "url": "https://inno-models.com/shop/",
            "meta": {}
        })
        pending.append({
            "source": "my64_list",
            "url": "https://www.my64.com.my/usr/product.aspx?pgid=4&grpid=26&lang=en&pg=1",
            "meta": {"toy_brand": "INNO64", "grp_id": "26", "page": 1}
        })
        pending.append({
            "source": "my64_list",
            "url": "https://www.my64.com.my/usr/product.aspx?pgid=4&grpid=27&lang=en&pg=1",
            "meta": {"toy_brand": "INNO64", "grp_id": "27", "page": 1}
        })

        return pending

    def parse_task(self, html_or_json: str, task: Dict) -> Optional[List[Dict]]:
        source = task["source"]
        meta = task["meta"]
        url = task["url"]

        if source == "woocommerce_list":
            soup = BeautifulSoup(html_or_json, "lxml")
            new_tasks = []
            
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/product/" in href:
                    new_tasks.append({
                        "source": "woocommerce_detail",
                        "url": href,
                        "meta": {"page_url": href}
                    })
                elif "/shop/page/" in href or "paged=" in href:
                    new_tasks.append({
                        "source": "woocommerce_list",
                        "url": href,
                        "meta": {}
                    })
            return new_tasks

        elif source == "woocommerce_detail":
            self._parse_woocommerce_detail(html_or_json, url)

        elif source == "my64_list":
            return parse_my64_list(self.crawler, html_or_json, meta["grp_id"], meta["toy_brand"])

        elif source == "my64_detail":
            parse_my64_detail(self.crawler, html_or_json, url, meta["toy_brand"], meta["grp_id"])

        return None

    def _parse_woocommerce_detail(self, html_or_json: str, url: str) -> None:
        soup = BeautifulSoup(html_or_json, "lxml")
        
        title_tag = soup.find("h1", class_="product_title")
        product_name = title_tag.get_text(strip=True) if title_tag else ""
        if not product_name:
            title_tag = soup.find("title")
            if title_tag:
                product_name = title_tag.get_text(strip=True).split("-")[0].strip()

        if not product_name:
            return

        sku_tag = soup.find(class_="sku")
        sku = sku_tag.get_text(strip=True) if sku_tag else ""
        if not sku:
            sku_match = re.search(r"\b(IN64-[A-Z0-9-]+|IN18-R-[A-Z0-9-]+)\b", product_name, re.I)
            if sku_match:
                sku = sku_match.group(1).upper()
            else:
                sku_match = re.search(r"\b(IN64-[A-Z0-9-]+|IN18-R-[A-Z0-9-]+)\b", url, re.I)
                if sku_match:
                    sku = sku_match.group(1).upper()

        if not sku:
            return

        brand = "INNO64"
        brand_match = re.search(r"^[A-Z0-9\s.-]+(?=\s-\s|\s//)", product_name, re.I)
        if brand_match:
            brand = brand_match.group(0).strip()

        # Parse category tags
        series = "Regular"
        sub_series = "Regular"
        tags = []
        meta_tags = soup.find(class_="tagged_as")
        if meta_tags:
            for a in meta_tags.find_all("a"):
                tags.append(a.get_text(strip=True))

        attributes = {}
        if tags:
            attributes["tags"] = tags

        img_urls = []
        gallery = soup.find(class_=re.compile(r"(images|gallery|slider)", re.I))
        if gallery:
            for img in gallery.find_all("img"):
                src = img.get("src") or img.get("data-src") or ""
                if src and not src.endswith(".gif") and "logo" not in src.lower():
                    img_urls.append(src.split("?")[0])
        
        if not img_urls:
            for img in soup.find_all("img"):
                src = img.get("src") or img.get("data-src") or ""
                if src and "wp-content/uploads" in src and not src.endswith(".gif") and "logo" not in src.lower():
                    img_urls.append(src.split("?")[0])

        scale = "1:64"
        if "1:18" in product_name or "1/18" in product_name or "IN18-R" in sku:
            scale = "1:18"
        elif "1:43" in product_name or "1/43" in product_name:
            scale = "1:43"

        status = "Released"
        stock_html = soup.find(class_=re.compile(r"(out-of-stock|backorder)", re.I))
        if stock_html:
            status = "Pre-Order"

        self.crawler._save_or_merge_product(
            item_number=sku,
            product_name=product_name,
            brand=brand,
            scale=scale,
            series=series,
            img_urls=img_urls,
            source="official",
            release_year=None,
            release_year_confidence=None,
            status=status,
            toy_brand="INNO64",
            sub_series=sub_series,
            attributes=attributes
        )
