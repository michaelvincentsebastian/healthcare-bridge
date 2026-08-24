"""
Dipanggil oleh Docker HEALTHCHECK. Exit 0 = sehat, exit != 0 = unhealthy.

Sengaja query salah satu view di schema `bridge` (bukan cuma `SELECT 1`),
supaya healthcheck ini benar-benar menguji jalur penuh:
quack server -> DuckDB session -> mysql extension -> MariaDB.
"""
import os
import sys
import duckdb

TOKEN = os.environ["QUACK_TOKEN"]
PROBE_VIEW = os.environ.get("HEALTHCHECK_VIEW", "bridge.tabpatient")

try:
    con = duckdb.connect()
    con.execute("INSTALL quack; LOAD quack;")
    # LIMIT 1, bukan count(*) -- count(*) di atas view mysql-attached
    # memicu bug internal DuckDB (lihat catatan di serve.py).
    con.execute(f"""
        FROM quack_query(
            'quack:localhost:9494',
            'SELECT * FROM {PROBE_VIEW} LIMIT 1',
            token = '{TOKEN}',
            disable_ssl => true
        )
    """).fetchall()
    sys.exit(0)
except Exception as e:
    print(f"healthcheck failed: {e}", file=sys.stderr)
    sys.exit(1)