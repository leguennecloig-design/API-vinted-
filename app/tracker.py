"""Boucle de veille : capte les nouvelles annonces et relève leurs favoris à chaque cycle."""
from __future__ import annotations

import asyncio
import logging
import random
import time

from . import vinted
from .db import DB, brand_stats, enrich_items

log = logging.getLogger("tracker")

MAX_PAGES = 15
BACKOFF = [30, 60, 120, 240, 480, 600]  # secondes, en cas de 403 / 429
MIN_INTERVAL = 45


class Tracker:
    """Une seule veille à la fois."""

    def __init__(self, db: DB):
        self.db = db
        self.task: asyncio.Task | None = None
        self.stop_event = asyncio.Event()
        self.reset_state()

    def reset_state(self):
        self.sid: int | None = None
        self.clock = vinted.RealClock()
        self.client = None
        self.p: dict = {}
        self.api = {"level": "idle", "text": "En attente"}
        self.warnings: list[str] = []
        self.last_cycle: dict = {}
        self.next_cycle_at: float | None = None
        self.known: set[int] = set()
        self.cycles = 0
        self.requests = 0

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    # ------------------------------------------------------------------ pilotage
    async def start(self, filters: dict, duration_min: int, window_min: int, threshold: int,
                    interval_s: int, demo: bool) -> int:
        if self.running:
            raise RuntimeError("Une veille est déjà en cours.")
        self.reset_state()
        self.stop_event = asyncio.Event()
        if demo:
            self.clock = vinted.DemoClock()
            self.client = vinted.DemoClient(self.clock, filters)
        else:
            self.clock = vinted.RealClock()
            self.client = vinted.VintedClient()
        start = self.clock.now()
        self.p = dict(filters=filters, duration_min=duration_min, window_min=window_min,
                      threshold=threshold, interval_s=max(MIN_INTERVAL, interval_s), demo=demo,
                      start=start, capture_end=start + duration_min * 60,
                      final_end=start + (duration_min + window_min) * 60)
        self.sid = self.db.create_session(
            start_ts=start, capture_end=self.p["capture_end"], final_end=self.p["final_end"],
            status="running", demo=int(demo), filters=filters,
            filters_label=vinted.filters_label(filters), duration_min=duration_min,
            window_min=window_min, hit_threshold=threshold, interval_s=self.p["interval_s"],
        )
        self.api = {"level": "ok", "text": "Démarrage…"}
        self.task = asyncio.create_task(self._run())
        return self.sid

    async def stop(self):
        if not self.running:
            return
        self.stop_event.set()
        try:
            await asyncio.wait_for(asyncio.shield(self.task), timeout=40)
        except asyncio.TimeoutError:
            self.task.cancel()

    async def _wait(self, seconds: float) -> bool:
        """Attend (en temps de veille). Renvoie True si on a demandé l'arrêt."""
        self.next_cycle_at = self.clock.now() + seconds
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=max(0.0, seconds) / self.clock.speed)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self.next_cycle_at = None

    # ------------------------------------------------------------------ boucle
    async def _run(self):
        status = "done"
        backoff_i = 0
        api_errors = 0
        try:
            while not self.stop_event.is_set():
                try:
                    await self._cycle()
                    backoff_i = api_errors = 0
                    self.api = {"level": "ok", "text": "OK"}
                except vinted.RateLimited as e:
                    delay = BACKOFF[min(backoff_i, len(BACKOFF) - 1)]
                    backoff_i += 1
                    self.api = {"level": "warn",
                                "text": f"Vinted limite les requêtes (HTTP {e.status}) — pause {delay} s"
                                        + (f" (essai {backoff_i})" if backoff_i > 1 else "")}
                    log.warning("HTTP %s, backoff %ss", e.status, delay)
                    if isinstance(self.client, vinted.VintedClient):
                        self.client.warmed = False  # nouveaux cookies au prochain essai
                    if await self._wait(delay):
                        break
                    continue
                except vinted.ApiError as e:
                    api_errors += 1
                    self.api = {"level": "error", "text": f"Erreur API : {e} ({api_errors}/10)"}
                    log.warning("API error: %s", e)
                    if api_errors >= 10:
                        status = "error"
                        self._warn("Arrêt après 10 erreurs API consécutives.")
                        break
                    if await self._wait(self.p["interval_s"]):
                        break
                    continue

                now = self.clock.now()
                if now >= self.p["final_end"]:
                    break
                wait = self.p["interval_s"] + random.uniform(0, 6)
                wait = min(wait, self.p["final_end"] - now + 1)
                if await self._wait(wait):
                    break
            if self.stop_event.is_set() and status == "done":
                status = "stopped"
        except asyncio.CancelledError:
            status = "stopped"
            raise
        except Exception as e:  # imprévu : on garde les données
            log.exception("Veille plantée")
            status = "error"
            self._warn(f"Erreur inattendue : {e}")
        finally:
            self.api = {"level": "idle", "text": {"done": "Veille terminée", "stopped": "Veille arrêtée",
                                                   "error": "Veille arrêtée sur erreur"}[status]}
            self.db.update_session(self.sid, status=status, ended_at=time.time(), cycles=self.cycles,
                                   requests=self.requests, warnings=self.warnings)

    def _warn(self, msg: str):
        if msg not in self.warnings:
            self.warnings.append(msg)
            self.db.update_session(self.sid, warnings=self.warnings)

    async def _fetch(self, page: int) -> list[dict]:
        self.requests += 1
        if self.client.demo:
            return self.client.fetch_page(self.p["filters"], page)
        return await asyncio.to_thread(self.client.fetch_page, self.p["filters"], page)

    async def _cycle(self):
        p = self.p
        cutoff = p["window_min"] * 60 + 120  # on pagine jusqu'à fenêtre + 2 min
        new_items, snaps, seen = [], [], set()
        t0 = time.time()
        pages = 0
        try:
            for page in range(1, MAX_PAGES + 1):
                self.api = {"level": "ok", "text": f"Scan en cours — page {page}"}
                raw = await self._fetch(page)
                pages = page
                now = self.clock.now()
                items = [it for it in (vinted.normalize_item(r) for r in raw) if it]
                for it in items:
                    iid = it["item_id"]
                    if iid in seen:
                        continue
                    seen.add(iid)
                    age = now - it["posted_at"]
                    if iid not in self.known:
                        # Uniquement les annonces postées pendant la captation.
                        if not (p["start"] <= it["posted_at"] <= p["capture_end"]):
                            continue
                        self.known.add(iid)
                        new_items.append({**it, "ts": now, "age": age})
                    snaps.append({"item_id": iid, "ts": now, "age": age,
                                  "favs": it["favs"], "views": it["views"]})
                # Condition d'arrêt sur la dernière annonce non sponsorisée de la page.
                ordered = [i for i in items if not i["promoted"]] or items
                if len(raw) < vinted.PER_PAGE or not ordered:
                    break
                last = ordered[-1]
                if now - last["posted_at"] > cutoff or last["posted_at"] < p["start"]:
                    break
                if page == MAX_PAGES:
                    self._warn(f"Limite de {MAX_PAGES} pages atteinte : catégorie trop active, "
                               "certaines annonces ont pu manquer des relevés. Resserre les filtres.")
                    break
                await self.clock.sleep(random.uniform(0.8, 1.8))
        finally:
            if new_items or snaps:
                self.db.record_cycle(self.sid, new_items, snaps, p["window_min"] * 60)
            self.cycles += 1
            self.last_cycle = {"pages": pages, "new": len(new_items), "snaps": len(snaps),
                               "duration_s": round(time.time() - t0, 1), "at": self.clock.now()}
            self.db.update_session(self.sid, cycles=self.cycles, requests=self.requests)

    # ------------------------------------------------------------------ état pour l'UI
    def state(self) -> dict:
        if self.sid is None:
            return {"running": False, "session": None}
        s = self.db.get_session(self.sid)
        if s is None:  # session supprimée
            return {"running": False, "session": None}
        now = self.clock.now() if self.running else None
        items = enrich_items(self.db.items(self.sid), s["window_min"], s["hit_threshold"], now)
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
        return {
            "running": self.running,
            "session": s,
            "demo": bool(s["demo"]),
            "speed": self.clock.speed,
            "now": t,
            "phase": phase,
            "phase_progress": round(min(1, max(0, phase_prog)), 4),
            "progress": round(min(1, max(0, (t - s["start_ts"]) / (s["final_end"] - s["start_ts"]))), 4),
            "remaining_s": max(0, s["final_end"] - t),
            "next_cycle_in": max(0, self.next_cycle_at - t) if (self.next_cycle_at and now) else None,
            "api": self.api,
            "warnings": self.warnings or s["warnings"],
            "last_cycle": self.last_cycle,
            "n_items": len(items),
            "n_hits": sum(1 for i in items if i["is_hit"]),
            "top_items": top,
            "top_brands": brands[:10],
        }
