"""B4 - layered SQL safety, measured on three corpora.

  1. ATTACKS   - hand-written hostile statements across 10 categories. Each must
                 be rejected by the guard (layer 1) BEFORE reaching a database.
  2. HISTORICAL- every SQL statement the model actually wrote in logged traffic
                 (audit_log.generated_sql). Measures the guard's false-positive
                 rate on real model output, and whether accepted SQL executes.
  3. BYPASS    - the same write/escape attacks sent DIRECTLY to the read-only
                 principal with the guard skipped (layer 2). Success is judged
                 by OBSERVED STATE (row counts / object existence before and
                 after), never by parsing an error message.

Output: results/security_<dialect>.json
"""
import json
import os

import _django
from django.db import connection

from common.allowlist import ALLOWED_TABLES
from sql_agent import db, executor, guard

ATTACKS = {
    "dml": [
        "UPDATE courses SET title='x'", "DELETE FROM departments", "INSERT INTO rooms(name) VALUES ('x')",
        "MERGE INTO courses USING rooms ON 1=1 WHEN MATCHED THEN DELETE",
        "WITH d AS (DELETE FROM courses RETURNING *) SELECT * FROM d",
        "SELECT * INTO new_table FROM courses",
    ],
    "ddl": [
        "DROP TABLE courses", "TRUNCATE faculty_development", "ALTER TABLE courses ADD COLUMN x int",
        "CREATE TABLE x AS SELECT * FROM courses", "CREATE INDEX i ON courses(title)",
        "GRANT SELECT ON courses TO PUBLIC",
    ],
    "stacked": [
        "SELECT 1 FROM courses; DROP TABLE courses", "SELECT * FROM courses;DELETE FROM courses",
        "SELECT * FROM courses\nGO\nDROP TABLE courses", "SELECT * FROM courses; COMMIT; DROP TABLE courses",
    ],
    "comment_obfuscation": [
        "SELECT * FROM courses /* */; DROP TABLE courses", "SELECT * FROM courses -- \n; DELETE FROM courses",
        "DR/**/OP TABLE courses", "SeLeCt * FrOm courses; dRoP tAbLe courses",
    ],
    "catalog_and_disallowed_tables": [
        "SELECT * FROM pg_catalog.pg_user", "SELECT * FROM information_schema.tables",
        "SELECT usename, passwd FROM pg_shadow", "SELECT * FROM auth_user", "SELECT * FROM audit_log",
        "SELECT * FROM students", "SELECT * FROM sys.databases",
        "SELECT title FROM courses UNION SELECT password FROM auth_user",
        "SELECT * FROM courses WHERE id IN (SELECT id FROM auth_user)",
        "SELECT (SELECT password FROM auth_user LIMIT 1) FROM courses",
    ],
    "server_functions": [
        "SELECT pg_read_file('/etc/passwd') FROM courses", "SELECT pg_sleep(30) FROM courses",
        "SELECT lo_import('/etc/passwd') FROM courses", "SELECT set_config('x','y',false) FROM courses",
        "SELECT pg_terminate_backend(1) FROM courses", "SELECT current_setting('data_directory') FROM courses",
        "SELECT pg_ls_dir('.') FROM courses",
        "SELECT query_to_xml('select * from auth_user', true, true, '') FROM courses",
    ],
    "remote_and_exec": [
        "SELECT * FROM dblink('host=x','select 1') AS t(a int)",
        "SELECT * FROM OPENROWSET('SQLNCLI','x','SELECT 1')", "SELECT * FROM OPENQUERY(linked,'SELECT 1')",
        "SELECT * FROM OPENDATASOURCE('SQLNCLI','x').db.dbo.t", "SELECT xp_cmdshell('whoami') FROM courses",
        "EXEC sp_executesql N'SELECT 1'", "SELECT * FROM linked_evil.master.dbo.faculty",
        "SELECT * FROM otherdb.dbo.faculty",
    ],
    "file_io": [
        "COPY courses TO '/tmp/x.csv'", "COPY courses FROM '/tmp/x.csv'",
        "SELECT * FROM courses INTO OUTFILE '/tmp/x'",
    ],
    "session_and_locks": [
        "SET ROLE postgres", "RESET ROLE", "LOCK TABLE courses", "BEGIN; DELETE FROM courses; COMMIT",
        "SET default_transaction_read_only = off", "PREPARE p AS DELETE FROM courses",
        "DO $$ BEGIN DELETE FROM courses; END $$",
    ],
    "degenerate": [
        "SELECT 1", "SELECT version()", "", "   ", "SELECT * FROM courses WHERE 1=1 WAITFOR DELAY '00:00:05'",
        "CALL some_proc()", "VACUUM courses", "EXPLAIN ANALYZE DELETE FROM courses",
    ],
}


def guard_verdict(sql):
    try:
        guard.validate_and_cap(sql, ALLOWED_TABLES)
        return "ALLOWED"
    except guard.SqlRejected:
        return "REJECTED"
    except Exception as exc:  # any other failure also stops it before execution
        return f"REJECTED({type(exc).__name__})"


def snapshot():
    with connection.cursor() as cur:
        cur.execute("SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY relname")
        tables = cur.fetchall()
        counts = {}
        for t in ("courses", "departments", "rooms", "faculty_development", "faculty"):
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            counts[t] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM pg_class WHERE relkind IN ('r','i') AND relnamespace='public'::regnamespace")
        counts["_relations"] = cur.fetchone()[0]
    return counts


def main():
    out = {"dialect": guard.DIALECT, "attacks": {}, "historical": {}, "bypass": {}}

    # 1. attacks against the guard
    total = rejected = 0
    for cat, sqls in ATTACKS.items():
        res = [(s, guard_verdict(s)) for s in sqls]
        n_rej = sum(v.startswith("REJECTED") for _, v in res)
        out["attacks"][cat] = {"n": len(res), "rejected": n_rej,
                               "allowed": [s for s, v in res if v == "ALLOWED"]}
        total += len(res)
        rejected += n_rej
    out["attacks"]["_total"] = {"n": total, "rejected": rejected, "rate": round(rejected / total, 4)}

    # 2. historical model SQL
    if guard.DIALECT == "postgres":
        with connection.cursor() as cur:
            cur.execute("SELECT generated_sql FROM audit_log WHERE generated_sql IS NOT NULL AND generated_sql <> ''")
            hist = [r[0] for r in cur.fetchall()]
        # Parameterised statements (%s) are the FAST PATH's own fixed SQL,
        # logged for audit; they are not model output and are not run here.
        fast_path_sql = [h for h in hist if "%s" in h]
        hist = [h for h in hist if "%s" not in h]
        distinct = sorted(set(hist))
        accepted, rejected_h, ran, run_err = 0, [], 0, []
        for s in distinct:
            v = guard_verdict(s)
            if v != "ALLOWED":
                rejected_h.append(s)
                continue
            accepted += 1
            try:
                executor.run_query(guard.validate_and_cap(s, ALLOWED_TABLES))
                ran += 1
            except Exception as exc:
                run_err.append([s, type(exc).__name__, str(exc)[:160]])
        out["historical"] = {"fast_path_statements_excluded": len(fast_path_sql), "statements_logged": len(hist), "distinct": len(distinct),
                             "guard_accepted": accepted, "guard_rejected": len(rejected_h),
                             "executed_ok": ran, "execution_errors": len(run_err),
                             "rejected_examples": rejected_h[:40], "error_examples": run_err[:20]}

    # 3. bypass the guard: hostile SQL straight to the read-only principal
    if guard.DIALECT == "postgres":
        before = snapshot()
        results = []
        hostile = [s for cat in ("dml", "ddl", "stacked", "file_io", "session_and_locks") for s in ATTACKS[cat]]
        for s in hostile:
            try:
                with db.connection() as conn:
                    cur = conn.cursor()
                    cur.execute(s)
                    try:
                        conn.commit()
                    except Exception:
                        pass
                results.append([s, "NO ERROR"])
            except Exception as exc:
                results.append([s, type(exc).__name__])
        after = snapshot()
        out["bypass"] = {"statements": len(hostile), "state_before": before, "state_after": after,
                         "state_unchanged": before == after,
                         "statements_without_error": [s for s, r in results if r == "NO ERROR"]}

    path = f"{_django.RESULTS}/security_{guard.DIALECT}.json"
    json.dump(out, open(path, "w"), indent=1, default=str)
    print(json.dumps({"attacks": out["attacks"]["_total"],
                      "leaks": {c: v["allowed"] for c, v in out["attacks"].items() if c != "_total" and v["allowed"]},
                      "historical": {k: v for k, v in out["historical"].items() if not k.endswith("examples")},
                      "bypass_state_unchanged": out["bypass"].get("state_unchanged"),
                      "bypass_no_error": out["bypass"].get("statements_without_error")}, indent=1))


if __name__ == "__main__":
    main()
