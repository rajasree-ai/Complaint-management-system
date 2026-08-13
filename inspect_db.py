import sqlite3, os
p = os.path.join(os.getcwd(), 'grievance_hub.db')
print('DB path:', p, 'exists=', os.path.exists(p))
if not os.path.exists(p):
    raise SystemExit('DB not found')
conn = sqlite3.connect(p)
cur = conn.cursor()
print('Tables:')
for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
    print(' ', row[0])
print('\nColumns for table "user":')
cols = cur.execute("PRAGMA table_info('user')").fetchall()
for c in cols:
    print(' ', c)
conn.close()
