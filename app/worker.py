import concurrent.futures
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

# Most of a task's wall-clock time is spent waiting on the Anthropic API
# (network I/O, GIL released), not on CPU -- so a thread pool inside one
# process gets real parallelism without needing separate Railway replicas.
# repo.claim_task's SELECT ... FOR UPDATE SKIP LOCKED is what makes this
# safe: concurrent claimants never double-claim the same row. Independent
# Stage 2/4 tasks (most sections have no dependency on each other) now run
# at the same time instead of strictly one-after-another; the dependent
# chains (e.g. executive_summary waiting on everything) still serialize
# themselves naturally through depends_on.
WORKER_CONCURRENCY = int(os.environ.get("WORKER_CONCURRENCY", "6"))

_shutdown_requested = False


def _handle_shutdown_signal(signum, frame):
    global _shutdown_requested
    log.info("received signal %s, will stop after in-flight tasks finish", signum)
    _shutdown_requested = True


def run_one_task(worker_id: str) -> bool:
    """Claim and run a single ready task. Returns False if nothing was ready."""
    task = repo.claim_task(worker_id)
    if task is None:
        return False

    log.info("[%s] claimed task %s (%s / %s)", worker_id, task["id"], task["task_type"], task["task_key"])
    try:
        handler = get_handler(task["task_type"])
        result = handler(task)
    except Exception as exc:
        log.exception("[%s] task %s failed", worker_id, task["id"])
        repo.fail_task(task["id"], str(exc))
    else:
        repo.complete_task(task["id"], result)
        log.info("[%s] task %s completed", worker_id, task["id"])
    return True


def _worker_loop(slot: int) -> None:
    worker_id = f"{WORKER_ID}-{slot}"
    while not _shutdown_requested:
        did_work = run_one_task(worker_id)
        if not did_work:
            time.sleep(POLL_INTERVAL_SECONDS)


def main():
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    log.info("worker %s starting with %d concurrent slots", WORKER_ID, WORKER_CONCURRENCY)
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKER_CONCURRENCY) as pool:
        futures = [pool.submit(_worker_loop, slot) for slot in range(WORKER_CONCURRENCY)]
        concurrent.futures.wait(futures)
    log.info("worker %s shutting down cleanly", WORKER_ID)


if __name__ == "__main__":
    main()
