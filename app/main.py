"""Vinted Niche Watch — serveur FastAPI (API + page unique)."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import logging
import os
import socket
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime
from html import escape
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import tracker, vinted
from .db import DB, brand_stats, enrich_items
from .tracker import MIN_INTERVAL

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
PUBLIC = Path(__file__).resolve().parent.parent / "public"
PORT = int(os.environ.get("VNW_PORT", "8000"))
SERVERLESS = bool(os.environ.get("VERCEL"))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")

CONFIG_ERROR = ""
db: DB | None = None
# Sur Vercel sans base : « mode navigateur ». Le serveur ne fait que les appels Vinted (/api/scan)
# et le suivi + l'historique sont stockés dans le navigateur (IndexedDB).
BROWSER_MODE = SERVERLESS and not (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL"))
if not BROWSER_MODE:
    try:
        db = DB()
    except Exception as e:  # base injoignable / identifiants faux
        logging.exception("DB")
        CONFIG_ERROR = f"Connexion à la base impossible : {type(e).__name__}: {e}"


def lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


async def local_loop():
    """En local (start.bat), la veille avance toute seule, même page fermée."""
    while True:
        try:
            await asyncio.to_thread(tracker.tick, db)
        except Exception:
            logging.exception("tick")
        run = db.running_session()
        await asyncio.sleep(1 if run and run["demo"] else 5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    if not SERVERLESS:
        task = asyncio.create_task(local_loop())
        ip = lan_ip()
        print(f"\n  Vinted Niche Watch : http://localhost:{PORT}")
        if ip:
            print(f"  Depuis ton téléphone (même Wi-Fi) : http://{ip}:{PORT}\n")
        if os.environ.get("VNW_OPEN_BROWSER") == "1":
            threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    yield
    if task:
        task.cancel()


app = FastAPI(title="Vinted Niche Watch", lifespan=lifespan)


def _authorized(request: Request) -> bool:
    if not APP_PASSWORD:
        return True
    key = request.headers.get("x-app-key") or request.query_params.get("key") or ""
    if key and hmac.compare_digest(key, APP_PASSWORD):
        return True
    auth = request.headers.get("authorization", "")
    return bool(CRON_SECRET and hmac.compare_digest(auth, f"Bearer {CRON_SECRET}"))


@app.middleware("http")
async def password_guard(request: Request, call_next):
    if CONFIG_ERROR and not request.url.path.startswith("/demo-photo"):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": CONFIG_ERROR}, status_code=503)
        return Response(f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                        f"<body style='font:16px system-ui;max-width:560px;margin:60px auto;padding:0 16px'>"
                        f"<h2 style='color:#09827e'>Vinted Niche Watch : configuration à terminer</h2>"
                        f"<p>{escape(CONFIG_ERROR)}</p><p>Voir le README, section Vercel.</p>",
                        media_type="text/html", status_code=503)
    if request.url.path.startswith("/api/") and request.url.path != "/api/info" and not _authorized(request):
        return JSONResponse({"detail": "Mot de passe requis"}, status_code=401)
    return await call_next(request)


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
    return FileResponse(PUBLIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/info")
def info():
    return {"serverless": SERVERLESS, "auth_required": bool(APP_PASSWORD), "min_interval": MIN_INTERVAL,
            "storage": "browser" if BROWSER_MODE else "server", "server_time": time.time(),
            "demo_speed": vinted.DEMO_SPEED, "backoff": tracker.BACKOFF}


@app.middleware("http")
async def browser_mode_guard(request: Request, call_next):
    """En mode navigateur, seules les routes sans état sont disponibles."""
    p = request.url.path
    if BROWSER_MODE and p.startswith("/api/") and p not in ("/api/info", "/api/scan", "/api/parse-url"):
        return JSONResponse({"detail": "Mode navigateur : cette route est gérée par la page."}, status_code=400)
    return await call_next(request)


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


class DemoParams(BaseModel):
    seed: int
    now: float
    speed: float
    request_no: int = 0


class ScanBody(BaseModel):
    filters: dict
    stop_id: int | None = None      # on pagine jusqu'à passer sous cet ID
    baseline: bool = False          # scan de référence (1 page) au lancement
    cookies: dict | None = None
    demo: DemoParams | None = None
    debug: bool = False


@app.post("/api/scan")
def scan(body: ScanBody):
    """Un scan sans état (mode navigateur) : renvoie les annonces vues, la page fait le suivi."""
    if body.demo:
        d, real0 = body.demo, time.time()
        now_fn = lambda: d.now + (time.time() - real0) * d.speed  # noqa: E731
        sleep_fn = lambda sec: time.sleep(sec / d.speed)  # noqa: E731
        client = vinted.DemoClient(d.now, body.filters, d.seed, d.request_no)
    else:
        now_fn, sleep_fn = time.time, time.sleep
        client = vinted.VintedClient(body.cookies)
    out: list = []
    meta = {"requests": 0, "pages": 0}
    warnings: list[str] = []
    res = {"status": "ok"}
    if body.debug and not body.demo:  # diagnostic : forme brute d'une annonce (sans le vendeur)
        try:
            raw = client.fetch_page(body.filters, 1)
            sample = {k: v for k, v in (raw[0] if raw else {}).items() if k not in ("user", "seller")}
            return {"status": "ok", "count": len(raw), "sample": sample,
                    "ids": [r.get("id") for r in raw[:30]]}
        except Exception as e:
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}
    try:
        tracker.scan_pages(client, body.filters, body.stop_id, now_fn, sleep_fn, out, meta, warnings.append,
                           max_pages=1 if body.baseline else tracker.MAX_PAGES)
    except vinted.RateLimited as e:
        res = {"status": "rate_limited", "http": e.status}
    except vinted.ApiError as e:
        res = {"status": "error", "message": str(e)}
    keep = ("item_id", "title", "brand", "price", "currency", "size", "condition", "url", "photo_url",
            "posted_at", "favs", "views", "seen_at")
    res.update(items=[{k: it[k] for k in keep} for it in out], pages=meta["pages"], requests=meta["requests"],
               warnings=warnings, now=now_fn(),
               cookies=(client.export_cookies() if not client.demo and client.warmed else None))
    return res


@app.post("/api/start")
def start(body: StartBody):
    f = build_filters(body)
    try:
        sid = tracker.start(db, f, body.duration_min, body.window_min, body.threshold, body.interval_s, body.demo)
    except tracker.Busy as e:
        raise HTTPException(409, str(e))
    return {"session_id": sid}


@app.post("/api/stop")
def stop():
    tracker.stop(db)
    return {"ok": True}


@app.api_route("/api/tick", methods=["GET", "POST"])
def tick():
    """Fait avancer la veille d'un scan si besoin. Appelé par la page ouverte ou par un cron externe."""
    return tracker.tick(db)


@app.get("/api/state")
def state():
    return tracker.state(db)


@app.get("/api/sessions")
def sessions():
    out = []
    for s in db.list_sessions():
        summ = s.get("summary")
        if not summ:  # veille en cours : calcul à la volée
            items = enrich_items(db.items(s["id"]), s["window_min"], s["hit_threshold"])
            brands = brand_stats(items)
            summ = {"n_hits": sum(1 for i in items if i["is_hit"]),
                    "top_brands": [{"brand": b["brand"], "score": b["score"], "hits": b["hits"]} for b in brands[:3]]}
        s.update(summ)
        out.append(s)
    return out


def _session_or_404(sid: int) -> dict:
    s = db.get_session(sid)
    if not s:
        raise HTTPException(404, "Session introuvable")
    return s


def _session_data(sid: int):
    s = _session_or_404(sid)
    live = s["status"] == "running"
    now = tracker.clock_now(s) if live else None
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
    if db.get_session(sid)["status"] == "running":
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
