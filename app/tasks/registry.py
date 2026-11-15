from typing import Callable

# Handlers receive the full claimed task row (id, job_id, task_type,
# task_key, payload, depends_on, attempts), not just payload -- so a
# handler can record its own task id (e.g. report_sections.source_task_id)
# without needing it duplicated into payload at creation time.
TaskHandler = Callable[[dict], dict]

_REGISTRY: dict[str, TaskHandler] = {}


def register(task_type: str):
    def deco(fn: TaskHandler) -> TaskHandler:
        _REGISTRY[task_type] = fn
        return fn

    return deco


def get_handler(task_type: str) -> TaskHandler:
    return _REGISTRY[task_type]


class TaskDeferred(Exception):
    """Raised by a handler that is waiting on something external (a Message
    Batch): the worker puts the task back to pending, not to be claimed for
    `seconds`, without counting it as a failed attempt. `payload`, if given,
    replaces the task's payload so the next run knows what it waits for."""

    def __init__(self, seconds: int, payload: dict | None = None, reason: str = ""):
        super().__init__(reason or f"deferred {seconds}s")
        self.seconds = seconds
        self.payload = payload
