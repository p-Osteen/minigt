import os
import re
import json
import logging
import urllib.parse
from typing import List, Dict, Set, Optional
from bs4 import BeautifulSoup

from crawler.utils import clean_fandom_image_url, get_row_product_images

logger = logging.getLogger("crawler")

class MiniGTBrandHandler:
    def __init__(self, crawler):
        self.crawler = crawler

    def discover_sources(self) -> List[Dict]:
        pending = []
        # 1. Discover Official site brands
        official_brands = []
        logger.info("Discovering Official site Brands...")
        
        brands_dict = {}
        
        def parse_brands_from_html(html_content):
            if not html_content:
                return
            try:
                soup = BeautifulSoup(html_content, "lxml")
                for link in soup.find_all("a", href=True):
                    href = link["href"]
                    text = link.get_text(strip=True)
                    if "action=product-list" in href:
                        b_id_match = re.search(r"b_id=(\d+)", href)
                        if b_id_match:
                            b_id = b_id_match.group(1)
                            if text:
                                if text == "QubeCarz":
                                    text = "Qube Cars"
                                if b_id not in brands_dict:
                                    brands_dict[b_id] = text
            except Exception as e:
                logger.error(f"Error parsing brands HTML: {e}")

        # Live fetch
        off_html = self.crawler.fetch_url(
            "https://minigt.tsm-models.com/index.php?action=product", use_cache=False
        )
        if off_html:
            parse_brands_from_html(off_html)

        # Merge with local reference HTML to guarantee completeness
        ref_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "reference_htmls",
            "MINIGT.com – Welcome to the World of 1_64!.html"
        )
        if os.path.exists(ref_path):
            logger.info("Merging brands from local reference HTML...")
            try:
                with open(ref_path, "r", encoding="utf-8") as f:
                    ref_html = f.read()
                parse_brands_from_html(ref_html)
            except Exception as e:
                logger.error(f"Failed to read local reference HTML: {e}")

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
            return self._parse_official_list(html_or_json, meta["brand_name"], meta["b_id"], meta["page"])
        elif source == "official_detail":
            self._parse_official_detail(html_or_json, meta["brand_page_name"])
        elif source == "fandom":
            self._parse_fandom_page(html_or_json, meta["page_name"])
        elif source == "myminigt":
            self._parse_myminigt_detail(html_or_json, url)
        return None

    def _parse_official_detail(self, html: str, brand_page_name: str) -> None:
        soup = BeautifulSoup(html, "lxml")

        name_node = soup.find(class_="pro-name")
        product_name = name_node.get_text(strip=True) if name_node else ""
        if not product_name:
            return
        if brand_page_name == "QubeCarz":
            brand_page_name = "Qube Cars"

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
                    if marque == "QubeCarz":
                        marque = "Qube Cars"
                elif "status" in tl:
                    status = txt.replace("Status", "").replace("status", "").strip()

        if not item_number:
            return

        # Series mapping
        series = "Regular"
        special_brands = {
            "007 Movie Car", "QubeCarz", "Qube Cars", "IMSA",
            "KAIDOHOUSE x MINI GT", "SUPER GT SERIES",
        }
        if brand_page_name in special_brands and brand_page_name != marque:
            series = brand_page_name

        # Release year should be based on release date / Fandom models list, not the name
        release_year = None
        release_year_confidence = None

        img_urls: List[str] = []
        seen_img: Set[str] = set()

        def _is_product_img(src: str) -> bool:
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

        self.crawler._save_or_merge_product(
            item_number, product_name, marque, scale, series, img_urls,
            source="official", release_year=release_year,
            release_year_confidence=release_year_confidence, status=status
        )

    def _parse_official_list(
        self, html: str, brand_name: str, b_id: str, page: int
    ) -> List[Dict]:
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

                self.crawler._save_or_merge_product(
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

                        self.crawler._save_or_merge_product(
                            code_candidate, name_candidate, brand, "1:64", "Regular", img_urls,
                            source="fandom", release_year=release_year,
                            release_year_confidence=release_year_confidence, status=status
                        )

    def _parse_myminigt_detail(self, html: str, detail_url: str) -> None:
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

            code_match = re.match(r"^([A-Z0-9_-]+)\b", product_name, re.IGNORECASE)
            item_number = ""
            if code_match:
                matched_code = code_match.group(1).upper()
                if any(matched_code.startswith(pfx) for pfx in ["KHMG", "KH", "MGT", "DM", "DBW", "BL", "S", "XX"]):
                    item_number = matched_code
                    product_name = product_name[code_match.end():].strip().lstrip("-").strip()

            if not item_number:
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

            description = product_node.get("description", "").lower()
            if "pre order" in description or "pre-order" in description:
                status = "Pre-Order"
            elif "cancelled" in description or "discontinued" in description:
                status = "Cancelled"
            else:
                status = "Released"

            self.crawler._save_or_merge_product(
                item_number, product_name, brand, "1:64", series, img_urls,
                source="myminigt", release_year=release_year,
                release_year_confidence=release_year_confidence, status=status
            )
        except Exception as e:
            logger.error(f"MyMiniGT JSON-LD parsing failed for {detail_url}: {e}")
