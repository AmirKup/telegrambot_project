import pandas as pd
import sqlite3
import math

EXCEL_PATH = "tumi_excel_stock_updated_red.xlsx"  # твой Excel с новым "Описание"
DB_PATH = "products.db"

def is_empty(x):
    if x is None: return True
    if isinstance(x, float) and math.isnan(x): return True
    if isinstance(x, str) and x.strip() == "": return True
    return False

def norm(x):
    return str(x).strip() if not is_empty(x) else None

def main():
    df = pd.read_excel(EXCEL_PATH)

    col_article = "Артикул"
    col_desc = "Описание"

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    updated = 0
    skipped = 0

    for _, row in df.iterrows():
        article = norm(row.get(col_article))
        desc = row.get(col_desc)

        if not article or is_empty(desc):
            skipped += 1
            continue

        cur.execute(
            "UPDATE products SET description = ? WHERE article = ?",
            (str(desc), article)
        )
        if cur.rowcount > 0:
            updated += 1
        else:
            skipped += 1

    conn.commit()
    conn.close()
    print(f"✅ Обновлено описаний: {updated}")
    print(f"⚠️ Пропущено строк: {skipped}")

if __name__ == "__main__":
    main()