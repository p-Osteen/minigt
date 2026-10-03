import os
import re
import json
import shutil
import logging
import sys
from contextlib import contextmanager
from typing import Generator
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker, Session
from database.models import (
    Base, MiniGTProduct, PopRaceProduct,
    TarmacWorksProduct, Inno64Product, TrendsHobbyProduct,
    get_product_model
)

DB_DIR = os.path.dirname(os.path.abspath(__file__))
os.makedirs(DB_DIR, exist_ok=True)

DB_PATH = os.path.join(DB_DIR, "products.db")
JSON_PATH = os.path.join(DB_DIR, "products.json")

DATABASE_URL = f"sqlite:///{DB_PATH}"

# Thread-safe connection pool with WAL mode for better concurrency
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    execution_options={"isolation_level": "SERIALIZABLE"},
)

# Enable WAL mode for better concurrent read/write performance
with engine.connect() as conn:
    conn.execute(text("PRAGMA journal_mode=WAL"))
    conn.execute(text("PRAGMA synchronous=NORMAL"))
    conn.execute(text("PRAGMA cache_size=-64000"))  # 64MB cache
    conn.commit()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

logger = logging.getLogger("db_manager")


def migrate_to_separate_tables() -> None:
    """Migrate unified products table into separate per-brand tables."""
    inspector = inspect(engine)
    table_names = inspector.get_table_names()
    
    # 1. Initialize the new tables if they don't exist
    Base.metadata.create_all(bind=engine)
    
    if "products" in table_names:
        logger.info("Found old 'products' table. Migrating data to brand-specific tables...")
        with get_db_session() as session:
            conn = session.connection()
            result = conn.execute(text("SELECT * FROM products"))
            columns = result.keys()
            rows = result.fetchall()
            
            migrated_count = 0
            for row in rows:
                p_dict = dict(zip(columns, row))
                toy_brand = p_dict.get("toy_brand", "MINI GT")
                model_cls = get_product_model(toy_brand)
                
                item_num = p_dict.get("item_number")
                existing = session.query(model_cls).filter(model_cls.item_number == item_num).first()
                if not existing:
                    new_item = model_cls(
                        item_number=item_num,
                        product_name=p_dict.get("product_name"),
                        brand=p_dict.get("brand"),
                        scale=p_dict.get("scale", "1:64"),
                        series=p_dict.get("series"),
                        sub_series=p_dict.get("sub_series") or "Regular",
                        images=p_dict.get("images"),
                        source=p_dict.get("source"),
                        release_year=p_dict.get("release_year"),
                        release_year_confidence=p_dict.get("release_year_confidence"),
                        status=p_dict.get("status"),
                        is_cancelled=bool(p_dict.get("is_cancelled", 0)),
                        toy_brand=toy_brand
                    )
                    session.add(new_item)
                    migrated_count += 1
            
            logger.info(f"Successfully migrated {migrated_count} records to brand tables.")
            
        # Drop the old products table
        logger.info("Dropping old 'products' table...")
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE products"))
        logger.info("Old 'products' table dropped successfully.")


def init_db() -> None:
    """Creates database tables and indexes if they do not exist, migrating columns if needed."""
    try:
        migrate_to_separate_tables()
        logger.info("SQLite database tables and indexes initialized.")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise


@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    """Provide a transactional scope around a series of operations."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"Database session rolled back: {e}")
        raise
    finally:
        session.close()


def deduplicate_database() -> None:
    """
    Performs comprehensive deduplication across all brands (MINI GT, Pop Race,
    Tarmac Works, INNO64, Trends Hobby).
    Handles:
    - Base SKU matches (stripping prefixes like TH-, MGT, PR64-, IN64- and punctuation).
    - Slug / dummy SKUs vs real manufacturer SKUs (e.g. TH-164PORSCHE911GT3REMA vs TH-241084K).
    - Identical primary image and title duplicates across different scraped sources.
    - Suffix variants (-L vs -R for MINI GT).
    Preserves all rich metadata and images onto the winning record before deleting duplicates.
    """
    logger.info("Starting multi-brand database deduplication process...")
    try:
        from database.classify import is_cancelled_product
        from database.models import BRAND_MODELS
        from collections import defaultdict

        def clean_sku(s: str) -> str:
            if not s: return ""
            return re.sub(r'[\s\-_]', '', s.upper())

        def base_sku(s: str) -> str:
            cs = clean_sku(s)
            cs = re.sub(r'^(TH|PR64|PR|TW|IN64|IN|MGT)', '', cs)
            return cs

        def normalize_title(name: str) -> str:
            if not name: return ""
            s = name.lower()
            s = s.replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')
            s = re.sub(r'[\u2010-\u2015\-–—_]', ' ', s)
            s = re.sub(r'\blemans\b', 'le mans', s)
            s = re.sub(r'\b1\s*[/:]\s*64\b', ' ', s)
            s = re.sub(r'^(mini\s*gt|pop\s*race|tarmac\s*works|inno64|trends\s*hobby)\s+', '', s)
            s = re.sub(r'\bpick\s*up\b', 'pickup', s)
            s = re.sub(r'\brauh\s*welt\s*begriff\b', 'rwb', s)
            s = re.sub(r'(\d+)([a-zA-Z]+)', r'\1 \2', s)
            s = re.sub(r'([a-zA-Z]+)(\d+)', r'\1 \2', s)
            s = re.sub(r'[^a-z0-9\s]', ' ', s)
            stop = {'diecast', 'model', 'car', 'scale', 'edition', 'special', 'version', 'ver', 'the', 'w', 'pre', 'order', 'preorder'}
            tokens = [t for t in s.split() if t not in stop]
            return ' '.join(tokens)

        def get_primary_img(p) -> str:
            imgs = p.image_list
            if not imgs: return ""
            u = imgs[0].split('?')[0].split('#')[0]
            u = re.sub(r'/revision/.*$', '', u)
            u = re.sub(r'[-_]\d+x\d+(\.[a-zA-Z]+)$', r'\1', u)
            u = re.sub(r'_(pico|icon|thumb|small|compact|medium|large|grande|original|\d+x\d*|\d*x\d+)(\.[a-zA-Z]+)$', r'\2', u)
            fn = u.split('/')[-1].lower()
            if len(fn) < 6 or fn.startswith('placeholder') or fn == 'latest':
                return ""
            return fn

        def is_slug_or_dummy_sku(sku: str, brand: str) -> bool:
            sku = sku.upper()
            if brand == "Trends Hobby":
                if sku.startswith("TH-164") or sku.startswith("164SCALE") or "PORSCHE" in sku or "LAMBORGHINI" in sku or "2PCSSET" in sku:
                    return True
            if brand == "INNO64":
                if sku.startswith("DSC") or sku in ("A90MG", "MAGGTDRAWINPRO", "F40LBWHITE02"):
                    return True
            return False

        def sku_quality_score(sku: str, brand: str) -> int:
            if not sku: return 0
            sku = sku.strip()
            score = 100
            if brand == "Trends Hobby":
                if re.match(r'^TH-\d{5,}[A-Z]?$', sku):
                    score += 60
                elif re.match(r'^\d{5,}[A-Z]?$', sku):
                    score += 30
                elif sku.startswith("TH-"):
                    score += 20
                if "164" in sku or "PORSCHE" in sku or "LAMBORGHINI" in sku or len(sku) > 15:
                    score -= 50
            elif brand == "INNO64":
                if re.match(r'^IN64-?[A-Z0-9]+', sku):
                    score += 50
                if sku.startswith("DSC") or "X1080" in sku:
                    score -= 60
            elif brand == "MINI GT":
                if sku.startswith("MGTS"):
                    score += 30
                elif sku.startswith("MGT"):
                    score += 20
                elif sku.startswith("KHMG"):
                    score += 20
            elif brand == "Pop Race":
                if re.match(r'^PR64\d{4,}$', sku):
                    score += 50
                elif sku.startswith("PR64"):
                    score += 30
                elif sku.startswith("S") and len(sku) <= 6:
                    score -= 30
            return score

        def check_duplicate(p1, p2, brand_name: str):
            s1 = (p1.scale or "1:64").strip()
            s2 = (p2.scale or "1:64").strip()
            if s1 != s2:
                return False, None
            if (p1.item_number.startswith('T64') and p2.item_number.startswith('T43')) or \
               (p1.item_number.startswith('T43') and p2.item_number.startswith('T64')):
                return False, None

            t1 = normalize_title(p1.product_name)
            t2 = normalize_title(p2.product_name)

            if ('kids' in t1 and 'kids' not in t2) or ('kids' in t2 and 'kids' not in t1):
                return False, None

            bs1 = base_sku(p1.item_number)
            bs2 = base_sku(p2.item_number)
            img1 = get_primary_img(p1)
            img2 = get_primary_img(p2)

            # Condition 1: Same base SKU
            if bs1 and bs2 and bs1 == bs2 and len(bs1) >= 4 and not bs1.startswith('164'):
                if t1 == t2 or p1.brand == p2.brand or t1 in t2 or t2 in t1:
                    return True, f"Base SKU match: '{bs1}'"

            # Condition 2: Same primary image
            if img1 and img2 and img1 == img2:
                if t1 == t2:
                    return True, f"Identical Image & Title: '{t1[:30]}'"
                w1, w2 = set(t1.split()), set(t2.split())
                shared = w1 & w2
                if (t1 in t2 or t2 in t1 or len(shared) >= 3):
                    models = {'skyline', 'laurel', 'civic', 'integrale', 'supra', 'rx7', 'chaser', 'fairlady', 'nsx'}
                    models1 = w1 & models
                    models2 = w2 & models
                    if not (models1 and models2 and models1 != models2):
                        return True, f"Identical Image & Compatible Vehicle: '{img1}'"

            # Condition 3: One is a slug/dummy SKU and other is real SKU with matching title
            is_dummy1 = is_slug_or_dummy_sku(p1.item_number, brand_name)
            is_dummy2 = is_slug_or_dummy_sku(p2.item_number, brand_name)
            if (is_dummy1 or is_dummy2) and not (is_dummy1 and is_dummy2):
                if t1 and t2 and (t1 == t2 or (t1 in t2 and len(t1) >= 15) or (t2 in t1 and len(t2) >= 15)):
                    return True, f"Slug/Dummy SKU with matching Title: '{t1[:30]}'"

            # Condition 4: MINI GT MGT prefix match & L/R
            if brand_name == "MINI GT":
                c1, c2 = clean_sku(p1.item_number), clean_sku(p2.item_number)
                if (c1 == 'MGT' + c2 or c2 == 'MGT' + c1) and (t1 == t2 or t1 in t2 or t2 in t1):
                    return True, f"MINI GT Prefix match: '{c1}' vs '{c2}'"
                m1 = re.match(r"^([A-Z0-9]+?)[\s-]*([RL])$", p1.item_number.strip().upper())
                m2 = re.match(r"^([A-Z0-9]+?)[\s-]*([RL])$", p2.item_number.strip().upper())
                if m1 and m2 and m1.group(1) == m2.group(1):
                    return True, f"MINI GT L/R Base Code: '{m1.group(1)}'"

            # Condition 5: Exact normalized title match in Trends Hobby (different stores)
            if brand_name == "Trends Hobby":
                if t1 and t2 and t1 == t2 and len(t1) >= 15:
                    return True, f"Trends Hobby identical title: '{t1[:30]}'"

            return False, None

        total_deleted = 0
        prio_map = {"official": 1, "shopify": 2, "myminigt": 3, "fandom": 4}

        with get_db_session() as session:
            for brand_name, model_cls in BRAND_MODELS.items():
                products = session.query(model_cls).all()
                if not products:
                    continue

                parent = {p.item_number: p.item_number for p in products}
                prod_map = {p.item_number: p for p in products}

                def find(x):
                    if parent[x] != x:
                        parent[x] = find(parent[x])
                    return parent[x]

                def union(x, y):
                    rx, ry = find(x), find(y)
                    if rx != ry:
                        parent[rx] = ry

                candidates = set()
                by_bs = defaultdict(list)
                by_img = defaultdict(list)
                by_title = defaultdict(list)
                by_mgt_base = defaultdict(list)

                for p in products:
                    bs = base_sku(p.item_number)
                    if bs and len(bs) >= 4 and not bs.startswith('164'):
                        by_bs[bs].append(p.item_number)
                    img = get_primary_img(p)
                    if img:
                        by_img[img].append(p.item_number)
                    t = normalize_title(p.product_name)
                    if len(t) >= 12:
                        by_title[t].append(p.item_number)
                    if brand_name == "MINI GT":
                        m = re.match(r"^([A-Z0-9]+?)[\s-]*([RL])$", p.item_number.strip().upper())
                        if m and m.group(1) and m.group(1)[-1].isdigit():
                            by_mgt_base[m.group(1)].append(p.item_number)

                for lst in by_bs.values():
                    for i in range(len(lst)):
                        for j in range(i+1, len(lst)): candidates.add((lst[i], lst[j]))
                for lst in by_img.values():
                    for i in range(len(lst)):
                        for j in range(i+1, len(lst)): candidates.add((lst[i], lst[j]))
                for lst in by_title.values():
                    for i in range(len(lst)):
                        for j in range(i+1, len(lst)): candidates.add((lst[i], lst[j]))
                for lst in by_mgt_base.values():
                    for i in range(len(lst)):
                        for j in range(i+1, len(lst)): candidates.add((lst[i], lst[j]))

                for itm1, itm2 in candidates:
                    p1, p2 = prod_map[itm1], prod_map[itm2]
                    is_dup, reason = check_duplicate(p1, p2, brand_name)
                    if is_dup:
                        union(itm1, itm2)

                EXPLICIT_MERGES = {
                    "Trends Hobby": [
                        ('TH-241084', 'TH-241084F'),
                        ('TH-A011002', 'TH-241084C'),
                        ('TH-A012013', 'TH-241082A'),
                        ('TH-PORSCHE911GT3RBLACKA', 'TH-A018007'),
                        ('TH-PORSCHE911GT1LM1998H', 'TH-241085B'),
                        ('TH-PORSCHE911GT3RBATHUR', 'TH-241084G'),
                        ('TH-B008028', 'TH-241083C'),
                        ('TH-PORSCHECAYENNETURBOG', 'TH-241090C'),
                        ('TH-MCLARENSENNAGTRCHAME', 'TH-A009008'),
                        ('TH-LAMBORGHINIHURACAN', 'TH-241083D'),
                        ('TH-164PORSCHE911GT3RPUR', 'TH-241084L'),
                        ('TH-164LAMBORGHINIHURACA', 'TH-241083F'),
                    ]
                }
                if brand_name in EXPLICIT_MERGES:
                    for s1, s2 in EXPLICIT_MERGES[brand_name]:
                        if s1 in prod_map and s2 in prod_map:
                            union(s1, s2)

                clusters = defaultdict(list)
                for itm in parent:
                    root = find(itm)
                    clusters[root].append(prod_map[itm])

                dup_clusters = {k: v for k, v in clusters.items() if len(v) > 1}
                brand_deleted = 0

                TITLE_FIXES = {
                    'TH-241084F': 'Porsche 911 GT3 R DTM 2025 #91 - Yellow',
                    'TH-241084C': 'Porsche 911 GT3 R DTM 2024 #91 - Yellow',
                    'TH-241082A': 'Lamborghini Temerario - Matte Blue',
                    'TH-A018007': 'Porsche 911 GT3 R APA XPO 2025 Limited Edition - Black',
                    'TH-241085B': 'Porsche 911 GT1 LM 1998 (Hong Kong Toy Car Salon 2025)',
                    'TH-241084G': 'Porsche 911 GT3 R #911 Bathurst 12 Hour 2026 (GT Show China)',
                    'TH-241083C': 'Lamborghini Huracan GT3 EVO2 #6 SSR LetsCall DTM 2023 (TMCS 2026 Exclusive)',
                    'TH-PORSCHECAYENNETURBOG': 'Porsche Cayenne Turbo GT Weissach - Blue (TMCS Singapore 2025)',
                    'TH-241090C': 'Porsche Cayenne Turbo GT Weissach - Blue (TMCS Singapore 2025)',
                    'TH-A009008': 'McLaren Senna GTR Chameleon Purple (HEC 2025 Exclusive)',
                    'TH-241083D': 'Lamborghini Huracan GT3 EVO2 #63 Absolute Racing Ultraman - China GT 2026',
                    'TH-241084L': 'Porsche 911 GT3 R Pure Rxcing #92 - Le Mans 24H 2024 LMGT3 - White',
                    'TH-241083F': 'Lamborghini Huracan GT3 EVO2 GRT #63 DTM 2025 - Black / Gold',
                    'TH-C002018': 'Lamborghini Urus Performante & Temerario tokidoki 2-Car Set (HKTS 2026)',
                    'TH-241094A': 'Lamborghini Urus Performante x Jean-Michel Basquiat - White Livery',
                    'TH-241085E': 'Porsche 911 GT1 1998 - Silver',
                }

                for root, group_list in dup_clusters.items():
                    # Sort to find the winner
                    group_list.sort(key=lambda p: (
                        sku_quality_score(p.item_number, brand_name),
                        1 if p.release_year else 0,
                        len(p.image_list),
                        -prio_map.get((p.source or "").lower(), 9),
                        -len(p.item_number)
                    ), reverse=True)

                    winner = group_list[0]
                    losers = group_list[1:]

                    # 1. Merge release year
                    if winner.release_year is None:
                        yr_rec = next((p for p in losers if p.release_year is not None), None)
                        if yr_rec:
                            winner.release_year = yr_rec.release_year
                            winner.release_year_confidence = yr_rec.release_year_confidence

                    # 2. Merge images (combine unique images)
                    winner_imgs = list(winner.image_list)
                    seen_imgs = set(winner_imgs)
                    for l in losers:
                        for img in l.image_list:
                            if img and img not in seen_imgs:
                                winner_imgs.append(img)
                                seen_imgs.add(img)
                    winner.set_images(winner_imgs)

                    # 3. Merge status
                    spec_status = next((p.status for p in losers if p.status and p.status.lower() not in ("released", "none")), None)
                    if spec_status and (not winner.status or winner.status.lower() == "released"):
                        winner.status = spec_status

                    # 4. Check cancellation
                    has_cancelled = any(is_cancelled_product(p.product_name, p.series, p.status) for p in group_list)
                    if has_cancelled:
                        winner.is_cancelled = True
                        if winner.toy_brand not in ("MINI GT", "Pop Race"):
                            if not winner.status or winner.status.lower() == "released":
                                winner.status = "Cancelled"

                    for loser in losers:
                        session.delete(loser)
                        brand_deleted += 1

                # Clean up titles, typos, and (Copy) across all products of this brand
                for p in products:
                    if p.item_number in TITLE_FIXES:
                        p.product_name = TITLE_FIXES[p.item_number]
                    elif p.product_name:
                        new_name = p.product_name
                        new_name = re.sub(r'\s*\(Copy\)\s*', '', new_name, flags=re.IGNORECASE)
                        new_name = re.sub(r'\bSliver\b', 'Silver', new_name, flags=re.IGNORECASE)
                        new_name = re.sub(r'\bHurancan\b', 'Huracan', new_name, flags=re.IGNORECASE)
                        new_name = re.sub(r'\bWeissech\b', 'Weissach', new_name, flags=re.IGNORECASE)
                        if p.item_number == 'TH-C002018' or new_name.strip() == 'Car Set':
                            new_name = 'Lamborghini Urus Performante & Temerario tokidoki 2-Car Set (HKTS 2026)'
                        p.product_name = new_name.strip()

                total_deleted += brand_deleted
                if brand_deleted > 0:
                    logger.info(f"Deduplicated {brand_name}: Removed {brand_deleted} duplicate records.")

            session.commit()
        logger.info(f"Database deduplication complete. Total records removed across all brands: {total_deleted}")
    except Exception as e:
        logger.error(f"Error during database deduplication: {e}")


def sync_to_json() -> None:
    """Dumps SQLite records to brand-specific products JSON files."""
    try:
        deduplicate_database()
        
        minigt_data = []
        poprace_data = []
        tarmacworks_data = []
        inno64_data = []
        trendshobby_data = []
        
        with get_db_session() as session:
            minigt_prods = session.query(MiniGTProduct).all()
            poprace_prods = session.query(PopRaceProduct).all()
            tarmacworks_prods = session.query(TarmacWorksProduct).all()
            inno64_prods = session.query(Inno64Product).all()
            trendshobby_prods = session.query(TrendsHobbyProduct).all()
            
            # 1. Classification
            from database.classify import get_manufacturers, classify_product
            
            for p in minigt_prods:
                d = p.to_dict()
                m_primary, m_list = get_manufacturers(p.product_name, p.brand, p.series or "Regular")
                d["manufacturer"] = m_primary
                # Normalize N/A or missing scale to 1:64
                if not d.get("scale") or d["scale"] in ("N/A", "n/a", ""):
                    d["scale"] = "1:64"
                d["year"] = str(p.release_year) if p.release_year else None
                d = classify_product(d, p.toy_brand)
                minigt_data.append(d)
                
            for p in poprace_prods:
                d = p.to_dict()
                m_primary, m_list = get_manufacturers(p.product_name, p.brand, p.series or "Regular")
                d["manufacturer"] = m_primary
                year_val = p.release_year
                if not year_val:
                    for img in (p.images or []):
                        m_img = re.search(r'/uploads/(\d{4})/', img)
                        if m_img:
                            year_val = int(m_img.group(1))
                            break
                    if not year_val and p.item_number:
                        m_pr = re.match(r'^PR640*(\d+)$', p.item_number)
                        if m_pr:
                            num = int(m_pr.group(1))
                            if num <= 79:
                                year_val = 2023
                            elif num <= 220:
                                year_val = 2024
                            elif num <= 380:
                                year_val = 2025
                            elif num <= 520:
                                year_val = 2026
                            else:
                                year_val = 2027
                if year_val:
                    d["release_year"] = year_val
                d["year"] = str(year_val) if year_val else None
                d = classify_product(d, p.toy_brand)
                poprace_data.append(d)

            for p in tarmacworks_prods:
                d = p.to_dict()
                m_primary, m_list = get_manufacturers(p.product_name, p.brand, p.series or "Regular")
                d["manufacturer"] = m_primary
                d["year"] = str(p.release_year) if p.release_year else None
                d = classify_product(d, p.toy_brand)
                tarmacworks_data.append(d)

            for p in inno64_prods:
                d = p.to_dict()
                m_primary, m_list = get_manufacturers(p.product_name, p.brand, p.series or "Regular")
                d["manufacturer"] = m_primary
                d["year"] = str(p.release_year) if p.release_year else None
                d = classify_product(d, p.toy_brand)
                inno64_data.append(d)

            for p in trendshobby_prods:
                d = p.to_dict()
                m_primary, m_list = get_manufacturers(p.product_name, p.brand, p.series or "Regular")
                d["manufacturer"] = m_primary
                d["year"] = str(p.release_year) if p.release_year else None
                d = classify_product(d, p.toy_brand)
                trendshobby_data.append(d)
            
            # 2. Sorting
            # MINI GT sorting (existing custom sort_key logic)
            def is_abnormal(item: str) -> bool:
                if not item:
                    return True
                if "OEM" in item.upper():
                    return False
                if not any(c.isdigit() for c in item):
                    return True
                if len(item) > 15:
                    return True
                return False

            def minigt_sort_key(p_dict):
                item = (p_dict["item_number"] or "").strip()
                if is_abnormal(item):
                    return (5, item, 0, "")
                if "OEM" in item.upper():
                    match = re.match(r"^(\d+)?OEM([A-Z0-9]+)?$", item, re.IGNORECASE)
                    if match:
                        yy, nn = match.groups()
                        yy_num = int(yy) if yy and yy.isdigit() else 0
                        nn_str = nn if nn else ""
                        nn_num = int(nn_str) if nn_str and nn_str.isdigit() else 999999
                        return (3, yy_num, nn_num, nn_str)
                    else:
                        return (3, 0, 999999, item)
                match = re.match(r"^([a-zA-Z]+)(\d+)", item)
                if not match:
                    return (4, item, 0, "")
                prefix, num_str = match.groups()
                num = int(num_str)
                prefix_upper = prefix.upper()
                if prefix_upper == "MGT":
                    return (1, num, 0, "")
                if prefix_upper == "KHMG":
                    return (2, num, 0, "")
                return (4, prefix_upper, num, "")

            minigt_data.sort(key=minigt_sort_key)
            
            # Helper for natural alphanumeric sorting (PR640002 before PR640010, T64-001 before T64-010)
            def natural_sort_key(s: str):
                return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s or "")]

            # All non-MINI GT brands: oldest release date / model number / SKU first
            def brand_oldest_first_sort_key(p_dict):
                year_val = p_dict.get("release_year") or p_dict.get("year")
                try:
                    y = int(year_val) if year_val else 0
                except (ValueError, TypeError):
                    y = 0
                if y > 1900:
                    return (0, y, natural_sort_key(p_dict.get("item_number", "")))
                return (1, 9999, natural_sort_key(p_dict.get("item_number", "")))
                
            poprace_data.sort(key=brand_oldest_first_sort_key)
            tarmacworks_data.sort(key=brand_oldest_first_sort_key)
            inno64_data.sort(key=brand_oldest_first_sort_key)
            trendshobby_data.sort(key=brand_oldest_first_sort_key)
            
        # Write files
        minigt_path = os.path.join(DB_DIR, "products_minigt.json")
        poprace_path = os.path.join(DB_DIR, "products_poprace.json")
        tarmacworks_path = os.path.join(DB_DIR, "products_tarmacworks.json")
        inno64_path = os.path.join(DB_DIR, "products_inno64.json")
        trendshobby_path = os.path.join(DB_DIR, "products_trendshobby.json")
        
        with open(minigt_path, "w", encoding="utf-8") as f:
            json.dump(minigt_data, f, indent=2, ensure_ascii=False)
        with open(poprace_path, "w", encoding="utf-8") as f:
            json.dump(poprace_data, f, indent=2, ensure_ascii=False)
        with open(tarmacworks_path, "w", encoding="utf-8") as f:
            json.dump(tarmacworks_data, f, indent=2, ensure_ascii=False)
        with open(inno64_path, "w", encoding="utf-8") as f:
            json.dump(inno64_data, f, indent=2, ensure_ascii=False)
        with open(trendshobby_path, "w", encoding="utf-8") as f:
            json.dump(trendshobby_data, f, indent=2, ensure_ascii=False)
            
        # Compatibility backup
        with open(JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(minigt_data, f, indent=2, ensure_ascii=False)
            
        logger.info(
            f"Synchronized brand JSONs: MINI GT ({len(minigt_data)}), "
            f"Pop Race ({len(poprace_data)}), "
            f"Tarmac Works ({len(tarmacworks_data)}), INNO64 ({len(inno64_data)}), "
            f"Trends Hobby ({len(trendshobby_data)})"
        )
    except Exception as e:
        logger.error(f"Failed to synchronize database to JSON: {e}")


def rebuild_db_indexes() -> None:
    """Executes SQL REINDEX on the database to optimize index lookups."""
    try:
        with engine.connect() as conn:
            conn.execute(text("REINDEX"))
            conn.commit()
        logger.info("SQLite database indexes rebuilt successfully.")
        print("[SUCCESS] Database indexes rebuilt successfully.")
    except Exception as e:
        logger.error(f"Failed to rebuild indexes: {e}")
        print(f"[ERROR] Failed to rebuild indexes: {e}")


def purge_d_prefix_products() -> int:
    """
    Deletes all products whose item_number starts with 'D' (case-insensitive).
    Also regenerates products.json after purge.
    Returns the number of records deleted.
    """
    deleted_count = 0
    try:
        with get_db_session() as session:
            # Find all D-prefix products
            all_products = session.query(MiniGTProduct).all()
            d_items = [p for p in all_products if re.match(r'^D', p.item_number, re.IGNORECASE)]

            if not d_items:
                print("[INFO] No D-prefix products found in database.")
                return 0

            print(f"\n--- D-prefix Products Found ({len(d_items)}) ---")
            for p in d_items:
                print(f"  - {p.item_number}: {p.product_name}")
                session.delete(p)
                deleted_count += 1

        print(f"\n[SUCCESS] Deleted {deleted_count} D-prefix product(s).")

        # Regenerate JSON
        sync_to_json()
        print("[SUCCESS] products.json regenerated.")
        return deleted_count

    except Exception as e:
        logger.error(f"Failed to purge D-prefix products: {e}")
        print(f"[ERROR] Purge failed: {e}")
        return 0


def clear_all_data() -> None:
    """
    Completely removes database, caches, logs, and metadata files
    so the database can be built from scratch.
    """
    print("\n--- Clearing All Local Catalog Data ---")

    # Release database engine lock
    global engine
    try:
        engine.dispose()
    except Exception as e:
        logger.error(f"Failed to dispose engine: {e}")

    # Safely close all log handlers to release lock on crawler.log
    for logger_name in [None, "crawler", "db_manager"]:
        lg = logging.getLogger(logger_name)
        for handler in list(lg.handlers):
            try:
                handler.close()
                lg.removeHandler(handler)
            except Exception:
                pass

    # 1. Delete SQLite file
    if os.path.exists(DB_PATH):
        try:
            os.remove(DB_PATH)
            print("[x] Removed SQLite database products.db")
        except Exception as e:
            print(f"[ERROR] Failed to remove products.db: {e}")

    # 2. Delete JSON files
    json_files = [
        JSON_PATH,
        os.path.join(DB_DIR, "products_minigt.json"),
        os.path.join(DB_DIR, "products_poprace.json"),
        os.path.join(DB_DIR, "products_tarmacworks.json"),
        os.path.join(DB_DIR, "products_inno64.json"),
        os.path.join(DB_DIR, "products_trendshobby.json")
    ]
    for jp in json_files:
        if os.path.exists(jp):
            try:
                os.remove(jp)
                print(f"[x] Removed JSON database {os.path.basename(jp)}")
            except Exception as e:
                print(f"[ERROR] Failed to remove {os.path.basename(jp)}: {e}")

    # 3. Delete folders: images/, cache/, logs/, exports/
    workspace_root = os.path.dirname(DB_DIR)
    folders_to_delete = ["images", "cache", "logs", "exports"]

    for folder in folders_to_delete:
        path = os.path.join(workspace_root, folder)
        if os.path.exists(path):
            try:
                shutil.rmtree(path)
                print(f"[x] Removed folder: {folder}/")
            except Exception as e:
                print(f"[ERROR] Failed to remove folder {folder}/: {e}")

    # Re-create empty directory structure
    for folder in ["cache", "logs", "exports"]:
        os.makedirs(os.path.join(workspace_root, folder), exist_ok=True)
    os.makedirs(DB_DIR, exist_ok=True)

    # Re-setup logging
    root_logger = logging.getLogger()
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)
    try:
        fh = logging.FileHandler(os.path.join(workspace_root, "logs", "crawler.log"), encoding="utf-8")
        sh = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        fh.setFormatter(formatter)
        sh.setFormatter(formatter)
        root_logger.addHandler(fh)
        root_logger.addHandler(sh)
        root_logger.setLevel(logging.INFO)
    except Exception:
        pass

    # Re-initialize database with fresh engine
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.commit()
    SessionLocal.configure(bind=engine)
    init_db()
    print("[SUCCESS] All local data cleared and database re-initialized.")


def clear_brand_data(toy_brand: str) -> None:
    """
    Clears all records for a specific toy brand from its SQLite table,
    resets its crawler state, and regenerates the corresponding JSON export.
    """
    model_cls = get_product_model(toy_brand)
    print(f"\n--- Clearing Local Catalog Data for {toy_brand} ---")
    try:
        with get_db_session() as session:
            # Delete all rows from this brand's table
            deleted_count = session.query(model_cls).delete()
            logger.info(f"Cleared {deleted_count} records from {model_cls.__tablename__} table.")
            print(f"[x] Removed {deleted_count} records from database.")
        
        # Also clean crawler state crawled_urls for this brand
        workspace_root = os.path.dirname(DB_DIR)
        state_path = os.path.join(workspace_root, "cache", "crawler_state.json")
        if os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                patterns = {
                    "MINI GT": ["minigt.tsm-models.com", "myminigt.com", "minigt.fandom.com"],
                    "Pop Race": ["pop-race.fandom.com", "diecastsociety.com", "my64.com.my/usr/product.aspx?pgid=4&grpid=28"],
                    "Tarmac Works": ["tarmacworks.fandom.com", "tarmacworks.com"],
                    "INNO64": ["my64.com.my/usr/product.aspx?pgid=4&grpid=26"],
                    "Trends Hobby": ["treasuredmodels.com"]
                }
                brand_pats = patterns.get(toy_brand, [])
                crawled = state.get("crawled_urls", [])
                state["crawled_urls"] = [
                    u for u in crawled if not any(p in u for p in brand_pats)
                ]
                pending = state.get("pending_urls", [])
                state["pending_urls"] = [
                    t for t in pending if t.get("brand") != toy_brand
                ]
                with open(state_path, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2)
                logger.info(f"Cleared crawler state cached URLs for {toy_brand}.")
            except Exception as e:
                logger.error(f"Failed to clear crawler state for {toy_brand}: {e}")
        
        # Regenerate JSON files
        sync_to_json()
        print(f"[SUCCESS] JSON catalog for {toy_brand} regenerated.")
    except Exception as e:
        logger.error(f"Failed to clear data for {toy_brand}: {e}")
        print(f"[ERROR] Clear failed: {e}")
