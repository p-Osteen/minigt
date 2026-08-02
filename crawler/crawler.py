"""
MINI GT Catalog Crawler — High-Performance Multi-Source Scraper
Sources: minigt.tsm-models.com | minigt.fandom.com | myminigt.com
"""
import os
import sys
import re
import json
import time
import hashlib
import logging
import threading
import urllib.parse
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Set, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

from database.db_manager import get_db_session, sync_to_json
from database.models import (
    MiniGTProduct, HotWheelsProduct, PopRaceProduct,
    TarmacWorksProduct, Inno64Product, TrendsHobbyProduct,
    get_product_model
)

# Suppress SSL warnings (we disable SSL verification for speed)
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# Ensure required directories exist (no images/ — URLs only)
os.makedirs("logs", exist_ok=True)
os.makedirs("cache", exist_ok=True)
os.makedirs("cache/html", exist_ok=True)

# Logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("logs/crawler.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("crawler")

# Thread locks
state_lock = threading.Lock()
db_lock = threading.Lock()
counter_lock = threading.Lock()

# Persistent state path
STATE_PATH = "cache/crawler_state.json"

# D-prefix pattern — never save these
D_PREFIX_RE = re.compile(r"^D", re.IGNORECASE)

# DB write batch size
BATCH_SIZE = 50


def clean_fandom_image_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        query = parsed.query
        path = parsed.path
        if "/revision/latest" in path:
            parts = path.split("/revision/latest")
            new_path = parts[0] + "/revision/latest"
        else:
            new_path = path
        new_parsed = parsed._replace(path=new_path, query=query)
        return urllib.parse.urlunparse(new_parsed)
    except Exception:
        return url


def clean_diecastsociety_image_url(url: str) -> str:
    if not url:
        return ""
    # Remove WordPress dimension suffix, e.g. -75x50 or -650x320
    cleaned_url = re.sub(r"-\d+x\d+(\.[a-zA-Z0-9]+)$", r"\1", url)
    return cleaned_url


def get_row_product_images(tr) -> List[str]:
    img_urls = []
    seen = set()
    for img in tr.find_all("img"):
        parent_a = img.find_parent("a")
        is_product = False
        if parent_a:
            cls = parent_a.get("class", [])
            if any("image" in c for c in cls):
                is_product = True
        if img.find_parent("figure"):
            is_product = True
        if "thumbimage" in img.get("class", []):
            is_product = True
            
        if is_product:
            url = img.get("data-src") or img.get("src", "")
            if url and "data:image" not in url:
                url = clean_fandom_image_url(url)
                if url not in seen:
                    img_urls.append(url)
                    seen.add(url)
    return img_urls


def get_links_from_filters_json(filepath: str) -> List[str]:
    if not os.path.exists(filepath):
        logger.warning(f"Filters JSON file not found: {filepath}")
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        links = []
        def walk(node):
            if isinstance(node, dict):
                if "link" in node and node["link"]:
                    links.append(node["link"])
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for x in node:
                    walk(x)
        
        walk(data.get("filters", {}))
        return list(set(links))
    except Exception as e:
        logger.error(f"Error loading links from {filepath}: {e}")
        return []


class MINI_GTCrawler:
    def __init__(self, max_workers: int = 20, rate_limit_delay: float = 0.2):
        self.max_workers = max_workers
        self.rate_limit_delay = rate_limit_delay
        self.crawler_state = self._load_state()
        self._session = self._make_session()
        # Dynamic task counters (fixed progress bug)
        self._total_tasks = 0
        self._completed_tasks = 0
        self.handlers = {
            "MINI GT": MiniGTBrandHandler(self),
            "Hot Wheels": HotWheelsBrandHandler(self),
            "Pop Race": PopRaceBrandHandler(self),
            "Tarmac Works": TarmacWorksBrandHandler(self),
            "INNO64": Inno64BrandHandler(self),
            "Trends Hobby": TrendsHobbyBrandHandler(self),
        }

    # ------------------------------------------------------------------ #
    #  HTTP Session                                                        #
    # ------------------------------------------------------------------ #

    def _make_session(self) -> requests.Session:
        """Creates a requests Session with connection pooling and retry logic."""
        session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.4,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"],
        )
        adapter = HTTPAdapter(
            pool_connections=30,
            pool_maxsize=30,
            max_retries=retry,
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            }
        )
        return session

    # ------------------------------------------------------------------ #
    #  State Persistence                                                   #
    # ------------------------------------------------------------------ #

    def _load_state(self) -> Dict:
        with state_lock:
            if os.path.exists(STATE_PATH):
                try:
                    with open(STATE_PATH, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception as e:
                    logger.error(f"Failed to load crawler state: {e}")
            return {
                "pending_urls": [],
                "crawled_urls": [],
                "discovered_sources": {
                    "official_brands": [],
                    "fandom_pages": [],
                    "myminigt_urls": [],
                },
            }

    def save_state(self) -> None:
        with state_lock:
            try:
                with open(STATE_PATH, "w", encoding="utf-8") as f:
                    json.dump(self.crawler_state, f, indent=2)
            except Exception as e:
                logger.error(f"Failed to save crawler state: {e}")

    # ------------------------------------------------------------------ #
    #  HTTP Fetching with Local HTML Cache                                 #
    # ------------------------------------------------------------------ #

    def fetch_url(self, url: str, use_cache: bool = True) -> Optional[str]:
        """Fetches page content, reading from local HTML cache when available."""
        url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
        # Ensure cache path is absolute to the project directory to avoid Cwd shifting issues
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cache_file = os.path.join(root_dir, "cache", "html", f"{url_hash}.html")

        # Serve from cache — NO rate-limit delay needed
        if use_cache and os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                logger.warning(f"Failed to read cache for {url}: {e}")

        # For diecastsilkroad.com and tarmacworks.com, use curl.exe via subprocess to bypass Shopify rate limits/fingerprinting
        if "diecastsilkroad.com" in url or "tarmacworks.com" in url:
            import subprocess
            try:
                result = subprocess.run(
                    ["curl.exe", "-s", url],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore"
                )
                if result.returncode == 0 and result.stdout:
                    # Write to cache
                    try:
                        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
                        with open(cache_file, "w", encoding="utf-8") as f:
                            f.write(result.stdout)
                    except Exception as e:
                        logger.warning(f"Failed to cache {url}: {e}")
                    return result.stdout
            except Exception as e:
                logger.error(f"Failed to fetch {url} via curl.exe: {e}")

        # Live fetch — apply rate-limit delay
        time.sleep(self.rate_limit_delay)

        max_retries = 5
        for attempt in range(max_retries):
            try:
                resp = self._session.get(url, timeout=15, verify=False)
                
                # Handle rate limiting with exponential backoff
                if resp.status_code == 429:
                    backoff = min(2 ** (attempt + 1), 60)  # 2, 4, 8, 16, 32 seconds
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        backoff = max(backoff, int(retry_after))
                    logger.warning(f"Rate limited (429) on {url}, retrying in {backoff}s (attempt {attempt+1}/{max_retries})")
                    time.sleep(backoff)
                    continue
                
                resp.raise_for_status()
                content = resp.text

                # Write to cache
                try:
                    os.makedirs(os.path.dirname(cache_file), exist_ok=True)
                    with open(cache_file, "w", encoding="utf-8") as f:
                        f.write(content)
                except Exception as e:
                    logger.warning(f"Failed to cache {url}: {e}")

                return content

            except Exception as e:
                if attempt < max_retries - 1 and "429" in str(e):
                    backoff = 2 ** (attempt + 1)
                    logger.warning(f"Retry {attempt+1}/{max_retries} for {url} after error: {e}")
                    time.sleep(backoff)
                    continue
                logger.error(f"Failed to fetch {url}: {e}")
                return None
        
        logger.error(f"Exhausted {max_retries} retries for {url}")
        return None

    # ------------------------------------------------------------------ #
    #  Discovery Phase                                                     #
    # ------------------------------------------------------------------ #

    def run_discovery(self, brand_limit: Optional[str] = None) -> None:
        """Discovers product listing URLs from the specified brand(s)."""
        logger.info("Starting Crawl Discovery Phase...")
        pending = []
        brands_to_run = [brand_limit] if brand_limit else list(self.handlers.keys())
        for brand in brands_to_run:
            handler = self.handlers.get(brand)
            if not handler:
                continue
            logger.info(f"Running discovery for {brand}...")
            brand_tasks = handler.discover_sources()
            for task in brand_tasks:
                task["brand"] = brand
            pending.extend(brand_tasks)
            
        self.crawler_state["pending_urls"] = pending
        self.save_state()
        logger.info(f"Discovery Phase complete. Queued {len(pending)} source tasks.")

    # ------------------------------------------------------------------ #
    #  Data Saving — URL-Only, No Image Downloads                         #
    # ------------------------------------------------------------------ #

    def _save_or_merge_product(
        self,
        item_number: str,
        product_name: str,
        brand: str,
        scale: str,
        series: str,
        img_urls: List[str],
        source: str = "",
        release_year: Optional[int] = None,
        release_year_confidence: Optional[str] = None,
        status: Optional[str] = None,
        toy_brand: str = "MINI GT",
        sub_series: Optional[str] = None,
        attributes: dict = None
    ) -> None:
        """
        Saves product to DB, merging if already exists.
        - Skips D-prefix item numbers entirely.
        - Stores image URLs directly (no downloading).
        """
        # --- Scale filter ---
        # Bypassed for Kaido House items (always 1:64)
        is_kaido = "kaido" in series.lower() or "kaido" in brand.lower() or "kaido" in product_name.lower()
        # Skip scale filter for brands that may have mixed scales (INNO64, Tarmac Works, Trends Hobby)
        scale_exempt_brands = {"INNO64", "Tarmac Works", "Trends Hobby"}
        if not is_kaido and toy_brand not in scale_exempt_brands:
            if not scale or "1:64" not in scale:
                return

        # --- Normalise item number ---
        clean_num = re.sub(r"[^a-zA-Z0-9]", "", item_number).upper()

        if toy_brand == "MINI GT":
            # --- D-prefix filter: never save these ---
            if D_PREFIX_RE.match(clean_num):
                logger.debug(f"Skipping D-prefix product: {clean_num}")
                return

            # --- A-prefix filter: never save these ---
            if clean_num.startswith("A"):
                logger.debug(f"Skipping A-prefix product: {clean_num}")
                return

            # --- MJ-suffix filter: never save these ---
            if clean_num.endswith("MJ"):
                logger.debug(f"Skipping MJ-suffix product: {clean_num}")
                return

        if not clean_num:
            clean_num = re.sub(r"[^A-Z0-9]", "_", product_name.upper())
            if not clean_num:
                return

        brand = brand.strip() or "MINI GT"
        product_name = product_name.strip()
        series = series.strip() or "Regular"
        sub_series = (sub_series or "Regular").strip()

        # --- Deduplicate URLs ---
        seen_urls: Set[str] = set()
        clean_img_urls: List[str] = []
        for u in img_urls:
            if u and "data:image" not in u and "favicon" not in u and u not in seen_urls:
                clean_img_urls.append(u)
                seen_urls.add(u)

        with db_lock:
            with get_db_session() as session:
                model_cls = get_product_model(toy_brand)
                existing = (
                    session.query(model_cls)
                    .filter(model_cls.item_number == clean_num)
                    .first()
                )

                if existing:
                    # Determine source priorities (lower is higher priority)
                    if toy_brand == "MINI GT":
                        prio_map = {"official": 1, "myminigt": 2, "fandom": 3}
                    else:
                        prio_map = {"fandom": 1, "diecastsociety": 2}
                    incoming_prio = prio_map.get(source.lower(), 9)
                    existing_prio = prio_map.get((existing.source or "").lower(), 9)

                    # Overwrite metadata and images if incoming has higher priority
                    if incoming_prio < existing_prio:
                        existing.product_name = product_name
                        existing.brand = brand
                        
                        existing_is_generic = (existing.series or "").strip().lower() in ("regular", "regular collection", "")
                        incoming_is_generic = (series or "").strip().lower() in ("regular", "regular collection", "")
                        if not incoming_is_generic or existing_is_generic:
                            existing.series = series
                            
                        existing.sub_series = sub_series
                        existing.scale = scale
                        existing.source = source
                        existing.set_images(clean_img_urls)
                        
                        if release_year is not None:
                            existing.release_year = release_year
                            existing.release_year_confidence = release_year_confidence
                        if status is not None:
                            existing.status = status
                        if attributes:
                            try:
                                existing_attrs = json.loads(existing.attributes) if existing.attributes else {}
                                existing_attrs.update(attributes)
                                existing.attributes = json.dumps(existing_attrs, ensure_ascii=False)
                            except Exception:
                                existing.attributes = json.dumps(attributes, ensure_ascii=False)
                        logger.debug(f"Overwrote product {clean_num} with higher-priority source data")
                    elif incoming_prio == existing_prio:
                        # Tie-breaker: keep the longer product name, do not merge images
                        if len(product_name) > len(existing.product_name):
                            existing.product_name = product_name
                        
                        existing_is_generic = (existing.series or "").strip().lower() in ("regular", "regular collection", "")
                        incoming_is_generic = (series or "").strip().lower() in ("regular", "regular collection", "")
                        if not incoming_is_generic or existing_is_generic:
                            existing.series = series
                            
                        if sub_series and sub_series != "Regular":
                            existing.sub_series = sub_series
                        if existing.release_year is None:
                            existing.release_year = release_year
                            existing.release_year_confidence = release_year_confidence
                        if existing.status is None:
                            existing.status = status
                        if attributes:
                            try:
                                existing_attrs = json.loads(existing.attributes) if existing.attributes else {}
                                existing_attrs.update(attributes)
                                existing.attributes = json.dumps(existing_attrs, ensure_ascii=False)
                            except Exception:
                                existing.attributes = json.dumps(attributes, ensure_ascii=False)
                    else:
                        if existing.release_year is None and release_year is not None:
                            existing.release_year = release_year
                            existing.release_year_confidence = release_year_confidence
                        if (not existing.status or existing.status.lower() == "released") and status and status.lower() not in ("released", "none"):
                            existing.status = status
                        logger.debug(f"Ignored lower-priority source data for product {clean_num}")
                        
                    # Fallback image assignment: if existing has no valid image but incoming does
                    existing_imgs = existing.image_list or []
                    existing_has_no_valid = not existing_imgs or any("Image_Not_Available" in img for img in existing_imgs)
                    incoming_has_valid = clean_img_urls and not any("Image_Not_Available" in img for img in clean_img_urls)
                    if existing_has_no_valid and incoming_has_valid:
                        existing.set_images(clean_img_urls)
                        logger.debug(f"Assigned valid incoming image to product {clean_num} which had placeholder")
                else:
                    new_prod = model_cls(
                        toy_brand=toy_brand,
                        item_number=clean_num,
                        product_name=product_name,
                        brand=brand,
                        scale=scale,
                        series=series,
                        sub_series=sub_series,
                        source=source,
                        release_year=release_year,
                        release_year_confidence=release_year_confidence,
                        status=status,
                        attributes=json.dumps(attributes, ensure_ascii=False) if attributes else None
                    )
                    new_prod.set_images(clean_img_urls)
                    session.add(new_prod)
                    logger.info(
                        f"Added product {clean_num} ({product_name}) from marque {brand} (Source: {source})"
                    )

    # ------------------------------------------------------------------ #
    #  Parsers                                                             #
    # ------------------------------------------------------------------ #

    def _parse_official_detail(self, html: str, brand_page_name: str) -> None:
        """Parses a single product detail page from the official TSM website."""
        soup = BeautifulSoup(html, "lxml")

        name_node = soup.find(class_="pro-name")
        product_name = name_node.get_text(strip=True) if name_node else ""
        if not product_name:
            return

        item_number = ""
        scale = "1:64"
        marque = brand_page_name
        status = "Released"
        info_div = soup.find(class_=re.compile(r"info[-_]list", re.I))
        if info_div:
            for li in info_div.find_all("li"):
                txt = li.get_text(strip=True)
                tl = txt.lower()
                if "item no." in tl:
                    item_number = txt.replace("Item No.", "").replace("Item no.", "").strip()
                elif "scale" in tl:
                    scale = txt.replace("Scale", "").replace("scale", "").strip()
                elif "marque" in tl:
                    marque = txt.replace("Marque", "").replace("marque", "").strip()
                elif "status" in tl:
                    status = txt.replace("Status", "").replace("status", "").strip()

        if not item_number:
            return

        # Series mapping
        series = "Regular"
        special_brands = {
            "007 Movie Car", "QubeCarz", "IMSA",
            "KAIDOHOUSE x MINI GT", "SUPER GT SERIES",
        }
        if brand_page_name in special_brands and brand_page_name != marque:
            series = brand_page_name

        # Parse inferred year from product name
        release_year = None
        release_year_confidence = None
        ym = re.search(r"\b(20\d{2})\b", product_name)
        if ym:
            release_year = int(ym.group(1))
            release_year_confidence = "inferred"

        # Image URLs — collect all product image src attributes
        # Real URL patterns observed on live site:
        #   upload/mini_gt/products_gif/product_pic_big/...   (older products)
        #   upload/picfile/YYYY/MM/...                        (newer products)
        # Container class is 'product_hover product_box' (NOT pro_wrap-d / product_gallery)
        img_urls: List[str] = []
        seen_img: Set[str] = set()

        def _is_product_img(src: str) -> bool:
            """Returns True if src looks like a full-size product photo (not a thumbnail/logo)."""
            if not src:
                return False
            s = src.lower()
            return (
                "upload/mini_gt/products_gif/product_pic_big" in s
                or "upload/picfile" in s
            ) and not s.endswith(".svg")


        def _abs(src: str) -> str:
            return src if src.startswith("http") else f"https://minigt.tsm-models.com/{src.lstrip('/')}"

        def _is_inside_related(node) -> bool:
            curr = node
            for _ in range(8):
                curr = curr.parent
                if not curr or curr.name in (None, "[document]"):
                    break
                cls = curr.get("class", [])
                if isinstance(cls, str):
                    cls = [cls]
                if any(c in cls for c in ["related_pro"]):
                    return True
            return False

        # Primary: any elements with class 'product_box' (e.g. 'a' or 'div')
        # excluding images inside known related-products containers
        for tag in soup.find_all(class_="product_box"):
            if _is_inside_related(tag):
                continue
            img = tag if tag.name == "img" else tag.find("img")
            if img:
                src = img.get("src") or img.get("data-src", "")
                if _is_product_img(src):
                    url = _abs(src)
                    if url not in seen_img:
                        img_urls.append(url)
                        seen_img.add(url)

        # Fallback: any img on the page that matches the product path patterns,
        # excluding images inside known related-products containers
        if not img_urls:
            for img in soup.find_all("img"):
                if _is_inside_related(img):
                    continue
                src = img.get("src") or img.get("data-src", "")
                if not _is_product_img(src):
                    continue
                url = _abs(src)
                if url not in seen_img:
                    img_urls.append(url)
                    seen_img.add(url)

        self._save_or_merge_product(
            item_number, product_name, marque, scale, series, img_urls,
            source="official", release_year=release_year,
            release_year_confidence=release_year_confidence, status=status
        )

    def _parse_official_list(
        self, html: str, brand_name: str, b_id: str, page: int
    ) -> List[Dict]:
        """Parses a brand product-list page and returns new tasks to queue."""
        soup = BeautifulSoup(html, "lxml")

        detail_ids: Set[str] = set()
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if "action=product-detail" in href:
                id_match = re.search(r"id=(\d+)", href)
                if id_match:
                    detail_ids.add(id_match.group(1))

        if not detail_ids:
            return []  # Last page reached

        new_tasks: List[Dict] = []
        for det_id in detail_ids:
            new_tasks.append(
                {
                    "source": "official_detail",
                    "url": (
                        f"https://minigt.tsm-models.com/index.php"
                        f"?action=product-detail&id={det_id}"
                    ),
                    "meta": {"brand_page_name": brand_name},
                }
            )

        # Queue next page
        new_tasks.append(
            {
                "source": "official_list",
                "url": (
                    f"https://minigt.tsm-models.com/index.php"
                    f"?action=product-list&b_id={b_id}&p={page + 1}"
                ),
                "meta": {"brand_name": brand_name, "b_id": b_id, "page": page + 1},
            }
        )
        return new_tasks

    def _parse_fandom_page(self, json_str: str, page_name: str) -> None:
        """Parses product tables and plain lists from a Fandom Wiki article."""
        try:
            res_data = json.loads(json_str)
            if "parse" not in res_data or "text" not in res_data["parse"]:
                return
            html_content = res_data["parse"]["text"]["*"]
            soup = BeautifulSoup(html_content, "lxml")
        except Exception as e:
            logger.error(f"Fandom JSON parse error for {page_name}: {e}")
            return

        parsed_codes = set()
        status = "Cancelled" if page_name == "Cancelled_Models" else None
        release_year = None
        release_year_confidence = None
        ym = re.search(r"\b(20\d{2})\b", page_name)
        if ym:
            release_year = int(ym.group(1))
            release_year_confidence = "confirmed"

        # 1. Parse tables
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            code_idx = name_idx = brand_idx = photo_idx = -1

            for idx, h in enumerate(headers):
                if "model #" in h:
                    code_idx = idx
                elif any(k in h for k in ("code", "item", "number", "toy", "sku")):
                    code_idx = idx
                elif h == "model":
                    name_idx = idx
                elif any(k in h for k in ("name", "model")):
                    name_idx = idx
                elif any(k in h for k in ("brand", "marque")):
                    brand_idx = idx
                elif any(k in h for k in ("photo", "image", "pic")):
                    photo_idx = idx

            if code_idx == -1 or name_idx == -1:
                continue

            for row in table.find_all("tr")[1:]:
                cells = row.find_all(["td", "th"])
                if len(cells) <= max(code_idx, name_idx):
                    continue

                item_number = cells[code_idx].get_text(strip=True)
                product_name = cells[name_idx].get_text(strip=True)

                if not item_number or not product_name or item_number.strip() == "-":
                    continue

                brand = "MINI GT"
                if brand_idx != -1 and brand_idx < len(cells):
                    brand = cells[brand_idx].get_text(strip=True) or "MINI GT"
                elif page_name not in {
                    "2018_Models", "2019_Models", "2020_Models",
                    "2021_Models", "2022_Models", "2023_Models", "Full_Collection",
                }:
                    brand = page_name.replace("_", " ")

                # Extract images using robust finder and fallback
                img_urls = get_row_product_images(row)
                if not img_urls and photo_idx != -1 and photo_idx < len(cells):
                    img_tag = cells[photo_idx].find("img")
                    if img_tag:
                        img_url = img_tag.get("data-src") or img_tag.get("src", "")
                        if img_url and "data:image" not in img_url:
                            img_url = clean_fandom_image_url(img_url)
                            img_urls = [img_url]

                clean_num = re.sub(r"[^a-zA-Z0-9]", "", item_number).upper()
                parsed_codes.add(clean_num)

                self._save_or_merge_product(
                    item_number, product_name, brand, "1:64", "Regular", img_urls,
                    source="fandom", release_year=release_year,
                    release_year_confidence=release_year_confidence, status=status
                )

        # 2. Parse plain list items <li> and paragraphs <p>
        item_pattern = re.compile(
            r"\b(MGT[0-9]{5}[A-Z]*|MGTAC[0-9]+|MGTS[0-9]+|KHMG[0-9]{3}|K[0-9]+|[0-9]{2}OEM[0-9]{2}|AC[0-9]+)\b",
            re.I
        )
        for tag in soup.find_all(["li", "p"]):
            txt = tag.get_text(" ", strip=True)
            parts = re.split(r"[-–—:]", txt, maxsplit=1)
            if len(parts) == 2:
                code_candidate = parts[0].strip()
                name_candidate = parts[1].strip()
                if item_pattern.match(code_candidate) and len(name_candidate) > 3:
                    clean_num = re.sub(r"[^a-zA-Z0-9]", "", code_candidate).upper()
                    if clean_num not in parsed_codes:
                        parsed_codes.add(clean_num)
                        
                        # Try to find an adjacent image
                        img_urls = []
                        img_tag = tag.find("img")
                        if not img_tag:
                            nxt = tag.next_sibling
                            if nxt and nxt.name in ("p", "div", "span"):
                                img_tag = nxt.find("img")
                        if img_tag:
                            img_url = img_tag.get("data-src") or img_tag.get("src", "")
                            if img_url and "data:image" not in img_url:
                                img_url = clean_fandom_image_url(img_url)
                                img_urls.append(img_url)
                        
                        brand = "MINI GT"
                        if page_name not in {
                            "2018_Models", "2019_Models", "2020_Models",
                            "2021_Models", "2022_Models", "2023_Models", "Full_Collection",
                        }:
                            brand = page_name.replace("_", " ")

                        self._save_or_merge_product(
                            code_candidate, name_candidate, brand, "1:64", "Regular", img_urls,
                            source="fandom", release_year=release_year,
                            release_year_confidence=release_year_confidence, status=status
                        )

    def _parse_myminigt_detail(self, html: str, detail_url: str) -> None:
        """Parses a model detail page from myminigt.com via JSON-LD schema."""
        soup = BeautifulSoup(html, "lxml")

        next_data = soup.find("script", type="application/ld+json")
        if not next_data:
            return

        try:
            js = json.loads(next_data.string)
            product_node = None
            if isinstance(js, dict) and "@graph" in js:
                for node in js["@graph"]:
                    if node.get("@type") == "Product":
                        product_node = node
                        break
            elif isinstance(js, dict) and js.get("@type") == "Product":
                product_node = js

            if not product_node:
                return

            product_name = product_node.get("name", "").strip()
            sku = product_node.get("sku", "").strip()

            if not sku:
                return

            # Check if name starts with a model code (e.g. KHMG195 or KH195 or MGT00195)
            code_match = re.match(r"^([A-Z0-9_-]+)\b", product_name, re.IGNORECASE)
            item_number = ""
            if code_match:
                matched_code = code_match.group(1).upper()
                if any(matched_code.startswith(pfx) for pfx in ["KHMG", "KH", "MGT", "DM", "DBW", "BL", "S", "XX"]):
                    item_number = matched_code
                    # Strip the item number from the beginning of the name for cleaner storage
                    product_name = product_name[code_match.end():].strip().lstrip("-").strip()

            if not item_number:
                # Fall back to SKU formatting
                item_number = f"MGT{sku.zfill(5)}" if sku.isdigit() else sku

            brand = "MINI GT"
            brand_node = product_node.get("brand")
            if isinstance(brand_node, dict):
                brand = brand_node.get("name", "MINI GT")

            series = product_node.get("category", "Regular") or "Regular"
            img_url = product_node.get("image", "")
            img_urls = [img_url] if img_url else []

            release_year = None
            release_year_confidence = None
            release_date = product_node.get("releaseDate")
            if release_date:
                ym = re.match(r"^(\d{4})", str(release_date))
                if ym:
                    release_year = int(ym.group(1))
                    release_year_confidence = "confirmed"

            # Parse status from description when available
            description = product_node.get("description", "").lower()
            if "pre order" in description or "pre-order" in description:
                status = "Pre-Order"
            elif "cancelled" in description or "discontinued" in description:
                status = "Cancelled"
            else:
                status = "Released"

            self._save_or_merge_product(
                item_number, product_name, brand, "1:64", series, img_urls,
                source="myminigt", release_year=release_year,
                release_year_confidence=release_year_confidence, status=status
            )
        except Exception as e:
            logger.error(f"MyMiniGT JSON-LD parsing failed for {detail_url}: {e}")

    # ------------------------------------------------------------------ #
    #  Core Crawler Loop                                                   #
    # ------------------------------------------------------------------ #

    def run_crawler(self, brand_limit: Optional[str] = None) -> None:
        """
        Runs the concurrent task queue loop with correct progress tracking.
        Supports pause/resume via saved state.
        """
        logger.info("Starting Crawl Execution Phase...")

        if not self.crawler_state.get("pending_urls"):
            self.run_discovery(brand_limit)

        pending_queue: List[Dict] = self.crawler_state["pending_urls"]
        crawled_urls: Set[str] = set(self.crawler_state.get("crawled_urls", []))

        # FIX: initialize total from starting queue size; updated atomically when tasks added
        with counter_lock:
            self._total_tasks = len(pending_queue)
            self._completed_tasks = 0

        logger.info(f"Crawl Queue size: {self._total_tasks} tasks.")

        while pending_queue:
            # Dequeue a batch
            with state_lock:
                batch_size = min(len(pending_queue), self.max_workers * 2)
                current_batch = [pending_queue.pop(0) for _ in range(batch_size) if pending_queue]

            if not current_batch:
                break

            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                future_to_task: Dict = {}
                for task in current_batch:
                    url = task["url"]
                    if url in crawled_urls and task["source"] != "official_list":
                        # Already processed — count but don't re-fetch
                        with counter_lock:
                            self._completed_tasks += 1
                        continue
                    fut = executor.submit(self.fetch_url, url, True)
                    future_to_task[fut] = task

                for future in as_completed(future_to_task):
                    task = future_to_task[future]
                    url = task["url"]
                    source = task["source"]
                    meta = task["meta"]
                    brand = task.get("brand", "MINI GT")

                    try:
                        html = future.result()
                        if html:
                            handler = self.handlers.get(brand)
                            if handler:
                                new_tasks = handler.parse_task(html, task)
                                if new_tasks:
                                    for nt in new_tasks:
                                        nt["brand"] = brand
                                    with state_lock:
                                        pending_queue.extend(new_tasks)
                                    # FIX: update total as new tasks are discovered
                                    with counter_lock:
                                        self._total_tasks += len(new_tasks)

                            crawled_urls.add(url)
                    except Exception as e:
                        logger.error(f"Error executing task for {url}: {e}")
                        # Re-queue failed task (only once)
                        with state_lock:
                            pending_queue.append(task)
                        with counter_lock:
                            self._total_tasks += 1  # count the re-queued task

                    with counter_lock:
                        self._completed_tasks += 1
                        done = self._completed_tasks
                        total = self._total_tasks

                    if done % 20 == 0:
                        logger.info(f"Progress: {done}/{total} tasks completed.")
                        # Incremental state save
                        with state_lock:
                            self.crawler_state["pending_urls"] = list(pending_queue)
                            self.crawler_state["crawled_urls"] = list(crawled_urls)
                        self.save_state()

        # Final cleanup + JSON sync
        with state_lock:
            self.crawler_state["pending_urls"] = []
            self.crawler_state["crawled_urls"] = list(crawled_urls)
        self.save_state()

        logger.info("Crawl execution phase complete. Syncing database to products.json...")
        sync_to_json()
        logger.info("Scrape and sync complete!")
        print("\n[SUCCESS] Scrape and JSON sync complete!")


class MiniGTBrandHandler:
    def __init__(self, crawler: "MINI_GTCrawler"):
        self.crawler = crawler

    def discover_sources(self) -> List[Dict]:
        pending = []
        # 1. Discover Official site brands
        official_brands = []
        logger.info("Discovering Official site Brands...")
        off_html = self.crawler.fetch_url(
            "https://minigt.tsm-models.com/index.php?action=product", use_cache=False
        )
        if off_html:
            soup = BeautifulSoup(off_html, "lxml")
            brands_dict = {}
            for link in soup.find_all("a", href=True):
                href = link["href"]
                text = link.get_text(strip=True)
                if "action=product-list" in href:
                    b_id_match = re.search(r"b_id=(\d+)", href)
                    if b_id_match:
                        b_id = b_id_match.group(1)
                        if text and text not in brands_dict.values():
                            brands_dict[b_id] = text
            for b_id, name in brands_dict.items():
                official_brands.append({"b_id": b_id, "name": name})

        self.crawler.crawler_state["discovered_sources"]["official_brands"] = official_brands
        for brand in official_brands:
            pending.append(
                {
                    "source": "official_list",
                    "url": (
                        f"https://minigt.tsm-models.com/index.php"
                        f"?action=product-list&b_id={brand['b_id']}&p=1"
                    ),
                    "meta": {"brand_name": brand["name"], "b_id": brand["b_id"], "page": 1},
                }
            )

        # 2. Discover Fandom Wiki Category pages
        fandom_pages = []
        logger.info("Discovering Fandom Wiki category pages...")
        fandom_api = (
            "https://minigt.fandom.com/api.php"
            "?action=parse&page=MINI_GT&format=json&prop=text"
        )
        api_res = self.crawler.fetch_url(fandom_api, use_cache=False)
        if api_res:
            try:
                res_data = json.loads(api_res)
                html_content = res_data["parse"]["text"]["*"]
                soup = BeautifulSoup(html_content, "lxml")
                links = set()
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if "/wiki/" in href and not any(
                        x in href for x in [":", "Main_Page", "Special:", "File:", "Category:"]
                    ):
                        page_name = href.split("/wiki/")[-1]
                        page_name = urllib.parse.unquote(page_name)
                        links.add(page_name)
                fandom_pages = list(links)
            except Exception as e:
                logger.error(f"Failed to parse Fandom Wiki API: {e}")
        self.crawler.crawler_state["discovered_sources"]["fandom_pages"] = fandom_pages
        for page in fandom_pages:
            api_url = (
                f"https://minigt.fandom.com/api.php"
                f"?action=parse&page={urllib.parse.quote(page)}&format=json&prop=text"
            )
            pending.append({"source": "fandom", "url": api_url, "meta": {"page_name": page}})

        # 3. Discover MyMiniGT sitemaps
        myminigt_urls = []
        logger.info("Discovering MyMiniGT catalog items from sitemap...")
        sitemap_html = self.crawler.fetch_url("https://myminigt.com/sitemap.xml", use_cache=True)
        if sitemap_html:
            soup = BeautifulSoup(sitemap_html, "lxml-xml")
            for loc in soup.find_all("loc"):
                loc_url = loc.get_text(strip=True)
                if "modelId=" in loc_url:
                    myminigt_urls.append(loc_url)
        self.crawler.crawler_state["discovered_sources"]["myminigt_urls"] = myminigt_urls
        crawled = set(self.crawler.crawler_state.get("crawled_urls", []))
        for url in myminigt_urls:
            if url not in crawled:
                pending.append({"source": "myminigt", "url": url, "meta": {}})

        return pending

    def parse_task(self, html_or_json: str, task: Dict) -> Optional[List[Dict]]:
        source = task["source"]
        meta = task["meta"]
        url = task["url"]
        
        if source == "official_list":
            return self.crawler._parse_official_list(html_or_json, meta["brand_name"], meta["b_id"], meta["page"])
        elif source == "official_detail":
            self.crawler._parse_official_detail(html_or_json, meta["brand_page_name"])
        elif source == "fandom":
            self.crawler._parse_fandom_page(html_or_json, meta["page_name"])
        elif source == "myminigt":
            self.crawler._parse_myminigt_detail(html_or_json, url)
        return None


class HotWheelsBrandHandler:
    def __init__(self, crawler: "MINI_GTCrawler"):
        self.crawler = crawler
        self.seen_castings = set()

    def discover_sources(self) -> List[Dict]:
        pending = []
        seen_pages = set()
        
        # Load the reference filters JSON which contains all categories and list links
        json_path = os.path.join(os.path.dirname(__file__), "..", "reference_htmls", "hot_wheels_filters.json")
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                hw_filters = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load hot_wheels_filters.json: {e}")
            return []

        # Extract all links
        urls = set()
        def extract_links(obj):
            if isinstance(obj, dict):
                if "link" in obj: urls.add(obj["link"])
                if "category_link" in obj: urls.add(obj["category_link"])
                for v in obj.values(): extract_links(v)
            elif isinstance(obj, list):
                for v in obj: extract_links(v)
                
        extract_links(hw_filters.get("filters", {}))
        
        # We process each url depending on whether it's a Category or a List
        for url in urls:
            # extract page title
            if "/wiki/" not in url:
                continue
            page = url.split("/wiki/")[-1]
            page_decoded = urllib.parse.unquote(page)
            
            if page_decoded.startswith("Category:"):
                cmcontinue = ""
                while True:
                    api_url = (
                        f"https://hotwheels.fandom.com/api.php?action=query&list=categorymembers"
                        f"&cmtitle={urllib.parse.quote(page_decoded)}&cmlimit=500&format=json"
                    )
                    if cmcontinue:
                        api_url += f"&cmcontinue={cmcontinue}"
                        
                    res_json = self.crawler.fetch_url(api_url, use_cache=True)
                    if not res_json:
                        break
                        
                    try:
                        data = json.loads(res_json)
                        members = data.get("query", {}).get("categorymembers", [])
                        for m in members:
                            member_page = m["title"]
                            if member_page.startswith("Category:") or any(x in member_page for x in ["Special:", "File:", "Template:", "Help:"]):
                                continue
                            if member_page in seen_pages:
                                continue
                            seen_pages.add(member_page)
                            
                            page_api = (
                                f"https://hotwheels.fandom.com/api.php"
                                f"?action=parse&page={urllib.parse.quote(member_page)}&format=json&prop=text"
                            )
                            # Infer release year
                            year = None
                            ym = re.search(r"\b(20\d{2})\b", member_page)
                            if not ym:
                                ym = re.search(r"\b(19\d{2})\b", member_page)
                            if ym:
                                year = int(ym.group(1))
                                
                            pending.append({
                                "source": "fandom_list",
                                "url": page_api,
                                "meta": {
                                    "page_name": member_page,
                                    "year": year,
                                    "series_group": page_decoded.replace("Category:", "").replace("_", " "),
                                    "sub_series": "Regular"
                                }
                            })
                        
                        cmcontinue = data.get("continue", {}).get("cmcontinue", "")
                        if not cmcontinue:
                            break
                    except Exception as e:
                        logger.error(f"Error parsing Hot Wheels category {page_decoded}: {e}")
                        break
            else:
                # Direct list page
                if page_decoded in seen_pages or any(x in page_decoded for x in ["Special:", "File:", "Template:", "Help:"]):
                    continue
                seen_pages.add(page_decoded)
                
                page_api = (
                    f"https://hotwheels.fandom.com/api.php"
                    f"?action=parse&page={urllib.parse.quote(page_decoded)}&format=json&prop=text"
                )
                year = None
                ym = re.search(r"\b(20\d{2})\b", page_decoded)
                if not ym:
                    ym = re.search(r"\b(19\d{2})\b", page_decoded)
                if ym:
                    year = int(ym.group(1))
                    
                pending.append({
                    "source": "fandom_list",
                    "url": page_api,
                    "meta": {
                        "page_name": page_decoded,
                        "year": year,
                        "series_group": "Lists",
                        "sub_series": "Regular"
                    }
                })

        # To guarantee ABSOLUTELY NO RESTRICTIONS, we also exhaustively crawl ALL PAGES (20k+)
        logger.info("Hot Wheels discovery: initiating complete allpages traversal for 20k+ coverage...")
        apcontinue = ""
        while True:
            api_url = (
                f"https://hotwheels.fandom.com/api.php?action=query&list=allpages"
                f"&apnamespace=0&aplimit=500&format=json"
            )
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
                    if any(x in page_name for x in [":", "Main_Page", "Category:", "Special:", "File:", "Template:", "Help:"]):
                        continue
                        
                    if page_name in seen_pages:
                        continue
                    seen_pages.add(page_name)
                    
                    page_api = f"https://hotwheels.fandom.com/api.php?action=parse&page={urllib.parse.quote(page_name)}&format=json&prop=text"
                    
                    # Infer release year
                    year = None
                    ym = re.search(r"\b(20\d{2})\b", page_name)
                    if not ym:
                        ym = re.search(r"\b(19\d{2})\b", page_name)
                    if ym:
                        year = int(ym.group(1))

                    pending.append({
                        "source": "fandom_casting",
                        "url": page_api,
                        "meta": {
                            "page_name": page_name,
                            "year": year,
                            "series_group": "Complete Collection",
                            "sub_series": "Regular"
                        }
                    })
                
                apcontinue = data.get("continue", {}).get("apcontinue", "")
                if not apcontinue:
                    break
            except Exception as e:
                logger.error(f"Error fetching Hot Wheels Fandom allpages: {e}")
                break

        logger.info(f"Hot Wheels discovery: found {len(pending)} definitive pages across categories, lists, and exhaustive allpages.")
        return pending

    def parse_task(self, html_or_json: str, task: Dict) -> Optional[List[Dict]]:
        try:
            res_data = json.loads(html_or_json)
            if "parse" not in res_data or "text" not in res_data["parse"]:
                return None
            html_content = res_data["parse"]["text"]["*"]
            soup = BeautifulSoup(html_content, "lxml")
        except Exception as e:
            logger.error(f"Hot Wheels JSON parse error for {task['meta']['page_name']}: {e}")
            return None

        meta = task["meta"]
        page_name = meta.get("page_name", "")
        source_type = task.get("source")
        
        # Check if it is a category page
        if page_name.startswith("Category:"):
            new_tasks = []
            seen_links = set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                href_decoded = urllib.parse.unquote(href)
                if "/wiki/" in href_decoded:
                    parts = href_decoded.split("/wiki/")
                    member_page = parts[-1]
                    if any(x in member_page for x in [":", "Main_Page", "Special:", "File:", "Category:", "Help:", "Template:"]):
                        continue
                    if member_page not in seen_links:
                        seen_links.add(member_page)
                        api_url = (
                            f"https://hotwheels.fandom.com/api.php"
                            f"?action=parse&page={urllib.parse.quote(member_page)}&format=json&prop=text"
                        )
                        new_tasks.append({
                            "source": "fandom_list",
                            "url": api_url,
                            "meta": {
                                "page_name": member_page,
                                "year": meta.get("year"),
                                "series_group": "Category Member",
                                "sub_series": "Regular"
                            }
                        })
            return new_tasks

        # Otherwise, parse product tables
        new_tasks = []
        page_year = meta.get("year")
        series_group = meta.get("series_group", "By Year")
        default_sub_series = meta.get("sub_series", "Regular")

        # Determine casting name if we are on a casting page
        casting_name = page_name.replace("_", " ")

        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            
            code_idx = name_idx = series_idx = photo_idx = year_idx = color_idx = -1
            for idx, h in enumerate(headers):
                if "toy #" in h or h == "toy":
                    code_idx = idx
                elif any(k in h for k in ("code", "item", "number", "toy", "sku")):
                    code_idx = idx
                elif "model name" in h or h == "model":
                    name_idx = idx
                elif any(k in h for k in ("name", "model")):
                    name_idx = idx
                elif "color" in h:
                    color_idx = idx
                elif "series" in h:
                    series_idx = idx
                elif "year" in h:
                    year_idx = idx
                elif any(k in h for k in ("photo", "image", "pic")):
                    photo_idx = idx

            if code_idx == -1 and name_idx == -1 and color_idx == -1:
                continue

            for row in table.find_all("tr")[1:]:
                cells = row.find_all(["td", "th"])
                if len(cells) <= max(code_idx, name_idx, color_idx, series_idx, year_idx):
                    continue

                item_number = ""
                if code_idx != -1:
                    item_number = cells[code_idx].get_text(strip=True)
                    if item_number == "-":
                        item_number = ""

                row_name = ""
                if name_idx != -1:
                    row_name = cells[name_idx].get_text(strip=True)
                
                row_color = ""
                if color_idx != -1:
                    row_color = cells[color_idx].get_text(strip=True)

                if source_type == "fandom_casting":
                    if row_color:
                        product_name = f"{casting_name} ({row_color})"
                    elif row_name:
                        product_name = f"{casting_name} ({row_name})"
                    else:
                        product_name = casting_name
                else:
                    product_name = row_name or row_color or casting_name

                if not product_name:
                    continue

                row_year = page_year
                if year_idx != -1:
                    year_str = cells[year_idx].get_text(strip=True)
                    ym = re.search(r"\b((?:19|20)\d{2})\b", year_str)
                    if ym:
                        row_year = int(ym.group(1))

                series = series_group
                sub_series = default_sub_series
                if series_idx != -1:
                    cell_series = cells[series_idx].get_text(" ", strip=True)
                    series_cleaned = cell_series.split("\n")[0].split("New for")[0].strip()
                    if series_cleaned:
                        sub_series = series_cleaned

                img_urls = get_row_product_images(row)
                if not img_urls and photo_idx != -1 and photo_idx < len(cells):
                    img_tag = cells[photo_idx].find("img")
                    if img_tag:
                        img_url = img_tag.get("data-src") or img_tag.get("src", "")
                        if img_url and "data:image" not in img_url:
                            img_url = clean_fandom_image_url(img_url)
                            img_urls = [img_url]

                # If this is a list page, discover casting page links!
                if source_type == "fandom_list":
                    target_cells = []
                    if name_idx != -1:
                        target_cells.append(cells[name_idx])
                    if color_idx != -1:
                        target_cells.append(cells[color_idx])
                    for cell in target_cells:
                        for a in cell.find_all("a", href=True):
                            href = a["href"]
                            if "/wiki/" in href:
                                casting_page = href.split("/wiki/")[-1]
                                casting_page = urllib.parse.unquote(casting_page)
                                if casting_page and not any(x in casting_page for x in [":", "Main_Page", "Special:", "File:", "Category:", "Help:", "Template:"]):
                                    if "List_of_" not in casting_page:
                                        api_url = (
                                            f"https://hotwheels.fandom.com/api.php"
                                            f"?action=parse&page={urllib.parse.quote(casting_page)}&format=json&prop=text"
                                        )
                                        if casting_page not in self.seen_castings:
                                            self.seen_castings.add(casting_page)
                                            new_tasks.append({
                                                "source": "fandom_casting",
                                                "url": api_url,
                                                "meta": {
                                                    "page_name": casting_page,
                                                    "year": row_year,
                                                    "series_group": "Casting Variation"
                                                }
                                            })

                attributes = {}
                if row_color:
                    attributes["color"] = row_color
                if row_name:
                    attributes["vehicle_model"] = row_name
                
                self.crawler._save_or_merge_product(
                    item_number=item_number,
                    product_name=product_name,
                    brand="Hot Wheels",
                    scale="1:64",
                    series=series,
                    img_urls=img_urls,
                    source="fandom",
                    release_year=row_year,
                    release_year_confidence="confirmed" if row_year else None,
                    status="Released",
                    toy_brand="Hot Wheels",
                    sub_series=sub_series,
                    attributes=attributes
                )
        return new_tasks

class PopRaceBrandHandler:
    def __init__(self, crawler: "MINI_GTCrawler"):
        self.crawler = crawler
        self._page_series_map = {}  # page_name -> series

    def _load_series_map(self):
        """Load pop_race_filters.json and build a page→series lookup."""
        json_path = os.path.join(os.path.dirname(__file__), "..", "reference_htmls", "pop_race_filters.json")
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                pr_filters = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load pop_race_filters.json: {e}")
            return

        filters = pr_filters.get("filters", {})

        # "collection" contains top-level series: Regular Collection, Enigma, Event Exclusives, etc.
        for item in filters.get("collection", []):
            series_name = item.get("name", "")
            link = item.get("link", "")
            if link and "/wiki/" in link:
                page = urllib.parse.unquote(link.split("/wiki/")[-1])
                self._page_series_map[page] = series_name
            # Some series pages have children (sub-pages listed under them)
            for child in item.get("children", []):
                child_link = child.get("link", "")
                if child_link and "/wiki/" in child_link:
                    child_page = urllib.parse.unquote(child_link.split("/wiki/")[-1])
                    self._page_series_map[child_page] = series_name

        # "by_manufacturer" has nested structure: region -> manufacturer -> model pages
        by_mfr = filters.get("by_manufacturer", {})
        for region_name, mfr_list in by_mfr.items():
            if not isinstance(mfr_list, list):
                continue
            for mfr in mfr_list:
                mfr_link = mfr.get("link", "")
                if mfr_link and "/wiki/" in mfr_link:
                    mfr_page = urllib.parse.unquote(mfr_link.split("/wiki/")[-1])
                    # Manufacturer pages aren't series pages, but their children are model pages
                for child in mfr.get("children", []):
                    child_link = child.get("link", "")
                    if child_link and "/wiki/" in child_link:
                        child_page = urllib.parse.unquote(child_link.split("/wiki/")[-1])
                        # Don't overwrite if already assigned from "collection"
                        if child_page not in self._page_series_map:
                            self._page_series_map[child_page] = "Regular Collection"

        # "by_year" has year pages and "Pre-Order" — Pre-Order is a status, not a series
        for item in filters.get("by_year", []):
            year_name = item.get("name", "")
            link = item.get("link", "")
            if link and "/wiki/" in link:
                page = urllib.parse.unquote(link.split("/wiki/")[-1])
                if year_name.lower() != "pre-order":
                    if page not in self._page_series_map:
                        self._page_series_map[page] = "Regular Collection"

        logger.info(f"Pop Race series map loaded: {len(self._page_series_map)} page->series mappings")

    def _get_series_for_page(self, page_name: str) -> str:
        """Look up the correct series for a page, defaulting to Regular Collection."""
        if page_name in self._page_series_map:
            return self._page_series_map[page_name]
        # Also try URL-encoded variants
        encoded = page_name.replace(" ", "_")
        if encoded in self._page_series_map:
            return self._page_series_map[encoded]
        return "Regular Collection"

    def discover_sources(self) -> List[Dict]:
        pending = []
        seen_urls = set()

        # Load the series map from reference JSON
        self._load_series_map()

        # 1. Exhaustively crawl all pages from Pop Race Fandom
        apcontinue = ""
        while True:
            api_url = (
                f"https://pop-race.fandom.com/api.php?action=query&list=allpages"
                f"&apnamespace=0&aplimit=500&format=json"
            )
            if apcontinue:
                api_url += f"&apcontinue={urllib.parse.quote(apcontinue)}"
                
            res_json = self.crawler.fetch_url(api_url, use_cache=False)
            if not res_json:
                break
                
            try:
                data = json.loads(res_json)
                pages = data.get("query", {}).get("allpages", [])
                for p in pages:
                    page_name = p["title"]
                    if any(x in page_name for x in [":", "Main_Page", "Category:"]):
                        continue
                        
                    page_api = f"https://pop-race.fandom.com/api.php?action=parse&page={urllib.parse.quote(page_name)}&format=json&prop=text"
                    if page_api not in seen_urls:
                        seen_urls.add(page_api)
                        series = self._get_series_for_page(page_name)
                        pending.append({
                            "source": "fandom_list",
                            "url": page_api,
                            "meta": {
                                "page_name": page_name,
                                "series": series
                            }
                        })
                
                apcontinue = data.get("continue", {}).get("apcontinue", "")
                if not apcontinue:
                    break
            except Exception as e:
                logger.error(f"Error fetching Pop Race Fandom allpages: {e}")
                break

        # Diecastsociety fallback
        pending.append({"source": "diecastsociety_search", "url": "https://diecastsociety.com/page/1/?s=Pop+Race", "meta": {"page": 1}})

        return pending

    def parse_task(self, html_or_json: str, task: Dict) -> Optional[List[Dict]]:
        source = task["source"]
        meta = task["meta"]

        if source == "fandom_list":
            return self._parse_fandom_list(html_or_json, meta)
        elif source == "diecastsociety_search":
            return self._parse_diecastsociety_search(html_or_json, meta)
        elif source == "diecastsociety_post":
            self._parse_diecastsociety_post(html_or_json, task["url"])
        return None

    def _parse_fandom_list(self, html_or_json: str, meta: Dict) -> Optional[List[Dict]]:
        try:
            res_data = json.loads(html_or_json)
            if "parse" not in res_data or "text" not in res_data["parse"]:
                return None
            html_content = res_data["parse"]["text"]["*"]
            soup = BeautifulSoup(html_content, "lxml")
        except Exception as e:
            logger.error(f"Pop Race JSON parse error for {meta['page_name']}: {e}")
            return None

        page_name = meta.get("page_name", "")
        # Check if it is a category page
        if page_name.startswith("Category:"):
            new_tasks = []
            seen_links = set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                href_decoded = urllib.parse.unquote(href)
                if "/wiki/" in href_decoded:
                    parts = href_decoded.split("/wiki/")
                    member_page = parts[-1]
                    if any(x in member_page for x in [":", "Main_Page", "Special:", "File:", "Category:", "Help:", "Template:"]):
                        continue
                    
                    # Exclude non-Pop Race brands
                    member_clean = member_page.replace("_", " ").lower()
                    if any(b in member_clean for b in ["bm creations", "inno64", "mini gt", "para64", "tarmac works", "unique model"]):
                        continue
                        
                    if member_page not in seen_links:
                        seen_links.add(member_page)
                        api_url = (
                            f"https://pop-race.fandom.com/api.php"
                            f"?action=parse&page={urllib.parse.quote(member_page)}&format=json&prop=text"
                        )
                        new_tasks.append({
                            "source": "fandom_list",
                            "url": api_url,
                            "meta": {
                                "page_name": member_page,
                                "series": meta.get("series", "Regular Collection")
                            }
                        })
            return new_tasks

        # Otherwise, parse product tables
        default_series = meta.get("series", "Regular Collection")

        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            
            code_idx = name_idx = make_idx = year_idx = photo_idx = -1
            for idx, h in enumerate(headers):
                if "model #" in h:
                    code_idx = idx
                elif any(k in h for k in ("code", "item", "number", "toy", "sku")):
                    code_idx = idx
                elif h == "model":
                    name_idx = idx
                elif any(k in h for k in ("name", "model")):
                    name_idx = idx
                elif "make" in h:
                    make_idx = idx
                elif "release" in h:
                    year_idx = idx
                elif any(k in h for k in ("photo", "image", "pic")):
                    photo_idx = idx

            if code_idx == -1 or name_idx == -1:
                continue

            for row in table.find_all("tr")[1:]:
                cells = row.find_all(["td", "th"])
                if len(cells) <= max(code_idx, name_idx):
                    continue

                item_number = cells[code_idx].get_text(strip=True)
                product_name = cells[name_idx].get_text(strip=True)

                if not item_number or not product_name or item_number.strip() == "-":
                    continue

                brand = "Pop Race"
                if make_idx != -1 and make_idx < len(cells):
                    m_val = cells[make_idx].get_text(strip=True)
                    if m_val:
                        brand = m_val

                release_year = None
                release_year_confidence = None
                status = "Released"
                if year_idx != -1 and year_idx < len(cells):
                    release_val = cells[year_idx].get_text(strip=True)
                    ym = re.search(r"\b(20\d{2})\b", release_val)
                    if ym:
                        release_year = int(ym.group(1))
                        release_year_confidence = "confirmed"
                    if "pre-order" in release_val.lower() or "pre order" in release_val.lower():
                        status = "Pre-Order"

                if release_year is None and page_name.isdigit():
                    release_year = int(page_name)
                    release_year_confidence = "confirmed"

                if "pre-order" in page_name.lower() or "pre order" in page_name.lower():
                    status = "Pre-Order"

                # Extract images using robust finder and fallback
                img_urls = get_row_product_images(row)
                if not img_urls and photo_idx != -1 and photo_idx < len(cells):
                    img_tag = cells[photo_idx].find("img")
                    if img_tag:
                        img_url = img_tag.get("data-src") or img_tag.get("src", "")
                        if img_url and "data:image" not in img_url:
                            img_url = clean_fandom_image_url(img_url)
                            img_urls = [img_url]

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
                    status=status,
                    toy_brand="Pop Race"
                )
        return None

    def _parse_diecastsociety_search(self, html: str, meta: Dict) -> List[Dict]:
        soup = BeautifulSoup(html, "lxml")
        new_tasks = []
        found_articles = False
        for article in soup.find_all("article"):
            title_node = article.find("h2")
            if title_node:
                found_articles = True
                link_node = title_node.find("a", href=True)
                if link_node:
                    url = link_node["href"]
                    title_text = link_node.get_text(strip=True)
                    if "pop race" in title_text.lower() or "pop-race" in title_text.lower():
                        new_tasks.append({
                            "source": "diecastsociety_post",
                            "url": url,
                            "meta": {"title": title_text}
                        })
        if found_articles:
            next_page = meta.get("page", 1) + 1
            new_tasks.append({
                "source": "diecastsociety_search", 
                "url": f"https://diecastsociety.com/page/{next_page}/?s=Pop+Race", 
                "meta": {"page": next_page}
            })
        return new_tasks

    def _parse_diecastsociety_post(self, html: str, post_url: str) -> None:
        soup = BeautifulSoup(html, "lxml")
        entry_content = soup.find(class_=re.compile(r"(post-content|entry-content|post-holder)", re.I))
        if not entry_content:
            entry_content = soup
            
        full_text = entry_content.get_text("\n")
        
        all_imgs = entry_content.find_all("img")
        img_dict = {}
        for img in all_imgs:
            src = img.get("src") or img.get("data-src", "")
            if src and "data:image" not in src:
                src_cleaned = clean_diecastsociety_image_url(src)
                filename = os.path.basename(src_cleaned).lower().split(".")[0]
                filename = re.sub(r"-\d+x\d+$", "", filename)
                img_dict[filename] = src_cleaned

        code_pattern = re.compile(r"\b(PR64\d{3,4}|PRDC\d{2,3}|PR64-[A-Z0-9-]+)\b", re.I)
        
        lines = [line.strip() for line in full_text.split("\n") if line.strip()]
        for line in lines:
            matches = list(code_pattern.finditer(line))
            if not matches:
                continue
                
            for i, m in enumerate(matches):
                code = m.group(1).upper()
                start_idx = m.end()
                end_idx = matches[i+1].start() if i+1 < len(matches) else len(line)
                name_candidate = line[start_idx:end_idx].strip()
                
                name_candidate = re.sub(r"^[\s\-–—:#+•/]+", "", name_candidate).strip()
                if not name_candidate or len(name_candidate) < 3:
                    continue
                
                matched_img = None
                code_lower = code.lower()
                for fn, src in img_dict.items():
                    if code_lower in fn:
                        matched_img = src
                        break
                        
                img_urls = [matched_img] if matched_img else []
                
                release_year = None
                release_year_confidence = None
                title_text = post_url
                title_node = soup.find("h1")
                if title_node:
                    title_text = title_node.get_text(strip=True)
                ym = re.search(r"\b(20\d{2})\b", title_text)
                if ym:
                    release_year = int(ym.group(1))
                    release_year_confidence = "inferred"
                
                self.crawler._save_or_merge_product(
                    item_number=code,
                    product_name=name_candidate,
                    brand="Pop Race",
                    scale="1:64",
                    series="Pre-Order",
                    img_urls=img_urls,
                    source="diecastsociety",
                    release_year=release_year,
                    release_year_confidence=release_year_confidence,
                    status="Pre-Order",
                    toy_brand="Pop Race"
                )


class TarmacWorksBrandHandler:
    """Crawls Tarmac Works models from the official Shopify API."""
    def __init__(self, crawler: "MINI_GTCrawler"):
        self.crawler = crawler

    def discover_sources(self) -> List[Dict]:
        pending = []
        # Tarmac Works uses Shopify, which exposes products.json
        # We start at page 1 and dynamically enqueue subsequent pages.
        api_url = "https://www.tarmacworks.com/products.json?limit=250&page=1"
        pending.append({
            "source": "shopify_json",
            "url": api_url,
            "meta": {"page": 1}
        })
        
        return pending

    def parse_task(self, html_or_json: str, task: Dict) -> Optional[List[Dict]]:
        url = task["url"]
        try:
            data = json.loads(html_or_json)
        except Exception as e:
            logger.error(f"Tarmac Works JSON parse error for {url}: {e}")
            return None

        products = data.get("products", [])
        if not products:
            logger.info(f"Tarmac Works reached empty page at {url}")
            return None

        new_tasks = []
        if len(products) == 250:
            next_page = task.get("meta", {}).get("page", 1) + 1
            new_tasks.append({
                "source": "shopify_json",
                "url": f"https://www.tarmacworks.com/products.json?limit=250&page={next_page}",
                "meta": {"page": next_page}
            })

        for p in products:
            title = p.get("title", "")
            tags = p.get("tags", [])
            product_type = p.get("product_type", "")
            vendor = p.get("vendor", "Tarmac Works")
            
            # Skip non-diecast items based on type or tags if obvious
            if "apparel" in product_type.lower() or "accessories" in product_type.lower():
                continue

            # Extract scale from tags
            scale = "1:64"
            if "1/43" in tags or "1:43" in tags:
                scale = "1:43"
            elif "1/18" in tags or "1:18" in tags:
                scale = "1:18"
            
            # Try to figure out series
            series = "Regular"
            tag_upper = [t.upper() for t in tags]
            if "HOBBY64" in tag_upper: series = "HOBBY64"
            elif "GLOBAL64" in tag_upper: series = "GLOBAL64"
            elif "ROAD64" in tag_upper: series = "ROAD64"
            elif "COLLAB64" in tag_upper: series = "COLLAB64"
            elif "PIT GARAGE DIORAMA" in tag_upper: series = "Diorama"
            elif "TRUCK64" in tag_upper: series = "TRUCK64"

            # Get image URLs
            img_urls = []
            for img in p.get("images", []):
                src = img.get("src")
                if src:
                    # Strip shopify query params to get original image
                    clean_src = src.split("?")[0]
                    img_urls.append(clean_src)

            # Get SKU
            variants = p.get("variants", [])
            item_number = None
            for v in variants:
                sku = v.get("sku")
                if sku:
                    item_number = sku
                    break
            
            if not item_number:
                # If no SKU is set (often for bundles), use the handle as a fallback unique ID
                item_number = p.get("handle")

            if not item_number:
                continue

            # Check year from created_at or title
            created_at = p.get("created_at", "")
            year = None
            if created_at:
                year_str = created_at[:4]
                if year_str.isdigit():
                    year = int(year_str)

            attributes = {
                "tags": tags,
                "product_type": product_type
            }

            self.crawler._save_or_merge_product(
                item_number=item_number,
                product_name=title,
                brand=vendor,
                scale=scale,
                series=series,
                img_urls=img_urls,
                source="shopify",
                release_year=year,
                release_year_confidence="inferred",
                status="Released",
                toy_brand="Tarmac Works",
                attributes=attributes
            )
        
        return new_tasks if new_tasks else None


class Inno64BrandHandler:
    """Crawls INNO64 models from the official WooCommerce site inno-models.com."""
    def __init__(self, crawler: "MINI_GTCrawler"):
        self.crawler = crawler

    def discover_sources(self) -> List[Dict]:
        pending = []
        # Fetch the WooCommerce sitemap index
        index_url = "https://www.inno-models.com/sitemap_index.xml"
        index_xml = self.crawler.fetch_url(index_url, use_cache=True)
        sitemaps = []
        if index_xml:
            try:
                soup = BeautifulSoup(index_xml, "lxml-xml")
                for loc in soup.find_all("loc"):
                    smap_url = loc.get_text(strip=True)
                    if "product-sitemap" in smap_url:
                        sitemaps.append(smap_url)
            except Exception as e:
                logger.error(f"Failed to parse INNO64 sitemap index: {e}")
        
        # Fallback if index fails
        if not sitemaps:
            sitemaps = ["https://www.inno-models.com/product-sitemap.xml"]

        for sitemap_url in sitemaps:
            sitemap_xml = self.crawler.fetch_url(sitemap_url, use_cache=True)
            if sitemap_xml:
                try:
                    soup = BeautifulSoup(sitemap_xml, "lxml-xml")
                    for loc in soup.find_all("loc"):
                        url = loc.get_text(strip=True)
                        # Only product pages (not images)
                        if "/product/" in url and not url.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                            pending.append({
                                "source": "inno64_product",
                                "url": url,
                                "meta": {}
                            })
                except Exception as e:
                    logger.error(f"Failed to parse INNO64 sitemap {sitemap_url}: {e}")
        logger.info(f"INNO64 discovery: found {len(pending)} product pages.")
        return pending

    def parse_task(self, html_or_json: str, task: Dict) -> Optional[List[Dict]]:
        url = task["url"]
        try:
            soup = BeautifulSoup(html_or_json, "lxml")
        except Exception as e:
            logger.error(f"INNO64 parse error for {url}: {e}")
            return None

        # Extract title
        title_node = soup.find("h1", class_="product_title")
        if not title_node:
            title_node = soup.find("h1")
        product_name = title_node.get_text(strip=True) if title_node else ""
        if not product_name:
            return None

        # Extract dynamic listing fields (Jet Engine widgets)
        fields = {}
        for widget in soup.find_all(class_=lambda x: x and "elementor-widget-jet-listing-dynamic-" in x):
            content_node = widget.find(class_="jet-listing-dynamic-field__content")
            terms_node = widget.find(class_="jet-listing-dynamic-terms__link")
            text = ""
            if content_node:
                text = content_node.get_text(strip=True)
            elif terms_node:
                text = terms_node.get_text(strip=True)
            else:
                text = widget.get_text(strip=True)
            
            text_lower = text.lower()
            if text_lower.startswith("scale"):
                fields["scale"] = text[5:].strip()
            elif text_lower.startswith("brand"):
                fields["brand"] = text[5:].strip()
            elif text_lower.startswith("status"):
                fields["status"] = text[6:].strip()
            elif text_lower.startswith("sku"):
                fields["sku"] = text[3:].strip()
            elif text_lower.startswith("type"):
                fields["type"] = text[4:].strip()
            elif text_lower.startswith("description"):
                fields["description"] = text[11:].strip()

        sku = fields.get("sku", "").strip()
        if not sku:
            # Try to extract from URL slug
            slug = url.rstrip("/").split("/")[-1]
            sku = slug.upper().replace("-", " ")
        if not sku:
            return None

        brand = fields.get("brand", "INNO64").strip() or "INNO64"
        scale = fields.get("scale", "1/64").strip()
        # Normalize scale format
        scale = scale.replace("/", ":")
        if not scale.startswith("1:"):
            scale = f"1:{scale}" if scale.isdigit() else scale

        status = fields.get("status", "Released").strip()
        model_type = fields.get("type", "Diecast").strip()

        # Fix comma-delimited series: e.g. "Diecast,Limited Editions" -> series="Diecast", sub_series="Limited Editions"
        series = "Diecast"
        sub_series = "Regular"
        if model_type:
            parts = [p.strip() for p in model_type.split(",") if p.strip()]
            primary_parts = [p for p in parts if p.lower() not in ("limited editions", "limited edition")]
            qualifier_parts = [p for p in parts if p.lower() in ("limited editions", "limited edition")]
            if primary_parts:
                series = primary_parts[0]
            if qualifier_parts:
                sub_series = qualifier_parts[0]

        # Image: get the main product image
        img_urls = []
        # Look for the primary product image in the gallery
        for img in soup.find_all("img"):
            src = img.get("src") or ""
            if (
                src
                and "wp-content/uploads" in src
                and "logo" not in src.lower()
                and "data:image" not in src
                and not src.endswith(".svg")
                and "-600x" not in src  # skip thumbnails
                and "-300x" not in src
                and "-150x" not in src
            ):
                if src not in img_urls:
                    img_urls.append(src)
                    break  # Just need one primary image

        # Fallback: find any product image (even thumbnail, then strip size suffix)
        if not img_urls:
            for img in soup.find_all("img"):
                src = img.get("src") or ""
                if src and "wp-content/uploads" in src and "logo" not in src.lower() and "data:image" not in src:
                    # Strip WordPress thumbnail suffix
                    cleaned = re.sub(r"-\d+x\d+(\.[a-zA-Z0-9]+)$", r"\1", src)
                    if cleaned not in img_urls:
                        img_urls.append(cleaned)
                        break

        attributes = {
            "vehicle_type": model_type,
            "description": fields.get("description", "").strip()
        }

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
        return None


class TrendsHobbyBrandHandler:
    """Crawls Trends Hobby (Tiny HK) models from tiny.com.hk."""
    def __init__(self, crawler: "MINI_GTCrawler"):
        self.crawler = crawler

    def discover_sources(self) -> List[Dict]:
        pending = []
        import subprocess
        try:
            api_url = "https://diecastsilkroad.com/collections/trends-hobby/products.json?limit=250"
            result = subprocess.run(
                ["curl.exe", "-s", api_url],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore"
            )
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                products = data.get("products", [])
                logger.info(f"Trends Hobby (diecastsilkroad) discovery: found {len(products)} products.")
                pending.append({
                    "source": "silkroad_json",
                    "url": api_url,
                    "meta": {"products": products}
                })
        except Exception as e:
            logger.error(f"Failed to fetch Trends Hobby from diecastsilkroad: {e}")
        return pending

    def parse_task(self, html_or_json: str, task: Dict) -> Optional[List[Dict]]:
        meta = task.get("meta", {})
        products = meta.get("products", [])
        if not products:
            return None

        for p in products:
            title = p.get("title", "")
            if not title:
                continue

            # Clean title
            product_name = title.replace("Trends Hobby - ", "").replace("[Free Shipping] ", "").strip()

            # SKU/Item number
            item_number = None
            for v in p.get("variants", []):
                sku = v.get("sku")
                if sku:
                    item_number = sku
                    break
            if not item_number:
                item_number = p.get("handle")
            if not item_number:
                continue

            # Brand (parse from title)
            brand = "Trends Hobby"
            title_lower = title.lower()
            if "porsche" in title_lower:
                brand = "Porsche"
            elif "lamborghini" in title_lower:
                brand = "Lamborghini"
            elif "toyota" in title_lower:
                brand = "Toyota"
            elif "mclaren" in title_lower:
                brand = "McLaren"

            # Images
            img_urls = []
            for img in p.get("images", []):
                src = img.get("src")
                if src:
                    img_urls.append(src.split("?")[0])

            # Release Year
            year = None
            ym = re.search(r"\b(20\d{2})\b", title)
            if not ym:
                ym = re.search(r"\b(19\d{2})\b", title)
            if ym:
                year = int(ym.group(1))

            # Status
            status = "Released"
            if "pre-order" in title_lower or "pre order" in title_lower:
                status = "Pre-Order"

            # Series
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
                status=status,
                toy_brand="Trends Hobby",
                sub_series="Regular",
                attributes=attributes
            )
        return None

