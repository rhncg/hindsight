"""
capture.py — continuous screen capture with change detection.

Takes a screenshot every INTERVAL seconds, compares it against a rolling
window of recently-saved frames using a perceptual hash, and only writes it
to disk if the screen looks meaningfully different from all of them — so
alt-tabbing back to a screen we just captured doesn't save it again. This is
stage 1 and 2 of the pipeline — Gemma is called in the background so that the
capture loop is never blocked.

Run it:
    python capture.py
Stop it:
    Ctrl+C
"""

import json
import logging
import time
import subprocess
import queue
import threading
from collections import deque
from datetime import datetime
from io import BytesIO
from pathlib import Path

import imagehash
import mss
import ollama
from mss.base import MSSBase
from PIL import Image

import db

INTERVAL_SECONDS = 6
HASH_DIFF_THRESHOLD = 5
RECENT_HASH_WINDOW = 12
SAVE_DIR = Path("data/screenshots")
MONITOR_INDEX = 1

OLLAMA_MODEL = "gemma4:e4b"

ANALYSIS_PROMPT = (
    "You are looking at a screenshot of someone's screen. "
    "Analyze it carefully. Extract metadata and write a concise summary that describes the user's primary activity.\n"
    "Identify any active app, website, or document. Pay special attention to scheduled events, calendar invites, "
    "incoming/outgoing messages, emails, registration forms, summer camps, invoice/receipt details, and topics being read/written.\n\n"
    "Respond with ONLY a JSON object matching this exact shape (no markdown backticks, no preamble):\n"
    "{\n"
    '  "app_name": "Name of the active application, browser tab, website domain, or system window (e.g. Chrome - Google Calendar, Slack)",\n'
    '  "raw_text": "Extract literal high-value terms: calendar event titles, times, attendees, names of places, camp names, email subjects, sender/receiver names, registration numbers, codes, dates, task list items, and exact search-worthy phrases",\n'
    '  "summary": "A detailed one-sentence summary explaining exactly what the user is doing or viewing on this screen (e.g., Scheduling a dentist appointment on calendar, Reading a summer camp brochure for Camp Yosemite)"\n'
    "}"
)

SAVE_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("capture")


def get_active_app_macos() -> str:
    """
    Get the name of the frontmost application using macOS osascript,
    with a fallback to lsappinfo. Does not require Accessibility permissions.
    """
    try:
        cmd = "osascript -e 'name of application (path to frontmost application as text)'"
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=0.8)
        app_name = res.stdout.strip()
        if app_name:
            return app_name
    except Exception:
        pass

    try:
        cmd = 'lsappinfo info $(lsappinfo front) | head -n 1 | cut -d \'"\' -f 2'
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=0.8)
        app_name = res.stdout.strip()
        if app_name:
            return app_name
    except Exception:
        pass

    return "Unknown"


def take_screenshot(sct: MSSBase, monitor_index: int) -> Image.Image:
    """Grab the current screen as a PIL Image."""
    monitor = sct.monitors[monitor_index]
    raw = sct.grab(monitor)
    return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


def perceptual_hash(img: Image.Image) -> imagehash.ImageHash:
    """
    Compute a perceptual hash of the image. Similar-looking images produce
    similar hashes — so we can measure how different two frames are.
    """
    return imagehash.phash(img)


def has_changed(
    new_hash: imagehash.ImageHash,
    recent_hashes: "deque[imagehash.ImageHash]",
) -> bool:
    """
    Decide whether a frame is different enough from the recently-saved frames
    to be worth keeping. Returns True only if it differs from EVERY hash in
    the recent window — so returning to a screen we captured moments ago
    (e.g. A -> B -> A window flipping) is treated as a duplicate, not a new
    frame. Comparing only against the single previous frame missed those.
    """
    if not recent_hashes:
        return True
    closest = min(new_hash - h for h in recent_hashes)
    return closest > HASH_DIFF_THRESHOLD


def save_screenshot(img: Image.Image, captured_at: datetime) -> Path:
    filename = captured_at.strftime("%Y-%m-%d_%H-%M-%S")
    path = SAVE_DIR / f"{filename}.png"
    img.save(path)
    return path


def prepare_image_for_analysis(img: Image.Image, max_dim: int = 1024) -> bytes:
    """
    Resize image to a maximum dimension while maintaining aspect ratio,
    and compress it to JPEG format to minimize payload size and accelerate inference.
    """
    analysis_img = img.copy()

    w, h = analysis_img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        new_size = (int(w * scale), int(h * scale))
        analysis_img = analysis_img.resize(new_size, Image.Resampling.LANCZOS)

    if analysis_img.mode != "RGB":
        analysis_img = analysis_img.convert("RGB")

    buf = BytesIO()
    analysis_img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def analyze_screenshot(img: Image.Image, active_app: str) -> dict:
    """
    Send the screenshot to a local Gemma model via the ollama client and
    get back structured extracted content.
    """
    compressed_bytes = prepare_image_for_analysis(img)

    enriched_prompt = f"Active Application: {active_app}\n\n{ANALYSIS_PROMPT}"

    response = ollama.generate(
        model=OLLAMA_MODEL,
        prompt=enriched_prompt,
        images=[compressed_bytes],
        format="json",
    )
    text = response["response"].strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Gemma response wasn't valid JSON, storing raw text as-is")
        data = {"app_name": None, "raw_text": text, "summary": text}

    return data


def process_analysis_queue(row_id: int, image_path: Path, active_app: str):
    """Worker task that runs background analysis and updates the database row."""
    try:
        log.info("Background analysis started for row %d (%s)", row_id, image_path.name)
        start_time = time.time()

        with Image.open(image_path) as img:
            extracted = analyze_screenshot(img, active_app)

        elapsed = time.time() - start_time

        final_app_name = active_app if active_app and active_app != "Unknown" else (extracted.get("app_name") or "Unknown")

        with db.get_conn() as conn:
            conn.execute(
                """
                UPDATE screenshots
                SET analyzed_at = ?,
                    app_name = ?,
                    raw_text = ?,
                    summary = ?
                WHERE id = ?
                """,
                (
                    datetime.now().isoformat(),
                    final_app_name,
                    extracted.get("raw_text"),
                    extracted.get("summary"),
                    row_id,
                ),
            )
            conn.commit()

        log.info(
            "Background analysis finished for row %d in %.2fs: %s",
            row_id,
            elapsed,
            extracted.get("summary"),
        )
    except Exception as e:
        log.error("Background analysis failed for row %d: %s", row_id, e)


analysis_queue = queue.Queue()


def worker_loop():
    """Background worker loop that processes screenshots sequentially from the queue."""
    while True:
        try:
            task = analysis_queue.get()
            if task is None:
                break

            row_id, image_path, active_app = task
            process_analysis_queue(row_id, image_path, active_app)
        except Exception as e:
            log.error("Error in background analysis worker loop: %s", e)
        finally:
            analysis_queue.task_done()


def run():
    db.init_db()
    log.info(
        "Starting capture loop (interval=%ss, threshold=%s, window=%s)",
        INTERVAL_SECONDS,
        HASH_DIFF_THRESHOLD,
        RECENT_HASH_WINDOW,
    )
    recent_hashes: "deque[imagehash.ImageHash]" = deque(maxlen=RECENT_HASH_WINDOW)
    saved_count = 0
    skipped_count = 0

    worker_thread = threading.Thread(target=worker_loop, daemon=True)
    worker_thread.start()

    with mss.MSS() as sct:
        try:
            while True:
                img = take_screenshot(sct, MONITOR_INDEX)
                current_hash = perceptual_hash(img)

                if has_changed(current_hash, recent_hashes):
                    captured_at = datetime.now()
                    active_app = get_active_app_macos()
                    path = save_screenshot(img, captured_at)
                    saved_count += 1
                    log.info(
                        "Saved  %s [App: %s]  (saved=%d, skipped=%d)",
                        path.name,
                        active_app,
                        saved_count,
                        skipped_count,
                    )
                    recent_hashes.append(current_hash)

                    try:
                        row_id = db.insert_screenshot(
                            captured_at=captured_at.isoformat(),
                            image_path=str(path),
                            image_hash=str(current_hash),
                            raw_text="",
                            summary="Analyzing in background...",
                            app_name=active_app,
                        )
                        analysis_queue.put((row_id, path, active_app))
                    except Exception as e:
                        log.error("Failed to queue screenshot for analysis: %s", e)
                else:
                    skipped_count += 1

                time.sleep(INTERVAL_SECONDS)
        except KeyboardInterrupt:
            log.info("Stopping... canceling pending analysis tasks.")
            while not analysis_queue.empty():
                try:
                    analysis_queue.get_nowait()
                    analysis_queue.task_done()
                except queue.Empty:
                    break
            analysis_queue.put(None)
            log.info("Stopped. Total saved=%d, skipped=%d", saved_count, skipped_count)


if __name__ == "__main__":
    run()
