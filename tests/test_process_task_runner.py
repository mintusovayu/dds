"""
Тесты ``ProcessTaskRunner`` — базовые сценарии.

Назначение
----------
Проверка контракта
:class:`~dds_core.infrastructure.process_task_runner.ProcessTaskRunner`:
успешный запуск задачи в subprocess, ограничение параллелизма,
pickle-сериализация аргументов, корректный graceful shutdown,
поведение при падении worker'а без ответа, идемпотентность
``close()``.

Модуль покрывает **только базовые** сценарии. Тесты таймаутов
(SIGTERM → SIGKILL, worker игнорирует SIGTERM) — в отдельном файле
``tests/test_process_runner_timeout.py``.

Синхронные тесты и изоляция event loop
--------------------------------------

Тесты написаны как **синхронные функции**, а корутины запускаются
через helper :func:`_run`, который выполняет ``asyncio.run()`` в
**отдельном потоке**. Причина двухуровневая:

1. ``pytest-asyncio`` в режиме ``auto`` оборачивает тесты в общий
   event loop. Async-тесты оказываются в running loop, и forkserver
   в Python 3.14 (который внутри создаёт ``asyncio.Runner``) падает
   с ``RuntimeError: Runner.run() cannot be called from a running
   event loop``.
2. Даже **полностью sync** тест не спасает: при полном прогоне набора
   (300+ тестов) running loop остаётся активным в главном потоке от
   предыдущих async-тестов (баг ``pytest-asyncio`` 1.4.0 +
   Python 3.14). Прямой ``asyncio.run(coro)`` в этом случае падает с
   ``RuntimeError: asyncio.run() cannot be called from a running
   event loop`` — **до** создания forkserver'а, что подтверждено
   минимальным probe-тестом.

Отдельный поток **гарантированно** не имеет running loop: ``asyncio.run``
внутри него всегда создаёт свежий loop. Это изолирует наши тесты от
состояния, оставленного другими тестами, без правок pytest-asyncio
конфигурации и без изменения production-кода.

Проверяемые сценарии
--------------------

+-------------------------------------+--------------------------------+
| Группа                              | Что проверяется                |
+=====================================+================================+
| ``run``: успешное выполнение        | Возврат значения (int, str,    |
|                                     | list, dict, tuple, None).      |
+-------------------------------------+--------------------------------+
| ``run``: проброс исключений         | ``Exception`` и ``BaseException``|
|                                     | из worker'а → родителю.        |
+-------------------------------------+--------------------------------+
| ``run``: pickle-сериализация        | Несериализуемые аргументы →    |
|                                     | ``RuntimeError``.              |
+-------------------------------------+--------------------------------+
| ``run``: worker упал без ответа     | ``os._exit`` в worker'е →      |
|                                     | ``RuntimeError`` с exitcode.   |
+-------------------------------------+--------------------------------+
| ``run``: ограничение параллелизма   | ``asyncio.Semaphore`` ограничи-|
|                                     | вает число одновременных       |
|                                     | процессов.                     |
+-------------------------------------+--------------------------------+
| ``run``: раздельные пулы            | ``kind="batch"`` использует    |
|                                     | отдельный семафор.             |
+-------------------------------------+--------------------------------+
| ``run`` после ``close``             | ``RuntimeError``.              |
+-------------------------------------+--------------------------------+
| ``close``: идемпотентность          | Повторный вызов — no-op.       |
+-------------------------------------+--------------------------------+
| ``close``: пустой runner            | Без активных процессов —       |
|                                     | возврат сразу.                 |
+-------------------------------------+--------------------------------+

Стратегия тестирования
----------------------
- **Реальные subprocess.** Предмет теста — поведение реального
  процесса, включая fork, IPC и терминацию. Задачи стартуют за
  ~50–200 мс через forkserver.
- **Sync-обёртки над async API.** Каждый тест — sync-функция
  с внутренним ``_run(_test_body())``. См. выше о running loop.
- **Worker-функции на уровне модуля.** Pickle-сериализация требует,
  чтобы callable была определена в модуле (не вложенная функция,
  не lambda).
- **Файловый маркер для timing-тестов.** Для проверки параллелизма
  worker записывает ``(pid, start_ts, end_ts)`` в общий файл;
  тест вычисляет максимальное количество перекрывающихся интервалов.

Границы
-------
- **Таймауты.** Только базовое поведение (задача укладывается
  в timeout). Сценарии SIGTERM/SIGKILL — в
  ``test_process_runner_timeout.py``.
- **SIGSEGV.** Эмулируется через ``os._exit(code)``.
- **Производительность.** Замер latency fork — в бенчмарках.

Запуск
------
::

    pytest tests/test_process_task_runner.py -v

Принципы
--------
- Модуль не выполняет логирования.
- Тесты изолированы друг от друга (каждый создаёт свой runner).
- Детерминированы: одинаковый вход → одинаковый результат.
- Не зависят от порядка выполнения.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, TypeVar

import pytest
from dds_core.infrastructure.process_task_runner import ProcessTaskRunner

# =====================================================================
# Константы
# =====================================================================

_TEST_TIMEOUT = 30.0
"""Таймаут для ``run()`` в тестах.

Достаточно велик, чтобы forkserver успел стартовать (первый fork
медленный, ~200–500 мс на холодную) и задача выполнилась. Если
тест превышает этот таймаут — это баг, а не медленная машина.
"""

_TEST_SHUTDOWN_TIMEOUT = 2.0
"""Таймаут ``close()`` в тестах.

Уменьшен с production-значения (10 с) для скорости тестов. Если
worker не завершается за 2 секунды после SIGTERM — тест упадёт,
но это означает реальную проблему.
"""

_T = TypeVar("_T")


# =====================================================================
# Worker-функции (модульные — обязательное условие для pickle)
# =====================================================================


def _return_value(value: Any) -> Any:
    """Возвращает переданное значение без изменений.

    Используется для проверки транспорта значений через IPC.

    Args:
        value: Любое pickle-сериализуемое значение.

    Returns:
        То же самое значение.
    """
    return value


def _raise_value_error(message: str) -> None:
    """Поднимает ``ValueError`` с заданным сообщением.

    Проверяет проброс обычных исключений из worker'а в родителя.

    Args:
        message: Текст исключения.

    Raises:
        ValueError: Всегда.
    """
    raise ValueError(message)


def _raise_keyboard_interrupt() -> None:
    """Поднимает ``KeyboardInterrupt`` (``BaseException``).

    Проверяет, что runner не «теряет» BaseException-исключения
    (важно: стандартный ``except Exception`` их не поймает).

    Raises:
        KeyboardInterrupt: Всегда.
    """
    raise KeyboardInterrupt("simulated keyboard interrupt")


def _exit_without_result(code: int) -> None:
    """Завершает процесс без отправки результата.

    Эмулирует SIGSEGV: worker умирает, не отправив ничего в очередь.
    Родитель получит ``queue.Empty`` и ``exitcode != 0``.

    Args:
        code: Код возврата процесса.
    """
    os._exit(code)


def _record_interval(
    marker_path: str,
    pid: int,
    start_ts: float,
    end_ts: float,
) -> None:
    """Записывает интервал работы worker'а в файл (append-режим).

    Формат строки: ``{pid};{start_ts};{end_ts}\\n``. Append коротких
    строк через POSIX-файл атомарен на уровне ядра (O_APPEND) —
    достаточно для тестов.

    Args:
        marker_path: Путь к файлу-маркеру.
        pid: PID процесса worker'а.
        start_ts: Время начала в секундах (``time.monotonic``).
        end_ts: Время окончания в секундах.
    """
    with open(marker_path, "a", encoding="utf-8") as f:
        f.write(f"{pid};{start_ts};{end_ts}\n")


def _sleep_and_record(
    marker_path: str,
    duration: float,
) -> None:
    """Спит ``duration`` секунд и записывает интервал в файл.

    Используется для проверки параллелизма: если несколько worker'ов
    действительно работают параллельно, их интервалы в файле будут
    перекрываться.

    Args:
        marker_path: Путь к файлу-маркеру.
        duration: Длительность сна в секундах.
    """
    pid = os.getpid()
    start = time.monotonic()
    time.sleep(duration)
    end = time.monotonic()
    _record_interval(marker_path, pid, start, end)


# =====================================================================
# Helpers
# =====================================================================


def _run(coro: Coroutine[Any, Any, _T]) -> _T:
    """Запускает корутину в отдельном потоке с собственным event loop.

    **Зачем отдельный поток, а не прямой ``asyncio.run()``.**
    ``pytest-asyncio`` в режиме ``auto`` оборачивает тесты в общий
    event loop. При полном прогоне набора (300+ тестов) running loop
    остаётся активным в главном потоке от предыдущих async-тестов
    (баг ``pytest-asyncio`` 1.4.0 + Python 3.14). Прямой
    ``asyncio.run(coro)`` в этом случае падает с
    ``RuntimeError: asyncio.run() cannot be called from a running
    event loop`` — даже для полностью sync-теста, не имеющего
    отношения к asyncio.

    Отдельный поток не имеет running loop по определению —
    ``asyncio.run(coro)`` внутри него всегда создаёт свежий loop.
    Это изолирует тесты ``ProcessTaskRunner`` от состояния,
    оставленного другими тестами, и не требует ни правок
    pytest-asyncio конфигурации, ни изменения production-кода.

    **Оверхед.** Один ``threading.Thread`` на тест (~10 мкс на
    создание) — на 25 тестов это меньше 1 мс суммарно. Создание
    forkserver'а — ~200 мс, поток на его фоне пренебрежим.

    **Аннотация ``Coroutine[Any, Any, _T]``.** Соответствует
    typeshed: ``asyncio.run`` принимает именно ``Coroutine``, а не
    произвольный ``Awaitable``. Все вызовы ``_run`` — от ``async
    def``-функций (или от ``_with_runner``, возвращающего корутину),
    поэтому сужение типа корректно.

    **Проброс исключений.** Исключение из корутины сохраняется в
    списке ``errors`` и поднимается в главном потоке. ``pytest.raises``
    в вызывающем тесте видит его как обычно.

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
) -> _T:
    """Создаёт runner, выполняет тело, гарантированно закрывает.

    Стандартный сценарий теста: создать runner, выполнить операции,
    закрыть в ``finally``. Сокращает бойлерплейт.

    Аннотация ``body`` — ``Callable[..., Coroutine[...]]``, потому что
    вызывающий код передаёт ``async def``-функции. Вызов такой
    функции даёт ``Coroutine``, а не произвольный ``Awaitable``.

    Args:
        body: Асинхронная функция, принимающая runner.

    Returns:
        Результат ``body(runner)``.
    """
    runner = ProcessTaskRunner(shutdown_timeout=_TEST_SHUTDOWN_TIMEOUT)
    try:
        return await body(runner)
    finally:
        await runner.close()


def _read_intervals(marker_path: Path) -> list[tuple[int, float, float]]:
    """Читает интервалы из файла-маркера.

    Args:
        marker_path: Путь к файлу.

    Returns:
        Список кортежей ``(pid, start_ts, end_ts)``.
    """
    intervals: list[tuple[int, float, float]] = []
    content = marker_path.read_text(encoding="utf-8")
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_str, start_str, end_str = line.split(";")
        intervals.append((int(pid_str), float(start_str), float(end_str)))
    return intervals


def _max_overlap(intervals: list[tuple[int, float, float]]) -> int:
    """Вычисляет максимальное количество перекрывающихся интервалов.

    Sweep-line: собирает все начала и концы, сортирует,
    подсчитывает текущую глубину перекрытия.

    Args:
        intervals: Список ``(pid, start_ts, end_ts)``.

    Returns:
        Максимальное число одновременно перекрывающихся интервалов.
    """
    events: list[tuple[float, int]] = []
    for _pid, start, end in intervals:
        events.append((start, +1))
        events.append((end, -1))
    events.sort(key=lambda e: (e[0], -e[1]))  # +1 раньше -1 при равных

    depth = 0
    max_depth = 0
    for _ts, delta in events:
        depth += delta
        max_depth = max(max_depth, depth)
    return max_depth


# =====================================================================
# Раздел 1. run: успешное выполнение
# =====================================================================


def test_run_returns_integer() -> None:
    """Успешный ``run`` возвращает результат worker'а."""

    async def _body(runner: ProcessTaskRunner) -> None:
        result = await runner.run(
            _return_value,
            42,
            timeout=_TEST_TIMEOUT,
        )
        assert result == 42

    _run(_with_runner(_body))


@pytest.mark.parametrize(
    "value",
    [
        0,
        -1,
        2**31,
        "hello",
        "",
        "кириллица",
        [1, 2, 3],
        [],
        {"key": "value", "nested": {"a": 1}},
        (1, "two", 3.0),
        None,
        True,
        False,
    ],
    ids=[
        "zero",
        "negative",
        "large-int",
        "string",
        "empty-string",
        "cyrillic",
        "list",
        "empty-list",
        "dict",
        "tuple",
        "none",
        "true",
        "false",
    ],
)
def test_run_returns_various_types(value: Any) -> None:
    """Различные pickle-сериализуемые типы транспортируются корректно."""

    async def _body(runner: ProcessTaskRunner) -> None:
        result = await runner.run(
            _return_value,
            value,
            timeout=_TEST_TIMEOUT,
        )
        assert result == value
        assert type(result) is type(value)

    _run(_with_runner(_body))


# =====================================================================
# Раздел 2. run: проброс исключений из worker'а
# =====================================================================


def test_run_propagates_value_error() -> None:
    """``ValueError`` в worker'е пробрасывается родителю."""

    async def _body(runner: ProcessTaskRunner) -> None:
        with pytest.raises(ValueError, match="test message"):
            await runner.run(
                _raise_value_error,
                "test message",
                timeout=_TEST_TIMEOUT,
            )

    _run(_with_runner(_body))


def test_run_propagates_base_exception() -> None:
    """``KeyboardInterrupt`` (``BaseException``) пробрасывается родителю.

    Проверяет, что runner не «глотает» BaseException-исключения.
    Стандартный ``except Exception`` их не поймает — поэтому worker
    использует ``except BaseException`` для отправки результата.
    """

    async def _body(runner: ProcessTaskRunner) -> None:
        with pytest.raises(KeyboardInterrupt, match="simulated"):
            await runner.run(
                _raise_keyboard_interrupt,
                timeout=_TEST_TIMEOUT,
            )

    _run(_with_runner(_body))


# =====================================================================
# Раздел 3. run: pickle-сериализация
# =====================================================================


def test_run_with_lambda_raises_runtime_error() -> None:
    """Lambda как callable → ``RuntimeError`` (не picklable).

    Lambda определяется в локальном scope и не имеет pickle-пути.
    ``pickle.dumps`` падает раньше, чем создаётся процесс.
    """

    async def _body(runner: ProcessTaskRunner) -> None:
        with pytest.raises(RuntimeError, match="not picklable"):
            await runner.run(
                lambda x: x,  # noqa: E731
                1,
                timeout=_TEST_TIMEOUT,
            )

    _run(_with_runner(_body))


def test_run_with_unpicklable_arg_raises_runtime_error() -> None:
    """Несериализуемый аргумент → ``RuntimeError``.

    Открытый файл не pickle-сериализуем.
    """

    class Unpicklable:
        def __reduce__(self) -> Any:
            raise TypeError("deliberately unpicklable")

    async def _body(runner: ProcessTaskRunner) -> None:
        with pytest.raises(RuntimeError, match="not picklable"):
            await runner.run(
                _return_value,
                Unpicklable(),
                timeout=_TEST_TIMEOUT,
            )

    _run(_with_runner(_body))


# =====================================================================
# Раздел 4. run: worker упал без ответа
# =====================================================================


def test_run_worker_exit_without_result() -> None:
    """Worker завершился через ``os._exit`` → ``RuntimeError``.

    ``os._exit(7)`` завершает процесс немедленно, минуя ``finally``
    и отправку в очередь. Родитель получает ``queue.Empty`` и
    обнаруживает ``exitcode == 7``.
    """

    async def _body(runner: ProcessTaskRunner) -> None:
        with pytest.raises(RuntimeError, match="worker died without result"):
            await runner.run(
                _exit_without_result,
                7,
                timeout=_TEST_TIMEOUT,
            )

    _run(_with_runner(_body))


# =====================================================================
# Раздел 5. Ограничение параллелизма
# =====================================================================


def test_concurrency_limit_interactive(tmp_path: Path) -> None:
    """``interactive`` pool ограничивает параллелизм.

    Запускает 6 задач на пуле с ``max_concurrent=2``. Каждый worker
    пишет свой интервал в файл. После завершения вычисляется
    максимальное число одновременно работающих процессов — оно
    не должно превышать 2 (допуск +1 на диспетчеризацию).
    """
    marker = tmp_path / "marker.csv"
    marker.touch()

    async def _body() -> None:
        runner = ProcessTaskRunner(
            max_concurrent=2,
            batch_slots=1,
            shutdown_timeout=_TEST_SHUTDOWN_TIMEOUT,
        )
        try:
            await asyncio.gather(
                *[
                    runner.run(
                        _sleep_and_record,
                        str(marker),
                        0.3,
                        timeout=_TEST_TIMEOUT,
                    )
                    for _ in range(6)
                ]
            )
        finally:
            await runner.close()

    _run(_body())

    intervals = _read_intervals(marker)
    assert len(intervals) == 6
    overlap = _max_overlap(intervals)
    # Пул ограничен двумя; допускаем 3 для перекрытия на границе
    # (один завершается, второй стартует в тот же момент).
    assert overlap <= 3, f"Параллелизм превысил лимит: overlap={overlap}, pool_size=2, допуск 3"
    # Хотя бы два одновременно — иначе семафор не пропускает
    # параллельную работу вовсе.
    assert overlap >= 2, f"Параллелизм слишком мал: overlap={overlap}"


def test_batch_uses_separate_semaphore(tmp_path: Path) -> None:
    """``kind="batch"`` использует отдельный семафор.

    Пул ``batch_slots=1`` запускает 3 задачи последовательно.
    Максимальный overlap должен быть 1 (с допуском 1 на границу).
    """
    marker = tmp_path / "marker.csv"
    marker.touch()

    async def _body() -> None:
        runner = ProcessTaskRunner(
            max_concurrent=10,
            batch_slots=1,
            shutdown_timeout=_TEST_SHUTDOWN_TIMEOUT,
        )
        try:
            await asyncio.gather(
                *[
                    runner.run(
                        _sleep_and_record,
                        str(marker),
                        0.2,
                        timeout=_TEST_TIMEOUT,
                        kind="batch",
                    )
                    for _ in range(3)
                ]
            )
        finally:
            await runner.close()

    _run(_body())

    intervals = _read_intervals(marker)
    assert len(intervals) == 3
    overlap = _max_overlap(intervals)
    # batch_slots=1 → строго последовательно; допуск +1 на границу.
    assert overlap <= 2, f"batch-пул не изолирован: overlap={overlap}, batch_slots=1, допуск 2"


# =====================================================================
# Раздел 6. run после close
# =====================================================================


def test_run_after_close_raises() -> None:
    """``run`` после ``close`` → ``RuntimeError``.

    Проверяет защиту от использования закрытого runner'а.
    """

    async def _body() -> None:
        runner = ProcessTaskRunner(shutdown_timeout=_TEST_SHUTDOWN_TIMEOUT)
        await runner.close()

        with pytest.raises(RuntimeError, match="closed"):
            await runner.run(
                _return_value,
                1,
                timeout=_TEST_TIMEOUT,
            )

    _run(_body())


# =====================================================================
# Раздел 7. close: идемпотентность и пустой runner
# =====================================================================


def test_close_idempotent() -> None:
    """Повторный ``close`` — no-op.

    Первый вызов завершает runner; второй не должен падать и не
    должен делать ничего.
    """

    async def _body() -> None:
        runner = ProcessTaskRunner(shutdown_timeout=_TEST_SHUTDOWN_TIMEOUT)
        await runner.close()
        await runner.close()  # не должно упасть

    _run(_body())


def test_close_without_active_tasks() -> None:
    """``close`` без активных задач возвращается сразу.

    Проверяет, что нет лишних задержек (shutdown_timeout не
    применяется, если процессов нет).
    """

    async def _body() -> None:
        runner = ProcessTaskRunner(shutdown_timeout=_TEST_SHUTDOWN_TIMEOUT)
        start = time.monotonic()
        await runner.close()
        elapsed = time.monotonic() - start
        # Закрытие пустого runner'а должно быть мгновенным.
        assert elapsed < 0.5, f"close() пустого runner'а занял {elapsed:.3f}s"

    _run(_body())


# =====================================================================
# Раздел 8. Композиция: несколько задач на одном runner
# =====================================================================


def test_multiple_sequential_runs() -> None:
    """Последовательные ``run`` на одном runner работают.

    Проверяет, что runner переиспользуем и корректно освобождает
    ресурсы между вызовами (очередь закрывается, процесс удаляется
    из ``_processes``).
    """

    async def _body(runner: ProcessTaskRunner) -> None:
        for i in range(3):
            result = await runner.run(
                _return_value,
                i,
                timeout=_TEST_TIMEOUT,
            )
            assert result == i

    _run(_with_runner(_body))
