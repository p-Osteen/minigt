import os
import re
import json
import logging
import urllib.parse
from typing import List, Dict, Set, Optional
from bs4 import BeautifulSoup

from crawler.utils import clean_fandom_image_url, get_row_product_images

logger = logging.getLogger("crawler")

class HotWheelsBrandHandler:
    """
    Crawls Hot Wheels from hotwheels.fandom.com.

    Discovery sources, in order:
      1. Local reference HTML for the wiki hub page (Hot_Wheels.html) — every
         category on that page is a single element (a <p> or <tr>) whose
         direct child is <i>Category Name</i>, immediately followed by all
         of that category's <a href="/wiki/..."> links. We just grab every
         link inside that same element — no sibling-walking needed.
      2. The "Series/Categories" and "Designer Pages" tables on the same hub
         page (each is a <b>Label</b> inside a <ul>, immediately followed by
         a sibling <table> of links).
      3. Live Fandom `allpages` API traversal, so every casting/list page on
         the wiki is eventually queued even if it isn't linked from the hub.

    Product tables (list pages like "List_of_2026_Hot_Wheels" and casting
    pages) are parsed generically: any <table class="wikitable"> is treated
    as a product table, with header detection for Toy #/Model Name/Series/
    Color/Year/Photo columns.
    """

    SKIP_NS = {"File:", "Special:", "Template:", "Help:", "Talk:", "User:",
               "User_talk:", "Forum:", "Board:", "Thread:", "Category:"}

    NON_PRODUCT_HEADINGS = {
        "contents", "gallery", "trivia", "references", "external links",
        "see also", "navigation", "variations", "releases", "casting",
        "history",
    }

    def __init__(self, crawler):
        self.crawler = crawler
        self.seen_pages: Set[str] = set()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_text(text: str) -> str:
        """Collapse internal whitespace/newlines from wiki markup line-wraps."""
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _clean_page(href: str) -> Optional[str]:
        if "/wiki/" not in href:
            return None
        page = urllib.parse.unquote(href.split("/wiki/")[-1]).split("#")[0]
        if not page or page == "Main_Page":
            return None
        ns = page.split(":", 1)[0] + ":" if ":" in page else ""
        if ns in HotWheelsBrandHandler.SKIP_NS:
            return None
        return page

    @staticmethod
    def _infer_year(text: str) -> Optional[int]:
        ym = re.search(r"\b(20\d{2})\b", text) or re.search(r"\b(19\d{2})\b", text)
        if ym:
            y = int(ym.group(1))
            if 1968 <= y <= 2035:
                return y
        return None

    def _page_task(self, page_name: str, series_group: str, series_name: str = "",
                    year: Optional[int] = None, source: str = "fandom_list") -> Dict:
        api_url = (f"https://hotwheels.fandom.com/api.php?action=parse"
                   f"&page={urllib.parse.quote(page_name)}&format=json&prop=text")
        return {
            "source": source,
            "url": api_url,
            "meta": {
                "page_name": page_name,
                "year": year if year is not None else self._infer_year(page_name),
                "series_group": series_group,
                "series_name": series_name or page_name.replace("_", " "),
                "sub_series": "Regular",
            },
        }

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def discover_sources(self) -> List[Dict]:
        pending: List[Dict] = []
        self.seen_pages = set()

        def enqueue(page_name: str, series_group: str, series_name: str = ""):
            if page_name in self.seen_pages:
                return
            self.seen_pages.add(page_name)
            pending.append(self._page_task(page_name, series_group, series_name))

        # ---- 1. Parse the hub page (local reference HTML, else live) ----
        hub_html = None
        ref_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "reference_htmls", "Hot_Wheels.html"
        )
        if os.path.exists(ref_path):
            logger.info("Discovering Hot Wheels links from local reference HTML (Hot_Wheels.html)...")
            try:
                with open(ref_path, "r", encoding="utf-8") as f:
                    hub_html = f.read()
            except Exception as e:
                logger.error(f"Failed to read local Hot_Wheels.html: {e}")

        if not hub_html:
            logger.info("Fetching Hot Wheels hub page live...")
            hub_html = self.crawler.fetch_url(
                "https://hotwheels.fandom.com/api.php?action=parse&page=Hot_Wheels&format=json&prop=text",
                use_cache=True
            )
            if hub_html:
                try:
                    data = json.loads(hub_html)
                    hub_html = data.get("parse", {}).get("text", {}).get("*", "")
                except Exception as e:
                    logger.error(f"Failed to unwrap live Hot Wheels hub JSON: {e}")
                    hub_html = None

        if hub_html:
            try:
                soup = BeautifulSoup(hub_html, "lxml")
                content = soup.find(class_="mw-parser-output") or soup

                for el in content.find_all(["p", "tr"]):
                    i_tag = el.find("i", recursive=False)
                    if not i_tag:
                        continue
                    category = self._clean_text(i_tag.get_text(" ", strip=True)).rstrip(":").strip()
                    if not category:
                        continue
                    for a in el.find_all("a", href=True):
                        page_name = self._clean_page(a["href"])
                        if page_name:
                            series_name = self._clean_text(a.get_text(" ", strip=True)) or page_name.replace("_", " ")
                            enqueue(page_name, category, series_name)

                for b_tag in content.find_all("b"):
                    label = self._clean_text(b_tag.get_text(" ", strip=True)).rstrip(":").strip()
                    if label not in ("Series/Categories", "Designer Pages"):
                        continue
                    ul_parent = b_tag.find_parent("ul")
                    if not ul_parent:
                        continue
                    sib = ul_parent.next_sibling
                    while sib is not None and (not getattr(sib, "name", None) or sib.name != "table"):
                        sib = sib.next_sibling
                    if sib is None:
                        continue
                    for a in sib.find_all("a", href=True):
                        page_name = self._clean_page(a["href"])
                        if page_name:
                            enqueue(page_name, label, self._clean_text(a.get_text(" ", strip=True)))
            except Exception as e:
                logger.error(f"Failed to parse Hot Wheels hub page: {e}")

        logger.info(f"Hot Wheels discovery: {len(pending)} pages queued from hub page.")

        # ---- 2. Exhaustive allpages traversal for full wiki coverage ----
        logger.info("Hot Wheels discovery: running allpages traversal for full coverage...")
        apcontinue = ""
        while True:
            api_url = ("https://hotwheels.fandom.com/api.php?action=query&list=allpages"
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
                    if "redirect" in p:
                        continue
                    ns = page_name.split(":", 1)[0] + ":" if ":" in page_name else ""
                    if ns in self.SKIP_NS or page_name == "Main_Page":
                        continue
                    enqueue(page_name, "Complete Collection")

                apcontinue = data.get("continue", {}).get("apcontinue", "")
                if not apcontinue:
                    break
            except Exception as e:
                logger.error(f"Error fetching Hot Wheels allpages: {e}")
                break

        logger.info(f"Hot Wheels discovery: {len(pending)} total pages queued.")
        return pending

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------
    def parse_task(self, html_or_json: str, task: Dict) -> Optional[List[Dict]]:
        meta = task.get("meta", {})
        page_name = meta.get("page_name", "")
        try:
            res_data = json.loads(html_or_json)
            html_content = res_data.get("parse", {}).get("text", {}).get("*", "")
            if not html_content:
                return None
            soup = BeautifulSoup(html_content, "lxml")
        except Exception as e:
            logger.error(f"Hot Wheels JSON parse error for {page_name}: {e}")
            return None

        if page_name.startswith("Category:"):
            new_tasks = []
            for a in soup.find_all("a", href=True):
                member_page = self._clean_page(a["href"])
                if member_page and member_page not in self.seen_pages:
                    self.seen_pages.add(member_page)
                    new_tasks.append(self._page_task(
                        member_page, "Category Member", a.get_text(" ", strip=True)
                    ))
            return new_tasks

        page_year = meta.get("year")
        series_group = meta.get("series_group", "By Year")
        default_sub_series = meta.get("sub_series", "Regular")
        casting_name = page_name.replace("_", " ")

        def get_preceding_heading(tbl):
            curr = tbl
            while curr:
                prev = curr.previous_sibling
                while prev:
                    if getattr(prev, "name", None) in ("h2", "h3", "h4"):
                        text = self._clean_text(re.sub(r"\[.*?\]", "", prev.get_text(" ", strip=True)))
                        if text.lower() not in self.NON_PRODUCT_HEADINGS:
                            return text
                    if getattr(prev, "name", None):
                        headings = prev.find_all(["h2", "h3", "h4"])
                        if headings:
                            text = self._clean_text(re.sub(r"\[.*?\]", "", headings[-1].get_text(" ", strip=True)))
                            if text.lower() not in self.NON_PRODUCT_HEADINGS:
                                return text
                    prev = prev.previous_sibling
                curr = curr.parent
                if curr and curr.name == "body":
                    break
            return None

        new_tasks = []

        for table in soup.find_all("table"):
            table_classes = table.get("class") or []
            if "wikitable" not in table_classes:
                continue
            if table.find_parent("table") is not None:
                continue

            table_heading = get_preceding_heading(table)
            thead = table.find("thead", recursive=False)
            tbody = table.find("tbody", recursive=False)
            tfoot = table.find("tfoot", recursive=False)

            if thead is not None:
                header_rows = thead.find_all("tr", recursive=False)
                if not header_rows:
                    continue
                header_row = header_rows[0]
                all_data_rows = ((tbody.find_all("tr", recursive=False) if tbody else []) +
                                  (tfoot.find_all("tr", recursive=False) if tfoot else []))
            else:
                container = tbody if tbody is not None else table
                all_rows = container.find_all("tr", recursive=False)
                if not all_rows:
                    continue
                header_row = all_rows[0]
                all_data_rows = all_rows[1:]

            header_cells = header_row.find_all(["th", "td"], recursive=False)
            headers = [c.get_text(strip=True).lower() for c in header_cells]

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
                elif h == "series" or ("series" in h and "series #" not in h and "series no" not in h and "series number" not in h):
                    series_idx = idx
                elif "year" in h:
                    year_idx = idx
                elif any(k in h for k in ("photo", "image", "pic")):
                    photo_idx = idx

            if code_idx == -1 and name_idx == -1 and color_idx == -1:
                continue

            for row in all_data_rows:
                cells = row.find_all(["td", "th"], recursive=False)
                if len(cells) <= max(code_idx, name_idx, color_idx, series_idx, year_idx):
                    continue

                item_number = cells[code_idx].get_text(strip=True) if code_idx != -1 else ""
                if item_number == "-":
                    item_number = ""

                row_name = cells[name_idx].get_text(strip=True) if name_idx != -1 else ""
                row_color = cells[color_idx].get_text(strip=True) if color_idx != -1 else ""

                if task.get("source") == "fandom_casting":
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
                    y = self._infer_year(cells[year_idx].get_text(strip=True))
                    if y:
                        row_year = y

                series_val = meta.get("series_name") or page_name.replace("_", " ")
                sub_series_val = table_heading or default_sub_series
                if (not table_heading or len(table_heading) < 3) and series_idx != -1:
                    cell_series = cells[series_idx].get_text(" ", strip=True)
                    series_cleaned = cell_series.split("\n")[0].split("New for")[0].strip()
                    if series_cleaned:
                        sub_series_val = series_cleaned

                img_urls = get_row_product_images(row)
                if not img_urls and photo_idx != -1 and photo_idx < len(cells):
                    img_tag = cells[photo_idx].find("img")
                    if img_tag:
                        img_url = img_tag.get("data-src") or img_tag.get("src", "")
                        if img_url and "data:image" not in img_url:
                            img_urls = [clean_fandom_image_url(img_url)]

                if task.get("source") == "fandom_list":
                    target_cells = []
                    if name_idx != -1:
                        target_cells.append(cells[name_idx])
                    if color_idx != -1:
                        target_cells.append(cells[color_idx])
                    for cell in target_cells:
                        for a in cell.find_all("a", href=True):
                            casting_page = self._clean_page(a["href"])
                            if casting_page and "List_of_" not in casting_page and casting_page not in self.seen_pages:
                                self.seen_pages.add(casting_page)
                                new_tasks.append({
                                    "source": "fandom_casting",
                                    "url": (f"https://hotwheels.fandom.com/api.php?action=parse"
                                             f"&page={urllib.parse.quote(casting_page)}&format=json&prop=text"),
                                    "meta": {
                                        "page_name": casting_page,
                                        "year": row_year,
                                        "series_group": series_group,
                                        "series_name": series_val,
                                        "sub_series": "Regular",
                                    },
                                })

                attributes: Dict = {"series_group": series_group}
                if row_color:
                    attributes["color"] = row_color
                if row_name:
                    attributes["vehicle_model"] = row_name

                hw_scale = "1:64"
                scale_haystack = f"{product_name} {series_val} {sub_series_val}".lower()
                for sc in ["1:18", "1/18", "1:43", "1/43", "1:50", "1/50", "1:24", "1/24"]:
                    if sc in scale_haystack:
                        hw_scale = sc.replace("/", ":")
                        break

                self.crawler._save_or_merge_product(
                    item_number=item_number,
                    product_name=product_name,
                    brand="Hot Wheels",
                    scale=hw_scale,
                    series=series_val,
                    img_urls=img_urls,
                    source="fandom",
                    release_year=row_year,
                    release_year_confidence="confirmed" if row_year else None,
                    status="Released",
                    toy_brand="Hot Wheels",
                    sub_series=sub_series_val,
                    attributes=attributes,
                )

        return new_tasks
