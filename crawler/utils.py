import os
import re
import json
import logging
import urllib.parse
from typing import List, Dict, Set, Optional
from bs4 import BeautifulSoup

logger = logging.getLogger("crawler")

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
            url = ""
            if parent_a and parent_a.get("href", "").startswith("http"):
                url = parent_a["href"]
            else:
                url = img.get("data-src") or img.get("src", "")
                if url and "data:image" not in url:
                    url = clean_fandom_image_url(url)

            if url and "data:image" not in url and url not in seen:
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


def parse_my64_list(crawler, html: str, grp_id: str, toy_brand: str) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    new_tasks = []
    
    # 1. Discover product detail pages
    seen_ids = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "id=" in href and f"grpid={grp_id}" in href:
            id_match = re.search(r"id=(\d+)", href)
            if id_match:
                prod_id = id_match.group(1)
                if prod_id not in seen_ids:
                    seen_ids.add(prod_id)
                    abs_url = href
                    if not abs_url.startswith("http"):
                        clean_href = abs_url.lstrip("/")
                        if clean_href.startswith("usr/"):
                            clean_href = clean_href[4:]
                        abs_url = f"https://www.my64.com.my/usr/{clean_href}"
                    new_tasks.append({
                        "source": "my64_detail",
                        "url": abs_url,
                        "meta": {
                            "toy_brand": toy_brand,
                            "grp_id": grp_id
                        }
                    })
                    
    # 2. Discover pagination
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "pg=" in href and f"grpid={grp_id}" in href:
            pg_match = re.search(r"pg=(\d+)", href)
            if pg_match:
                page_num = pg_match.group(1)
                abs_url = href
                if not abs_url.startswith("http"):
                    clean_href = abs_url.lstrip("/")
                    if clean_href.startswith("usr/"):
                        clean_href = clean_href[4:]
                    abs_url = f"https://www.my64.com.my/usr/{clean_href}"
                new_tasks.append({
                    "source": "my64_list",
                    "url": abs_url,
                    "meta": {
                        "toy_brand": toy_brand,
                        "grp_id": grp_id,
                        "page": int(page_num)
                    }
                })
                
    return new_tasks


def parse_my64_detail(crawler, html: str, url: str, toy_brand: str, grp_id: str) -> None:
    soup = BeautifulSoup(html, "lxml")
    
    product_name = ""
    title_tag = soup.find("title")
    if title_tag:
        title_text = title_tag.get_text(strip=True)
        if "::" in title_text:
            parts = title_text.split("::")
            if len(parts) > 1:
                product_name = parts[1].strip()
                
    for div in soup.find_all("div"):
        txt = div.get_text(strip=True)
        if "home > products" in txt.lower() and ">" in txt:
            parts = [p.strip() for p in txt.split(">") if p.strip()]
            if len(parts) > 2:
                product_name = parts[-1]
                break

    if not product_name:
        h2_tags = soup.find_all("h2")
        for h2 in h2_tags:
            h2_txt = h2.get_text(strip=True)
            if h2_txt and len(h2_txt) > 10:
                product_name = h2_txt
                break

    if not product_name:
        return

    brand = toy_brand
    item_number = ""
    
    full_text = soup.get_text("\n")
    lines = [l.strip() for l in full_text.split("\n") if l.strip()]
    for idx, line in enumerate(lines):
        line_lower = line.lower()
        if "brand :" in line_lower or "brand:" in line_lower:
            val = line.split(":")[-1].strip()
            if not val and idx + 1 < len(lines):
                val = lines[idx+1].strip()
            if val:
                brand = val
        elif "item code" in line_lower:
            val = line.split(":")[-1].strip()
            if not val and idx + 1 < len(lines):
                val = lines[idx+1].strip()
            if val:
                item_number = val

    if not item_number:
        id_match = re.search(r"id=(\d+)", url)
        if id_match:
            item_number = f"MY64-{toy_brand.replace(' ', '')}-{id_match.group(1)}"
        else:
            return

    img_urls = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and ("wp-content" in src or "/data/prod/" in src) and not src.endswith(".gif") and "logo" not in src.lower():
            if src.startswith("."):
                src = src.lstrip(".")
            if not src.startswith("http"):
                src = f"https://www.my64.com.my/{src.lstrip('/')}"
            if src not in img_urls:
                img_urls.append(src)

    scale = "1:64"
    if "1:18" in product_name or "1/18" in product_name or grp_id == "27":
        scale = "1:18"
    elif "1:43" in product_name or "1/43" in product_name:
        scale = "1:43"
    elif "1:64" in product_name or "1/64" in product_name:
        scale = "1:64"

    crawler._save_or_merge_product(
        item_number=item_number,
        product_name=product_name,
        brand=brand,
        scale=scale,
        series="Regular Collection" if toy_brand == "Pop Race" else "Regular",
        img_urls=img_urls,
        source="my64",
        release_year=None,
        release_year_confidence=None,
        status=None if toy_brand == "Pop Race" else "Released",
        toy_brand=toy_brand
    )
