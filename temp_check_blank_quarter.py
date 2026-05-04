import sqlite3

c = sqlite3.connect("decision_db.db")

sql = """
select count(*)
from earnings_summaries
where ifnull(quarter,'') = ''
  and title like '%決算短信%'
"""

r = c.execute(sql).fetchone()
print("blank quarter earnings:", r[0])

c.close()
