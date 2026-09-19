"""
Конвейер параллельного сканирования.

Класс ``ScanPipeline`` реализует параллельный конвейер сканирования
каталога рабочей документации из четырёх этапов, связанных через
``asyncio.Queue``:

Этап 1: Сканирование каталога (через пул потоков)
    ``IScanner.scan_directory()`` → ``_file_queue``

Этап 2: Ленивое хеширование (через пул потоков)
    ``_file_queue`` → проверка метаданных и хеша → ``_extract_queue``
    Обновление счётчиков ``skipped_files``, ``duplicate_files``, ``error_files``.

Этап 3: Извлечение текста и батчевая запись (через ProcessPoolExecutor или пул потоков)
    ``_extract_queue`` → ``extract_document_queries`` или ``TextIndexer.prepare_document_queries`` →
    буфер → ``TextIndexer.write_documents_batch`` → БД
    Обновление счётчиков ``indexed_files``, ``error_files``.

Этап 4: Запись в БД (внутри этапа 3, батчевая)
    ``TextIndexer`` → ``SQLiteAdapter.execute_write_batched_documents``
    (``threading.Lock``)

Архитектурные решения:

+-------------------------------+------------------------------------------+
| Решение                       | Обоснование                              |
+===============================+==========================================+
| Точный трекинг прогресса      | Счётчик ``processed_files`` инкременти-  |
| (исправление UI-бага)         | руется только когда файл достигает       |
|                               | терминального состояния (пропущен,       |
|                               | дубликат, ошибка или успешно записан).   |
|                               | Это предотвращает ситуацию, когда        |
|                               | прогресс показывает 100% на этапе        |
|                               | хеширования, хотя извлечение текста      |
|                               | ещё продолжается.                        |
+-------------------------------+------------------------------------------+
| Выделенный пул потоков        | Все блокирующие операции выносятся в     |
| (``scan_executor``)           | выделенный пул потоков. Это предотвра-   |
|                               | щает конкуренцию с веб-запросами.        |
+-------------------------------+------------------------------------------+
| ``asyncio.Queue``             | Неблокирующая координация. ``maxsize``   |
| между этапами                 | предотвращает переполнение памяти        |
|                               | (backpressure).                          |
+-------------------------------+------------------------------------------+
| Использование ``HashResult``  | Явное разделение исходов хеширования     |
|                               | (``HashOutcome``) позволяет точно        |
|                               | классифицировать файлы и обновлять       |
|                               | соответствующие счётчики прогресса.      |
+-------------------------------+------------------------------------------+
| Кэширование таблицы           | Перед фазой хеширования таблица          |
| ``documents`` в памяти        | ``documents`` загружается в память       |
|                               | одним SELECT-запросом. Три запроса       |
|                               | к БД на каждый файл заменены поиском     |
|                               | в словаре за O(1).                       |
+-------------------------------+------------------------------------------+
| Публикация только важных      | Публикуются только ошибки и прогресс     |
| событий                       | (не чаще одного раза в секунду на        |
|                               | конвейер).                               |
+-------------------------------+------------------------------------------+
| Батчевая запись документов    | Запись группы документов через           |
| (Фаза 3 оптимизации)          | ``SAVEPOINT`` и батчевые ``COMMIT``      |
|                               | снижает количество коммитов и            |
|                               | ускоряет массовое индексирование.        |
+-------------------------------+------------------------------------------+
| Таймауты блокирующих          | Все критичные блокирующие операции       |
| операций                      | обёрнуты в ``run_with_timeout``          |
|                               | из ``timeout_guard``. При срабатывании   |
|                               | таймаута публикуется событие             |
|                               | ``OperationTimedOut`` на уровне WARNING. |
+-------------------------------+------------------------------------------+
| Проброс ``CancelledError``    | При отмене задачи извне                  |
|                               | (``task.cancel()``) конвейер             |
|                               | пробрасывает ``CancelledError`` для      |
|                               | корректной обработки в                   |
|                               | ``ScanOrchestrator.run_primary_scan``.   |
+-------------------------------+------------------------------------------+
| Отслеживание зависших         | При таймауте извлечения текста           |
| процессов                     | трекер фиксирует зависший процесс.       |
|                               | При достижении порога                    |
|                               | ``_max_extract_workers`` конвейер        |
|                               | переключается на потоковый fallback.     |
+-------------------------------+------------------------------------------+
| Безопасное завершение при     | При отмене сканирования остаток буфера   |
| отмене                        | не записывается в БД. При вынужденном    |
|                               | сбросе (RuntimeError, timeout) потери    |
|                               | логируются через ``_log_buffer_loss``.   |
+-------------------------------+------------------------------------------+
| Отмена через ``asyncio.Event``| Механика отмены построена на             |
|                               | ``_cancel_event`` (не на булевом флаге). |
|                               | Воркеры ожидают задачу через             |
|                               | ``_wait_task_or_cancel``, а отправляют   |
|                               | результаты через ``_put_or_cancel``.    |
|                               | Оба используют ``asyncio.wait`` с        |
|                               | приоритетом отмены: при одновременной    |
|                               | готовности — побеждает отмена.           |
+-------------------------------+------------------------------------------+
| Уникальный тип ``_QueueSentinel``| Маркер завершения очереди — не         |
|                               | ``None``, а экземпляр класса             |
|                               | ``_QueueSentinel``. Это позволяет        |
|                               | различить сентинел и сигнал отмены       |
|                               | (``None``), которые иначе совпали бы.    |
+-------------------------------+------------------------------------------+
| ``_put_or_cancel``            | Симметричен ``_wait_task_or_cancel``:    |
|                               | защищает ``queue.put`` от блокировки при |
|                               | заполненной очереди и установленном      |
|                               | ``_cancel_event``. Обязателен для        |
|                               | предотвращения deadlock: без него        |
|                               | продюсеры (hash-воркеры) блокируются на  |
|                               | ``put`` в заполненную ``_extract_queue``,|
|                               | не проверяя отмену.                      |
+-------------------------------+------------------------------------------+

Принципы:
- Application layer: оркестрирует, не содержит бизнес-логики.
- DIP: зависит от абстракций, реализации через DI.
- Не выполняет логирование (кроме потерь буфера при отмене),
  публикует важные события через шину.
- Конвейер является асинхронной корутиной.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from concurrent.futures import Executor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from typing import Final

from ..domain import config
from ..domain.events import FileProcessingFailed
from ..domain.interfaces import (
    IDatabase,
    IDocumentCache,
    IEventBus,
    IHasher,
    IScanner,
    ITextExtractor,
    ITextIndexer,
)
from ..domain.models import (
    HashOutcome,
    HashResult,
    ScanProgress,
    ScanStatus,
)
from .async_utils import run_blocking_in_executor
from .extract_worker import extract_document_queries
from .progress_tracker import ScanProgressTracker
from .timeout_guard import run_with_timeout

# ----------------------------------------------------------------------
# Логгер потерь буфера
# ----------------------------------------------------------------------

_logger = logging.getLogger("dds.scan_pipeline")
"""Логгер конвейера. Используется только для сообщений о потере
несохранённых документов при финальном сбросе буфера (RuntimeError,
timeout, отмена). Все остальные события публикуются через шину."""


# ----------------------------------------------------------------------
# Модель данных для передачи между этапами конвейера
# ----------------------------------------------------------------------


@dataclass
class _FileTask:
    """Задача обработки одного файла в конвейере.

    Передаётся между этапами через ``asyncio.Queue``.

    Заполнение полей по этапам:

    +--------------------+-----------+------------------------------------+
    | Поле               | Этап      | Описание                           |
    +====================+===========+====================================+
    | ``abs_file_path``  | 1         | Абсолютный путь к файлу.           |
    +--------------------+-----------+------------------------------------+
    | ``relative_path``  | 1         | Относительный путь от каталога РД. |
    +--------------------+-----------+------------------------------------+
    | ``file_hash``      | 2         | Хеш файла (SHA-256).               |
    +--------------------+-----------+------------------------------------+
    | ``file_size``      | 2         | Размер файла в байтах.             |
    +--------------------+-----------+------------------------------------+
    | ``last_modified``  | 2         | Дата изменения (ISO 8601).         |
    +--------------------+-----------+------------------------------------+
    | ``doc_id``         | 2         | UUID (новый) или существующий      |
    |                    |           | ``doc_id`` (переиндексация).       |
    +--------------------+-----------+------------------------------------+
    """

    abs_file_path: str
    relative_path: str
    file_hash: str = ""
    file_size: int = 0
    last_modified: str = ""
    doc_id: str = ""


# ----------------------------------------------------------------------
# Сигнальный объект для завершения очереди
# ----------------------------------------------------------------------


class _QueueSentinel:
    """Уникальный маркер завершения очереди.

    Экземпляр класса создаётся один раз на уровне модуля
    (константа ``_QUEUE_SENTINEL``). Воркеры распознают его через
    проверку ``is _QUEUE_SENTINEL``. Отдельный класс нужен, чтобы
    отличать маркер от ``None`` (сигнал кооперативной отмены):
    при использовании ``None`` в качестве сентинела оба сигнала
    совпали бы, и воркер не смог бы отличить нормальное завершение
    от отмены.

    Атрибутов нет: класс используется как уникальный тип-маркер.
    """

    __slots__ = ()


_QUEUE_SENTINEL: Final = _QueueSentinel()
"""Единственный экземпляр маркера завершения очереди.

Передаётся через очереди ``_file_queue`` и ``_extract_queue``,
чтобы воркеры корректно завершили цикл обработки после того,
как продюсер закончил отправку задач.
"""


# ----------------------------------------------------------------------
# Конвейер параллельного сканирования
# ----------------------------------------------------------------------


class ScanPipeline:
    """
    Конвейер параллельного сканирования.

    Оркестрирует параллельное сканирование каталога РД через
    этапы, связанные ``asyncio.Queue``.

    Точный трекинг прогресса (исправление UI-бага):
    Счётчик ``processed_files`` представляет собой сумму
    терминальных состояний файла:

    ``processed_files = indexed_files + duplicate_files +
    skipped_files + error_files``.

    Файл считается «обработанным» только когда он полностью
    прошёл конвейер (записан в БД, отклонён как дубликат,
    пропущен без изменений или завершился с ошибкой).
    Это гарантирует, что прогресс-бар в UI отражает реальную
    нагрузку на систему, включая тяжёлую фазу извлечения текста.

    Отмена (скорректированный план):
    Механика отмены построена на ``asyncio.Event`` — ``_cancel_event``.
    Воркеры ожидают задачу через ``_wait_task_or_cancel`` и
    отправляют результаты через ``_put_or_cancel``. Оба метода
    используют ``asyncio.wait`` с двумя задачами: операцией с
    очередью и ``cancel_event.wait()``. При одновременной
    готовности обеих задач приоритет отдаётся отмене.

    Отмена ``cancel()`` не прерывает задачу asyncio — она лишь
    устанавливает флаг. Это кооперативная отмена: воркеры
    завершаются сами, после текущей операции.

    Внешняя отмена через ``task.cancel()`` приводит к тому, что
    ``run_async`` перехватывает ``CancelledError``, устанавливает
    статус ``INTERRUPTED`` и пробрасывает исключение дальше.

    Таймауты блокирующих операций:
    Все критичные блокирующие операции обёрнуты в
    ``run_with_timeout`` из ``timeout_guard``.

    Отслеживание зависших процессов:
    При таймауте извлечения текста трекер фиксирует зависший
    процесс через ``record_stuck_process()``. При достижении
    порога ``_max_extract_workers`` конвейер устанавливает
    ``_executor_broken = True`` и переключается на потоковый
    fallback.

    Безопасное завершение при отмене:
    При отмене сканирования (``_cancel_event.is_set()``) остаток
    буфера документов **не записывается** в БД. При вынужденном
    сбросе потери логируются через ``_log_buffer_loss``.

    Attributes:

    +-------------------------+-------------------------------------------+
    | Атрибут                 | Описание                                  |
    +=========================+===========================================+
    | ``_db``                 | Абстракция базы данных.                   |
    +-------------------------+-------------------------------------------+
    | ``_scanner``            | Абстракция сканера каталога.              |
    +-------------------------+-------------------------------------------+
    | ``_hasher``             | Абстракция хешера файлов.                 |
    +-------------------------+-------------------------------------------+
    | ``_text_extractor``     | Абстракция извлекателя текста.            |
    +-------------------------+-------------------------------------------+
    | ``_indexer``            | Абстракция индексатора текстового слоя.   |
    +-------------------------+-------------------------------------------+
    | ``_event_bus``          | Шина событий для публикации.              |
    +-------------------------+-------------------------------------------+
    | ``_tracker``            | Трекер прогресса и троттлинга.            |
    +-------------------------+-------------------------------------------+
    | ``_document_cache``     | Кэш метаданных документов или ``None``.   |
    +-------------------------+-------------------------------------------+
    | ``_extract_executor``   | Пул процессов для извлечения текста или   |
    |                         | ``None`` (fallback на потоки).            |
    +-------------------------+-------------------------------------------+
    | ``_executor_broken``    | Флаг, указывающий, что пул процессов      |
    |                         | сломан и следует использовать fallback.   |
    +-------------------------+-------------------------------------------+
    | ``_cancel_event``       | ``asyncio.Event`` для кооперативной       |
    |                         | отмены. Создаётся в ``run_async``,        |
    |                         | устанавливается в ``cancel()``.           |
    +-------------------------+-------------------------------------------+
    """

    def __init__(
        self,
        db: IDatabase,
        scanner: IScanner,
        hasher: IHasher,
        text_extractor: ITextExtractor,
        indexer: ITextIndexer,
        event_bus: IEventBus,
        max_hash_workers: int | None = None,
        max_extract_workers: int | None = None,
        scan_executor: Executor | None = None,
        document_cache: IDocumentCache | None = None,
        extract_executor: Executor | None = None,
    ) -> None:
        """
        Инициализирует конвейер параллельного сканирования.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Сохранение ссылок на зависимости.                   |
        +---+-----------------------------------------------------+
        | 2 | Инициализация ``_tracker`` через                     |
        |   | ``ScanProgressTracker(event_bus)``.                  |
        +---+-----------------------------------------------------+
        | 3 | Сохранение ``_document_cache`` и                     |
        |   | ``_extract_executor``.                               |
        +---+-----------------------------------------------------+
        | 4 | Установка ``_executor_broken = False``.              |
        +---+-----------------------------------------------------+
        | 5 | Инициализация ``_cancel_event = None``. Событие      |
        |   | создаётся в ``run_async``.                          |
        +---+-----------------------------------------------------+
        """
        self._db = db
        self._scanner = scanner
        self._hasher = hasher
        self._text_extractor = text_extractor
        self._indexer = indexer
        self._event_bus = event_bus
        self._max_hash_workers = (
            max_hash_workers if max_hash_workers is not None else config.SCAN_HASH_WORKERS
        )
        self._max_extract_workers = (
            max_extract_workers if max_extract_workers is not None else config.SCAN_EXTRACT_WORKERS
        )
        self._cancel_event: asyncio.Event | None = None
        self._tracker = ScanProgressTracker(event_bus)
        self._rd_directory: str = ""
        self._correlation_id: str = ""
        self._file_queue: asyncio.Queue[str | _QueueSentinel] | None = None
        self._extract_queue: asyncio.Queue[_FileTask | _QueueSentinel] | None = None
        self._scan_executor = scan_executor
        self._document_cache = document_cache
        self._extract_executor = extract_executor
        self._executor_broken = False

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _log_buffer_loss(self, count: int, reason: str) -> None:
        """Логирует потерю несохранённых документов.

        Вызывается при вынужденном сбросе буфера в финальном
        ``flush_buffer`` (RuntimeError от закрытого executor'а,
        timeout, отмена). Потери происходят при отмене сканирования,
        таймауте записи или закрытии БД.

        Логирование выполняется через стандартный ``logging``, а не
        через шину событий: это не ошибка обработки конкретного файла,
        а отмена на уровне конвейера.

        Args:
            count: Количество потерянных документов в буфере.
            reason: Краткое описание причины (например,
                ``"timeout"``, ``"RuntimeError: ..."``).
        """
        _logger.warning(
            "Потеряно %d документов при финальном flush (причина: %s)",
            count,
            reason,
        )

    def _check_error_threshold(self) -> bool:
        """Проверяет, превышен ли порог ошибок сканирования.

        Если количество ошибок достигло или превысило
        ``config.MAX_SCAN_ERRORS``, устанавливает статус ``ERROR``
        и устанавливает ``_cancel_event``, возвращая ``True``.
        В противном случае возвращает ``False``.

        Returns:
            ``True``, если порог превышен и сканирование должно
            быть остановлено; ``False`` в противном случае.
        """
        if self._tracker.get_error_count() >= config.MAX_SCAN_ERRORS:
            self._tracker.set_status(ScanStatus.ERROR)
            if self._cancel_event is not None:
                self._cancel_event.set()
            return True
        return False

    async def _wait_task_or_cancel(
        self,
        queue: asyncio.Queue,
    ) -> object | None:
        """Ожидает задачу из очереди или кооперативную отмену.

        Создаёт две задачи: получение элемента из очереди и ожидание
        события отмены. Ждёт первую завершившуюся через
        ``asyncio.wait``.

        Приоритет отмене: если сработала отмена (даже если
        одновременно готова задача из очереди), метод возвращает
        ``None``. Задача ``get_task`` отменяется; если элемент уже
        был получен из очереди, он **не теряется** — остаётся в
        локальной переменной до уничтожения конвейера.

        Если сработала задача — отменяет ``cancel_task`` и
        возвращает результат ``get_task``.

        Args:
            queue: Очередь для получения задачи.

        Returns:
            Задача из очереди или ``None`` при отмене.

        Raises:
            RuntimeError: Если ``_cancel_event`` не инициализирован
                (вызов до ``run_async``).
        """
        if self._cancel_event is None:
            raise RuntimeError("_cancel_event не инициализирован")

        get_task: asyncio.Task = asyncio.create_task(queue.get())
        cancel_task: asyncio.Task = asyncio.create_task(self._cancel_event.wait())

        done, _ = await asyncio.wait(
            {get_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Приоритет отмене: если сработала — игнорируем полученную
        # задачу. Она не потеряется: останется в очереди до
        # уничтожения конвейера.
        if cancel_task in done:
            get_task.cancel()
            try:
                await get_task
            except asyncio.CancelledError:
                pass
            return None

        # Задача получена, отмена не сработала.
        cancel_task.cancel()
        try:
            await cancel_task
        except asyncio.CancelledError:
            pass
        return get_task.result()

    async def _put_or_cancel(
        self,
        queue: asyncio.Queue,
        item: object,
    ) -> bool:
        """Кладёт элемент в очередь с возможностью кооперативной отмены.

        Симметричен ``_wait_task_or_cancel``: гоняет ``queue.put(item)``
        против ``cancel_event.wait()`` через ``asyncio.wait``.
        Приоритет отмене: при одновременной готовности обеих задач
        элемент **не кладётся** в очередь (или, если put уже успел
        завершиться, элемент остаётся в очереди, но потребители его
        проигнорируют после отмены).

        Без этого метода hash-воркеры могут блокироваться на
        ``await extract_queue.put(task)`` при заполненной очереди и
        не проверять ``_cancel_event`` — это приводит к deadlock
        всего конвейера.

        Args:
            queue: Очередь для отправки.
            item: Элемент для отправки.

        Returns:
            ``True``, если элемент положен в очередь.
            ``False``, если сработала отмена (элемент не положен).
        """
        if self._cancel_event is None:
            # Конвейер не запущен через run_async — put без отмены.
            await queue.put(item)
            return True

        if self._cancel_event.is_set():
            return False

        put_task: asyncio.Task = asyncio.create_task(queue.put(item))
        cancel_task: asyncio.Task = asyncio.create_task(self._cancel_event.wait())

        done, _ = await asyncio.wait(
            {put_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Приоритет отмене: если сработала — отменяем put (если ещё
        # не завершился). Если put уже завершился, элемент остаётся
        # в очереди — это допустимо: после отмены потребители его
        # проигнорируют.
        if cancel_task in done:
            put_task.cancel()
            try:
                await put_task
            except asyncio.CancelledError:
                pass
            return False

        # Put завершился, отмена не сработала.
        cancel_task.cancel()
        try:
            await cancel_task
        except asyncio.CancelledError:
            pass
        return True

    # ------------------------------------------------------------------
    # Публичные методы
    # ------------------------------------------------------------------

    async def run_async(
        self,
        rd_directory: str,
        scan_id: int,
        correlation_id: str,
    ) -> ScanProgress:
        """
        Запускает конвейер асинхронно в текущем event loop.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Создание ``_cancel_event`` (свежий для запуска).    |
        +---+-----------------------------------------------------+
        | 2 | Сохранение параметров и настройка трекера.          |
        +---+-----------------------------------------------------+
        | 3 | Загрузка кэша документов с таймаутом                |
        |   | ``scan.cache_load``. При таймауте кэш               |
        |   | отключается (``_document_cache = None``).           |
        +---+-----------------------------------------------------+
        | 4 | Создание очередей с ``maxsize`` (backpressure).     |
        +---+-----------------------------------------------------+
        | 5 | Запуск этапов в ``asyncio.TaskGroup``.              |
        +---+-----------------------------------------------------+
        | 6 | Обработка отмены и ошибок → установка статуса.      |
        |   | При ``CancelledError`` — установка статуса          |
        |   | ``INTERRUPTED`` и **проброс** исключения.           |
        +---+-----------------------------------------------------+
        | 7 | Возврат итогового ``ScanProgress``.                 |
        +---+-----------------------------------------------------+

        Примечание:
            При получении ``CancelledError`` извне
            (``task.cancel()``) конвейер устанавливает статус
            ``INTERRUPTED`` и пробрасывает исключение. Это
            позволяет ``ScanOrchestrator.run_primary_scan``
            корректно обработать отмену и опубликовать событие
            ``ScanCancelled``.

        Args:
            rd_directory: Путь к каталогу рабочей документации.
            scan_id: Идентификатор записи в таблице ``scan_state``.
            correlation_id: Идентификатор корреляции для событий.

        Returns:
            Итоговый ``ScanProgress``.
        """
        self._cancel_event = asyncio.Event()
        self._rd_directory = rd_directory
        self._correlation_id = correlation_id

        # Настройка трекера для текущего запуска
        self._tracker.configure(correlation_id)

        # Загрузка кэша документов (если доступен)
        # с таймаутом
        if self._document_cache is not None:
            try:
                await run_with_timeout(
                    run_blocking_in_executor(
                        self._scan_executor,
                        self._document_cache.load,
                    ),
                    operation="scan.cache_load",
                    event_bus=self._event_bus,
                    recovery_action=("Кэш не загружен, используется прямое обращение к БД."),
                )
            except TimeoutError:
                self._document_cache = None

        self._file_queue = asyncio.Queue(maxsize=config.SCAN_QUEUE_MAX_SIZE)
        self._extract_queue = asyncio.Queue(maxsize=config.SCAN_QUEUE_MAX_SIZE)

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._scan_directory_task(rd_directory, scan_id))
                tg.create_task(self._hash_worker_pool(scan_id))
                tg.create_task(self._extract_worker_pool(scan_id))
        except asyncio.CancelledError:
            self._tracker.set_status(ScanStatus.INTERRUPTED)
            raise  # Обязательно пробрасываем для корректной
            # обработки в ScanOrchestrator.run_primary_scan
        except ExceptionGroup:
            self._tracker.set_status(ScanStatus.ERROR)
        except Exception:  # noqa: BLE001
            self._tracker.set_status(ScanStatus.ERROR)

        if self._tracker.get_status() == ScanStatus.RUNNING:
            if self._cancel_event is not None and self._cancel_event.is_set():
                self._tracker.set_status(ScanStatus.INTERRUPTED)
            else:
                self._tracker.set_status(ScanStatus.COMPLETED)

        return self.get_progress()

    def cancel(self) -> None:
        """Запрашивает кооперативную отмену сканирования.

        Устанавливает ``_cancel_event``. Воркеры, заблокированные
        на ``_wait_task_or_cancel`` или ``_put_or_cancel``,
        разблокируются и завершатся. Воркеры, обрабатывающие файл
        в данный момент, завершат текущую операцию и затем увидят
        флаг отмены.

        Если сканирование не запущено (``_cancel_event is None``),
        метод не выполняет никаких действий.

        Примечание:
            ``asyncio.Event.set()`` безопасен только из event loop,
            в котором создан Event. Метод вызывается из
            ``ScanOrchestrator.cancel_scan`` в том же event loop.
        """
        if self._cancel_event is not None:
            self._cancel_event.set()

    def get_progress(self) -> ScanProgress:
        """Возвращает текущий прогресс сканирования (копию).

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Делегирование в ``_tracker.get_progress()``.        |
        +---+-----------------------------------------------------+
        """
        return self._tracker.get_progress()

    # ------------------------------------------------------------------
    # Этап 1: Сканирование каталога
    # ------------------------------------------------------------------

    async def _scan_directory_task(
        self,
        rd_directory: str,
        scan_id: int,
    ) -> None:
        """Этап 1: сканирование каталога и заполнение очереди файлов.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | ``scan_directory`` в пуле потоков с таймаутом       |
        |   | ``scan.directory_scan``. При таймауте:              |
        |   | a. Установка статуса ``ERROR``.                     |
        |   | b. Установка ``_cancel_event``.                     |
        |   | c. Выход (hash-воркеры завершатся по флагу).        |
        +---+-----------------------------------------------------+
        | 2 | Установка ``total_files`` через трекер.             |
        +---+-----------------------------------------------------+
        | 3 | Отправка путей в ``_file_queue`` через              |
        |   | ``_put_or_cancel``. При отмене — выход.             |
        +---+-----------------------------------------------------+
        | 4 | Отправка ``_QUEUE_SENTINEL`` через ``_put_or_cancel``|
        |   | — по одному на hash-воркера. При отмене — выход.    |
        +---+-----------------------------------------------------+

        Примечание:
            Отправка через ``_put_or_cancel`` защищает от
            блокировки при заполненной очереди и установленном
            ``_cancel_event``.
        """
        try:
            files = await run_with_timeout(
                run_blocking_in_executor(
                    self._scan_executor,
                    self._scanner.scan_directory,
                    rd_directory,
                ),
                operation="scan.directory_scan",
                event_bus=self._event_bus,
                context=rd_directory,
                recovery_action="Сканирование каталога прервано.",
            )
        except TimeoutError:
            self._tracker.set_status(ScanStatus.ERROR)
            if self._cancel_event is not None:
                self._cancel_event.set()
            return

        self._tracker.set_total_files(len(files))

        await run_blocking_in_executor(
            self._scan_executor,
            self._db.execute_write,
            "UPDATE scan_state SET total_files = ? WHERE scan_id = ?",
            (len(files), scan_id),
        )

        for file_path in files:
            if not await self._put_or_cancel(self._file_queue, file_path):
                return  # Отмена

        for _ in range(self._max_hash_workers):
            if not await self._put_or_cancel(self._file_queue, _QUEUE_SENTINEL):
                return  # Отмена

    # ------------------------------------------------------------------
    # Этап 2: Ленивое хеширование
    # ------------------------------------------------------------------

    async def _hash_worker_pool(self, scan_id: int) -> None:
        """Этап 2: пул асинхронных воркеров для ленивого хеширования.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Запуск воркеров в ``TaskGroup``.                    |
        +---+-----------------------------------------------------+
        | 2 | Отправка ``_QUEUE_SENTINEL`` в ``_extract_queue``    |
        |   | через ``_put_or_cancel`` (по одному на              |
        |   | extract-воркера). При отмене — выход.               |
        +---+-----------------------------------------------------+

        Примечание:
            Отправка сентинелов вынесена за пределы TaskGroup
            hash-воркеров. К моменту выхода из TaskGroup все
            hash-воркеры завершены, новые задачи в ``_extract_queue``
            не поступают. ``_put_or_cancel`` разблокируется, так как
            extract-воркеры продолжают читать очередь (или
            завершаются по отмене).
        """
        async with asyncio.TaskGroup() as tg:
            for _ in range(self._max_hash_workers):
                tg.create_task(self._hash_worker(scan_id))

        # Все hash-воркеры завершены. Отправляем сентинелы в extract.
        # При отмене _put_or_cancel вернёт False, и мы выйдем.
        for _ in range(self._max_extract_workers):
            if not await self._put_or_cancel(self._extract_queue, _QUEUE_SENTINEL):
                return

    async def _hash_worker(self, scan_id: int) -> None:
        """Один воркер ленивого хеширования.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Ожидание задачи через ``_wait_task_or_cancel``.     |
        +---+-----------------------------------------------------+
        | 2 | Если ``None`` — отмена, выход из цикла.             |
        +---+-----------------------------------------------------+
        | 3 | Если ``_QueueSentinel`` — завершение очереди,       |
        |   | ``task_done()`` и выход.                            |
        +---+-----------------------------------------------------+
        | 4 | Вызов ``_hash_single_file`` в пуле потоков.         |
        +---+-----------------------------------------------------+
        | 5 | Обновление счётчиков через трекер.                  |
        +---+-----------------------------------------------------+
        | 6 | Проверка ``MAX_SCAN_ERRORS``.                       |
        +---+-----------------------------------------------------+
        | 7 | Отправка задачи в ``_extract_queue`` через          |
        |   | ``_put_or_cancel`` при исходе ``TO_INDEX``.         |
        +---+-----------------------------------------------------+
        | 8 | Публикация ``FileProcessingFailed`` при ошибке.     |
        +---+-----------------------------------------------------+
        | 9 | Троттлинг публикации прогресса через трекер.        |
        +---+-----------------------------------------------------+
        | 10| ``task_done()`` для ``_file_queue``.                |
        +---+-----------------------------------------------------+

        Примечание:
            Отправка в ``_extract_queue`` через ``_put_or_cancel``
            предотвращает deadlock: без этого при заполненной
            очереди и установленном ``_cancel_event`` воркер
            заблокировался бы на ``put`` и не увидел бы отмену.
        """
        while True:
            item = await self._wait_task_or_cancel(self._file_queue)
            if item is None:
                # Кооперативная отмена.
                break
            if isinstance(item, _QueueSentinel):
                self._file_queue.task_done()
                break

            file_path: str = item
            self._tracker.set_current_file(file_path)

            # Блокирующая операция хеширования
            result: HashResult = await run_blocking_in_executor(
                self._scan_executor,
                self._hash_single_file,
                file_path,
            )

            # Обновление счётчиков через трекер
            self._tracker.record_hash_outcome(result.outcome, file_path)

            # Проверка порога ошибок
            self._check_error_threshold()

            # Отправка в следующий этап
            put_ok = True
            if result.outcome == HashOutcome.TO_INDEX:
                task = _FileTask(
                    abs_file_path=result.abs_file_path,
                    relative_path=result.relative_path,
                    file_hash=result.file_hash,
                    file_size=result.file_size,
                    last_modified=result.last_modified,
                    doc_id=result.doc_id,
                )
                put_ok = await self._put_or_cancel(self._extract_queue, task)
            elif result.outcome == HashOutcome.ERROR:
                # Публикация события об ошибке хеширования
                self._event_bus.publish(
                    FileProcessingFailed(
                        correlation_id=self._correlation_id,
                        source="ScanPipeline",
                        stage="hashing",
                        scan_id=scan_id,
                        file_path=result.relative_path or result.abs_file_path,
                        error_type="HashError",
                        error_message=result.error_message,
                        traceback="",
                    )
                )

            # Троттлинг публикации прогресса
            self._tracker.publish_throttled(scan_id, "hashing")

            self._file_queue.task_done()

            if not put_ok:
                # Отмена во время отправки в extract_queue.
                break

    def _hash_single_file(
        self,
        file_path: str,
    ) -> HashResult:
        """Ленивое хеширование одного файла (выполняется в пуле потоков).

        Операции:

        +----+----------------------------------------------------+
        | №  | Описание                                           |
        +====+====================================================+
        | 1  | Получение метаданных файла.                        |
        +----+----------------------------------------------------+
        | 2  | Поиск в ``_document_cache`` (если задан) или через |
        |    | ``_indexer.get_document_metadata``.                |
        |    | Возвращает 4-элементный кортеж                     |
        |    | ``(doc_id, file_hash, cached_size, cached_mtime)``.|
        |    | Если метаданные совпадают → ``SKIPPED_UNCHANGED``. |
        +----+----------------------------------------------------+
        | 3  | Вычисление хеша.                                   |
        +----+----------------------------------------------------+
        | 4  | Проверка дубликата по хешу через кэш/индексатор.   |
        |    | Если найден → ``DUPLICATE``.                       |
        +----+----------------------------------------------------+
        | 5  | Если есть ``doc_id`` по пути (переиндексация),     |
        |    | регистрируется хеш и возвращается ``TO_INDEX``     |
        |    | с существующим ``doc_id``.                         |
        +----+----------------------------------------------------+
        | 6  | Иначе создаётся новый ``doc_id`` и регистрируется  |
        |    | хеш, возвращается ``TO_INDEX``.                    |
        +----+----------------------------------------------------+
        | 7  | При ``Exception`` → ``ERROR``.                     |
        +----+----------------------------------------------------+
        """
        try:
            file_info = self._scanner.get_file_info(file_path)
            relative_path = self._scanner.get_relative_path(
                file_path,
                self._rd_directory,
            )

            # 1. Ленивое хеширование: поиск метаданных в кэше или БД.
            cached: tuple[str, str, int, str] | None
            if self._document_cache is not None:
                cached = self._document_cache.get_by_path(relative_path)
            else:
                cached = self._indexer.get_document_metadata(relative_path)

            cached_doc_id: str = ""
            cached_size: int = 0
            cached_mtime: str = ""
            if cached is not None:
                # file_hash из кэша не используется: он нужен только
                # для сравнения метаданных, а оно идёт по size и mtime.
                cached_doc_id, _cached_hash, cached_size, cached_mtime = cached

            current_size = file_info.get("file_size", 0)
            current_mtime = file_info.get("last_modified", "")

            if cached is not None and cached_size == current_size and cached_mtime == current_mtime:
                return HashResult(
                    outcome=HashOutcome.SKIPPED_UNCHANGED,
                    abs_file_path=file_path,
                    relative_path=relative_path,
                    file_size=current_size,
                    last_modified=current_mtime,
                )

            # 2. Вычисление хеша
            file_hash = self._hasher.compute_hash(file_path)

            # 3. Проверка дубликата по хешу
            existing_by_hash: str | None
            if self._document_cache is not None:
                existing_by_hash = self._document_cache.get_doc_id_by_hash(file_hash)
            else:
                existing_by_hash = self._indexer.get_document_by_hash(file_hash)

            if existing_by_hash is not None:
                return HashResult(
                    outcome=HashOutcome.DUPLICATE,
                    abs_file_path=file_path,
                    relative_path=relative_path,
                    file_hash=file_hash,
                    file_size=current_size,
                    last_modified=current_mtime,
                )

            # 4. Переиндексация по пути
            if cached_doc_id:
                if self._document_cache is not None:
                    self._document_cache.register_hash(file_hash, cached_doc_id)
                return HashResult(
                    outcome=HashOutcome.TO_INDEX,
                    abs_file_path=file_path,
                    relative_path=relative_path,
                    file_hash=file_hash,
                    file_size=current_size,
                    last_modified=current_mtime,
                    doc_id=cached_doc_id,
                )

            # 5. Новый файл
            new_doc_id = str(uuid.uuid4())
            if self._document_cache is not None:
                self._document_cache.register_hash(file_hash, new_doc_id)
            return HashResult(
                outcome=HashOutcome.TO_INDEX,
                abs_file_path=file_path,
                relative_path=relative_path,
                file_hash=file_hash,
                file_size=current_size,
                last_modified=current_mtime,
                doc_id=new_doc_id,
            )
        except Exception as e:  # noqa: BLE001
            return HashResult(
                outcome=HashOutcome.ERROR,
                abs_file_path=file_path,
                error_message=str(e),
            )

    # ------------------------------------------------------------------
    # Этап 3: Извлечение текста и батчевая запись
    # ------------------------------------------------------------------

    async def _extract_worker_pool(self, scan_id: int) -> None:
        """Этап 3: пул асинхронных воркеров для извлечения текста
        и батчевой записи.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Запуск воркеров в ``TaskGroup``.                    |
        +---+-----------------------------------------------------+
        """
        async with asyncio.TaskGroup() as tg:
            for _ in range(self._max_extract_workers):
                tg.create_task(self._extract_worker(scan_id))

    async def _extract_worker(self, scan_id: int) -> None:
        """Один воркер извлечения текста и батчевой записи.

        Операции:

        +----+----------------------------------------------------+
        | №  | Описание                                           |
        +====+====================================================+
        | 1  | Ожидание задачи через ``_wait_task_or_cancel``.    |
        +----+----------------------------------------------------+
        | 2  | Если ``None`` — отмена, выход из цикла.             |
        +----+----------------------------------------------------+
        | 3  | Если ``_QueueSentinel`` — завершение очереди.       |
        +----+----------------------------------------------------+
        | 4  | Проверка ``MAX_SCAN_ERRORS``.                      |
        +----+----------------------------------------------------+
        | 5  | Извлечение текста через ``run_with_timeout``       |
        |    | с дескриптором ``scan.extract``:                   |
        |    | a. ``ProcessPoolExecutor`` если доступен и не      |
        |    |    сломан.                                         |
        |    | b. Иначе — потоковый fallback.                     |
        +----+----------------------------------------------------+
        | 6  | При ``TimeoutError``:                              |
        |    | a. ``_tracker.record_stuck_process()``.            |
        |    | b. Если ``stuck >= _max_extract_workers`` →        |
        |    |    ``_executor_broken = True``.                    |
        |    | c. ``_tracker.record_extract_result(0, 1, ...)``.  |
        |    | d. ``task_done()`` и ``continue``.                 |
        +----+----------------------------------------------------+
        | 7  | При ``BrokenProcessPool`` → fallback на потоки.    |
        +----+----------------------------------------------------+
        | 8  | При успехе — добавление запросов в буфер.          |
        +----+----------------------------------------------------+
        | 9  | При ошибке — обновление счётчика и публикация.     |
        +----+----------------------------------------------------+
        | 10 | При заполнении буфера до ``SCAN_COMMIT_INTERVAL``  |
        |    | — вызов ``flush_buffer()``.                        |
        +----+----------------------------------------------------+
        | 11 | Троттлинг публикации прогресса.                    |
        +----+----------------------------------------------------+
        | 12 | При завершении (сентинел/отмена) — запись          |
        |    | остатка буфера только если ``_cancel_event``       |
        |    | не установлен.                                     |
        +----+----------------------------------------------------+
        """
        document_buffer: list[list[tuple[str, tuple]]] = []

        async def flush_buffer() -> None:
            """Записывает накопленный буфер документов."""
            if not document_buffer:
                return
            success, failed = await run_blocking_in_executor(
                self._scan_executor,
                self._indexer.write_documents_batch,
                document_buffer,
            )
            document_buffer.clear()
            self._tracker.record_extract_result(success, failed)

        while True:
            item = await self._wait_task_or_cancel(self._extract_queue)
            if item is None:
                # Кооперативная отмена.
                break
            if isinstance(item, _QueueSentinel):
                self._extract_queue.task_done()
                break

            task: _FileTask = item

            # Проверка MAX_SCAN_ERRORS
            if self._check_error_threshold():
                self._extract_queue.task_done()
                continue

            # Подготовка запросов (извлечение текста)
            # с таймаутом
            queries: list[tuple[str, tuple]] | None = None
            try:
                loop = asyncio.get_running_loop()
                if self._extract_executor is not None and not self._executor_broken:
                    queries = await run_with_timeout(
                        loop.run_in_executor(
                            self._extract_executor,
                            extract_document_queries,
                            task.abs_file_path,
                            task.doc_id,
                            task.relative_path,
                            task.file_hash,
                            task.file_size,
                            task.last_modified,
                        ),
                        operation="scan.extract",
                        event_bus=self._event_bus,
                        context=task.relative_path,
                        recovery_action="Файл помечен как ошибочный.",
                    )
                else:
                    queries = await run_with_timeout(
                        run_blocking_in_executor(
                            self._scan_executor,
                            self._indexer.prepare_document_queries,
                            task.doc_id,
                            task.relative_path,
                            task.file_hash,
                            task.file_size,
                            task.last_modified,
                            task.abs_file_path,
                        ),
                        operation="scan.extract",
                        event_bus=self._event_bus,
                        context=task.relative_path,
                        recovery_action="Файл помечен как ошибочный.",
                    )
            except TimeoutError:
                self._tracker.record_stuck_process()
                if self._tracker.get_stuck_processes_count() >= self._max_extract_workers:
                    self._executor_broken = True
                self._tracker.record_extract_result(0, 1, task.relative_path)
                self._extract_queue.task_done()
                continue
            except BrokenProcessPool:
                self._executor_broken = True
                try:
                    queries = await run_blocking_in_executor(
                        self._scan_executor,
                        self._indexer.prepare_document_queries,
                        task.doc_id,
                        task.relative_path,
                        task.file_hash,
                        task.file_size,
                        task.last_modified,
                        task.abs_file_path,
                    )
                except Exception:  # noqa: BLE001
                    queries = None
            except Exception:  # noqa: BLE001
                queries = None

            if queries is not None:
                document_buffer.append(queries)
            else:
                # Ошибка извлечения текста
                self._tracker.record_extract_result(0, 1, task.relative_path)
                self._event_bus.publish(
                    FileProcessingFailed(
                        correlation_id=self._correlation_id,
                        source="ScanPipeline",
                        stage="indexing",
                        scan_id=scan_id,
                        file_path=task.relative_path,
                        error_type="TextExtractionError",
                        error_message="Не удалось подготовить запросы документа",
                        traceback="",
                    )
                )

            # Батчевая запись при достижении порога
            if len(document_buffer) >= config.SCAN_COMMIT_INTERVAL:
                await flush_buffer()

            # Троттлинг публикации прогресса
            self._tracker.publish_throttled(scan_id, "indexing")

            self._extract_queue.task_done()

        # Записать остаток буфера при завершении воркера,
        # только если отмена не запрошена. При сбое потери
        # логируются через _log_buffer_loss.
        if document_buffer and not (self._cancel_event is not None and self._cancel_event.is_set()):
            try:
                await asyncio.wait_for(
                    flush_buffer(),
                    timeout=config.OPERATION_TIMEOUTS["scan.db_write_batch"],
                )
            except TimeoutError:
                self._log_buffer_loss(len(document_buffer), "timeout")
                document_buffer.clear()
            except asyncio.CancelledError:
                self._log_buffer_loss(len(document_buffer), "cancelled")
                document_buffer.clear()
                raise
            except (RuntimeError, sqlite3.Error) as e:
                self._log_buffer_loss(
                    len(document_buffer),
                    f"{type(e).__name__}: {e}",
                )
                document_buffer.clear()
