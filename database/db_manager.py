import os
import re
import json
import shutil
import logging
import sys
from contextlib import contextmanager
from typing import Generator
from sqlalchemy import create_engine, text
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
    """Creates database tables and indexes if they do not exist."""
    try:
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


def sync_to_json() -> None:
    """Dumps all SQLite records to database/products.json."""
    try:
        with get_db_session() as session:
            products = session.query(Product).order_by(Product.item_number).all()
            data = [p.to_dict() for p in products]

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
