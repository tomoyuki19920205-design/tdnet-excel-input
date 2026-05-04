import sqlite3

c = sqlite3.connect("decision_db.db")

sql = """
select ticker, title, quarter
from earnings_summaries
where ifnull(quarter,'') = ''
  and title like '%決算短信%'
order by created_at desc
"""

for row in c.execute(sql).fetchall():
    print(row)

c.close()
