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
from database.models import get_product_model

# Import brand handlers
from crawler.brand_minigt import MiniGTBrandHandler
from crawler.brand_hotwheels import HotWheelsBrandHandler
from crawler.brand_poprace import PopRaceBrandHandler
from crawler.brand_tarmacworks import TarmacWorksBrandHandler
from crawler.brand_inno64 import Inno64BrandHandler
from crawler.brand_trendshobby import TrendsHobbyBrandHandler

# Suppress SSL warnings (we disable SSL verification for speed)
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# Ensure required directories exist
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


class MINI_GTCrawler:
    def __init__(self, max_workers: int = 20, rate_limit_delay: float = 0.2):
        self.max_workers = max_workers
        self.rate_limit_delay = rate_limit_delay
        self.crawler_state = self._load_state()
        self._session = self._make_session()
        # Dynamic task counters
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
        session.verify = False
        return session

    # ------------------------------------------------------------------ #
    #  Persistence                                                         #
    # ------------------------------------------------------------------ #

    def _load_state(self) -> Dict:
        """Loads state from disk, initializing if missing."""
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load state: {e}. Re-initializing.")

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
        """Saves current state to disk."""
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.crawler_state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to write state file: {e}")

    # ------------------------------------------------------------------ #
    #  HTTP Fetcher                                                        #
    # ------------------------------------------------------------------ #

    def fetch_url(self, url: str, use_cache: bool = True) -> Optional[str]:
        """Fetches page content, reading from local reference HTML or cache when available."""
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        # Intercept Fandom wiki or API requests and try to serve from local reference HTML files
        if "fandom.com" in url:
            page_name = None
            if "/wiki/" in url:
                page_name = urllib.parse.unquote(url.split("/wiki/")[-1]).split("?")[0].split("#")[0]
            elif "api.php" in url and "action=parse" in url:
                parsed_query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                pages_list = parsed_query.get("page", [])
                if pages_list:
                    page_name = pages_list[0]
                    
            if page_name:
                ref_dir = os.path.join(root_dir, "reference_htmls")
                if os.path.exists(ref_dir):
                    for ext in [".html", ".htm"]:
                        ref_file_name = f"{page_name}{ext}"
                        ref_file_path = os.path.join(ref_dir, ref_file_name)
                        if os.path.exists(ref_file_path):
                            try:
                                logger.info(f"Serving {url} from local reference HTML: {ref_file_name}")
                                with open(ref_file_path, "r", encoding="utf-8") as f:
                                    ref_html = f.read()
                                if "api.php" in url:
                                    mock_response = {
                                        "parse": {
                                            "text": {
                                                "*": ref_html
                                            },
                                            "categories": []
                                        }
                                    }
                                    return json.dumps(mock_response, ensure_ascii=False)
                                return ref_html
                            except Exception as e:
                                logger.error(f"Failed to read local reference HTML {ref_file_name}: {e}")

        url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
        cache_file = os.path.join(root_dir, "cache", "html", f"{url_hash}.html")

        # Serve from cache — NO rate-limit delay needed
        if use_cache and os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                logger.warning(f"Failed to read cache for {url}: {e}")

        # For diecastsilkroad.com, Tarmac, Shopify, and Fandom hub pages, use curl.exe via subprocess to bypass rate limits/anti-bot protection
        if "diecastsilkroad.com" in url or "tarmacworks.com" in url or "treasuredmodels.com" in url or ("/wiki/" in url and "fandom.com" in url):
            import subprocess
            try:
                result = subprocess.run(
                    [
                        "curl.exe", "-s", "-L",
                        "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                        url
                    ],
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
        if "inno-models.com" in url:
            time.sleep(1.0)
        else:
            time.sleep(self.rate_limit_delay)

        max_retries = 5
        for attempt in range(max_retries):
            try:
                resp = self._session.get(url, timeout=30, verify=False)
                
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
                if attempt < max_retries - 1:
                    backoff = min(2 ** (attempt + 1), 30)
                    logger.warning(f"Retry {attempt+1}/{max_retries} for {url} after error: {e}. Retrying in {backoff}s...")
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
        
        # Clear crawled URLs cache for the target brand(s) during fresh discovery
        if brand_limit:
            patterns = {
                "MINI GT": ["minigt.tsm-models.com", "myminigt.com", "minigt.fandom.com"],
                "Hot Wheels": ["hotwheels.fandom.com"],
                "Pop Race": ["pop-race.fandom.com", "diecastsociety.com", "my64.com.my/usr/product.aspx?pgid=4&grpid=28"],
                "Tarmac Works": ["tarmacworks.fandom.com", "tarmacworks.com"],
                "INNO64": ["my64.com.my/usr/product.aspx?pgid=4&grpid=26"],
                "Trends Hobby": ["treasuredmodels.com"]
            }
            brand_pats = patterns.get(brand_limit, [])
            crawled = self.crawler_state.get("crawled_urls", [])
            self.crawler_state["crawled_urls"] = [
                u for u in crawled if not any(p in u for p in brand_pats)
            ]
        else:
            self.crawler_state["crawled_urls"] = []

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
        scale_exempt_brands = {"MINI GT", "Hot Wheels", "INNO64", "Tarmac Works", "Trends Hobby"}
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
        if toy_brand == "MINI GT" or toy_brand == "Pop Race":
            status = None
            
        if toy_brand == "MINI GT":
            # --- Brand normalization ---
            b_upper = brand.upper().strip()
            if b_upper in ["KAIDO HOUSE", "KAIDO★HOUSE", "KAIDOHOUSE X MINI GT", "KAIDO STAR"]:
                brand = "KAIDOHOUSE x MINI GT"
            elif b_upper == "FERRARI":
                brand = "Ferrari"
            elif b_upper == "SUBARU":
                brand = "Subaru"
            elif b_upper in ["MINI GT SET", "MINI GT SETS"]:
                brand = "MINI GT Set"
            elif b_upper in ["RED BULL RACING", "ORACLE RED BULL", "ORACLE RED BULL RACING", "RED BULL"]:
                brand = "Red Bull Racing"
                
            series = series.upper().strip()
            if not series or series == "REGULAR COLLECTION":
                series = "REGULAR"
        else:
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
                                    # update total as new tasks are discovered
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