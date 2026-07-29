# MINI GT 1:64 Scale Catalog System

A senior-grade Python full-stack application designed to crawl, maintain, and display a local offline database of MINI GT 1:64 scale product releases from the official website. The system supports advanced searching, filtering, and printing custom, high-resolution catalogs (resembling the official MINI GT grid style) in both PDF and PNG formats.

---

## Key Features

1. **Robust, Resilient Web Crawler**:
   - Discovers all brand listings under the MINI GT Products index.
   - Crawls each brand page-by-page concurrently using `ThreadPoolExecutor`.
   - Filters and saves **only** 1:64 scale models, ignoring duplicate Item Numbers.
   - Respects robots.txt configurations and implements rate-limiting and retry logic.
   - Features **session resumption**: state is saved in `cache/crawler_state.json` to safely resume if interrupted.
   - Logs comprehensive crawler activity to `logs/crawler.log`.

2. **Local Offline Storage & Sync**:
   - Store all data locally under a unified database layout.
   - Uses **SQLite** (`database/products.db`) as the primary database managed with **SQLAlchemy**.
   - Syncs all products automatically to `database/products.json` after successful crawl actions.

3. **Premium Web Application (FastAPI + Bootstrap 5 + HTMX)**:
   - Responsive grid UI utilizing glassmorphism cards and smooth micro-animations.
   - **Theme Manager**: Built-in toggle for sleek Dark Mode and Clean Light Mode.
   - **Instant Search**: Type item numbers or names and view results instantly using HTMX.
   - **Selection Checklist**: Select custom products using card checkboxes to generate personalized catalogs.
   - **Real-Time Crawling Console**: Open the "Update DB" modal, select Incremental or Full mode, and watch live progress and terminal log output scroll in real-time.

4. **Printable Catalog Generator (Pillow)**:
   - Export grids matching the official MINI GT catalog aesthetics (white background, thin light-grey border around cards, centered vehicle images, metadata positioned nicely, and automatic wrapping for long model names).
   - Dynamically loads and caches the modern typography font **Inter** from Google Fonts.
   - Fetches and caches remote images on-the-fly to accelerate rendering.
   - Supports 2, 3, 4, 5, or 6 column grid layouts.
   - Outputs multi-page high-resolution **PDF** books or a packaged **ZIP** of high-res PNG pages.

---

## Project Structure

```
MINIGT/
│
├── api/
│   └── routes.py             # FastAPI routing and controller logic
│
├── cache/
│   ├── image_cache/          # Cached remote product images for catalog generation
│   └── Inter-Regular.ttf     # Cached TrueType typography font
│
├── crawler/
│   └── crawler.py            # Concurrent BeautifulSoup crawler logic
│
├── database/
│   ├── db_manager.py         # SQLite connection session & JSON sync script
│   ├── models.py             # SQLAlchemy schema for Product releases
│   ├── products.db           # SQLite database file (gitignored)
│   └── products.json         # JSON copy of database
│
├── exports/                  # Generated PDF/PNG catalogs (gitignored)
├── logs/                     # Application logs (app.log & crawler.log)
├── static/
│   └── styles.css            # Responsive styles and dark/light themes
│
├── templates/
│   ├── base.html             # Main skeleton layout
│   ├── index.html            # Search filters and product grid dashboard
│   ├── product_detail.html   # Model spec sheet detail page
│   └── product_grid_items.html # HTMX infinite-scroller rows
│
├── tests/
│   └── test_system.py        # Pytest unit tests for parsing and schemas
│
├── app.py                    # Server startup script
├── requirements.txt          # Python package requirements
└── README.md                 # Project documentation
```

---

## Setup & Running the Application

### 1. Prerequisites
Make sure Python 3.13+ is installed.

### 2. Install Dependencies
Run the command below in your shell to install required dependencies:
```powershell
pip install -r requirements.txt
```

### 3. Run Pytest Suite
Run unit tests to verify HTML parsers and models:
```powershell
py -m pytest
```

### 4. Start the Application
Boot the FastAPI application:
```powershell
python app.py
```
- The server initializes SQLite schema tables.
- It will automatically launch your default web browser to: `http://127.0.0.1:8000`
- Click the **Update DB** button in the navbar to start your first crawler session and populate your catalog!
