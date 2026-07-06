"""Build dist/index.html: inject data/worldcup.json into the frontend template.

The template carries the display font as a data URI already; the only injection
point is the `/*__DATA__*/ null` marker. Output is fully self-contained.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "frontend" / "worldcup26.template.html"
ABOUT_TEMPLATE = ROOT / "frontend" / "about.template.html"
FONT_B64 = ROOT / "frontend" / "oswald.b64"
DATA_FILE = ROOT / "data" / "worldcup.json"
DIST = ROOT / "dist" / "index.html"
ABOUT_DIST = ROOT / "dist" / "about.html"

MARKER = "/*__DATA__*/ null"
FONT_MARKER = "/*OSWALD_B64*/"
UPDATED_MARKER = "/*UPDATED*/"


def build(data: dict | None = None) -> Path:
    template = TEMPLATE.read_text(encoding="utf-8")
    if MARKER not in template:
        raise RuntimeError(f"marker {MARKER!r} missing from {TEMPLATE} — template is broken")
    if data is None:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    from .models import WorldCupData
    WorldCupData.model_validate(data)

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    DIST.parent.mkdir(parents=True, exist_ok=True)
    DIST.write_text(template.replace(MARKER, payload), encoding="utf-8")

    about = ABOUT_TEMPLATE.read_text(encoding="utf-8")
    if FONT_MARKER not in about or UPDATED_MARKER not in about:
        raise RuntimeError(f"markers missing from {ABOUT_TEMPLATE} — template is broken")
    about = about.replace(FONT_MARKER, FONT_B64.read_text().strip())
    about = about.replace(UPDATED_MARKER, data["updated"])
    ABOUT_DIST.write_text(about, encoding="utf-8")
    return DIST


if __name__ == "__main__":
    print(build())
