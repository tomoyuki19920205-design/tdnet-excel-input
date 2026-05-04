import sqlite3

c = sqlite3.connect("decision_db.db")

sql = """
update earnings_summaries
set quarter = 'FY'
where ifnull(quarter,'') = ''
  and title like '%決算短信%'
  and title not like '%有価証券報告書%'
"""

cur = c.execute(sql)
c.commit()

print("updated:", cur.rowcount)

r = c.execute("""
select count(*)
from earnings_summaries
where ifnull(quarter,'') = ''
  and title like '%決算短信%'
""").fetchone()
print("blank quarter remaining:", r[0])

c.close()
