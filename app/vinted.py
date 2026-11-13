"""Accès à l'API Vinted (curl_cffi, empreinte Chrome) + flux simulé pour le mode démo."""
from __future__ import annotations

import math
import random
import re
from urllib.parse import parse_qs, urlparse

BASE = "https://www.vinted.fr"
# Depuis septembre 2026, le catalogue est servi par api.vinted.fr/svc-catalogue (l'ancien
# www.vinted.fr/api/v2/catalog/items répond 404). Jeton = cookie access_token_web du site www.
API = "https://api.vinted.fr/svc-catalogue/items"
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
    """Paramètres de svc-catalogue : filtres renommés en attribute_ids[...], aucun filtre vide (sinon 400)."""
    p = {"page": page, "per_page": PER_PAGE, "order": "newest_first", "currency": "EUR"}
    if f.get("search_text"):
        p["search_text"] = f["search_text"]
    for key, attr in (("catalog_ids", "catalog"), ("brand_ids", "brand"), ("size_ids", "size"),
                      ("status_ids", "status")):
        if f.get(key):
            p[f"attribute_ids[{attr}]"] = ",".join(map(str, f[key]))
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
    """Ne garde que les champs de l'annonce. Les infos vendeur (raw['user']) sont ignorées.

    API svc-catalogue (sept. 2026) : plus de date de mise en ligne (posted_at = None, estimée par
    le tracker), marque et taille dans item_box (« first_line » = marque si différente du titre,
    « second_line » = « taille · état »), URL relative.
    """
    iid = raw.get("id")
    if iid is None:
        return None
    photo = raw.get("photo") or {}
    ts = (photo.get("high_resolution") or {}).get("timestamp")
    price, cur = _price(raw.get("price"))
    thumb = photo.get("url")
    for t in photo.get("thumbnails") or []:
        if t.get("type") == "thumb310x430":
            thumb = t.get("url") or thumb
            break
    title = raw.get("title") or ""
    box = raw.get("item_box") or {}
    brand = (raw.get("brand_title") or "").strip()
    if not brand and box.get("first_line") and box["first_line"] != title:
        brand = box["first_line"].strip()
    second = [x.strip() for x in (box.get("second_line") or "").split("·")]
    size = raw.get("size_title") or (second[0] if len(second) > 1 else "")
    condition = raw.get("status") or (second[-1] if second and second[-1] else "")
    url = raw.get("url") or f"/items/{iid}"
    if url.startswith("/"):
        url = BASE + url
    return {
        "item_id": int(iid),
        "title": title,
        "brand": brand or "(sans marque)",
        "price": price,
        "currency": cur,
        "size": size,
        "condition": condition,
        "url": url,
        "photo_url": thumb or "",
        "posted_at": float(ts) if ts else None,
        "favs": int(raw.get("favourite_count") or 0),
        "views": int(raw.get("view_count") or 0),
        "promoted": bool(raw.get("promoted") or raw.get("is_promoted")),
    }


# ---------------------------------------------------------------- client réel
class VintedClient:
    """Client synchrone. Les cookies sont sauvegardés en base entre deux scans (serverless)."""

    demo = False

    def __init__(self, cookies: dict | None = None):
        from curl_cffi import requests as creq

        self.session = creq.Session(impersonate="chrome")
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "Referer": BASE + "/catalog",
        })
        self.warmed = False
        if cookies:
            for k, v in cookies.items():
                self.session.cookies.set(k, v, domain=".vinted.fr")
            self.warmed = True

    def export_cookies(self) -> dict:
        try:
            return {c.name: c.value for c in self.session.cookies.jar}
        except Exception:
            return dict(self.session.cookies)

    def warmup(self):
        """Visite le site www pour obtenir les cookies access_token_web (jeton API) et anon_id."""
        try:
            r = self.session.get(BASE + "/", timeout=20)
        except Exception as e:
            raise ApiError(f"Erreur réseau (page d'accueil) : {e}") from e
        if r.status_code in (403, 429):
            raise RateLimited(r.status_code)
        if not self._cookie("access_token_web"):
            raise ApiError("Vinted n'a pas fourni de jeton (cookie access_token_web absent)")
        self.warmed = True

    def _cookie(self, name: str) -> str | None:
        try:
            return self.session.cookies.get(name, domain=".vinted.fr") or self.session.cookies.get(name)
        except Exception:
            for c in self.session.cookies.jar:
                if c.name == name:
                    return c.value
        return None

    def _headers(self) -> dict:
        h = {"Accept": "application/json", "Origin": BASE, "Referer": BASE + "/", "Locale": "fr"}
        tok, anon = self._cookie("access_token_web"), self._cookie("anon_id")
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        if anon:
            h["x-anon-id"] = anon
        return h

    def fetch_page(self, filters: dict, page: int, extra: dict | None = None) -> list[dict]:
        if not self.warmed or not self._cookie("access_token_web"):
            self.warmup()
        for attempt in (1, 2):
            try:
                params = api_params(filters, page)
                for k, v in (extra or {}).items():  # diagnostic uniquement
                    if v is None:
                        params.pop(k, None)
                    else:
                        params[k] = v
                r = self.session.get(API, params=params, headers=self._headers(), timeout=20)
            except Exception as e:  # réseau
                raise ApiError(f"Erreur réseau : {e}") from e
            if r.status_code in (401, 403) and attempt == 1:
                self.session.cookies.clear()  # jeton expiré : on en reprend un
                self.warmup()
                continue
            if r.status_code in (403, 429):
                raise RateLimited(r.status_code)
            if r.status_code != 200:
                snippet = " ".join((r.text or "")[:200].split())
                raise ApiError(f"HTTP {r.status_code} sur {str(r.url)[:140]} — {snippet}")
            try:
                data = r.json()
            except Exception as e:
                raise ApiError("Réponse non JSON") from e
            return data.get("items") or []
        raise ApiError("Jeton refusé malgré le rafraîchissement des cookies")


# ---------------------------------------------------------------- mode démo
DEMO_BRANDS = [
    # (marque, chaleur = favoris/min au départ, prix moyen, poids dans le flux)
    ("Carhartt", 2.6, 45, 6), ("Stüssy", 2.2, 55, 3), ("Ralph Lauren", 1.6, 38, 8),
    ("Arc'teryx", 3.2, 120, 2), ("The North Face", 1.4, 60, 6), ("Nike", 0.9, 30, 12),
    ("Lacoste", 1.1, 35, 6), ("Patagonia", 1.8, 70, 3), ("Levi's", 0.7, 25, 7),
    ("Zara", 0.25, 15, 12), ("H&M", 0.15, 9, 12), ("Shein", 0.08, 6, 8),
    ("Stone Island", 2.9, 140, 2), ("Kiabi", 0.05, 5, 5), ("", 0.2, 12, 6),
]
DEMO_SPEED = 30.0   # 1 minute simulée = 2 secondes réelles
DEMO_RATE = 8.0     # annonces / minute simulée


class DemoClient:
    """Imite /api/v2/catalog/items. Flux déterministe (même graine = mêmes annonces) :
    rien à garder en mémoire entre deux scans, compatible serverless."""

    demo = True

    def __init__(self, now: float, filters: dict, seed: int, request_no: int = 0):
        self.now = now
        self.filters = filters
        self.seed = seed
        self.request_no = request_no

    def export_cookies(self) -> dict:
        return {}

    def _bucket(self, b: int) -> list[dict]:
        """Annonces postées pendant la minute simulée n° b."""
        rng = random.Random(f"{self.seed}:{b}")
        n = 0
        L, p = math.exp(-DEMO_RATE), 1.0  # tirage de Poisson
        while True:
            p *= rng.random()
            if p <= L:
                break
            n += 1
        weights = [x[3] for x in DEMO_BRANDS]
        pf, pt = self.filters.get("price_from"), self.filters.get("price_to")
        out = []
        for k in range(n):
            brand, heat, pmean, _ = rng.choices(DEMO_BRANDS, weights)[0]
            price = round(max(1, rng.lognormvariate(math.log(pmean), 0.45)), 0)
            if pf is not None:
                price = max(price, pf)
            if pt is not None:
                price = min(price, pt)
            ts = b * 60 + rng.random() * 60
            out.append({
                "id": int(ts) * 10 + k,  # comme sur Vinted : l'ID croît avec le temps
                "brand": brand, "price": price, "ts": ts,
                "q": rng.lognormvariate(0, 0.6), "heat": heat,
                "title": f"{brand or 'Pièce'} – {rng.choice(['Sweat', 'Veste', 'Hoodie', 'Pull', 'Polaire', 'T-shirt'])}",
                "size": rng.choice(["XS", "S", "M", "L", "XL"]),
            })
        return sorted(out, key=lambda i: i["ts"], reverse=True)

    def _raw(self, it: dict) -> dict:
        """Même forme que svc-catalogue : pas d'horodatage, marque/taille dans item_box."""
        age_min = max(0.0, (self.now - it["ts"]) / 60)
        tau = 12.0
        favs = int(it["heat"] * it["q"] * tau * (1 - math.exp(-age_min / tau)))
        views = int(favs * 7 + age_min * 4 * (0.5 + it["q"]))
        return {
            "id": it["id"], "title": it["title"], "promoted": False,
            "price": {"amount": f"{it['price']:.2f}", "currency_code": "EUR"},
            "item_box": {"first_line": it["brand"] or it["title"], "second_line": f"{it['size']} · Très bon état"},
            "url": f"/items/{it['id']}-demo", "favourite_count": favs, "view_count": views,
            "photo": {"url": f"/demo-photo/{it['id']}.svg?b={it['brand'] or 'NA'}", "high_resolution": {}},
            "user": {"login": "non-stocké"},  # présent dans l'API, volontairement ignoré
        }

    def fetch_page(self, filters: dict, page: int) -> list[dict]:
        self.request_no += 1
        if self.request_no == 25:  # un 429 simulé, pour tester l'affichage du backoff
            raise RateLimited(429)
        need = page * PER_PAGE
        found: list[dict] = []
        b = int(self.now // 60)
        for _ in range(240):
            found += [i for i in self._bucket(b) if i["ts"] <= self.now]
            if len(found) >= need:
                break
            b -= 1
        return [self._raw(i) for i in found[(page - 1) * PER_PAGE: need]]
