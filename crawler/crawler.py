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
from database.models import Product

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


class MINI_GTCrawler:
    def __init__(self, max_workers: int = 20, rate_limit_delay: float = 0.2):
        self.max_workers = max_workers
        self.rate_limit_delay = rate_limit_delay
        self.crawler_state = self._load_state()
        self._session = self._make_session()
        # Dynamic task counters (fixed progress bug)
        self._total_tasks = 0
        self._completed_tasks = 0

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
        cache_file = os.path.join("cache", "html", f"{url_hash}.html")

        # Serve from cache — NO rate-limit delay needed
        if use_cache and os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                logger.warning(f"Failed to read cache for {url}: {e}")

        # Live fetch — apply rate-limit delay
        time.sleep(self.rate_limit_delay)

        try:
            resp = self._session.get(url, timeout=15, verify=False)
            resp.raise_for_status()
            content = resp.text

            # Write to cache
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as e:
                logger.warning(f"Failed to cache {url}: {e}")

            return content

        except Exception as e:
            logger.error(f"Failed to fetch {url}: {e}")
            return None

    # ------------------------------------------------------------------ #
    #  Discovery Phase                                                     #
    # ------------------------------------------------------------------ #

    def run_discovery(self) -> None:
        """Discovers product listing URLs from all three sources."""
        logger.info("Starting Crawl Discovery Phase...")

        # 1. Official site brands
        official_brands = []
        logger.info("Discovering Official site Brands...")
        off_html = self.fetch_url(
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

        self.crawler_state["discovered_sources"]["official_brands"] = official_brands
        logger.info(f"Discovered {len(official_brands)} Official Brands.")

        # 2. Fandom Wiki
        fandom_pages = []
        logger.info("Discovering Fandom Wiki category pages...")
        fandom_api = (
            "https://minigt.fandom.com/api.php"
            "?action=parse&page=MINI_GT&format=json&prop=text"
        )
        api_res = self.fetch_url(fandom_api, use_cache=False)
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

        self.crawler_state["discovered_sources"]["fandom_pages"] = fandom_pages
        logger.info(f"Discovered {len(fandom_pages)} Fandom articles.")

        # 3. MyMiniGT sitemap
        myminigt_urls = []
        logger.info("Discovering MyMiniGT catalog items from sitemap...")
        sitemap_html = self.fetch_url("https://myminigt.com/sitemap.xml", use_cache=True)
        if sitemap_html:
            soup = BeautifulSoup(sitemap_html, "lxml-xml")
            for loc in soup.find_all("loc"):
                loc_url = loc.get_text(strip=True)
                if "modelId=" in loc_url:
                    myminigt_urls.append(loc_url)

        self.crawler_state["discovered_sources"]["myminigt_urls"] = myminigt_urls
        logger.info(f"Discovered {len(myminigt_urls)} MyMiniGT items.")

        # 4. Build task queue
        pending: List[Dict] = []
        crawled = set(self.crawler_state.get("crawled_urls", []))

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

        for page in fandom_pages:
            api_url = (
                f"https://minigt.fandom.com/api.php"
                f"?action=parse&page={urllib.parse.quote(page)}&format=json&prop=text"
            )
            pending.append({"source": "fandom", "url": api_url, "meta": {"page_name": page}})

        for url in myminigt_urls:
            if url not in crawled:
                pending.append({"source": "myminigt", "url": url, "meta": {}})

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
    ) -> None:
        """
        Saves product to DB, merging if already exists.
        - Skips D-prefix item numbers entirely.
        - Stores image URLs directly (no downloading).
        """
        # --- Scale filter ---
        # Bypassed for Kaido House items (always 1:64)
        is_kaido = "kaido" in series.lower() or "kaido" in brand.lower() or "kaido" in product_name.lower()
        if not is_kaido:
            if not scale or "1:64" not in scale:
                return

        # --- Normalise item number ---
        clean_num = re.sub(r"[^a-zA-Z0-9]", "", item_number).upper()

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

        # --- Deduplicate URLs ---
        seen_urls: Set[str] = set()
        clean_img_urls: List[str] = []
        for u in img_urls:
            if u and "data:image" not in u and "favicon" not in u and u not in seen_urls:
                clean_img_urls.append(u)
                seen_urls.add(u)

        with db_lock:
            with get_db_session() as session:
                existing = (
                    session.query(Product)
                    .filter(Product.item_number == clean_num)
                    .first()
                )

                if existing:
                    # Determine source priorities (lower is higher priority)
                    prio_map = {"official": 1, "myminigt": 2, "fandom": 3}
                    incoming_prio = prio_map.get(source, 9)
                    existing_prio = prio_map.get(existing.source, 9)

                    # Overwrite metadata and images if incoming has higher priority
                    if incoming_prio < existing_prio:
                        existing.product_name = product_name
                        existing.brand = brand
                        existing.series = series
                        existing.scale = scale
                        existing.source = source
                        existing.set_images(clean_img_urls)
                        logger.debug(f"Overwrote product {clean_num} with higher-priority source data")
                    elif incoming_prio == existing_prio:
                        # Tie-breaker: keep the longer product name, do not merge images
                        if len(product_name) > len(existing.product_name):
                            existing.product_name = product_name
                    else:
                        logger.debug(f"Ignored lower-priority source data for product {clean_num}")
                else:
                    new_prod = Product(
                        item_number=clean_num,
                        product_name=product_name,
                        brand=brand,
                        scale=scale,
                        series=series,
                        source=source,
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
                if any(c in cls for c in ["related_pro", "product_o", "products_list"]):
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

        self._save_or_merge_product(item_number, product_name, marque, scale, series, img_urls, source="official")

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
        """Parses product tables from a Fandom Wiki article."""
        try:
            res_data = json.loads(json_str)
            if "parse" not in res_data or "text" not in res_data["parse"]:
                return
            html_content = res_data["parse"]["text"]["*"]
            soup = BeautifulSoup(html_content, "lxml")
        except Exception as e:
            logger.error(f"Fandom JSON parse error for {page_name}: {e}")
            return

        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            code_idx = name_idx = brand_idx = photo_idx = -1

            for idx, h in enumerate(headers):
                if any(k in h for k in ("code", "item", "number")):
                    code_idx = idx
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

                img_urls: List[str] = []
                if photo_idx != -1 and photo_idx < len(cells):
                    img_tag = cells[photo_idx].find("img")
                    if img_tag:
                        img_url = img_tag.get("data-src") or img_tag.get("src", "")
                        if img_url and "data:image" not in img_url:
                            if "/revision/latest" in img_url:
                                img_url = img_url.split("/revision/latest")[0]
                            img_urls.append(img_url)

                series = "Regular"
                if "model" in page_name.lower():
                    series = page_name.replace("_", " ")
                elif "house" in page_name.lower():
                    series = "Kaido House"
                elif page_name in {"Bentley_Shop_Exclusives", "Cancelled_Models", "Accessories"}:
                    series = page_name.replace("_", " ")

                self._save_or_merge_product(item_number, product_name, brand, "1:64", series, img_urls, source="fandom")

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

            self._save_or_merge_product(item_number, product_name, brand, "1:64", series, img_urls, source="myminigt")
        except Exception as e:
            logger.error(f"MyMiniGT JSON-LD parsing failed for {detail_url}: {e}")

    # ------------------------------------------------------------------ #
    #  Core Crawler Loop                                                   #
    # ------------------------------------------------------------------ #

    def run_crawler(self) -> None:
        """
        Runs the concurrent task queue loop with correct progress tracking.
        Supports pause/resume via saved state.
        """
        logger.info("Starting Crawl Execution Phase...")

        if not self.crawler_state.get("pending_urls"):
            self.run_discovery()

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

                    try:
                        html = future.result()
                        if html:
                            if source == "official_detail":
                                self._parse_official_detail(html, meta["brand_page_name"])
                            elif source == "official_list":
                                new_tasks = self._parse_official_list(
                                    html, meta["brand_name"], meta["b_id"], meta["page"]
                                )
                                with state_lock:
                                    pending_queue.extend(new_tasks)
                                # FIX: update total as new tasks are discovered
                                with counter_lock:
                                    self._total_tasks += len(new_tasks)
                            elif source == "fandom":
                                self._parse_fandom_page(html, meta["page_name"])
                            elif source == "myminigt":
                                self._parse_myminigt_detail(html, url)

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
