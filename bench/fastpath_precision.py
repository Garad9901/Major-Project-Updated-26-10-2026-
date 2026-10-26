"""B3 - precision of the deterministic tier on a labelled probe set.

Each case is a question plus the SQL that computes its true answer (written by
hand, independently of the fast path), or null when no single figure in the
database answers it. For every case the fast path either DECLINES (falls
through to the model - safe, only slower) or ANSWERS; an answer is CORRECT when
its number equals the truth and WRONG otherwise. A WRONG answer is the failure
that matters: it bypasses the model and the verifier and reaches the user as a
confident figure.

Output: results/fastpath_precision_<label>.json
"""
import json
import re
import sys

import _django
from django.db import connection

from fast_path import service as fp
from sql_agent import db

label = sys.argv[1] if len(sys.argv) > 1 else "run"
CASES = sys.argv[2] if len(sys.argv) > 2 else "fastpath_cases.json"
cases = json.load(open(__file__.replace("fastpath_precision.py", CASES)))
rows = []
for q, truth_sql in cases:
    truth = None
    if truth_sql:
        with connection.cursor() as cur:
            cur.execute(truth_sql)
            truth = float(cur.fetchone()[0])
    a = fp.try_answer(q, db.connection)
    if a is None:
        outcome = "DECLINED"
        got = None
    else:
        got = float(a.rows[0].get("n", a.rows[0].get("a")))
        outcome = "CORRECT" if truth is not None and abs(got - truth) < 0.051 else "WRONG"
    rows.append({"question": q, "truth": truth, "answer": got, "outcome": outcome,
                 "intent": a.intent if a else None, "text": a.text if a else None})

n = len(rows)
ans = [r for r in rows if r["outcome"] != "DECLINED"]
wrong = [r for r in rows if r["outcome"] == "WRONG"]
summary = {
    "label": label, "cases": n,
    "answered": len(ans), "correct": n - len(wrong) - (n - len(ans)), "wrong": len(wrong),
    "declined": n - len(ans),
    "precision": round((len(ans) - len(wrong)) / len(ans), 4) if ans else None,
    "silent_error_rate": round(len(wrong) / n, 4),
}
json.dump({"summary": summary, "cases": rows}, open(f"{_django.RESULTS}/fastpath_precision_{label}.json", "w"), indent=1)
print(json.dumps(summary))
for r in rows:
    print(f'{r["outcome"]:9} truth={r["truth"]} got={r["answer"]} | {r["question"]}')
