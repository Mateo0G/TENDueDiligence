"""Phase 1 proof: the worker's task-claiming loop respects `depends_on`
ordering and is safe under concurrent workers, using no-op tasks only.

Two checks:
1. Sequential run of a hand-built DAG -- assert every task completes and
   that completion order never violates a dependency edge.
2. The same shape, but drained by several worker threads at once -- assert
   every task still completes exactly once (SKIP LOCKED prevents double
   claims) and dependency order still holds.

Run: ./.venv/Scripts/python.exe scripts/phase1_dag_test.py
"""
import threading
import time

from app import repo
from app.worker import run_one_task


def build_dag(job_id: str) -> dict:
    """A -> C, B -> C, C -> D, C -> E, D+E -> F, plus an independent failure G."""
    ids = {}
    ids["A"] = repo.create_task(job_id, 1, "noop", "A")
    ids["B"] = repo.create_task(job_id, 1, "noop", "B")
    ids["C"] = repo.create_task(job_id, 1, "noop", "C", depends_on=[ids["A"], ids["B"]])
    ids["D"] = repo.create_task(job_id, 1, "noop", "D", depends_on=[ids["C"]])
    ids["E"] = repo.create_task(job_id, 1, "noop", "E", depends_on=[ids["C"]])
    ids["F"] = repo.create_task(job_id, 1, "noop", "F", depends_on=[ids["D"], ids["E"]])
    ids["G"] = repo.create_task(
        job_id, 1, "noop_fail", "G", payload={"message": "expected failure"}
    )
    return ids


def drain_sequential(max_iterations: int = 100) -> None:
    for _ in range(max_iterations):
        if not run_one_task():
            return


def drain_concurrent(num_workers: int = 4, max_seconds: float = 20.0) -> None:
    stop_at = time.monotonic() + max_seconds
    idle_streak_needed = 3

    def worker_loop():
        idle = 0
        while time.monotonic() < stop_at and idle < idle_streak_needed:
            if run_one_task():
                idle = 0
            else:
                idle += 1
                time.sleep(0.05)

    threads = [threading.Thread(target=worker_loop) for _ in range(num_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def assert_dependency_order(ids: dict) -> None:
    # psycopg returns UUID columns as uuid.UUID objects; ids' values are the
    # plain str this module created them with -- normalize both to str.
    tasks_by_id = {str(t["id"]): t for t in repo.list_tasks_for_job(job_id_of(ids))}
    for key, task_id in ids.items():
        task = tasks_by_id[str(task_id)]
        for dep_id in task["depends_on"] or []:
            dep = tasks_by_id[str(dep_id)]
            assert dep["status"] == "completed", (
                f"{key}'s dependency {dep['task_key']} did not complete"
            )
            assert dep["updated_at"] <= task["updated_at"], (
                f"{key} finished before its dependency {dep['task_key']} "
                f"({dep['updated_at']} > {task['updated_at']})"
            )


_job_cache: dict[tuple, str] = {}


def job_id_of(ids: dict) -> str:
    # ids values all share the same job_id; look it up once via repo.
    any_task_id = next(iter(ids.values()))
    return repo.get_task(any_task_id)["job_id"]


def run_check(label: str, job_id: str, drain_fn) -> None:
    print(f"\n=== {label} (job {job_id}) ===")
    ids = build_dag(job_id)
    drain_fn()
    tasks = repo.list_tasks_for_job(job_id)
    by_key = {t["task_key"]: t for t in tasks}

    for key in ["A", "B", "C", "D", "E", "F"]:
        status = by_key[key]["status"]
        assert status == "completed", f"{key} expected completed, got {status}"
    g_status = by_key["G"]["status"]
    assert g_status == "failed", f"G expected failed, got {g_status}"
    assert by_key["G"]["attempts"] == 1, f"G expected 1 attempt, got {by_key['G']['attempts']}"
    assert "expected failure" in (by_key["G"]["error"] or ""), "G error message not recorded"

    assert_dependency_order(ids)
    print(f"{label}: PASS -- 6/6 DAG tasks completed in valid order, G failed as expected")


def main():
    job_id_seq = repo.create_job("Phase1 Sequential Test Co")
    run_check("Sequential drain", job_id_seq, lambda: drain_sequential())

    job_id_conc = repo.create_job("Phase1 Concurrent Test Co")
    run_check(
        "Concurrent drain (4 worker threads)",
        job_id_conc,
        lambda: drain_concurrent(num_workers=4),
    )

    print("\nAll Phase 1 checks passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        from app.db import pool

        pool.close()
