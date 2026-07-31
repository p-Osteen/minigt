# Progress Status — Add Hot Wheels & Pop Race

We have completed the setup, DB migration, and initial crawl refactoring tasks. Below is the progress status and the checklist to resume.

## Work Completed

1. **Database Schema Migration (Task 1)**:
   - Added the `toy_brand` namespace column to `Product` model in `database/models.py`.
   - Setup a composite primary key `(toy_brand, item_number)` to prevent cross-brand collisions.
   - Wrote and executed `database/migration.py` which restructured the schema while preserving all existing MINI GT records (1,969 items verified).
   - Scoped `deduplicate_database()` inside `database/db_manager.py` to only process MINI GT records.

2. **JSON Export Brand Split (Task 6)**:
   - Updated `sync_to_json()` in `database/db_manager.py` to classify and split items by brand.
   - Outputs:
     - `database/products_minigt.json`
     - `database/products_hotwheels.json`
     - `database/products_poprace.json`
     - `database/products.json` (as fallback containing MINI GT data).
   - Custom sorting applied (Standard custom groups for MINI GT, year desc + series/SKU for Hot Wheels, year desc + model # for Pop Race).

## Next Steps to Resume

1. **Task 2: Refactor Crawler Architecture**:
   - Refactor `crawler/crawler.py` to extract the common base class (`DiecastCrawler` / base request & cache logic).
   - Create brand-specific handlers (`MiniGTBrandHandler`, `HotWheelsBrandHandler`, `PopRaceBrandHandler`) implementing a common `discover_sources()` and `parse_task()` interface.

2. **Task 3: Implement Hot Wheels Crawler Handler**:
   - Fetch mainlines (e.g. `List_of_2024_Hot_Wheels`) and premium lines (`2024_Car_Culture`, `Hot_Wheels_Boulevard_(2024)`) from Fandom Wiki.
   - Parse them and save with `toy_brand="Hot Wheels"`.

3. **Task 4: Implement Pop Race Crawler Handler**:
   - Fetch `Regular_Collection` from `pop-race.fandom.com` and preorders/announcements from `diecastsociety.com`.
   - Parse them and save with `toy_brand="Pop Race"`.

4. **Task 5: Brand-Specific Classification**:
   - In `database/classify.py`, add classification functions to extract makers/series/finishes for Hot Wheels and Pop Race.

5. **Task 7: Frontend & Style Changes**:
   - Add a Brand Tab selector to the site header in `index.html`.
   - Lazy load corresponding JSON file when a tab is clicked.
   - Dynamically update filter dropdown values.
   - Style in `static/styles.css`.
