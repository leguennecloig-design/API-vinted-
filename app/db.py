"""Stockage : Postgres si DATABASE_URL / POSTGRES_URL est défini (Vercel + Neon), sinon SQLite local.

Aucune donnée vendeur n'est stockée : uniquement l'annonce et ses compteurs.
"""
from __future__ import annotations

import json
import os
import sqlite3
import statistics
import threading
from pathlib import Path

DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""
SQLITE_PATH = Path(__file__).resolve().parent.parent / "data" / "niche_watch.db"

SESSION_COLS = """
    created_at    {real} NOT NULL,
    start_ts      {real} NOT NULL,
    capture_end   {real} NOT NULL,
    final_end     {real} NOT NULL,
    ended_at      {real},
    status        TEXT NOT NULL,
    demo          INTEGER NOT NULL DEFAULT 0,
    speed         {real} NOT NULL DEFAULT 1,
    demo_t0       {real},
    seed          INTEGER NOT NULL DEFAULT 0,
    filters_json  TEXT NOT NULL,
    filters_label TEXT NOT NULL,
    duration_min  INTEGER NOT NULL,
    window_min    INTEGER NOT NULL,
    hit_threshold INTEGER NOT NULL,
    interval_s    INTEGER NOT NULL,
    cycles        INTEGER NOT NULL DEFAULT 0,
    requests      INTEGER NOT NULL DEFAULT 0,
    next_tick_at  {real} NOT NULL DEFAULT 0,
    tick_lock     {real},
    last_cycle_at {real},
    backoff_i     INTEGER NOT NULL DEFAULT 0,
    api_level     TEXT NOT NULL DEFAULT 'ok',
    api_text      TEXT NOT NULL DEFAULT 'Démarrage…',
    cookies       TEXT,
    last_cycle    TEXT NOT NULL DEFAULT '{{}}',
    warnings      TEXT NOT NULL DEFAULT '[]',
    summary       TEXT
"""
ITEM_COLS = ["session_id", "item_id", "title", "brand", "price", "currency", "size", "condition", "url",
             "photo_url", "posted_at", "first_seen", "first_age", "last_age", "last_fav", "last_views",
             "window_fav", "window_age"]


def _schema(pg: bool) -> list[str]:
    real = "DOUBLE PRECISION" if pg else "REAL"
    pk = "id BIGSERIAL PRIMARY KEY" if pg else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    big = "BIGINT" if pg else "INTEGER"
    return [
        f"CREATE TABLE IF NOT EXISTS sessions ({pk}, {SESSION_COLS.format(real=real)})",
        f"""CREATE TABLE IF NOT EXISTS items (
            session_id {big} NOT NULL, item_id {big} NOT NULL, title TEXT, brand TEXT,
            price {real}, currency TEXT, size TEXT, condition TEXT, url TEXT, photo_url TEXT,
            posted_at {real} NOT NULL, first_seen {real} NOT NULL, first_age {real} NOT NULL,
            last_age {real}, last_fav INTEGER, last_views INTEGER, window_fav INTEGER, window_age {real},
            PRIMARY KEY (session_id, item_id))""",
        f"""CREATE TABLE IF NOT EXISTS snapshots (
            session_id {big} NOT NULL, item_id {big} NOT NULL, ts {real} NOT NULL,
            age_s {real} NOT NULL, favs INTEGER NOT NULL, views INTEGER)""",
        "CREATE INDEX IF NOT EXISTS idx_snap ON snapshots(session_id, item_id)",
    ]


class DB:
    def __init__(self):
        self.pg = bool(DB_URL)
        self.lock = threading.RLock()
        self._conn = None
        with self.lock:
            for stmt in _schema(self.pg):
                self.execute(stmt)

    # ------------------------------------------------------------------ bas niveau
    def _connect(self):
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row
            return psycopg.connect(DB_URL, autocommit=True, row_factory=dict_row, connect_timeout=15)
        SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(SQLITE_PATH), check_same_thread=False, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _run(self, sql: str, params=(), fetch: str | None = None):
        if self.pg:
            sql = sql.replace("?", "%s")
        with self.lock:
            for attempt in (1, 2):
                if self._conn is None:
                    self._conn = self._connect()
                try:
                    cur = self._conn.execute(sql, params)
                    if fetch == "all":
                        return [dict(r) for r in cur.fetchall()]
                    if fetch == "one":
                        r = cur.fetchone()
                        return dict(r) if r else None
                    return cur.rowcount
                except Exception as e:
                    # Connexion Postgres coupée (Neon met en veille les connexions inactives) : on retente.
                    if self.pg and attempt == 1 and _is_conn_error(e):
                        try:
                            self._conn.close()
                        except Exception:
                            pass
                        self._conn = None
                        continue
                    raise

    def execute(self, sql, params=()) -> int:
        return self._run(sql, params)

    def all(self, sql, params=()) -> list[dict]:
        return self._run(sql, params, "all")

    def one(self, sql, params=()) -> dict | None:
        return self._run(sql, params, "one")

    # ------------------------------------------------------------------ sessions
    def create_session(self, **kw) -> int:
        kw["filters_json"] = json.dumps(kw.pop("filters"), ensure_ascii=False)
        cols = ",".join(kw)
        q = ",".join("?" for _ in kw)
        return self.one(f"INSERT INTO sessions ({cols}) VALUES ({q}) RETURNING id", tuple(kw.values()))["id"]

    def update_session(self, sid: int, **kw):
        for k in ("warnings", "last_cycle", "summary"):
            if k in kw and kw[k] is not None and not isinstance(kw[k], str):
                kw[k] = json.dumps(kw[k], ensure_ascii=False)
        sets = ",".join(f"{k}=?" for k in kw)
        self.execute(f"UPDATE sessions SET {sets} WHERE id=?", (*kw.values(), sid))

    def claim_tick(self, sid: int, real_now: float, clock_now: float) -> bool:
        """Verrou atomique : un seul scan à la fois, et seulement s'il est dû."""
        n = self.execute(
            """UPDATE sessions SET tick_lock=? WHERE id=? AND status='running' AND next_tick_at<=?
               AND (tick_lock IS NULL OR tick_lock<?)""",
            (real_now, sid, clock_now, real_now - 90),
        )
        return n == 1

    def get_session(self, sid: int) -> dict | None:
        r = self.one("SELECT * FROM sessions WHERE id=?", (sid,))
        return _session_dict(r) if r else None

    def running_session(self) -> dict | None:
        r = self.one("SELECT * FROM sessions WHERE status='running' ORDER BY id DESC LIMIT 1")
        return _session_dict(r) if r else None

    def latest_session(self) -> dict | None:
        r = self.one("SELECT * FROM sessions ORDER BY id DESC LIMIT 1")
        return _session_dict(r) if r else None

    def list_sessions(self) -> list[dict]:
        rows = self.all(
            """SELECT s.*, (SELECT COUNT(*) FROM items i WHERE i.session_id=s.id) AS n_items
               FROM sessions s ORDER BY s.created_at DESC""")
        return [_session_dict(r) for r in rows]

    def delete_session(self, sid: int):
        with self.lock:
            self.execute("DELETE FROM snapshots WHERE session_id=?", (sid,))
            self.execute("DELETE FROM items WHERE session_id=?", (sid,))
            self.execute("DELETE FROM sessions WHERE id=?", (sid,))

    # ------------------------------------------------------------------ annonces
    def items(self, sid: int) -> list[dict]:
        return self.all("SELECT * FROM items WHERE session_id=?", (sid,))

    def snapshot_count(self, sid: int) -> int:
        return self.one("SELECT COUNT(*) AS n FROM snapshots WHERE session_id=?", (sid,))["n"]

    def upsert_items(self, rows: list[dict]):
        upd = ",".join(f"{c}=excluded.{c}" for c in
                       ("last_age", "last_fav", "last_views", "window_fav", "window_age"))
        for chunk in _chunks(rows, 400):
            ph = ",".join("(" + ",".join("?" for _ in ITEM_COLS) + ")" for _ in chunk)
            params = [r.get(c) for r in chunk for c in ITEM_COLS]
            self.execute(f"INSERT INTO items ({','.join(ITEM_COLS)}) VALUES {ph} "
                         f"ON CONFLICT (session_id, item_id) DO UPDATE SET {upd}", params)

    def insert_snapshots(self, sid: int, snaps: list[dict]):
        for chunk in _chunks(snaps, 1000):
            ph = ",".join("(?,?,?,?,?,?)" for _ in chunk)
            params = [v for s in chunk for v in (sid, s["item_id"], s["ts"], s["age"], s["favs"], s["views"])]
            self.execute(f"INSERT INTO snapshots (session_id,item_id,ts,age_s,favs,views) VALUES {ph}", params)


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _is_conn_error(e: Exception) -> bool:
    name = type(e).__name__
    return name in ("OperationalError", "InterfaceError") or "closed" in str(e).lower()


def _session_dict(r: dict) -> dict:
    d = dict(r)
    d["filters"] = json.loads(d.pop("filters_json"))
    d["warnings"] = json.loads(d.get("warnings") or "[]")
    d["last_cycle"] = json.loads(d.get("last_cycle") or "{}")
    d["summary"] = json.loads(d["summary"]) if d.get("summary") else None
    d.pop("cookies", None)  # jamais renvoyé à l'interface
    return d


# ---------------------------------------------------------------------- calculs
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
