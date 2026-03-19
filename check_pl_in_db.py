import sqlite3, re, os, sys

DB = "decision_db.db"   # ← 今あなたのフォルダにあるDB名

if not os.path.exists(DB):
    print("DB file not found:", DB)
    sys.exit(1)

con = sqlite3.connect(DB)
cur = con.cursor()

tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print("DB:", DB)
print("tables:", tables)

# 売上/営業利益を “列” で持っているテーブルを探す
sales_keys = ["sales","revenue","net_sales","売上","売上高"]
op_keys    = ["operating_profit","op","営業利益","operatingincome","operating_income"]

def norm(s):
    return re.sub(r"[^a-z0-9_]+","", str(s).lower())

sales_keys_n=[norm(x) for x in sales_keys]
op_keys_n   =[norm(x) for x in op_keys]

hits=[]
for t in tables:
    cols = cur.execute(f"PRAGMA table_info({t})").fetchall()
    colnames = [c[1] for c in cols]
    colnames_n = [norm(c) for c in colnames]

    has_sales = any(any(k in cn for k in sales_keys_n) for cn in colnames_n)
    has_op    = any(any(k in cn for k in op_keys_n) for cn in colnames_n)

    if has_sales or has_op:
        hits.append((t, has_sales, has_op, colnames))

print("\n--- candidate tables (sales/op columns) ---")
if not hits:
    print("No obvious sales/op columns found (maybe item/value style).")
else:
    for t, hs, ho, colnames in hits:
        print(f"- {t}: sales={hs} op={ho}")
        print("  cols:", ", ".join(colnames))

# ついでに各テーブルの行数も出す（入ってるかの最重要指標）
print("\n--- row counts (top) ---")
for t in tables:
    try:
        n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        if n:
            print(f"{t}: {n:,}")
    except Exception:
        pass

con.close()
