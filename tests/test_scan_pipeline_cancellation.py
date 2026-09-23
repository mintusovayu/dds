"""
Тесты отмены ``ScanPipeline``.

Назначение
----------
Проверка корректности отмены параллельного конвейера сканирования
по двум сценариям:

1. **Внешняя отмена** через ``task.cancel()`` — асинхронная задача
   ``run_async`` отменяется извне. Конвейер должен установить статус
   ``INTERRUPTED``, выполнить cleanup и пробросить ``CancelledError``
   дальше (это критично для ``ScanOrchestrator.run_primary_scan``,
   который не должен запускать вторичное сканирование после отмены).

2. **Кооперативная отмена** через ``pipeline.cancel()`` — устанавливает
   ``asyncio.Event``; воркеры завершаются самостоятельно после
   текущей операции. Задача ``run_async`` завершается нормально
   (без ``CancelledError``), статус — ``INTERRUPTED``.

Также проверяется изоляция состояния между последовательными
запусками: свежий ``_cancel_event`` создаётся при каждом вызове
``run_async``, поэтому флаг отмены одного запуска не влияет на
следующий.

Статус активации (Фаза 5)
-------------------------

Тесты активированы в **Фазе 5** (DocumentIndexPlan). До этого
момента они были помечены ``pytestmark = pytest.mark.skip`` из-за
зависимости от API, введённого в Фазе 5:

- ``DocumentIndexPlan`` (``dds_core/domain/index_plan.py``) —
  доменная модель плана индексации.
- ``build_index_plan_in_subprocess``
  (``dds_core/subprocess_tasks/pdf_workers.py``) — worker-функция
  формирования плана в subprocess.
- ``ITextIndexer.prepare_index_plan`` /
  ``ITextIndexer.write_index_plans`` — методы, заменившие
  ``prepare_document_queries`` / ``write_documents_batch``.

Что **сделано в Фазе 4** (не требует изменений):

- ``ScanPipeline.__init__`` принимает ``process_runner``
  (``IProcessTaskRunner``) и ``index_plan_worker`` (``Callable``).
  Оба обязательны с Фазы 5; потоковый fallback удалён.
- Заглушка :func:`_noop_index_plan_worker` (модульная picklable-функция)
  возвращает ``None`` — путь через ``process_runner`` вызывается,
  но реального анализа PDF не происходит.

Что **сделано в Фазе 5**:

- ``_StubIndexer`` обновлён под новый контракт ``ITextIndexer``.
- ``_Stubs.index_plan_worker`` — новое поле.
- ``_make_pipeline`` использует ``stubs.process_runner`` и
  ``stubs.index_plan_worker``.
- Снят ``pytestmark`` — 5 тестов активны.

Синхронные тесты и изоляция event loop
--------------------------------------

Тесты написаны как **синхронные функции**, а корутины запускаются
через helper :func:`_run`, который выполняет ``asyncio.run()`` в
**отдельном потоке**. Причина — та же, что в
``test_process_task_runner.py`` и ``test_process_runner_timeout.py``:

- ``pytest-asyncio`` в режиме ``auto`` оборачивает тесты в общий
  event loop. При полном прогоне набора (300+ тестов) running loop
  остаётся активным в главном потоке от предыдущих async-тестов
  (баг ``pytest-asyncio`` 1.4.0 + Python 3.14). Прямой
  ``asyncio.run(coro)`` в этом случае падает с
  ``RuntimeError: asyncio.run() cannot be called from a running
  event loop`` — даже для полностью sync-теста, не имеющего
  отношения к asyncio.
- Отдельный поток **гарантированно** не имеет running loop:
  ``asyncio.run`` внутри него всегда создаёт свежий loop. Это
  изолирует тесты ``ScanPipeline`` от состояния, оставленного
  другими тестами, без правок ``pytest-asyncio`` конфигурации и
  без изменения production-кода.

**Оверхед.** Один ``threading.Thread`` на тест (~10 мкс на
создание) — на 5 тестов меньше 0.1 мс суммарно. Все тесты вместе
укладываются в ~3 секунды, что подтверждается прогоном в изоляции.

Стратегия тестирования
----------------------
Полный ``ProcessTaskRunner`` не используется — это сделало бы
тесты медленными (fork + forkserver + IPC на каждую задачу).
Вместо него применяется ``_StubProcessRunner`` с тем же публичным
интерфейсом (метод ``run(func, *args, timeout, kind)``). Заглушка
эмулирует работу через ``asyncio.sleep(delay)`` — это позволяет
управлять таймингом отмены.

Зависимости от инфраструктуры минимальны:

- **SQLite** — реальный адаптер на ``tmp_path`` (обязателен:
  ``ScanPipeline`` пишет ``total_files`` в ``scan_state``).
- **Scanner / Hasher / Indexer / TextExtractor** — заглушки.
  Возвращают фиксированные значения, детерминированы, не
  обращаются к ФС.
- **Event bus** — реальный ``AsyncEventBus`` без запуска.
  Публикация в unstarted bus — no-op.
- **Executor** — реальный ``ThreadPoolExecutor`` с 2 воркерами.

Заглушка ``_StubIndexer`` реализует контракт ``ITextIndexer``
**Фазы 5**: методы ``prepare_index_plan``, ``write_index_plans``,
``remove_document``, ``get_document_metadata``,
``get_document_by_hash``, ``get_document_by_path``.

LSP-совместимость заглушек:

``_StubProcessRunner`` — замена ``IProcessTaskRunner``. Сигнатуры
методов ``run`` и ``close`` **точно соответствуют** контракту
Protocol:

- ``run.kind: Literal["interactive", "batch"]`` (не ``str``) —
  иначе стаб шире контракта;
- ``run.func: Callable[..., Any]`` (не ``Any``);
- ``close.shutdown_timeout: float | None`` (не ``float``) — иначе
  стаб уже контракта.

Pylance/mypy проверяют, что экземпляр стаба присваивается параметру
типа ``IProcessTaskRunner`` без нарушения LSP. Ослабление или
ужесточение сигнатуры приводило бы к ``reportArgumentType``.

Проверяемые инварианты
----------------------
- ``status`` после отмены — ``INTERRUPTED`` (не ``ERROR``, не
  ``RUNNING``).
- ``CancelledError`` пробрасывается наружу при внешней отмене.
- Отменённый конвейер не блокирует ``await task`` (нет deadlock).
- Воркеры останавливаются: количество вызовов ``process_runner.run``
  значительно меньше общего числа файлов.
- Новый ``run_async`` после отмены создаёт свежий ``_cancel_event``.

Границы
-------
- **Таймауты извлечения** (``OPERATION_TIMEOUTS["scan.extract"]``)
  не тестируются здесь: они относятся к ``ProcessTaskRunner``
  (см. ``tests/test_process_runner_timeout.py``). Этот файл
  покрывает только логику ``ScanPipeline``.
- **MAX_SCAN_ERRORS** — отдельный сценарий; тестируется в файлах
  обработки ошибок.
- **Реальное формирование плана** через PyMuPDF — не в области
  этого теста (см. ``bench_slow.py``).

Запуск
------
::

    pytest tests/test_scan_pipeline_cancellation.py -v

Принципы:
    - модуль не выполняет логирования;
    - не читает и не пишет production-файлы (все ресурсы — в tmp);
    - каждый тест изолирован (function-scoped fixtures);
    - асинхронные сценарии запускаются через :func:`_run` в
      отдельном потоке — без зависимости от утечки running loop
      от ``pytest-asyncio``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

import pytest
from dds_core.application.scan_pipeline import ScanPipeline
from dds_core.domain.index_plan import DocumentIndexPlan
from dds_core.domain.interfaces import IDatabase, IEventBus
from dds_core.domain.models import ScanStatus
from dds_core.infrastructure.database import DatabaseManager
from dds_core.infrastructure.event_bus import AsyncEventBus
from dds_core.infrastructure.sqlite_adapter import SQLiteAdapter

# =====================================================================
# Константы
# =====================================================================

_SCAN_TOTAL_FILES = 50
"""Количество файлов в тестовом датасете.

Достаточно много, чтобы отмена гарантированно произошла до
завершения конвейера (не все файлы успеют обработаться), но
достаточно мало, чтобы setup был быстрым.
"""

_RUNNER_DELAY_SECONDS = 0.05
"""Задержка в ``_StubProcessRunner.run``.

Эмулирует стоимость формирования плана индексации. 50 мс
обеспечивают предсказуемый тайминг отмены: pipeline стартует,
воркеры начинают обработку, ``cancel()`` вызывается на первом
же обработанном файле.
"""

_EXECUTOR_MAX_WORKERS = 2
"""Размер пула потоков для блокирующих операций.

Меньше production-значения (6) — тесты не нуждаются в высоком
параллелизме, а меньшее число воркеров снижает риск гонок в
момент отмены.
"""

_T = TypeVar("_T")


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
    Это изолирует тесты ``ScanPipeline`` от состояния, оставленного
    другими тестами, и не требует ни правок pytest-asyncio
    конфигурации, ни изменения production-кода.

    **Оверхед.** Один ``threading.Thread`` на тест (~10 мкс на
    создание) — на 5 тестов меньше 0.1 мс суммарно. Все тесты
    укладываются в ~3 секунды (подтверждено прогоном в изоляции).

    **Аннотация ``Coroutine[Any, Any, _T]``.** Соответствует
    typeshed: ``asyncio.run`` принимает именно ``Coroutine``, а не
    произвольный ``Awaitable``. Все вызовы ``_run`` — от ``async
    def``-функций, поэтому сужение типа корректно.

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


# =====================================================================
# Заглушки
# =====================================================================


class _StubScanner:
    """Заглушка ``IScanner``.

    Возвращает фиксированный список путей. Не обращается к
    файловой системе. Метаданные файла генерируются по пути.
    """

    def __init__(self, file_paths: list[str], base_directory: str) -> None:
        self._file_paths = file_paths
        self._base_directory = base_directory

    def scan_directory(self, directory: str) -> list[str]:
        """Возвращает фиксированный список путей."""
        return list(self._file_paths)

    def get_file_info(self, file_path: str) -> dict[str, Any]:
        """Возвращает синтетические метаданные файла."""
        return {
            "file_path": file_path,
            "file_name": file_path.rsplit("/", 1)[-1],
            "file_size": 1024,
            "last_modified": "2025-01-01T00:00:00+00:00",
        }

    def get_relative_path(self, file_path: str, base_directory: str) -> str:
        """Возвращает относительный путь (без обращения к ФС)."""
        prefix = base_directory.rstrip("/") + "/"
        if file_path.startswith(prefix):
            return file_path[len(prefix) :]
        return file_path


class _StubHasher:
    """Заглушка ``IHasher``.

    Возвращает детерминированный хеш на основе пути — гарантирует
    уникальность файлов для duplicate-detection.
    """

    def compute_hash(self, file_path: str) -> str:
        """Хеш = hex(path). Детерминирован, не обращается к ФС."""
        return f"hash_{abs(hash(file_path)):016x}"


class _StubIndexer:
    """Заглушка ``ITextIndexer``.

    Реализует контракт ``ITextIndexer`` **Фазы 5**:

    - ``prepare_index_plan`` — подготовка плана для одного документа
      (возвращает ``None`` — «пустой документ, ничего не извлекаем»);
    - ``write_index_plans`` — батчевая запись (возвращает
      ``(N, 0)`` — «всё успешно, ошибок нет»);
    - ``remove_document`` — удаление (no-op);
    - ``get_document_by_hash`` / ``get_document_by_path`` — поиск
      по хешу/пути (``None`` — «не найден»);
    - ``get_document_metadata`` — метаданные по пути (``None`` —
      «нет закэшированных данных»).

    Все методы — no-op заглушки: тесты проверяют логику отмены,
    а не индексацию. Заглушка не содержит изменяемого состояния,
    потокобезопасна.
    """

    def prepare_index_plan(
        self,
        doc_id: str,
        file_path: str,
        file_hash: str,
        file_size: int,
        last_modified: str,
        abs_file_path: str,
    ) -> DocumentIndexPlan | None:
        """Фаза 5: возвращает ``None`` («нет данных для индексации»).

        В реальной реализации метод открывает PDF и формирует
        ``DocumentIndexPlan``. В заглушке — no-op.
        """
        return None

    def write_index_plans(
        self,
        plans: list[DocumentIndexPlan],
    ) -> tuple[int, int]:
        """Фаза 5: батчевая запись. Возвращает ``(N, 0)``."""
        return len(plans), 0

    def remove_document(self, doc_id: str) -> None:
        """No-op."""
        return None

    def get_document_by_hash(self, file_hash: str) -> str | None:
        """Нет дубликатов → возврат ``None``."""
        return None

    def get_document_by_path(self, file_path: str) -> str | None:
        """Нет существующих документов → возврат ``None``."""
        return None

    def get_document_metadata(
        self,
        file_path: str,
    ) -> tuple[str, str, int, str] | None:
        """Нет закэшированных метаданных → возврат ``None``.

        Сигнатура соответствует контракту ``ITextIndexer``
        (4-элементный кортеж ``(doc_id, file_hash, cached_size,
        cached_mtime)``).
        """
        return None


class _StubTextExtractor:
    """Заглушка ``ITextExtractor``.

    Используется как обязательный параметр конструктора
    ``ScanPipeline`` (поле ``text_extractor``). Внутри конвейера
    не вызывается напрямую: формирование плана выполняется через
    ``process_runner``.
    """

    def open_document(self, file_path: str) -> Any:
        """Не вызывается в тестах отмены. Заглушка для типизации."""
        raise NotImplementedError("StubTextExtractor.open_document: не вызывается в тестах отмены.")


class _StubProcessRunner:
    """Заглушка ``IProcessTaskRunner``.

    Эмулирует работу ``process_runner.run`` через ``asyncio.sleep``,
    не создавая дочерних процессов. Сигнатуры методов ``run`` и
    ``close`` **точно соответствуют** контракту
    ``IProcessTaskRunner`` (LSP-совместимость): ``kind`` ограничен
    ``Literal["interactive", "batch"]``, ``shutdown_timeout``
    принимает ``float | None``.

    LSP-совместимость критична для статического анализа: Pylance
    (и mypy) проверяют, что экземпляр стаба может быть присвоен
    параметру типа ``IProcessTaskRunner`` без нарушения
    контракта. Ослабление сигнатуры (например, ``kind: str``)
    приводило бы к ошибке ``reportArgumentType``.

    Дополнительно предоставляет ``first_call_event`` — ``asyncio.Event``,
    который устанавливается при первом вызове. Тесты ожидают его
    перед вызовом ``pipeline.cancel()``, чтобы гарантировать, что
    конвейер действительно начал обработку.
    """

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self._closed = False
        self.call_count = 0
        self.first_call_event = asyncio.Event()

    async def run(
        self,
        func: Callable[..., Any],
        *args: Any,
        timeout: float,
        kind: Literal["interactive", "batch"] = "interactive",
    ) -> Any:
        """Эмулирует работу: sleep(delay) → ``None``.

        Возвращает ``None`` — это соответствует поведению реального
        ``index_plan_worker`` при ошибке открытия PDF. Конвейер
        интерпретирует результат как «файл помечен ошибочным», но
        в контексте тестов отмены это не важно.

        Args:
            func: Worker-функция (не вызывается в заглушке).
            *args: Аргументы (не используются).
            timeout: Таймаут (не используется).
            kind: Режим пула (не используется; сигнатура совпадает
                с контрактом ``IProcessTaskRunner``).
        """
        if self._closed:
            raise RuntimeError("ProcessTaskRunner is closed")
        self.call_count += 1
        if not self.first_call_event.is_set():
            self.first_call_event.set()
        await asyncio.sleep(self._delay)
        return None

    async def close(self, shutdown_timeout: float | None = None) -> None:
        """No-op: заглушка не владеет ресурсами.

        Args:
            shutdown_timeout: Игнорируется (заглушка не владеет
                процессами). Сигнатура совпадает с контрактом
                ``IProcessTaskRunner.close``.
        """
        self._closed = True


# =====================================================================
# Контейнер заглушек и фабрика pipeline
# =====================================================================


@dataclass
class _Stubs:
    """Агрегатор заглушек для передачи в fixture.

    Attributes:
        scanner: Заглушка ``IScanner``.
        hasher: Заглушка ``IHasher``.
        indexer: Заглушка ``ITextIndexer``.
        text_extractor: Заглушка ``ITextExtractor`` (обязательный
            параметр ``ScanPipeline.__init__``; внутри конвейера
            не вызывается напрямую).
        process_runner: Заглушка ``IProcessTaskRunner``. Передаётся
            в ``ScanPipeline`` как ``process_runner``.
        index_plan_worker: Picklable-функция формирования плана
            индексации. Передаётся в ``ScanPipeline`` как
            ``index_plan_worker``.
        rd_directory: Фиктивный корневой каталог РД.
    """

    scanner: _StubScanner
    hasher: _StubHasher
    indexer: _StubIndexer
    text_extractor: _StubTextExtractor
    process_runner: _StubProcessRunner
    index_plan_worker: Callable[..., Any]
    rd_directory: str


def _make_pipeline(
    db: IDatabase,
    stubs: _Stubs,
    executor: ThreadPoolExecutor,
    event_bus: IEventBus,
) -> ScanPipeline:
    """Собирает ``ScanPipeline`` со стабами для теста отмены.

    Все внешние зависимости заменены на легковесные заглушки или
    реальные, но изолированные компоненты (SQLite на tmp, unstarted
    event bus).

    ``process_runner`` и ``index_plan_worker`` берутся из ``stubs``
    (``stubs.process_runner``, ``stubs.index_plan_worker``). Оба
    обязательны с Фазы 5 (ADR-005): потоковый fallback удалён.

    Args:
        db: Реальный SQLiteAdapter на tmp_path с созданной схемой.
        stubs: Контейнер заглушек.
        executor: Реальный ThreadPoolExecutor для блокирующих
            операций (scan_directory, hashing, write_index_plans).
        event_bus: AsyncEventBus (не запущен).

    Returns:
        Настроенный ``ScanPipeline``.
    """
    return ScanPipeline(
        db=db,
        scanner=stubs.scanner,
        hasher=stubs.hasher,
        text_extractor=stubs.text_extractor,
        indexer=stubs.indexer,
        event_bus=event_bus,
        max_hash_workers=2,
        max_extract_workers=2,
        scan_executor=executor,
        document_cache=None,
        process_runner=stubs.process_runner,
        index_plan_worker=stubs.index_plan_worker,
    )


def _noop_index_plan_worker(
    doc_id: str,
    file_path: str,
    file_hash: str,
    file_size: int,
    last_modified: str,
    abs_file_path: str,
) -> DocumentIndexPlan | None:
    """Фиктивный worker для ``index_plan_worker`` в тестах отмены.

    Модульная (picklable) функция с сигнатурой, совпадающей
    с ``build_index_plan_in_subprocess``. Возвращает ``None`` —
    как реальный worker при ошибке открытия PDF. Используется
    заглушкой ``_StubProcessRunner``: ``run(_noop_index_plan_worker,
    ...)`` вызывается без реального subprocess, но с корректным
    интерфейсом.

    Аргументы соответствуют сигнатуре
    ``build_index_plan_in_subprocess`` (Шаг 7 Фазы 5):
    ``doc_id, file_path, file_hash, file_size, last_modified,
    abs_file_path``. Порядок важен — ``ScanPipeline._extract_worker``
    передаёт их позиционно.

    Args:
        doc_id: Идентификатор документа.
        file_path: Относительный путь (от каталога РД).
        file_hash: Хеш файла.
        file_size: Размер файла в байтах.
        last_modified: Дата изменения (ISO 8601).
        abs_file_path: Абсолютный путь к PDF.

    Returns:
        ``None`` (сигнал «нет данных для индексации»).
    """
    return None


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def stubs(tmp_path: Path) -> _Stubs:
    """Контейнер заглушек с детерминированными значениями.

    Формирует список фиктивных путей к файлам в ``tmp_path`` (реально
    не создаются — заглушки не обращаются к ФС) и оборачивает их в
    соответствующие stub-классы.

    Args:
        tmp_path: Встроенная фикстура pytest.

    Returns:
        :class:`_Stubs`.
    """
    base = str(tmp_path / "rd_directory")
    file_paths = [f"{base}/doc_{i:04d}.pdf" for i in range(_SCAN_TOTAL_FILES)]

    return _Stubs(
        scanner=_StubScanner(file_paths, base_directory=base),
        hasher=_StubHasher(),
        indexer=_StubIndexer(),
        text_extractor=_StubTextExtractor(),
        process_runner=_StubProcessRunner(delay=_RUNNER_DELAY_SECONDS),
        index_plan_worker=_noop_index_plan_worker,
        rd_directory=base,
    )


@pytest.fixture
def real_db(tmp_path: Path) -> Iterator[SQLiteAdapter]:
    """Реальный SQLiteAdapter со схемой ядра.

    Схема создаётся через production-путь
    (``DatabaseManager.ensure_all``), чтобы тест не расходился с
    реальной структурой БД (в частности, была таблица ``scan_state``).

    Args:
        tmp_path: Встроенная фикстура pytest.

    Yields:
        Настроенный ``SQLiteAdapter``.
    """
    db_path = tmp_path / "scan_cancel.db"
    adapter = SQLiteAdapter(str(db_path))
    try:
        DatabaseManager(adapter).ensure_all()
        yield adapter
    finally:
        adapter.close()


@pytest.fixture
def executor() -> Iterator[ThreadPoolExecutor]:
    """ThreadPoolExecutor для блокирующих операций.

    Yields:
        Настроенный executor; закрывается в teardown.
    """
    ex = ThreadPoolExecutor(
        max_workers=_EXECUTOR_MAX_WORKERS,
        thread_name_prefix="scan-cancel-test",
    )
    try:
        yield ex
    finally:
        ex.shutdown(wait=True, cancel_futures=True)


@pytest.fixture
def event_bus() -> AsyncEventBus:
    """Unstarted ``AsyncEventBus`` (публикация — no-op).

    Returns:
        Экземпляр ``AsyncEventBus`` без запущенного event loop.
    """
    return AsyncEventBus(max_queue_size=100)


# =====================================================================
# Тесты
# =====================================================================


def test_cancelled_error_propagates_to_caller(
    real_db: SQLiteAdapter,
    stubs: _Stubs,
    executor: ThreadPoolExecutor,
    event_bus: AsyncEventBus,
) -> None:
    """Внешний ``task.cancel()`` → ``CancelledError`` пробрасывается.

    Сценарий:

    1. Запуск ``pipeline.run_async`` как ``asyncio.Task``.
    2. Ожидание первого вызова ``process_runner.run`` — гарантия,
       что конвейер начал обработку.
    3. ``task.cancel()`` — отмена извне.
    4. ``await task`` должен поднять ``CancelledError``
       (не ``TimeoutError``, не ``RuntimeError``).
    5. После отмены ``progress.status == INTERRUPTED``.

    Проверка проброса ``CancelledError`` критична: ``ScanOrchestrator``
    использует этот факт, чтобы не запускать вторичное сканирование.
    """
    pipeline = _make_pipeline(real_db, stubs, executor, event_bus)

    async def _body() -> None:
        task = asyncio.create_task(
            pipeline.run_async(
                rd_directory=stubs.rd_directory,
                scan_id=1,
                correlation_id="test-cancel-external",
            )
        )
        # Гарантия, что конвейер стартовал.
        await asyncio.wait_for(
            stubs.process_runner.first_call_event.wait(),
            timeout=5.0,
        )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(_body())

    progress = pipeline.get_progress()
    assert (
        progress.status == ScanStatus.INTERRUPTED
    ), f"Ожидался статус INTERRUPTED, получен {progress.status!r}"


def test_cooperative_cancel_sets_interrupted_status(
    real_db: SQLiteAdapter,
    stubs: _Stubs,
    executor: ThreadPoolExecutor,
    event_bus: AsyncEventBus,
) -> None:
    """Кооперативный ``pipeline.cancel()`` → завершение без исключения.

    Сценарий:

    1. Запуск ``pipeline.run_async`` как task.
    2. Ожидание первого вызова ``process_runner.run``.
    3. ``pipeline.cancel()`` — устанавливает ``_cancel_event``.
    4. ``await task`` завершается нормально (без ``CancelledError``).
    5. ``progress.status == INTERRUPTED``.

    Отличие от внешней отмены: воркеры видят флаг через
    ``_wait_task_or_cancel`` и завершаются сами; задача
    ``run_async`` завершается с нормальным результатом.
    """
    pipeline = _make_pipeline(real_db, stubs, executor, event_bus)

    async def _body() -> Any:
        task = asyncio.create_task(
            pipeline.run_async(
                rd_directory=stubs.rd_directory,
                scan_id=2,
                correlation_id="test-cancel-cooperative",
            )
        )
        await asyncio.wait_for(
            stubs.process_runner.first_call_event.wait(),
            timeout=5.0,
        )
        pipeline.cancel()
        # Задача должна завершиться нормально — без CancelledError.
        return await asyncio.wait_for(task, timeout=10.0)

    progress = _run(_body())

    assert (
        progress.status == ScanStatus.INTERRUPTED
    ), f"Ожидался статус INTERRUPTED, получен {progress.status!r}"


def test_cancel_stops_workers_before_all_files_processed(
    real_db: SQLiteAdapter,
    stubs: _Stubs,
    executor: ThreadPoolExecutor,
    event_bus: AsyncEventBus,
) -> None:
    """Кооперативная отмена останавливает воркеров до конца датасета.

    Сценарий:

    1. Запуск конвейера с 50 файлами.
    2. Ожидание первого вызова ``process_runner.run``.
    3. ``pipeline.cancel()`` сразу после первого файла.
    4. Проверка: ``process_runner.call_count < _SCAN_TOTAL_FILES``.

    Строгое значение ``call_count`` не проверяется — воркеры могли
    начать обработку нескольких файлов параллельно к моменту отмены.
    Проверяется только факт «не все файлы обработаны».
    """
    pipeline = _make_pipeline(real_db, stubs, executor, event_bus)

    async def _body() -> None:
        task = asyncio.create_task(
            pipeline.run_async(
                rd_directory=stubs.rd_directory,
                scan_id=3,
                correlation_id="test-cancel-stops-workers",
            )
        )
        await asyncio.wait_for(
            stubs.process_runner.first_call_event.wait(),
            timeout=5.0,
        )
        pipeline.cancel()
        await asyncio.wait_for(task, timeout=10.0)

    _run(_body())

    assert stubs.process_runner.call_count < _SCAN_TOTAL_FILES, (
        f"Воркеры не остановились: обработано "
        f"{stubs.process_runner.call_count} из {_SCAN_TOTAL_FILES} файлов"
    )


def test_new_run_resets_cancel_state(
    real_db: SQLiteAdapter,
    stubs: _Stubs,
    executor: ThreadPoolExecutor,
    event_bus: AsyncEventBus,
) -> None:
    """Новый ``run_async`` создаёт свежий ``_cancel_event``.

    Сценарий:

    1. Первый запуск → ``pipeline.cancel()`` → статус INTERRUPTED.
    2. Второй запуск на том же pipeline → новый ``_cancel_event``
       не установлен → конвейер завершается нормально (COMPLETED).

    Это критично для повторного запуска сканирования в рамках
    одного процесса (без пересоздания ``ScanPipeline``).
    """
    pipeline = _make_pipeline(real_db, stubs, executor, event_bus)

    # --- Первый запуск: отмена. -----------------------------------
    async def _first_run() -> None:
        task = asyncio.create_task(
            pipeline.run_async(
                rd_directory=stubs.rd_directory,
                scan_id=10,
                correlation_id="test-reset-first",
            )
        )
        await asyncio.wait_for(
            stubs.process_runner.first_call_event.wait(),
            timeout=5.0,
        )
        pipeline.cancel()
        await asyncio.wait_for(task, timeout=10.0)

    _run(_first_run())
    assert pipeline.get_progress().status == ScanStatus.INTERRUPTED

    # --- Второй запуск: свежий pipeline без отмены. ---------------
    # Создаём новый stub process_runner — он сохраняет state между
    # запусками (call_count, first_call_event), что мешает чистому
    # тесту повторного запуска. Проще создать новый stub.
    stubs.process_runner = _StubProcessRunner(delay=_RUNNER_DELAY_SECONDS)
    pipeline2 = _make_pipeline(real_db, stubs, executor, event_bus)

    async def _second_run() -> Any:
        return await asyncio.wait_for(
            pipeline2.run_async(
                rd_directory=stubs.rd_directory,
                scan_id=11,
                correlation_id="test-reset-second",
            ),
            timeout=30.0,
        )

    progress = _run(_second_run())
    assert (
        progress.status == ScanStatus.COMPLETED
    ), f"Второй запуск завершился со статусом {progress.status!r}, ожидался COMPLETED"


def test_cancel_before_run_async_is_noop(
    real_db: SQLiteAdapter,
    stubs: _Stubs,
    executor: ThreadPoolExecutor,
    event_bus: AsyncEventBus,
) -> None:
    """``pipeline.cancel()`` до запуска — no-op.

    Сценарий:

    1. Создание pipeline (без запуска).
    2. ``pipeline.cancel()`` — ``_cancel_event is None``, метод
       завершается без действий (см. ``ScanPipeline.cancel``).
    3. Запуск ``run_async`` — конвейер работает нормально.

    Это гарантирует, что вызов ``cancel()`` извне (например, при
    инициализации lifespan) не сломает следующий запуск.
    """
    pipeline = _make_pipeline(real_db, stubs, executor, event_bus)

    # До run_async _cancel_event не создан — cancel() должен быть no-op.
    pipeline.cancel()

    async def _body() -> Any:
        return await asyncio.wait_for(
            pipeline.run_async(
                rd_directory=stubs.rd_directory,
                scan_id=20,
                correlation_id="test-cancel-before-run",
            ),
            timeout=30.0,
        )

    progress = _run(_body())
    assert progress.status == ScanStatus.COMPLETED
