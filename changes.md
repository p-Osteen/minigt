# Implementation Prompt — Add Hot Wheels & Pop Race to the Catalog System

## Context

This project (`TSM`) is a local, occasionally-run crawler + static catalog viewer for MINI GT diecast models. It has three layers:
- `crawler/crawler.py` — a single hardcoded `MINI_GTCrawler` class that scrapes three MINI GT-specific sources (official site, Fandom wiki, MyMiniGT) into SQLite.
- `database/models.py` + `database/db_manager.py` + `database/classify.py` — SQLite schema, sync/migration logic, and deterministic post-scrape classification (manufacturer, category, collaboration, theme, body style, etc.).
- `index.html` — a static frontend that loads the fully-exported `products.json` into browser memory and filters client-side.

SQLite (`database/products.db`) is already the source of truth; `products.json` is a generated export the frontend reads. This is a script run occasionally to refresh the dataset — not a live server with concurrent users — so **do not introduce a backend/API layer**. Keep the existing shape: SQLite as source of truth, JSON as a generated static export for the frontend.

## Goal

Extend this system to also crawl, classify, and display **Hot Wheels** and **Pop Race** catalogs alongside the existing MINI GT catalog, without breaking MINI GT's existing data or functionality.

## Required first step: measure before deciding the export shape

Before writing any Hot Wheels crawler code, estimate the realistic total row count for Hot Wheels (mainline + Premium lines: Silver Series, Car Culture, Boulevard, Team Transport, Fast & Furious, and other recurring official series) and project the resulting `products.json` size using the current file's actual bytes-per-item as a baseline. Use that number to decide, and state the decision explicitly before proceeding:
- **If a single combined `products.json` stays in a reasonable size range for the static frontend to load** (validate against real page-load behavior, not a guess), keep one file.
- **If the projected size is large enough to hurt load time/mobile performance**, split the export into one JSON file per brand, loaded lazily by the frontend when that brand is selected (this is a data-export and frontend-loading change only — it does not require a backend, API, or database engine change).

Do not silently pick one without measuring — this decision affects the frontend work in a later step, so get it right early.

## Required second step: fix the primary-key collision risk

`Product.item_number` is currently the sole primary key across the entire catalog, and `_save_or_merge_product` merges purely on `item_number` collision. Hot Wheels and Pop Race each have their own independent numbering schemes that are **not guaranteed to be unique against MINI GT's or each other's** item numbers. Before importing any non-MINI-GT data:
- Add a `brand` (or equivalent) column if one doesn't already function as a true namespace, and change the effective uniqueness constraint to `(brand, item_number)` rather than `item_number` alone — via a composite primary key or a surrogate key plus a unique constraint.
- Update `_save_or_merge_product` (and any other code that looks up/merges by `item_number` alone) to always scope by brand as well.
- Write a migration that preserves all existing MINI GT rows and their current `item_number` values unchanged.

## Crawler architecture

Refactor `MINI_GTCrawler` so the brand-agnostic parts (HTTP session/retry setup, the HTML cache-by-URL-hash in `fetch_url`, rate limiting, state persistence in `crawler_state.json`, the threaded batch-write pattern) are shared, and each brand's source discovery + page parsing lives in its own module/class implementing a common interface (e.g., `discover_sources() -> list of tasks`, `parse_detail(html, meta) -> product fields`). Do not simply add more `if source == "..."` branches to the existing methods — the existing three-source logic is already interleaved in `run_discovery()` in a way that would become unmaintainable with two more brands' sources added the same way.

Keep the existing tie-breaking merge logic in `_save_or_merge_product` (priority overwrite / tie-break-by-longer-name / fill-missing-fields) as the general pattern, but make the source-priority map brand-scoped instead of one global 3-entry dict, since Hot Wheels and Pop Race each need their own source-reliability ranking (see sources below) — neither has an "official site" source the way MINI GT does.

### Hot Wheels sources
- **Primary: Hot Wheels Fandom Wiki** (`hotwheels.fandom.com`) — same MediaWiki API pattern already used for MINI GT's Fandom source (`action=parse&page=X&format=json`), directly reusable.
- **Secondary: CollectHW** (`collecthw.com`) — likely more current/accurate for recent releases, but its page structure is unverified. Do a markup discovery pass first (fetch a handful of real pages, inspect structure, check for a public API or JSON-LD before assuming raw HTML scraping is needed) before estimating or writing its parser.
- There is no official Mattel database/API — do not assume one exists or search for one; this has been confirmed absent.

### Pop Race sources
- **Primary: DiecastSociety.com** — a diecast news blog with same-quarter posts covering new Pop Race releases including item numbers; closest thing to a real-time announcement feed for this brand.
- **Secondary: one or two retailer catalog pages** (e.g. sites carrying Pop Race stock) for image/availability cross-checks. Pick based on a quick stocking/coverage check at implementation time rather than assuming any single retailer is authoritative.
- There is no official Pop Race database/API either — same caveat as Hot Wheels.

For both brands, do a **markup discovery/spike pass** (fetch and inspect a handful of real pages from each planned source) before writing parser code or committing to a time estimate for that source — do not write parsers against assumed/guessed page structure.

## Classification

Do not extend `classify.py`'s existing MINI GT-tuned keyword lists (collaborations, themes, body styles) with Hot Wheels/Pop Race terms mixed in — build brand-specific classification rule sets, since the underlying taxonomies are structurally different:
- **Hot Wheels** needs its own concepts: Series/line (Mainline, Premium/Car Culture/Boulevard/Team Transport/Fast & Furious/other recurring lines), and — decide and confirm during implementation, not upfront — whether Treasure Hunt/Super Treasure Hunt status and case-assortment/year are worth tracking as fields. Also decide during implementation (not upfront) what one catalog row represents: one casting, or one casting+colorway+year combination — this materially affects both row count and how classification rules are written, so make this call once you're looking at real scraped data, document the decision in code comments, and apply it consistently.
- **Pop Race** needs a `sub_series` concept (e.g. "Dark Chrome", "Black Chrome") and can likely reuse the existing `status` field for preorder/released tracking (`Released`/`Pre-Order` values already exist in the current schema).

Keep each brand's classification logic isolated (e.g., separate functions/modules per brand called from a shared dispatcher) rather than one growing set of if/elif branches — this keeps MINI GT's existing, already-tuned classification untouched and reduces risk of regressing it.

## Frontend

Extend `index.html` to support multiple brands:
- Add a brand selector (tabs or dropdown) instead of the hardcoded `"MINI GT"` header label.
- Facet filters (`populateFacets()`, `applyFilters()`) need to reflect the selected brand's actual classification fields, since Hot Wheels/Pop Race won't share MINI GT's exact facet set.
- Apply whatever loading strategy was decided in the "measure before deciding" step above (single JSON vs. per-brand JSON files).
- Do not break existing MINI GT filtering/search/print behavior — treat this as additive.

## Verification

- Confirm existing MINI GT rows and their `item_number` values are unchanged after the primary-key migration.
- Confirm no cross-brand `item_number` collisions occur after importing sample Hot Wheels and Pop Race data (test with real scraped data, not synthetic).
- Confirm the frontend loads and filters correctly for all three brands, and that switching brands doesn't require a full page reload of unrelated brands' data (if the per-brand JSON split was chosen).
- Spot-check classification output against real sampled items per brand, the same way MINI GT's classification was validated — do not assume the rules are correct without checking actual output against real product names/pages.

## Explicit non-goals

- No backend/API server — this stays a script-generates-static-files architecture.
- No image downloading pipeline — continue storing remote image URLs only, matching the existing MINI GT approach, unless a specific source requires otherwise (confirm before changing this pattern).
- Do not attempt to unify Hot Wheels' or Pop Race's classification taxonomy with MINI GT's existing one — keep brand-specific rule sets as described above.