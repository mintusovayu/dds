"""
Управление жизненным циклом приложения DDS.

Этот модуль содержит функцию ``create_app_with_lifespan``, которая
создаёт экземпляр FastAPI с настроенным lifespan-обработчиком.
Lifespan выполняет инициализацию компонентов при старте и
корректное завершение при остановке.

Удаление Service Locator (скорректированный план, Фаза 2):

Модульные глобалы ``dds_web.api._context`` и
``dds_web.auth._auth_service`` и функции ``set_context()`` /
``set_auth_service()`` удалены. Все зависимости передаются
через ``app.state``:

- ``app.state.api_context`` — :class:`~dds_web.api.APIContext`;
- ``app.state.auth_service`` — :class:`~dds_web.auth.AuthService`.

FastAPI dependency-функции :func:`dds_web.api.get_context` и
:func:`dds_web.auth.get_auth_service` читают значения из
``request.app.state`` в момент обработки запроса. Это устраняет
скрытую глобальную связь между эндпоинтами и делает зависимости
явными через ``Depends(...)``. Тесты могут подменять контекст
через ``app.dependency_overrides[get_context]`` и
``app.dependency_overrides[get_auth_service]``.

Асинхронная модель (Фаза 4, вариант A):
Lifespan-обработчики позволяют выполнять асинхронную
инициализацию и завершение в том же event loop, что и
веб-обработчики. Это обеспечивает корректное управление
ресурсами, включая ожидание завершения активной задачи
сканирования перед закрытием БД.

Разделение пулов и subprocess-задач (Фаза 4):
При инициализации создаются:
- ``api_executor`` — ``ThreadPoolExecutor`` для веб-запросов
  (чтение из БД, файловой системы);
- ``scan_executor`` — ``ThreadPoolExecutor`` для операций
  сканирования и рендера PDF;
- ``process_runner`` — ``ProcessTaskRunner`` (process-per-task
  на контексте forkserver) для формирования планов индексации
  PDF в изолированных subprocess.

``ProcessTaskRunner`` заменяет прежний ``ProcessPoolExecutor`` для
``extract_executor``. Преимущества: изоляция состояния (forkserver),
устойчивость к сегфолтам PyMuPDF (процесс-per-task), автоматическое
освобождение ресурсов. Worker-функция
``build_index_plan_in_subprocess`` передаётся в ``create_components``
как ``Callable`` — это единственная точка импорта
``dds_core.subprocess_tasks`` во всём проекте (контракт
``subprocess-tasks-isolation``). Метод ``close()`` runner'а
вызывается в shutdown до закрытия БД.

DocumentIndexPlan (Фаза 5, ADR-005):
Запись планов индексации в БД делегируется ``SqliteIndexWriter``
(реализация ``IIndexWriter``). ``SqliteIndexWriter`` создаётся
в ``create_components`` и передаётся в ``TextIndexer``. Ранее
SQL-строки формировались в ``application/query_builder.py``;
теперь SQL-специфика сосредоточена в infrastructure-слое.

Read-side SQL cleanup (Фаза 8, ADR-008):
Read-side доступ к документам (поиск по хешу и пути, чтение
метаданных и текста страниц) делегируется
``SqliteDocumentRepository`` (реализация
``IDocumentRepository``). ``SqliteDocumentRepository`` создаётся
в ``create_components`` и передаётся в ``TextIndexer``,
``SearchEngine`` и ``DocumentCache``. До Фазы 8 SQL-строки для
чтения находились в application-слое; теперь application-слой
не содержит SQL ни на запись (ADR-005), ни на чтение (ADR-008).

Фильтрация по метаданным:
При старте приложения создаются репозитории справочников
(объектов, дисциплин, типов документов) и сервис обновления
метаданных документов. Они передаются в ``ScanOrchestrator``
и ``APIContext``, обеспечивая работу новых фильтров и
эндпоинтов ``/api/filters/metadata`` и ``/api/scan/refresh-metadata``.

Инициализация справочников из JSON:
Если справочные таблицы пусты, при старте выполняется загрузка
эталонных данных из JSON-файла (``reference_data_path`` в
``config.json`` или ``core_config.DEFAULT_REFERENCES_JSON_PATH``).
Это позволяет автоматически заполнять справочники при первом
развёртывании без ручных действий.

Файловое логирование:
При старте настраивается файловое логирование с ротацией.

Сброс зависших записей:
При старте вызывается ``ScanOrchestrator.reset_stale_running_scans()``.

Событийная модель (Фаза 5):
При старте создаётся и запускается асинхронная шина событий
``AsyncEventBus``, а также подписчик логирования ``LoggingSubscriber``.

Фоновая очистка сессий:
При старте создаётся асинхронная задача очистки сессий.

Упрощённое ожидание задачи сканирования при остановке:
Используется прямое ожидание с таймаутом. Таймаут берётся из
``core_config.OPERATION_TIMEOUTS["scan.cancel_grace"]``.

Монтирование статических файлов:
Каталог ``dds_web/static/`` монтируется по пути ``/static``.

Предпросмотр документа (рендер и подсветка):
При старте создаются ``HighlightsService`` и ``WordIndexCache``.
Оба сервиса передаются в ``APIContext``. Кэш сохраняется в
``app.state`` для очистки в shutdown.

Автологин по IP-адресу:
При старте выполняется парсинг и валидация конфигурации
``auth.auto_login``. Невалидный CIDR приводит к остановке
приложения (fail-fast); несуществующие пользователи и пустые
коллекции — к WARNING в stdout. Валидированный
``AutoLoginConfig`` сохраняется в ``app.state.auto_login_config``.

Middleware ``auto_login_middleware`` регистрируется в
``create_app_with_lifespan`` (не в startup). Порядок регистрации:
``events_middleware`` добавляется первым (становится innermost),
``auto_login_middleware`` — вторым (становится outermost и
выполняется первым на каждом запросе).

Middleware читает ``auto_login_config``, ``auth_service`` и
``event_bus`` из ``request.app.state`` в момент обработки запроса,
что позволяет обойти ограничение FastAPI «middleware нельзя
добавить после старта приложения».

Управление паролями через env (скорректированный план):
``create_auth_service`` поддерживает три источника паролей
(в порядке приоритета):

1. ``DDS_USER_<NAME>_PASSWORD`` — пароль в открытом виде.
2. ``DDS_USER_<NAME>_PASSWORD_HASH`` + ``DDS_USER_<NAME>_PASSWORD_SALT``
   — готовый хеш и соль (PBKDF2-HMAC-SHA256, 64 hex-символа).
3. Поле ``password`` в ``config.json`` — только для локальной
   разработки.

``<NAME>`` формируется функцией ``_env_var_name``: ``username``
приводится к верхнему регистру, все символы кроме ``[A-Z0-9_]``
заменяются на ``_``. При обнаружении коллизии (два username дают
одно env-имя) выводится WARNING в stdout.

Graceful shutdown (Фаза 4):
При остановке:
- ``ProcessTaskRunner.close(shutdown_timeout)`` завершает активные
  subprocess-задачи (SIGTERM → grace → SIGKILL); метод
  асинхронный, вызывается в event loop lifespan;
- ``ThreadPoolExecutor.shutdown(wait=True, cancel_futures=True)``
  гарантирует, что активные задачи в ``api_executor`` и
  ``scan_executor`` завершатся до закрытия БД, а ещё не начатые
  будут отменены.

Порядок операций предотвращает обращение к закрытой БД из
фоновых потоков и subprocess-задач.

Принципы:
- Модуль находится в presentation layer и не содержит бизнес-логики.
- Компоненты создаются через dependency injection.
- Зависимости сохраняются в ``app.state`` и читаются FastAPI
  dependency-функциями в момент обработки запроса. Модульные
  глобалы (Service Locator) не используются.
- Все блокирующие операции выполняются через ``run_in_executor``.
- Graceful shutdown гарантирует корректное завершение всех компонентов.
- Пулы потоков и subprocess-задачи разделяются для предотвращения
  конкуренции.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from typing import Any
from urllib.parse import quote

from dds_core.application.dependency_checker import DependencyChecker
from dds_core.application.document_cache import DocumentCache
from dds_core.application.document_metadata_service import (
    DocumentMetadataService,
)
from dds_core.application.highlights_service import HighlightsService
from dds_core.application.indexer import TextIndexer
from dds_core.application.module_lifecycle import ModuleLifecycle
from dds_core.application.module_loader import ModuleLoader
from dds_core.application.reference_data_initializer import (
    ReferenceDataInitializer,
)
from dds_core.application.reference_data_service import ReferenceDataService
from dds_core.application.scan_orchestrator import ScanOrchestrator
from dds_core.application.search_engine import SearchEngine
from dds_core.application.word_index_cache import WordIndexCache
from dds_core.domain import config as core_config
from dds_core.domain.events import (
    ApplicationStarted,
    ApplicationStopped,
    ApplicationStopping,
    RequestCompleted,
    RequestStarted,
)
from dds_core.domain.interfaces import IEventBus
from dds_core.infrastructure.database import DatabaseManager
from dds_core.infrastructure.event_bus import AsyncEventBus
from dds_core.infrastructure.file_hasher import FileHasher
from dds_core.infrastructure.file_scanner import DirectoryScanner
from dds_core.infrastructure.fts5_search_backend import FTS5SearchBackend
from dds_core.infrastructure.logging_subscriber import LoggingSubscriber
from dds_core.infrastructure.process_task_runner import ProcessTaskRunner
from dds_core.infrastructure.pymupdf_text_extractor import (
    PyMuPDFTextExtractor,
)
from dds_core.infrastructure.sqlite_adapter import SQLiteAdapter
from dds_core.infrastructure.sqlite_document_repository import (
    SqliteDocumentRepository,
)
from dds_core.infrastructure.sqlite_index_writer import SqliteIndexWriter
from dds_core.infrastructure.sqlite_reference_repository import (
    SqliteReferenceRepository,
)
from dds_core.subprocess_tasks.pdf_workers import build_index_plan_in_subprocess
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import APIContext
from .api import router as api_router
from .auth import (
    AuthService,
    LoginRequiredException,
    UserRole,
)
from .auto_login import (
    auto_login_middleware,
    parse_auto_login_config,
    validate_auto_login_config,
)
from .pages import pages_router
from .security import is_safe_redirect_path

# ----------------------------------------------------------------------
# Константы
# ----------------------------------------------------------------------

SHUTDOWN_SCAN_TIMEOUT_SECONDS: float = core_config.OPERATION_TIMEOUTS["scan.cancel_grace"]
"""Таймаут ожидания завершения задачи сканирования при остановке.

Значение берётся из реестра ``OPERATION_TIMEOUTS`` по ключу
``scan.cancel_grace`` (по умолчанию 5 секунд). Если ключ отсутствует
в реестре, при импорте модуля возникает ``KeyError`` — это
намеренное поведение: конфигурация должна быть согласованной.
"""

# ----------------------------------------------------------------------
# Вспомогательные функции
# ----------------------------------------------------------------------


def load_config(config_path: str) -> dict:
    """Читает конфигурацию из JSON-файла.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Проверка ``os.path.isfile(config_path)``.           |
    +---+-----------------------------------------------------+
    | 2 | При отсутствии → ``FileNotFoundError``.             |
    +---+-----------------------------------------------------+
    | 3 | ``open(config_path)`` → ``json.load()``.            |
    +---+-----------------------------------------------------+
    | 4 | Возврат словаря конфигурации.                       |
    +---+-----------------------------------------------------+

    Args:
        config_path: Путь к файлу ``config.json``.

    Returns:
        Словарь с конфигурацией.

    Raises:
        FileNotFoundError: Файл конфигурации не найден.
        json.JSONDecodeError: Файл не является валидным JSON.
    """
    if not os.path.isfile(config_path):
        raise FileNotFoundError(
            f"Файл конфигурации не найден: {config_path}. "
            f"Скопируйте config.json.example в config.json "
            f"и измените параметры."
        )
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def create_components(
    config: dict,
    event_bus: IEventBus,
    process_runner: ProcessTaskRunner,
    index_plan_worker: Callable[..., Any],
    scan_executor: Executor | None = None,
) -> dict:
    """Создаёт все компоненты DDS через dependency injection.

    Включая компоненты фильтрации по метаданным, инициализацию
    справочников из JSON, ``SqliteIndexWriter`` для записи планов
    индексации, ``SqliteDocumentRepository`` для read-side доступа
    к документам (Фаза 8, ADR-008) и передачу исполнителя
    subprocess-задач (``process_runner``) вместе с worker'ом
    (``index_plan_worker``) в ``ScanOrchestrator``.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Извлечение путей из ``config["dds"]``.              |
    +---+-----------------------------------------------------+
    | 2 | Создание ``SQLiteAdapter`` +                        |
    |   | ``DatabaseManager.ensure_all()``.                   |
    +---+-----------------------------------------------------+
    | 3 | Инициализация справочников из JSON, если таблицы    |
    |   | пусты.                                             |
    +---+-----------------------------------------------------+
    | 4 | Создание инфраструктурных компонентов:              |
    |   | ``DirectoryScanner``, ``FileHasher``,               |
    |   | ``PyMuPDFTextExtractor``, ``FTS5SearchBackend``,    |
    |   | ``SqliteIndexWriter``, ``SqliteDocumentRepository``.|
    +---+-----------------------------------------------------+
    | 5 | Создание application-компонентов:                   |
    |   | ``TextIndexer`` (с ``SqliteIndexWriter`` и          |
    |   | ``IDocumentRepository``), ``SearchEngine`` (с       |
    |   | ``IDocumentRepository``), ``ModuleLifecycle``,      |
    |   | ``ScanOrchestrator``.                               |
    +---+-----------------------------------------------------+
    | 6 | Создание ``DocumentCache`` (с                       |
    |   | ``IDocumentRepository``) и передача его в           |
    |   | ``ScanOrchestrator``.                               |
    +---+-----------------------------------------------------+
    | 7 | Создание репозиториев справочников и сервисов       |
    |   | фильтрации.                                         |
    +---+-----------------------------------------------------+
    | 8 | Передача ``event_bus`` во все компоненты, которые   |
    |   | публикуют события.                                 |
    +---+-----------------------------------------------------+
    | 9 | Передача ``process_runner`` и ``index_plan_worker`` |
    |   | в ``ScanOrchestrator``.                             |
    +---+-----------------------------------------------------+
    | 10| Возврат словаря компонентов.                        |
    +---+-----------------------------------------------------+

    Args:
        config: Словарь конфигурации из ``config.json``.
        event_bus: Шина событий для публикации.
        process_runner: Исполнитель subprocess-задач
            (``ProcessTaskRunner``). Передаётся в
            ``ScanOrchestrator``. Обязателен.
        index_plan_worker: Picklable-функция формирования плана
            индексации одного PDF (``build_index_plan_in_subprocess``).
            Передаётся как ``Callable`` без импорта
            ``subprocess_tasks`` в application. Обязательна.
        scan_executor: Пул потоков для операций сканирования.
            Если ``None``, используется дефолтный пул.

    Returns:
        Словарь созданных компонентов.
    """
    # Пути из конфигурации
    dds_config = config.get("dds", {})
    db_path = dds_config.get("db_path", core_config.DEFAULT_DB_PATH)
    rd_directory = dds_config.get(
        "rd_directory",
        core_config.DEFAULT_RD_DIRECTORY,
    )
    modules_directory = dds_config.get(
        "modules_directory",
        core_config.DEFAULT_MODULES_DIRECTORY,
    )

    # База данных
    db = SQLiteAdapter(db_path, event_bus=event_bus)
    database_manager = DatabaseManager(db)
    database_manager.ensure_all()

    # Инициализация справочников из JSON (если база пуста)
    reference_data_path = dds_config.get(
        "reference_data_path",
        core_config.DEFAULT_REFERENCES_JSON_PATH,
    )
    ref_initializer = ReferenceDataInitializer(db, reference_data_path)
    stats = ref_initializer.initialize()
    if stats["codes_inserted"] or stats["aliases_inserted"]:
        print(
            f"INFO:     Справочники загружены из JSON: "
            f"кодов={stats['codes_inserted']}, "
            f"псевдонимов={stats['aliases_inserted']}"
        )
    else:
        print("INFO:     Инициализация справочников не выполнена (см. логи).")

    # Инфраструктурные компоненты
    scanner = DirectoryScanner()
    hasher = FileHasher()
    text_extractor = PyMuPDFTextExtractor()
    search_backend = FTS5SearchBackend(db)
    index_writer = SqliteIndexWriter(db)

    # Application-компоненты
    #
    # Read-side доступ к документам делегируется
    # SqliteDocumentRepository (Фаза 8, ADR-008). SQL-строки
    # для чтения метаданных и текста страниц выведены из
    # application-слоя.
    document_repository = SqliteDocumentRepository(db)
    indexer = TextIndexer(text_extractor, index_writer, document_repository)
    search_engine = SearchEngine(search_backend, document_repository)

    # Модульный загрузчик и проверка зависимостей
    module_loader = ModuleLoader(db, modules_directory)
    dependency_checker = DependencyChecker(db, "1.0.0")
    module_lifecycle = ModuleLifecycle(
        db=db,
        modules_directory=modules_directory,
        dds_version="1.0.0",
        event_bus=event_bus,
        loader=module_loader,
        checker=dependency_checker,
    )

    document_cache = DocumentCache(document_repository)

    # Репозитории справочников (основная таблица + таблица псевдонимов)
    object_repo = SqliteReferenceRepository(
        db,
        main_table_name="object_reference",
        alias_table_name="object_reference_alias",
    )
    discipline_repo = SqliteReferenceRepository(
        db,
        main_table_name="discipline_reference",
        alias_table_name="discipline_reference_alias",
    )
    document_type_repo = SqliteReferenceRepository(
        db,
        main_table_name="document_type_reference",
        alias_table_name="document_type_reference_alias",
    )

    document_metadata_service = DocumentMetadataService(
        db=db,
        object_repo=object_repo,
        discipline_repo=discipline_repo,
        document_type_repo=document_type_repo,
    )

    reference_data_service = ReferenceDataService(
        object_repo=object_repo,
        discipline_repo=discipline_repo,
        document_type_repo=document_type_repo,
    )

    scan_orchestrator = ScanOrchestrator(
        db=db,
        scanner=scanner,
        hasher=hasher,
        text_extractor=text_extractor,
        indexer=indexer,
        module_lifecycle=module_lifecycle,
        event_bus=event_bus,
        process_runner=process_runner,
        index_plan_worker=index_plan_worker,
        scan_executor=scan_executor,
        document_cache=document_cache,
        document_metadata_service=document_metadata_service,
    )

    return {
        "db": db,
        "database_manager": database_manager,
        "scanner": scanner,
        "hasher": hasher,
        "text_extractor": text_extractor,
        "document_repository": document_repository,
        "indexer": indexer,
        "search_engine": search_engine,
        "module_lifecycle": module_lifecycle,
        "scan_orchestrator": scan_orchestrator,
        "rd_directory": rd_directory,
        "document_metadata_service": document_metadata_service,
        "reference_data_service": reference_data_service,
    }


def initialize_modules(config: dict, components: dict) -> None:
    """Инициализирует модули из ``dds_modules/``.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Извлечение ``config["modules"]`` →                  |
    |   | ``module_configs``.                                 |
    +---+-----------------------------------------------------+
    | 2 | ``module_lifecycle.initialize_all(module_configs)`` |
    |   | → ``(loaded, errors)``.                             |
    +---+-----------------------------------------------------+
    | 3 | Вывод списка загруженных модулей.                   |
    +---+-----------------------------------------------------+
    | 4 | Вывод списка ошибок загрузки.                       |
    +---+-----------------------------------------------------+

    Args:
        config: Словарь конфигурации из ``config.json``.
        components: Словарь созданных компонентов.
    """
    module_configs = config.get("modules", {})
    module_lifecycle = components["module_lifecycle"]
    loaded, errors = module_lifecycle.initialize_all(module_configs)

    if loaded:
        print(f"Загружено модулей: {len(loaded)}")
        for name in loaded:
            print(f"INFO:     {name}")

    if errors:
        print(f"Ошибки загрузки модулей: {len(errors)}")
        for error in errors:
            print(f"  ✗ {error}")


# ----------------------------------------------------------------------
# Env-переменные для паролей
# ----------------------------------------------------------------------

_ENV_NAME_SAFE = re.compile(r"[^A-Za-z0-9_]")
"""Регулярное выражение для нормализации имени env-переменной.

Все символы, кроме латинских букв, цифр и подчёркивания,
заменяются на подчёркивание. Используется в :func:`_env_var_name`.
"""


def _env_var_name(username: str, suffix: str) -> str:
    """Возвращает имя env-переменной для пользователя.

    Нормализация детерминированная: ``username`` приводится к
    верхнему регистру, все символы кроме ``[A-Za-z0-9_]``
    заменяются на ``_``.

    Примеры:

    +-------------------+---------------------------------------+
    | ``username``      | Результат для ``suffix="PASSWORD"``   |
    +===================+=======================================+
    | ``admin``         | ``DDS_USER_ADMIN_PASSWORD``           |
    +-------------------+---------------------------------------+
    | ``john.doe``      | ``DDS_USER_JOHN_DOE_PASSWORD``        |
    +-------------------+---------------------------------------+
    | ``ivan-petrov``   | ``DDS_USER_IVAN_PETROV_PASSWORD``     |
    +-------------------+---------------------------------------+
    | ``иван``          | ``DDS_USER______PASSWORD`` (не        |
    |                   | рекомендуется: используйте латинские  |
    |                   | логины для env-паролей)               |
    +-------------------+---------------------------------------+

    Примечание о коллизиях:
        Разные ``username`` могут дать одинаковое env-имя
        (например, ``john.doe`` и ``john_doe``). Обнаружение
        коллизий — ответственность :func:`create_auth_service`,
        которая выводит WARNING в stdout при старте.

    Args:
        username: Имя пользователя из ``config.json``.
        suffix: Суффикс переменной (``"PASSWORD"``,
            ``"PASSWORD_HASH"``, ``"PASSWORD_SALT"``).

    Returns:
        Имя env-переменной в формате
        ``DDS_USER_<NORMALIZED>_<SUFFIX>``.
    """
    normalized = _ENV_NAME_SAFE.sub("_", username).upper()
    return f"DDS_USER_{normalized}_{suffix}"


def create_auth_service(config: dict) -> AuthService:
    """Создаёт сервис аутентификации.

    Пароли загружаются из трёх источников в порядке приоритета:

    1. ``DDS_USER_<NAME>_PASSWORD`` — открытый пароль из env.
    2. ``DDS_USER_<NAME>_PASSWORD_HASH`` +
       ``DDS_USER_<NAME>_PASSWORD_SALT`` — готовый хеш и соль
       (PBKDF2-HMAC-SHA256, 64 hex-символа).
    3. Поле ``password`` в ``config.json`` — только для локальной
       разработки.

    Перед загрузкой пользователей проверяется коллизия env-имён:
    если два разных ``username`` дают одинаковое env-имя,
    выводится WARNING (пароль из env будет применён к каждому;
    для разных паролей задайте их в ``config.json``).

    Операции:

    +----+----------------------------------------------------+
    | №  | Описание                                           |
    +====+====================================================+
    | 1  | Создание ``AuthService`` с TTL из конфигурации.    |
    +----+----------------------------------------------------+
    | 2  | Проверка коллизий env-имён для ``PASSWORD``:       |
    |    | построение ``env_name → [usernames]``; WARNING     |
    |    | для записей с несколькими пользователями.          |
    +----+----------------------------------------------------+
    | 3  | Для каждого пользователя из ``config["auth"]       |
    |    | ["users"]``:                                        |
    |    | a. Извлечение ``username``, ``password``, ``role``.|
    |    | b. Пропуск при отсутствии имени.                    |
    |    | c. Определение ``UserRole``.                        |
    |    | d. Чтение env-переменных (``PASSWORD``,              |
    |    |    ``PASSWORD_HASH``, ``PASSWORD_SALT``).           |
    |    | e. Если ``env_password`` — ``add_user``.            |
    |    | f. Иначе если ``env_hash`` и ``env_salt`` —         |
    |    |    ``add_user_with_hash``.                          |
    |    | g. Иначе если ``password`` в config — ``add_user``. |
    |    | h. Иначе — пропуск с сообщением.                    |
    |    | i. При ``ValueError`` — вывод предупреждения.       |
    +----+----------------------------------------------------+
    | 4  | Возврат ``auth_service``.                          |
    +----+----------------------------------------------------+

    Args:
        config: Словарь конфигурации из ``config.json``.

    Returns:
        Экземпляр ``AuthService`` с загруженными пользователями.
    """
    auth_config = config.get("auth", {})
    session_ttl = auth_config.get("session_ttl_seconds", 3600)
    auth_service = AuthService(session_ttl_seconds=session_ttl)

    # Предварительная проверка коллизий env-имён.
    env_name_to_users: dict[str, list[str]] = {}
    for user_data in auth_config.get("users", []):
        username = user_data.get("username", "")
        if not username:
            continue
        env_name = _env_var_name(username, "PASSWORD")
        env_name_to_users.setdefault(env_name, []).append(username)

    for env_name, users in env_name_to_users.items():
        if len(users) > 1:
            print(
                f"WARNING:  env-имя {env_name} соответствует нескольким "
                f"пользователям: {users}. Env-переменная будет применена "
                f"к каждому; при разных паролях задайте config.json."
            )

    for user_data in auth_config.get("users", []):
        username = user_data.get("username", "")
        if not username:
            continue

        role_str = user_data.get("role", "user")
        role = UserRole.ADMIN if role_str == "admin" else UserRole.USER

        env_password = os.environ.get(_env_var_name(username, "PASSWORD"))
        env_hash = os.environ.get(_env_var_name(username, "PASSWORD_HASH"))
        env_salt = os.environ.get(_env_var_name(username, "PASSWORD_SALT"))

        try:
            if env_password:
                auth_service.add_user(username, env_password, role)
                print(
                    f"INFO:     Пароль пользователя '{username}' получен "
                    f"из env {_env_var_name(username, 'PASSWORD')}."
                )
            elif env_hash and env_salt:
                auth_service.add_user_with_hash(username, env_hash, env_salt, role)
                print(
                    f"INFO:     Хеш пароля пользователя '{username}' получен "
                    f"из env {_env_var_name(username, 'PASSWORD_HASH')}."
                )
            else:
                password = user_data.get("password", "")
                if not password:
                    print(
                        f"  ! Пользователь '{username}' пропущен: "
                        f"пароль не задан ни в config.json, ни в env "
                        f"{_env_var_name(username, 'PASSWORD')}."
                    )
                    continue
                auth_service.add_user(username, password, role)
                print(
                    f"  ! Пароль пользователя '{username}' взят из "
                    f"config.json. Для production задайте env "
                    f"{_env_var_name(username, 'PASSWORD')}."
                )
        except ValueError as e:
            print(f"  ✗ Пользователь '{username}' не добавлен: {e}")
            continue

    return auth_service


# ----------------------------------------------------------------------
# Создание приложения с lifespan
# ----------------------------------------------------------------------


def create_app_with_lifespan(config_path: str) -> FastAPI:
    """Создаёт экземпляр FastAPI с настроенным lifespan.

    Операции:

    +----+----------------------------------------------------+
    | №  | Описание                                           |
    +====+====================================================+
    | 1  | Определить асинхронный контекстный менеджер        |
    |    | ``lifespan``.                                      |
    +----+----------------------------------------------------+
    | 2  | В startup:                                        |
    |    | a. Загрузить конфигурацию.                        |
    |    | b. Настроить файловое логирование.                |
    |    | c. Создать и запустить шину событий.             |
    |    | d. Создать и запустить подписчика логирования.    |
    |    | e. Создать пулы потоков и ``ProcessTaskRunner``.  |
    |    | f. Создать компоненты через ``create_components``|
    |    |    (передача ``process_runner`` и                  |
    |    |    ``index_plan_worker``).                          |
    |    | g. Инициализировать модули.                        |
    |    | h. Создать сервис аутентификации.                 |
    |    | i. Парсинг и валидация конфигурации                |
    |    |    ``auth.auto_login``; WARNING в stdout;          |
    |    |    сохранение в ``app.state.auto_login_config``.   |
    |    | j. Запустить фоновую очистку сессий.              |
    |    | k. Вызвать сброс зависших записей сканирования.   |
    |    | l. Создать ``HighlightsService`` и                 |
    |    |    ``WordIndexCache`` для предпросмотра.           |
    |    | m. Создать ``APIContext``.                        |
    |    | n. Сохранить ссылки в ``app.state``:              |
    |    |    ``api_context``, ``auth_service``,             |
    |    |    ``event_bus``, ``components``,                 |
    |    |    ``logging_subscriber``, пулы,                  |
    |    |    ``process_runner``,                            |
    |    |    ``word_index_cache``, ``auto_login_config``.    |
    |    | o. Публикация ``ApplicationStarted``.             |
    +----+----------------------------------------------------+
    | 3  | В shutdown:                                       |
    |    | a. Получить компоненты из ``app.state``.          |
    |    | b. Публикация ``ApplicationStopping``.            |
    |    | c. Отменить фоновую задачу очистки сессий.        |
    |    | d. Отменить сканирование.                         |
    |    | e. Дождаться завершения задачи сканирования.      |
    |    | f. Остановить модули.                             |
    |    | g. Очистить ``word_index_cache``.                 |
    |    | h. ``await process_runner.close(shutdown_timeout)``|
    |    |    → завершение subprocess-задач.                 |
    |    | i. Завершить ``api_executor`` и ``scan_executor`` |
    |    |    через ``shutdown(wait=True, cancel_futures=True)``.|
    |    | j. Закрыть БД.                                    |
    |    | k. Остановить подписчик логирования и шину.       |
    |    | l. Публикация ``ApplicationStopped``.             |
    +----+----------------------------------------------------+
    | 4  | Создать ``FastAPI`` с lifespan.                    |
    +----+----------------------------------------------------+
    | 5  | Смонтировать статические файлы.                   |
    +----+----------------------------------------------------+
    | 6  | Добавить middleware для условной публикации HTTP. |
    +----+----------------------------------------------------+
    | 7  | Зарегистрировать ``auto_login_middleware``         |
    |    | (из ``dds_web/auto_login.py``) вторым — после     |
    |    | ``events_middleware``. Становится outermost.       |
    +----+----------------------------------------------------+
    | 8  | Подключить роутеры API и страниц.                 |
    +----+----------------------------------------------------+
    | 9  | Зарегистрировать обработчик LoginRequiredException.|
    +----+----------------------------------------------------+
    | 10 | Вернуть приложение.                               |
    +----+----------------------------------------------------+

    Примечание:
        Порядок middleware определён правилами Starlette:
        последний добавленный middleware становится outermost
        и выполняется первым. ``events_middleware`` добавляется
        первым (становится innermost), ``auto_login_middleware``
        — вторым (становится outermost). Таким образом, на
        каждом запросе сначала выполняется автологин (инжектит
        токен в ``scope["state"]``), затем логирование
        HTTP-запросов, затем route handler.

    Примечание (Фаза 2):
        Зависимости передаются через ``app.state`` и читаются
        FastAPI dependency-функциями
        (:func:`dds_web.api.get_context`,
        :func:`dds_web.auth.get_auth_service`) в момент обработки
        запроса. Модульные глобалы (``_context``, ``_auth_service``)
        не используются: их установка и сброс из ``lifespan``
        удалены. Тесты могут подменять зависимости через
        ``app.dependency_overrides``.

    Примечание (Фаза 4):
        ``ProcessTaskRunner`` создаётся в startup и закрывается
        в shutdown через ``await process_runner.close(...)``.
        Worker-функция ``build_index_plan_in_subprocess``
        импортируется из ``dds_core.subprocess_tasks.pdf_workers``
        — это единственная точка импорта ``subprocess_tasks``
        во всём проекте (контракт ``subprocess-tasks-isolation``).

    Args:
        config_path: Путь к файлу ``config.json``.

    Returns:
        Экземпляр ``FastAPI``.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # ── Startup ──
        print("INFO:     Инициализация компонентов DDS...")

        # Шаг 1: Загрузка конфигурации
        config = load_config(config_path)

        # Шаг 2: Настройка файлового логирования с ротацией
        log_file_path = core_config.DEFAULT_LOG_FILE_PATH
        log_dir = os.path.dirname(os.path.abspath(log_file_path))
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        log_handler = RotatingFileHandler(
            log_file_path,
            maxBytes=core_config.LOG_MAX_FILE_SIZE_MB * 1024 * 1024,
            backupCount=core_config.LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        log_formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        log_handler.setFormatter(log_formatter)
        events_logger = logging.getLogger("dds.events")
        events_logger.addHandler(log_handler)
        print("INFO:     Файловое логирование настроено")

        # Шаг 3: Создание и запуск шины событий
        event_bus = AsyncEventBus(
            max_queue_size=core_config.EVENT_BUS_QUEUE_SIZE,
        )
        await event_bus.start()
        print("INFO:     Шина событий запущена")

        # Шаг 4: Создание и запуск подписчика логирования.
        # Уровень логгера и набор типов событий для подписки
        # вычисляются автоматически внутри LoggingSubscriber на
        # основе config.DEFAULT_LOG_LEVEL.
        logging_subscriber = LoggingSubscriber()
        await logging_subscriber.start(event_bus)
        print("INFO:     Подписчик логирования запущен")

        # Шаг 5: Создание пулов потоков и ProcessTaskRunner.
        #
        # api_executor и scan_executor — ThreadPoolExecutor:
        #   api_executor — веб-запросы (чтение БД, файловой системы).
        #   scan_executor — сканирование, рендер PDF, построение
        #                   индексов слов.
        #
        # ProcessTaskRunner — process-per-task исполнитель для
        # формирования планов индексации PDF в изолированных
        # subprocess (forkserver на Linux, spawn на macOS/Windows).
        # Заменяет прежний ProcessPoolExecutor для ``extract_executor``.
        # Метод ``run()`` асинхронный; старт forkserver выполняется
        # в отдельном потоке через ``asyncio.to_thread`` внутри
        # ``ProcessTaskRunner.run`` (обход ограничения Python 3.14).
        api_executor = ThreadPoolExecutor(
            max_workers=core_config.API_EXECUTOR_MAX_WORKERS,
            thread_name_prefix="dds-api",
        )
        scan_executor = ThreadPoolExecutor(
            max_workers=core_config.SCAN_EXECUTOR_MAX_WORKERS,
            thread_name_prefix="dds-scan",
        )
        process_runner = ProcessTaskRunner()
        print("INFO:     Пулы потоков и ProcessTaskRunner созданы")

        # Шаг 6: Создание компонентов с передачей пулов
        # и исполнителя subprocess-задач.
        #
        # build_index_plan_in_subprocess импортируется здесь —
        # единственная точка импорта subprocess_tasks во всём
        # проекте (контракт subprocess-tasks-isolation). Передаётся
        # как index_plan_worker; ScanPipeline вызывает его через
        # process_runner.run(...).
        components = create_components(
            config,
            event_bus,
            process_runner=process_runner,
            index_plan_worker=build_index_plan_in_subprocess,
            scan_executor=scan_executor,
        )
        print("INFO:     Компоненты инициализированы.")

        # Шаг 7: Инициализация модулей
        print("INFO:     Инициализация модулей...")
        initialize_modules(config, components)

        # Шаг 8: Создание сервиса аутентификации
        print("INFO:     Создание сервиса аутентификации...")
        auth_service = create_auth_service(config)

        # Шаг 9: Парсинг и валидация конфигурации автологина.
        # Невалидный CIDR → RuntimeError (fail-fast).
        # Прочие проблемы → WARNING в stdout.
        auto_login_raw = config.get("auth", {}).get("auto_login", {})
        try:
            auto_login_config = parse_auto_login_config(auto_login_raw)
        except ValueError as e:
            raise RuntimeError(f"Некорректная конфигурация auto_login: {e}") from e

        for warning in validate_auto_login_config(auto_login_config, auth_service):
            print(f"WARNING:  {warning}")

        if auto_login_config.enabled:
            print(
                f"INFO:     Автологин включён: "
                f"сетей={len(auto_login_config.allowed_networks)}, "
                f"IP-адресов={len(auto_login_config.ip_user_map)}"
            )
        else:
            print("INFO:     Автологин отключён")

        # Шаг 10: Запуск фоновой задачи очистки сессий
        async def _session_cleanup_task(auth_svc, interval):
            while True:
                try:
                    await asyncio.sleep(interval)
                    auth_svc.cleanup_expired_sessions()
                except asyncio.CancelledError:
                    break
                except Exception:
                    pass

        session_cleanup = asyncio.create_task(
            _session_cleanup_task(
                auth_service,
                core_config.SESSION_CLEANUP_INTERVAL_SECONDS,
            )
        )
        app.state.session_cleanup_task = session_cleanup
        print("INFO:     Фоновая очистка сессий запущена")

        # Шаг 11: Сброс зависших записей сканирования
        try:
            components["scan_orchestrator"].reset_stale_running_scans()
            print("INFO:     Зависшие записи сканирования сброшены")
        except Exception as e:
            print(f"  ✗ Ошибка сброса зависших записей: {e}")

        # Шаг 12: Создание сервисов предпросмотра документа.
        # HighlightsService выполняет поиск совпадений в нормализованном
        # WordIndex; WordIndexCache кэширует построенные индексы с
        # автоинвалидацией по file_hash.
        highlights_service = HighlightsService()
        word_index_cache = WordIndexCache(
            max_size=core_config.WORD_INDEX_CACHE_SIZE,
        )
        print("INFO:     Сервисы предпросмотра документа созданы")

        # Шаг 13: Создание APIContext с передачей сервиса справочников
        # и сервисов предпросмотра.
        api_context = APIContext(
            search_engine=components["search_engine"],
            scan_orchestrator=components["scan_orchestrator"],
            module_lifecycle=components["module_lifecycle"],
            rd_directory=components["rd_directory"],
            config_path=config_path,
            event_bus=event_bus,
            api_executor=api_executor,
            scan_executor=scan_executor,
            reference_data_service=components["reference_data_service"],
            text_extractor=components["text_extractor"],
            word_index_cache=word_index_cache,
            highlights_service=highlights_service,
        )

        # Шаг 14: Сохранение ссылок в app.state.
        # Значения читаются FastAPI dependency-функциями
        # (get_context, get_auth_service) в момент обработки
        # запроса. Модульные глобалы не используются.
        #
        # ``process_runner`` сохраняется для вызова ``close()``
        # в shutdown; отдельного ``extract_executor`` нет —
        # ``ProcessTaskRunner`` полностью заменяет прежний пул
        # процессов для формирования планов индексации.
        app.state.components = components
        app.state.auth_service = auth_service
        app.state.api_context = api_context
        app.state.event_bus = event_bus
        app.state.logging_subscriber = logging_subscriber
        app.state.api_executor = api_executor
        app.state.scan_executor = scan_executor
        app.state.process_runner = process_runner
        app.state.word_index_cache = word_index_cache
        app.state.auto_login_config = auto_login_config

        # Шаг 15: Публикация события ApplicationStarted
        event_bus.publish(
            ApplicationStarted(
                correlation_id=str(uuid.uuid4()),
                source="Lifespan",
                stage="startup",
                version="1.0.0",
                config_path=config_path,
            )
        )
        print("INFO:     DDS запущен.")

        yield

        # ── Shutdown ──
        print("\nINFO:     Завершение работы компонентов DDS...")

        ctx = app.state.api_context
        components_shutdown = app.state.components
        event_bus_shutdown = app.state.event_bus
        logging_subscriber_shutdown = app.state.logging_subscriber
        api_executor_shutdown = app.state.api_executor
        scan_executor_shutdown = app.state.scan_executor
        process_runner_shutdown = app.state.process_runner
        word_index_cache_shutdown = app.state.word_index_cache

        event_bus_shutdown.publish(
            ApplicationStopping(
                correlation_id=str(uuid.uuid4()),
                source="Lifespan",
                stage="shutdown",
            )
        )

        session_cleanup_task = app.state.session_cleanup_task
        if session_cleanup_task is not None:
            session_cleanup_task.cancel()
            try:
                await session_cleanup_task
            except asyncio.CancelledError:
                pass

        try:
            components_shutdown["scan_orchestrator"].cancel_scan()
            print("INFO:     Отмена сканирования запрошена")
        except Exception as e:
            print(f"  ✗ Ошибка отмены сканирования: {e}")

        scan_task = ctx.get_scan_task()
        if scan_task is not None and not scan_task.done():
            print("  INFO:     Ожидание завершения задачи сканирования...")
            try:
                await asyncio.wait_for(
                    scan_task,
                    timeout=SHUTDOWN_SCAN_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                print(
                    f"  ! Таймаут ожидания "
                    f"({SHUTDOWN_SCAN_TIMEOUT_SECONDS} с) истёк. "
                    f"Принудительная отмена задачи сканирования."
                )
                scan_task.cancel()
                try:
                    await scan_task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                print("  ! Задача сканирования была отменена.")
            except Exception as e:
                print(f"  ✗ Ошибка при ожидании задачи: {e}")

        try:
            components_shutdown["module_lifecycle"].shutdown_all()
            print("INFO:     Модули остановлены")
        except Exception as e:
            print(f"  ✗ Ошибка остановки модулей: {e}")

        # Очистка кэша индексов слов.
        if word_index_cache_shutdown is not None:
            try:
                word_index_cache_shutdown.clear()
                print("INFO:     Кэш индексов слов очищен")
            except Exception as e:
                print(f"  ✗ Ошибка очистки кэша индексов слов: {e}")

        # Завершение subprocess-задач и пулов потоков.
        #
        # Порядок:
        # 1. ``await process_runner.close(shutdown_timeout)`` —
        #    активные subprocess-задачи получают SIGTERM, через
        #    grace — SIGKILL. Метод асинхронный: вызывается в
        #    event loop lifespan.
        # 2. ``ThreadPoolExecutor.shutdown(wait=True, cancel_futures=True)``:
        #    wait=True — дождаться активных задач; cancel_futures=True —
        #    отменить ещё не начатые.
        #
        # Оба шага выполняются до закрытия БД: это гарантирует,
        # что фоновые потоки и subprocess-задачи не обратятся к
        # закрытой БД.
        try:
            await process_runner_shutdown.close(
                shutdown_timeout=core_config.PROCESS_RUNNER_SHUTDOWN_TIMEOUT,
            )
            api_executor_shutdown.shutdown(wait=True, cancel_futures=True)
            scan_executor_shutdown.shutdown(wait=True, cancel_futures=True)
            print("INFO:     ProcessTaskRunner и пулы потоков завершены")
        except Exception as e:
            print(f"  ✗ Ошибка завершения пулов: {e}")

        try:
            components_shutdown["db"].close()
            print("INFO:     Соединение с БД закрыто")
        except Exception as e:
            print(f"  ✗ Ошибка закрытия БД: {e}")

        event_bus_shutdown.publish(
            ApplicationStopped(
                correlation_id=str(uuid.uuid4()),
                source="Lifespan",
                stage="shutdown",
            )
        )

        try:
            await logging_subscriber_shutdown.stop()
            print("INFO:     Подписчик логирования остановлен")
        except Exception as e:
            print(f"  ✗ Ошибка остановки подписчика логирования: {e}")

        try:
            await event_bus_shutdown.stop()
            print("INFO:     Шина событий остановлена")
        except Exception as e:
            print(f"  ✗ Ошибка остановки шины событий: {e}")

        print("INFO:     DDS остановлен.")

    app = FastAPI(
        title="Deep Doc Search",
        description="Система поиска и анализа рабочей документации",
        version="1.0.0",
        lifespan=lifespan,
    )

    # ── Монтирование статических файлов ──
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # ── Middleware для условной публикации HTTP-событий ──
    # Добавляется первым → становится innermost. Выполняется
    # вторым на каждом запросе.
    @app.middleware("http")
    async def events_middleware(request: Request, call_next):
        event_bus = request.app.state.event_bus
        publish_http = event_bus.has_subscribers_for_prefix("http.")
        start_time = time.monotonic()

        if publish_http:
            event_bus.publish(
                RequestStarted(
                    method=request.method,
                    path=request.url.path,
                )
            )

        response = await call_next(request)

        if publish_http:
            duration_ms = (time.monotonic() - start_time) * 1000
            event_bus.publish(
                RequestCompleted(
                    method=request.method,
                    path=request.url.path,
                    status_code=response.status_code,
                    duration_ms=duration_ms,
                )
            )

        return response

    # ── Middleware автологина по IP-адресу ──
    # Добавляется вторым → становится outermost. Выполняется
    # первым на каждом запросе. Зависимости (config, auth,
    # event_bus) читает из request.app.state в момент обработки,
    # поскольку регистрация middleware возможна только до старта
    # приложения, а компоненты создаются в lifespan startup.
    app.middleware("http")(auto_login_middleware)

    # ── Exception handler для LoginRequiredException ──
    @app.exception_handler(LoginRequiredException)
    async def login_required_handler(
        request: Request,  # noqa: ARG001 — контракт FastAPI exception_handler
        exc: LoginRequiredException,
    ) -> RedirectResponse:
        redirect_path = exc.redirect_path
        if not is_safe_redirect_path(redirect_path):
            redirect_path = "/"
        safe_path = quote(redirect_path, safe="/")
        login_url = f"/login?next={safe_path}"
        return RedirectResponse(url=login_url, status_code=303)

    # Подключить API routes и страницы
    app.include_router(api_router)
    app.include_router(pages_router)

    return app
