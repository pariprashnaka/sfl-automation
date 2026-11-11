"""Wrong-flag matching test. Run: python test_wrong.py (uses a temporary database)."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import leads_app as m

tmp = tempfile.mkdtemp()
m.DB = os.path.join(tmp, "t.db")
m._BIZ_READY = False
m.init()
m.ensure_businesses()
c = m.conn()
c.execute("INSERT INTO businesses(name,name_key,city,phones,status) VALUES('Verma',?,'Patna','[{\"n\": \"+919876500001\", \"t\": \"mobile\"}]','wrong')", (m.name_key_of("Verma"),))
c.commit()
c.close()

checks = [
    ("Verma Hospital in Patna is NOT skipped", m.wrong_match("Verma Hospital", [], "Patna"), False),
    ("Verma Hospital, no phone, no city is NOT skipped", m.wrong_match("Verma Hospital", [], ""), False),
    ("exact 'Verma' in Patna is skipped", m.wrong_match("Verma", [], "Patna"), True),
    ("exact 'verma' (case) in Patna is skipped", m.wrong_match("verma", [], "Patna"), True),
    ("'Verma' in another city (Gaya) is NOT skipped", m.wrong_match("Verma", [], "Gaya"), False),
    ("same phone is skipped even with another name", m.wrong_match("Anything Clinic", ["9876500001"], "Gaya"), True),
    ("Verma Hospital with a different phone is NOT skipped", m.wrong_match("Verma Hospital", ["9123456789"], "Patna"), False),
]
bad = 0
for label, got, want in checks:
    ok = got == want
    bad += not ok
    print(("PASS " if ok else "FAIL ") + label)
sys.exit(1 if bad else 0)
