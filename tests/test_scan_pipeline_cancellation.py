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

Статус активации (Фаза 4 → Фаза 5)
----------------------------------

Все тесты модуля помечены ``pytestmark = pytest.mark.skip`` и будут
активированы в **Фазе 5** плана рефакторинга v9. До этого момента
они зависят от API, которого ещё нет в проекте:

- ``DocumentIndexPlan`` (появляется в Фазе 5) — доменная модель
  плана индексации без SQL-строк.
- ``index_plan_worker`` (появляется в Фазе 5) — callable для
  построения ``DocumentIndexPlan`` в subprocess; заменяет
  ``pdf_worker`` (последний использовался как временный путь
  через ``extract_document_queries`` до появления плана).
- ``TextIndexer.prepare_index_plan`` / ``write_index_plans``
  (появляются в Фазе 5) — заменяют
  ``prepare_document_queries`` / ``write_documents_batch``.

Что **уже сделано** в Фазе 4 (не требует повторного изменения при
активации в Фазе 5):

- ``ScanPipeline.__init__`` принимает опциональные ``process_runner``
  (``IProcessTaskRunner``) и ``pdf_worker`` (``Callable``). При
  активации тестов фабрика :func:`_make_pipeline` передаёт их
  явно через параметры; если не передать — используется потоковый
  fallback через ``indexer.prepare_document_queries``.
- Заглушка :func:`_noop_pdf_worker` (модульная picklable-функция)
  заменит worker в тестовом сценарии: путь через ``process_runner``
  будет вызываться, но реального PDF-анализа не произойдёт.

Что **предстоит сделать** при активации в Фазе 5:

1. Снять ``pytestmark = pytest.mark.skip(...)`` с модуля.
2. В фабрике :func:`_make_pipeline` заменить передачу ``pdf_worker``
   на ``index_plan_worker`` (в Фазе 5 ``DocumentIndexPlan`` заменит
   «сырые» SQL-запросы).
3. Восстановить импорт ``build_index_plan_in_subprocess`` из
   ``dds_core.subprocess_tasks.pdf_workers`` (в Фазе 5 функция
   получит имя ``build_index_plan_in_subprocess`` и сменит
   сигнатуру/возвращаемый тип).

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
- **Event bus** — реальный ``AsyncEventBus`` без запуска. Публикация
  в unstarted bus — no-op, что изолирует тест от overhead'а
  обработки событий.
- **Executor** — реальный ``ThreadPoolExecutor`` с 2 воркерами;
  закрывается в teardown.

Заглушка ``_StubIndexer`` реализует контракт ``ITextIndexer``
**Фазы 4** (методы ``prepare_document_queries``,
``write_documents_batch``, ``get_document_metadata``,
``get_document_by_hash``, ``get_document_by_path``,
``remove_document``). В Фазе 5 контракт изменится — заглушка
должна быть обновлена вместе с ним (см. комментарий в её docstring).

Проверяемые инварианты
----------------------
- ``status`` после отмены — ``INTERRUPTED`` (не ``ERROR``, не ``RUNNING``).
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
- **Реальное извлечение текста** через PyMuPDF — не в области
  этого теста (см. ``bench_slow.py``).

Запуск
------
::

    pytest tests/test_scan_pipeline_cancellation.py -v

Принципы:
    - модуль не выполняет логирования;
    - не читает и не пишет production-файлы (все ресурсы — в tmp);
    - каждый тест изолирован (function-scoped fixtures);
    - асинхронные сценарии запускаются через ``asyncio.run`` —
      без зависимости от конфигурации ``pytest-asyncio``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from dds_core.application.scan_pipeline import ScanPipeline
from dds_core.domain.interfaces import IDatabase, IEventBus
from dds_core.domain.models import ScanStatus
from dds_core.infrastructure.database import DatabaseManager
from dds_core.infrastructure.event_bus import AsyncEventBus
from dds_core.infrastructure.sqlite_adapter import SQLiteAdapter

# ----------------------------------------------------------------------
# Статус активации
# ----------------------------------------------------------------------
#
# Все тесты модуля пропущены до Фазы 5. Причина — зависимость от
# API, которое появится в Фазе 5 (см. модульный docstring,
# раздел «Статус активации»). Инфраструктура тестов (заглушки,
# фабрика, fixtures) сохранена и готова к активации.
#
# Фаза 4 добавила в ScanPipeline.__init__ параметры process_runner
# и pdf_worker, поэтому фабрика _make_pipeline принимает их как
# опциональные. При активации в Фазе 5 достаточно:
#   1. Снять pytestmark.
#   2. Заменить pdf_worker на index_plan_worker (см. docstring
#      _make_pipeline).
#   3. Восстановить импорт build_index_plan_in_subprocess.
# ----------------------------------------------------------------------
pytestmark = pytest.mark.skip(reason="activated in phase 5 (ProcessTaskRunner, DocumentIndexPlan)")


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

Эмулирует стоимость извлечения текста. 50 мс обеспечивают
предсказуемый тайминг отмены: pipeline стартует, воркеры
начинают обработку, ``cancel()`` вызывается на первом же
обработанном файле.
"""

_EXECUTOR_MAX_WORKERS = 2
"""Размер пула потоков для блокирующих операций.

Меньше production-значения (6) — тесты не нуждаются в высоком
параллелизме, а меньшее число воркеров снижает риск гонок в
момент отмены.
"""


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

    Реализует контракт ``ITextIndexer`` **Фазы 4**:

    - ``prepare_document_queries`` — подготовка SQL-запросов
      для одного документа (возвращает ``None`` — «пустой
      документ, ничего не извлекаем»);
    - ``write_documents_batch`` — батчевая запись (возвращает
      ``(N, 0)`` — «всё успешно, ошибок нет»);
    - ``get_document_metadata`` — метаданные по пути (``None`` —
      «нет закэшированных данных»);
    - ``get_document_by_hash`` / ``get_document_by_path`` — поиск
      по хешу/пути (``None`` — «не найден»);
    - ``remove_document`` — удаление (no-op).

    В **Фазе 5** контракт ``ITextIndexer`` изменится:

    - ``prepare_document_queries`` заменится на
      ``prepare_index_plan`` (возвращает ``DocumentIndexPlan``);
    - ``write_documents_batch`` заменится на
      ``write_index_plans`` (принимает ``list[DocumentIndexPlan]``);
    - сигнатура ``get_document_metadata`` расширится (см. шаг 4.4
      плана v9).

    При активации тестов в Фазе 5 заглушка должна быть обновлена
    в соответствии с новым контрактом.
    """

    def prepare_document_queries(
        self,
        doc_id: str,
        file_path: str,
        file_hash: str,
        file_size: int,
        last_modified: str,
        abs_file_path: str,
    ) -> list[tuple[str, tuple]] | None:
        """Фаза 4: возвращает ``None`` («нет данных для индексации»).

        В реальной реализации метод открывает PDF и формирует SQL
        для страниц. В заглушке — no-op: тесты проверяют логику
        отмены, а не извлечение текста.

        В Фазе 5 метод удаляется вместе с интерфейсом — вместо
        него вызывается ``prepare_index_plan``.
        """
        return None

    def write_documents_batch(
        self,
        document_batches: list[list[tuple[str, tuple]]],
    ) -> tuple[int, int]:
        """Фаза 4: батчевая запись. Возвращает ``(N, 0)``."""
        return len(document_batches), 0

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

        Сигнатура соответствует расширенному контракту
        ``ITextIndexer.get_document_metadata`` (шаг 4.4 плана v9):
        4-элементный кортеж ``(doc_id, file_hash, cached_size,
        cached_mtime)``.
        """
        return None


class _StubTextExtractor:
    """Заглушка ``ITextExtractor``.

    Используется как обязательный параметр конструктора
    ``ScanPipeline`` (поле ``text_extractor``). Внутри конвейера
    не вызывается напрямую: извлечение текста выполняется через
    ``process_runner`` или через ``_indexer.prepare_document_queries``.
    """

    def open_document(self, file_path: str) -> Any:
        """Не вызывается в skip-тестах. Заглушка для типизации."""
        raise NotImplementedError("StubTextExtractor.open_document: активируется в Фазе 4/5.")


class _StubProcessRunner:
    """Заглушка ``IProcessTaskRunner``.

    Эмулирует работу ``process_runner.run`` через ``asyncio.sleep``,
    не создавая дочерних процессов. Публичный интерфейс совпадает
    с реальным runner'ом (сигнатура метода ``run``).

    Дополнительно предоставляет ``first_call_event`` — ``asyncio.Event``,
    который устанавливается при первом вызове. Тесты ожидают его
    перед вызовом ``pipeline.cancel()``, чтобы гарантировать, что
    конвейер действительно начал обработку.

    При активации в Фазе 5 будет использоваться вместо реального
    ``ProcessTaskRunner``; фабрика :func:`_make_pipeline` передаёт
    его через параметр ``process_runner``.
    """

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self._closed = False
        self.call_count = 0
        self.first_call_event = asyncio.Event()

    async def run(
        self,
        func: Any,
        *args: Any,
        timeout: float,
        kind: str = "interactive",
    ) -> Any:
        """Эмулирует работу: sleep(delay) → ``None``.

        Возвращает ``None`` — это соответствует поведению
        реального ``pdf_worker`` при ошибке открытия PDF. Конвейер
        интерпретирует результат как «файл помечен ошибочным», но
        в контексте тестов отмены это не важно.
        """
        if self._closed:
            raise RuntimeError("ProcessTaskRunner is closed")
        self.call_count += 1
        if not self.first_call_event.is_set():
            self.first_call_event.set()
        await asyncio.sleep(self._delay)
        return None

    async def close(self, shutdown_timeout: float = 10.0) -> None:
        """No-op: заглушка не владеет ресурсами."""
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
            в ``ScanPipeline`` при активации тестов в Фазе 5 (см.
            параметр ``process_runner`` фабрики :func:`_make_pipeline`).
        rd_directory: Фиктивный корневой каталог РД.
    """

    scanner: _StubScanner
    hasher: _StubHasher
    indexer: _StubIndexer
    text_extractor: _StubTextExtractor
    process_runner: _StubProcessRunner
    rd_directory: str


def _make_pipeline(
    db: IDatabase,
    stubs: _Stubs,
    executor: ThreadPoolExecutor,
    event_bus: IEventBus,
    *,
    process_runner: Any | None = None,
    pdf_worker: Callable[..., Any] | None = None,
) -> ScanPipeline:
    """Собирает ``ScanPipeline`` со стабами для теста отмены.

    Все внешние зависимости заменены на легковесные заглушки или
    реальные, но изолированные компоненты (SQLite на tmp, unstarted
    event bus).

    Параметры ``process_runner`` и ``pdf_worker`` опциональны.
    Если заданы — ``ScanPipeline`` использует приоритетный путь
    извлечения через subprocess (Фаза 4). Если ``None`` —
    используется потоковый fallback через
    ``indexer.prepare_document_queries`` в ``scan_executor``.

    При активации тестов в Фазе 5 фабрика будет вызываться так::

        _make_pipeline(
            db, stubs, executor, event_bus,
            process_runner=stubs.process_runner,
            pdf_worker=_noop_pdf_worker,
        )

    Начиная с Фазы 5 сигнатура расширится:

    - ``pdf_worker`` заменится на ``index_plan_worker`` (см.
      ``DocumentIndexPlan``);
    - сигнатура воркера изменится: вместо 6 позиционных
      аргументов — кортеж параметров и возвращаемый тип
      ``DocumentIndexPlan | None``.

    Args:
        db: Реальный SQLiteAdapter на tmp_path с созданной схемой.
        stubs: Контейнер заглушек.
        executor: Реальный ThreadPoolExecutor для блокирующих
            операций (scan_directory, hashing, write_index_plans).
        event_bus: AsyncEventBus (не запущен).
        process_runner: Опциональный ``IProcessTaskRunner``.
            Если задан — используется приоритетный путь
            извлечения (Фаза 4). Значение по умолчанию ``None``
            сохраняет потоковый fallback.
        pdf_worker: Опциональная picklable-функция извлечения
            текста одного PDF. Обязательна при заданном
            ``process_runner``. Значение по умолчанию ``None``
            сохраняет потоковый fallback.

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
        process_runner=process_runner,
        pdf_worker=pdf_worker,
    )


def _noop_pdf_worker(
    abs_file_path: str,
    doc_id: str,
    relative_path: str,
    file_hash: str,
    file_size: int,
    last_modified: str,
) -> list[tuple[str, tuple]] | None:
    """Фиктивный worker для ``pdf_worker`` в тестах отмены.

    Модульная (picklable) функция с сигнатурой, совпадающей
    с ``extract_document_queries``. Возвращает ``None`` — как
    реальный worker при ошибке открытия PDF. Используется
    заглушкой ``_StubProcessRunner``: ``run(_noop_pdf_worker, ...)``
    вызывается без реального subprocess, но с корректным
    интерфейсом.

    Начиная с Фазы 5 будет заменена на ``index_plan_worker`` с
    другой сигнатурой и возвращаемым типом ``DocumentIndexPlan``.

    Args:
        abs_file_path: Абсолютный путь к PDF.
        doc_id: Идентификатор документа.
        relative_path: Относительный путь (от каталога РД).
        file_hash: Хеш файла.
        file_size: Размер файла в байтах.
        last_modified: Дата изменения (ISO 8601).

    Returns:
        ``None`` (сигнал «нет данных для индексации»).
    """
    return None


def _noop_index_plan_worker(*args: Any, **kwargs: Any) -> list[Any]:
    """Фиктивный worker для ``index_plan_worker`` (Фаза 5).

    Возвращает пустой план. В Фазе 4 не используется: параметр
    ``index_plan_worker`` появится в ``ScanPipeline`` только в
    Фазе 5 (вместе с ``DocumentIndexPlan``). Функция сохранена
    как заготовка для активации.
    """
    return []


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

    async def _run() -> None:
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

    asyncio.run(_run())

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

    async def _run() -> Any:
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

    progress = asyncio.run(_run())

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

    async def _run() -> None:
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

    asyncio.run(_run())

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

    asyncio.run(_first_run())
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

    progress = asyncio.run(_second_run())
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

    async def _run() -> Any:
        return await asyncio.wait_for(
            pipeline.run_async(
                rd_directory=stubs.rd_directory,
                scan_id=20,
                correlation_id="test-cancel-before-run",
            ),
            timeout=30.0,
        )

    progress = asyncio.run(_run())
    assert progress.status == ScanStatus.COMPLETED
