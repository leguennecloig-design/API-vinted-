"""Accès à l'API Vinted (curl_cffi, empreinte Chrome) + flux simulé pour le mode démo."""
from __future__ import annotations

import math
import random
import re
import threading
import time
from urllib.parse import parse_qs, urlparse

BASE = "https://www.vinted.fr"
API = BASE + "/api/v2/catalog/items"
PER_PAGE = 96


class RateLimited(Exception):
    """403 / 429 : il faut ralentir."""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


class ApiError(Exception):
    pass


# ---------------------------------------------------------------- filtres
def parse_search_url(url: str) -> dict:
    """Extrait les filtres d'une URL de recherche vinted.fr.

    Gère catalog[]=, catalog_ids=, price_from, price_to, brand_ids[], size_ids[],
    status_ids[], search_text et le format /catalog/79-sweats.
    """
    u = urlparse(url.strip())
    qs = parse_qs(u.query, keep_blank_values=False)

    def ids(*keys: str) -> list[int]:
        out: list[int] = []
        for k in keys:
            for v in qs.get(k, []):
                for part in v.split(","):
                    part = part.strip()
                    if part.isdigit() and int(part) not in out:
                        out.append(int(part))
        return out

    catalog = ids("catalog[]", "catalog_ids[]", "catalog_ids", "catalog")
    m = re.search(r"/catalog/(\d+)", u.path)
    if m and int(m.group(1)) not in catalog:
        catalog.insert(0, int(m.group(1)))

    def num(key: str) -> float | None:
        v = (qs.get(key) or [None])[0]
        try:
            return float(v.replace(",", ".")) if v else None
        except ValueError:
            return None

    return {
        "catalog_ids": catalog,
        "price_from": num("price_from"),
        "price_to": num("price_to"),
        "brand_ids": ids("brand_ids[]", "brand_ids", "brand_id"),
        "size_ids": ids("size_ids[]", "size_ids"),
        "status_ids": ids("status_ids[]", "status_ids"),
        "search_text": (qs.get("search_text") or [""])[0].strip(),
    }


def filters_label(f: dict) -> str:
    parts = []
    if f.get("search_text"):
        parts.append(f"« {f['search_text']} »")
    if f.get("catalog_ids"):
        parts.append("cat. " + ",".join(map(str, f["catalog_ids"])))
    pf, pt = f.get("price_from"), f.get("price_to")
    if pf is not None or pt is not None:
        parts.append(f"{_fmt(pf) if pf is not None else '0'}–{_fmt(pt) if pt is not None else '∞'} €")
    for key, lab in (("brand_ids", "marques"), ("size_ids", "tailles"), ("status_ids", "états")):
        if f.get(key):
            parts.append(f"{len(f[key])} {lab}")
    return " · ".join(parts) or "Tout Vinted"


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.2f}".replace(".", ",")


def api_params(f: dict, page: int) -> dict:
    p = {"page": page, "per_page": PER_PAGE, "order": "newest_first", "currency": "EUR"}
    if f.get("search_text"):
        p["search_text"] = f["search_text"]
    for key in ("catalog_ids", "brand_ids", "size_ids", "status_ids"):
        if f.get(key):
            p[key] = ",".join(map(str, f[key]))
    if f.get("price_from") is not None:
        p["price_from"] = f["price_from"]
    if f.get("price_to") is not None:
        p["price_to"] = f["price_to"]
    return p


# ---------------------------------------------------------------- normalisation
def _price(v) -> tuple[float | None, str]:
    if isinstance(v, dict):
        amt, cur = v.get("amount"), v.get("currency_code") or "EUR"
    else:
        amt, cur = v, "EUR"
    try:
        return float(str(amt).replace(",", ".")), cur
    except (TypeError, ValueError):
        return None, cur


def normalize_item(raw: dict) -> dict | None:
    """Ne garde que les champs de l'annonce. Les infos vendeur (raw['user']) sont ignorées."""
    photo = raw.get("photo") or {}
    ts = (photo.get("high_resolution") or {}).get("timestamp")
    if not ts:
        return None
    price, cur = _price(raw.get("price"))
    thumb = photo.get("url")
    for t in photo.get("thumbnails") or []:
        if t.get("type") in ("thumb310x430", "thumb150x210"):
            thumb = t.get("url") or thumb
            break
    iid = raw.get("id")
    return {
        "item_id": int(iid),
        "title": raw.get("title") or "",
        "brand": (raw.get("brand_title") or "").strip() or "(sans marque)",
        "price": price,
        "currency": cur,
        "size": raw.get("size_title") or "",
        "condition": raw.get("status") or "",
        "url": raw.get("url") or f"{BASE}/items/{iid}",
        "photo_url": thumb or "",
        "posted_at": float(ts),
        "favs": int(raw.get("favourite_count") or 0),
        "views": int(raw.get("view_count") or 0),
        "promoted": bool(raw.get("promoted") or raw.get("is_promoted")),
    }


# ---------------------------------------------------------------- client réel
class VintedClient:
    """Client synchrone (appelé dans un thread par le tracker)."""

    demo = False

    def __init__(self):
        from curl_cffi import requests as creq

        self._creq = creq
        self.session = creq.Session(impersonate="chrome")
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "Referer": BASE + "/catalog",
        })
        self._lock = threading.Lock()
        self.warmed = False

    def warmup(self):
        """Appelle la page d'accueil pour récupérer les cookies (access_token_web…)."""
        try:
            r = self.session.get(BASE + "/", timeout=30)
        except Exception as e:
            raise ApiError(f"Erreur réseau (page d'accueil) : {e}") from e
        if r.status_code in (403, 429):
            raise RateLimited(r.status_code)
        self.warmed = True

    def fetch_page(self, filters: dict, page: int) -> list[dict]:
        with self._lock:
            if not self.warmed:
                self.warmup()
            for attempt in (1, 2):
                try:
                    r = self.session.get(API, params=api_params(filters, page), timeout=30)
                except Exception as e:  # réseau
                    raise ApiError(f"Erreur réseau : {e}") from e
                if r.status_code == 401 and attempt == 1:
                    self.session.cookies.clear()
                    self.warmup()
                    continue
                if r.status_code in (403, 429):
                    raise RateLimited(r.status_code)
                if r.status_code != 200:
                    raise ApiError(f"HTTP {r.status_code}")
                try:
                    data = r.json()
                except Exception as e:
                    raise ApiError("Réponse non JSON") from e
                return data.get("items") or []
            raise ApiError("401 persistant malgré le rafraîchissement des cookies")


# ---------------------------------------------------------------- mode démo
DEMO_BRANDS = [
    # (marque, chaleur = favoris/min au départ, prix moyen, poids dans le flux)
    ("Carhartt", 2.6, 45, 6), ("Stüssy", 2.2, 55, 3), ("Ralph Lauren", 1.6, 38, 8),
    ("Arc'teryx", 3.2, 120, 2), ("The North Face", 1.4, 60, 6), ("Nike", 0.9, 30, 12),
    ("Lacoste", 1.1, 35, 6), ("Patagonia", 1.8, 70, 3), ("Levi's", 0.7, 25, 7),
    ("Zara", 0.25, 15, 12), ("H&M", 0.15, 9, 12), ("Shein", 0.08, 6, 8),
    ("Stone Island", 2.9, 140, 2), ("Kiabi", 0.05, 5, 5), ("", 0.2, 12, 6),
]
DEMO_SPEED = 60.0  # 1 minute simulée = 1 seconde réelle


class DemoClient:
    """Imite /api/v2/catalog/items : flux d'annonces généré, temps accéléré."""

    demo = True

    def __init__(self, clock, filters: dict, rate_per_min: float = 8.0, seed: int | None = None):
        self.clock = clock
        self.filters = filters
        self.rng = random.Random(seed)
        self.rate = rate_per_min
        self.items: list[dict] = []  # trié du plus ancien au plus récent
        self.next_id = 4_000_000_000 + self.rng.randint(0, 10_000_000)
        self.calls = 0
        self.warmed = True
        now = clock.now()
        self._gen_until = now - 40 * 60  # historique de 40 min avant le lancement
        self._generate(now)

    def _generate(self, until: float):
        t = self._gen_until
        weights = [b[3] for b in DEMO_BRANDS]
        pf, pt = self.filters.get("price_from"), self.filters.get("price_to")
        while True:
            t += self.rng.expovariate(self.rate / 60)
            if t > until:
                break
            brand, heat, pmean, _ = self.rng.choices(DEMO_BRANDS, weights)[0]
            price = round(max(1, self.rng.lognormvariate(math.log(pmean), 0.45)), 0)
            if pf is not None:
                price = max(price, pf)
            if pt is not None:
                price = min(price, pt)
            self.next_id += self.rng.randint(1, 40)
            self.items.append({
                "id": self.next_id, "brand": brand, "price": price, "ts": t,
                "q": self.rng.lognormvariate(0, 0.6), "heat": heat,
                "title": f"{brand or 'Pièce'} – {self.rng.choice(['Sweat', 'Veste', 'Hoodie', 'Pull', 'Polaire', 'T-shirt'])}",
                "size": self.rng.choice(["XS", "S", "M", "L", "XL"]),
            })
        self._gen_until = until

    def _raw(self, it: dict, now: float) -> dict:
        age_min = max(0.0, (now - it["ts"]) / 60)
        tau = 12.0
        favs = int(it["heat"] * it["q"] * tau * (1 - math.exp(-age_min / tau)))
        views = int(favs * 7 + age_min * 4 * (0.5 + it["q"]))
        return {
            "id": it["id"], "title": it["title"], "brand_title": it["brand"],
            "price": {"amount": f"{it['price']:.1f}", "currency_code": "EUR"},
            "size_title": it["size"], "status": "Très bon état",
            "url": f"{BASE}/items/{it['id']}", "favourite_count": favs, "view_count": views,
            "photo": {"url": f"/demo-photo/{it['id']}.svg?b={it['brand'] or 'NA'}",
                      "high_resolution": {"timestamp": int(it["ts"])}},
            "user": {"login": "non-stocké"},  # présent dans l'API, volontairement ignoré
        }

    def fetch_page(self, filters: dict, page: int) -> list[dict]:
        self.calls += 1
        # Un 429 simulé une fois, pour tester l'affichage du backoff.
        if self.calls == 25:
            raise RateLimited(429)
        now = self.clock.now()
        self._generate(now)
        newest = self.items[::-1]
        chunk = newest[(page - 1) * PER_PAGE: page * PER_PAGE]
        return [self._raw(it, now) for it in chunk]


# ---------------------------------------------------------------- horloges
class RealClock:
    speed = 1.0

    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float):
        import asyncio
        await asyncio.sleep(seconds)


class DemoClock:
    def __init__(self, speed: float = DEMO_SPEED):
        self.speed = speed
        self.t0 = time.time()

    def now(self) -> float:
        return self.t0 + (time.time() - self.t0) * self.speed

    async def sleep(self, seconds: float):
        import asyncio
        await asyncio.sleep(seconds / self.speed)
