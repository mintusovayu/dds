"""
Оркестратор сканирования DDS.

Этот модуль координирует процесс сканирования каталога рабочей
документации (РД) и управляет поэтапным сканированием.

Фазы сканирования:

Фаза 1 (primary): индексирование текстового слоя.
- Сканирование каталога РД.
- Ленивое хеширование файлов для контроля дубликатов.
- Формирование доменных планов индексации (``DocumentIndexPlan``)
  через ``ITextExtractor`` в subprocess.
- Запись планов в таблицы documents и text_index_fts через
  ``IIndexWriter``.
После завершения Фазы 1 поиск становится доступен.

Фаза 2 (secondary): запуск модулей и обновление метаданных.
- Сначала выполняется обновление метаданных документов на основе
  парсинга имён файлов (если сервис обновления метаданных доступен).
- Затем передача списка проиндексированных документов модулям.
- Модули извлекают дополнительные данные (штампы, таблицы).
Фаза 2 не блокирует поиск.

Параллельная обработка (Фаза 3 оптимизации):
Первичное сканирование выполняется через параллельный конвейер
``ScanPipeline``, который обеспечивает:
- Параллельное ленивое хеширование файлов.
- Параллельное формирование планов индексации.
- Потокобезопасную запись в БД.
- Корректное обновление прогресса в реальном времени.
- Корректную отмену сканирования.

Асинхронная модель (Фаза 4):
Конвейер ``ScanPipeline`` является асинхронной корутиной и
запускается в основном event loop через ``asyncio.create_task()``.
Метод ``run_primary_scan`` является асинхронным (``async def``).
Блокирующие операции (создание и завершение записи в
``scan_state``) выполняются через выделенный пул потоков
``scan_executor`` для предотвращения блокировки event loop.

Выделенный пул потоков (скорректированный план):
Все блокирующие операции оркестратора и конвейера выполняются
через выделенный пул потоков ``scan_executor``, переданный через
конструктор. Это предотвращает конкуренцию с веб-запросами за
потоки дефолтного пула.

Формирование планов индексации через subprocess (Фаза 4/5):
Оркестратор передаёт в ``ScanPipeline`` исполнитель
subprocess-задач (``process_runner``, реализация
``IProcessTaskRunner``) и picklable worker-функцию
(``index_plan_worker`` — ``build_index_plan_in_subprocess`` из
composition root). Формирование плана индексации одного PDF
выполняется в изолированном процессе (``forkserver`` на Linux),
что даёт устойчивость к сегфолтам PyMuPDF и изоляцию состояния
между задачами. Конкретные реализации передаются из composition
root (``dds_web/lifespan.py``); сам оркестратор не импортирует
``infrastructure.process_task_runner`` (сохраняет контракт
``application-isolation``) и не импортирует ``subprocess_tasks``
(сохраняет контракт ``subprocess-tasks-isolation``). Worker-функция
передаётся как ``Callable`` — без знания о её модуле.

``process_runner`` и ``index_plan_worker`` — обязательные
параметры конструктора. Потоковый fallback (через
``indexer.prepare_document_queries``) удалён в Фазе 5: в production
оба всегда передаются из composition root.

Ленивое хеширование (Фаза 2 оптимизации):
При повторном сканировании каталога РД метаданные файла
(размер и дата последнего изменения) сравниваются с сохранёнными
в таблице ``documents`` значениями полей ``cached_size`` и
``cached_mtime``. Если метаданные не изменились, хеширование
файла пропускается, что значительно ускоряет повторное
сканирование.

Кэширование состояния индексации (скорректированный план):
Тяжёлый метод определения состояния индексации
(``_calculate_index_status``) кэшируется. Публичные методы
``get_cached_index_status`` и ``refresh_index_status``
обеспечивают доступ к кэшу без выполнения тяжёлых операций
при каждом запросе.

Точный учёт файлов и дубликатов (Исправление UI-бага):
Метод ``_calculate_index_status`` теперь явно вычисляет
количество дубликатов на диске (``duplicate_files_on_disk``).
Если файл отсутствует в БД по пути, но его хеш совпадает с
хешем уже проиндексированного файла, он учитывается как
дубликат, а не как новый файл. Это обеспечивает математический
баланс: ``Total = Indexed + Duplicates + New + Modified``.

Событийная модель (Фаза 5):
Оркестратор публикует события в шину событий:
- ``ScanStarted`` — при запуске первичного сканирования.
- ``ScanCancelRequested`` — при запросе отмены сканирования.
- ``ScanCancelled`` — при фактическом завершении сканирования с
  статусом ``INTERRUPTED`` (как через ``CancelledError``, так и
  через кооперативный флаг).
- ``ScanCompleted`` — при завершении (успех, ошибка, отмена).
- ``ScanSecondaryStarted`` — при начале вторичного сканирования.
- ``ScanSecondaryProgressUpdated`` — при прогрессе вторичного сканирования.
- ``ScanSecondaryCompleted`` — при завершении вторичного сканирования.

События ``ScanCompleted`` и ``ScanProgressUpdated`` теперь
передают детальную разбивку прогресса (indexed, duplicate,
skipped, error). События вторичного сканирования обеспечивают
индикацию его этапов для веб-интерфейса.

Принципы:
- Модуль находится в application layer и не содержит бизнес-логики.
- Модуль зависит от domain/interfaces через dependency injection.
- Модуль не выполняет логирование, но публикует события.
- Состояние индексации определяется по фактическим данным в БД
  и файловой системе, а не по таблице scan_state.
- Прогресс сканирования обновляется в памяти конвейера.
  Запись в БД выполняется только при создании и завершении
  записи в ``scan_state``.
- Параллельная обработка выполняется через ``ScanPipeline``,
  который инкапсулирует логику конвейера.

Классы:
ScanOrchestrator — оркестратор сканирования.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Executor
from datetime import UTC, datetime
from typing import Any

from ..domain import config
from ..domain.events import (
    ScanCancelled,
    ScanCancelRequested,
    ScanCompleted,
    ScanSecondaryCompleted,
    ScanSecondaryProgressUpdated,
    ScanSecondaryStarted,
    ScanStarted,
)
from ..domain.interfaces import (
    IDatabase,
    IDocumentCache,
    IEventBus,
    IHasher,
    IModuleLifecycle,
    IProcessTaskRunner,
    IScanner,
    ITextExtractor,
    ITextIndexer,
)
from ..domain.models import (
    DocumentInfo,
    IndexState,
    IndexStatus,
    ScanPhase,
    ScanProgress,
    ScanStatus,
)
from .async_utils import run_blocking_in_executor
from .document_metadata_service import DocumentMetadataService
from .scan_pipeline import ScanPipeline

# ----------------------------------------------------------------------
# Оркестратор сканирования
# ----------------------------------------------------------------------


class ScanOrchestrator:
    """
    Оркестратор сканирования каталога РД.

    Координирует процесс сканирования, управляет поэтапным
    сканированием и предоставляет информацию о состоянии индексации.

    Первичное сканирование выполняется через параллельный конвейер
    ``ScanPipeline`` (Фаза 3 оптимизации). Конвейер обеспечивает
    параллельное хеширование и формирование планов индексации,
    что значительно ускоряет обработку больших каталогов РД.

    Асинхронная модель (Фаза 4):
    Метод ``run_primary_scan`` является асинхронной корутиной.
    Конвейер ``ScanPipeline`` запускается в основном event loop
    через ``await pipeline.run_async()``. Блокирующие операции
    (создание и завершение записи в ``scan_state``) выполняются
    через выделенный пул потоков ``scan_executor``.

    Формирование планов индексации через subprocess (Фаза 4/5):
    Оркестратор принимает ``process_runner`` (реализация
    ``IProcessTaskRunner``) и ``index_plan_worker`` (picklable
    функция формирования плана индексации одного PDF, реализуемая
    ``build_index_plan_in_subprocess``). Оба передаются в
    ``ScanPipeline`` при создании конвейера. Оба — обязательные
    параметры: потоковый fallback удалён в Фазе 5. Конкретные
    реализации передаются через DI из composition root
    (``dds_web/lifespan.py``); сам оркестратор не импортирует
    infrastructure и subprocess_tasks.

    Кэширование состояния индексации (скорректированный план):
    Тяжёлый метод ``_calculate_index_status`` кэшируется.
    Публичный метод ``get_cached_index_status`` возвращает кэш
    без выполнения тяжёлых операций. Метод ``refresh_index_status``
    выполняет принудительный пересчёт с ограничением частоты.

    Живой прогресс сканирования (скорректированный план):
    Метод ``get_scan_status`` возвращает живой прогресс из
    ``_current_pipeline``, если сканирование активно. Иначе
    читает последнюю запись из ``scan_state``.

    Сохранение детальной статистики в памяти:
    Поскольку схема БД не расширяется для хранения детальной
    разбивки (indexed, duplicate, skipped, error), оркестратор
    сохраняет итоговый ``ScanProgress`` в поле ``_last_scan_progress``.
    Это позволяет UI получать полную статистику последнего сканирования
    даже после его завершения, когда конвейер уже уничтожен.

    Событийная модель (Фаза 5):
    Оркестратор использует шину событий для публикации событий
    начала, отмены и завершения сканирования, а также вторичного
    сканирования. Correlation ID, переданный из presentation layer,
    связывает все события одного сканирования.

    Обновление метаданных:
    Оркестратор может принимать сервис обновления метаданных
    документов (``DocumentMetadataService``). При наличии этого
    сервиса вторичное сканирование начинается с вызова
    ``update_all_documents()``, который заполняет поля фильтрации
    на основе парсинга имён файлов. Также доступен метод
    ``refresh_document_metadata()`` для повторного запуска
    обновления по требованию (например, после изменения справочников).

    Восстановление ``rd_directory`` в событии вторичного
    сканирования (скорректированный план, шаг 4.3 рефакторинга v5.0):

    Ранее в ``run_secondary_scans`` при публикации события
    ``ScanSecondaryStarted`` передавалось ``rd_directory=""``
    (пустая строка). Это не соответствовало контракту события —
    подписчики (в частности, веб-интерфейс и SSE) получали пустой
    путь к каталогу РД, что затрудняло диагностику и индикацию.
    Теперь передаётся актуальное значение ``self._rd_directory``,
    сохранённое в ``run_primary_scan``. Поле ``_rd_directory``
    инициализируется пустой строкой в ``__init__`` и перезаписывается
    при старте первичного сканирования; вторичное сканирование
    всегда следует за первичным (см. ``dds_web/api.py::_scan_coroutine``),
    поэтому к моменту публикации события значение актуально.

    Пример использования::

        orchestrator = ScanOrchestrator(
            db=db_adapter,
            scanner=directory_scanner,
            hasher=file_hasher,
            text_extractor=pymupdf_extractor,
            indexer=text_indexer,
            module_lifecycle=module_lifecycle,
            event_bus=event_bus,
            process_runner=process_runner,
            index_plan_worker=build_index_plan_in_subprocess,
            scan_executor=scan_executor,
            document_cache=document_cache,
            document_metadata_service=document_metadata_service,
        )

        # Первичное сканирование (асинхронное, в основном event loop):
        task = asyncio.create_task(
            orchestrator.run_primary_scan(
                "/path/to/rd",
                correlation_id="scan-123",
            )
        )

        # Вторичное сканирование (синхронное), включает обновление метаданных:
        results = orchestrator.run_secondary_scans()

        # Повторное обновление метаданных вручную:
        orchestrator.refresh_document_metadata()

        # Кэшированное состояние индексации:
        index_status = orchestrator.get_cached_index_status("/path/to/rd")

        # Принудительный пересчёт:
        index_status = orchestrator.refresh_index_status("/path/to/rd")

        # Диагностика пула соединений:
        pool_stats = orchestrator.get_pool_stats()

        # Удаление документа:
        orchestrator.remove_document("doc_001")

    Attributes:

    +-----------------------------+-------------------------------------------+
    | Атрибут                     | Описание                                  |
    +=============================+===========================================+
    | ``_db``                     | Абстракция базы данных.                   |
    +-----------------------------+-------------------------------------------+
    | ``_scanner``                | Абстракция сканера каталога.              |
    +-----------------------------+-------------------------------------------+
    | ``_hasher``                 | Абстракция хешера файлов.                 |
    +-----------------------------+-------------------------------------------+
    | ``_text_extractor``         | Абстракция извлекателя текста.            |
    +-----------------------------+-------------------------------------------+
    | ``_indexer``                | Абстракция индексатора текстового слоя.   |
    +-----------------------------+-------------------------------------------+
    | ``_module_lifecycle``       | Абстракция менеджера жизненного цикла     |
    |                             | модулей.                                  |
    +-----------------------------+-------------------------------------------+
    | ``_event_bus``              | Шина событий для публикации.              |
    +-----------------------------+-------------------------------------------+
    | ``_scan_executor``          | Выделенный пул потоков для блокирующих    |
    |                             | операций сканирования.                    |
    +-----------------------------+-------------------------------------------+
    | ``_document_cache``         | Кэш метаданных документов или ``None``.   |
    +-----------------------------+-------------------------------------------+
    | ``_process_runner``         | Исполнитель subprocess-задач              |
    |                             | (``IProcessTaskRunner``). Обязателен.     |
    +-----------------------------+-------------------------------------------+
    | ``_index_plan_worker``      | Picklable-функция формирования плана      |
    |                             | индексации одного PDF. Обязательна.       |
    +-----------------------------+-------------------------------------------+
    | ``_document_metadata_service`` | Сервис обновления метаданных           |
    |                             | документов (может быть ``None``).         |
    +-----------------------------+-------------------------------------------+
    | ``_cancel_requested``       | Флаг запроса отмены сканирования.         |
    +-----------------------------+-------------------------------------------+
    | ``_current_pipeline``       | Текущий конвейер сканирования.            |
    |                             | Используется для отмены сканирования      |
    |                             | и получения живого прогресса.              |
    |                             | ``None``, если сканирование не            |
    |                             | выполняется.                              |
    +-----------------------------+-------------------------------------------+
    | ``_current_scan_id``        | Идентификатор текущего сканирования.      |
    |                             | Используется для публикации событий       |
    |                             | отмены.                                   |
    +-----------------------------+-------------------------------------------+
    | ``_current_correlation_id`` | Correlation ID текущего сканирования.     |
    |                             | Используется для публикации событий       |
    |                             | отмены.                                   |
    +-----------------------------+-------------------------------------------+
    | ``_last_scan_progress``     | Кэш итогового прогресса последнего        |
    |                             | сканирования. Сохраняет детальную         |
    |                             | разбивку (indexed, duplicate, etc.),      |
    |                             | которая не хранится в БД.                 |
    +-----------------------------+-------------------------------------------+
    | ``_index_status_cache``     | Кэшированный результат состояния          |
    |                             | индексации. ``None``, если кэш пуст.      |
    +-----------------------------+-------------------------------------------+
    | ``_index_status_at``        | Время последнего расчёта состояния        |
    |                             | индексации (``time.monotonic()``).        |
    +-----------------------------+-------------------------------------------+
    | ``_index_status_lock``      | Блокировка для потокобезопасного доступа  |
    |                             | к кэшу состояния индексации.              |
    +-----------------------------+-------------------------------------------+
    """

    def __init__(
        self,
        db: IDatabase,
        scanner: IScanner,
        hasher: IHasher,
        text_extractor: ITextExtractor,
        indexer: ITextIndexer,
        module_lifecycle: IModuleLifecycle,
        event_bus: IEventBus,
        process_runner: IProcessTaskRunner,
        index_plan_worker: Callable[..., Any],
        scan_executor: Executor | None = None,
        document_cache: IDocumentCache | None = None,
        document_metadata_service: DocumentMetadataService | None = None,
    ) -> None:
        """
        Инициализирует оркестратор сканирования.

        Параметры:

        +----------------------+--------------------------------------+
        | Параметр             | Описание                             |
        +======================+======================================+
        | ``db``               | Реализация ``IDatabase`` для доступа |
        |                      | к БД.                                |
        +----------------------+--------------------------------------+
        | ``scanner``          | Реализация ``IScanner`` для          |
        |                      | сканирования каталога.               |
        +----------------------+--------------------------------------+
        | ``hasher``           | Реализация ``IHasher`` для           |
        |                      | хеширования файлов.                  |
        +----------------------+--------------------------------------+
        | ``text_extractor``   | Реализация ``ITextExtractor`` для    |
        |                      | извлечения текста.                   |
        +----------------------+--------------------------------------+
        | ``indexer``          | Реализация ``ITextIndexer`` для      |
        |                      | подготовки и записи индекса.         |
        +----------------------+--------------------------------------+
        | ``module_lifecycle`` | Реализация ``IModuleLifecycle`` для  |
        |                      | управления модулями.                 |
        +----------------------+--------------------------------------+
        | ``event_bus``        | Шина событий для публикации событий. |
        +----------------------+--------------------------------------+
        | ``process_runner``   | Исполнитель subprocess-задач         |
        |                      | (``IProcessTaskRunner``).            |
        |                      | Обязателен; передаётся в             |
        |                      | ``ScanPipeline``.                    |
        +----------------------+--------------------------------------+
        | ``index_plan_worker``| Picklable-функция формирования плана |
        |                      | индексации одного PDF. Передаётся    |
        |                      | как ``Callable`` без импорта         |
        |                      | ``subprocess_tasks`` в application.  |
        |                      | Обязательна.                         |
        +----------------------+--------------------------------------+
        | ``scan_executor``    | Выделенный пул потоков для           |
        |                      | блокирующих операций сканирования.   |
        |                      | Если ``None``, используется          |
        |                      | дефолтный пул потоков.               |
        +----------------------+--------------------------------------+
        | ``document_cache``   | Кэш метаданных документов. Если      |
        |                      | ``None``, используется прямое        |
        |                      | обращение к БД через ``ITextIndexer``.|
        +----------------------+--------------------------------------+
        | ``document_metadata_service`` | Сервис обновления метаданных. |
        |                      | Если ``None``, обновление метаданных |
        |                      | не выполняется.                      |
        +----------------------+--------------------------------------+
        """
        self._db = db
        self._scanner = scanner
        self._hasher = hasher
        self._text_extractor = text_extractor
        self._indexer = indexer
        self._module_lifecycle = module_lifecycle
        self._event_bus = event_bus
        self._scan_executor = scan_executor
        self._document_cache = document_cache
        self._document_metadata_service = document_metadata_service
        self._process_runner = process_runner
        self._index_plan_worker = index_plan_worker

        self._cancel_requested = False
        self._current_pipeline: ScanPipeline | None = None
        self._current_scan_id: int = 0
        self._current_correlation_id: str = ""

        # Кэш детальной статистики последнего сканирования
        self._last_scan_progress: ScanProgress | None = None

        # Кэш состояния индексации
        self._index_status_cache: IndexStatus | None = None
        self._index_status_at: float = 0.0
        self._index_status_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Первичное сканирование
    # ------------------------------------------------------------------

    async def run_primary_scan(
        self,
        rd_directory: str,
        correlation_id: str | None = None,
    ) -> ScanProgress:
        """
        Выполняет первичное сканирование каталога РД.

        Асинхронная корутина. Запускается в основном event loop
        через ``asyncio.create_task()``. Конвейер ``ScanPipeline``
        выполняется через ``await pipeline.run_async()`` в том же
        event loop, что и веб-обработчики.

        Публикует события:
        - ``ScanStarted`` после создания записи в ``scan_state``.
        - ``ScanCompleted`` после завершения записи в ``scan_state``.
        - ``ScanCancelled`` при завершении со статусом ``INTERRUPTED``.

        Сохранение детальной статистики:
        По завершении конвейера (успех, ошибка или отмена), итоговый
        ``ScanProgress`` (содержащий разбивку indexed/duplicate/
        skipped/error) сохраняется в ``_last_scan_progress``. Это
        позволяет API возвращать полную статистику даже после
        завершения сканирования, когда конвейер уже не существует.

        Correlation ID:
        Если ``correlation_id`` не передан, генерируется новый UUID.
        Он связывает все события данного сканирования (включая
        события, публикуемые конвейером).

        Обработка отмены (скорректированный план):
        При получении ``CancelledError`` или кооперативном завершении
        со статусом ``INTERRUPTED`` оркестратор:
        1. Формирует прогресс со статусом ``INTERRUPTED``.
        2. Завершает запись в ``scan_state``.
        3. Публикует ``ScanCompleted``.
        4. Публикует ``ScanCancelled``.
        5. Пробрасывает ``CancelledError`` дальше (если это был
           ``CancelledError``).

        Проброс ``CancelledError`` предотвращает продолжение
        вторичного сканирования после отмены. Вызывающий код
        (``_scan_coroutine`` в ``api.py``) получает отмену и
        не запускает ``run_secondary_scans``.

        Операции:

        +----+----------------------------------------------------+
        | №  | Описание                                           |
        +====+====================================================+
        | 1  | Сброс ``_cancel_requested = False``.               |
        +----+----------------------------------------------------+
        | 2  | Генерация ``correlation_id``, если не передан.     |
        |    | Сохранение в ``_current_correlation_id``.          |
        +----+----------------------------------------------------+
        | 3  | ``await run_blocking_in_executor(                  |
        |    | self._scan_executor, _create_scan_record)``        |
        |    | → создание записи в ``scan_state``. Блокирующая    |
        |    | операция записи в БД выполняется в выделенном      |
        |    | пуле потоков.                                      |
        +----+----------------------------------------------------+
        | 4  | Сохранение ``scan_id`` в ``_current_scan_id``.     |
        +----+----------------------------------------------------+
        | 5  | Публикация ``ScanStarted`` через шину событий.     |
        +----+----------------------------------------------------+
        | 6  | Создание ``ScanPipeline`` с параметрами из         |
        |    | ``config`` и передачей ``event_bus``,              |
        |    | ``scan_executor``, ``document_cache``,             |
        |    | ``process_runner``, ``index_plan_worker``.         |
        +----+----------------------------------------------------+
        | 7  | Сохранение ссылки на конвейер в                    |
        |    | ``_current_pipeline`` для возможности отмены       |
        |    | и получения живого прогресса.                       |
        +----+----------------------------------------------------+
        | 8  | ``await pipeline.run_async(rd_directory, scan_id,  |
        |    | correlation_id=correlation_id)``                   |
        |    | → запуск конвейера в основном event loop.          |
        +----+----------------------------------------------------+
        | 9  | При ``asyncio.CancelledError`` → прогресс со       |
        |    | статусом ``INTERRUPTED``, завершение записи,       |
        |    | публикация ``ScanCompleted`` и ``ScanCancelled``,  |
        |    | проброс ``CancelledError``.                        |
        +----+----------------------------------------------------+
        | 10 | При ``Exception`` → прогресс со статусом           |
        |    | ``ERROR``, завершение записи, публикация события.  |
        +----+----------------------------------------------------+
        | 11 | При нормальном завершении: если статус             |
        |    | ``INTERRUPTED``, публикация ``ScanCancelled``.     |
        +----+----------------------------------------------------+
        | 12 | Сохранение ``progress`` в ``_last_scan_progress``. |
        +----+----------------------------------------------------+
        | 13 | ``finally``: очистка ``_current_pipeline = None``. |
        +----+----------------------------------------------------+
        | 14 | Возврат ``ScanProgress``.                          |
        +----+----------------------------------------------------+

        Args:
            rd_directory: Путь к каталогу рабочей документации.
            correlation_id: Идентификатор корреляции. Если ``None``,
                генерируется новый UUID. Должен передаваться из
                presentation layer для связывания всех событий
                одного HTTP-запроса или задачи.

        Returns:
            ``ScanProgress`` с результатом сканирования.

        Raises:
            asyncio.CancelledError: Если сканирование было отменено.
                Пробрасывается после завершения записи в ``scan_state``
                и публикации событий ``ScanCompleted`` и ``ScanCancelled``.
        """
        self._cancel_requested = False
        self._rd_directory = rd_directory

        # Correlation ID для связывания событий
        if correlation_id is None:
            correlation_id = str(uuid.uuid4())
        self._current_correlation_id = correlation_id

        # Создать запись о сканировании.
        scan_id = await run_blocking_in_executor(
            self._scan_executor,
            self._create_scan_record,
            ScanPhase.PRIMARY,
        )
        self._current_scan_id = scan_id

        # Публикация события начала сканирования
        self._event_bus.publish(
            ScanStarted(
                correlation_id=correlation_id,
                source="ScanOrchestrator",
                stage="primary_scan",
                scan_id=scan_id,
                phase=ScanPhase.PRIMARY.value,
                rd_directory=rd_directory,
            )
        )

        # Создать параллельный конвейер сканирования.
        # process_runner и index_plan_worker обязательны: потоковый
        # fallback удалён в Фазе 5 (ADR-005).
        pipeline = ScanPipeline(
            db=self._db,
            scanner=self._scanner,
            hasher=self._hasher,
            text_extractor=self._text_extractor,
            indexer=self._indexer,
            event_bus=self._event_bus,
            process_runner=self._process_runner,
            index_plan_worker=self._index_plan_worker,
            max_hash_workers=config.SCAN_HASH_WORKERS,
            max_extract_workers=config.SCAN_EXTRACT_WORKERS,
            scan_executor=self._scan_executor,
            document_cache=self._document_cache,
        )
        self._current_pipeline = pipeline

        try:
            progress = await pipeline.run_async(
                rd_directory=rd_directory,
                scan_id=scan_id,
                correlation_id=correlation_id,
            )
        except asyncio.CancelledError:
            progress = ScanProgress(
                phase=ScanPhase.PRIMARY,
                status=ScanStatus.INTERRUPTED,
            )
            # Завершить запись о сканировании.
            await run_blocking_in_executor(
                self._scan_executor,
                self._complete_scan_record,
                scan_id,
                progress,
            )
            # Публикация события завершения сканирования
            self._publish_scan_completed(correlation_id, scan_id, progress)
            # Публикация события отмены сканирования
            self._event_bus.publish(
                ScanCancelled(
                    correlation_id=correlation_id,
                    source="ScanOrchestrator",
                    stage="primary_scan",
                    scan_id=scan_id,
                )
            )
            # Сохранить прогресс в памяти перед пробросом
            self._last_scan_progress = progress
            # Пробросить CancelledError для предотвращения
            # продолжения вторичного сканирования.
            raise
        except Exception:
            progress = ScanProgress(
                phase=ScanPhase.PRIMARY,
                status=ScanStatus.ERROR,
            )
            # Завершить запись о сканировании.
            await run_blocking_in_executor(
                self._scan_executor,
                self._complete_scan_record,
                scan_id,
                progress,
            )
            # Публикация события завершения сканирования
            self._publish_scan_completed(correlation_id, scan_id, progress)
            # Сохранить прогресс в памяти
            self._last_scan_progress = progress
            return progress
        finally:
            self._current_pipeline = None

        # Успешное завершение (или завершение с внутренними ошибками конвейера)
        # Завершить запись о сканировании.
        await run_blocking_in_executor(
            self._scan_executor,
            self._complete_scan_record,
            scan_id,
            progress,
        )
        # Публикация события завершения сканирования
        self._publish_scan_completed(correlation_id, scan_id, progress)

        # Публикация события отмены, если сканирование прервано
        # кооперативно (через флаг _cancel_requested)
        if progress.status == ScanStatus.INTERRUPTED:
            self._event_bus.publish(
                ScanCancelled(
                    correlation_id=correlation_id,
                    source="ScanOrchestrator",
                    stage="primary_scan",
                    scan_id=scan_id,
                )
            )

        # Сохранение детальной статистики в памяти
        self._last_scan_progress = progress
        return progress

    def _publish_scan_completed(
        self, correlation_id: str, scan_id: int, progress: ScanProgress
    ) -> None:
        """Публикует событие ScanCompleted с детальной разбивкой.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Формирование и публикация события ``ScanCompleted`` |
        |   | с передачей всех новых счётчиков из ``progress``.   |
        +---+-----------------------------------------------------+

        Args:
            correlation_id: Идентификатор корреляции.
            scan_id: Идентификатор записи сканирования.
            progress: Итоговый прогресс сканирования.
        """
        self._event_bus.publish(
            ScanCompleted(
                correlation_id=correlation_id,
                source="ScanOrchestrator",
                stage="primary_scan",
                scan_id=scan_id,
                phase=ScanPhase.PRIMARY.value,
                status=progress.status.value,
                processed_files=progress.processed_files,
                total_files=progress.total_files,
                indexed_files=progress.indexed_files,
                duplicate_files=progress.duplicate_files,
                skipped_files=progress.skipped_files,
                error_files=progress.error_files,
            )
        )

    # ------------------------------------------------------------------
    # Вторичное сканирование
    # ------------------------------------------------------------------

    def run_secondary_scans(self) -> dict[str, int]:
        """
        Запускает вторичное сканирование: обновление метаданных и модули.

        Вторичное сканирование начинается с обновления метаданных
        документов (заполнение полей фильтрации на основе парсинга
        имён файлов), если сервис обновления метаданных доступен.
        Затем передаёт список проиндексированных документов модулям
        для дополнительной обработки.

        В процессе выполнения публикуются события:
        - ``ScanSecondaryStarted`` в начале.
        - ``ScanSecondaryProgressUpdated`` при обновлении метаданных
          и прогрессе обработки модулей.
        - ``ScanSecondaryCompleted`` по завершении.

        Восстановление ``rd_directory`` (скорректированный план,
        шаг 4.3 рефакторинга v5.0):
        Ранее в событие ``ScanSecondaryStarted`` передавалось
        ``rd_directory=""`` (пустая строка), что не соответствовало
        контракту события. Теперь передаётся актуальное значение
        ``self._rd_directory``, сохранённое в ``run_primary_scan``.

        Примечание:
        Метод ``run_secondary_scans`` НЕ создаёт запись
        в ``scan_state``. Управление состоянием вторичного
        сканирования является ответственностью
        ``ModuleLifecycle``, а не ``ScanOrchestrator``.

        Примечание (Фаза 4):
        Метод является синхронным. При вызове из асинхронного
        контекста должен вызываться через ``run_in_executor``
        с использованием ``scan_executor`` для предотвращения
        блокировки event loop.

        Returns:
            Словарь: имя модуля -> количество обработанных
            документов.
        """
        # 1. Получение списка документов
        documents = self._get_indexed_documents()
        total_documents = len(documents)

        # 2. Публикация события начала вторичного сканирования.
        #    rd_directory берётся из состояния оркестратора —
        #    значение установлено в run_primary_scan.
        self._event_bus.publish(
            ScanSecondaryStarted(
                correlation_id=self._current_correlation_id,
                source="ScanOrchestrator",
                stage="secondary_scan",
                scan_id=self._current_scan_id,
                rd_directory=self._rd_directory or "",
            )
        )

        # 3. Публикация начального прогресса (этап обновления метаданных)
        self._event_bus.publish(
            ScanSecondaryProgressUpdated(
                correlation_id=self._current_correlation_id,
                source="ScanOrchestrator",
                stage="metadata_update",
                scan_id=self._current_scan_id,
                processed_documents=0,
                total_documents=total_documents,
                module_name="",
                progress_percent=0.0,
            )
        )

        # 4. Обновление метаданных документов
        processed_metadata = self.refresh_document_metadata()
        if processed_metadata is None:
            processed_metadata = 0

        # 5. Публикация завершения этапа обновления метаданных
        self._event_bus.publish(
            ScanSecondaryProgressUpdated(
                correlation_id=self._current_correlation_id,
                source="ScanOrchestrator",
                stage="metadata_update",
                scan_id=self._current_scan_id,
                processed_documents=processed_metadata,
                total_documents=total_documents,
                module_name="",
                progress_percent=(
                    (processed_metadata / total_documents * 100) if total_documents > 0 else 0.0
                ),
            )
        )

        # 6. Обработка модулями
        if not documents:
            self._event_bus.publish(
                ScanSecondaryCompleted(
                    correlation_id=self._current_correlation_id,
                    source="ScanOrchestrator",
                    stage="secondary_scan",
                    scan_id=self._current_scan_id,
                    status="completed",
                    processed_documents=0,
                    total_documents=0,
                    errors=0,
                )
            )
            return {}

        # Callback для публикации прогресса по модулям.
        # `current_doc_id` не используется — сигнатура обязательна
        # по контракту `IModuleLifecycle.run_secondary_scans(on_progress=...)`.
        def on_module_progress(module_name, processed, total, current_doc_id):  # noqa: ARG001
            self._event_bus.publish(
                ScanSecondaryProgressUpdated(
                    correlation_id=self._current_correlation_id,
                    source="ScanOrchestrator",
                    stage="modules",
                    scan_id=self._current_scan_id,
                    processed_documents=processed,
                    total_documents=total,
                    module_name=module_name,
                    progress_percent=((processed / total * 100) if total > 0 else 0.0),
                )
            )

        module_results = self._module_lifecycle.run_secondary_scans(
            documents,
            on_progress=on_module_progress,
        )

        # 7. Публикация завершения вторичного сканирования
        self._event_bus.publish(
            ScanSecondaryCompleted(
                correlation_id=self._current_correlation_id,
                source="ScanOrchestrator",
                stage="secondary_scan",
                scan_id=self._current_scan_id,
                status="completed",
                processed_documents=total_documents,
                total_documents=total_documents,
                errors=0,
            )
        )

        return module_results

    def refresh_document_metadata(self) -> int | None:
        """Обновляет метаданные документов на основе парсинга имён.

        Вызывает ``update_all_documents()`` сервиса
        ``DocumentMetadataService``, если он был передан в
        конструктор. Если сервис не задан, метод ничего не делает
        и возвращает ``None``.

        Метод синхронный и выполняет блокирующие операции (чтение
        всех документов и обновление записей). При вызове из
        асинхронного контекста должен выполняться через
        ``run_in_executor`` с ``scan_executor``.

        Returns:
            Количество обновлённых документов или ``None``,
            если сервис недоступен.
        """
        if self._document_metadata_service is None:
            return None
        return self._document_metadata_service.update_all_documents()

    def _get_indexed_documents(self) -> list[DocumentInfo]:
        """
        Возвращает список проиндексированных документов.

        Примечание (Фаза 4):
        Метод является синхронным и выполняет блокирующую
        операцию чтения из БД. При вызове из асинхронного
        контекста должен вызываться через ``run_in_executor``.

        Returns:
            Список ``DocumentInfo`` для всех документов в таблице
            ``documents``.
        """
        results = self._db.execute(
            "SELECT doc_id, file_path, file_hash, file_size, "
            "page_count, indexed_at, last_modified FROM documents"
        )
        documents: list[DocumentInfo] = []
        for row in results:
            documents.append(
                DocumentInfo(
                    doc_id=str(row[0]),
                    file_path=str(row[1]),
                    file_hash=str(row[2]),
                    file_size=int(row[3]),
                    page_count=int(row[4]),
                    indexed_at=str(row[5]),
                    last_modified=str(row[6]),
                )
            )
        return documents

    # ------------------------------------------------------------------
    # Управление сканированием
    # ------------------------------------------------------------------

    def cancel_scan(self) -> None:
        """
        Запрашивает отмену текущего сканирования.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | ``_cancel_requested = True``.                       |
        +---+-----------------------------------------------------+
        | 2 | Если ``_current_pipeline is not None``:             |
        |   | a. Публикация ``ScanCancelRequested`` с текущим     |
        |   |    ``scan_id`` и ``correlation_id``.                |
        |   | b. ``_current_pipeline.cancel()``.                  |
        |   | Конвейер устанавливает флаг ``_cancel_requested``,  |
        |   | который проверяется каждым воркером перед           |
        |   | обработкой следующего файла.                        |
        +---+-----------------------------------------------------+
        | 3 | Сканирование завершится после текущего              |
        |   | обрабатываемого файла в каждом воркере конвейера.   |
        +---+-----------------------------------------------------+

        Если сканирование не выполняется (конвейер не запущен),
        метод устанавливает флаг отмены, который будет проверен
        при следующем запуске сканирования.

        Примечание (Фаза 4):
        Отмена выполняется через флаг ``_cancel_requested``
        конвейера. Воркеры проверяют флаг перед обработкой
        каждого файла. Это обеспечивает корректное завершение
        текущей операции перед остановкой. Метод является
        синхронным и не блокирует event loop.
        """
        self._cancel_requested = True
        if self._current_pipeline is not None:
            self._event_bus.publish(
                ScanCancelRequested(
                    correlation_id=self._current_correlation_id,
                    source="ScanOrchestrator",
                    stage="cancel",
                    scan_id=self._current_scan_id,
                )
            )
            self._current_pipeline.cancel()

    def get_scan_status(self) -> ScanProgress:
        """
        Возвращает статус текущего или последнего сканирования.

        Живой прогресс (скорректированный план):
        Если ``_current_pipeline`` не равен ``None`` (сканирование
        активно), возвращается живой прогресс из памяти конвейера.
        Это обеспечивает мгновенный доступ к актуальному прогрессу
        без обращения к БД.

        Если сканирование не активно, но есть сохранённый прогресс
        в ``_last_scan_progress`` (от последнего завершённого
        сканирования), возвращается он. Это позволяет сохранить
        детальную разбивку (indexed, duplicate, skipped, error),
        которая не хранится в таблице ``scan_state``.

        Если ни конвейер, ни кэш не доступны, читается последняя
        запись из таблицы ``scan_state`` (базовые поля).

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Если ``_current_pipeline is not None``:             |
        |   | → возврат ``_current_pipeline.get_progress()``.     |
        +---+-----------------------------------------------------+
        | 2 | Если ``_last_scan_progress is not None``:           |
        |   | → возврат ``_last_scan_progress``.                  |
        +---+-----------------------------------------------------+
        | 3 | Иначе: чтение последней записи из ``scan_state``.   |
        +---+-----------------------------------------------------+
        | 4 | Если записей нет → возврат статуса ``IDLE``.        |
        +---+-----------------------------------------------------+
        | 5 | Формирование ``ScanProgress`` из записи БД.         |
        +---+-----------------------------------------------------+

        Примечание (Фаза 4):
        Метод является синхронным. При активном сканировании
        чтение прогресса из памяти конвейера является быстрой
        операцией. При отсутствии активного сканирования метод
        выполняет блокирующую операцию чтения из БД. При вызове
        из асинхронного контекста должен вызываться через
        ``run_in_executor``.

        Returns:
            ``ScanProgress`` с статусом текущего или последнего
            сканирования. Если сканирований не было, возвращает
            статус ``IDLE``.
        """
        # 1. Живой прогресс из конвейера
        if self._current_pipeline is not None:
            return self._current_pipeline.get_progress()

        # 2. Кэш детальной статистики последнего сканирования
        if self._last_scan_progress is not None:
            return self._last_scan_progress

        # 3. Последняя запись из БД (базовые поля)
        results = self._db.execute(
            "SELECT phase, status, total_files, processed_files, "
            "current_file, errors FROM scan_state "
            "ORDER BY scan_id DESC LIMIT 1"
        )
        if not results:
            return ScanProgress(status=ScanStatus.IDLE)

        row = results[0]
        return ScanProgress(
            phase=ScanPhase(str(row[0])),
            status=ScanStatus(str(row[1])),
            total_files=int(row[2]),
            processed_files=int(row[3]),
            current_file=str(row[4]),
            error_files=int(row[5]),
        )

    # ------------------------------------------------------------------
    # Сброс зависших записей сканирования
    # ------------------------------------------------------------------

    def reset_stale_running_scans(self) -> None:
        """
        Помечает старые записи ``running`` в ``scan_state``
        как ``interrupted``.

        Вызывается при старте приложения для восстановления
        после аварийного завершения. Если процесс был завершён
        без корректного shutdown, записи со статусом ``running``
        остаются в БД и могут блокировать новый запуск сканирования.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Формирование ``completed_at`` (ISO 8601, UTC).      |
        +---+-----------------------------------------------------+
        | 2 | ``UPDATE scan_state SET status = 'interrupted',     |
        |   | completed_at = ... WHERE status = 'running'``.      |
        +---+-----------------------------------------------------+

        Примечание:
        Метод является синхронным и выполняет блокирующую
        операцию записи в БД. Вызывается при старте приложения
        через ``run_in_executor`` или напрямую (вне асинхронного
        контекста).

        Метод идемпотентен: повторный вызов не изменяет данные,
        если записей со статусом ``running`` нет.
        """
        completed_at = datetime.now(UTC).isoformat()
        self._db.execute_write(
            "UPDATE scan_state SET status = ?, completed_at = ? WHERE status = ?",
            (ScanStatus.INTERRUPTED.value, completed_at, ScanStatus.RUNNING.value),
        )

    # ------------------------------------------------------------------
    # Кэшированное состояние индексации
    # ------------------------------------------------------------------

    def get_cached_index_status(self, rd_directory: str) -> IndexStatus:  # noqa: ARG002
        """
        Возвращает кэшированное состояние индексации.

        Не выполняет тяжёлых операций (сканирование каталога,
        чтение всей таблицы ``documents``, хеширование файлов).
        Возвращает кэш, если он актуален (возраст меньше
        ``INDEX_STATUS_CACHE_TTL_SECONDS``).

        Если кэш пуст или устарел, возвращает заглушку с
        состоянием ``IndexState.UNKNOWN`` и сообщением о
        необходимости обновления.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Захват ``_index_status_lock``.                      |
        +---+-----------------------------------------------------+
        | 2 | Если кэш существует и возраст < TTL:                |
        |   | → возврат кэша.                                     |
        +---+-----------------------------------------------------+
        | 3 | Иначе: возврат заглушки ``IndexStatus`` с           |
        |   | состоянием ``UNKNOWN`` и сообщением о               |
        |   | необходимости обновления.                           |
        +---+-----------------------------------------------------+

        Примечание:
        Метод является синхронным и не выполняет блокирующих
        операций. Может вызываться из асинхронного контекста
        без ``run_in_executor``.

        Args:
            rd_directory: Путь к каталогу рабочей документации.
                Используется для формирования сообщения.

        Returns:
            ``IndexStatus`` с кэшированным состоянием или заглушкой.
        """
        with self._index_status_lock:
            if self._index_status_cache is not None:
                age = time.monotonic() - self._index_status_at
                if age < config.INDEX_STATUS_CACHE_TTL_SECONDS:
                    return self._index_status_cache

            # Кэш пуст или устарел — вернуть заглушку
            return IndexStatus(
                state=IndexState.UNKNOWN,
                total_files_in_directory=0,
                indexed_files=0,
                duplicate_files_on_disk=0,
                new_files=0,
                modified_files=0,
                unindexed_files=0,
                search_available=False,
                message="Состояние индекса ещё не рассчитано. Нажмите «Обновить».",
            )

    def refresh_index_status(
        self,
        rd_directory: str,
        force: bool = False,
    ) -> IndexStatus:
        """
        Выполняет принудительный пересчёт состояния индексации.

        Выполняет тяжёлый расчёт (сканирование каталога, чтение
        таблицы ``documents``, хеширование изменённых файлов) и
        сохраняет результат в кэш.

        Защита от слишком частого пересчёта:
        Если с момента последнего пересчёта прошло меньше
        ``INDEX_STATUS_MIN_REFRESH_INTERVAL_SECONDS`` и ``force``
        не установлен, возвращается кэшированный результат.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Если ``force == False``: проверка минимального      |
        |   | интервала. Если интервал не соблюдён и кэш          |
        |   | существует → возврат кэша.                          |
        +---+-----------------------------------------------------+
        | 2 | Вызов ``_calculate_index_status(rd_directory)``     |
        |   | → тяжёлый расчёт.                                   |
        +---+-----------------------------------------------------+
        | 3 | Захват ``_index_status_lock``.                      |
        +---+-----------------------------------------------------+
        | 4 | Сохранение результата в кэш.                        |
        +---+-----------------------------------------------------+
        | 5 | Обновление ``_index_status_at``.                    |
        +---+-----------------------------------------------------+
        | 6 | Освобождение блокировки → возврат результата.       |
        +---+-----------------------------------------------------+

        Примечание:
        Метод является синхронным и выполняет блокирующие
        операции (сканирование каталога, чтение из БД,
        хеширование файлов). При вызове из асинхронного
        контекста должен вызываться через ``run_in_executor``
        с использованием ``scan_executor``.

        Args:
            rd_directory: Путь к каталогу рабочей документации.
            force: Если ``True``, пересчёт выполняется независимо
                от минимального интервала.

        Returns:
            ``IndexStatus`` с фактическим состоянием индексации.
        """
        # Проверка минимального интервала
        if not force:
            with self._index_status_lock:
                if self._index_status_at > 0:
                    elapsed = time.monotonic() - self._index_status_at
                    if (
                        elapsed < config.INDEX_STATUS_MIN_REFRESH_INTERVAL_SECONDS
                        and self._index_status_cache is not None
                    ):
                        return self._index_status_cache

        # Тяжёлый расчёт
        result = self._calculate_index_status(rd_directory)

        # Сохранение в кэш
        with self._index_status_lock:
            self._index_status_cache = result
            self._index_status_at = time.monotonic()

        return result

    # ------------------------------------------------------------------
    # Фактическое состояние индексации (тяжёлый расчёт)
    # ------------------------------------------------------------------

    def _calculate_index_status(self, rd_directory: str) -> IndexStatus:
        """
        Вычисляет фактическое состояние индексации системы.

        Анализирует данные в БД и файловой системе для определения
        реального состояния индексации. Это источник истины для
        отображения состояния системы.

        Исправление "пропавших 5%" (Дубликаты на диске):
        Если файл на диске отсутствует в БД по пути, метод не
        считает его сразу "новым". Сначала вычисляется его хеш.
        Если хеш совпадает с уже проиндексированным документом,
        файл учитывается как ``duplicate_files_on_disk``.
        Это обеспечивает математический баланс:
        ``Total = Indexed + Duplicates + New + Modified``.

        Примечание (Фаза 4):
        Метод является синхронным и выполняет блокирующие операции
        (сканирование каталога, чтение из БД, хеширование файлов).
        При вызове из асинхронного контекста должен вызываться
        через ``run_in_executor``.

        Примечание (скорректированный план):
        Метод является приватным. Публичный доступ к состоянию
        индексации осуществляется через ``get_cached_index_status``
        и ``refresh_index_status``.

        Ленивое хеширование (Фаза 2 оптимизации):
        При сравнении файлов на диске с данными в БД сначала
        проверяются метаданные файла (размер и дата последнего
        изменения). Если метаданные совпадают с сохранёнными
        значениями ``cached_size`` и ``cached_mtime``, файл
        считается проиндексированным без хеширования. Хеширование
        выполняется только для файлов, метаданные которых
        изменились. Это значительно ускоряет определение состояния
        индексации для больших каталогов РД.

        Логика определения состояния:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Сканировать каталог РД и получить список файлов.    |
        +---+-----------------------------------------------------+
        | 2 | Получить список проиндексированных документов из    |
        |   | БД (включая сохранённые метаданные).                |
        +---+-----------------------------------------------------+
        | 3 | Для каждого файла на диске:                         |
        |   | a. Если пути нет в БД → вычислить хеш.              |
        |   |    - Если хеш есть в БД → ``duplicate_files_on_disk``|
        |   |    - Иначе → ``new_files``.                         |
        |   | b. Если путь есть в БД → сравнить метаданные.       |
        |   |    - Если совпадают → ``indexed_files``.            |
        |   |    - Если нет → вычислить хеш.                      |
        |   |      * Если хеш изменился → ``modified_files``.     |
        |   |      * Если хеш совпал → ``indexed_files``.         |
        +---+-----------------------------------------------------+
        | 4 | Определить состояние и сформировать сообщение.      |
        +---+-----------------------------------------------------+

        Для документов, проиндексированных до применения миграции
        версии 2, поля ``cached_size`` и ``cached_mtime`` содержат
        значения по умолчанию (0 и пустая строка). Это приведёт
        к хешированию файла при первом определении состояния
        после миграции.

        Args:
            rd_directory: Путь к каталогу рабочей документации.

        Returns:
            ``IndexStatus`` с фактическим состоянием индексации.
        """
        # Сканировать каталог РД
        try:
            files = self._scanner.scan_directory(rd_directory)
        except Exception:
            return IndexStatus(
                state=IndexState.EMPTY,
                total_files_in_directory=0,
                indexed_files=0,
                duplicate_files_on_disk=0,
                new_files=0,
                modified_files=0,
                unindexed_files=0,
                search_available=False,
                message="Каталог РД недоступен.",
            )

        total_files_in_directory = len(files)

        indexed_docs = self._db.execute(
            "SELECT file_path, file_hash, cached_size, cached_mtime FROM documents"
        )
        indexed_by_path: dict[str, tuple[str, int, str]] = {}
        for row in indexed_docs:
            indexed_by_path[str(row[0])] = (
                str(row[1]),
                int(row[2]),
                str(row[3]),
            )

        indexed_files = 0
        duplicate_files_on_disk = 0
        new_files = 0
        modified_files = 0

        for file_path in files:
            relative_path = self._scanner.get_relative_path(
                file_path,
                rd_directory,
            )

            if relative_path not in indexed_by_path:
                # Файла нет в БД по пути. Проверяем, не является ли он дубликатом.
                try:
                    current_hash = self._hasher.compute_hash(file_path)
                    existing_by_hash = self._indexer.get_document_by_hash(current_hash)
                    if existing_by_hash is not None:
                        duplicate_files_on_disk += 1
                    else:
                        new_files += 1
                except Exception:
                    # Если не удалось хешировать (битый файл), считаем новым/ошибочным
                    new_files += 1
                continue

            stored_hash, cached_size, cached_mtime = indexed_by_path[relative_path]
            file_info = self._scanner.get_file_info(file_path)
            current_size = file_info.get("file_size", 0)
            current_mtime = file_info.get("last_modified", "")

            # Проверка ленивого хеширования
            if cached_size == current_size and cached_mtime == current_mtime:
                indexed_files += 1
                continue

            # Метаданные изменились, проверяем хеш
            try:
                current_hash = self._hasher.compute_hash(file_path)
            except Exception:
                modified_files += 1
                continue

            if current_hash != stored_hash:
                modified_files += 1
            else:
                # Метаданные изменились, но хеш совпал (например, пересохранение без изменений)
                indexed_files += 1

        unindexed_files = new_files + modified_files

        # Исправление search_available: базируемся на наличии документов в БД,
        # а не на indexed_files (файлы на диске)
        existing_doc = self._db.execute("SELECT 1 FROM documents LIMIT 1")
        search_available = len(existing_doc) > 0

        # Определение состояния и сообщения
        if total_files_in_directory == 0:
            state = IndexState.NO_SOURCE_FILES
            message = "В каталоге РД нет файлов для индексации."
        elif indexed_files == 0 and duplicate_files_on_disk == 0 and total_files_in_directory > 0:
            state = IndexState.EMPTY
            message = (
                f"Сканирование не выполнялось. "
                f"В каталоге РД {total_files_in_directory} файлов. "
                f"Запустите сканирование."
            )
        elif unindexed_files == 0:
            if indexed_files > 0:
                state = IndexState.FULLY_INDEXED
                msg_parts = [
                    f"Индексация завершена. {indexed_files} уникальных файлов проиндексированы."
                ]
                if duplicate_files_on_disk > 0:
                    msg_parts.append(f"Обнаружено {duplicate_files_on_disk} дубликатов на диске.")
                msg_parts.append("Поиск доступен.")
                message = " ".join(msg_parts)
            else:
                # Все файлы на диске — дубликаты, оригиналы отсутствуют
                state = IndexState.NEEDS_UPDATE
                message = (
                    f"Все {duplicate_files_on_disk} файлов на диске являются дубликатами "
                    f"ранее проиндексированных документов. "
                    f"Запустите сканирование для переиндексации."
                )
        elif indexed_files > 0 and unindexed_files > 0:
            state = IndexState.PARTIALLY_INDEXED
            message = (
                f"Индексация не завершена. "
                f"{indexed_files} из {total_files_in_directory} файлов "
                f"проиндексировано. "
                f"Запустите сканирование для индексации "
                f"оставшихся {unindexed_files} файлов."
            )
        else:
            state = IndexState.NEEDS_UPDATE
            if indexed_files == 0:
                message = (
                    f"Обнаружено {unindexed_files} новых или изменённых файлов"
                    + (
                        f" и {duplicate_files_on_disk} дубликатов"
                        if duplicate_files_on_disk > 0
                        else ""
                    )
                    + ". Запустите сканирование для создания индекса."
                )
            else:
                message = (
                    "Обнаружены новые или изменённые файлы. "
                    "Запустите сканирование для обновления индекса."
                )

        return IndexStatus(
            state=state,
            total_files_in_directory=total_files_in_directory,
            indexed_files=indexed_files,
            duplicate_files_on_disk=duplicate_files_on_disk,
            new_files=new_files,
            modified_files=modified_files,
            unindexed_files=unindexed_files,
            search_available=search_available,
            message=message,
        )

    # ------------------------------------------------------------------
    # Вспомогательные методы для работы с scan_state
    # ------------------------------------------------------------------

    def _create_scan_record(self, phase: ScanPhase) -> int:
        """
        Создаёт запись о сканировании в таблице ``scan_state``.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Формирование ``started_at`` (ISO 8601, UTC).        |
        +---+-----------------------------------------------------+
        | 2 | Вызов ``execute_write_returning_id`` для             |
        |   | атомарной вставки и получения ``scan_id``.          |
        +---+-----------------------------------------------------+
        | 3 | Возврат ``scan_id``.                                |
        +---+-----------------------------------------------------+

        Args:
            phase: Фаза сканирования.

        Returns:
            ``scan_id`` созданной записи.
        """
        started_at = datetime.now(UTC).isoformat()
        return self._db.execute_write_returning_id(
            "INSERT INTO scan_state "
            "(phase, status, started_at, completed_at, total_files, "
            " processed_files, current_file, errors) "
            "VALUES (?, ?, ?, '', 0, 0, '', 0)",
            (phase.value, ScanStatus.RUNNING.value, started_at),
        )

    def _complete_scan_record(
        self,
        scan_id: int,
        progress: ScanProgress,
    ) -> None:
        """
        Завершает запись о сканировании.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Формирование ``completed_at`` (ISO 8601, UTC).      |
        +---+-----------------------------------------------------+
        | 2 | ``UPDATE scan_state SET status, completed_at,       |
        |   | total_files, processed_files, current_file,         |
        |   | errors WHERE scan_id``.                             |
        +---+-----------------------------------------------------+

        Args:
            scan_id: Идентификатор записи сканирования.
            progress: Итоговый прогресс сканирования.
        """
        completed_at = datetime.now(UTC).isoformat()
        self._db.execute_write(
            "UPDATE scan_state SET "
            "status = ?, completed_at = ?, total_files = ?, "
            "processed_files = ?, current_file = ?, errors = ? "
            "WHERE scan_id = ?",
            (
                progress.status.value,
                completed_at,
                progress.total_files,
                progress.processed_files,
                progress.current_file,
                progress.error_files,
                scan_id,
            ),
        )

    # ------------------------------------------------------------------
    # Методы-делегаты для диагностического API
    # ------------------------------------------------------------------

    def get_pool_stats(self) -> dict:
        """
        Возвращает статистику пула соединений БД для диагностики.

        Делегирует вызов методу ``get_pool_stats`` объекта ``_db``.

        Returns:
            Словарь со статистикой пула соединений.
        """
        return self._db.get_pool_stats()

    def remove_document(self, doc_id: str) -> None:
        """
        Удаляет документ из индекса.

        Делегирует вызов методу ``remove_document`` объекта ``_indexer``.

        Args:
            doc_id: Идентификатор документа для удаления.
        """
        self._indexer.remove_document(doc_id)
