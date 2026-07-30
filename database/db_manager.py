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
from database.models import Base, Product

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


def init_db() -> None:
    """Creates database tables and indexes if they do not exist, migrating columns if needed."""
    try:
        inspector = inspect(engine)
        if "products" in inspector.get_table_names():
            existing_cols = [c["name"] for c in inspector.get_columns("products")]
            cols_to_add = {
                "release_year": "INTEGER",
                "release_year_confidence": "VARCHAR",
                "status": "VARCHAR",
                "is_cancelled": "BOOLEAN DEFAULT 0"
            }
            for col_name, col_type in cols_to_add.items():
                if col_name not in existing_cols:
                    logger.info(f"Migrating products table: adding {col_name} column...")
                    with engine.begin() as conn:
                        conn.execute(text(f"ALTER TABLE products ADD COLUMN {col_name} {col_type}"))

        Base.metadata.create_all(bind=engine)
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
            products = session.query(Product).all()
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
    """Dumps SQLite records to products.json with preserved OEM item numbers and sorted by custom groups."""
    try:
        deduplicate_database()
        with get_db_session() as session:
            products = session.query(Product).all()

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

            def sort_key(p):
                item = (p.item_number or "").strip()
                
                # Group 5: Malformed or outlier entries (at the very bottom)
                if is_abnormal(item):
                    return (5, item, 0, "")
                    
                # Group 3: OEM models (sorted numerically on the fly, keeping original text)
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
                    
                # Group 1 & 2: Standard MGT and KHMG models
                match = re.match(r"^([a-zA-Z]+)(\d+)", item)
                if not match:
                    # Group 4: Remaining non-standard normal items without standard prefix+digits
                    return (4, item, 0, "")
                    
                prefix, num_str = match.groups()
                num = int(num_str)
                prefix_upper = prefix.upper()
                
                if prefix_upper == "MGT":
                    return (1, num, 0, "")
                if prefix_upper == "KHMG":
                    return (2, num, 0, "")
                    
                # Group 4: Remaining normal models (sorted naturally by prefix, then number)
                return (4, prefix_upper, num, "")

            products.sort(key=sort_key)
            
            # Export data, classifying and preserving OEM numbers
            data = []
            for p in products:
                d = p.to_dict()
                
                # Apply deterministic classification rules
                from database.classify import (
                    get_manufacturers, get_category, get_collaboration,
                    get_theme, get_body_style, get_region
                )
                
                m_primary, m_list = get_manufacturers(p.product_name, p.brand, p.series or "Regular")
                
                d["manufacturer"] = m_primary
                d["set_manufacturers"] = m_list
                d["category"] = get_category(p.item_number, p.brand, p.product_name, p.series or "Regular", bool(p.is_cancelled))
                d["collaboration"] = get_collaboration(p.product_name, p.brand, p.series or "Regular", d["category"])
                d["theme"] = get_theme(p.product_name, p.brand, p.series or "Regular")
                d["region"] = get_region(p.item_number, p.product_name, p.brand, p.series or "Regular")
                d["body_style"] = get_body_style(p.product_name, d["category"])
                d["year"] = str(p.release_year) if p.release_year and p.release_year_confidence == "confirmed" else None
                
                data.append(d)

        with open(JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        logger.info(f"Synchronized {len(data)} products to {JSON_PATH}")
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
            all_products = session.query(Product).all()
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

    # 2. Delete JSON file
    if os.path.exists(JSON_PATH):
        try:
            os.remove(JSON_PATH)
            print("[x] Removed JSON database products.json")
        except Exception as e:
            print(f"[ERROR] Failed to remove products.json: {e}")

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
