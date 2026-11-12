"""Veille sans état en mémoire : chaque « tick » fait au plus un scan, puis rend la main.

Compatible serverless (Vercel) : tout l'état (prochain scan, backoff, cookies…) est en base.
Les ticks viennent de la page ouverte, d'un cron externe, ou d'une boucle locale (start.bat).
"""
from __future__ import annotations

import json
import logging
import random
import time

from . import vinted
from .db import DB, brand_stats, enrich_items

log = logging.getLogger("tracker")

MAX_PAGES = 15
BACKOFF = [30, 60, 120, 240, 480, 600]  # secondes, en cas de 403 / 429
MIN_INTERVAL = 45
TICK_BUDGET_S = 50       # temps réel max d'un scan (limite des fonctions serverless)
ABANDON_AFTER_S = 5 * 60  # sans scan depuis la fin de veille + 5 min : veille interrompue


class Busy(Exception):
    pass


def clock_now(s: dict) -> float:
    """Heure de la veille (accélérée en mode démo)."""
    if s["demo"]:
        return s["demo_t0"] + (time.time() - s["demo_t0"]) * s["speed"]
    return time.time()


def _sleep(s: dict, seconds: float):
    time.sleep(seconds / (s["speed"] if s["demo"] else 1))


# ---------------------------------------------------------------------- pilotage
def start(db: DB, filters: dict, duration_min: int, window_min: int, threshold: int,
          interval_s: int, demo: bool) -> int:
    cur = db.running_session()
    if cur:
        finalize_if_stale(db, cur)
        if db.running_session():
            raise Busy("Une veille est déjà en cours. Arrête-la d'abord.")
    real = time.time()
    speed = vinted.DEMO_SPEED if demo else 1.0
    start_ts = real
    return db.create_session(
        created_at=real, start_ts=start_ts, capture_end=start_ts + duration_min * 60,
        final_end=start_ts + (duration_min + window_min) * 60, status="running",
        demo=int(demo), speed=speed, demo_t0=real if demo else None, seed=random.randint(1, 10**9),
        filters=filters, filters_label=vinted.filters_label(filters), duration_min=duration_min,
        window_min=window_min, hit_threshold=threshold, interval_s=max(MIN_INTERVAL, interval_s),
        next_tick_at=0, api_level="ok", api_text="Démarrage… premier scan imminent",
    )


def stop(db: DB, sid: int | None = None):
    s = db.get_session(sid) if sid else db.running_session()
    if s and s["status"] == "running":
        finish(db, s, "stopped")


def finish(db: DB, s: dict, status: str):
    items = enrich_items(db.items(s["id"]), s["window_min"], s["hit_threshold"])
    brands = brand_stats(items)
    summary = {"n_hits": sum(1 for i in items if i["is_hit"]),
               "top_brands": [{"brand": b["brand"], "score": b["score"], "hits": b["hits"]} for b in brands[:3]]}
    text = {"done": "Veille terminée", "stopped": "Veille arrêtée", "error": "Veille arrêtée sur erreur",
            "interrupted": "Veille interrompue (plus aucun scan)"}[status]
    db.update_session(s["id"], status=status, ended_at=time.time(), api_level="idle", api_text=text,
                      summary=summary, tick_lock=None, cookies=None)


def finalize_if_stale(db: DB, s: dict) -> bool:
    """Veille abandonnée (plus aucun tick bien après la fin) : on la clôt."""
    now = clock_now(s)
    if now > s["final_end"] + ABANDON_AFTER_S:
        last = s.get("last_cycle_at") or 0
        finish(db, s, "done" if last >= s["final_end"] else "interrupted")
        return True
    return False


# ---------------------------------------------------------------------- tick
def tick(db: DB) -> dict:
    """Fait un scan si la veille en cours en a besoin. Rapide (no-op) sinon."""
    s = db.running_session()
    if not s:
        return {"ran": False, "reason": "aucune veille en cours"}
    if finalize_if_stale(db, s):
        return {"ran": False, "reason": "veille clôturée"}
    now = clock_now(s)
    if not db.claim_tick(s["id"], time.time(), now):
        return {"ran": False, "reason": "pas encore l'heure du prochain scan"}
    # On relit la ligne complète (avec les cookies) après avoir pris le verrou.
    row = db.one("SELECT cookies FROM sessions WHERE id=?", (s["id"],))
    cookies = json.loads(row["cookies"]) if row and row.get("cookies") else None
    if s["demo"]:
        client = vinted.DemoClient(now, s["filters"], s["seed"], s["requests"])
    else:
        client = vinted.VintedClient(cookies)

    upd: dict = {"tick_lock": None}
    try:
        _cycle(db, s, client, upd)
        upd.update(backoff_i=0, api_level="ok", api_text="OK")
        nxt = clock_now(s) + s["interval_s"] + random.uniform(0, 6)
        upd["next_tick_at"] = min(nxt, s["final_end"] + 1)
    except vinted.RateLimited as e:
        delay = BACKOFF[min(s["backoff_i"], len(BACKOFF) - 1)]
        upd.update(backoff_i=s["backoff_i"] + 1, api_level="warn", next_tick_at=clock_now(s) + delay,
                   api_text=f"Vinted limite les requêtes (HTTP {e.status}) — pause {delay} s"
                            + (f" (essai {s['backoff_i'] + 1})" if s["backoff_i"] else ""))
        client.warmed = False
        cookies = None
        log.warning("HTTP %s, backoff %ss", e.status, delay)
    except vinted.ApiError as e:
        n = s["backoff_i"] + 1
        upd.update(backoff_i=n, api_level="error", api_text=f"Erreur API : {e} ({n}/10)",
                   next_tick_at=clock_now(s) + s["interval_s"])
        log.warning("API error: %s", e)
        if n >= 10:
            _warn(db, s, "Arrêt après 10 erreurs API consécutives.")
            db.update_session(s["id"], **upd)
            finish(db, db.get_session(s["id"]), "error")
            return {"ran": True, "status": "error"}
    except Exception as e:
        log.exception("Scan planté")
        upd.update(api_level="error", api_text=f"Erreur inattendue : {e}",
                   next_tick_at=clock_now(s) + s["interval_s"])
    if not client.demo:
        try:
            upd["cookies"] = json.dumps(client.export_cookies()) if client.warmed else None
        except Exception:
            pass
    db.update_session(s["id"], **upd)

    if clock_now(s) >= s["final_end"] and s.get("last_cycle_at_new", 0) >= s["final_end"]:
        finish(db, db.get_session(s["id"]), "done")
        return {"ran": True, "status": "done"}
    return {"ran": True, "status": "running"}


def _warn(db: DB, s: dict, msg: str):
    if msg not in s["warnings"]:
        s["warnings"].append(msg)
        db.update_session(s["id"], warnings=s["warnings"])


def scan_pages(client, filters: dict, start_ts: float, cutoff: float, now_fn, sleep_fn,
               out: list, meta: dict, warn) -> None:
    """Pagine du plus récent au plus ancien et remplit `out` (annonces normalisées + âge).

    S'arrête dès qu'on atteint des annonces plus vieilles que `cutoff` (fenêtre + 2 min) ou
    antérieures au début de la veille. `out` est rempli même si une exception interrompt le scan.
    """
    seen: set[int] = set()
    t0 = time.time()
    for page in range(1, MAX_PAGES + 1):
        meta["requests"] = meta.get("requests", 0) + 1
        raw = client.fetch_page(filters, page)
        meta["pages"] = page
        now = now_fn()
        items = [it for it in (vinted.normalize_item(r) for r in raw) if it]
        for it in items:
            if it["item_id"] in seen:
                continue
            seen.add(it["item_id"])
            out.append({**it, "age": now - it["posted_at"], "seen_at": now})
        # Condition d'arrêt sur la dernière annonce non sponsorisée de la page.
        ordered = [i for i in items if not i["promoted"]] or items
        if len(raw) < vinted.PER_PAGE or not ordered:
            return
        last = ordered[-1]
        if now - last["posted_at"] > cutoff or last["posted_at"] < start_ts:
            return
        if page == MAX_PAGES:
            warn(f"Limite de {MAX_PAGES} pages atteinte : catégorie trop active, "
                 "certaines annonces ont pu manquer des relevés. Resserre les filtres.")
            return
        if time.time() - t0 > TICK_BUDGET_S:
            warn("Scan coupé faute de temps (limite serverless) : resserre les filtres.")
            return
        sleep_fn(random.uniform(0.8, 1.8))


def _cycle(db: DB, s: dict, client, upd: dict):
    W = s["window_min"] * 60
    existing = {r["item_id"]: r for r in db.items(s["id"])}
    rows: dict[int, dict] = {}
    snaps: list[dict] = []
    seen: list[dict] = []
    meta = {"requests": s["requests"], "pages": 0}
    t0 = time.time()
    new = 0
    completed = False
    try:
        scan_pages(client, s["filters"], s["start_ts"], W + 120, lambda: clock_now(s),
                   lambda sec: _sleep(s, sec), seen, meta, lambda m: _warn(db, s, m))
        completed = True
    finally:
        for it in seen:
            iid, age, now = it["item_id"], it["age"], it["seen_at"]
            row = existing.get(iid)
            if row is None:
                # Uniquement les annonces postées pendant la captation.
                if not (s["start_ts"] <= it["posted_at"] <= s["capture_end"]):
                    continue
                row = {c: it.get(c) for c in ("title", "brand", "price", "currency", "size",
                                                "condition", "url", "photo_url", "posted_at")}
                row.update(session_id=s["id"], item_id=iid, first_seen=now, first_age=age,
                           window_fav=None, window_age=None)
                new += 1
            else:
                row = dict(row)
            row.update(last_age=age, last_fav=it["favs"], last_views=it["views"])
            # Favoris fenêtre = dernier relevé d'âge <= fenêtre + 60 s.
            if age <= W + 60 and (row["window_age"] is None or age >= row["window_age"]):
                row.update(window_fav=it["favs"], window_age=age)
            rows[iid] = row
            snaps.append({"item_id": iid, "ts": now, "age": age, "favs": it["favs"], "views": it["views"]})
        if rows:
            db.upsert_items(list(rows.values()))
        if snaps:
            db.insert_snapshots(s["id"], snaps)
        upd["requests"] = meta["requests"]
        upd["last_cycle"] = {"pages": meta["pages"], "new": new, "snaps": len(snaps),
                             "duration_s": round(time.time() - t0, 1), "at": clock_now(s)}
    if completed:
        at = clock_now(s)
        upd.update(cycles=s["cycles"] + 1, last_cycle_at=at)
        s["last_cycle_at_new"] = at


# ---------------------------------------------------------------------- état pour l'UI
def state(db: DB) -> dict:
    s = db.running_session()
    running = s is not None
    if s and finalize_if_stale(db, s):
        s, running = None, False
    if not s:
        s = db.latest_session()
    if not s:
        return {"running": False, "session": None}
    now = clock_now(s) if running else None
    items = enrich_items(db.items(s["id"]), s["window_min"], s["hit_threshold"], now)
    brands = brand_stats(items)
    top = sorted(items, key=lambda i: (i["window_fav"] if i["window_fav"] is not None else -1,
                                        i["favs_per_min"] or 0), reverse=True)[:10]
    t = now if now is not None else s["final_end"]
    if t < s["capture_end"]:
        phase = "capture"
        phase_prog = (t - s["start_ts"]) / (s["capture_end"] - s["start_ts"])
    elif t < s["final_end"]:
        phase = "window"
        phase_prog = (t - s["capture_end"]) / max(1, s["final_end"] - s["capture_end"])
    else:
        phase, phase_prog = "end", 1.0
    stalled = bool(running and now - (s["next_tick_at"] or now) > 90)
    return {
        "running": running,
        "session": s,
        "demo": bool(s["demo"]),
        "speed": s["speed"] if s["demo"] else 1,
        "now": t,
        "phase": phase,
        "phase_progress": round(min(1, max(0, phase_prog)), 4),
        "progress": round(min(1, max(0, (t - s["start_ts"]) / (s["final_end"] - s["start_ts"]))), 4),
        "remaining_s": max(0, s["final_end"] - t),
        "next_cycle_in": max(0, s["next_tick_at"] - t) if running and s["next_tick_at"] else None,
        "stalled": stalled,
        "api": {"level": s["api_level"], "text": s["api_text"]},
        "warnings": s["warnings"],
        "last_cycle": s["last_cycle"],
        "n_items": len(items),
        "n_hits": sum(1 for i in items if i["is_hit"]),
        "top_items": top,
        "top_brands": brands[:10],
    }
