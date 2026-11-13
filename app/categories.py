"""Arbre complet des catégories Vinted, lu dans la page de recherche www.vinted.fr/catalog.

La page embarque l'arbre dans ses données React (`catalogTree:[...]`, souvent dans une chaîne JS
échappée). Méthode reprise du projet open source jasp-nerd/vinted-bot (taxonomy.py).
Cache : mémoire + fichier (7 jours) ; à défaut, copie livrée avec l'app (public/categories.json).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from . import vinted

TTL = 7 * 86400
SNAPSHOT = Path(__file__).resolve().parent.parent / "public" / "categories.json"
CACHE = Path(os.environ.get("VNW_CACHE_DIR") or (
    tempfile.gettempdir() if os.environ.get("VERCEL") else Path(__file__).resolve().parent.parent / "data"
)) / "vinted_categories.json"
_mem: dict = {}


def _matching_bracket(text: str) -> int:
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i + 1
    raise ValueError("arbre des catégories tronqué")


def extract_tree(html: str) -> list[dict]:
    at = html.find('catalogTree\\"')
    if at != -1:
        start = html.find("[", at)
        seg = html[start:start + 4_000_000].replace('\\"', '"').replace("\\\\", "\\")
    else:
        at = html.find('"catalogTree":')
        if at == -1:
            raise ValueError("la page Vinted ne contient pas l'arbre des catégories")
        start = html.find("[", at)
        seg = html[start:start + 4_000_000]
    return compact(json.loads(seg[:_matching_bracket(seg)]))


def compact(nodes) -> list[dict]:
    out = []
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        try:
            nid = int(n["id"])
        except (KeyError, TypeError, ValueError):
            continue
        title = (n.get("title") or "").strip()
        if title:
            out.append({"id": nid, "title": title, "children": compact(n.get("catalogs"))})
    return out


def _read(path: Path, max_age: float | None):
    try:
        d = json.loads(path.read_text("utf-8"))
        if max_age is None or time.time() - d["fetched_at"] < max_age:
            return d
    except Exception:
        return None


def get_tree(refresh: bool = False) -> dict:
    """{"fetched_at", "source", "tree"} : Vinted en direct (mis en cache 7 jours), sinon copie livrée."""
    if not refresh and _mem.get("tree") and time.time() - _mem["fetched_at"] < TTL:
        return _mem
    if not refresh:
        d = _read(CACHE, TTL)
        if d:
            _mem.update(d, source="cache")
            return _mem
    try:
        client = vinted.VintedClient()
        r = client.session.get(vinted.BASE + "/catalog", timeout=25)
        tree = extract_tree(r.text)
        if len(tree) < 3:
            raise ValueError("arbre incomplet")
        d = {"fetched_at": time.time(), "tree": tree}
        try:
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
        except OSError:
            pass
        _mem.update(d, source="vinted")
        return _mem
    except Exception as e:  # Vinted injoignable : copie livrée avec l'app
        d = _read(SNAPSHOT, None) or {"fetched_at": 0, "tree": []}
        _mem.update(d, source=f"copie locale ({type(e).__name__})")
        return _mem


def titles(tree: list[dict]) -> dict[int, str]:
    """id -> « Hommes › Vêtements › Sweats »."""
    out: dict[int, str] = {}

    def walk(nodes, path):
        for n in nodes:
            p = [*path, n["title"]]
            out[n["id"]] = " › ".join(p)
            walk(n["children"], p)
    walk(tree, [])
    return out
