"""B6 - does a change in the college database reach answers, and how fast?

Simulates another college system writing to the database directly (as the
owner, not through any import command of ours, so nothing in the application
is told about it): insert one row, ask, delete it, ask. Repeats on the fast
path and on a cached question. Pass = the answer reflects the row on the very
next ask, and the deletion likewise.

Output: results/freshness.json
"""
import json
import os
import sys
import time

import _django  # noqa: F401
from django.db import connection

from client import Client

PASSWORD = os.environ.get("BENCH_PASSWORD")
if not PASSWORD:
    sys.exit("BENCH_PASSWORD is not set. Set it to the benchmark accounts' password before running this script.")

Q = "How many faculty in the Medicine department are from Deemed universities?"


def row_sql():
    with connection.cursor() as cur:
        cur.execute("SELECT * FROM faculty_development WHERE department='Medicine' AND university_type='Deemed' LIMIT 1")
        cols = [c[0] for c in cur.description]
        row = dict(zip(cols, cur.fetchone()))
    row.pop("id", None)
    row["faculty_id"] = "FAC_BENCH_1"
    return row


def insert(row):
    cols = list(row)
    with connection.cursor() as cur:
        cur.execute(f"INSERT INTO faculty_development ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})",
                    [row[c] for c in cols])


def delete():
    with connection.cursor() as cur:
        cur.execute("DELETE FROM faculty_development WHERE faculty_id='FAC_BENCH_1'")


def n_from(answer):
    import re
    m = re.search(r"([\d,]+) faculty", answer)
    return int(m.group(1).replace(",", "")) if m else None


c = Client("bench003", PASSWORD)
steps = []
a0 = c.ask(Q); steps.append(("baseline", n_from(a0["answer"]), a0["route"], a0["total_ms"]))
a1 = c.ask(Q); steps.append(("repeat (cache?)", n_from(a1["answer"]), a1["route"], a1["total_ms"]))
row = row_sql()
insert(row); t = time.time()
a2 = c.ask(Q); steps.append(("after external INSERT", n_from(a2["answer"]), a2["route"], a2["total_ms"]))
delete()
a3 = c.ask(Q); steps.append(("after external DELETE", n_from(a3["answer"]), a3["route"], a3["total_ms"]))
base = steps[0][1]
out = {"question": Q, "steps": [dict(zip(("step", "n", "route", "ms"), s)) for s in steps],
       "insert_reflected": steps[2][1] == base + 1, "delete_reflected": steps[3][1] == base}
json.dump(out, open(f"{_django.RESULTS}/freshness.json", "w"), indent=1)
print(json.dumps(out, indent=1))
