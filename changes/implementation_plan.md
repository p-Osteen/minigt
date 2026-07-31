# Implementation Plan - Scraping Everything & High-Quality Images

This plan details the technical changes needed to ensure `crawler/crawler.py` scrapes all products from the Hot Wheels and Pop Race wikis using the filter structures in `reference_htmls`, while also resolving issues with missing and low-quality images.

## User Review Required

> [!IMPORTANT]
> The scraping scope will increase significantly since we are moving from hardcoded subsets (e.g. Hot Wheels years 2020-2026 only) to crawling all filter URLs defined in the `reference_htmls/` JSON files (170 URLs for Hot Wheels and 110 URLs for Pop Race).

## Proposed Changes

### Crawler Module

#### [MODIFY] [crawler.py](file:///c:/Users/paulp/Desktop/minigt/crawler/crawler.py)

We will introduce URL and image cleaning helpers, implement dynamic category membership discovery, and replace fragile column-based image parsing.

##### 1. Add Image & Filter Helper Functions
Add functions after imports to clean image paths and load filters:
- **`clean_fandom_image_url(url)`**: Strips sizing suffixes (`/scale-to-width-down/X`) from Fandom CDN paths while preserving `/revision/latest` and any query parameters (like cache busters `?cb=...`).
- **`clean_diecastsociety_image_url(url)`**: Strips WordPress dimension suffixes (e.g. `-75x50` or `-650x320`) from image filenames to point to original high-res uploads.
- **`get_row_product_images(tr)`**: Scans all columns in a table row to identify product images based on class wrappers (`class="image"`, `class="mw-file-description image"`, `class="thumbimage"`, or inside `<figure>`). Extracts `data-src` (resolving lazy loading) or fallback `src`, cleans them, and returns unique URLs.
- **`get_links_from_filters_json(filepath)`**: Recursively traverses the `"filters"` structure of a local JSON file and extracts all target links to seed the crawler.

##### 2. Refactor Hot Wheels Brand Handler
- **`discover_sources`**:
  - Load and extract all seed URLs from `reference_htmls/hot_wheels_filters.json`.
  - Convert them to Fandom API parse URLs (e.g. page name inside `action=parse&page=...`).
- **`parse_task`**:
  - Check if the task is a category page (`Category:...`). If yes, parse all member page links in the category and enqueue them as new tasks.
  - If it is a list page/casting page, parse tables using `get_row_product_images` for columns containing product images.

##### 3. Refactor Pop Race Brand Handler
- **`discover_sources`**:
  - Load and extract all seed URLs from `reference_htmls/pop_race_filters.json` (excluding manufacturer links naturally by using the structure defined in the `"filters"` key).
  - Convert them to Fandom API parse URLs and enqueue.
  - Keep `diecastsociety.com` search pages.
- **`parse_task`**:
  - Handle `Category:` links (if any) by enqueuing category member pages.
  - Parse product tables using robust row-based image finding (`get_row_product_images`).
- **`_parse_diecastsociety_post`**:
  - Clean extracted image URLs using `clean_diecastsociety_image_url` and correctly associate clean filenames with product codes.

##### 4. Refactor General Fandom Parsing in Crawler class
- Update `_parse_fandom_page` to use `clean_fandom_image_url` for list items `<li>` and paragraphs `<p>`.

---

## Verification Plan

### Automated Tests
We will execute python code validation to ensure syntax is correct and run test parses on cached pages:
- Verify that `extract_links.py` results match the loaded seed queues.
- Verify that the image URLs parsed from `be9efd1ad886331a3c17c67dd28e2312.html` (2021 Hot Wheels) and `d01a4cc7a42551602625af4c65cfaccd.html` (Pop Race Enigma) are correctly resolved to high quality `/revision/latest` URLs.

### Manual Verification
- Clear `cache/crawler_state.json` to force rediscovery.
- Run `py app.py` and select **Option 1 -> Scrape Pop Race** and **Option 1 -> Scrape Hot Wheels** to verify the discovery phase enqueues the complete list of filters.
- Verify that the database JSON outputs (`database/products_hotwheels.json` and `database/products_poprace.json`) are updated with the full range of years and high-res image URLs.
