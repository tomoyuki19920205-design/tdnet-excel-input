import sqlite3
import os

db_path = "C:/Users/takuy/OneDrive/tdnet-excel-input/data/backfill_state.db"

if not os.path.exists(db_path):
    print(f"Error: {db_path} not found")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# テーブル一覧
tables = cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print(f"Tables: {[t[0] for t in tables]}")

for table in [t[0] for t in tables]:
    print(f"\nTable: {table}")
    columns = cursor.execute(f"PRAGMA table_info({table})").fetchall()
    for col in columns:
        print(f"  {col[1]} ({col[2]})")
    
    # サンプル 1 件
    sample = cursor.execute(f"SELECT * FROM {table} LIMIT 1").fetchone()
    print(f"  Sample: {sample}")

conn.close()
