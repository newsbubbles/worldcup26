"""Build dist/ from the templates + data.

dist/index.html    — the match center (data + faces injected)
dist/about.html    — the project page
dist/img/**        — player portraits (copied from frontend/img)

Two image modes:
  relative (default) — cards reference img/<code>/<slug>.jpg; dist/img is copied
                       alongside. Right for GitHub Pages / any static host.
  inline  (inline=True) — every portrait is base64'd into a data: URI so the HTML
                       is one fully self-contained file. Right for the Artifact
                       (its CSP blocks all external/relative requests).
"""

from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "frontend" / "worldcup26.template.html"
ABOUT_TEMPLATE = ROOT / "frontend" / "about.template.html"
FONT_B64 = ROOT / "frontend" / "oswald.b64"
DATA_FILE = ROOT / "data" / "worldcup.json"
FACES_FILE = ROOT / "data" / "faces.json"
IMG_SRC = ROOT / "frontend" / "img"
DIST = ROOT / "dist" / "index.html"
ABOUT_DIST = ROOT / "dist" / "about.html"
IMG_DIST = ROOT / "dist" / "img"

MARKER = "/*__DATA__*/ null"
FONT_MARKER = "/*OSWALD_B64*/"
UPDATED_MARKER = "/*UPDATED*/"


def _load_faces(inline: bool) -> dict:
    if not FACES_FILE.exists():
        return {}
    faces = json.loads(FACES_FILE.read_text(encoding="utf-8"))
    if not inline:
        return faces
    out = {}
    for name, rec in faces.items():
        fp = ROOT / "frontend" / rec["img"]
        rec = dict(rec)
        if fp.exists():
            rec["img"] = "data:image/jpeg;base64," + base64.b64encode(fp.read_bytes()).decode()
        out[name] = rec
    return out


def render_index(data: dict) -> str:
    template = TEMPLATE.read_text(encoding="utf-8")
    if MARKER not in template:
        raise RuntimeError(f"marker {MARKER!r} missing from {TEMPLATE} — template is broken")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return template.replace(MARKER, payload)


def build(data: dict | None = None, inline: bool = False) -> Path:
    if data is None:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    data = {**data, "faces": _load_faces(inline)}

    from .models import WorldCupData
    WorldCupData.model_validate(data)

    DIST.parent.mkdir(parents=True, exist_ok=True)
    DIST.write_text(render_index(data), encoding="utf-8")

    if not inline and IMG_SRC.exists():
        shutil.copytree(IMG_SRC, IMG_DIST, dirs_exist_ok=True)

    about = ABOUT_TEMPLATE.read_text(encoding="utf-8")
    if FONT_MARKER not in about or UPDATED_MARKER not in about:
        raise RuntimeError(f"markers missing from {ABOUT_TEMPLATE} — template is broken")
    about = about.replace(FONT_MARKER, FONT_B64.read_text().strip())
    about = about.replace(UPDATED_MARKER, data["updated"])
    ABOUT_DIST.write_text(about, encoding="utf-8")
    return DIST


def build_inline_string(data: dict | None = None) -> str:
    """Return the fully self-contained index HTML (portraits inlined) — for the Artifact."""
    if data is None:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    data = {**data, "faces": _load_faces(inline=True)}
    from .models import WorldCupData
    WorldCupData.model_validate(data)
    return render_index(data)


if __name__ == "__main__":
    print(build())
