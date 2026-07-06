"""Source freely-licensed player portraits from Wikimedia and crop them to faces.

Deterministic asset pipeline (exact lookup + verification, no LLM judgment):
  1. For every player in data/worldcup.json, find their Wikipedia page image.
  2. Verify the page is actually an association footballer (Wikidata short
     description / extract must say so) — guards against name collisions.
  3. Download the thumbnail, crop to a head-biased square, resize, save under
     frontend/img/<code>/<slug>.jpg.
  4. Record author + license + file page URL per player for attribution.

Output: frontend/img/**, data/faces.json  (name -> {img, credit, license, src}).
Players with no verified Commons portrait are simply skipped — the card falls
back to the shirt-number circle. No guessing, no fallback fabrication.

    python -m updater.faces            # all players
    python -m updater.faces --only ARG,FRA
    python -m updater.faces --force    # re-fetch even if the file exists
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import threading
import time
import unicodedata
from io import BytesIO
from pathlib import Path

import cv2
import httpx
import numpy as np
from PIL import Image

_CASCADES = [
    cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml"),
    cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml"),
]

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "worldcup.json"
IMG_DIR = ROOT / "frontend" / "img"
FACES_FILE = ROOT / "data" / "faces.json"

WP_API = "https://en.wikipedia.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
HEADERS = {
    "User-Agent": "worldcup26-faces/1.0 (https://github.com/newsbubbles/worldcup26; nathaniel.gibson@gmail.com)"
}
FOOTBALL_RE = re.compile(r"footballer|football player|soccer|goalkeeper", re.I)
TARGET = 360  # output square px
THUMB = 800   # requested source thumbnail px

_throttle = threading.Semaphore(1)
_last = [0.0]


def polite_get(client: httpx.Client, url: str, **kw) -> httpx.Response:
    """GET with global min-interval throttling + backoff on 429/503 — Wikimedia is
    shared infra; hammering it just gets us rate-limited."""
    for attempt in range(5):
        with _throttle:
            wait = 0.35 - (time.monotonic() - _last[0])
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.monotonic()
        r = client.get(url, **kw)
        if r.status_code in (429, 503):
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r


def slugify(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    n = re.sub(r"[^a-zA-Z0-9]+", "-", n).strip("-").lower()
    return n or "player"


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def candidate_titles(client: httpx.Client, name: str) -> list[str]:
    """Search-ranked candidate page titles. An exact (accent-insensitive) title
    match is tried first, but the rest follow so a disambiguation page like
    'Rodri' falls through to 'Rodri (footballer, born 1996)'."""
    r = polite_get(client, WP_API, params={
        "action": "query", "format": "json", "list": "search",
        "srsearch": f"{name} footballer", "srlimit": 6, "srnamespace": 0,
    })
    hits = [h["title"] for h in r.json().get("query", {}).get("search", [])]
    want = slugify(name)
    hits.sort(key=lambda t: 0 if slugify(t) == want else 1)
    return hits


def page_image(client: httpx.Client, title: str) -> tuple[str, str, str] | None:
    """(thumb_url, file_title, page_url) for a verified footballer page, else None."""
    r = polite_get(client, WP_API, params={
        "action": "query", "format": "json", "redirects": 1, "titles": title,
        "prop": "pageimages|description|extracts",
        "piprop": "thumbnail|name", "pithumbsize": THUMB,
        "exintro": 1, "explaintext": 1, "exsentences": 2,
    })
    r.raise_for_status()
    pages = r.json().get("query", {}).get("pages", {})
    page = next(iter(pages.values()), {})
    if "thumbnail" not in page:
        return None
    blurb = f"{page.get('description', '')} {page.get('extract', '')}"
    if not FOOTBALL_RE.search(blurb):
        return None  # name collision — not a footballer, skip
    file_title = "File:" + page["pageimage"] if page.get("pageimage") else None
    page_url = f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"
    return page["thumbnail"]["source"], file_title, page_url


def image_license(client: httpx.Client, file_title: str) -> dict:
    r = polite_get(client, COMMONS_API, params={
        "action": "query", "format": "json", "titles": file_title,
        "prop": "imageinfo", "iiprop": "extmetadata|url",
    })
    r.raise_for_status()
    pages = r.json().get("query", {}).get("pages", {})
    page = next(iter(pages.values()), {})
    info = (page.get("imageinfo") or [{}])[0]
    meta = info.get("extmetadata", {})
    return {
        "author": _strip_html(meta.get("Artist", {}).get("value", "")) or "Unknown",
        "license": _strip_html(meta.get("LicenseShortName", {}).get("value", "")) or "see source",
        "descriptionurl": info.get("descriptionurl", ""),
    }


def crop_face(data: bytes) -> bytes:
    img = Image.open(BytesIO(data))
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    faces: list = []
    try:
        gray = cv2.cvtColor(np.ascontiguousarray(img), cv2.COLOR_RGB2GRAY)
        gray = cv2.equalizeHist(gray)
        for casc in _CASCADES:
            for box in casc.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6,
                                             minSize=(int(min(w, h) * 0.12), int(min(w, h) * 0.12))):
                faces.append(tuple(int(v) for v in box))
    except cv2.error:
        faces = []
    if faces:
        fx, fy, fw, fh = max(faces, key=lambda b: b[2] * b[3])
        cx, cy = fx + fw / 2, fy + fh / 2
        # head-and-shoulders framing, but never so tight it becomes an eye close-up
        side = min(max(int(max(fw, fh) * 2.1), int(min(w, h) * 0.45)), w, h)
        x0 = int(min(max(cx - side / 2, 0), w - side))
        y0 = int(min(max(cy - side * 0.42, 0), h - side))  # nudge up to keep hair in frame
    else:
        side = min(w, h)  # no face found: portraits keep the head at the very top
        x0 = (w - side) // 2
        y0 = 0 if h >= w else int((h - side) * 0.12)
    img = img.crop((x0, y0, x0 + side, y0 + side)).resize((TARGET, TARGET), Image.LANCZOS)
    out = BytesIO()
    img.save(out, "JPEG", quality=82, optimize=True)
    return out.getvalue()


def collect_players(data: dict, only: set[str] | None) -> list[tuple[str, str]]:
    """(name, team_code) for every distinct player, first team wins."""
    seen: dict[str, str] = {}
    for code, team in data["teams"].items():
        if only and code not in only:
            continue
        for p in team["lineup"]:
            seen.setdefault(p["name"], code)
        for s in team.get("stars", []):
            seen.setdefault(s["name"], code)
    return [(n, c) for n, c in seen.items()]


def fetch_one(name: str, code: str, force: bool) -> tuple[str, dict | None, str]:
    out_path = IMG_DIR / code / f"{slugify(name)}.jpg"
    rel = f"img/{code}/{slugify(name)}.jpg"
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        try:
            if out_path.exists() and not force:
                # keep image, but we still need credit metadata if missing
                return name, {"img": rel, "_keep": True}, "cached"
            titles = candidate_titles(client, name)
            if not titles:
                return name, None, "no page"
            found = next((f for t in titles if (f := page_image(client, t))), None)
            if not found:
                return name, None, "no verified image"
            thumb_url, file_title, page_url = found
            img_bytes = polite_get(client, thumb_url).content
            face = crop_face(img_bytes)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(face)
            lic = image_license(client, file_title) if file_title else {}
            return name, {
                "img": rel,
                "credit": lic.get("author", "Unknown"),
                "license": lic.get("license", "see source"),
                "src": lic.get("descriptionurl") or page_url,
            }, "ok"
        except Exception as e:  # network / parse — skip this player, never fabricate
            return name, None, f"error: {type(e).__name__}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated team codes")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    only = set(args.only.split(",")) if args.only else None
    players = collect_players(data, only)
    print(f"sourcing faces for {len(players)} players...", flush=True)

    faces: dict = {}
    if FACES_FILE.exists():
        faces = json.loads(FACES_FILE.read_text(encoding="utf-8"))

    ok = skipped = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_one, n, c, args.force): n for n, c in players}
        for fut in concurrent.futures.as_completed(futures):
            name, rec, status = fut.result()
            if rec is None:
                skipped += 1
                print(f"  – {name}: {status}", flush=True)
                continue
            if rec.get("_keep"):
                # already had the image; keep existing credit if present
                if name not in faces:
                    faces[name] = {"img": rec["img"], "credit": "Unknown", "license": "see source", "src": ""}
                ok += 1
                continue
            faces[name] = rec
            ok += 1
            print(f"  ✓ {name} [{rec['license']}]", flush=True)

    # prune faces whose image file no longer exists
    for name in list(faces):
        if not (ROOT / "frontend" / faces[name]["img"]).exists():
            del faces[name]

    FACES_FILE.write_text(json.dumps(faces, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\ndone: {ok} with portraits, {skipped} fell back to number circle", flush=True)
    print(f"faces.json -> {FACES_FILE}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
