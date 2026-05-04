import sqlite3

conn = sqlite3.connect("decision_db.db")
cur = conn.cursor()

sql = """
select ticker, period, sales, operating_profit, ordinary_profit, net_income
from financials
where ticker='5423'
order by rowid desc
limit 5
"""

rows = cur.execute(sql).fetchall()

print("=== financials（財務数値テーブル）===")
for r in rows:
    print(r)

print("件数:", len(rows))
