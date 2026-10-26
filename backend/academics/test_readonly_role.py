# Copyright (c) 2026 Yash Garad. All rights reserved.

"""The read-only role's connection limit must fit the pool that uses it.

Three numbers have to move together (gunicorn.conf.py explains why): worker
threads, RAG_AGENT_RO_POOL_MAX, and the role's CONNECTION LIMIT. The limit is
written in TWO places - the SQL bootstrap and setup_readonly_role, which runs on
every container start - and the second one stayed at 10 after the others moved,
so each boot silently undid the fix. These tests pin all of them together.
"""

import os
import re

from django.test import SimpleTestCase

from academics.management.commands import setup_readonly_role
from sql_agent import db

SQL_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "db", "sql", "create_rag_agent_ro.sql")


class ConnectionLimitFitsThePoolTests(SimpleTestCase):
    def test_command_limit_exceeds_pool_plus_sync_worker(self):
        self.assertGreater(setup_readonly_role.CONNECTION_LIMIT, db.POOL_MAX + 1)

    def test_command_and_sql_bootstrap_agree(self):
        if not os.path.exists(SQL_FILE):
            self.skipTest("db/sql not present in this image")
        with open(SQL_FILE, encoding="utf-8") as f:
            found = re.findall(r"CONNECTION LIMIT\s+(\d+)", f.read())
        self.assertTrue(found)
        self.assertEqual({int(n) for n in found}, {setup_readonly_role.CONNECTION_LIMIT})
