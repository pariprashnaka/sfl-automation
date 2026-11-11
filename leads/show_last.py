import sqlite3
c = sqlite3.connect("leads.db")
c.row_factory = sqlite3.Row
k = c.execute("select * from campaigns order by id desc limit 1").fetchone()
cid = k["id"]
print(dict(k))
n = lambda q: c.execute(q, (cid,)).fetchone()[0]
print("leads:", n("select count(*) from leads where campaign_id=?"),
      "| with phone:", n("select count(*) from leads where campaign_id=? and phones!=''"),
      "| with email:", n("select count(*) from leads where campaign_id=? and emails!=''"))
for r in c.execute("select * from leads where campaign_id=? order by id limit 40", (cid,)):
    print(r["title"][:55], "|", r["phones"], "|", r["domain"])
