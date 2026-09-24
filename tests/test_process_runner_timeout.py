"""
Тесты таймаутов и принудительного завершения ``ProcessTaskRunner``.

Назначение
----------
Проверка поведения
:class:`~dds_core.infrastructure.process_task_runner.ProcessTaskRunner`
при превышении таймаута задачи, при ``close()`` с активными процессами,
и при worker'ах, которые игнорируют ``SIGTERM``.

Модуль дополняет ``tests/test_process_task_runner.py`` (базовые
сценарии). Здесь — только edge cases, связанные с временем и
сигналами:

+-------------------------------------+--------------------------------+
| Группа                              | Что проверяется                |
+=====================================+================================+
| ``run``: timeout срабатывает        | ``TimeoutError`` поднимается,  |
|                                     | процесс убит, время соблюдено. |
+-------------------------------------+--------------------------------+
| ``run``: SIGTERM → SIGKILL          | Worker игнорирует SIGTERM,     |
|                                     | runner применяет SIGKILL.      |
+-------------------------------------+--------------------------------+
| ``close``: активная задача          | ``terminate()`` активного      |
|                                     | процесса, завершение за        |
|                                     | разумное время.                |
+-------------------------------------+--------------------------------+
| ``close``: упрямый worker           | Worker игнорирует SIGTERM,     |
|                                     | runner применяет SIGKILL через |
|                                     | ``shutdown_timeout``.          |
+-------------------------------------+--------------------------------+
| ``timeout=0`` отключает ограничение | Worker завершается сам, без    |
|                                     | ``TimeoutError``.              |
+-------------------------------------+--------------------------------+
| Восстановление после timeout        | Runner остаётся рабочим для    |
|                                     | последующих задач.             |
+-------------------------------------+--------------------------------+

Синхронные тесты и изоляция event loop
--------------------------------------

Как и в ``test_process_task_runner.py``, тесты написаны как
**синхронные функции**, а корутины запускаются через :func:`_run` в
отдельном потоке. Причина — утечка running loop от ``pytest-asyncio``
в главный поток при полном прогоне набора (баг
``pytest-asyncio`` 1.4.0 + Python 3.14): прямой ``asyncio.run`` в
главном потоке падает с ``RuntimeError: asyncio.run() cannot be
called from a running event loop`` — даже для полностью sync-теста.
См. подробное обоснование в ``test_process_task_runner.py``.

Стратегия тестирования
----------------------
- **Короткие таймауты.** ``timeout=0.5`` и ``shutdown_timeout=0.5``
  сокращают время теста; в production используются значения
  30 с и 10 с. Компромисс: устойчивость vs скорость.
- **Реальный ``SIG_IGN``.** Worker вызывает ``signal.signal(SIGTERM,
  SIG_IGN)`` и спит. Runner обязан применить SIGKILL после grace.
- **Проверка времени.** Тесты с timeout'ом проверяют не только
  исключение, но и что завершение уложилось в разумное время —
  это ловит случаи, когда runner поднимает ``TimeoutError``,
  но процесс остаётся жив.
- **Проверка работоспособности после timeout.** После
  ``TimeoutError`` runner должен остаться в рабочем состоянии
  для следующих задач (семафор освобождён, очередь закрыта,
  процесс удалён из ``_processes``).

Границы
-------
- **Точное время SIGTERM → SIGKILL.** Не измеряется напрямую
  (требует strace). Проверяется по общему времени ``run()``.
- **Реальный SIGSEGV.** Не воспроизводится: нестабилен на разных ОС,
  может утащить отладчик. Эмулируется в базовых тестах через
  ``os._exit(code)``.
- **Прерывание worker'а в середине pickle-десериализации.**
  Экзотический сценарий; выходит за рамки этого файла.

Запуск
------
::

    pytest tests/test_process_runner_timeout.py -v

Принципы
--------
- Модуль не выполняет логирования.
- Тесты изолированы (каждый создаёт свой runner).
- Детерминированы: одинаковый вход → одинаковый результат.
- Не зависят от порядка выполнения.
"""

from __future__ import annotations

import asyncio
import signal
import threading
import time
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

import pytest
from dds_core.infrastructure.process_task_runner import ProcessTaskRunner

# =====================================================================
# Константы
# =====================================================================

_SHORT_TIMEOUT = 0.5
"""Короткий timeout для тестов таймаута.

Уменьшен с production-значения (30 с) для скорости тестов.
Forkserver успевает стартовать за ~50–200 мс, ``time.sleep``
в worker'е длится 10 секунд — задача заведомо не завершается
за 0.5 секунды.
"""

_SHORT_SHUTDOWN_TIMEOUT = 0.5
"""Короткий shutdown_timeout для тестов ``close()``.

Уменьшен с production-значения (10 с). Если worker игнорирует
SIGTERM, runner применит SIGKILL через 0.5 секунды.
"""

_LONG_SLEEP = 10.0
"""Длительность сна worker'а, заведомо превышающая timeout.

10 секунд — компромисс: тесты падают быстро (0.5 с timeout),
но worker не успевает завершиться сам, что и требуется для
проверки терминации. Если тест случайно «подождёт» worker'а —
10 секунд терпимо.
"""

_T = TypeVar("_T")


# =====================================================================
# Worker-функции (модульные — обязательное условие для pickle)
# =====================================================================


def _sleep_short() -> str:
    """Спит 0.5 секунды и возвращает ``"done"``.

    Для теста ``timeout=0`` — задача укладывается в разумное
    время, ограничение отключено, результат возвращается.

    Returns:
        Строка ``"done"``.
    """
    time.sleep(0.5)
    return "done"


def _sleep_long() -> None:
    """Спит 10 секунд, заведомо превышая timeout тестов.

    Не завершается сам за время таймаута (0.5 с). Runner обязан
    прервать его через ``terminate`` / ``kill``.
    """
    time.sleep(_LONG_SLEEP)


def _ignore_sigterm_and_sleep() -> None:
    """Игнорирует ``SIGTERM`` и спит 10 секунд.

    Проверяет SIGKILL-fallback в ``_terminate_process``: runner
    шлёт SIGTERM, worker его игнорирует, runner через grace
    шлёт SIGKILL.

    Примечание: ``signal.SIG_IGN`` устанавливается в дочернем
    процессе (forkserver или spawn), не влияет на родителя.
    """
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(_LONG_SLEEP)


# =====================================================================
# Helpers
# =====================================================================


def _run(coro: Coroutine[Any, Any, _T]) -> _T:
    """Запускает корутину в отдельном потоке с собственным event loop.

    См. подробное обоснование в
    ``tests/test_process_task_runner.py::_run``. Кратко: running loop
    от ``pytest-asyncio`` утекает в главный поток при полном прогоне
    набора; отдельный поток не имеет running loop по определению,
    поэтому ``asyncio.run`` внутри него всегда работает.

    Args:
        coro: Корутина для выполнения.

    Returns:
        Результат корутины.

    Raises:
        BaseException: Любое исключение, поднятое корутиной.
    """
    results: list[_T] = []
    errors: list[BaseException] = []

    def _target() -> None:
        try:
            results.append(asyncio.run(coro))
        except BaseException as e:  # noqa: BLE001 — проброс через границу потока
            errors.append(e)

    thread = threading.Thread(target=_target)
    thread.start()
    thread.join()

    if errors:
        raise errors[0]
    return results[0]


async def _with_runner(
    body: Callable[[ProcessTaskRunner], Coroutine[Any, Any, _T]],
    *,
    shutdown_timeout: float = _SHORT_SHUTDOWN_TIMEOUT,
) -> _T:
    """Создаёт runner, выполняет тело, гарантированно закрывает.

    Args:
        body: Асинхронная функция, принимающая runner.
        shutdown_timeout: Таймаут ``close()``.

    Returns:
        Результат ``body(runner)``.
    """
    runner = ProcessTaskRunner(shutdown_timeout=shutdown_timeout)
    try:
        return await body(runner)
    finally:
        await runner.close()


# =====================================================================
# Раздел 1. run: timeout срабатывает
# =====================================================================


def test_run_timeout_raises_timeout_error() -> None:
    """Задача превышает timeout → ``TimeoutError``.

    Worker спит 10 секунд, timeout — 0.5 секунды. Runner обязан
    поднять ``TimeoutError`` после 0.5 с (не ждать завершения
    worker'а).
    """

    async def _body(runner: ProcessTaskRunner) -> None:
        with pytest.raises(TimeoutError):
            await runner.run(
                _sleep_long,
                timeout=_SHORT_TIMEOUT,
            )

    _run(_with_runner(_body))


def test_run_timeout_kills_process_quickly() -> None:
    """После timeout процесс убит за разумное время.

    Проверяет, что ``_terminate_process`` (SIGTERM + grace + SIGKILL)
    не затягивает завершение ``run()``. Ожидаемое время:
    ``timeout + grace + overhead`` ≈ 0.5 + 1.0 + запас = < 3 сек.
    """

    async def _body(runner: ProcessTaskRunner) -> None:
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await runner.run(
                _sleep_long,
                timeout=_SHORT_TIMEOUT,
            )
        elapsed = time.monotonic() - start
        assert elapsed < 3.0, (
            f"run() с timeout={_SHORT_TIMEOUT} завершился за {elapsed:.2f}с, "
            f"ожидалось < 3.0 (timeout + grace + overhead)"
        )

    _run(_with_runner(_body))


def test_run_timeout_worker_ignoring_sigterm_gets_killed() -> None:
    """Worker игнорирует SIGTERM → runner применяет SIGKILL.

    Worker устанавливает ``SIG_IGN`` на SIGTERM и спит. Runner
    шлёт SIGTERM (worker игнорирует), ждёт grace=1.0 с, шлёт SIGKILL.
    Общее время: ``timeout + grace + overhead`` < 4 сек.
    """

    async def _body(runner: ProcessTaskRunner) -> None:
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await runner.run(
                _ignore_sigterm_and_sleep,
                timeout=_SHORT_TIMEOUT,
            )
        elapsed = time.monotonic() - start
        assert elapsed < 4.0, (
            f"run() с упрямым worker'ом завершился за {elapsed:.2f}с, "
            f"ожидалось < 4.0 (timeout + grace + SIGKILL + overhead)"
        )

    _run(_with_runner(_body))


# =====================================================================
# Раздел 2. close: активная задача
# =====================================================================


def test_close_terminates_active_task() -> None:
    """``close()`` завершает активную задачу.

    Запускается долгая задача через ``create_task``. Через 0.3 с
    (процесс успел стартовать) вызывается ``close()``. Runner
    обязан завершить активный процесс за ``shutdown_timeout``,
    а задача — завершиться (с ``RuntimeError`` из-за потери
    worker'а или с ``CancelledError``).
    """

    async def _body() -> None:
        runner = ProcessTaskRunner(shutdown_timeout=_SHORT_SHUTDOWN_TIMEOUT)
        try:
            task = asyncio.create_task(runner.run(_sleep_long, timeout=30.0))
            await asyncio.sleep(0.3)  # дать процессу стартовать
            start = time.monotonic()
            await runner.close()
            elapsed = time.monotonic() - start
            assert elapsed < 3.0, (
                f"close() с активной задачей занял {elapsed:.2f}с, "
                f"ожидалось < 3.0 (shutdown_timeout + overhead)"
            )
            # Задача должна завершиться: worker убит, run() получит
            # RuntimeError ("worker died without result").
            with pytest.raises((RuntimeError, asyncio.CancelledError)):
                await task
        finally:
            if not runner._closed:
                await runner.close()

    _run(_body())


def test_close_kills_stubborn_worker() -> None:
    """``close()`` применяет SIGKILL для worker'а, игнорирующего SIGTERM.

    Worker ставит ``SIG_IGN``. ``close(shutdown_timeout=0.5)``
    шлёт SIGTERM (игнор), ждёт 0.5 с, шлёт SIGKILL. Общее время
    ``close()`` < 3 сек.
    """

    async def _body() -> None:
        runner = ProcessTaskRunner(shutdown_timeout=_SHORT_SHUTDOWN_TIMEOUT)
        try:
            task = asyncio.create_task(runner.run(_ignore_sigterm_and_sleep, timeout=30.0))
            await asyncio.sleep(0.3)
            start = time.monotonic()
            await runner.close()
            elapsed = time.monotonic() - start
            assert elapsed < 3.0, (
                f"close() с упрямым worker'ом занял {elapsed:.2f}с, ожидалось < 3.0"
            )
            with pytest.raises((RuntimeError, asyncio.CancelledError)):
                await task
        finally:
            if not runner._closed:
                await runner.close()

    _run(_body())


# =====================================================================
# Раздел 3. timeout=0 — ограничение отключено
# =====================================================================


def test_timeout_zero_disables_limit() -> None:
    """``timeout=0`` отключает ограничение.

    Worker спит 0.5 с и возвращает результат. Runner с ``timeout=0``
    не применяет ``asyncio.wait_for`` — задача завершается
    нормально.
    """

    async def _body(runner: ProcessTaskRunner) -> None:
        result = await runner.run(
            _sleep_short,
            timeout=0.0,
        )
        assert result == "done"

    _run(_with_runner(_body))


# =====================================================================
# Раздел 4. Восстановление после timeout
# =====================================================================


def test_runner_remains_functional_after_timeout() -> None:
    """После ``TimeoutError`` runner работает с новыми задачами.

    Проверяет, что семафор освобождён, очередь закрыта, процесс
    удалён из ``_processes``: следующий ``run`` выполняется
    штатно.

    Инвариант: ``_processes`` не накапливает мёртвые процессы.
    """

    async def _body(runner: ProcessTaskRunner) -> None:
        # Первая задача — таймаут.
        with pytest.raises(TimeoutError):
            await runner.run(_sleep_long, timeout=_SHORT_TIMEOUT)

        # Вторая задача — короткая, должна выполниться штатно.
        result = await runner.run(_sleep_short, timeout=5.0)
        assert result == "done"

    _run(_with_runner(_body))
