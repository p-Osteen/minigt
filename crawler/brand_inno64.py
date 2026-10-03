import os
import re
import json
import logging
import urllib.parse
import unicodedata
from typing import List, Dict, Set, Optional
from bs4 import BeautifulSoup

from crawler.utils import (
    clean_fandom_image_url,
    get_row_product_images,
    parse_my64_list,
    parse_my64_detail
)

logger = logging.getLogger("crawler")


# Common mojibake replacements (UTF-8 bytes misread as Latin-1/Windows-1252)
_MOJIBAKE_MAP = {
    "\u00e3\u0080\u008c": "\u300c",  # Left corner bracket「
    "\u00e3\u0080\u008d": "\u300d",  # Right corner bracket」
    "\u00e2\u0080\u009c": "\u201c",  # Left double quotation "
    "\u00e2\u0080\u009d": "\u201d",  # Right double quotation "
    "\u00e2\u0080\u0093": "\u2013",  # En dash –
    "\u00e2\u0080\u0094": "\u2014",  # Em dash —
    "\u00e2\u0080\u0099": "\u2019",  # Right single quote '
    "\u00e2\u0080\u0098": "\u2018",  # Left single quote '
    "\u00c3\u00a9": "\u00e9",        # é
    "\u00c3\u00ab": "\u00eb",        # ë
    "\u00c3\u00bc": "\u00fc",        # ü
    "\u00c3\u00b6": "\u00f6",        # ö
    "\u00c3\u00a1": "\u00e1",        # á
}

def _fix_mojibake(text: str) -> str:
    """Repair common mojibake encoding issues in product names."""
    if not text:
        return text
    # Try standard re-encode fix
    try:
        fixed = text.encode("latin-1").decode("utf-8")
        if fixed != text:
            return fixed
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    # Fallback: replace known mojibake sequences
    for bad, good in _MOJIBAKE_MAP.items():
        text = text.replace(bad, good)
    # Strip any remaining replacement characters
    text = text.replace("\ufffd", "")
    # Normalize unicode
    text = unicodedata.normalize("NFC", text)
    return text


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
        if not sku:
            # Try broader SKU patterns: IN64-xxx, IN18-R-xxx, COKE-xxx, numeric codes
            sku_patterns = [
                r"\b(IN64-[A-Z0-9-]+)\b",
                r"\b(IN18-?R?-[A-Z0-9-]+)\b",
                r"\b(COKE-?\d+)\b",
                r"\b(IN64R?-[A-Z0-9-]+)\b",
            ]
            for pat in sku_patterns:
                sku_match = re.search(pat, product_name, re.I)
                if sku_match:
                    sku = sku_match.group(1).upper()
                    break
            if not sku:
                for pat in sku_patterns:
                    sku_match = re.search(pat, url, re.I)
                    if sku_match:
                        sku = sku_match.group(1).upper()
                        break

        # Images discovery first so we can use image codes for SKU and year
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

        # If SKU is still missing or a long slug (>20 chars), extract from image URL code
        if not sku or len(sku) > 20:
            for img_u in img_urls:
                m_code = re.search(r"/([^/]+)-\d+-\d+x\d+\.(?:png|jpg)", img_u)
                if not m_code:
                    m_code = re.search(r"/([^/]+)\.(?:png|jpg)", img_u)
                if m_code:
                    code_cand = m_code.group(1).upper()
                    if 4 <= len(code_cand) <= 22 and not code_cand.isdigit():
                        sku = code_cand
                        break

        if not sku:
            # Last resort: derive from URL slug
            slug_match = re.search(r"/product/([^/]+)/?", url)
            if slug_match:
                slug = slug_match.group(1).replace("-", " ").strip()
                if len(slug) <= 40:
                    sku = re.sub(r"[^A-Z0-9]", "", slug.upper())
            if not sku:
                return

        # Fix mojibake and clean product name
        product_name = self._clean_inno_name(product_name)

        # Detect automotive brand
        brand = self._detect_car_brand(product_name)
        if brand == "INNO64":
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

        scale = "1:64"
        if "1:18" in product_name or "1/18" in product_name or "IN18-R" in sku or "IN18R" in sku:
            scale = "1:18"
        elif "1:43" in product_name or "1/43" in product_name:
            scale = "1:43"

        status = "Released"
        stock_html = soup.find(class_=re.compile(r"(out-of-stock|backorder)", re.I))
        if stock_html:
            status = "Pre-Order"

        # Extract clean description text
        desc_tag = soup.find(class_=re.compile(r"(product-description|woocommerce-product-details__short-description|description)", re.I))
        description = ""
        if desc_tag:
            description = self._clean_inno_name(desc_tag.get_text(strip=True))

        # Extract release year
        release_year = None
        release_year_confidence = None
        ym = re.search(r"\b(20[12]\d)\b", product_name)
        if ym:
            release_year = int(ym.group(1))
            release_year_confidence = "confirmed"
        if not release_year and description:
            ym = re.search(r"\b(20[12]\d)\b", description)
            if ym:
                release_year = int(ym.group(1))
                release_year_confidence = "confirmed"
        if not release_year:
            for img_u in img_urls:
                ym = re.search(r"/wp-content/uploads/(20[12]\d)/", img_u)
                if ym:
                    release_year = int(ym.group(1))
                    release_year_confidence = "inferred"
                    break

        attributes = {}
        if tags:
            attributes["tags"] = tags
        if description:
            attributes["description"] = description

        self.crawler._save_or_merge_product(
            item_number=sku,
            product_name=product_name,
            brand=brand,
            scale=scale,
            series=series,
            img_urls=img_urls,
            source="official",
            release_year=release_year,
            release_year_confidence=release_year_confidence,
            status=status,
            toy_brand="INNO64",
            sub_series=sub_series,
            attributes=attributes
        )

    @staticmethod
    def _clean_inno_name(name: str) -> str:
        """Fix mojibake and clean up product name."""
        if not name:
            return ""
        name = _fix_mojibake(name)
        # Replace word or 「word」 with "word"
        name = re.sub(r"[\ufffd\u300c]+([^\ufffd\u300d]+)[\ufffd\u300d]+", r'"\1"', name)
        name = name.replace("\ufffd", "").replace("", "")
        name = re.sub(r"\s+", " ", name).strip()
        return name

    @staticmethod
    def _detect_car_brand(text: str) -> str:
        """Detect real automotive manufacturer brand from text."""
        car_brands = [
            "Nissan", "Toyota", "Honda", "Mitsubishi", "Mazda", "Subaru", "Ford",
            "Ferrari", "Porsche", "BMW", "Mercedes-Benz", "Mercedes", "Audi", "Lamborghini",
            "Suzuki", "Peugeot", "Chevrolet", "Dodge", "Jaguar", "Alfa Romeo", "Volvo"
        ]
        for cb in car_brands:
            if re.search(r"\b" + re.escape(cb) + r"\b", text, re.I):
                return "Mercedes-Benz" if cb == "Mercedes" else cb
        tl = text.lower()
        if any(k in tl for k in ["skyline", "silvia", "gt-r", "180sx", "fairlady", "patrol", "gtr"]):
            return "Nissan"
        if any(k in tl for k in ["supra", "celica", "ae86", "corolla", "yaris", "chaser", "mr2", "altezza"]):
            return "Toyota"
        if any(k in tl for k in ["civic", "nsx", "integra", "accord", "s2000", "city turbo"]):
            return "Honda"
        if any(k in tl for k in ["lancer", "evolution", "evo", "pajero"]):
            return "Mitsubishi"
        if any(k in tl for k in ["rx-7", "rx-8", "miata", "rx7", "rx8"]):
            return "Mazda"
        if any(k in tl for k in ["f40", "enzo", "testarossa", "458"]):
            return "Ferrari"
        return "INNO64"
