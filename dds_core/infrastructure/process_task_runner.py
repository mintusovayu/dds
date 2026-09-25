"""
ProcessTaskRunner — исполнитель subprocess-задач для DDS.

Компонент реализует стратегию **process-per-task**: каждая задача
выполняется в отдельном процессе, созданном через контекст
``forkserver``. Это устраняет два класса проблем, характерных для
``ProcessPoolExecutor``:

- **Переиспользование «испорченного» процесса.** Если один процесс
  упал с SIGSEGV (типичный сценарий при работе с повреждёнными PDF
  через PyMuPDF), весь пул ``ProcessPoolExecutor`` становится
  непригодным. В ``process-per-task`` падение одного процесса не
  влияет на другие.
- **Наследование состояния через ``fork``.** Классический ``fork``
  на Linux копирует состояние родителя: открытые файловые дескрипторы,
  захваченные блокировки, event loop. ``forkserver`` запускает
  дочерний процесс из «чистого» состояния — только stdlib и
  необходимые модули, без наследия от отца.

Архитектурные решения (обоснование — в ADR-004):

+-------------------------------+------------------------------------------+
| Решение                       | Обоснование                              |
+===============================+==========================================+
| ``forkserver`` на Linux       | Изоляция состояния. Избегает наследования|
|                               | fd, блокировок, event loop.              |
+-------------------------------+------------------------------------------+
| ``spawn`` на macOS / Windows  | ``forkserver`` недоступен или нестабилен |
|                               | вне Linux. ``spawn`` — портируемая       |
|                               | альтернатива (ценой более медленного     |
|                               | старта).                                 |
+-------------------------------+------------------------------------------+
| Process-per-task              | Падение процесса не влияет на другие.    |
|                               | Устойчивость к сегфолтам PyMuPDF.        |
+-------------------------------+------------------------------------------+
| ``asyncio.Semaphore``         | Ограничение количества одновременно     |
|                               | живых процессов.                         |
+-------------------------------+------------------------------------------+
| Раздельные семафоры для       | Резервирование слотов под batch-задачи,  |
| ``interactive`` и ``batch``   | чтобы interactive-нагрузка (рендер      |
|                               | страницы по запросу пользователя) не     |
|                               | голодала batch-очередь.                  |
+-------------------------------+------------------------------------------+
| Явный ``pickle.dumps`` до     | Ранняя диагностика несериализуемых       |
| ``process.start()``           | аргументов без затрат на fork.           |
|                               | Ошибка pickle-сериализации отдаётся      |
|                               | как понятное исключение, а не как        |
|                               | падение forkserver.                      |
+-------------------------------+------------------------------------------+
| ``mp.Queue`` для результата   | Стандартный IPC. Сериализуется через     |
|                               | спецпротокол multiprocessing (не pickle  |
|                               | в общем смысле).                         |
+-------------------------------+------------------------------------------+
| Polling ``process.is_alive()``| ``queue.get`` не видит смерть worker'а и |
| в ``_blocking_get``           | блокируется до полного таймаута.         |
|                               | Polling каждые ``_POLL_INTERVAL_SECONDS``|
|                               | позволяет обнаружить смерть немедленно   |
|                               | и поднять ``RuntimeError`` с ``exitcode``|
|                               | вместо ложного ``TimeoutError``.         |
+-------------------------------+------------------------------------------+
| ``process.start()`` в         | ``forkserver`` в Python 3.14 при старте  |
| изолированном контексте       | серверного процесса использует           |
| через ``contextvars.Context`` | внутренний ``asyncio.Runner``, который   |
|                               | проверяет наличие running loop через     |
|                               | ``ContextVar _RunningLoop``. При вызове  |
|                               | из контекста, где этот ContextVar        |
|                               | унаследован от активного event loop      |
|                               | (например, внутри pytest-asyncio или     |
|                               | ``asyncio.to_thread``, который           |
|                               | копирует контекст), старт падает с       |
|                               | ``RuntimeError: Runner.run() cannot be   |
|                               | called from a running event loop``.      |
|                               | Обёртка ``contextvars.Context().run(...)``|
|                               | запускает ``start()`` в пустом контексте |
|                               | без ``_RunningLoop`` — старт проходит    |
|                               | штатно.                                  |
+-------------------------------+------------------------------------------+
| Таймаут с терминацией         | При превышении: SIGTERM → пауза →        |
|                               | SIGKILL. Процесс не «утекает» в          |
|                               | бесконечное ожидание.                    |
+-------------------------------+------------------------------------------+
| ``daemon=True``               | Если родитель упадёт — дети умрут        |
|                               | автоматически.                           |
+-------------------------------+------------------------------------------+

Поток управления (успешный сценарий):

1. ``run(func, *args, timeout=...)`` — вызывающий код.
2. Захват ``asyncio.Semaphore`` (по ``kind``).
3. Проверка ``_closed`` — защита от использования после ``close()``.
4. ``pickle.dumps((func, args))`` — ранняя проверка сериализуемости.
5. Создание ``mp.Queue`` для результата.
6. Регистрация процесса в ``_processes`` (под ``threading.Lock``).
7. ``await asyncio.to_thread(_start_in_fresh_context, process)`` —
   fork через forkserver в изолированном контексте (обход
   ограничения Python 3.14).
8. Ожидание результата с таймаутом: ``loop.run_in_executor`` +
   ``_blocking_get`` (polling ``process.is_alive()``).
9. Распаковка результата: ``pickle.loads``.
10. Освобождение семафора и очереди.

Поток управления (таймаут):

1. ``asyncio.wait_for`` сработал по таймауту.
2. ``process.terminate()`` (SIGTERM).
3. ``process.join(timeout=1.0)``.
4. Если жив — ``process.kill()`` (SIGKILL) + ``join``.
5. Исключение ``TimeoutError`` поднимается наверх.

Поток управления (worker упал без ответа):

1. ``_blocking_get`` обнаруживает ``not process.is_alive()``.
2. Даёт очереди flush-шанс (ещё один ``q.get`` на
   ``_POLL_INTERVAL_SECONDS``).
3. Если пусто — возвращает ``None``.
4. ``_await_result`` возвращает ``None``.
5. ``run`` поднимает ``RuntimeError`` с ``exitcode``.

Поток управления (graceful close):

1. ``close(shutdown_timeout=...)``.
2. Установка ``_closed = True`` (защита от новых ``run``).
3. Все активные процессы: ``terminate()`` (SIGTERM).
4. Ожидание до ``shutdown_timeout``.
5. Оставшиеся: ``kill()`` (SIGKILL).
6. Финальный ``join``.

Принципы
--------

- **Не логирует.** Runner — низкоуровневый инфраструктурный компонент.
  Диагностика (таймаут, падение) — ответственность вызывающего кода
  через проброшенные исключения (``TimeoutError``, ``RuntimeError``).
- **Не знает о бизнес-логике.** Runner принимает любую picklable
  callable и аргументы; что именно она делает — не его дело.
- **Не зависит от других инфраструктурных модулей.** Единственная
  зависимость — ``dds_core.domain.config`` (значения по умолчанию).
- **Потокобезопасность.** Все обращения к ``_processes`` защищены
  ``threading.Lock``; семафоры — ``asyncio.Semaphore`` (привязаны
  к event loop, но их acquire/release не блокируют другие потоки).

Использование
-------------

.. code-block:: python

    runner = ProcessTaskRunner()
    try:
        result = await runner.run(
            extract_document_queries,
            abs_path, doc_id, rel_path, file_hash, size, mtime,
            timeout=config.OPERATION_TIMEOUTS["scan.extract"],
        )
    finally:
        await runner.close()

В ``lifespan.py``: runner создаётся один раз в startup, передаётся в
``ScanOrchestrator`` и закрывается в shutdown.

Ссылки
------

- ``docs/architecture-decisions/ADR-004-process-task-runner.md`` —
  обоснование процесса-per-task и forkserver, альтернативы
  (``ProcessPoolExecutor``, ``multiprocessing.Pool``, ``fork``).
- ``dds_core/subprocess_tasks/pdf_workers.py`` — пример worker'а.
- ``dds_web/lifespan.py`` — единственная точка создания и закрытия.
"""

from __future__ import annotations

import asyncio
import contextvars
import multiprocessing as mp
import pickle
import queue
import sys
import threading
import time
from collections.abc import Callable
from multiprocessing.process import BaseProcess
from typing import Any, Literal, TypeVar

from ..domain import config as core_config

# ======================================================================
# Константы
# ======================================================================

_POLL_INTERVAL_SECONDS: float = 0.1
"""Интервал polling'а в ``_blocking_get``.

Каждые 0.1 секунды проверяется ``process.is_alive()`` — это позволяет
обнаружить смерть worker'а раньше, чем истечёт основной таймаут.
Значение 0.1 — компромисс между отзывчивостью (100 мс — незаметная
для пользователя задержка) и минимальным оверхедом на проверку
(``is_alive`` — это waitpid-подобный вызов, ~мкс).
"""


# ======================================================================
# Worker-entry (модульная функция для pickle)
# ======================================================================

_T = TypeVar("_T")


def _worker_entry(payload: bytes, result_queue: mp.Queue) -> None:
    """Точка входа дочернего процесса.

    Распаковывает payload (сериализованные ``func`` и ``args``),
    выполняет вызов, отправляет результат обратно через ``result_queue``.

    Формат сообщения в очереди — кортеж ``(status, payload_bytes)``:

    - ``("ok", pickle.dumps(result))`` — успешное выполнение.
    - ``("error", pickle.dumps(exception))`` — исключение в worker'е.

    **Не является частью публичного API.** Определена на уровне модуля,
    чтобы быть picklable (вложенные функции pickle-сериализовать нельзя).
    Вызывается только через ``ProcessTaskRunner.run()``.

    Args:
        payload: Сериализованный кортеж ``(func, args)``.
        result_queue: Очередь multiprocessing для отправки результата
            в родительский процесс.
    """
    try:
        func, args = pickle.loads(payload)
        result = func(*args)
        try:
            result_bytes = pickle.dumps(result)
        except Exception as serialization_error:
            # Результат несериализуем — это ошибка worker'а.
            error = RuntimeError(
                f"Worker result is not picklable: "
                f"{type(serialization_error).__name__}: {serialization_error}"
            )
            try:
                result_queue.put(("error", pickle.dumps(error)))
            except Exception:
                # Не удалось передать даже ошибку — молча выходим.
                # Родитель обнаружит это по exitcode процесса.
                pass
            return
        result_queue.put(("ok", result_bytes))
    except BaseException as e:
        # Ловим BaseException (включая KeyboardInterrupt, SystemExit),
        # чтобы гарантированно отправить хоть что-то родителю.
        try:
            err_bytes = pickle.dumps(e)
        except Exception:
            # Исключение несериализуемо (например, кастомный класс без
            # __reduce__). Заменяем на RuntimeError с текстом.
            err_bytes = pickle.dumps(RuntimeError(f"{type(e).__name__}: {e}"))
        try:
            result_queue.put(("error", err_bytes))
        except Exception:
            pass


# ======================================================================
# ProcessTaskRunner
# ======================================================================


class ProcessTaskRunner:
    """Исполнитель subprocess-задач (process-per-task, forkserver).

    Создаёт отдельный процесс на каждый вызов ``run()``. Одновременно
    живых процессов — не более ``max_concurrent`` (interactive) или
    ``batch_slots`` (batch). Задачи сериализуются через ``pickle``,
    результат доставляется через ``multiprocessing.Queue``.

    Пример использования::

        runner = ProcessTaskRunner()
        try:
            result = await runner.run(
                my_worker,
                arg1, arg2,
                timeout=30.0,
            )
        finally:
            await runner.close()

    Attributes:

    +-----------------------------+------------------------------------------+
    | Атрибут                     | Описание                                 |
    +=============================+==========================================+
    | ``_max_concurrent``         | Лимит interactive-задач.                 |
    +-----------------------------+------------------------------------------+
    | ``_batch_slots``            | Лимит batch-задач.                       |
    +-----------------------------+------------------------------------------+
    | ``_shutdown_timeout``       | Таймаут graceful shutdown по умолчанию.  |
    +-----------------------------+------------------------------------------+
    | ``_ctx``                    | ``multiprocessing`` контекст             |
    |                             | (``forkserver`` / ``spawn``).            |
    +-----------------------------+------------------------------------------+
    | ``_interactive_semaphore``  | ``asyncio.Semaphore`` для                |
    |                             | interactive-задач.                       |
    +-----------------------------+------------------------------------------+
    | ``_batch_semaphore``        | ``asyncio.Semaphore`` для batch-задач.   |
    +-----------------------------+------------------------------------------+
    | ``_processes``              | Множество активных процессов             |
    |                             | (``multiprocessing.process.BaseProcess``).|
    +-----------------------------+------------------------------------------+
    | ``_processes_lock``         | ``threading.Lock`` для ``_processes``.   |
    +-----------------------------+------------------------------------------+
    | ``_closed``                 | Флаг закрытия.                           |
    +-----------------------------+------------------------------------------+
    """

    def __init__(
        self,
        max_concurrent: int | None = None,
        batch_slots: int | None = None,
        shutdown_timeout: float | None = None,
    ) -> None:
        """Инициализирует runner.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Определение лимитов (из ``config`` при ``None``).   |
        +---+-----------------------------------------------------+
        | 2 | Выбор ``multiprocessing`` контекста: ``forkserver`` |
        |   | на Linux, ``spawn`` иначе.                          |
        +---+-----------------------------------------------------+
        | 3 | Создание двух ``asyncio.Semaphore`` (interactive    |
        |   | и batch).                                            |
        +---+-----------------------------------------------------+
        | 4 | Инициализация пустого множества процессов и         |
        |   | ``threading.Lock``.                                  |
        +---+-----------------------------------------------------+
        | 5 | Установка ``_closed = False``.                      |
        +---+-----------------------------------------------------+

        Args:
            max_concurrent: Лимит одновременно работающих
                interactive-задач. Если ``None`` — значение из
                ``config.PROCESS_RUNNER_MAX_CONCURRENT``.
            batch_slots: Лимит одновременно работающих batch-задач.
                Если ``None`` — значение из
                ``config.PROCESS_RUNNER_BATCH_SLOTS``.
            shutdown_timeout: Таймаут graceful shutdown по умолчанию
                в секундах. Если ``None`` — значение из
                ``config.PROCESS_RUNNER_SHUTDOWN_TIMEOUT``.
        """
        self._max_concurrent: int = (
            max_concurrent
            if max_concurrent is not None
            else core_config.PROCESS_RUNNER_MAX_CONCURRENT
        )
        self._batch_slots: int = (
            batch_slots if batch_slots is not None else core_config.PROCESS_RUNNER_BATCH_SLOTS
        )
        self._shutdown_timeout: float = (
            shutdown_timeout
            if shutdown_timeout is not None
            else core_config.PROCESS_RUNNER_SHUTDOWN_TIMEOUT
        )

        # Выбор mp-контекста. На Linux forkserver — стандартный и
        # наиболее безопасный (изоляция состояния, см. ADR-004).
        # На macOS/Windows forkserver недоступен или нестабилен,
        # используется spawn.
        if sys.platform.startswith("linux"):
            self._ctx = mp.get_context("forkserver")
        else:
            self._ctx = mp.get_context("spawn")

        # Семафоры. interactive — основной пул, batch — отдельный,
        # чтобы batch-задачи не вытесняли interactive и наоборот.
        self._interactive_semaphore = asyncio.Semaphore(self._max_concurrent)
        self._batch_semaphore = asyncio.Semaphore(self._batch_slots)

        # ``BaseProcess`` — базовый класс для ``ForkServerProcess``,
        # ``SpawnProcess``, ``ForkProcess``. ``context.Process(...)``
        # возвращает один из этих подклассов; аннотация ``BaseProcess``
        # корректно сужает их общий интерфейс (start/join/terminate/
        # kill/is_alive/exitcode). Импорт — явный, из
        # ``multiprocessing.process``, потому что ``mp.process`` как
        # атрибут пакета не виден статическим анализаторам через
        # ``import multiprocessing as mp``.
        #
        # Активные процессы. Обращение под локом, потому что
        # ``run()`` может регистрировать процесс, а ``close()`` —
        # их обходить. В одном event loop гонка исключена, но при
        # вызове close() из другого потока (что допустимо) — нужна
        # защита.
        self._processes: set[BaseProcess] = set()
        self._processes_lock = threading.Lock()

        self._closed = False

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    async def run(
        self,
        func: Callable[..., _T],
        *args: Any,
        timeout: float,
        kind: Literal["interactive", "batch"] = "interactive",
    ) -> _T:
        """Выполняет функцию в отдельном процессе.

        Операции:

        +----+----------------------------------------------------+
        | №  | Описание                                           |
        +====+====================================================+
        | 1  | Проверка ``_closed``. Если закрыт —                 |
        |    | ``RuntimeError``.                                   |
        +----+----------------------------------------------------+
        | 2  | Выбор семафора по ``kind``.                         |
        +----+----------------------------------------------------+
        | 3  | Захват семафора (ожидание свободного слота).        |
        +----+----------------------------------------------------+
        | 4  | ``pickle.dumps((func, args))`` — проверка           |
        |    | сериализуемости до создания процесса.               |
        +----+----------------------------------------------------+
        | 5  | Повторная проверка ``_closed`` (race).              |
        +----+----------------------------------------------------+
        | 6  | Создание ``mp.Queue`` и ``mp.Process``              |
        |    | с ``target=_worker_entry``.                         |
        +----+----------------------------------------------------+
        | 7  | Регистрация процесса в ``_processes``.              |
        +----+----------------------------------------------------+
        | 8  | ``await asyncio.to_thread(_start_in_fresh_context,  |
        |    | process)`` — старт в отдельном потоке и в пустом    |
        |    | ``contextvars.Context`` (обход ограничения          |
        |    | forkserver + running event loop в Python 3.14).     |
        +----+----------------------------------------------------+
        | 9  | Ожидание результата:                                |
        |    | ``loop.run_in_executor`` + ``_blocking_get``.       |
        |    | ``_blocking_get`` каждые ``_POLL_INTERVAL_SECONDS`` |
        |    | проверяет ``process.is_alive()``.                   |
        +----+----------------------------------------------------+
        | 10 | При ``TimeoutError``:                               |
        |    | a. ``terminate()`` (SIGTERM).                       |
        |    | b. ``join(timeout=1.0)``.                           |
        |    | c. Если жив — ``kill()`` (SIGKILL) + ``join``.      |
        |    | d. Проброс ``TimeoutError``.                        |
        +----+----------------------------------------------------+
        | 11 | При ``outcome is None`` (worker упал без ответа):   |
        |    | ``RuntimeError`` с ``exitcode``.                    |
        +----+----------------------------------------------------+
        | 12 | Распаковка результата:                              |
        |    | ``("ok", data)`` → ``pickle.loads(data)``;          |
        |    | ``("error", data)`` → ``pickle.loads(data)``        |
        |    | и ``raise``.                                        |
        +----+----------------------------------------------------+
        | 13 | ``finally``: удаление из ``_processes``, закрытие   |
        |    | очереди, освобождение семафора.                     |
        +----+----------------------------------------------------+

        Args:
            func: Callable, выполняемая в дочернем процессе. Должна
                быть pickle-сериализуемой (модульная функция, не
                вложенная, не lambda).
            *args: Позиционные аргументы. Все должны быть
                pickle-сериализуемыми.
            timeout: Таймаут в секундах. Обязателен. Если ``<= 0``,
                таймаут отключён (использовать с осторожностью:
                зависший процесс заблокирует слот навсегда).
            kind: ``"interactive"`` (по умолчанию) — использует
                основной пул; ``"batch"`` — использует
                зарезервированные слоты.

        Returns:
            Результат ``func(*args)``.

        Raises:
            RuntimeError: Если runner закрыт; если аргументы не
                pickle-сериализуемы; если дочерний процесс завершился
                без ответа (например, SIGSEGV).
            TimeoutError: Если задача не завершилась за ``timeout``.
            BaseException: Любое исключение, поднятое ``func``.
        """
        if self._closed:
            raise RuntimeError("ProcessTaskRunner is closed")

        semaphore = self._batch_semaphore if kind == "batch" else self._interactive_semaphore

        async with semaphore:
            # Повторная проверка под семафором: между первым check и
            # acquire мог произойти close().
            if self._closed:
                raise RuntimeError("ProcessTaskRunner is closed")

            # Ранняя сериализация аргументов — до создания процесса.
            try:
                payload = pickle.dumps((func, args))
            except (pickle.PicklingError, TypeError, AttributeError) as e:
                raise RuntimeError(
                    f"ProcessTaskRunner: arguments not picklable ({type(e).__name__}: {e})"
                ) from e

            result_queue: mp.Queue = self._ctx.Queue()
            # Аннотация не указана: mypy выводит конкретный тип
            # (``ForkServerProcess`` на Linux, ``SpawnProcess`` иначе).
            # Он гарантированно совместим с ``set[BaseProcess]`` при
            # ``self._processes.add(process)`` ниже.
            process = self._ctx.Process(
                target=_worker_entry,
                args=(payload, result_queue),
                daemon=True,
            )

            with self._processes_lock:
                if self._closed:
                    raise RuntimeError("ProcessTaskRunner is closed")
                self._processes.add(process)

            try:
                # ``process.start()`` для forkserver-контекста требует
                # однократного запуска forkserver-сервера. В Python 3.14
                # этот запуск использует внутренний ``asyncio.Runner``,
                # который проверяет ``ContextVar _RunningLoop``.
                # ``asyncio.to_thread`` копирует текущий контекст (в том
                # числе ``_RunningLoop``), и старт падает. Обёртка
                # ``_start_in_fresh_context`` выполняет ``start()``
                # в пустом ``contextvars.Context`` без унаследованного
                # ``_RunningLoop`` — старт проходит штатно. Последующие
                # задачи переиспользуют уже запущенный forkserver-сервер.
                await asyncio.to_thread(_start_in_fresh_context, process)

                try:
                    outcome = await self._await_result(process, result_queue, timeout)
                except TimeoutError:
                    self._terminate_process(process, grace=1.0)
                    raise

                if outcome is None:
                    # Worker умер без отправки результата.
                    process.join(timeout=1.0)
                    raise RuntimeError(
                        f"ProcessTaskRunner: worker died without result "
                        f"(exitcode={process.exitcode})"
                    )

                status, data_bytes = outcome
                if status == "ok":
                    return pickle.loads(data_bytes)
                # status == "error"
                error = pickle.loads(data_bytes)
                if isinstance(error, BaseException):
                    raise error
                raise RuntimeError(
                    f"ProcessTaskRunner: worker returned non-exception error payload: {error!r}"
                )
            finally:
                with self._processes_lock:
                    self._processes.discard(process)
                # Закрыть очередь. join_thread() дожидается, что
                # feeder-поток отправил все данные. Ошибки подавляем:
                # после kill процесса очередь может быть уже закрыта.
                try:
                    result_queue.close()
                    result_queue.join_thread()
                except Exception:
                    pass
                # Если процесс ещё жив (например, из-за ошибки в
                # распаковке), не оставляем его висеть.
                if process.is_alive():
                    self._terminate_process(process, grace=1.0)

    async def close(self, shutdown_timeout: float | None = None) -> None:
        """Закрывает runner и завершает активные процессы.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Если уже закрыт — выход (идемпотентность).          |
        +---+-----------------------------------------------------+
        | 2 | Установка ``_closed = True`` (новые ``run``          |
        |   | получат ``RuntimeError``).                          |
        +---+-----------------------------------------------------+
        | 3 | Снимок активных процессов под локом.                |
        +---+-----------------------------------------------------+
        | 4 | ``terminate()`` (SIGTERM) всем живым.               |
        +---+-----------------------------------------------------+
        | 5 | Ожидание завершения до ``shutdown_timeout``          |
        |   | (через ``asyncio.to_thread``, чтобы не блокировать   |
        |   | event loop).                                        |
        +---+-----------------------------------------------------+
        | 6 | Оставшимся: ``kill()`` (SIGKILL).                   |
        +---+-----------------------------------------------------+
        | 7 | Финальный ``join`` для каждого.                     |
        +---+-----------------------------------------------------+

        Метод идемпотентен: повторный вызов — no-op.

        Args:
            shutdown_timeout: Таймаут graceful shutdown в секундах.
                Если ``None`` — значение из конструктора
                (по умолчанию ``config.PROCESS_RUNNER_SHUTDOWN_TIMEOUT``).
        """
        if self._closed:
            return
        self._closed = True

        effective_timeout = (
            shutdown_timeout if shutdown_timeout is not None else self._shutdown_timeout
        )

        with self._processes_lock:
            processes = list(self._processes)

        if not processes:
            return

        # Шаг 1: мягкая терминация — SIGTERM.
        for p in processes:
            if p.is_alive():
                p.terminate()

        # Шаг 2: ожидание до shutdown_timeout.
        deadline = time.monotonic() + effective_timeout
        for p in processes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if p.is_alive():
                await asyncio.to_thread(p.join, remaining)

        # Шаг 3: принудительное убийство оставшихся.
        for p in processes:
            if p.is_alive():
                p.kill()
                await asyncio.to_thread(p.join, 1.0)

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    async def _await_result(
        self,
        process: BaseProcess,
        result_queue: mp.Queue,
        timeout: float,
    ) -> tuple[str, bytes] | None:
        """Ожидает результат из очереди с таймаутом и контролем жизни worker'а.

        Использует ``loop.run_in_executor`` для блокирующего
        ``_blocking_get`` и ``asyncio.wait_for`` для ограничения
        времени. ``_blocking_get`` каждые ``_POLL_INTERVAL_SECONDS``
        проверяет ``process.is_alive()``: если worker умер без
        ответа, возвращает ``None`` немедленно — до истечения
        основного таймаута.

        Внутренний таймаут ``timeout + 1.0`` рассчитан на случай,
        когда worker жив, но завис: ``asyncio.wait_for`` сработает
        первым, а executor-поток освободится самостоятельно через
        секунду после asyncio-таймаута.

        Возвращает ``None``, если worker завершился без ответа
        (например, SIGSEGV, ``os._exit``).

        Args:
            process: Запущенный процесс.
            result_queue: Очередь multiprocessing.
            timeout: Таймаут в секундах.

        Returns:
            ``(status, payload_bytes)`` или ``None``.

        Raises:
            TimeoutError: Если результат не получен за ``timeout``.
        """
        loop = asyncio.get_running_loop()
        internal_timeout = timeout + 1.0 if timeout > 0 else None

        get_future = loop.run_in_executor(
            None,
            _blocking_get,
            result_queue,
            process,
            internal_timeout,
        )

        if timeout <= 0:
            # Таймаут отключён: ждём сколько угодно (не рекомендовано).
            return await get_future

        try:
            return await asyncio.wait_for(
                asyncio.shield(get_future),
                timeout=timeout,
            )
        except TimeoutError:
            # Не отменяем get_future — он сам завершится через
            # internal_timeout. Просто пробрасываем TimeoutError.
            raise

    def _terminate_process(self, process: BaseProcess, grace: float = 1.0) -> None:
        """Завершает процесс: SIGTERM → пауза → SIGKILL.

        Args:
            process: Процесс для завершения.
            grace: Таймаут ожидания после SIGTERM перед SIGKILL.
        """
        if not process.is_alive():
            # Процесс уже мёртв — просто дожидаемся освобождения
            # ресурсов (сбор зомби).
            process.join(timeout=0.1)
            return
        process.terminate()
        process.join(timeout=grace)
        if process.is_alive():
            process.kill()
            process.join(timeout=grace)


# ======================================================================
# Вспомогательные функции
# ======================================================================


def _start_in_fresh_context(process: BaseProcess) -> None:
    """Вызывает ``process.start()`` в пустом ``contextvars.Context``.

    **Зачем.** В Python 3.14 forkserver при первом старте использует
    внутренний ``asyncio.Runner``, который проверяет
    ``ContextVar _RunningLoop``. Если вызвать ``process.start()`` из
    контекста, где ``_RunningLoop`` унаследован от активного event
    loop (например, из pytest-asyncio или из ``asyncio.to_thread``,
    который копирует контекст), старт падает с
    ``RuntimeError: Runner.run() cannot be called from a running event
    loop``.

    **Что делает.** ``contextvars.Context()`` создаёт **пустой**
    контекст (не копию текущего). ``Context().run(process.start)``
    выполняет ``start()`` в этом пустом контексте: ``_RunningLoop``
    не виден, forkserver стартует штатно.

    **Побочные эффекты.** Контекстные переменные родителя внутри
    ``process.start()`` не видны. Forkserver не использует пользовательские
    ContextVars на этапе старта сервера, поэтому это безопасно.

    **Когда вызывать.** Из ``ProcessTaskRunner.run()`` через
    ``await asyncio.to_thread(_start_in_fresh_context, process)``:
    ``to_thread`` переносит вызов в отдельный поток (не блокирует
    event loop), а обёртка изолирует контекст.

    Args:
        process: Процесс, который нужно запустить.
    """
    contextvars.Context().run(process.start)


def _blocking_get(
    q: mp.Queue,
    process: BaseProcess,
    timeout: float | None,
) -> tuple[str, bytes] | None:
    """Блокирующее чтение из очереди с контролем жизни worker'а.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Цикл ``q.get(timeout=_POLL_INTERVAL_SECONDS)``.     |
    +---+-----------------------------------------------------+
    | 2 | При получении сообщения — возврат результата.       |
    +---+-----------------------------------------------------+
    | 3 | При ``queue.Empty``:                                |
    |   | a. Если ``process.is_alive()`` ложно — worker       |
    |   |    мёртв; дать очереди короткий flush-шанс         |
    |   |    (ещё один ``q.get`` с тем же интервалом);       |
    |   |    если и там пусто — возврат ``None``.             |
    |   | b. Если внутренний дедлайн истёк — возврат ``None``.|
    +---+-----------------------------------------------------+
    | 4 | При ``timeout is None`` — цикл без дедлайна         |
    |   | (проверяется только ``is_alive``).                  |
    +---+-----------------------------------------------------+

    **Почему не просто ``q.get(timeout=timeout)``.** ``queue.get``
    ничего не знает о процессе: если worker умер, не отправив
    результат, ``get`` продолжит ждать до полного таймаута. Это
    приводило к ошибке диагностики: вызывающий код получал
    ``TimeoutError`` через ``timeout`` секунд вместо быстрого
    ``RuntimeError`` с ``exitcode``.

    **Flush-шанс после смерти.** Между ``q.get`` и ``is_alive``
    есть окно: worker мог успеть положить результат в очередь и
    умереть до того, как родитель проверил ``is_alive``. Второй
    ``q.get`` в ветке «мёртв» извлекает такой пограничный результат.

    Выполняется в executor-потоке (см. ``_await_result``).

    Args:
        q: Очередь multiprocessing.
        process: Процесс worker'а. Проверяется ``is_alive()``.
        timeout: Внутренний таймаут в секундах (обычно
            ``caller_timeout + 1``). ``None`` — без ограничения.

    Returns:
        Кортеж ``(status, payload_bytes)`` или ``None`` при смерти
        worker'а / внутреннем таймауте.
    """
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        try:
            return q.get(timeout=_POLL_INTERVAL_SECONDS)
        except queue.Empty:
            if not process.is_alive():
                # Worker мёртв. Даём очереди короткий flush-шанс:
                # feeder-поток мог успеть записать последнее
                # сообщение в pipe незадолго до смерти.
                try:
                    return q.get(timeout=_POLL_INTERVAL_SECONDS)
                except queue.Empty:
                    return None
            if deadline is not None and time.monotonic() >= deadline:
                return None
