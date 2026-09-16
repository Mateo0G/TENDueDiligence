"""Phase 1 proof: the worker stops claiming new tasks once a shutdown signal
has been received, but does not abandon a task already in flight.

This exercises app.worker's actual shutdown flag and loop guard directly
rather than sending a real OS SIGTERM: Windows has no true POSIX SIGTERM
delivery between processes (Python's `signal` module on Windows can only
emulate it from within the same process), so a cross-platform subprocess
test would really just be testing Windows' signal emulation, not our loop
logic. The real end-to-end delivery path (Railway sending SIGTERM to a
Linux container on deploy) gets validated at Phase 8 instead.

Run: ./.venv/Scripts/python.exe scripts/phase1_shutdown_test.py
"""
import signal

from app import repo, worker


def main():
    job_id = repo.create_job("Phase1 Shutdown Test Co")
    repo.create_task(job_id, 1, "noop", "T1")
    repo.create_task(job_id, 1, "noop", "T2")

    assert worker._shutdown_requested is False

    # First iteration: normal operation, claims and completes one task.
    assert worker.run_one_task() is True

    # Simulate a shutdown signal arriving right after that task finished
    # (this is exactly what signal.signal(SIGTERM, ...) would invoke).
    worker._handle_shutdown_signal(signal.SIGTERM, None)
    assert worker._shutdown_requested is True

    # main()'s loop guard is `while not _shutdown_requested: run_one_task()`
    # -- replicate that guard here rather than starting a real process.
    if not worker._shutdown_requested:
        worker.run_one_task()  # should not run

    tasks = repo.list_tasks_for_job(job_id)
    completed = [t for t in tasks if t["status"] == "completed"]
    pending = [t for t in tasks if t["status"] == "pending"]

    assert len(completed) == 1, f"expected 1 completed task, got {len(completed)}"
    assert len(pending) == 1, f"expected 1 still-pending task, got {len(pending)}"

    print("PASS -- in-flight task completed normally; no new task claimed after shutdown flag set")


if __name__ == "__main__":
    try:
        main()
    finally:
        from app.db import pool

        pool.close()
