#!/usr/bin/env python3
"""job_queue 失敗ジョブ詳細調査"""
import sqlite3
from collections import Counter

conn = sqlite3.connect("decision_db.db")
conn.row_factory = sqlite3.Row

cols = [c["name"] for c in conn.execute("PRAGMA table_info(job_queue)")]
print("=== [SCHEMA] ===")
print(cols)

print("\n=== [STATUS SUMMARY] ===")
for r in conn.execute("SELECT status, COUNT(*) cnt FROM job_queue GROUP BY status ORDER BY cnt DESC"):
    print(f"  {r[0]}: {r[1]}")

print("\n=== [FAILED BY TYPE] ===")
for r in conn.execute(
    "SELECT type, status, COUNT(*) cnt FROM job_queue "
    "WHERE status IN ('failed','permanent_failure','error','cancelled') "
    "GROUP BY type, status ORDER BY cnt DESC"
):
    print(f"  type={r[0]}  status={r[1]}  count={r[2]}")

err_col = "error" if "error" in cols else ("error_message" if "error_message" in cols else "id")
tgt_col = "target" if "target" in cols else ("target_id" if "target_id" in cols else "id")
print(f"\n(using error_col={err_col}, target_col={tgt_col})")

print("\n=== [LATEST 20 FAILED JOBS] ===")
rows = conn.execute(
    f"SELECT id, type, status, attempts, {tgt_col} as tgt, {err_col} as err, "
    "created_at, updated_at FROM job_queue "
    "WHERE status IN ('failed','permanent_failure','error','cancelled') "
    "ORDER BY id DESC LIMIT 20"
).fetchall()
for r in rows:
    err = str(r["err"])[:120] if r["err"] else "None"
    tgt = str(r["tgt"])[:60] if r["tgt"] else "None"
    print(f"  id={r['id']} type={r['type']} status={r['status']} attempts={r['attempts']}")
    print(f"    tgt  = {tgt}")
    print(f"    err  = {err}")
    print(f"    time = {r['created_at']} -> {r['updated_at']}")

print("\n=== [ERROR PATTERNS] ===")
rows2 = conn.execute(
    f"SELECT {err_col} FROM job_queue WHERE status IN ('failed','permanent_failure','error')"
).fetchall()
patterns = Counter()
for r in rows2:
    key = str(r[0])[:90].replace("\n", " ") if r[0] else "NULL"
    patterns[key] += 1
for pat, cnt in patterns.most_common(15):
    print(f"  [{cnt}x] {pat}")

print("\n=== [FAILED BY DATE] ===")
for r in conn.execute(
    "SELECT DATE(updated_at) day, COUNT(*) cnt FROM job_queue "
    "WHERE status IN ('failed','permanent_failure','error') "
    "GROUP BY day ORDER BY day DESC LIMIT 20"
):
    print(f"  {r[0]}: {r[1]} jobs")

print("\n=== [PENDING/RUNNING] ===")
for r in conn.execute(
    "SELECT status, COUNT(*) cnt, MAX(created_at) latest FROM job_queue "
    "WHERE status IN ('pending','running','queued') GROUP BY status"
):
    print(f"  {r[0]}: {r[1]}  latest={r[2]}")

print("\n=== [LAST SUCCESSFUL] ===")
for r in conn.execute(
    f"SELECT id, type, {tgt_col} as tgt, updated_at FROM job_queue "
    "WHERE status IN ('done','success','completed') ORDER BY id DESC LIMIT 10"
):
    print(f"  id={r['id']} type={r['type']} updated={r['updated_at']} tgt={str(r['tgt'])[:50]}")

conn.close()
print("\n=== DONE ===")
