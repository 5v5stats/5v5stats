"""SQLite database for the cs2render website (web/data/site.db).

The site only stores finished results uploaded from the rendering PC: match stats, players, clips.
"""
import json
import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DB_PATH = os.path.join(DATA, "site.db")

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
"""


def connect():
    os.makedirs(DATA, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def init():
    con = connect()
    old = [r[1] for r in con.execute("PRAGMA table_info(matches)")]
    if old and "match_key" not in old:  # database from the first prototype (demo uploads): start fresh
        con.close()
        os.replace(DB_PATH, DB_PATH + f".old-{int(time.time())}")
        for ext in ("-wal", "-shm"):
            if os.path.exists(DB_PATH + ext):
                os.remove(DB_PATH + ext)
        con = connect()
    con.executescript(SCHEMA)
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
