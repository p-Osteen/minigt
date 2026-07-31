import os
import sqlite3
import logging

logger = logging.getLogger("migration")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "products.db")

def run_migration():
    if not os.path.exists(DB_PATH):
        logger.info(f"Database file {DB_PATH} does not exist. No migration needed.")
        return

    logger.info(f"Checking schema migration for {DB_PATH}...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        # Check if table products exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='products'")
        if not cursor.fetchone():
            logger.info("Table 'products' does not exist yet. No migration needed.")
            conn.close()
            return

        # Check columns of products table
        cursor.execute("PRAGMA table_info(products)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if "toy_brand" in columns:
            logger.info("Database schema is already up to date (toy_brand column exists).")
            conn.close()
            return
            
        logger.info("Migrating products table schema: adding composite primary key with toy_brand namespace...")
        
        # 1. Begin transaction
        cursor.execute("BEGIN TRANSACTION")
        
        # 2. Rename existing table
        cursor.execute("ALTER TABLE products RENAME TO products_old")
        
        # 3. Create new products table with composite primary key
        cursor.execute("""
            CREATE TABLE products (
                toy_brand VARCHAR NOT NULL DEFAULT 'MINI GT',
                item_number VARCHAR NOT NULL,
                product_name VARCHAR NOT NULL,
                brand VARCHAR NOT NULL,
                scale VARCHAR NOT NULL DEFAULT '1:64',
                series VARCHAR,
                images TEXT,
                source VARCHAR,
                release_year INTEGER,
                release_year_confidence VARCHAR,
                status VARCHAR,
                is_cancelled BOOLEAN DEFAULT 0,
                PRIMARY KEY (toy_brand, item_number)
            )
        """)
        
        # 4. Copy data from products_old to products, setting toy_brand default to 'MINI GT'
        cursor.execute("""
            INSERT INTO products (
                toy_brand, item_number, product_name, brand, scale, series, images,
                source, release_year, release_year_confidence, status, is_cancelled
            )
            SELECT 
                'MINI GT', item_number, product_name, brand, scale, series, images,
                source, release_year, release_year_confidence, status, is_cancelled
            FROM products_old
        """)
        
        # 5. Drop old table
        cursor.execute("DROP TABLE products_old")
        
        # 6. Recreate indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_products_toy_brand ON products (toy_brand)")
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_products_item_number ON products (item_number)")
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_products_product_name ON products (product_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_products_brand ON products (brand)")
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_products_scale ON products (scale)")
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_products_series ON products (series)")
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_products_source ON products (source)")
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_products_release_year ON products (release_year)")
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_products_is_cancelled ON products (is_cancelled)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_brand_series ON products (brand, series)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_brand_scale ON products (brand, scale)")
        
        conn.commit()
        logger.info("Database schema migration completed successfully.")
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Migration failed: {e}")
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_migration()
