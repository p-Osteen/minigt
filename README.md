# MINI GT 1:64 Scale Catalog CLI Panel & Dashboard

A professional-grade, concurrent Python-based cataloging system designed to crawl, maintain, deduplicate, and host a comprehensive catalog of MINI GT 1:64 scale product releases. The system compiles data from multiple sources (Official website, MyMiniGT, and Fandom Wiki) into a local SQLite database, resolves LHD/RHD duplicates by source priority, and automatically builds/deploys a premium, responsive static dashboard to GitHub Pages.

---

## Key Features

1. **High-Performance BeautifulSoup Crawler**:
   * Scrapes concurrently from three sources: the Official site, MyMiniGT, and Fandom Wiki.
   * Utilizes local raw HTML caching in `cache/html/` to enable near-instantaneous incremental scrapes.
   * Saves only valid 1:64 scale diecast models.
   * Excludes accessories, container boxes, and figurines (purging A-prefix item codes).
   * Fully supports scrape resumption via state tracking in `cache/crawler_state.json`.

2. **Automated Duplicate Resolution (Cross-Source Merge)**:
   * Identifies duplicates strictly based on exact-match item numbers across data sources.
   * Suffix variations (e.g. standardizing LHD `MGT00006` and RHD `MGT00006R`) are recognized as distinct models and remain unchanged.
   * Keeps exactly one record from the highest-priority source:
     1. `https://minigt.tsm-models.com/` (Official)
     2. `https://myminigt.com/` (MyMiniGT)
     3. `https://minigt.fandom.com/wiki/MINI_GT` (Fandom)
   * Uses the winner's images and metadata exactly as-is, discarding all lower-priority duplicates without merging galleries or combining files.

3. **Advanced Grouped Series Sorting**:
   * Sorts the entire database systematically into continuous blocks by series prefix:
     1. **`MGT` Series**: Standard releases sorted numerically (`MGT00000` to `MGT64044`).
     2. **`KHMG`/`KH` Series**: Kaido House releases sorted numerically.
     3. **`OEM` Series**: Normalized automatically to `OEM-YY-NN` for display/sorting and sorted numerically.
     4. **Remaining Series**: Sorted alphabetically by prefix (e.g. `BL`, `S`, `XX`) and naturally by number.
     5. **Malformed/Abnormal Items**: Excessively long concatenated strings (e.g. `EUNOSROADSTER...`) are pushed to the very bottom of the catalog, preserving their original names.

4. **Sleek Static Catalog Dashboard (`index.html`)**:
   * Hosted for free on GitHub Pages.
   * Displays a responsive, premium grid layout (up to 6 columns).
   * Features virtual infinite scrolling for seamless loading of 1,900+ models.
   * Includes instant token-matching search and dynamic brand filters.
   * Toggleable sleek Dark/Light themes.
   * Integrated print dialog supporting custom page generation (by brand/all models) in a clean grid layout.

5. **Automated Git Deployment Pipeline (`deploy.py`)**:
   * Packages the deduplicated data into `database/products.json`.
   * Stages, commits, and pushes updates to your GitHub repository, automatically triggering GitHub Pages builds.

---

## Project Structure

```
TSM/
│
├── cache/
│   ├── html/                 # Cached raw product HTML pages
│   └── crawler_state.json    # Saved crawler queue for pausing/resuming
│
├── crawler/
│   └── crawler.py            # Beautiful Soup concurrent parser and sync rules
│
├── database/
│   ├── db_manager.py         # SQLite connection manager, custom sorting & deduplication
│   ├── models.py             # SQLAlchemy schema defining the Product table
│   ├── products.db           # Local SQLite database (gitignored)
│   └── products.json         # Synchronized sorted JSON catalog
│
├── logs/
│   └── crawler.log           # Crawling activity and deduplication logging
│
├── static/
│   └── styles.css            # Dashboard themes, fonts, and grid layout styles
│
├── app.py                    # Interactive CLI Control Panel
├── deploy.py                 # Automated git synchronization script
├── index.html                # Premium static dashboard frontend
├── catalog_print.html        # Print-preview template grid
├── requirements.txt          # Python package requirements
└── README.md                 # Project documentation
```

---

## Getting Started

### 1. Install Dependencies
Ensure you have Python 3.11+ installed. Run:
```bash
pip install -r requirements.txt
```

### 2. Configure Environment
Create a `.env` file in the root folder and add your GitHub Access Token for automated deploys:
```env
GITHUB_TOKEN=your_github_personal_access_token_here
```

### 3. Run the CLI Control Panel
Start the control panel:
```bash
python app.py
```
This launches the interactive menu:
```
======================================
   MINI GT 1:64 Scale Catalog Panel   
======================================
  1. Scrape / Update Catalog
  2. Resume Interrupted Scrape
  3. Deploy to GitHub Pages
  4. Start Preview Server
  5. Clear All Data
  6. Exit
======================================
```
* **Option 1**: Performs a fresh crawl, deduplicates the database, compiles the JSON, and deploys it to GitHub Pages.
* **Option 4**: Boots a local HTTP preview server at `http://127.0.0.1:8000` to test the website locally.
* **Option 5**: Safely wipes the SQLite database, JSON, and logs.
