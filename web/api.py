"""
api.py — Flask blueprint that exposes Hindsight's Python logic as a JSON/SSE API.

This is the same two-step flow the Streamlit app used, lifted out of the UI:
  1. `query.extract_search_terms` turns a question into FTS5 terms (+ time range)
  2. `query.retrieve` pulls candidate rows, `query.stream_synthesize_answer`
     streams a grounded answer token-by-token.

The frontend (the mockups in web/mockups) talks to these endpoints instead of
Streamlit widgets. Screenshot images are served by the parent app under
/screenshots/<name>.

Everything here wraps db.py / query.py / capture_service.py — no business logic
lives in the UI anymore.
"""

import json
import time
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Response, jsonify, request, stream_with_context

# The project modules live one directory up; server.py puts the repo root on
# sys.path before importing this blueprint.
import capture_service
import db
import query

api = Blueprint("api", __name__)

SCREENSHOT_DIR = Path("data/screenshots")
MODEL_OPTIONS = ["gemma4:e4b", "gemma4:12b"]

# In-memory app settings. The Streamlit version kept these in st.session_state;
# here they live on the server process for the life of the run.
SETTINGS = {
    "model_name": query.QUERY_MODEL,
    "debug_mode": False,
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _row(row) -> dict:
    """sqlite3.Row -> plain dict so it JSON-serializes."""
    return {k: row[k] for k in row.keys()}


def collect_screenshots(rows: list) -> list[dict]:
    """Turn retrieved rows into display-ready screenshots that exist on disk."""
    shots = []
    for r in rows:
        path = r["image_path"]
        if not path or not Path(path).exists():
            continue
        shots.append(
            {
                "url": "/screenshots/" + Path(path).name,
                "captured_at": r["captured_at"],
                "app_name": r["app_name"],
                "summary": r["summary"],
            }
        )
    return shots


def make_title(text: str, limit: int = 40) -> str:
    collapsed = " ".join(text.split())
    if not collapsed:
        return "New chat"
    return collapsed[:limit] + ("…" if len(collapsed) > limit else "")


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).isoformat()
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# conversations
# --------------------------------------------------------------------------- #
@api.get("/api/conversations")
def list_conversations():
    return jsonify([_row(c) for c in db.list_conversations()])


@api.post("/api/conversations")
def create_conversation():
    cid = db.create_conversation()
    return jsonify({"id": cid, "title": "New chat"}), 201


@api.patch("/api/conversations/<int:cid>")
def rename_conversation(cid: int):
    title = (request.json or {}).get("title", "").strip() or "New chat"
    db.rename_conversation(cid, title)
    return jsonify({"id": cid, "title": title})


@api.delete("/api/conversations/<int:cid>")
def delete_conversation(cid: int):
    db.delete_conversation(cid)
    return jsonify({"ok": True})


@api.get("/api/conversations/<int:cid>/messages")
def conversation_messages(cid: int):
    messages = []
    for row in db.get_conversation_messages(cid):
        messages.append(
            {
                "role": row["role"],
                "content": row["content"],
                "screenshots": json.loads(row["screenshots"]) if row["screenshots"] else [],
                "elapsed_s": row["elapsed_s"],
            }
        )
    return jsonify(messages)


# --------------------------------------------------------------------------- #
# ask — Server-Sent Events stream of the grounded answer
# --------------------------------------------------------------------------- #
@api.post("/api/ask")
def ask():
    body = request.json or {}
    question = (body.get("question") or "").strip()
    cid = body.get("conversation_id")
    if not question or cid is None:
        return jsonify({"error": "question and conversation_id required"}), 400

    query.QUERY_MODEL = SETTINGS["model_name"]

    history = []
    for row in db.get_conversation_messages(cid):
        history.append({"role": row["role"], "content": row["content"]})

    if not history:
        db.rename_conversation(cid, make_title(question))

    db.add_message(cid, "user", question)

    def event_stream():
        started = time.perf_counter()
        terms = query.extract_search_terms(question, history=history)
        rows = query.retrieve(terms)
        yield _sse("terms", terms)

        parts = []
        for token in query.stream_synthesize_answer(question, rows, history=history):
            parts.append(token)
            yield _sse("token", {"text": token})

        answer = "".join(parts).strip()
        elapsed_s = time.perf_counter() - started
        screenshots = collect_screenshots(rows)

        db.add_message(cid, "assistant", answer, screenshots=screenshots, elapsed_s=elapsed_s)
        yield _sse("done", {"screenshots": screenshots, "elapsed_s": elapsed_s})

    return Response(stream_with_context(event_stream()), mimetype="text/event-stream")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# --------------------------------------------------------------------------- #
# settings, capture, data browser
# --------------------------------------------------------------------------- #
@api.get("/api/settings")
def get_settings():
    return jsonify({**SETTINGS, "model_options": MODEL_OPTIONS})


@api.post("/api/settings")
def update_settings():
    body = request.json or {}
    if "model_name" in body and body["model_name"] in MODEL_OPTIONS:
        SETTINGS["model_name"] = body["model_name"]
    if "debug_mode" in body:
        SETTINGS["debug_mode"] = bool(body["debug_mode"])
    return jsonify(SETTINGS)


@api.get("/api/capture/status")
def capture_status():
    return jsonify({"running": capture_service.is_running()})


@api.post("/api/capture/start")
def capture_start():
    capture_service.start()
    return jsonify({"running": capture_service.is_running()})


@api.post("/api/capture/stop")
def capture_stop():
    capture_service.stop()
    return jsonify({"running": capture_service.is_running()})


@api.get("/api/capture/logs")
def capture_logs():
    return jsonify({"logs": capture_service.read_logs()})


@api.get("/api/data")
def data_browser():
    shots = db.get_recent(limit=1000)
    convos = db.get_conversation_overview()
    return jsonify(
        {
            "screenshot_count": len(shots),
            "conversation_count": len(convos),
            "screenshots": [
                {
                    "captured_at": _parse_dt(r["captured_at"]),
                    "app_name": r["app_name"],
                    "summary": r["summary"],
                    "raw_text": r["raw_text"],
                    "image_path": r["image_path"],
                }
                for r in shots
            ],
            "conversations": [
                {
                    "title": c["title"],
                    "message_count": c["message_count"],
                    "created_at": _parse_dt(c["created_at"]),
                    "updated_at": _parse_dt(c["updated_at"]),
                }
                for c in convos
            ],
        }
    )


@api.post("/api/history/clear")
def clear_history():
    paths = db.clear_all()
    files_deleted = 0
    for p in paths:
        try:
            fp = Path(p)
            if fp.exists():
                fp.unlink()
                files_deleted += 1
        except OSError:
            pass
    if SCREENSHOT_DIR.exists():
        for fp in SCREENSHOT_DIR.glob("*.png"):
            try:
                fp.unlink()
                files_deleted += 1
            except OSError:
                pass
    return jsonify({"rows_deleted": len(paths), "files_deleted": files_deleted})
