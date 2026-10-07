"""Logged wrapper intended for Windows Task Scheduler or cron."""
from __future__ import annotations

# All imports are Python standard library.
import glob               # find old log files by filename pattern
import os                 # path handling, folders, deletion, modification times
import subprocess         # launch update.py as a child process
import sys                # reuse the same Python interpreter and forward arguments
from datetime import datetime

# run_daily.py is a logging/scheduling-friendly wrapper.  It does not contain the
# pricing logic itself: it starts update.py as a child process and captures all
# of its output in logs/update_<timestamp>.log.
ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(ROOT, "logs")
LOGS_TO_KEEP = 60


# Keep the log directory useful without growing forever.
def prune_logs() -> None:
    # Ensure the logs folder exists before searching it.
    os.makedirs(LOG_DIR, exist_ok=True)

    # glob returns ordinary path strings; sort newest-first using file modification time.
    pattern = os.path.join(LOG_DIR, "update_*.log")
    logs = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)

    # Delete only the older logs beyond the configured retention count.
    for path in logs[LOGS_TO_KEEP:]:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


# One invocation = one update attempt.  To make this happen automatically once
# per day, schedule this script with Windows Task Scheduler (or cron on Linux).
def main() -> int:
    # Create the log folder and remove only logs older than the retention limit.
    os.makedirs(LOG_DIR, exist_ok=True)
    prune_logs()

    # Timestamp in the filename keeps every update attempt easy to identify.
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_path = os.path.join(LOG_DIR, f"update_{stamp}.log")
    # Use the same Python interpreter that launched run_daily.py (important when
    # using a virtual environment), and forward optional CLI arguments.
    update_script = os.path.join(ROOT, "update.py")
    command = [sys.executable, update_script, *sys.argv[1:]]

    # Capture both normal output and errors in one UTF-8 log file.
    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"Started: {datetime.now().isoformat(timespec='seconds')}\n")
        log.write(f"Command: {' '.join(command)}\n\n")
        log.flush()
        # Wait for update.py to finish. stdout and stderr both go into the same log.
        # cwd=ROOT makes relative operations deterministic even when Task Scheduler
        # launches this script from another working directory.
        process = subprocess.run(
            command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, text=True
        )
        log.write(f"\nFinished: {datetime.now().isoformat(timespec='seconds')}\n")
        log.write(f"Exit code: {process.returncode}\n")
    # Print only the created log filename to the terminal, then mirror update.py's
    # success/failure exit code back to Task Scheduler or the calling shell.
    print(log_path)
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
