"""Exploration instantanée : radar « toutes catégories », recherche par favoris/âge, méthode d'Evan.

Tout est sans état côté serveur : la page orchestre les appels (une page de résultats à la fois) et
garde les données dans le navigateur.
"""
from __future__ import annotations

import hashlib
import math
import random
import statistics
import time

from . import idclock, vinted

BRANDS_API = vinted.BASE + "/api/v2/brands"
FILTERS_SEARCH = "https://api.vinted.fr/svc-filters/filters/search"

# Marques citées par Evan Martin comme « ultra concurrentielles » (à éviter en niche).
ULTRA_COMPETITIVE = ["Stone Island", "Moncler", "C.P. Company", "Levi's", "Lacoste", "Patagonia", "Nike", "Carhartt"]

_anchor = {"t": 0.0, "id": 0}


def _fresh_anchor(client) -> None:
    """Repère (heure, plus haut ID de tout Vinted), rafraîchi au plus toutes les 60 s."""
    if time.time() - _anchor["t"] < 60 or getattr(client, "demo", False):
        return
    raw = client.fetch_page({}, 1)
    ids = [int(r["id"]) for r in raw if r.get("id") and not r.get("promoted")]
    if ids:
        _anchor.update(t=time.time(), id=max(ids))
        idclock.add([[_anchor["t"], _anchor["id"]]])


def _sellers_concentration(raw: list[dict]) -> float | None:
    """Part des annonces détenues par des vendeurs qui en ont plusieurs dans la page (signe de revendeurs).
    Calculée à la volée : aucun identifiant vendeur n'est conservé ni renvoyé."""
    users = [((r.get("user") or {}).get("id")) for r in raw]
    users = [u for u in users if u]
    if len(users) < 10:
        return None
    counts: dict = {}
    for u in users:
        counts[u] = counts.get(u, 0) + 1
    return round(sum(c for c in counts.values() if c > 1) / len(users), 3)


def page(client, filters: dict, order: str, page_no: int, anchors=None, demo_now: float | None = None) -> dict:
    """Une page de résultats avec, pour chaque annonce, sa date de mise en ligne estimée."""
    if anchors:
        idclock.add(anchors)
    _fresh_anchor(client)
    extra = {"order": order}
    raw = client.fetch_page(filters, page_no, extra)
    now = demo_now or time.time()
    items = []
    for r in raw:
        it = vinted.normalize_item(r)
        if not it:
            continue
        posted = it["posted_at"] or (r.get("_demo_ts") or idclock.to_time(it["item_id"]))
        items.append({k: it[k] for k in ("item_id", "title", "brand", "price", "size", "condition", "url",
                                          "photo_url", "favs", "promoted")} | {
            "posted_at": posted, "age_min": round((now - posted) / 60, 1)})
    pag = getattr(client, "last_pagination", {}) or {}
    return {"items": items, "total": pag.get("total_entries"), "page": page_no, "now": now,
            "anchor": [_anchor["t"], _anchor["id"]] if _anchor["id"] else None,
            "clock_from": idclock.coverage_start()}


def _price_stats(prices: list[float]) -> dict:
    ps = sorted(p for p in prices if p)
    if len(ps) < 5:
        return {"n": len(ps)}
    q = lambda f: ps[min(len(ps) - 1, int(f * len(ps)))]  # noqa: E731
    med = statistics.median(ps)
    return {"n": len(ps), "median": med, "p25": q(0.25), "p75": q(0.75),
            "spread": round((q(0.75) - q(0.25)) / med, 2) if med else None}


def _flow_per_day(items: list[dict], total: int | None, now: float) -> float | None:
    """Annonces publiées par jour dans la niche : sur la page « plus récentes », N annonces couvrent
    l'âge de la plus ancienne d'entre elles (datée par l'horloge des IDs)."""
    ids = sorted((i["item_id"] for i in items if not i.get("promoted")), reverse=True)
    if len(ids) < 3:
        return 0.0
    age_days = max((now - idclock.to_time(ids[-1])) / 86400, 1 / 1440)
    return round(len(ids) / age_days, 1)


def brand_info(client, brand: str, catalog_id: int | None, price_from=None, price_to=None, status_ids=None) -> dict:
    """Mesures d'une niche « marque × catégorie » (méthode d'Evan) : volume de la marque, annonces dans la
    niche et dans la tranche de prix, flux (annonces publiées / jour), prix (médiane, quartiles, dispersion),
    part de revendeurs. 3 requêtes."""
    if getattr(client, "demo", False):
        return _demo_brand_info(brand, catalog_id, price_from, price_to)
    out: dict = {"brand": brand, "catalog_id": catalog_id}
    data = client.get_json(BRANDS_API, {"keyword": brand})
    cands = data.get("brands") or []
    best = next((b for b in cands if (b.get("title") or "").strip().lower() == brand.strip().lower()), None) or \
        (cands[0] if cands else None)
    if not best:
        out["error"] = "marque introuvable"
        return out
    out.update(brand_id=best["id"], brand_title=best.get("title"), brand_items=best.get("item_count"),
               brand_favourites=best.get("favourite_count"))
    base = {"brand_ids": [best["id"]], "catalog_ids": [catalog_id] if catalog_id else [], "status_ids": status_ids or []}
    norm = lambda raw: [x for x in (vinted.normalize_item(r) for r in raw) if x]  # noqa: E731
    time.sleep(random.uniform(0.5, 1.0))
    client.fetch_page(base, 1, {"order": "newest_first", "per_page": 20})
    out["niche_total"] = (getattr(client, "last_pagination", {}) or {}).get("total_entries")
    time.sleep(random.uniform(0.5, 1.0))
    raw = client.fetch_page(dict(base, price_from=price_from, price_to=price_to), 1, {"order": "newest_first"})
    items = norm(raw)
    out["niche_price_total"] = (getattr(client, "last_pagination", {}) or {}).get("total_entries")
    out["price_band_prices"] = _price_stats([i["price"] for i in items])
    out["niche_prices"] = out["price_band_prices"]
    out["resellers"] = _sellers_concentration(raw)
    out["flow_per_day"] = _flow_per_day(items, out["niche_price_total"], time.time())
    out["ultra_competitive"] = any(best.get("title", "").lower() == b.lower() for b in ULTRA_COMPETITIVE)
    return out


# ------------------------------------------------------------------ démo
def _h(*a) -> float:
    return int(hashlib.md5(":".join(map(str, a)).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def _demo_brand_info(brand, catalog_id, pf, pt) -> dict:
    x = _h(brand)
    items = int(200 + 40000 * x ** 2)
    niche = min(960, int(items * (0.02 + 0.2 * _h(brand, catalog_id))))
    med = 15 + 120 * _h(brand, "p")
    spread = round(0.2 + 1.2 * _h(brand, "s"), 2)
    return {"brand": brand, "catalog_id": catalog_id, "brand_id": int(x * 1e6), "brand_title": brand,
            "brand_items": items, "brand_favourites": int(items * 0.05), "niche_total": niche,
            "niche_prices": {"n": 96, "median": round(med, 1), "p25": round(med * (1 - spread / 2), 1),
                             "p75": round(med * (1 + spread / 2), 1), "spread": spread},
            "niche_price_total": int(niche * 0.6), "price_band_prices": {"n": 96, "median": round(med, 1), "spread": spread},
            "resellers": round(0.4 * _h(brand, "r"), 3), "flow_per_day": round(2 + 120 * _h(brand, catalog_id, "f") ** 2, 1),
            "ultra_competitive": any(brand.lower() == b.lower() for b in ULTRA_COMPETITIVE)}


class DemoExplore:
    """Résultats simulés sur 7 jours, à heures de mise en ligne fixes (une même annonce peut être revue
    plusieurs fois, comme sur Vinted) — pour tester l'interface sans appeler Vinted."""
    demo = True
    last_pagination: dict = {}
    BUCKET = 300  # une « tranche » de 5 min, 4 annonces en moyenne

    def __init__(self, now: float, seed: int = 7):
        self.now, self.seed = now, seed

    def _bucket(self, b: int, cat) -> list[dict]:
        rng = random.Random(f"{self.seed}:{cat}:{b}")
        out = []
        for k in range(rng.randint(2, 6)):
            brand, heat, pmean, _ = rng.choice(vinted.DEMO_BRANDS)
            ts = b * self.BUCKET + rng.random() * self.BUCKET
            out.append({"id": int(ts * 1000) * 100 + k, "brand": brand, "ts": ts, "q": rng.lognormvariate(0, 0.6),
                        "heat": heat, "price": round(max(3, rng.lognormvariate(math.log(pmean), 0.4)))})
        return out

    def _favs(self, i) -> int:
        age_h = max(0.0, (self.now - i["ts"]) / 3600)
        return int(i["heat"] * i["q"] * (12 * (1 - math.exp(-age_h * 60 / 12)) + 30 * (1 - math.exp(-age_h / 8))))

    def fetch_page(self, filters: dict, page_no: int, extra=None) -> list[dict]:
        order = (extra or {}).get("order", "newest_first")
        cat = (filters.get("catalog_ids") or [None])[0]
        pf, pt = filters.get("price_from"), filters.get("price_to")
        b_now = int(self.now // self.BUCKET)
        horizon = 7 * 86400 // self.BUCKET if order == "relevance" else 30
        pool = [i for b in range(b_now - horizon, b_now + 1) for i in self._bucket(b, cat)
                if i["ts"] <= self.now and (pf is None or i["price"] >= pf) and (pt is None or i["price"] <= pt)]
        for i in pool:
            i["favs"] = self._favs(i)
        if order == "relevance":  # récence + popularité, comme le tri « pertinence »
            pool.sort(key=lambda i: -(i["favs"] + 2) / ((self.now - i["ts"]) / 3600 + 0.5) ** 1.0)
        else:
            pool.sort(key=lambda i: -i["ts"])
        self.last_pagination = {"total_entries": min(960, len(pool))}
        chunk = pool[(page_no - 1) * 96: page_no * 96]
        return [{"id": i["id"], "title": f"{i['brand'] or 'Pièce'} – article", "price": {"amount": str(i["price"]), "currency_code": "EUR"},
                 "item_box": {"first_line": i["brand"] or "Pièce", "second_line": "M · Très bon état"},
                 "url": f"/items/{i['id']}-demo", "favourite_count": i["favs"], "view_count": 0, "promoted": False,
                 "photo": {"url": f"/demo-photo/{i['id']}.svg?b={i['brand'] or 'NA'}", "high_resolution": {}},
                 "_demo_ts": i["ts"]} for i in chunk]
