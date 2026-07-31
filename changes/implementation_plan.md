# Implementation Plan - Exclude Brands & Fix Hot Wheels & Pop Race Image Loading

This plan details the changes needed to prevent non-Pop Race brands from being imported into the Pop Race catalog and to resolve the missing/broken image display issues for both Hot Wheels and Pop Race.

## Hot Wheels & Pop Race Image Display Issues
- **Hot Wheels**: Bypasses Wikia Fandom CDN hotlinking block (403 Forbidden). Currently, 100% of Fandom image assets fail to render in the frontend dashboard.
- **Pop Race**: Bypasses hotlink protection on both the Wikia Fandom CDN and Diecast Society WordPress CDN, which also block third-party origin referrers.

---

## Proposed Changes

### Crawler Module

#### [MODIFY] [crawler.py](file:///c:/Users/paulp/Desktop/minigt/crawler/crawler.py)
We will exclude other diecast brands from the Pop Race Fandom Wiki crawling loop.
- **`PopRaceBrandHandler.discover_sources`**:
  - Introduce an exclusion check when parsing page links from the navigation header and body parser containers.
  - Skip enqueuing tasks if the page name matches any of the following non-Pop Race brands:
    * `BM Creations`
    * `INNO64`
    * `MINI GT`
    * `PARA64`
    * `Tarmac Works`
    * `unique model`

---

### Dashboard Web Interface

#### [MODIFY] [index.html](file:///c:/Users/paulp/Desktop/minigt/index.html)
Add a `<meta name="referrer" content="no-referrer">` tag in the `<head>` of the document. This stops the browser from sending a `Referer` header to the Wikia image CDN (`static.wikia.nocookie.net`) and Diecast Society, bypassing their hotlink protection and fixing the 403 Forbidden broken images.

#### [MODIFY] [catalog_print.html](file:///c:/Users/paulp/Desktop/minigt/catalog_print.html)
Add the same `<meta name="referrer" content="no-referrer">` tag to ensure that high-resolution images are also rendered successfully in the print view layout.

---

## Verification Plan

### Automated Verification
- Verify that `crawler/crawler.py` compiles successfully.

### Manual Verification
- Clear Pop Race brand data and cached crawler state.
- Run the Pop Race scraper to verify that no Tarmac Works, MINI GT, INNO64, or other excluded brands are imported into the Pop Race tables.
- Open `index.html` in a web browser and check that:
  - Fandom Wiki images load successfully for Hot Wheels and Pop Race.
  - No 403 Forbidden errors are returned in the browser console.
