# Implementation Plan — Add Hot Wheels & Pop Race to the Catalog System

We will extend the catalog system to crawl, classify, and display **Hot Wheels** and **Pop Race** models alongside **MINI GT**.

---

## User Review Required

> [!IMPORTANT]
> **Data Size Measurement and Lazy-Loading Decision**:
> - Current MINI GT JSON: **1,969 items** (~1.44 MB).
> - Hot Wheels (Mainline + Premium): Estimated **10,000 to 25,000 items** (~7.5 MB to 19 MB).
> - Pop Race: Estimated **480 items** (~370 KB).
> - **Decision**: We will split the JSON exports by brand into separate static files: `products_minigt.json`, `products_hotwheels.json`, and `products_poprace.json`. The frontend will dynamically load these files lazily in the browser when a brand is selected, ensuring optimal performance on desktop and mobile.

> [!WARNING]
> **Database Schema Migration**:
> - `Product.item_number` is currently a single primary key, which risks cross-brand conflicts.
> - We will add a `toy_brand` column to partition the namespaces and change the primary key to a composite key `(toy_brand, item_number)`.
> - A migration script will be written to automatically restructure the database without losing any existing MINI GT records.

---

## Proposed Changes

### Database Layer

#### [MODIFY] [models.py](file:///c:/Users/Paul/Desktop/Mods/TSM/database/models.py)
* Update `Product` to define a composite primary key:
  ```python
  toy_brand = Column(String, primary_key=True, index=True, default="MINI GT")
  item_number = Column(String, primary_key=True, index=True)
  ```
* Update `to_dict()` to include the brand namespace.

#### [NEW] [migration.py](file:///c:/Users/Paul/Desktop/Mods/TSM/database/migration.py)
* Add a schema migration script to:
  1. Inspect the `products` table columns.
  2. If `toy_brand` is missing, rename `products` to `products_old`.
  3. Create the new `products` table with the composite primary key `(toy_brand, item_number)`.
  4. Recreate the search indexes (`idx_brand_series`, `idx_brand_scale`, etc.).
  5. Copy data from `products_old` to `products` setting `toy_brand = 'MINI GT'` for all existing rows.
  6. Verify and drop `products_old`.

#### [MODIFY] [db_manager.py](file:///c:/Users/Paul/Desktop/Mods/TSM/database/db_manager.py)
* **Deduplication**: Scope `deduplicate_database()` by `toy_brand` (so MINI GT's specific RHD/LHD deduplication rules are run only on MINI GT rows).
* **Export Split**: Update `sync_to_json()` to export products grouped by `toy_brand` to their respective JSON files: `products_minigt.json`, `products_hotwheels.json`, and `products_poprace.json`.

#### [MODIFY] [classify.py](file:///c:/Users/Paul/Desktop/Mods/TSM/database/classify.py)
* Separate brand classification rules:
  * For **MINI GT**: Keep existing tuned rules.
  * For **Hot Wheels**: Add a dedicated parser that extracts the car maker from `product_name`, maps the series/line (Mainline, Premium/Car Culture, Boulevard, etc.), and identifies case/assortment when available.
  * For **Pop Race**: Extract the car maker and sub-series (like Singer, RWB, BAPE collaborations, chrome editions).

---

### Crawler Layer

#### [MODIFY] [crawler/crawler.py](file:///c:/Users/Paul/Desktop/Mods/TSM/crawler/crawler.py)
* Refactor `crawler.py` to separate the crawl manager from brand-specific scraping logic:
  * Define a common scraper handler interface.
  * Keep the generic multi-threaded scheduler, `fetch_url` HTML caching, retry setup, and task state tracking in a base `DiecastCrawler` class.
  * Implement three handlers:
    1. `MiniGTCrawlerHandler` (crawls official site, fandom wiki category pages, and MyMiniGT sitemaps).
    2. `HotWheelsCrawlerHandler` (crawls yearly lists on `hotwheels.fandom.com`, e.g. `List_of_2024_Hot_Wheels`, `List_of_2023_Hot_Wheels`, and premium series pages like `2024_Car_Culture` or `2024_Car_Culture:_Team_Transport`).
    3. `PopRaceCrawlerHandler` (crawls the community-wide `Regular_Collection` page on `pop-race.fandom.com` and new announcement pages on `diecastsociety.com`).
* Update `_save_or_merge_product` to query and merge records scoped by `(toy_brand, item_number)`. Make the source priority rankings brand-specific.

---

### Frontend Layer

#### [MODIFY] [index.html](file:///c:/Users/Paul/Desktop/Mods/TSM/index.html)
* **Brand Tabs**: Replace the static `"MINI GT"` header logo text with a segmented tab selector (`MINI GT`, `Hot Wheels`, `Pop Race`).
* **Lazy Loading**: Change JS to lazily load the specific brand's JSON (`products_minigt.json`, `products_hotwheels.json`, or `products_poprace.json`) into `ALL_PRODUCTS` when the tab is clicked, caching it in memory.
* **Facet Filters**: Dynamically populate the select options based on the active brand's actual data.
* **Model Rendering**: Render model cards using brand-appropriate tags (e.g. showing "Toy #" on Hot Wheels, "Model #" on Pop Race, "Item #" on MINI GT).

#### [MODIFY] [static/styles.css](file:///c:/Users/Paul/Desktop/Mods/TSM/static/styles.css)
* Add styling for `.brand-tabs` and `.brand-tab` with a modern dark theme design.
* Adjust responsive layouts for header wrapping.

---

## Verification Plan

### Automated Verification
* Run the migration script and verify database integrity.
* Run the verification scripts (`verify_sync.py`) to confirm no primary key collisions occur.

### Manual Verification
1. Launch local preview server.
2. Verify that clicking on tabs (MINI GT, Hot Wheels, Pop Race) updates the catalog views seamlessly.
3. Test searching and filters across each brand.
4. Verify layout responsiveness on both desktop and mobile views.
