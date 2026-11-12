"""Vinted Niche Watch — serveur FastAPI (API + page unique)."""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
import socket
import threading
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime
from html import escape
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from . import vinted
from .db import DB, brand_stats, enrich_items
from .tracker import MIN_INTERVAL, Tracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).resolve().parent.parent / "static"
PORT = int(os.environ.get("VNW_PORT", "8000"))

db = DB()
tracker: Tracker | None = None


def lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global tracker
    tracker = Tracker(db)
    ip = lan_ip()
    print(f"\n  Vinted Niche Watch : http://localhost:{PORT}")
    if ip:
        print(f"  Depuis ton téléphone (même Wi-Fi) : http://{ip}:{PORT}\n")
    if os.environ.get("VNW_OPEN_BROWSER") == "1":
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    yield
    await tracker.stop()


app = FastAPI(title="Vinted Niche Watch", lifespan=lifespan)


class StartBody(BaseModel):
    url: str = ""
    catalog_id: str = ""
    price_min: float | None = None
    price_max: float | None = None
    duration_min: int = Field(60, ge=1, le=24 * 60)
    window_min: int = Field(10, ge=1, le=180)
    threshold: int = Field(20, ge=1)
    interval_s: int = Field(60, ge=MIN_INTERVAL)
    demo: bool = False


class UrlBody(BaseModel):
    url: str


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/info")
def info():
    return {"lan_url": f"http://{lan_ip()}:{PORT}" if lan_ip() else None, "min_interval": MIN_INTERVAL}


@app.post("/api/parse-url")
def parse_url(body: UrlBody):
    f = vinted.parse_search_url(body.url)
    return {"filters": f, "label": vinted.filters_label(f)}


def build_filters(b: StartBody) -> dict:
    if b.url.strip():
        if "vinted." not in b.url:
            raise HTTPException(400, "L'URL doit être une recherche vinted.fr.")
        f = vinted.parse_search_url(b.url)
    else:
        f = vinted.parse_search_url("")
    # Les champs manuels complètent / remplacent l'URL.
    ids = [int(x) for x in b.catalog_id.replace(";", ",").split(",") if x.strip().isdigit()]
    if ids:
        f["catalog_ids"] = ids
    if b.price_min is not None:
        f["price_from"] = b.price_min
    if b.price_max is not None:
        f["price_to"] = b.price_max
    if not (f["catalog_ids"] or f["search_text"] or f["brand_ids"]) and not b.demo:
        raise HTTPException(400, "Indique une URL de recherche ou un ID de catégorie.")
    if f["price_from"] is not None and f["price_to"] is not None and f["price_from"] > f["price_to"]:
        raise HTTPException(400, "Le prix min est supérieur au prix max.")
    return f


@app.post("/api/start")
async def start(body: StartBody):
    if tracker.running:
        raise HTTPException(409, "Une veille est déjà en cours. Arrête-la d'abord.")
    f = build_filters(body)
    sid = await tracker.start(f, body.duration_min, body.window_min, body.threshold,
                              body.interval_s, body.demo)
    return {"session_id": sid}


@app.post("/api/stop")
async def stop():
    await tracker.stop()
    return {"ok": True}


@app.get("/api/state")
def state():
    return tracker.state()


@app.get("/api/sessions")
def sessions():
    out = []
    for s in db.list_sessions():
        items = enrich_items(db.items(s["id"]), s["window_min"], s["hit_threshold"])
        brands = brand_stats(items)
        s["n_hits"] = sum(1 for i in items if i["is_hit"])
        s["top_brands"] = [{"brand": b["brand"], "score": b["score"], "hits": b["hits"]} for b in brands[:3]]
        out.append(s)
    return out


def _session_or_404(sid: int) -> dict:
    s = db.get_session(sid)
    if not s:
        raise HTTPException(404, "Session introuvable")
    return s


def _session_data(sid: int):
    s = _session_or_404(sid)
    live = tracker.running and tracker.sid == sid
    now = tracker.clock.now() if live else None
    items = enrich_items(db.items(sid), s["window_min"], s["hit_threshold"], now)
    items.sort(key=lambda i: (i["window_fav"] if i["window_fav"] is not None else -1), reverse=True)
    return s, items, brand_stats(items), live


@app.get("/api/sessions/{sid}")
def session_detail(sid: int):
    s, items, brands, live = _session_data(sid)
    s["n_snapshots"] = db.snapshot_count(sid)
    return {"session": s, "items": items, "brands": brands, "live": live}


@app.delete("/api/sessions/{sid}")
def session_delete(sid: int):
    _session_or_404(sid)
    if tracker.running and tracker.sid == sid:
        raise HTTPException(409, "Impossible de supprimer la veille en cours.")
    db.delete_session(sid)
    return {"ok": True}


def _num(x, nd=2):
    if x is None:
        return ""
    if isinstance(x, float):
        x = round(x, nd)
        return (str(int(x)) if x.is_integer() else str(x)).replace(".", ",")
    return str(x)


def _csv_response(rows: list[list], filename: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerows(rows)
    data = buf.getvalue().encode("utf-8-sig")
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _ts(t: float | None) -> str:
    return datetime.fromtimestamp(t).strftime("%d/%m/%Y %H:%M") if t else ""


@app.get("/api/sessions/{sid}/export/items.csv")
def export_items(sid: int):
    s, items, _, _ = _session_data(sid)
    rows = [["id", "titre", "marque", "prix", "devise", "taille", "état", "mise en ligne",
             f"favoris {s['window_min']} min", "âge du relevé (min)", "mesure", "hit",
             "favoris dernier relevé", "vues dernier relevé", "lien"]]
    for i in items:
        mesure = "non mesurée" if not i["measured"] else "partielle" if i["partial"] else \
            "en cours" if i["in_progress"] else "complète"
        rows.append([i["item_id"], i["title"], i["brand"], _num(i["price"]), i["currency"], i["size"],
                     i["condition"], _ts(i["posted_at"]), _num(i["window_fav"]),
                     _num(i["window_age"] / 60 if i["window_age"] is not None else None, 1),
                     mesure, "oui" if i["is_hit"] else "non", _num(i["last_fav"]), _num(i["last_views"]),
                     i["url"]])
    return _csv_response(rows, f"veille_{sid}_annonces.csv")


@app.get("/api/sessions/{sid}/export/brands.csv")
def export_brands(sid: int):
    s, _, brands, _ = _session_data(sid)
    rows = [["rang", "marque", "annonces", "hits", "taux de hit (%)", "favoris moyens",
             "favoris médians", "favoris max", "prix moyen", "score"]]
    for n, b in enumerate(brands, 1):
        rows.append([n, b["brand"], b["n_items"], b["hits"], _num(b["hit_rate"], 1), _num(b["fav_mean"], 1),
                     _num(float(b["fav_median"]), 1), b["fav_max"], _num(b["price_mean"]), _num(b["score"])])
    return _csv_response(rows, f"veille_{sid}_marques.csv")


@app.get("/demo-photo/{item_id}.svg")
def demo_photo(item_id: int, b: str = "NA"):
    """Vignette générée pour le mode démo (aucune requête externe)."""
    hue = int(hashlib.md5(b.encode()).hexdigest()[:2], 16) * 360 // 256
    initials = escape("".join(w[0] for w in b.replace("'", " ").split()[:2]).upper() or "?")
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 160" width="120" height="160">'
           f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
           f'<stop offset="0" stop-color="hsl({hue},45%,62%)"/><stop offset="1" stop-color="hsl({(hue + 40) % 360},50%,40%)"/>'
           f'</linearGradient></defs><rect width="120" height="160" fill="url(#g)"/>'
           f'<text x="60" y="92" font-family="system-ui,sans-serif" font-size="38" font-weight="700" '
           f'fill="rgba(255,255,255,.92)" text-anchor="middle">{initials}</text></svg>')
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


def run():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")


if __name__ == "__main__":
    run()
