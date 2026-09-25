"""Общие помощники для sync-тестов, использующих async-код.

Единственная точка определения ``_run`` — устраняет дублирование
в трёх тестовых файлах, накопленное в Фазах 4 и 5.

Применение и обоснование — см.
``docs/architecture-decisions/notes/async-test-isolation.md``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

_T = TypeVar("_T")


def _run(coro: Coroutine[Any, Any, _T]) -> _T:
    """Запускает корутину в отдельном потоке с собственным event loop.

    Обходит утечку running loop от ``pytest-asyncio`` 1.4.0 +
    Python 3.14: после завершения первого async-теста running loop
    остаётся в главном потоке, и любой последующий ``asyncio.run``
    падает с ``RuntimeError: asyncio.run() cannot be called from a
    running event loop``. Отдельный поток гарантированно не имеет
    running loop, поэтому ``asyncio.run`` внутри него всегда
    создаёт свежий loop.

    Подробности — в
    ``docs/architecture-decisions/notes/async-test-isolation.md``.

    Args:
        coro: Корутина для выполнения.

    Returns:
        Результат корутины.

    Raises:
        BaseException: Любое исключение, поднятое корутиной
            (пробрасывается в главный поток).
    """
    results: list[_T] = []
    errors: list[BaseException] = []

    def _target() -> None:
        try:
            results.append(asyncio.run(coro))
        except BaseException as e:
            errors.append(e)

    thread = threading.Thread(target=_target)
    thread.start()
    thread.join()

    if errors:
        raise errors[0]
    return results[0]
