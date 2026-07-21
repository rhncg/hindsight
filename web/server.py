"""
server.py — Flask app that serves the Hindsight web mockups and the JSON/SSE API.

This replaces `streamlit run app.py`. The Python logic that used to live inside
the Streamlit script now lives behind the /api/* endpoints (see api.py), and the
UI is plain HTML/CSS/JS served from web/mockups.

Run it:
    python web/server.py
Then open:
    http://127.0.0.1:5000/            → gallery of the 3 mockups
    http://127.0.0.1:5000/m/1         → mockup 1 (Warm / Claude-inspired)
    http://127.0.0.1:5000/m/2         → mockup 2 (Slate / ChatGPT-inspired)
    http://127.0.0.1:5000/m/3         → mockup 3 (Aurora / modern glass)
"""

import sys
from pathlib import Path

# Make the repo root importable so api.py can `import db, query, capture_service`.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask, abort, redirect, send_from_directory  # noqa: E402

import db  # noqa: E402
from api import api  # noqa: E402

MOCKUP_DIR = Path(__file__).parent / "mockups"
ASSET_DIR = ROOT / "assets"
SCREENSHOT_DIR = ROOT / "data" / "screenshots"

MOCKUPS = {
    "1": ("mockup1.html", "Warm", "Claude-inspired — warm charcoal, cozy bubbles"),
    "2": ("mockup2.html", "Slate", "ChatGPT-inspired — minimal neutral, flat rows"),
    "3": ("mockup3.html", "Aurora", "Modern glass — deep black, orange glow"),
}

app = Flask(__name__)
app.register_blueprint(api)

# The API layer relies on relative paths (data/hindsight.db, data/screenshots).
# Run everything from the repo root so those resolve exactly as before.
import os  # noqa: E402

os.chdir(ROOT)
db.init_db()


@app.get("/")
def index():
    """Chosen design: Mockup 1 (Warm). Land straight on it."""
    return redirect("/m/1")


@app.get("/mockups")
def all_mockups():
    """One page that holds all three mockups — a switcher up top, the live
    mockup rendered full-size in an iframe below. Also has a side-by-side view."""
    tabs = "\n".join(
        f"""<button class="tab{' active' if k=='1' else ''}" data-k="{k}"
             onclick="show('{k}')"><b>{name}</b><span>{desc}</span></button>"""
        for k, (_, name, desc) in MOCKUPS.items()
    )
    frames = "\n".join(
        f"""<iframe class="frame" data-k="{k}" src="/m/{k}"
             style="{'display:block' if k=='1' else 'display:none'}"></iframe>"""
        for k in MOCKUPS
    )
    grid = "\n".join(
        f"""<div class="gcell"><div class="glabel">{k} · {name}</div>
             <iframe src="/m/{k}"></iframe></div>"""
        for k, (_, name, _) in MOCKUPS.items()
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hindsight — Mockups</title>
<link rel="icon" href="/assets/logo.png">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  html,body {{ height:100%; }}
  body {{ margin:0; background:#0f0d0b; color:#efe9e2;
         font-family:Inter,system-ui,sans-serif; display:flex; flex-direction:column; height:100vh; overflow:hidden; }}
  .bar {{ display:flex; align-items:center; gap:16px; padding:10px 16px; border-bottom:1px solid #241f1a;
          background:#141210; flex-shrink:0; }}
  .brand {{ display:flex; align-items:center; gap:10px; padding-right:8px; }}
  .brand img {{ width:28px; height:28px; image-rendering:pixelated; }}
  .brand h1 {{ font-family:'Instrument Serif',serif; font-weight:400; font-size:24px; margin:0; color:#fff; }}
  .tabs {{ display:flex; gap:8px; flex:1; }}
  .tab {{ display:flex; flex-direction:column; align-items:flex-start; gap:1px; text-align:left; cursor:pointer;
          padding:7px 14px; border-radius:11px; border:1px solid #2c2620; background:#1a1613; color:#b3a89a;
          font-family:inherit; transition:.14s; }}
  .tab:hover {{ border-color:#3d342a; color:#efe9e2; }}
  .tab.active {{ border-color:#d2591e; background:#241811; color:#fff; }}
  .tab b {{ font-family:'Instrument Serif',serif; font-weight:400; font-size:17px; }}
  .tab span {{ font-size:11px; color:#877c6d; }}
  .tab.active span {{ color:#c9a68a; }}
  .viewtoggle {{ display:flex; gap:2px; background:#1a1613; border:1px solid #2c2620; border-radius:10px; padding:3px; }}
  .viewtoggle button {{ padding:6px 12px; border:none; background:none; color:#9a8f82; cursor:pointer;
          border-radius:7px; font-family:inherit; font-size:13px; }}
  .viewtoggle button.on {{ background:#d2591e; color:#fff; }}
  .stage {{ flex:1; position:relative; background:#0b0a09; }}
  .frame {{ position:absolute; inset:0; width:100%; height:100%; border:none; }}
  /* side-by-side grid */
  .grid {{ position:absolute; inset:0; display:none; grid-template-columns:repeat(3,1fr); gap:1px; background:#241f1a; }}
  .grid.on {{ display:grid; }}
  .single-hidden {{ display:none !important; }}
  .gcell {{ position:relative; background:#0b0a09; overflow:hidden; display:flex; flex-direction:column; }}
  .glabel {{ padding:6px 12px; font-size:12px; color:#c9a68a; background:#141210; border-bottom:1px solid #241f1a; flex-shrink:0; }}
  .gcell iframe {{ flex:1; width:100%; border:none; }}
  @media(max-width:900px){{ .tab span{{display:none;}} }}
</style></head>
<body>
  <div class="bar">
    <div class="brand"><img src="/assets/logo.png"><h1>Hindsight.</h1></div>
    <div class="tabs">{tabs}</div>
    <div class="viewtoggle">
      <button id="v-single" class="on" onclick="setView('single')">Single</button>
      <button id="v-grid" onclick="setView('grid')">Side by side</button>
    </div>
  </div>
  <div class="stage">
    <div id="single">{frames}</div>
    <div id="grid" class="grid">{grid}</div>
  </div>

  <script>
    function show(k){{
      document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active', t.dataset.k===k));
      document.querySelectorAll('.frame').forEach(f=>f.style.display = f.dataset.k===k ? 'block':'none');
    }}
    function setView(mode){{
      const grid = document.getElementById('grid'), single = document.getElementById('single');
      const isGrid = mode==='grid';
      grid.classList.toggle('on', isGrid);
      single.classList.toggle('single-hidden', isGrid);
      document.getElementById('v-grid').classList.toggle('on', isGrid);
      document.getElementById('v-single').classList.toggle('on', !isGrid);
    }}
  </script>
</body></html>"""


@app.get("/m/<key>")
def mockup(key: str):
    if key not in MOCKUPS:
        abort(404)
    return send_from_directory(MOCKUP_DIR, MOCKUPS[key][0])


@app.get("/assets/<path:name>")
def assets(name: str):
    return send_from_directory(ASSET_DIR, name)


@app.get("/screenshots/<path:name>")
def screenshots(name: str):
    if not SCREENSHOT_DIR.exists():
        abort(404)
    return send_from_directory(SCREENSHOT_DIR, name)


@app.get("/favicon.ico")
def favicon():
    return redirect("/assets/logo.png")


if __name__ == "__main__":
    print("Hindsight web → http://127.0.0.1:5000")
    app.run(debug=True, port=5000)
