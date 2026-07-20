"""
capture_service.py — start/stop the background capture.py process and read its
logs, so the Streamlit app can control capture without importing capture.py's
heavy, capture-only dependencies (mss, imagehash, PIL, ...).

The running process is tracked with a PID file so its state survives Streamlit's
frequent reruns and even an app restart. Its stdout/stderr are teed to a log
file that the UI can tail.
"""

import os
import signal
import subprocess
import sys
from pathlib import Path

DATA_DIR = Path("data")
PID_FILE = DATA_DIR / "capture.pid"
LOG_FILE = DATA_DIR / "capture.log"
CAPTURE_SCRIPT = Path(__file__).with_name("capture.py")


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid exists. Signal 0 performs the existence
    check without actually delivering a signal."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_running() -> bool:
    """Whether the capture process is currently alive. Clears a stale PID file
    left behind by a crashed or externally-killed process."""
    pid = _read_pid()
    if pid is None:
        return False
    if _pid_alive(pid):
        return True
    PID_FILE.unlink(missing_ok=True)
    return False


def start() -> bool:
    """Launch capture.py in the background. Returns False if it was already
    running (no second copy is started)."""
    if is_running():
        return False

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_fh = open(LOG_FILE, "w")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(CAPTURE_SCRIPT)],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(CAPTURE_SCRIPT.parent),
        )
    finally:
        log_fh.close()

    PID_FILE.write_text(str(proc.pid))
    return True


def stop() -> bool:
    """Ask the capture process to shut down. Returns False if nothing was
    running. Uses SIGINT so capture.py runs its graceful KeyboardInterrupt
    path (drain queue, log "Stopping...")."""
    pid = _read_pid()
    if pid is None:
        return False

    stopped = False
    try:
        os.kill(pid, signal.SIGINT)
        stopped = True
    except ProcessLookupError:
        pass
    PID_FILE.unlink(missing_ok=True)
    return stopped


def read_logs(max_lines: int = 200) -> str:
    """Return the tail of the capture log."""
    try:
        lines = LOG_FILE.read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return ""
    return "\n".join(lines[-max_lines:])
