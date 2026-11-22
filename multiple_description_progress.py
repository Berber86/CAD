"""Safe progress reporting for the optional multi-description workflow."""
from __future__ import annotations
from contextlib import contextmanager
import threading
import time
from typing import Callable, Sequence


def emit(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is not None:
        try:
            callback(message)
        except Exception:
            # Display failures must not cause retries or alter the result.
            pass


def page_list(pages: Sequence[int]) -> str:
    shown = ', '.join(str(n) for n in pages[:20])
    return shown + (f' и ещё {len(pages) - 20}' if len(pages) > 20 else '')


@contextmanager
def waiting(callback: Callable[[str], None] | None, *, interval: float = 15.0):
    """A heartbeat while a blocking request is in flight; never sends requests."""
    if callback is None:
        yield
        return
    stopped = threading.Event()
    started = time.monotonic()

    def heartbeat() -> None:
        while not stopped.wait(interval):
            elapsed = int(time.monotonic() - started)
            emit(callback, f"Ожидаем ответ провайдера: {elapsed} с. Запрос уже отправлен.")

    thread = threading.Thread(target=heartbeat, daemon=True, name='multi-progress')
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()
