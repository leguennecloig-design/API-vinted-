"""Veille sans état en mémoire : chaque « tick » fait au plus un scan, puis rend la main.

Compatible serverless (Vercel) : tout l'état (prochain scan, backoff, cookies…) est en base.
Les ticks viennent de la page ouverte, d'un cron externe, ou d'une boucle locale (start.bat).
"""
from __future__ import annotations

import json
import os
import logging
import random
import time

from . import vinted
from . import measure
from .db import DB, brand_stats, enrich_items

log = logging.getLogger("tracker")

MAX_PAGES = 10          # Vinted refuse les pages au-delà de 10 (960 résultats)
BAND_CAPACITY = 800     # profondeur utile par tranche de prix (marge sous les 960 résultats accessibles)
MAX_BANDS = 6
PROBE_EVERY_S = float(os.environ.get("VNW_PROBE_S", "15"))      # écart mini entre deux sondes de la page 1
EXPECT_BATCH_S = 100     # au-delà, un nouveau lot d'index Vinted est attendu (période ~2-2,7 min)
DRAFT_GAP_S = 360        # écart ID/1re apparition au-delà duquel une annonce est un brouillon publié tard
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
    summary = measure.session_summary(items, s)
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
        baseline = s.get("id_floor") is None
        _cycle(db, s, client, upd)
        upd.update(backoff_i=0, api_level="ok",
                   api_text="OK — référence prise, la captation commence" if baseline else "OK")
        now = clock_now(s)
        lc = upd.get("last_cycle") or {}
        prev_lc = s.get("last_cycle") or {}
        if lc.get("probe") and not lc.get("probe_hit") and prev_lc.get("full_next"):
            full_next = prev_lc["full_next"]  # sonde sans nouveau lot : le scan complet prévu est inchangé
            upd["api_text"] = "OK — en attente du prochain lot Vinted"
        else:
            full_next = now + (20 if baseline else s["interval_s"] + random.uniform(0, 6))
            full_next = schedule_next(now, full_next, (s.get("_rows") or {}).values(), s["window_min"] * 60)
        nxt, is_probe = (full_next, False) if baseline else plan_next(now, full_next, s.get("_calib") or [])
        lc.update(next_probe=is_probe, full_next=full_next)
        upd["last_cycle"] = lc
        upd["next_tick_at"] = min(nxt, s["final_end"] + 1)
    except vinted.RateLimited as e:
        delay = BACKOFF[min(s["backoff_i"], len(BACKOFF) - 1)]
        upd.update(backoff_i=s["backoff_i"] + 1, api_level="warn", next_tick_at=clock_now(s) + delay,
                   api_text=f"Vinted limite les requêtes (HTTP {e.status}) — pause {delay} s"
                            + (f" (essai {s['backoff_i'] + 1})" if s["backoff_i"] else ""))
        client.warmed = False
        cookies = None
        log.warning("HTTP %s, backoff %ss", e.status, delay)
    except StaleResults as e:
        upd.update(api_level="warn", api_text=f"{e}", next_tick_at=clock_now(s) + 15)
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


class StaleResults(vinted.ApiError):
    """Vinted a servi une copie en cache plus ancienne que le scan précédent."""


def scan_pages(client, filters: dict, active_ids: set[int], max_seen: int | None, now_fn, sleep_fn,
               out: list, meta: dict, warn, max_pages: int = MAX_PAGES) -> None:
    """Pagine une recherche (du plus récent au plus ancien) et remplit `out` (annonces + heure du relevé).

    Réalité de l'API svc-catalogue (mesurée en nov. 2026) : l'ordre « newest_first » est approximatif
    (quelques annonces anciennes « remontées » s'intercalent), Vinted sert parfois une copie en cache
    plus ancienne, et refuse d'aller au-delà de la page 10. D'où :
    - page 1 plus ancienne que le scan précédent (ID max < max_seen) : on redemande (3 essais) ;
    - on pagine jusqu'à avoir revu toutes les annonces en cours de mesure (`active_ids`), puis on
      s'arrête dès que la page retombe dans des IDs déjà connus (<= max_seen) ;
    - page 10 atteinte avec des annonces encore à revoir : meta["limited"] (=> découpage en tranches).
    `out` est rempli même si une exception interrompt le scan. meta["t_first"]/["max_first"] = heure et
    plus haut ID de la page 1, qui servent à dater les annonces.
    """
    seen: set[int] = set()
    deadline = meta.setdefault("deadline", time.time() + TICK_BUDGET_S)
    for page in range(1, max_pages + 1):
        for attempt in range(1, 4):
            meta["requests"] = meta.get("requests", 0) + 1
            try:
                raw = client.fetch_page(filters, page)
            except vinted.PageLimit:
                meta["limited"] = meta.get("limited", 0) + bool(active_ids - seen)
                return
            now = now_fn()
            items = [it for it in (vinted.normalize_item(r) for r in raw) if it]
            fresh_ids = [i["item_id"] for i in items if not i["promoted"]]
            if page == 1 and meta.get("expect_new") and max_seen and fresh_ids and max(fresh_ids) == max_seen \
                    and attempt == 1 and not meta.get("rechecked"):
                # Aucun nouveau lot alors qu'un est attendu : c'est peut-être une copie en cache du lot
                # précédent (indétectable autrement). On redemande une fois la page 1.
                meta["rechecked"] = True
                meta["stale"] = meta.get("stale", 0) + 1
                sleep_fn(random.uniform(1.0, 2.0))
                continue
            if page > 1 or not max_seen or not fresh_ids or max(fresh_ids) >= max_seen:
                break
            meta["stale"] = meta.get("stale", 0) + 1
            if attempt == 3:
                raise StaleResults("Vinted renvoie des résultats en cache, nouvel essai au prochain scan")
            sleep_fn(random.uniform(1.0, 2.0))
        meta["pages"] = meta.get("pages", 0) + 1
        if page == 1 and fresh_ids:
            meta.setdefault("t_first", now)
            meta["max_first"] = max(meta.get("max_first") or 0, max(fresh_ids))
        for it in items:
            if it["item_id"] in seen:
                continue
            seen.add(it["item_id"])
            out.append({**it, "seen_at": now})
        # Dernière page = nettement moins de 96 résultats. Une page à 94-95 est normale : Vinted retire
        # de sa copie en cache les annonces vendues entre-temps.
        if len(raw) < vinted.PER_PAGE * 0.8 or not fresh_ids:
            return
        fresh_ids.sort()
        remaining = active_ids - seen
        if remaining:
            # On continue tant qu'on n'est pas nettement passé sous les annonces encore à revoir
            # (90 % de la page sous la plus ancienne d'entre elles = elles ont été vendues/supprimées).
            if fresh_ids[int(len(fresh_ids) * 0.9)] < min(remaining):
                return
        elif max_seen is None or fresh_ids[len(fresh_ids) // 2] <= max_seen:
            return  # plus rien à revoir, et on est revenu dans des annonces déjà connues
        if page == max_pages:
            meta["limited"] = meta.get("limited", 0) + bool(remaining)
            return
        if time.time() > deadline:
            warn("Scan coupé faute de temps (limite serverless) : resserre les filtres.")
            return
        sleep_fn(random.uniform(0.8, 1.8))


def needed_depth(items, s: dict, now: float) -> dict:
    """Profondeur à parcourir, par catégorie, pour revoir la plus ancienne annonce encore à mesurer.

    Vinted continue de publier en haut des résultats (y compris après la fin de la captation) : la
    profondeur ≈ rythme de mise en ligne observé (dans la catégorie) × âge de la plus ancienne annonce active.
    Renvoie {catégorie (ou None): profondeur}.
    """
    items = list(items)
    W = s["window_min"] * 60
    elapsed = max(60.0, min(now, s["capture_end"]) - s["start_ts"])
    out: dict = {}
    for cat in {i.get("catalog_id") for i in items}:
        its = [i for i in items if i.get("catalog_id") == cat]
        act = [i for i in its if now - i["posted_at"] <= W + 150]
        if not act:
            continue
        rate = sum(1 for i in its if i["posted_at"] <= s["capture_end"]) / (elapsed / 60)
        oldest = max(now - i["posted_at"] for i in act) / 60
        out[cat] = int(max(len(act) + 150, rate * oldest * 1.1 + 50))
    return out


def batch_period(calib: list) -> float:
    """Période de publication des lots d'index Vinted, apprise (médiane des écarts), 150 s par défaut."""
    gaps = sorted(b[0] - a[0] for a, b in zip(calib[1:], calib[2:]) if b[0] > a[0])
    return min(300.0, max(60.0, gaps[len(gaps) // 2])) if len(gaps) >= 2 else 150.0


def plan_next(now: float, full_next: float, calib: list, probe_done: bool = False) -> tuple[float, bool]:
    """Prochain passage : (heure, sonde ?). Si un nouveau lot d'index est attendu avant le prochain scan
    complet, on « sonde » la page 1 toutes les 15 s autour de l'heure attendue pour le voir au plus tôt."""
    if not calib:
        return full_next, False
    expected = calib[-1][0] + batch_period(calib)
    probe_at = max(now + PROBE_EVERY_S, expected - 5)
    if probe_at < full_next - PROBE_EVERY_S and now < expected + 120:
        return probe_at, True
    return full_next, False


def plan_bands(filters: dict, active_prices: list[float], depth: int | None = None) -> list[tuple]:
    """Découpe la recherche en tranches de prix si trop d'annonces sont à suivre pour 10 pages.

    Vinted ne sert que 960 résultats par recherche. Sur une catégorie large (ex. 80 nouvelles annonces
    par minute), une fenêtre de 15 min en demande ~1 300 : on découpe alors en tranches de prix à
    effectifs égaux (quantiles des prix des annonces suivies), chacune avec ses propres 10 pages.
    """
    pf, pt = filters.get("price_from"), filters.get("price_to")
    need = max(len(active_prices) + 150, depth or 0)
    n = min(MAX_BANDS, max(1, -(-need // BAND_CAPACITY)))
    if n == 1:
        return [(pf, pt)]
    ps = sorted(p for p in active_prices if p is not None)
    cuts = []
    for k in range(1, n):
        b = round(ps[len(ps) * k // n] * 2) / 2  # arrondi à 0,50 €
        if (pf is None or b > pf) and (pt is None or b < pt) and (not cuts or b > cuts[-1]):
            cuts.append(b)
    edges = [pf, *cuts, pt]
    return [(edges[k], (round(edges[k + 1] - 0.01, 2) if edges[k + 1] is not None and k + 1 < len(cuts) + 1 else edges[k + 1]))
            for k in range(len(edges) - 1)]


def scan_all(client, filters: dict, active: dict, max_seen: int | None, now_fn, sleep_fn,
             out: list, meta: dict, warn, baseline: bool = False, depth=None,
             expect_new: bool = False, probe: bool = False) -> None:
    """Scan complet : une recherche par catégorie choisie, chacune découpée en tranches de prix si besoin.

    `active` = {id: (prix, catégorie)} des annonces en cours de mesure. Chaque annonce vue reçoit
    it["cat"] = catégorie de la recherche qui l'a trouvée.
    Sonde (`probe`) : seulement la page 1 de la catégorie la plus active ; si un nouveau lot d'index y
    apparaît, on enchaîne aussitôt sur le scan complet, sinon on s'arrête là (1 requête).
    """
    act_info = {i: (v if isinstance(v, (tuple, list)) else (v, None)) for i, v in active.items()}
    cats = list(filters.get("catalog_ids") or []) or [None]
    multi = len(cats) > 1
    depth = depth if isinstance(depth, dict) else {None: depth} if depth else {}
    by_cat = {c: {i: p for i, (p, ic) in act_info.items() if not multi or ic == c} for c in cats}
    seen: set[int] = set()

    def run(cat, lo, hi, act, ms, max_pages):
        f = dict(filters, price_from=lo, price_to=hi)
        if cat is not None:
            f["catalog_ids"] = [cat]
        part: list = []
        try:
            scan_pages(client, f, act, ms, now_fn, sleep_fn, part, meta, warn, max_pages=max_pages)
        finally:
            for it in part:
                if it["item_id"] not in seen:
                    seen.add(it["item_id"])
                    out.append({**it, "cat": cat})

    if probe and not baseline and max_seen:
        c0 = max(cats, key=lambda c: len(by_cat[c]))
        before = meta.get("max_first") or 0
        run(c0, filters.get("price_from"), filters.get("price_to"), set(), max_seen, 1)
        meta["probe"] = True
        if (meta.get("max_first") or before) <= max_seen:
            meta["bands"] = 0
            return  # pas de nouveau lot : la sonde s'arrête là
        meta["probe_hit"] = True
        sleep_fn(random.uniform(0.5, 1.0))
    meta["expect_new"] = expect_new and not baseline and not meta.get("probe_hit")
    units = []
    for c in cats:
        prices = by_cat[c]
        bands = [(filters.get("price_from"), filters.get("price_to"))] if baseline else \
            plan_bands(filters, list(prices.values()), depth.get(c if multi else None, depth.get(c)))
        units += [(c, lo, hi, prices) for lo, hi in bands]
    meta["bands"] = len(units)
    for k, (c, lo, hi, prices) in enumerate(units):
        act = {i for i, p in prices.items() if p is None or ((lo is None or p >= lo) and (hi is None or p <= hi))}
        # Contrôle « copie en cache » par recherche : comparé à la plus récente annonce suivie de la recherche.
        ms = (max(act) if act else None) if len(units) > 1 else max_seen
        if k > 0:
            meta["expect_new"] = False
        run(c, lo, hi, act, ms, 1 if baseline else MAX_PAGES)
        if k + 1 < len(units):
            sleep_fn(random.uniform(0.8, 1.8))
    if meta.get("limited"):
        warn("Limite de Vinted (10 pages par recherche) atteinte : certaines annonces ont pu manquer des "
             "relevés. L'app découpe automatiquement en tranches de prix ; tu peux aussi resserrer les filtres.")


def schedule_next(now: float, nxt: float, items, window_s: float) -> float:
    """Avance le prochain scan pour qu'aucun palier (5, 10, 15 min…) ne tombe trop loin d'un relevé.

    Pour chaque palier c d'une annonce : si l'instant c + 60 s tombe avant le prochain scan prévu, on
    vise c + 30 s, sans jamais descendre sous 45 s d'intervalle (anti-blocage).
    """
    cps = measure.checkpoints(int(round(window_s / 60)))
    targets = [i["posted_at"] + c * 60 + 30 for i in items for c in cps
               if now < i["posted_at"] + c * 60 + 60 < nxt]
    if targets:
        return min(nxt, max(now + MIN_INTERVAL, min(targets)))
    return nxt


def active_ids(items, now: float, window_s: float) -> set[int]:
    """Annonces dont la mesure n'est pas finie (âge <= fenêtre + 2 min) : à revoir à chaque scan."""
    return {i["item_id"] for i in items if now - i["posted_at"] <= window_s + 150}


def fit_calib(calib: list, n: int = 20):
    """Droite robuste heure = a + k × ID sur les derniers points de calibration (Theil-Sen).

    `calib` = [(heure estimée de publication d'un lot d'index, plus haut ID du lot), ...]. Les IDs Vinted
    suivent un rythme global quasi constant sur quelques dizaines de minutes : une droite ajustée de façon
    robuste (médiane des pentes, médiane des résidus) neutralise les lots vus en retard (copie en cache,
    scan long…). Mesuré en démo : biais ~0 s, 99 % des annonces datées à ±1 min.
    """
    pts = calib[-n:]
    slopes = sorted((pts[j][0] - pts[i][0]) / (pts[j][1] - pts[i][1])
                    for i in range(len(pts)) for j in range(i + 1, len(pts)) if pts[j][1] > pts[i][1])
    if not slopes:
        return None
    k = slopes[len(slopes) // 2]
    res = sorted(t - i * k for t, i in pts)
    a = res[(len(res) - 1) // 2]
    return lambda item_id: a + item_id * k


def estimate_posted(item_id: int, calib: list, fit=None) -> float | None:
    """Heure de mise en ligne estimée à partir de l'ID (voir fit_calib)."""
    fit = fit or fit_calib(calib)
    return fit(item_id) if fit else None


def classify(seen: list[dict], rows: dict, s: dict, calib: list) -> int:
    """Met à jour `rows` (item_id -> ligne) avec un scan. Renvoie le nombre de nouvelles annonces.

    Nouvelle annonce = ID jamais vu et supérieur au plancher (plus haut ID au scan de référence).
    """
    W = s["window_min"] * 60
    floor = s.get("id_floor") or 0
    new = 0
    fit = fit_calib(calib)
    for it in seen:
        iid, now = it["item_id"], it["seen_at"]
        row = rows.get(iid)
        if row is None:
            if iid <= floor:
                continue  # déjà en ligne avant la veille (ou ancienne annonce remontée)
            posted = it["posted_at"] or estimate_posted(iid, calib, fit) or now
            # Brouillon publié tard : son ID (attribué à la création) le daterait bien avant sa 1re
            # apparition. Au-delà de 6 min d'écart, on le date au lot d'index précédant cette apparition.
            # En deçà, c'est une annonce simplement vue un lot en retard : la datation par ID est la bonne.
            if now - posted > DRAFT_GAP_S:
                posted = calib[-2][0] if len(calib) >= 2 else (calib[0][0] if calib else now)
            posted = min(posted, now)
            if not (s["start_ts"] <= posted <= s["capture_end"]):
                continue  # postée hors captation
            row = {c: it.get(c) for c in ("title", "brand", "price", "currency", "size",
                                            "condition", "url", "photo_url")}
            row["catalog_id"] = it.get("cat")
            row.update(session_id=s["id"], item_id=iid, posted_at=posted, first_seen=now,
                       first_age=now - posted, window_fav=None, window_age=None, cps={})
            new += 1
        else:
            row = dict(row, cps=dict(row.get("cps") or {}))
        age = now - row["posted_at"]
        row["cps"].pop("_m", None)  # revue : le compteur d'absences consécutives repart à zéro
        if age <= W + measure.MAX_GAP:  # les relevés au-delà ne servent plus à la mesure
            row.update(last_age=age, last_fav=it["favs"], last_views=it["views"])
            # Paliers 5 / 10 / 15 min… : favoris interpolés entre les relevés qui les encadrent.
            measure.update_cps(row["cps"], age, it["favs"], s["window_min"])
            row["window_fav"], row["window_age"] = measure.window_from_cps(row["cps"], s["window_min"])
        it["age"] = age
        rows[iid] = row
    return new


def count_misses(rows: dict, active: set[int], seen_ids: set[int], complete: bool) -> set[int]:
    """Compte, pour chaque annonce active absente d'un scan COMPLET (pagination allée jusqu'au bout, sans
    butée à la page 10), une absence « prouvée ». Deux absences prouvées = disparue (vendue ?)."""
    if not complete:
        return set()
    out = set()
    for iid in active - seen_ids:
        r = rows.get(iid)
        if r is not None:
            r["cps"] = dict(r.get("cps") or {})
            r["cps"]["_m"] = r["cps"].get("_m", 0) + 1
            out.add(iid)
    return out


def apply_scan(s: dict, rows: dict, seen: list[dict], meta: dict, calib: list) -> tuple[int, dict]:
    """Logique commune serveur / navigateur : met à jour plancher, calibration et annonces.
    Renvoie (nb de nouvelles, champs de session à enregistrer)."""
    upd: dict = {}
    ids = [i["item_id"] for i in seen if not i["promoted"]]
    if s.get("id_floor") is None:  # scan de référence : tout ce qui est en ligne est ignoré
        if meta.get("max_first"):
            floor = max(ids)
            upd.update(id_floor=floor, max_seen_id=floor, calib=[[meta["t_first"], floor]])
        return 0, upd
    if not calib:  # veille démarrée sur une version antérieure : calibration à partir du plancher
        calib = [[s.get("last_cycle_at") or s["start_ts"], s["id_floor"]]]
    if meta.get("max_first") and meta["max_first"] > calib[-1][1]:
        # Nouveau lot d'index : il a été publié entre le scan précédent (qui ne le voyait pas) et
        # celui-ci. On le date au milieu, ce qui supprime le retard moyen d'une demi-période de scan.
        prev = (s.get("last_cycle") or {}).get("t_first") or calib[-1][0]
        calib = [*calib, [(max(prev, calib[-1][0]) + meta["t_first"]) / 2, meta["max_first"]]]
    upd["calib"] = calib
    new = classify(seen, rows, s, calib)
    if ids:
        upd["max_seen_id"] = max([*ids, s.get("max_seen_id") or 0])
    return new, upd


def _cycle(db: DB, s: dict, client, upd: dict):
    existing = {r["item_id"]: r for r in db.items(s["id"])}
    seen: list[dict] = []
    meta = {"requests": s["requests"], "pages": 0}
    t0 = time.time()
    baseline = s.get("id_floor") is None
    active = set() if baseline else active_ids(existing.values(), clock_now(s), s["window_min"] * 60)
    completed = False
    new = 0
    rows = dict(existing)
    try:
        probe = bool((s.get("last_cycle") or {}).get("next_probe")) and not baseline
        scan_all(client, s["filters"], {i: (existing[i].get("price"), existing[i].get("catalog_id")) for i in active},
                 s.get("max_seen_id"), lambda: clock_now(s), lambda sec: _sleep(s, sec), seen, meta,
                 lambda m: _warn(db, s, m), baseline=baseline, depth=needed_depth(existing.values(), s, clock_now(s)),
                 expect_new=bool(s.get("calib")) and clock_now(s) - s["calib"][-1][0] > EXPECT_BATCH_S, probe=probe)
        completed = True
    finally:
        new, u = apply_scan(s, rows, seen, meta, s.get("calib") or [])
        s["_calib"] = u.get("calib", s.get("calib") or [])
        if "calib" in u:
            u["calib"] = json.dumps(u["calib"])
        upd.update(u)
        s["_rows"] = rows
        seen_ids = {i["item_id"] for i in seen}
        full = completed and not meta.get("limited") and not (meta.get("probe") and not meta.get("probe_hit"))
        missed_ids = count_misses(rows, active, seen_ids, full)
        changed = [r for iid, r in rows.items() if iid in seen_ids or iid in missed_ids]
        if changed and not baseline:
            db.upsert_items(changed)
        snaps = [{"item_id": i["item_id"], "ts": i["seen_at"], "age": i["age"], "favs": i["favs"],
                  "views": i["views"]} for i in seen if "age" in i]
        if snaps:
            db.insert_snapshots(s["id"], snaps)
        upd["requests"] = meta["requests"]
        missed = len(active - seen_ids)
        upd["last_cycle"] = {"pages": meta["pages"], "new": new, "snaps": len(snaps),
                             "duration_s": round(time.time() - t0, 1), "at": clock_now(s),
                             "baseline": baseline, "stale": meta.get("stale", 0), "missed": missed,
                             "bands": meta.get("bands", 1), "probe": bool(meta.get("probe")),
                             "probe_hit": bool(meta.get("probe_hit")),
                             "t_first": meta.get("t_first")}
    if completed and not (meta.get("probe") and not meta.get("probe_hit")):
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
    top = sorted(items, key=lambda i: (i["rank_value"] or 0, i["favs_per_min"] or 0), reverse=True)[:10]
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
        "n_hot": sum(1 for i in items if i["hot"]),
        "n_gone": sum(1 for i in items if i["gone"]),
        "checkpoints": measure.checkpoints(s["window_min"]),
        "alerts": [{"item_id": i["item_id"], "kind": "hit" if i["is_hit"] else "hot", "brand": i["brand"],
                    "price": i["price"], "favs": i["last_fav"], "age_min": i["age_min"], "url": i["url"],
                    "projected": i["projected"]} for i in items if (i["is_hit"] or i["hot"]) and i["in_progress"]],
        "top_items": top,
        "top_brands": brands[:10],
        "top_cats": measure.cat_stats(items, s["filters"].get("catalog_names"),
                                      max(1 / 60, (min(t, s["capture_end"]) - s["start_ts"]) / 3600))
        if len(s["filters"].get("catalog_ids") or []) > 1 else [],
    }
