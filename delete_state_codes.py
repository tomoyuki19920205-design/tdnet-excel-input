import sqlite3
DB=r"C:\Users\takuy\OneDrive\tdnet-excel-input\data\state.db"
con=sqlite3.connect(DB)
cur=con.cursor()

codes=("259","6635","8057")
cur.execute(f"""
DELETE FROM processing_log
WHERE CAST(code AS TEXT) IN ({",".join(["?"]*len(codes))})
""", codes)

print("deleted rows =", cur.rowcount)
con.commit()
con.close()
