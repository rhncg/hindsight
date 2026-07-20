"""
app.py — a Streamlit chat UI over your screenshot history.

Same two-step flow as query.py:
  1. Gemma turns your question into FTS5 search terms (+ optional time range).
  2. Those terms retrieve candidate rows from the local index, and a second
     Gemma call synthesizes an answer grounded only in what was retrieved.

Run it:
    streamlit run app.py
"""

import json
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

import capture_service
import db
import query

LOGO_PATH = "assets/logo.png"
WORDMARK_PATH = "assets/wordmark.png"

st.set_page_config(page_title="Hindsight", page_icon=LOGO_PATH)
st.logo(WORDMARK_PATH, size="large", icon_image=LOGO_PATH)

SCREENSHOT_DIR = Path("data/screenshots")
MODEL_OPTIONS = ["gemma4:e4b", "gemma4:12b"]

db.init_db()


def clear_all_history() -> tuple[int, int]:
    """Delete every screenshot row and its image file, returning
    (rows_deleted, files_deleted)."""
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

    return len(paths), files_deleted


def collect_screenshots(rows: list) -> list[dict]:
    """Turn retrieved rows into a display-ready list of screenshots on disk."""
    shots = []
    for r in rows:
        path = r["image_path"]
        if not path or not Path(path).exists():
            continue
        shots.append(
            {
                "path": path,
                "captured_at": r["captured_at"],
                "app_name": r["app_name"],
                "summary": r["summary"],
            }
        )
    return shots


def _format_caption(shot: dict) -> str:
    ts = shot["captured_at"]
    try:
        ts = datetime.fromisoformat(ts).strftime("%b %d, %I:%M %p")
    except (ValueError, TypeError):
        pass
    app = shot.get("app_name") or "unknown app"
    return f"{ts} · {app}"


def render_screenshots(shots: list[dict]) -> None:
    """Show the screenshots that back an answer as a thumbnail gallery."""
    if not shots:
        return
    shots = [s for s in shots if s.get("path") and Path(s["path"]).exists()]
    if not shots:
        return
    with st.expander(f"📸 Related screenshots ({len(shots)})", expanded=True):
        cols = st.columns(3)
        for i, shot in enumerate(shots):
            with cols[i % 3]:
                st.image(shot["path"], caption=_format_caption(shot), width="stretch")
                if shot.get("summary"):
                    st.caption(shot["summary"])


def render_elapsed(elapsed_s: float | None) -> None:
    """Show how long an answer took, at the bottom of the response."""
    if elapsed_s is None:
        return
    st.caption(f":material/timer: Answered in {elapsed_s:.1f}s")


def make_title(text: str, limit: int = 40) -> str:
    """Derive a short conversation title from the first question."""
    collapsed = " ".join(text.split())
    if not collapsed:
        return "New chat"
    return collapsed[:limit] + ("…" if len(collapsed) > limit else "")


def load_conversation(conversation_id: int) -> None:
    """Point session state at a conversation and hydrate its messages."""
    st.session_state.conversation_id = conversation_id
    messages = []
    for row in db.get_conversation_messages(conversation_id):
        messages.append(
            {
                "role": row["role"],
                "content": row["content"],
                "screenshots": json.loads(row["screenshots"]) if row["screenshots"] else [],
                "elapsed_s": row["elapsed_s"],
            }
        )
    st.session_state.messages = messages


def _parse_dt(value: str | None) -> datetime | None:
    """ISO string -> datetime so DatetimeColumn can format it; None if unset."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def render_data_browser() -> None:
    """Show everything the DB holds about the user as browsable tables."""
    shots = db.get_recent(limit=1000)
    convos = db.get_conversation_overview()

    total_col, convo_col = st.columns(2)
    total_col.metric("Screenshots captured", len(shots))
    convo_col.metric("Conversations", len(convos))

    st.markdown("**Screenshots**")
    if shots:
        st.dataframe(
            [
                {
                    "Captured": _parse_dt(r["captured_at"]),
                    "App": r["app_name"],
                    "Summary": r["summary"],
                    "Extracted text": r["raw_text"],
                    "File": r["image_path"],
                }
                for r in shots
            ],
            column_config={
                "Captured": st.column_config.DatetimeColumn(
                    "Captured", format="MMM DD, YYYY · h:mm a"
                ),
            },
            hide_index=True,
            width="stretch",
        )
        if len(shots) == 1000:
            st.caption("Showing the 1000 most recent screenshots.")
    else:
        st.caption("No screenshots captured yet. Run `capture.py` to start.")

    st.markdown("**Conversations**")
    if convos:
        st.dataframe(
            [
                {
                    "Title": c["title"],
                    "Messages": c["message_count"],
                    "Started": _parse_dt(c["created_at"]),
                    "Last active": _parse_dt(c["updated_at"]),
                }
                for c in convos
            ],
            column_config={
                "Started": st.column_config.DatetimeColumn(
                    "Started", format="MMM DD, YYYY · h:mm a"
                ),
                "Last active": st.column_config.DatetimeColumn(
                    "Last active", format="MMM DD, YYYY · h:mm a"
                ),
            },
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("No conversations yet.")


def render_danger_zone() -> None:
    """Destructive maintenance actions, guarded by a confirm step."""
    st.caption("Permanently delete every captured screenshot and its indexed history.")
    if st.session_state.get("confirm_clear"):
        st.warning(
            "Delete all screenshots and history? This can't be undone.",
            icon=":material/warning:",
        )
        confirm_col, cancel_col = st.columns(2)
        if confirm_col.button("Delete everything", type="primary", width="stretch"):
            rows, files = clear_all_history()
            st.session_state.confirm_clear = False
            st.toast(
                f"Deleted {rows} entries and {files} screenshot files.",
                icon=":material/delete_sweep:",
            )
            st.rerun()
        if cancel_col.button("Cancel", width="stretch"):
            st.session_state.confirm_clear = False
    else:
        if st.button("Clear all history", icon=":material/delete_forever:", width="stretch"):
            st.session_state.confirm_clear = True


def render_capture_controls() -> None:
    """Start/stop the background capture service, plus a log tail in debug mode."""
    running = capture_service.is_running()
    status = ":green[● Running]" if running else ":gray[● Stopped]"
    st.markdown(f"**Capture service** — {status}")
    st.caption("Continuously screenshots your screen and indexes what's on it.")

    if running:
        if st.button("Stop capture", icon=":material/stop:", width="stretch"):
            capture_service.stop()
            st.toast("Capture service stopped.", icon=":material/stop_circle:")
    else:
        if st.button(
            "Start capture", icon=":material/play_arrow:", type="primary", width="stretch"
        ):
            capture_service.start()
            st.toast("Capture service started.", icon=":material/videocam:")

    if st.session_state.get("debug_mode"):
        st.markdown("**Capture logs**")
        logs = capture_service.read_logs()
        st.code(logs or "(no logs yet — start the capture service)", language="log")
        st.button("Refresh logs", icon=":material/refresh:")


@st.dialog("Settings", width="large")
def settings_dialog() -> None:
    general_tab, data_tab, danger_tab = st.tabs(["General", "Your data", "Danger zone"])
    with general_tab:
        current_model = st.session_state.get("model_name", query.QUERY_MODEL)
        st.session_state.model_name = st.selectbox(
            "Model",
            MODEL_OPTIONS,
            index=MODEL_OPTIONS.index(current_model) if current_model in MODEL_OPTIONS else 0,
            help="e4b is faster; 12b answers better but runs slower. Both must be pulled in ollama.",
        )
        st.session_state.debug_mode = st.toggle(
            "Debug mode",
            value=st.session_state.get("debug_mode", False),
            help="Show the parsed search terms and retrieved entries for each answer.",
        )
        st.divider()
        render_capture_controls()
    with data_tab:
        render_data_browser()
    with danger_tab:
        render_danger_zone()


if "conversation_id" not in st.session_state:
    existing = db.list_conversations()
    load_conversation(existing[0]["id"] if existing else db.create_conversation())

query.QUERY_MODEL = st.session_state.get("model_name", query.QUERY_MODEL)

header = st.container(horizontal=True, vertical_alignment="center")
header.image(LOGO_PATH, width=52)
header.title("Hindsight")
st.caption("Ask about your screen history")

with st.sidebar:
    if st.button("Settings", icon=":material/settings:", width="stretch"):
        settings_dialog()

    st.divider()
    st.subheader("Conversations")

    if st.button("New chat", icon=":material/add:", width="stretch"):
        if st.session_state.messages:
            load_conversation(db.create_conversation())
        st.rerun()

    conversations = db.list_conversations()
    for convo in conversations:
        cid = convo["id"]
        active = cid == st.session_state.conversation_id
        if st.button(
            convo["title"] or "New chat",
            key=f"conv_{cid}",
            type="primary" if active else "secondary",
            width="stretch",
        ):
            load_conversation(cid)
            st.rerun()

    active_title = next(
        (c["title"] for c in conversations if c["id"] == st.session_state.conversation_id),
        "New chat",
    )
    rename_col, delete_col = st.columns(2)
    with rename_col.popover("Rename", icon=":material/edit:", width="stretch"):
        new_title = st.text_input("Conversation title", value=active_title, key="rename_input")
        if st.button("Save", width="stretch"):
            db.rename_conversation(
                st.session_state.conversation_id, new_title.strip() or "New chat"
            )
            st.rerun()
    if delete_col.button("Delete", icon=":material/delete:", width="stretch"):
        db.delete_conversation(st.session_state.conversation_id)
        remaining = db.list_conversations()
        load_conversation(remaining[0]["id"] if remaining else db.create_conversation())
        st.rerun()

debug = st.session_state.get("debug_mode", False)
query.DEBUG = False

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("screenshots"):
            render_screenshots(msg["screenshots"])
        render_elapsed(msg.get("elapsed_s"))
        if debug and msg.get("debug"):
            with st.expander("Retrieval details"):
                st.json(msg["debug"]["terms"])
                st.markdown("**Retrieved entries:**")
                st.code(msg["debug"]["context"], language=None)

question = st.chat_input("Ask about what you saw…")

if question:
    conversation_id = st.session_state.conversation_id
    history = list(st.session_state.messages)

    if not history:
        db.rename_conversation(conversation_id, make_title(question))

    st.session_state.messages.append({"role": "user", "content": question})
    db.add_message(conversation_id, "user", question)
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        started = time.perf_counter()
        with st.spinner("Searching your history…"):
            terms = query.extract_search_terms(question, history=history)
            rows = query.retrieve(terms)
            answer = query.synthesize_answer(question, rows, history=history)
        elapsed_s = time.perf_counter() - started

        screenshots = collect_screenshots(rows)

        st.markdown(answer)
        render_screenshots(screenshots)
        render_elapsed(elapsed_s)

        debug_payload = {"terms": terms, "context": query.format_context(rows)}
        if debug:
            with st.expander("Retrieval details"):
                st.json(terms)
                st.markdown("**Retrieved entries:**")
                st.code(debug_payload["context"], language=None)

    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": answer,
            "screenshots": screenshots,
            "elapsed_s": elapsed_s,
            "debug": debug_payload,
        }
    )
    db.add_message(
        conversation_id, "assistant", answer, screenshots=screenshots, elapsed_s=elapsed_s
    )

    st.rerun()
