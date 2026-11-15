"""Database for the cs2render website.

The site only stores finished results uploaded from the rendering PC: match stats, players, clips.

Two backends, same code:
  SQLite      web/data/site.db (default, for running on this PC)
  PostgreSQL  when DATABASE_URL is set (e.g. Neon), for the hosted site on Vercel

The code is written for SQLite (? placeholders, sqlite3.Row-style rows); for PostgreSQL the queries are
translated on the fly: ? -> %s, inserts into tables with an id get RETURNING id (for .lastrowid), the
schema's types are mapped, and rows can be read by index or by column name like sqlite3.Row.
"""
import json
import os
import re
import sqlite3
import threading
import time

import config

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DB_PATH = os.path.join(DATA, "site.db")
# Neon set up through Vercel also sets POSTGRES_URL
DATABASE_URL = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or "").strip()
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
  id           INTEGER PRIMARY KEY,
  slug         TEXT UNIQUE NOT NULL,          -- URL id: /m/<slug>, files in data/media/<slug>/
  match_key    TEXT UNIQUE NOT NULL,          -- from the package; the same match uploaded again updates this row
  title        TEXT,                          -- optional custom title
  map          TEXT, date TEXT, rounds INTEGER,
  team1        TEXT, score1 INTEGER, team2 TEXT, score2 INTEGER,   -- team1 = winner
  mvp_name     TEXT, mvp_rating REAL, mvp_avatar TEXT,
  thumb        TEXT, clip_count INTEGER DEFAULT 0,
  published    INTEGER DEFAULT 0,
  data_json    TEXT NOT NULL,                 -- full page data (scoreboard_html.collect on the PC)
  size_bytes   INTEGER DEFAULT 0,             -- files stored for this match
  uploads      INTEGER DEFAULT 1,
  media_rev    TEXT DEFAULT '',               -- storage folder of the latest upload (R2); '' = local disk
  created_at   REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS players (
  id INTEGER PRIMARY KEY,
  match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  steamid TEXT, name TEXT, team INTEGER,
  rating REAL, swing REAL, kills INTEGER, deaths INTEGER, assists INTEGER,
  adr REAL, kast REAL, hs_pct REAL, opening INTEGER, mvps INTEGER, rounds INTEGER
);
CREATE INDEX IF NOT EXISTS players_steamid ON players(steamid);
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY,
  match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
  idx INTEGER, player TEXT, steamid TEXT, label TEXT, round INTEGER, score INTEGER, file TEXT, thumb TEXT
);
CREATE TABLE IF NOT EXISTS uploads (
  id INTEGER PRIMARY KEY,
  match_id INTEGER REFERENCES matches(id) ON DELETE SET NULL,
  via TEXT, ok INTEGER, message TEXT, size_bytes INTEGER, created_at REAL
);
-- admins' choices about clips, by match and play (round + player), so they survive a re-upload of the match:
-- saved = kept when unsaved clips are cleaned up; removed = deleted (its files are gone, re-uploads skip it)
CREATE TABLE IF NOT EXISTS clip_marks (
  match_key TEXT NOT NULL, round INTEGER NOT NULL, steamid TEXT NOT NULL,
  saved INTEGER DEFAULT 0, removed INTEGER DEFAULT 0, by_admin TEXT, updated_at REAL,
  PRIMARY KEY (match_key, round, steamid)
);
-- extra admin accounts (the main admin logs in with ADMIN_PASSWORD); passwords are stored hashed
CREATE TABLE IF NOT EXISTS admins (
  name TEXT PRIMARY KEY, pw_hash TEXT NOT NULL, created_at REAL
);
"""


# ---------------------------------------------------------------- PostgreSQL adapter ----

ID_TABLES = {"matches", "players", "clips", "uploads", "games"}   # tables with an id column
_INSERT = re.compile(r"^\s*INSERT\s+INTO\s+(\w+)", re.I)


def pg_schema(script):
    """SQLite schema -> PostgreSQL: auto ids, 64-bit integers, double precision floats."""
    script = re.sub(r"\bINTEGER PRIMARY KEY\b", "BIGSERIAL PRIMARY KEY", script)
    script = re.sub(r"\bINTEGER\b", "BIGINT", script)
    return re.sub(r"\bREAL\b", "DOUBLE PRECISION", script)


def pg_sql(sql):
    sql = sql.replace("%", "%%").replace("?", "%s")
    m = _INSERT.match(sql)
    if m and m.group(1).lower() in ID_TABLES and "RETURNING" not in sql.upper():
        sql = sql.rstrip().rstrip(";") + " RETURNING id"
    return sql


class Row(tuple):
    """A result row readable by position, by column name and with dict(row), like sqlite3.Row."""

    def __new__(cls, values, names, index):
        r = tuple.__new__(cls, values)
        r._names, r._index = names, index
        return r

    def __getitem__(self, k):
        if isinstance(k, str):
            return tuple.__getitem__(self, self._index[k.lower()])
        return tuple.__getitem__(self, k)

    def keys(self):
        return list(self._names)


def _row_factory(cursor):
    names = [d.name for d in cursor.description] if cursor.description else []
    index = {}
    for i, n in enumerate(names):
        index.setdefault(n.lower(), i)
    return lambda values: Row(values, names, index)


class PgCursor:
    def __init__(self, cur, insert):
        self._cur, self._insert = cur, insert

    @property
    def lastrowid(self):
        return self._cur.fetchone()[0] if self._insert else None

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall() if self._cur.description else []

    def __iter__(self):
        return iter(self.fetchall())


class PgConnection:
    """sqlite3.Connection look-alike over psycopg."""

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, args=()):
        q = pg_sql(sql)
        cur = self.raw.cursor(row_factory=_row_factory)
        cur.execute(q, tuple(args))
        return PgCursor(cur, q.endswith(" RETURNING id"))

    def executemany(self, sql, rows):
        rows = [tuple(r) for r in rows]
        if rows:
            q = pg_sql(sql)
            if q.endswith(" RETURNING id"):
                q = q[: -len(" RETURNING id")]
            self.raw.cursor().executemany(q, rows)

    def executescript(self, script):
        self.raw.execute(pg_schema(script))

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        """Ends the transaction; the connection itself is kept for the next request on this thread."""
        try:
            self.raw.rollback()
        except Exception:
            _local.__dict__.pop("raw", None)


_local = threading.local()


def _numeric_loader():
    """SUM()/AVG() come back as numeric; give Python int/float like SQLite does, not Decimal."""
    from psycopg.adapt import Loader

    class NumericLoader(Loader):
        def load(self, data):
            s = bytes(data).decode()
            return float(s) if ("." in s or "e" in s.lower() or s in ("NaN", "Infinity", "-Infinity")) else int(s)
    return NumericLoader


def _pg_connect():
    import psycopg
    raw = getattr(_local, "raw", None)
    if raw is None or raw.closed or raw.broken:
        raw = psycopg.connect(DATABASE_URL, connect_timeout=15)
        raw.adapters.register_loader("numeric", _numeric_loader())
        _local.raw = raw
    return PgConnection(raw)


def connect():
    if IS_PG:
        return _pg_connect()
    os.makedirs(DATA, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def init():
    con = connect()
    if not IS_PG:
        old = [r[1] for r in con.execute("PRAGMA table_info(matches)")]
        if old and "match_key" not in old:  # database from the first prototype (demo uploads): start fresh
            con.close()
            os.replace(DB_PATH, DB_PATH + f".old-{int(time.time())}")
            for ext in ("-wal", "-shm"):
                if os.path.exists(DB_PATH + ext):
                    os.remove(DB_PATH + ext)
            con = connect()
    con.executescript(SCHEMA)
    if IS_PG:
        con.execute("ALTER TABLE matches ADD COLUMN IF NOT EXISTS media_rev TEXT DEFAULT ''")
    elif "media_rev" not in [r[1] for r in con.execute("PRAGMA table_info(matches)")]:
        con.execute("ALTER TABLE matches ADD COLUMN media_rev TEXT DEFAULT ''")
    import statsdb
    statsdb.init(con)
    con.commit()
    con.close()


def save_match_data(con, match_id, data, summary):
    """Store a match's page data: summary columns, player rows, clip rows, full JSON."""
    t = summary["teams"] + [{"name": "", "score": 0}] * 2
    con.execute("""UPDATE matches SET map=?, date=?, rounds=?, team1=?, score1=?, team2=?, score2=?,
                   mvp_name=?, mvp_rating=?, mvp_avatar=?, thumb=?, clip_count=?, data_json=?, updated_at=?
                   WHERE id=?""",
                (summary["map"], summary["date"], summary["rounds"], t[0]["name"], t[0]["score"], t[1]["name"],
                 t[1]["score"], summary["mvp"]["name"], summary["mvp"]["rating"], summary["mvp"]["avatar"],
                 summary["thumb"], summary["clips"], json.dumps(data, ensure_ascii=False), time.time(), match_id))
    con.execute("DELETE FROM players WHERE match_id = ?", (match_id,))
    con.executemany("""INSERT INTO players(match_id, steamid, name, team, rating, swing, kills, deaths, assists,
                       adr, kast, hs_pct, opening, mvps, rounds) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [(match_id, sid, p["name"], p["team"], p["rating"], p["swing"], p["kills"], p["deaths"],
                      p["assists"], p["adr"], p["kast"], p["hs_pct"], p["opening"], p["mvps"], p["rounds"])
                     for sid, p in data["players"].items()])
    con.execute("DELETE FROM clips WHERE match_id = ?", (match_id,))
    con.executemany("""INSERT INTO clips(match_id, idx, player, steamid, label, round, score, file, thumb)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    [(match_id, i, c["player"], c["steamid"], c["label"], c["round"], c["score"], c["file"],
                      c["thumb"]) for i, c in enumerate(data["clips"], 1)])
