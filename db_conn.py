"""db_conn.py - shared OracleDB connection (python-oracledb thin mode).

Sets CURRENT_SCHEMA (from config.DB_SCHEMA / env DB_SCHEMA) after connecting, so all
queries stay unqualified while the tables live in a different schema (name from env). The
connecting user still needs SELECT/INSERT/UPDATE/DELETE grants on those tables.
"""
import json
import os
import re

import oracledb

import config  # loads .env; exposes DB_SCHEMA

# On read, return BLOB as bytes and CLOB/NCLOB as str (not Lob objects).
oracledb.defaults.fetch_lobs = False

_IDENT = re.compile(r"^[A-Za-z0-9_$#]+$")


def as_json(value):
    """Decode a JSON column whether the driver returns bytes, str, or an already-parsed
    object (this DB returns `BLOB ... IS JSON` columns pre-parsed)."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return json.loads(value.decode("utf-8"))
    if isinstance(value, str):
        return json.loads(value)
    return value


def connect():
    user = os.environ.get("DB_USER")
    pw = os.environ.get("DB_PASSWORD")
    dsn = os.environ.get("DB_CONNECT_STRING")
    if not all([user, pw, dsn]):
        raise RuntimeError("Set DB_USER, DB_PASSWORD, DB_CONNECT_STRING in .env")
    con = oracledb.connect(user=user, password=pw, dsn=dsn)
    schema = (config.DB_SCHEMA or "").strip()
    if schema:
        if not _IDENT.match(schema):
            raise ValueError(f"unsafe DB_SCHEMA (not a plain identifier): {schema!r}")
        cur = con.cursor()
        cur.execute("ALTER SESSION SET CURRENT_SCHEMA = " + schema)  # not bindable
        cur.close()
    return con


if __name__ == "__main__":  # quick connectivity check
    c = connect()
    cur = c.cursor()
    cur.execute("SELECT sys_context('USERENV','CURRENT_SCHEMA') FROM dual")
    print("connected; CURRENT_SCHEMA =", cur.fetchone()[0])
    c.close()
