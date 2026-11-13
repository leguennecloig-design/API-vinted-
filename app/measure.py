"""Mesure de la demande : favoris aux paliers (5, 10, 15 min…), projection, « en chauffe ».

Fonctions pures, utilisées par le serveur (tracker / db) et recopiées à l'identique dans la page
(public/index.html) pour le mode navigateur.

Principe : pour chaque palier c, on garde le dernier relevé AVANT c et le premier relevé APRÈS c.
Les favoris à c s'obtiennent par interpolation linéaire entre les deux : on obtient la valeur exacte
à 10 min même si les scans tombent à 9 min 40 et 10 min 35.
"""
from __future__ import annotations

import statistics

STD_CHECKPOINTS = (5, 10, 15)
TOL_BEFORE = 30     # s : un relevé pris jusqu'à 30 s avant le palier suffit
TOL_AFTER = 60      # s : un relevé pris jusqu'à 60 s après le palier suffit
MAX_GAP = 300       # s : au-delà, l'interpolation est jugée trop approximative (mesure partielle)
HOT_MIN_AGE = 120   # s : pas de projection avant 2 min (trop bruité)
HOT_EXPONENT = 0.75  # les favoris ralentissent avec le temps : projection en puissance 0,75 (prudente)
GONE_WEIGHT = 3     # poids d'une annonce disparue (vendue ?) dans le score marque
HIT_WEIGHT = 2


def checkpoints(window_min: int) -> list[int]:
    """Paliers mesurés : 5, 10, 15 min (s'ils sont < fenêtre) + la fenêtre elle-même."""
    return sorted({c for c in STD_CHECKPOINTS if c < window_min} | {int(window_min)})


def update_cps(cps: dict, age: float, favs: int, window_min: int) -> dict:
    """Intègre un relevé (âge en s, favoris) dans les paliers. cps[str(c)] = [âge_av, fav_av, âge_ap, fav_ap]."""
    for c in checkpoints(window_min):
        t = c * 60
        e = list(cps.get(str(c)) or [None, None, None, None])
        if age <= t:
            if e[0] is None or age >= e[0]:
                e[0], e[1] = age, favs
        elif e[2] is None or age < e[2]:
            e[2], e[3] = age, favs
        cps[str(c)] = e
    return cps


def cp_value(e, c: int) -> tuple[float | None, bool]:
    """(favoris au palier c, mesure complète ?)."""
    if not e:
        return None, False
    t = c * 60
    ab, fb, aa, fa = e
    if ab is not None and aa is not None and aa - ab <= MAX_GAP:
        return fb + (fa - fb) * (t - ab) / max(1e-9, aa - ab), True
    if ab is not None and t - ab <= TOL_BEFORE:
        return float(fb), True
    if aa is not None and aa - t <= TOL_AFTER:
        return float(fa), True
    if ab is not None:
        return float(fb), False  # minorant : la mesure n'a pas pu être faite au bon moment
    return None, False


def window_from_cps(cps: dict, window_min: int) -> tuple[int | None, float | None]:
    """(favoris à la fenêtre, âge équivalent) — âge = fenêtre si la mesure est complète."""
    e = cps.get(str(int(window_min)))
    v, complete = cp_value(e, int(window_min))
    if v is None:
        return None, None
    return int(round(v)), (window_min * 60.0 if complete else e[0])


def enrich(it: dict, window_min: int, threshold: int, now: float | None) -> dict:
    """Ajoute : paliers, en cours / partielle / disparue, hit, fav/min, projection, « en chauffe »."""
    w = window_min * 60
    wf, wa = it.get("window_fav"), it.get("window_age")
    age_now = (now - it["posted_at"]) if now is not None else None
    it["measured"] = wf is not None
    it["in_progress"] = bool(age_now is not None and age_now < w + TOL_AFTER and (wa is None or wa < w - TOL_BEFORE))
    early = bool(wf is not None and wa is not None and wa < w - TOL_BEFORE and not it["in_progress"])
    # Plus jamais revue alors que les scans continuaient : vendue / réservée / retirée avant la fin
    # de sa fenêtre. C'est le signal de demande le plus fort.
    la = it.get("last_age")
    ended = now is None or age_now > w + 180
    # Il faut 2 absences « prouvées » CONSÉCUTIVES (scans complets, pas bloqués par la limite de 10 pages
    # de Vinted), et un dernier relevé à plus d'1 min de la fin de fenêtre, pour conclure à une
    # disparition ; sinon l'annonce était seulement hors de portée -> partielle.
    proven = (it.get("cps") or {}).get("_m", 0) >= 2
    it["gone"] = bool(early and ended and la is not None and la < w - TOL_AFTER and proven)
    it["partial"] = early and not it["gone"]
    # Hit : seuil atteint dans la fenêtre (pour une annonce en cours, ses favoris actuels sont dans la fenêtre).
    cur = it.get("last_fav") if it["in_progress"] else wf
    it["is_hit"] = bool(cur is not None and cur >= threshold)
    it["age_min"] = round(age_now / 60, 1) if age_now is not None else None
    cps = it.get("cps") or {}
    it["cp_values"] = {}
    for c in checkpoints(window_min):
        v, complete = cp_value(cps.get(str(c)), c)
        it["cp_values"][str(c)] = None if v is None or (not complete and not it["gone"] and not it["partial"]) \
            else int(round(v))
    # Vitesse : favoris par minute au dernier relevé utile (dans la fenêtre).
    lf, la_ = it.get("last_fav"), it.get("last_age")
    ref_f, ref_a = (wf, wa) if not it["in_progress"] and wf is not None else (lf, la_)
    it["favs_per_min"] = round(ref_f / max(ref_a / 60, 1), 2) if ref_f is not None and ref_a else None
    # Projection à la fenêtre pour les annonces jeunes, puis « en chauffe » si elle atteint le seuil.
    it["projected"] = None
    it["hot"] = False
    if it["in_progress"] and lf is not None and la_ and la_ >= HOT_MIN_AGE:
        it["projected"] = int(round(lf * (w / la_) ** HOT_EXPONENT)) if la_ < w else lf
        it["hot"] = it["projected"] >= threshold and lf >= max(3, threshold * 0.25) and not it["is_hit"]
    if it["in_progress"]:
        it["rank_value"] = max(lf or 0, it["projected"] or 0)
    else:
        it["rank_value"] = wf if wf is not None else (lf or 0)
    return it


import re as _re
import unicodedata as _ud

# Variantes d'une même marque regroupées (sous-lignes, collaborations, casse, accents).
BRAND_ALIASES = {
    "polo ralph lauren": "ralph lauren", "lauren ralph lauren": "ralph lauren", "polo by ralph lauren": "ralph lauren",
    "ralph lauren polo": "ralph lauren", "tommy jeans": "tommy hilfiger", "hilfiger denim": "tommy hilfiger",
    "calvin klein jeans": "calvin klein", "ck calvin klein": "calvin klein", "levis": "levi s", "levi strauss": "levi s",
    "the north face": "the north face", "north face": "the north face", "tnf": "the north face",
    "nike sportswear": "nike", "nike air": "nike", "nike sb": "nike", "nike acg": "nike", "nike golf": "nike",
    "adidas originals": "adidas", "adidas performance": "adidas", "adidas neo": "adidas", "adidas by stella mccartney": "adidas",
    "jordan": "jordan", "air jordan": "jordan", "zara basic": "zara", "zara trafaluc": "zara", "zara woman": "zara",
    "h m": "h&m", "h and m": "h&m", "hm": "h&m", "hugo boss": "boss", "boss orange": "boss", "boss black": "boss", "hugo": "boss",
    "g star raw": "g-star", "g star": "g-star", "stussy": "stussy", "cp company": "c.p. company", "c p company": "c.p. company",
    "dr martens": "dr. martens", "dr. martens": "dr. martens", "new balance": "new balance", "carhartt wip": "carhartt",
}
NO_BRAND = {"inconnu", "inconnue", "autre", "autres", "no brand", "sans marque", "unbranded", "other", "others",
            "non renseigne", "non renseignée", "non renseignee", "marque inconnue", "unknown", "aucune"}
_SUFFIX = _re.compile(r"(\s+(originals|original|sportswear|vintage|kids|junior|enfant|homme|femme|woman|women|men|"
                      r"collection|paris|london|italia|france|usa|official))+$")   # en fin de nom seulement


def brand_key(name: str | None) -> str:
    """Clé de regroupement d'une marque : « Polo Ralph Lauren », « RALPH LAUREN » -> « ralph lauren »."""
    if not name or name == "(sans marque)" or name.strip().lower() in NO_BRAND:
        return "(sans marque)"
    k = _ud.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    k = _re.split(r"\s+(?:x|×|&|feat\.?)\s+", k)[0]                 # collaborations : « Nike x Nocta » -> nike
    k = _re.sub(r"[^a-z0-9.&]+", " ", k).strip()
    k = BRAND_ALIASES.get(k, k)
    k2 = _SUFFIX.sub(" ", k).strip()
    k2 = _re.sub(r"\s+", " ", k2)
    return BRAND_ALIASES.get(k2, k2) or k


def group_brands(items: list[dict]) -> dict[str, tuple[str, list[dict]]]:
    """{clé: (nom affiché = variante la plus fréquente, annonces)}."""
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(brand_key(it.get("brand")), []).append(it)
    out = {}
    for k, its in groups.items():
        names: dict[str, int] = {}
        for i in its:
            names[i.get("brand") or "(sans marque)"] = names.get(i.get("brand") or "(sans marque)", 0) + 1
        out[k] = (max(names, key=names.get), its)
    return out


def brand_stats(items: list[dict]) -> list[dict]:
    out = []
    for _key, (brand, its) in group_brands(items).items():
        favs = [i["window_fav"] for i in its if i["window_fav"] is not None]
        prices = [i["price"] for i in its if i.get("price") is not None]
        hits = sum(1 for i in its if i["is_hit"])
        gone = sum(1 for i in its if i.get("gone"))
        hot = sum(1 for i in its if i.get("hot"))
        n = len(its)
        med = statistics.median(favs) if favs else 0
        out.append({
            "brand": brand,
            "n_items": n,
            "hits": hits,
            "hot": hot,
            "gone": gone,
            "hit_rate": round(100 * hits / n, 1) if n else 0,
            "fav_mean": round(statistics.fmean(favs), 1) if favs else 0,
            "fav_median": med,
            "fav_max": max(favs) if favs else 0,
            "price_mean": round(statistics.fmean(prices), 2) if prices else None,
            "score": round(med * min(n, 5) / 5 + HIT_WEIGHT * hits + GONE_WEIGHT * gone, 2),
        })
    out.sort(key=lambda b: (b["score"], b["hits"], b["n_items"]), reverse=True)
    return out


def session_summary(items: list[dict], s: dict) -> dict:
    """Indice de demande de la niche, pour comparer les veilles entre elles dans l'historique."""
    favs = [i["window_fav"] for i in items if i.get("window_fav") is not None and not i.get("partial")]
    n = len(items)
    hours = max(s["duration_min"] / 60, 1 / 60)
    brands = brand_stats(items)
    return {
        "n_hits": sum(1 for i in items if i["is_hit"]),
        "n_gone": sum(1 for i in items if i.get("gone")),
        "hit_rate": round(100 * sum(1 for i in items if i["is_hit"]) / n, 1) if n else 0,
        "fav_median": statistics.median(favs) if favs else 0,
        "fav_p90": sorted(favs)[int(len(favs) * 0.9)] if favs else 0,
        "per_hour": round(n / hours, 1),
        "top_brands": [{"brand": b["brand"], "score": b["score"], "hits": b["hits"]} for b in brands[:3]],
    }


def cat_stats(items: list[dict], names: dict | None = None, hours: float = 1.0) -> list[dict]:
    """Comparaison des catégories surveillées : volume, hits, favoris médians, vendues ?."""
    names = names or {}
    groups: dict = {}
    for it in items:
        groups.setdefault(it.get("catalog_id"), []).append(it)
    out = []
    for cat, its in groups.items():
        favs = [i["window_fav"] for i in its if i.get("window_fav") is not None and not i.get("partial")]
        hits = sum(1 for i in its if i["is_hit"])
        gone = sum(1 for i in its if i.get("gone"))
        n = len(its)
        med = statistics.median(favs) if favs else 0
        out.append({
            "catalog_id": cat,
            "name": (names.get(str(cat)) or names.get(cat) or ("Toutes" if cat is None else f"Catégorie {cat}")),
            "n_items": n, "per_hour": round(n / max(hours, 1 / 60), 1), "hits": hits,
            "hit_rate": round(100 * hits / n, 1) if n else 0, "fav_median": med,
            "fav_p90": sorted(favs)[int(len(favs) * 0.9)] if favs else 0, "gone": gone,
            "hot": sum(1 for i in its if i.get("hot")),
            "score": round(med * min(n, 5) / 5 + HIT_WEIGHT * hits + GONE_WEIGHT * gone, 2),
        })
    out.sort(key=lambda c: (c["hit_rate"], c["fav_median"]), reverse=True)
    return out
