from typing import Callable

TaskHandler = Callable[[dict], dict]

_REGISTRY: dict[str, TaskHandler] = {}


def register(task_type: str):
    def deco(fn: TaskHandler) -> TaskHandler:
        _REGISTRY[task_type] = fn
        return fn

    return deco


def get_handler(task_type: str) -> TaskHandler:
    return _REGISTRY[task_type]
