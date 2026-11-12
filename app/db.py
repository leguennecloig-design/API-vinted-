"""Stockage SQLite : sessions de veille, annonces suivies et relevés de favoris.

Aucune donnée vendeur n'est stockée : uniquement l'annonce et ses compteurs.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import threading
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "niche_watch.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    REAL NOT NULL,          -- heure réelle de création (pour l'historique)
    start_ts      REAL NOT NULL,          -- début de la veille (horloge de la veille)
    capture_end   REAL NOT NULL,          -- fin de la captation
    final_end     REAL NOT NULL,          -- fin de la fenêtre de fin
    ended_at      REAL,
    status        TEXT NOT NULL,          -- running | done | stopped | interrupted | error
    demo          INTEGER NOT NULL DEFAULT 0,
    filters_json  TEXT NOT NULL,
    filters_label TEXT NOT NULL,
    duration_min  INTEGER NOT NULL,
    window_min    INTEGER NOT NULL,
    hit_threshold INTEGER NOT NULL,
    interval_s    INTEGER NOT NULL,
    cycles        INTEGER NOT NULL DEFAULT 0,
    requests      INTEGER NOT NULL DEFAULT 0,
    warnings      TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS items (
    session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    item_id      INTEGER NOT NULL,
    title        TEXT,
    brand        TEXT,
    price        REAL,
    currency     TEXT,
    size         TEXT,
    condition    TEXT,
    url          TEXT,
    photo_url    TEXT,
    posted_at    REAL NOT NULL,
    first_seen   REAL NOT NULL,
    first_age    REAL NOT NULL,
    last_age     REAL,
    last_fav     INTEGER,
    last_views   INTEGER,
    window_fav   INTEGER,          -- favoris au dernier relevé d'âge <= fenêtre + 60 s
    window_age   REAL,             -- âge de ce relevé
    PRIMARY KEY (session_id, item_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    item_id    INTEGER NOT NULL,
    ts         REAL NOT NULL,
    age_s      REAL NOT NULL,
    favs       INTEGER NOT NULL,
    views      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snap ON snapshots(session_id, item_id);
"""


class DB:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()
        # Une veille "running" au démarrage = le serveur a été coupé pendant la veille.
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE sessions SET status='interrupted', ended_at=? WHERE status='running'",
                (time.time(),),
            )

    # ---------- sessions ----------
    def create_session(self, **kw) -> int:
        kw.setdefault("created_at", time.time())
        kw["filters_json"] = json.dumps(kw.pop("filters"), ensure_ascii=False)
        cols = ",".join(kw)
        q = ",".join("?" for _ in kw)
        with self.lock, self.conn:
            cur = self.conn.execute(f"INSERT INTO sessions ({cols}) VALUES ({q})", tuple(kw.values()))
            return cur.lastrowid

    def update_session(self, sid: int, **kw):
        if "warnings" in kw and not isinstance(kw["warnings"], str):
            kw["warnings"] = json.dumps(kw["warnings"], ensure_ascii=False)
        sets = ",".join(f"{k}=?" for k in kw)
        with self.lock, self.conn:
            self.conn.execute(f"UPDATE sessions SET {sets} WHERE id=?", (*kw.values(), sid))

    def get_session(self, sid: int) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        return _session_dict(r) if r else None

    def list_sessions(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                """SELECT s.*, (SELECT COUNT(*) FROM items i WHERE i.session_id=s.id) AS n_items
                   FROM sessions s ORDER BY s.created_at DESC"""
            ).fetchall()
        out = []
        for r in rows:
            d = _session_dict(r)
            d["n_items"] = r["n_items"]
            out.append(d)
        return out

    def delete_session(self, sid: int):
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM snapshots WHERE session_id=?", (sid,))
            self.conn.execute("DELETE FROM items WHERE session_id=?", (sid,))
            self.conn.execute("DELETE FROM sessions WHERE id=?", (sid,))

    # ---------- annonces ----------
    def known_item_ids(self, sid: int) -> set[int]:
        with self.lock:
            return {r[0] for r in self.conn.execute("SELECT item_id FROM items WHERE session_id=?", (sid,))}

    def record_cycle(self, sid: int, new_items: list[dict], snaps: list[dict], window_s: float):
        """Insère les nouvelles annonces et les relevés d'un cycle, en une transaction."""
        limit = window_s + 60
        with self.lock, self.conn:
            for it in new_items:
                self.conn.execute(
                    """INSERT OR IGNORE INTO items (session_id,item_id,title,brand,price,currency,size,
                       condition,url,photo_url,posted_at,first_seen,first_age)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (sid, it["item_id"], it["title"], it["brand"], it["price"], it["currency"], it["size"],
                     it["condition"], it["url"], it["photo_url"], it["posted_at"], it["ts"], it["age"]),
                )
            for s in snaps:
                self.conn.execute(
                    "INSERT INTO snapshots (session_id,item_id,ts,age_s,favs,views) VALUES (?,?,?,?,?,?)",
                    (sid, s["item_id"], s["ts"], s["age"], s["favs"], s["views"]),
                )
                self.conn.execute(
                    "UPDATE items SET last_age=?, last_fav=?, last_views=? WHERE session_id=? AND item_id=?",
                    (s["age"], s["favs"], s["views"], sid, s["item_id"]),
                )
                if s["age"] <= limit:
                    # Relevés chronologiques : le dernier relevé <= fenêtre + 60 s l'emporte.
                    self.conn.execute(
                        """UPDATE items SET window_fav=?, window_age=?
                           WHERE session_id=? AND item_id=? AND (window_age IS NULL OR window_age<=?)""",
                        (s["favs"], s["age"], sid, s["item_id"], s["age"]),
                    )

    def items(self, sid: int) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM items WHERE session_id=?", (sid,)).fetchall()
        return [dict(r) for r in rows]

    def snapshot_count(self, sid: int) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM snapshots WHERE session_id=?", (sid,)).fetchone()[0]


def _session_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["filters"] = json.loads(d.pop("filters_json"))
    d["warnings"] = json.loads(d["warnings"] or "[]")
    return d


# ---------- calculs ----------
def enrich_items(items: list[dict], window_min: int, threshold: int, now: float | None = None) -> list[dict]:
    """Ajoute à chaque annonce : favoris fenêtre, statut partiel / en cours, hit."""
    w = window_min * 60
    for it in items:
        wf = it.get("window_fav")
        wa = it.get("window_age")
        it["measured"] = wf is not None
        it["in_progress"] = bool(now is not None and (now - it["posted_at"]) < w and wf is not None)
        # Mesure partielle : le relevé retenu a été pris trop tôt (plus de 30 s avant la fin de la
        # fenêtre), par ex. veille arrêtée ou pause anti-blocage. 30 s de tolérance pour l'aléa des scans.
        it["partial"] = bool(wf is not None and wa is not None and wa < w - 30 and not it["in_progress"])
        it["is_hit"] = bool(wf is not None and wf >= threshold)
        it["age_min"] = round((now - it["posted_at"]) / 60, 1) if now else None
        it["favs_per_min"] = round(wf / max(wa / 60, 1), 2) if wf is not None and wa else None
    return items


def brand_stats(items: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(it.get("brand") or "(sans marque)", []).append(it)
    out = []
    for brand, its in groups.items():
        favs = [i["window_fav"] for i in its if i["window_fav"] is not None]
        prices = [i["price"] for i in its if i.get("price") is not None]
        hits = sum(1 for i in its if i["is_hit"])
        n = len(its)
        med = statistics.median(favs) if favs else 0
        out.append({
            "brand": brand,
            "n_items": n,
            "hits": hits,
            "hit_rate": round(100 * hits / n, 1) if n else 0,
            "fav_mean": round(statistics.fmean(favs), 1) if favs else 0,
            "fav_median": med,
            "fav_max": max(favs) if favs else 0,
            "price_mean": round(statistics.fmean(prices), 2) if prices else None,
            "score": round(med * min(n, 5) / 5 + 2 * hits, 2),
        })
    out.sort(key=lambda b: (b["score"], b["hits"], b["n_items"]), reverse=True)
    return out
