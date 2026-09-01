"""Local dev server: serves docs/ and runs FPL.PY modes via /api/run.

Run:  venv/bin/python server.py
Open: http://localhost:8000
"""

import importlib.util
import os
import shutil
from importlib.machinery import SourceFileLoader

from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(BASE_DIR, "docs")
DOCS_DATA_DIR = os.path.join(DOCS_DIR, "data")
MIRROR_FILES = (
    "bracket_state_classic.json",
    "bracket_state_h2h.json",
    "dashboard.json",
    "contests.json",
)

# "FPL.PY" can't be imported normally — loader requires a lowercase .py suffix.
_loader = SourceFileLoader("fpl", os.path.join(BASE_DIR, "FPL.PY"))
_spec = importlib.util.spec_from_loader("fpl", _loader)
fpl = importlib.util.module_from_spec(_spec)
_loader.exec_module(fpl)

app = Flask(__name__, static_folder=DOCS_DIR, static_url_path="")


def mirror_state():
    os.makedirs(DOCS_DATA_DIR, exist_ok=True)
    for name in MIRROR_FILES:
        src = os.path.join(fpl.STATE_DIR, name)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(DOCS_DATA_DIR, name))


def _flag(name):
    return request.args.get(name, "false").lower() == "true"


@app.route("/")
def index():
    return send_from_directory(DOCS_DIR, "index.html")


@app.route("/api/run", methods=["POST"])
def run():
    mode = request.args.get("mode", "advance")
    league = request.args.get("league", "both")
    start_gw = int(request.args.get("start_gw", fpl.BASE_KNOCKOUT_START_GW))
    gw = request.args.get("gw", type=int)
    targets = fpl.league_targets(league)

    try:
        if mode == "qualify":
            result = (
                fpl.force_qualify_snapshot(gw, lock=_flag("lock"))
                if gw
                else fpl.run_qualify(start_gw)
            )
        elif mode == "draw":
            if _flag("force"):
                for lk in targets:
                    path = fpl.bracket_draw_path(lk)
                    if os.path.exists(path):
                        os.remove(path)
            result = {lk: fpl.load_or_create_bracket_draw(lk) for lk in targets}
        elif mode == "init-bracket":
            result = {lk: fpl.init_bracket(lk, start_gw) for lk in targets}
        elif mode == "advance":
            result = {lk: fpl.advance_bracket(lk, live=_flag("live")) for lk in targets}
        elif mode == "dashboard":
            result = fpl.build_dashboard()
        elif mode == "contests":
            result = fpl.compute_contests()
        else:
            return jsonify({"error": f"unknown mode: {mode}"}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    mirror_state()
    return jsonify({"mode": mode, "league": league, "result": result})


if __name__ == "__main__":
    mirror_state()
    app.run(port=8000, debug=True)
