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
    Base, MiniGTProduct, HotWheelsProduct, PopRaceProduct,
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
    Identifies duplicate models based strictly on the exact item number.
    Retains only the record from the highest-priority source:
    1. official
    2. myminigt
    3. fandom
    Deletes lower-priority duplicates from the SQLite database as-is, without merging images or metadata.
    For cancelled/discontinued models:
      - Do NOT use images from the official MINI GT website.
      - Prefer images from myminigt.com, and fallback to fandom.
      - If none available, clear images.
    """
    logger.info("Starting database deduplication process...")
    try:
        from database.classify import is_cancelled_product

        with get_db_session() as session:
            products = session.query(MiniGTProduct).all()
            if not products:
                return

            def get_base_code(item_num: str) -> str:
                clean = item_num.strip().upper()
                match = re.match(r"^([A-Z0-9]+?)[\s-]*([RL])$", clean)
                if match:
                    base = match.group(1)
                    if base and base[-1].isdigit():
                        return base
                return clean

            # Group products by duplicate base code
            groups = {}
            for p in products:
                base = get_base_code(p.item_number)
                groups.setdefault(base, []).append(p)

            prio_map = {"official": 1, "myminigt": 2, "fandom": 3}

            for base, group_list in groups.items():
                # Determine if any record in the group indicates the product is cancelled
                has_cancelled = any(is_cancelled_product(p.product_name, p.series, p.status) for p in group_list)

                # Sort by source priority
                group_list.sort(key=lambda p: (prio_map.get((p.source or "").lower(), 9), len(p.item_number)))
                
                winner = group_list[0]
                losers = group_list[1:]

                # Copy release year and confidence if winner has none
                if winner.release_year is None:
                    year_record = next((p for p in group_list if p.release_year is not None), None)
                    if year_record:
                        winner.release_year = year_record.release_year
                        winner.release_year_confidence = year_record.release_year_confidence

                # Copy status if winner has generic but duplicate has specific
                specific_status = next((p.status for p in group_list if p.status and p.status.lower() not in ("released", "none")), None)
                if specific_status and (not winner.status or winner.status.lower() == "released"):
                    winner.status = specific_status

                # Update winner is_cancelled flag if anyone in the group was cancelled
                if has_cancelled:
                    winner.is_cancelled = True
                    if winner.toy_brand != "MINI GT" and winner.toy_brand != "Pop Race":
                        if not winner.status or winner.status.lower() == "released":
                            winner.status = "Cancelled"

                # Apply special cancelled model image rules
                if winner.is_cancelled:
                    new_images = []
                    img_src = None
                    
                    # 1. Prefer myminigt
                    myminigt_p = next((p for p in group_list if (p.source or "").lower() == "myminigt"), None)
                    if myminigt_p and myminigt_p.image_list:
                        new_images = myminigt_p.image_list
                        img_src = "myminigt"
                    else:
                        # 2. Fall back to fandom
                        fandom_p = next((p for p in group_list if (p.source or "").lower() == "fandom"), None)
                        if fandom_p and fandom_p.image_list:
                            new_images = fandom_p.image_list
                            img_src = "fandom"
                            
                    winner.set_images(new_images)
                    logger.info(f"Set cancelled/discontinued model {winner.item_number} images from {img_src or 'None'} (Official images ignored)")

                if len(group_list) > 1:
                    logger.info(
                        f"Deduplicating {base}: Keeping {winner.item_number} (Source: {winner.source}), "
                        f"discarding {len(losers)} duplicate records."
                    )
                    # Delete duplicate records without merging images or metadata
                    for loser in losers:
                        session.delete(loser)

            session.commit()
        logger.info("Database deduplication complete.")
    except Exception as e:
        logger.error(f"Error during database deduplication: {e}")


def sync_to_json() -> None:
    """Dumps SQLite records to brand-specific products JSON files."""
    try:
        deduplicate_database()
        
        minigt_data = []
        hotwheels_data = []
        poprace_data = []
        tarmacworks_data = []
        inno64_data = []
        trendshobby_data = []
        
        with get_db_session() as session:
            minigt_prods = session.query(MiniGTProduct).all()
            hotwheels_prods = session.query(HotWheelsProduct).all()
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
                d["year"] = str(p.release_year) if p.release_year and p.release_year_confidence == "confirmed" else None
                d = classify_product(d, p.toy_brand)
                minigt_data.append(d)
                
            for p in hotwheels_prods:
                d = p.to_dict()
                m_primary, m_list = get_manufacturers(p.product_name, p.brand, p.series or "Regular")
                d["manufacturer"] = m_primary
                d["year"] = str(p.release_year) if p.release_year and p.release_year_confidence == "confirmed" else None
                d = classify_product(d, p.toy_brand)
                hotwheels_data.append(d)
                
            for p in poprace_prods:
                d = p.to_dict()
                m_primary, m_list = get_manufacturers(p.product_name, p.brand, p.series or "Regular")
                d["manufacturer"] = m_primary
                d["year"] = str(p.release_year) if p.release_year and p.release_year_confidence == "confirmed" else None
                d = classify_product(d, p.toy_brand)
                poprace_data.append(d)

            for p in tarmacworks_prods:
                d = p.to_dict()
                m_primary, m_list = get_manufacturers(p.product_name, p.brand, p.series or "Regular")
                d["manufacturer"] = m_primary
                d["year"] = str(p.release_year) if p.release_year and p.release_year_confidence == "confirmed" else None
                d = classify_product(d, p.toy_brand)
                tarmacworks_data.append(d)

            for p in inno64_prods:
                d = p.to_dict()
                m_primary, m_list = get_manufacturers(p.product_name, p.brand, p.series or "Regular")
                d["manufacturer"] = m_primary
                d["year"] = str(p.release_year) if p.release_year and p.release_year_confidence == "confirmed" else None
                d = classify_product(d, p.toy_brand)
                inno64_data.append(d)

            for p in trendshobby_prods:
                d = p.to_dict()
                m_primary, m_list = get_manufacturers(p.product_name, p.brand, p.series or "Regular")
                d["manufacturer"] = m_primary
                d["year"] = str(p.release_year) if p.release_year and p.release_year_confidence == "confirmed" else None
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
            
            # Hot Wheels sorting: release_year desc (nulls last), then series, then item_number
            def hotwheels_sort_key(p_dict):
                year_val = p_dict.get("release_year")
                # Python doesn't support comparing None to int. We map None to 0 for desc sort.
                year_num = year_val if year_val is not None else 0
                return (-year_num, p_dict.get("series", "") or "", p_dict.get("item_number", "") or "")
                
            hotwheels_data.sort(key=hotwheels_sort_key)
            
            # Pop Race sorting: release_year desc (nulls last), then item_number
            def poprace_sort_key(p_dict):
                year_val = p_dict.get("release_year")
                year_num = year_val if year_val is not None else 0
                return (-year_num, p_dict.get("item_number", "") or "")
                
            poprace_data.sort(key=poprace_sort_key)

            # Tarmac Works sorting: release_year desc (nulls last), then item_number
            def tarmacworks_sort_key(p_dict):
                year_val = p_dict.get("release_year")
                year_num = year_val if year_val is not None else 0
                return (-year_num, p_dict.get("item_number", "") or "")

            tarmacworks_data.sort(key=tarmacworks_sort_key)

            # INNO64 sorting: release_year desc (nulls last), then item_number
            def inno64_sort_key(p_dict):
                year_val = p_dict.get("release_year")
                year_num = year_val if year_val is not None else 0
                return (-year_num, p_dict.get("item_number", "") or "")

            inno64_data.sort(key=inno64_sort_key)

            # Trends Hobby sorting: item_number
            trendshobby_data.sort(key=lambda p_dict: p_dict.get("item_number", "") or "")
            
        # Write files
        minigt_path = os.path.join(DB_DIR, "products_minigt.json")
        hotwheels_path = os.path.join(DB_DIR, "products_hotwheels.json")
        poprace_path = os.path.join(DB_DIR, "products_poprace.json")
        tarmacworks_path = os.path.join(DB_DIR, "products_tarmacworks.json")
        inno64_path = os.path.join(DB_DIR, "products_inno64.json")
        trendshobby_path = os.path.join(DB_DIR, "products_trendshobby.json")
        
        with open(minigt_path, "w", encoding="utf-8") as f:
            json.dump(minigt_data, f, indent=2, ensure_ascii=False)
        with open(hotwheels_path, "w", encoding="utf-8") as f:
            json.dump(hotwheels_data, f, indent=2, ensure_ascii=False)
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
            f"Hot Wheels ({len(hotwheels_data)}), Pop Race ({len(poprace_data)}), "
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
        os.path.join(DB_DIR, "products_hotwheels.json"),
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
                    "Hot Wheels": ["hotwheels.fandom.com"],
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
