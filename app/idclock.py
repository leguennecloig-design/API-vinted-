"""Horloge des IDs Vinted : convertit un numéro d'annonce en date de mise en ligne (estimée).

Vinted ne publie plus de date (ni dans l'API, ni sur la page de l'annonce, ni dans les dressings), mais
numérote les annonces dans l'ordre de mise en ligne, à l'échelle de toute la plateforme. L'app note donc
des « repères » (heure, plus haut ID visible) à chaque recherche et interpole entre eux. Hors de la plage
couverte (annonce plus ancienne que le plus vieux repère), on extrapole avec le rythme moyen mesuré :
précision ≈ ±15-20 % sur l'âge, qui devient exacte (±1 min) dès que l'app a des repères sur la période.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

# Repères réels relevés le 13/11/2026 (heure Unix, plus haut ID visible).
SEED = [[1794572281, 10790245075], [1794572614, 10790313784], [1794573035, 10790396825],
        [1794582641, 10792524326]]
DAY_RATE = 224.0     # IDs/s mesurés en journée (12 h-15 h)
AVG_RATE = 185.0     # IDs/s moyen sur 24 h (nuit plus calme) : utilisé pour extrapoler sur plusieurs jours
MIN_GAP_S = 300      # un repère conservé toutes les 5 min au plus

PATH = Path(os.environ.get("VNW_CACHE_DIR") or (
    tempfile.gettempdir() if os.environ.get("VERCEL") else Path(__file__).resolve().parent.parent / "data"
)) / "idclock.json"
_lock = threading.Lock()
_points: list[list[float]] = []


def _load():
    global _points
    if _points:
        return
    pts = [list(p) for p in SEED]
    try:
        pts += json.loads(PATH.read_text("utf-8"))
    except Exception:
        pass
    _points = _clean(pts)


def _clean(pts):
    """Tri, IDs strictement croissants dans le temps, un point toutes les MIN_GAP_S secondes."""
    out: list[list[float]] = []
    for t, i in sorted((float(t), int(i)) for t, i in pts):
        if out and (i <= out[-1][1] or t - out[-1][0] < MIN_GAP_S):
            if out and i > out[-1][1] and t >= out[-1][0]:
                out[-1] = [t, i]  # garder le plus récent de la fenêtre
            continue
        out.append([t, i])
    return out


def add(points) -> None:
    """Ajoute des repères (heure, plus haut ID visible) et les sauvegarde."""
    with _lock:
        _load()
        merged = _clean(_points + [list(p) for p in points if p and p[0] and p[1]])
        if merged != _points:
            _points[:] = merged
            try:
                PATH.parent.mkdir(parents=True, exist_ok=True)
                PATH.write_text(json.dumps(_points[-5000:]), "utf-8")
            except OSError:
                pass


def points() -> list[list[float]]:
    with _lock:
        _load()
        return list(_points)


def to_time(item_id: int, extra=None) -> float:
    """Heure de mise en ligne estimée de l'annonce `item_id`."""
    pts = _clean(points() + [list(p) for p in (extra or [])])
    if not pts:
        return time.time()
    if item_id >= pts[-1][1]:
        return pts[-1][0] + (item_id - pts[-1][1]) / DAY_RATE
    if item_id <= pts[0][1]:
        return pts[0][0] - (pts[0][1] - item_id) / AVG_RATE
    lo, hi = 0, len(pts) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if pts[mid][1] <= item_id:
            lo = mid
        else:
            hi = mid
    (t0, i0), (t1, i1) = pts[lo], pts[hi]
    return t0 + (t1 - t0) * (item_id - i0) / max(1, i1 - i0)


def coverage_start() -> float:
    """Avant cette date, l'âge est extrapolé (moins précis)."""
    p = points()
    return p[0][0] if p else time.time()
