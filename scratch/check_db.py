import sqlite3
import json

conn = sqlite3.connect('database/products.db')
cursor = conn.cursor()

# Get MGT00001
cursor.execute("SELECT item_number, product_name, images, source FROM products WHERE item_number = 'MGT00001'")
row = cursor.fetchone()
if row:
    print("MGT00001 images:", json.loads(row[2]))
    print("MGT00001 source:", row[3])
else:
    print("MGT00001 not found")

# Get MGT00001R
cursor.execute("SELECT item_number, product_name, images, source FROM products WHERE item_number = 'MGT00001R'")
row2 = cursor.fetchone()
if row2:
    print("MGT00001R images:", json.loads(row2[2]))
    print("MGT00001R source:", row2[3])

conn.close()
