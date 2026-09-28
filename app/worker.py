import logging
import os
import signal
import socket
import time

from app import repo
from app.tasks.registry import get_handler
import app.tasks  # noqa: F401  (registers handlers)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
POLL_INTERVAL_SECONDS = 1.0

_shutdown_requested = False


def _handle_shutdown_signal(signum, frame):
    global _shutdown_requested
    log.info("received signal %s, will stop after the current task finishes", signum)
    _shutdown_requested = True


def run_one_task() -> bool:
    """Claim and run a single ready task. Returns False if nothing was ready."""
    task = repo.claim_task(WORKER_ID)
    if task is None:
        return False

    log.info("claimed task %s (%s / %s)", task["id"], task["task_type"], task["task_key"])
    try:
        handler = get_handler(task["task_type"])
        result = handler(task)
    except Exception as exc:
        log.exception("task %s failed", task["id"])
        repo.fail_task(task["id"], str(exc))
    else:
        repo.complete_task(task["id"], result)
        log.info("task %s completed", task["id"])
    return True


def main():
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    log.info("worker %s starting", WORKER_ID)
    while not _shutdown_requested:
        did_work = run_one_task()
        if not did_work:
            time.sleep(POLL_INTERVAL_SECONDS)
    log.info("worker %s shutting down cleanly", WORKER_ID)


if __name__ == "__main__":
    main()
