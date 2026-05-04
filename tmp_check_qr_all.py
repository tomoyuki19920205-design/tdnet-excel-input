import sqlite3

con = sqlite3.connect("data/decision_db.db")
con.row_factory = sqlite3.Row

sql = """
select
  company_code,
  fiscal_year_end,
  quarter,
  sales,
  operating_profit,
  source_doc_id,
  updated_at
from quarterly_results
where company_code in ('1930','5423')
order by company_code, fiscal_year_end desc, quarter
limit 50
"""

rows = con.execute(sql).fetchall()
print("rows =", len(rows))

for r in rows:
    print(dict(r))
