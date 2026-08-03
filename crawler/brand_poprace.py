import os
import re
import json
import logging
import urllib.parse
from typing import List, Dict, Set, Optional
from bs4 import BeautifulSoup

from crawler.utils import (
    clean_fandom_image_url,
    clean_diecastsociety_image_url,
    get_row_product_images,
    parse_my64_list,
    parse_my64_detail
)

logger = logging.getLogger("crawler")

class PopRaceBrandHandler:
    """
    Crawls Pop Race from pop-race.fandom.com.

    3-level classification for Pop Race products:
      Level 1 (series_group) : "Collection" | "By Make" | "By Year"
      Level 2 (series)       : collection name e.g. "Regular Collection"
                               make group e.g. "Japanese" / "European"
                               year string e.g. "2022"
      Level 3 (sub_series)   : make name e.g. "Honda" (only for "By Make")

    Navigation (series_group/series/sub_series per page) is parsed once from
    the POP_RACE_Wiki.html nav dropdowns and cached in _page_meta_map, then
    looked up for every discovered page. This is the single source of truth
    for classification — discovery from allpages/DiecastSociety/my64 never
    overrides it, only fills in pages the nav doesn't mention.
    """

    SKIP_NS = {"File:", "Special:", "Template:", "Help:", "Talk:", "User:",
               "User_talk:", "Forum:", "Board:", "Thread:", "Category:"}

    def __init__(self, crawler):
        self.crawler = crawler
        self._page_meta_map: Dict[str, Dict] = {}

    # ------------------------------------------------------------------
    # Navigation parsing helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_page(href: str) -> Optional[str]:
        if "/wiki/" not in href:
            return None
        page = urllib.parse.unquote(href.split("/wiki/")[-1]).split("#")[0]
        if not page or page == "Main_Page":
            return None
        ns = page.split(":", 1)[0] + ":" if ":" in page else ""
        if ns in PopRaceBrandHandler.SKIP_NS:
            return None
        return page

    def _register(self, page: str, series_group: str, series: str, sub_series: Optional[str] = None):
        key = page.replace(" ", "_")
        if key not in self._page_meta_map:
            self._page_meta_map[key] = {
                "series_group": series_group,
                "series": series,
                "sub_series": sub_series,
            }

    def _load_nav_from_html(self, html: str) -> bool:
        """Parse navigation from POP_RACE_Wiki.html and populate _page_meta_map."""
        try:
            soup = BeautifulSoup(html, "lxml")

            all_dds = soup.find_all(class_="wds-dropdown")
            top_dds = [d for d in all_dds if not d.find_parent(class_="wds-dropdown__content")]

            for dd in top_dds:
                ph = dd.find(class_="wds-dropdown__placeholder")
                if not ph:
                    continue
                top_label = ph.get_text(strip=True)  # "Collection", "By Make", "By Year"
                content = dd.find(class_="wds-dropdown__content")
                if not content:
                    continue

                # ---- COLLECTION ----
                if top_label == "Collection":
                    for a in content.find_all("a", href=True):
                        page = self._clean_page(a["href"])
                        if page:
                            col_name = a.get_text(" ", strip=True)
                            self._register(page, "Collection", col_name)

                # ---- BY MAKE ----
                elif top_label == "By Make":
                    top_ul = content.find("ul")
                    l2_items = top_ul.find_all("li", class_="wds-dropdown-level-nested", recursive=False) if top_ul else []
                    for l2_li in l2_items:
                        l2_ph = l2_li.find(class_="wds-dropdown__placeholder")
                        if not l2_ph:
                            continue
                        make_group = l2_ph.get_text(strip=True)  # "Japanese", "European", ...

                        l2_content = l2_li.find(class_="wds-dropdown-level-nested__content")
                        if not l2_content:
                            continue

                        # L3: make-level items (e.g. Aston Martin, Honda)
                        l3_items = l2_content.find_all("li", class_="wds-dropdown-level-nested", recursive=False)
                        for l3_li in l3_items:
                            make_a = l3_li.find("a", class_="wds-dropdown-level-nested__toggle")
                            if make_a:
                                make_page = self._clean_page(make_a.get("href", ""))
                                make_name = make_a.get_text(" ", strip=True)
                                if make_page:
                                    self._register(make_page, "By Make", make_group, make_name)

                            # L4: model sub-pages nested under this make
                            l3_content = l3_li.find(class_="wds-dropdown-level-nested__content")
                            if l3_content:
                                for l4_a in l3_content.find_all("a", href=True):
                                    model_page = self._clean_page(l4_a["href"])
                                    if model_page:
                                        self._register(model_page, "By Make", make_group, make_name if make_a else None)

                        # Plain (non-nested) links directly under l2 content
                        for plain_a in l2_content.find_all("a", href=True):
                            if "wds-dropdown-level-nested__toggle" not in plain_a.get("class", []):
                                pg = self._clean_page(plain_a["href"])
                                if pg:
                                    self._register(pg, "By Make", make_group, None)

                # ---- BY YEAR ----
                elif top_label == "By Year":
                    for a in content.find_all("a", href=True):
                        page = self._clean_page(a["href"])
                        if page:
                            yr_label = a.get_text(" ", strip=True)
                            self._register(page, "By Year", yr_label)

            count = len(self._page_meta_map)
            logger.info(f"Pop Race: loaded {count} page->meta mappings from nav HTML.")
            return count > 0
        except Exception as e:
            logger.error(f"Pop Race nav parse error: {e}")
            return False

    def _load_series_map(self):
        self._page_meta_map = {}

        ref_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "reference_htmls", "POP_RACE_Wiki.html"
        )
        if os.path.exists(ref_path):
            logger.info("Loading Pop Race nav from local reference HTML (POP_RACE_Wiki.html)...")
            try:
                with open(ref_path, "r", encoding="utf-8") as f:
                    html = f.read()
                if self._load_nav_from_html(html):
                    return
            except Exception as e:
                logger.error(f"Failed to read local Pop Race nav HTML: {e}")

        logger.info("Fetching Pop Race nav from live Fandom wiki...")
        html = self.crawler.fetch_url("https://pop-race.fandom.com/wiki/POP_RACE_Wiki", use_cache=True)
        if html:
            self._load_nav_from_html(html)

    def _get_meta_for_page(self, page_name: str) -> Dict:
        key = page_name.replace(" ", "_")
        if key in self._page_meta_map:
            return self._page_meta_map[key]
        if page_name in self._page_meta_map:
            return self._page_meta_map[page_name]
        if page_name.isdigit():
            return {"series_group": "By Year", "series": page_name, "sub_series": None}
        return {"series_group": "Collection", "series": "Regular Collection", "sub_series": None}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def discover_sources(self) -> List[Dict]:
        pending: List[Dict] = []
        seen_urls: Set[str] = set()

        self._load_series_map()

        # 1. Enqueue all pages from nav reference HTML
        ref_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "reference_htmls", "POP_RACE_Wiki.html"
        )
        if os.path.exists(ref_path):
            logger.info("Discovering Pop Race links from local reference HTML (POP_RACE_Wiki.html)...")
            try:
                with open(ref_path, "r", encoding="utf-8") as f:
                    ref_html = f.read()
                soup = BeautifulSoup(ref_html, "lxml")
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if href.startswith("/wiki/"):
                        href = "https://pop-race.fandom.com" + href
                    elif href.startswith("//pop-race.fandom.com"):
                        href = "https:" + href

                    if "pop-race.fandom.com/wiki/" not in href:
                        continue

                    page_name = urllib.parse.unquote(href.split("/wiki/")[-1]).split("#")[0]
                    if not page_name:
                        continue
                    ns = page_name.split(":", 1)[0] + ":" if ":" in page_name else ""
                    if ns in self.SKIP_NS:
                        continue

                    page_api = (f"https://pop-race.fandom.com/api.php?action=parse"
                                f"&page={urllib.parse.quote(page_name)}&format=json&prop=text")
                    if page_api not in seen_urls:
                        seen_urls.add(page_api)
                        meta_info = self._get_meta_for_page(page_name)
                        pending.append({
                            "source": "fandom_list",
                            "url": page_api,
                            "meta": {
                                "page_name": page_name,
                                **meta_info,
                            }
                        })
            except Exception as e:
                logger.error(f"Failed to parse local Pop Race reference links: {e}")

        # 2. Live wiki exhaustive crawl
        apcontinue = ""
        while True:
            api_url = ("https://pop-race.fandom.com/api.php?action=query&list=allpages"
                       f"&apnamespace=0&aplimit=500&format=json")
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
                    if any(x in page_name for x in [":", "Main_Page"]):
                        continue
                    page_api = (f"https://pop-race.fandom.com/api.php?action=parse"
                                f"&page={urllib.parse.quote(page_name)}&format=json&prop=text")
                    if page_api not in seen_urls:
                        seen_urls.add(page_api)
                        meta_info = self._get_meta_for_page(page_name)
                        pending.append({
                            "source": "fandom_list",
                            "url": page_api,
                            "meta": {
                                "page_name": page_name,
                                **meta_info,
                            }
                        })
                apcontinue = data.get("continue", {}).get("apcontinue", "")
                if not apcontinue:
                    break
            except Exception as e:
                logger.error(f"Error fetching Pop Race allpages: {e}")
                break

        # 3. DiecastSociety Pop Race Search
        pending.append({
            "source": "diecastsociety_search",
            "url": "https://diecastsociety.com/page/1/?s=Pop+Race",
            "meta": {"page": 1}
        })

        # 4. Local my64 reference HTML
        local_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "reference_htmls", "4.Model Cars Online Malaysia __ POP RACE.html"
        )
        if os.path.exists(local_path):
            try:
                with open(local_path, "r", encoding="utf-8") as f:
                    local_html = f.read()
                pending.extend(parse_my64_list(self.crawler, local_html, "28", "Pop Race"))
            except Exception as e:
                logger.error(f"Failed to parse local Pop Race my64 HTML: {e}")

        # 5. Live my64 Pop Race list
        pending.append({
            "source": "my64_list",
            "url": "https://www.my64.com.my/usr/product.aspx?pgid=4&grpid=28&lang=en&pg=1",
            "meta": {"toy_brand": "Pop Race", "grp_id": "28", "page": 1}
        })

        return pending

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------
    def parse_task(self, html_or_json: str, task: Dict) -> Optional[List[Dict]]:
        source = task["source"]
        meta = task["meta"]
        if source == "fandom_list":
            return self._parse_fandom_list(html_or_json, meta)
        elif source == "diecastsociety_search":
            return self._parse_diecastsociety_search(html_or_json, meta)
        elif source == "diecastsociety_post":
            self._parse_diecastsociety_post(html_or_json, task["url"])
        elif source == "my64_list":
            return parse_my64_list(self.crawler, html_or_json, meta["grp_id"], meta["toy_brand"])
        elif source == "my64_detail":
            parse_my64_detail(self.crawler, html_or_json, task["url"], meta["toy_brand"], meta["grp_id"])
        return None

    def _parse_fandom_list(self, html_or_json: str, meta: Dict) -> Optional[List[Dict]]:
        try:
            res_data = json.loads(html_or_json)
            if "parse" not in res_data or "text" not in res_data["parse"]:
                return None
            html_content = res_data["parse"]["text"]["*"]
            soup = BeautifulSoup(html_content, "lxml")
        except Exception as e:
            logger.error(f"Pop Race JSON parse error for {meta.get('page_name','?')}: {e}")
            return None

        page_name = meta.get("page_name", "")

        if page_name.startswith("Category:"):
            new_tasks = []
            seen_links = set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                href_decoded = urllib.parse.unquote(href)
                if "/wiki/" not in href_decoded:
                    continue
                member_page = href_decoded.split("/wiki/")[-1]
                if any(x in member_page for x in [":", "Main_Page", "Special:", "File:", "Category:",
                                                    "Help:", "Template:"]):
                    continue
                skip_brands = ["bm creations", "inno64", "mini gt", "para64",
                               "tarmac works", "unique model"]
                if any(b in member_page.replace("_", " ").lower() for b in skip_brands):
                    continue
                if member_page not in seen_links:
                    seen_links.add(member_page)
                    api_url = (f"https://pop-race.fandom.com/api.php?action=parse"
                               f"&page={urllib.parse.quote(member_page)}&format=json&prop=text")
                    meta_info = self._get_meta_for_page(member_page)
                    new_tasks.append({
                        "source": "fandom_list",
                        "url": api_url,
                        "meta": {"page_name": member_page, **meta_info}
                    })
            return new_tasks

        # Nav map is the source of truth for classification; task meta (inherited
        # from a parent page during discovery) is only a fallback for pages the
        # nav parser never registered.
        page_meta = self._get_meta_for_page(page_name)
        series_group = page_meta.get("series_group") or meta.get("series_group")
        series       = page_meta.get("series")       or meta.get("series")
        sub_series   = page_meta.get("sub_series")   or meta.get("sub_series")

        for table in soup.find_all("table"):
            all_rows = table.find_all("tr")
            if not all_rows:
                continue
            header_row = all_rows[0]
            data_rows  = all_rows[1:]

            header_cells = header_row.find_all(["th", "td"], recursive=False)
            headers = [c.get_text(strip=True).lower() for c in header_cells]

            code_idx = name_idx = make_idx = release_idx = photo_idx = -1
            for idx, h in enumerate(headers):
                if "model #" in h or h in ("model#", "#"):
                    code_idx = idx
                elif any(k in h for k in ("code", "item", "number", "toy", "sku")) and code_idx == -1:
                    code_idx = idx
                elif h == "model":
                    name_idx = idx
                elif any(k in h for k in ("name", "model")) and name_idx == -1:
                    name_idx = idx
                elif h == "make":
                    make_idx = idx
                elif "release" in h:
                    release_idx = idx
                elif any(k in h for k in ("photo", "image", "pic")):
                    photo_idx = idx

            if code_idx == -1 or name_idx == -1:
                continue

            for row in data_rows:
                cells = row.find_all(["td", "th"])
                if len(cells) <= max(code_idx, name_idx):
                    continue

                item_number  = cells[code_idx].get_text(strip=True)
                product_name = cells[name_idx].get_text(strip=True)

                if not item_number or not product_name or item_number.strip() in ("-", ""):
                    continue

                row_make = None
                if make_idx != -1 and make_idx < len(cells):
                    row_make = cells[make_idx].get_text(strip=True) or None

                eff_sub_series = sub_series or row_make

                release_year = None
                release_year_confidence = None
                if release_idx != -1 and release_idx < len(cells):
                    release_val = cells[release_idx].get_text(strip=True)
                    ym = re.search(r"\b(20\d{2})\b", release_val)
                    if ym:
                        y = int(ym.group(1))
                        if 2019 <= y <= 2030:
                            release_year = y
                            release_year_confidence = "confirmed"

                if release_year is None and page_name.isdigit():
                    y = int(page_name)
                    if 2019 <= y <= 2030:
                        release_year = y
                        release_year_confidence = "confirmed"

                img_urls = get_row_product_images(row)
                if not img_urls and photo_idx != -1 and photo_idx < len(cells):
                    img_tag = cells[photo_idx].find("img")
                    if img_tag:
                        img_url = img_tag.get("data-src") or img_tag.get("src", "")
                        if img_url and "data:image" not in img_url:
                            img_urls = [clean_fandom_image_url(img_url)]

                attributes: Dict = {}
                if series_group:
                    attributes["series_group"] = series_group
                if row_make:
                    attributes["make"] = row_make

                self.crawler._save_or_merge_product(
                    item_number=item_number,
                    product_name=product_name,
                    brand="Pop Race",
                    scale="1:64",
                    series=series,
                    img_urls=img_urls,
                    source="fandom",
                    release_year=release_year,
                    release_year_confidence=release_year_confidence,
                    status=None,
                    toy_brand="Pop Race",
                    sub_series=eff_sub_series,
                    attributes=attributes,
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
                        new_tasks.append({"source": "diecastsociety_post",
                                          "url": url, "meta": {"title": title_text}})
        if found_articles:
            next_page = meta.get("page", 1) + 1
            new_tasks.append({"source": "diecastsociety_search",
                               "url": f"https://diecastsociety.com/page/{next_page}/?s=Pop+Race",
                               "meta": {"page": next_page}})
        return new_tasks

    def _parse_diecastsociety_post(self, html: str, post_url: str) -> None:
        soup = BeautifulSoup(html, "lxml")
        entry_content = soup.find(class_=re.compile(r"(post-content|entry-content|post-holder)", re.I))
        if not entry_content:
            entry_content = soup

        full_text = entry_content.get_text("\n")

        all_imgs = entry_content.find_all("img")
        img_dict: Dict = {}
        for img in all_imgs:
            src = img.get("src") or img.get("data-src", "")
            if src and "data:image" not in src:
                src_cleaned = clean_diecastsociety_image_url(src)
                filename = os.path.basename(src_cleaned).lower().split(".")[0]
                filename = re.sub(r"-\d+x\d+$", "", filename)
                img_dict[filename] = src_cleaned

        code_pattern = re.compile(r"\b(PR64[A-Z0-9-]{2,}|PRDC\d{2,3}|BM64[A-Z0-9-]{2,})\b", re.I)

        lines = [line.strip() for line in full_text.split("\n") if line.strip()]
        for line in lines:
            codes = code_pattern.findall(line)
            if not codes:
                continue
            code = codes[0].upper()

            name_candidate = re.sub(r"\b" + re.escape(code) + r"\b", "", line, flags=re.I).strip(" :-–—")
            name_candidate = re.sub(r"\s{2,}", " ", name_candidate).strip()
            if not name_candidate or len(name_candidate) < 3:
                continue

            img_urls = []
            slug = re.sub(r"[^a-z0-9]", "-", name_candidate.lower())
            slug = re.sub(r"-+", "-", slug).strip("-")
            for key, src in img_dict.items():
                if slug[:10] in key or code.lower() in key:
                    img_urls.append(src)

            release_year = None
            release_year_confidence = None
            ym = re.search(r"\b(20\d{2})\b", line)
            if ym:
                release_year = int(ym.group(1))
                release_year_confidence = "inferred"

            self.crawler._save_or_merge_product(
                item_number=code,
                product_name=name_candidate,
                brand="Pop Race",
                scale="1:64",
                series="Regular Collection",
                img_urls=img_urls,
                source="diecastsociety",
                release_year=release_year,
                release_year_confidence=release_year_confidence,
                status=None,
                toy_brand="Pop Race",
            )
