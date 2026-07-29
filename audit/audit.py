"""
MINI GT Catalog Source Audit & Verification Script.

Loads the local products.json and produces a detailed report:
  - D-prefix regression check
  - Missing images
  - Brand/scale breakdowns
  - Deduplication validation

Run: python audit/audit.py
Output: reports/audit_report.md
"""
import os
import re
import sys
import json
from datetime import datetime
from collections import Counter

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "reports")
JSON_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "database", "products.json")


def _md_table(headers: list, rows: list) -> str:
    """Build a Markdown table string."""
    sep = " | ".join(["---"] * len(headers))
    lines = ["| " + " | ".join(headers) + " |", "| " + sep + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)


def run_audit() -> str:
    os.makedirs(REPORTS_DIR, exist_ok=True)

    if not os.path.exists(JSON_PATH):
        print("[ERROR] database/products.json not found. Run a scrape first.")
        sys.exit(1)

    with open(JSON_PATH, encoding="utf-8") as f:
        products = json.load(f)

    print(f"\n=== MINI GT Catalog Audit ===")
    print(f"Total products loaded: {len(products)}")

    # --- D-prefix regression check ---
    d_items = [p for p in products if re.match(r"^D", p.get("item_number", ""), re.IGNORECASE)]
    print(f"D-prefix items (must be 0): {len(d_items)}")

    # --- Missing images ---
    no_img = [p for p in products if not p.get("images")]
    print(f"Products without images: {len(no_img)}")

    # --- Duplicate item numbers ---
    item_nums = [p["item_number"] for p in products]
    dup_counts = {k: v for k, v in Counter(item_nums).items() if v > 1}
    print(f"Duplicate item numbers: {len(dup_counts)}")

    # --- Scale distribution ---
    scale_dist = Counter(p.get("scale", "unknown") for p in products)

    # --- Brand distribution ---
    brand_dist = Counter(p.get("brand", "Unknown") for p in products)
    top_brands = brand_dist.most_common(20)

    # --- Image count distribution ---
    img_counts = Counter(len(p.get("images", [])) for p in products)

    # --- Build report ---
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    d_section = (
        "✅ **None found — D-prefix filter is working correctly.**"
        if not d_items
        else "⚠️ **D-prefix items still in database:**\n\n"
        + "\n".join(f"- `{p['item_number']}` — {p['product_name']}" for p in d_items)
    )

    no_img_section = (
        f"✅ **All {len(products)} products have at least one image.**"
        if not no_img
        else f"⚠️ **{len(no_img)} products have no images:**\n\n"
        + "\n".join(
            f"- `{p['item_number']}` — {p['product_name']} ({p.get('brand','')})"
            for p in no_img[:50]
        )
        + (f"\n\n_...and {len(no_img)-50} more_" if len(no_img) > 50 else "")
    )

    dup_section = (
        "✅ **No duplicate item numbers found.**"
        if not dup_counts
        else "⚠️ **Duplicate item numbers detected:**\n\n"
        + "\n".join(f"- `{k}` appears {v}×" for k, v in dup_counts.items())
    )

    report = f"""# MINI GT Catalog Audit Report
Generated: {ts}

---

## Summary

{_md_table(
    ["Metric", "Value"],
    [
        ["Total Products", len(products)],
        ["D-prefix Items (should be 0)", len(d_items)],
        ["Products Without Images", len(no_img)],
        ["Duplicate Item Numbers", len(dup_counts)],
        ["Scale Variants", len(scale_dist)],
        ["Distinct Brands", len(brand_dist)],
    ],
)}

---

## D-prefix Regression Check

{d_section}

---

## Products Without Images

{no_img_section}

---

## Duplicate Item Numbers

{dup_section}

---

## Scale Distribution

{_md_table(["Scale", "Count"], list(scale_dist.most_common()))}

---

## Top 20 Brands by Product Count

{_md_table(["Brand", "Count"], top_brands)}

---

## Image Count Distribution

{_md_table(["Images per Product", "Products"], sorted(img_counts.items()))}
"""

    report_path = os.path.join(REPORTS_DIR, "audit_report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\n[SUCCESS] Audit report saved → {report_path}")
    return report_path


if __name__ == "__main__":
    run_audit()
