"""
Управление жизненным циклом модулей DDS.

Этот модуль содержит класс ``ModuleLifecycle``, который оркестрирует
полный жизненный цикл модулей: инициализацию, обработку документов
на вторичной фазе, деактивацию и завершение работы.

Модуль находится в application layer и использует ``ModuleLoader``
и ``DependencyChecker`` для загрузки и проверки модулей.

Порядок работы:

1. Сканирование каталога модулей.
2. Проверка зависимостей (``DependencyChecker``).
3. Загрузка модулей в порядке топологической сортировки.
4. Первичная фаза сканирования (индексирование).
5. Вторичная фаза: для каждого модуля с ``scan_phase="secondary"``
   вызывается обработка документов.
6. Завершение работы: ``shutdown`` всех модулей.

Публичный доступ к обновлению статуса (скорректированный план):

Для обновления статуса модуля в реестре используется публичный
метод ``ModuleLoader.update_module_status()``. Приватный метод
``_update_module_status()`` удалён.

Публичный доступ к полной информации о модулях (Фаза 8):

Для получения полной информации о всех модулях реестра
(``version``, ``api_version``, ``status``, ``scan_phase``,
``description``) используется публичный метод
``get_all_module_info()``. Он используется веб-слоем
(``dds_web/api.py``) для отображения карточек модулей.

Удалённый метод ``get_all_module_statuses()`` (скорректированный план):

Ранее в классе был метод ``get_all_module_statuses()``,
возвращавший только ``{module_name: ModuleStatus}``. Он был удалён
как неиспользуемый: единственный потребитель
(``dds_web/api.py::list_modules``) использует
``get_all_module_info()``, возвращающий полный набор полей.
Соответствующий метод удалён и из протокола ``IModuleLifecycle``
(``dds_core/domain/interfaces.py``).

Событийная модель (Фаза 5, оптимизация):

Класс публикует только важные события через шину событий:

- ``ModuleLoaded`` — при успешной загрузке модуля.
- ``ModuleLoadFailed`` — при ошибке загрузки модуля.
- ``ModuleError`` — при ошибке обработки документа модулем.

Высокочастотные события (``ModuleProcessingStarted``,
``ModuleDocumentProcessed``) больше не публикуются для
снижения нагрузки на шину и логирование.

Принципы:

- Модуль находится в application layer и оркестрирует модули.
- Модуль зависит от абстракций (``IModule``, ``ISecondaryProcessor``,
  ``IEventBus``).
- Модуль не содержит бизнес-логики модулей.
- Модуль не выполняет логирование, но публикует важные события.
- Модуль взаимодействует с ``ModuleLoader`` через публичные методы,
  не нарушая инкапсуляцию.

Классы:
    ``ModuleLifecycle`` — управление жизненным циклом модулей.
"""

from __future__ import annotations

import time
import uuid

from ..domain import config
from ..domain.events import (
    ModuleError,
    ModuleLoaded,
    ModuleLoadFailed,
)
from ..domain.interfaces import IDatabase, IEventBus, ISecondaryProcessor
from ..domain.models import (
    DocumentInfo,
    ModuleConfig,
    ModuleInfo,
    ModuleManifest,
    ModuleStatus,
    ScanPhase,
)
from .dependency_checker import DependencyChecker
from .module_loader import ModuleLoader

# ----------------------------------------------------------------------
# Управление жизненным циклом модулей
# ----------------------------------------------------------------------


class ModuleLifecycle:
    """Управление жизненным циклом модулей DDS.

    Оркестрирует загрузку, инициализацию, обработку документов
    и завершение работы модулей.

    Публичный доступ к обновлению статуса (скорректированный план):

    Для обновления статуса модуля в реестре используется публичный
    метод ``ModuleLoader.update_module_status()``. Приватный метод
    ``_update_module_status()`` удалён.

    Пример использования::

        lifecycle = ModuleLifecycle(
            db=db_adapter,
            modules_directory="/path/to/dds_modules",
            dds_version="1.0.0",
            event_bus=event_bus,
        )

        lifecycle.initialize_all(module_configs)
        lifecycle.run_secondary_scans(documents)
        lifecycle.shutdown_all()

    Attributes:

    +-----------------------+----------------------------------------+
    | Атрибут               | Описание                               |
    +=======================+========================================+
    | ``_db``               | Абстракция базы данных.                |
    +-----------------------+----------------------------------------+
    | ``_loader``           | Загрузчик модулей.                     |
    +-----------------------+----------------------------------------+
    | ``_checker``          | Checker зависимостей.                  |
    +-----------------------+----------------------------------------+
    | ``_modules_directory``| Путь к каталогу модулей.               |
    +-----------------------+----------------------------------------+
    | ``_event_bus``        | Шина событий для публикации.           |
    +-----------------------+----------------------------------------+
    """

    def __init__(
        self,
        db: IDatabase,
        modules_directory: str,
        dds_version: str,
        event_bus: IEventBus,
        loader: ModuleLoader | None = None,
        checker: DependencyChecker | None = None,
    ) -> None:
        """Инициализирует менеджер жизненного цикла.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Сохранение ссылки на ``IDatabase``.                 |
        +---+-----------------------------------------------------+
        | 2 | Сохранение пути к каталогу модулей.                 |
        +---+-----------------------------------------------------+
        | 3 | Определение ``loader``: если передан, используется   |
        |   | он; иначе создаётся ``ModuleLoader(db,              |
        |   | modules_directory)``.                               |
        +---+-----------------------------------------------------+
        | 4 | Определение ``checker``: если передан, используется  |
        |   | он; иначе создаётся ``DependencyChecker(db,         |
        |   | dds_version)``.                                     |
        +---+-----------------------------------------------------+
        | 5 | Сохранение ссылки на ``IEventBus``.                 |
        +---+-----------------------------------------------------+

        Args:
            db: Реализация ``IDatabase`` для записи в ``module_registry``.
            modules_directory: Путь к каталогу модулей.
            dds_version: Текущая версия ядра DDS.
            event_bus: Шина событий для публикации важных событий.
            loader: Опциональный экземпляр ``ModuleLoader``. Если
                ``None``, создаётся автоматически.
            checker: Опциональный экземпляр ``DependencyChecker``.
                Если ``None``, создаётся автоматически.
        """
        self._db = db
        self._modules_directory = modules_directory
        self._loader = loader if loader is not None else ModuleLoader(db, modules_directory)
        self._checker = checker if checker is not None else DependencyChecker(db, dds_version)
        self._event_bus = event_bus

    def initialize_all(
        self,
        module_configs: dict[str, dict],
    ) -> tuple[list[str], list[str]]:
        """Инициализирует все модули.

        Сканирует каталог модулей, проверяет зависимости,
        загружает модули в порядке топологической сортировки.

        Публикует события ``ModuleLoaded`` при успешной загрузке
        и ``ModuleLoadFailed`` при ошибке.

        При ошибке загрузки модуля статус обновляется через
        публичный метод ``ModuleLoader.update_module_status()``.

        Операции:

        +----+----------------------------------------------------+
        | №  | Описание                                           |
        +====+====================================================+
        | 1  | ``self._loader.scan_modules_directory()`` →        |
        |    | список манифестов.                                 |
        +----+----------------------------------------------------+
        | 2  | Если манифестов нет → возврат ``([], [])``.        |
        +----+----------------------------------------------------+
        | 3  | ``self._checker.validate_all(manifests)`` →        |
        |    | ``(ordered, errors)``.                             |
        +----+----------------------------------------------------+
        | 4  | Если ``errors`` не пусты → возврат ``([], errors)``.|
        +----+----------------------------------------------------+
        | 5  | Для каждого манифеста в ``ordered``:               |
        |    | a. ``self._load_single_module(manifest, configs)`` |
        |    | b. При успехе:                                     |
        |    |    - Добавить в ``loaded``.                        |
        |    |    - Публикация ``ModuleLoaded``.                  |
        |    | c. При ``Exception``:                              |
        |    |    - Добавить в ``errors``.                        |
        |    |    - Обновление статуса через                      |
        |    |      ``self._loader.update_module_status()``.      |
        |    |    - Публикация ``ModuleLoadFailed``.              |
        +----+----------------------------------------------------+
        | 6  | Возврат ``(loaded, errors)``.                      |
        +----+----------------------------------------------------+

        Args:
            module_configs: Словарь конфигураций модулей.
                Ключ — ``module_name``, значение — словарь параметров
                из ``config.json``.

        Returns:
            Кортеж ``(список загруженных модулей, список ошибок)``.

        Пример::

            loaded = ["dds_stamp_extractor"]
            errors = ["Модуль 'dds_tqr_tracker': отсутствуют библиотеки: pandas"]
        """
        # Шаг 1: Сканирование каталога модулей
        manifests = self._loader.scan_modules_directory()

        if not manifests:
            return [], []

        # Шаг 2: Проверка зависимостей и топологическая сортировка
        ordered, errors = self._checker.validate_all(manifests)

        if errors:
            return [], errors

        # Шаг 3: Загрузка модулей в порядке сортировки
        loaded: list[str] = []

        for manifest in ordered:
            try:
                self._load_single_module(manifest, module_configs)
                loaded.append(manifest.module_name)

                self._event_bus.publish(
                    ModuleLoaded(
                        correlation_id=str(uuid.uuid4()),
                        source="ModuleLifecycle",
                        stage="initialization",
                        module_name=manifest.module_name,
                        version=manifest.version,
                    )
                )
            except Exception as e:
                error_msg = f"Модуль '{manifest.module_name}': {e}"
                errors.append(error_msg)

                # Обновление статуса через публичный метод
                self._loader.update_module_status(
                    manifest.module_name,
                    ModuleStatus.ERROR,
                    str(e),
                )

                self._event_bus.publish(
                    ModuleLoadFailed(
                        correlation_id=str(uuid.uuid4()),
                        source="ModuleLifecycle",
                        stage="initialization",
                        module_name=manifest.module_name,
                        error_message=error_msg,
                    )
                )

        return loaded, errors

    def shutdown_all(self) -> None:
        """Завершает работу всех загруженных модулей.

        Вызывает ``shutdown()`` для каждого загруженного модуля.
        Если ``shutdown()`` выбрасывает исключение, ошибка подавляется
        (вызывающим кодом), и модуль всё равно удаляется из реестра.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | ``self._loader.get_loaded_modules()`` → словарь.    |
        +---+-----------------------------------------------------+
        | 2 | Для каждого модуля:                                 |
        |   | ``self._loader.unload_module(module_name)``.        |
        +---+-----------------------------------------------------+

        Примечание:
            ``unload_module`` вызывает ``shutdown()`` модуля и
            обновляет статус в реестре через публичный метод
            ``update_module_status()``.
        """
        modules = self._loader.get_loaded_modules()

        for module_name in modules:
            self._loader.unload_module(module_name)

    def run_secondary_scans(
        self,
        documents: list[DocumentInfo],
        on_progress: object | None = None,
    ) -> dict[str, int]:
        """Запускает вторичную фазу сканирования для всех модулей.

        Для каждого модуля с ``scan_phase="secondary"`` вызывает
        фильтрацию и обработку документов. Отслеживает ошибки
        и деактивирует модули при превышении порога.

        Публикует событие ``ModuleError`` при ошибке обработки
        документа модулем. Высокочастотные события прогресса
        не публикуются.

        Операции:

        +----+----------------------------------------------------+
        | №  | Описание                                           |
        +====+====================================================+
        | 1  | ``self._loader.get_loaded_modules()`` → словарь.   |
        +----+----------------------------------------------------+
        | 2  | Для каждого модуля:                                |
        |    | a. Проверка ``isinstance(module,                   |
        |    |    ISecondaryProcessor)`` → пропуск, если нет.     |
        |    | b. Проверка ``info.scan_phase ==                   |
        |    |    ScanPhase.SECONDARY`` → пропуск, если нет.      |
        |    | c. Вызов ``_run_secondary_scan_for_module()``.     |
        |    | d. Сохранение результата.                          |
        +----+----------------------------------------------------+
        | 3  | Возврат словаря результатов.                       |
        +----+----------------------------------------------------+

        Args:
            documents: Список документов для обработки.
            on_progress: Опциональный callback для обновления
                прогресса. Вызывается с параметрами
                ``(module_name, processed, total, current_doc_id)``.

        Returns:
            Словарь результатов. Ключ — ``module_name``,
            значение — количество обработанных документов.
        """
        results: dict[str, int] = {}
        modules = self._loader.get_loaded_modules()

        for module_name, module in modules.items():
            # Проверить, реализует ли модуль ISecondaryProcessor
            if not isinstance(module, ISecondaryProcessor):
                continue

            # Проверить scan_phase модуля
            info = module.get_module_info()
            if info.scan_phase != ScanPhase.SECONDARY:
                continue

            processed = self._run_secondary_scan_for_module(
                module_name=module_name,
                processor=module,
                documents=documents,
                on_progress=on_progress,
            )

            results[module_name] = processed

        return results

    def get_module_status(self, module_name: str) -> ModuleStatus:
        """Возвращает статус модуля из реестра.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | ``SELECT status FROM module_registry WHERE          |
        |   | module_name = ?``.                                  |
        +---+-----------------------------------------------------+
        | 2 | Если результат найден → попытка преобразования      |
        |   | в ``ModuleStatus``.                                 |
        +---+-----------------------------------------------------+
        | 3 | При ``ValueError`` → возврат                       |
        |   | ``ModuleStatus.REMOVED``.                           |
        +---+-----------------------------------------------------+
        | 4 | Если результат не найден → возврат                 |
        |   | ``ModuleStatus.REMOVED``.                           |
        +---+-----------------------------------------------------+

        Args:
            module_name: Имя модуля.

        Returns:
            Статус модуля. ``ModuleStatus.REMOVED``, если модуль
            не найден в реестре.
        """
        results = self._db.execute(
            "SELECT status FROM module_registry WHERE module_name = ?",
            (module_name,),
        )

        if results:
            try:
                return ModuleStatus(str(results[0][0]))
            except ValueError:
                return ModuleStatus.REMOVED

        return ModuleStatus.REMOVED

    def get_all_module_info(self) -> dict[str, ModuleInfo]:
        """Возвращает полную информацию о всех модулях из реестра.

        Читает все поля (``module_name``, ``version``, ``api_version``,
        ``status``, ``scan_phase``, ``description``) из таблицы
        ``module_registry`` и формирует словарь :class:`ModuleInfo`.
        Используется веб-слоем для отображения карточек модулей
        с полной информацией (например, в API-эндпоинтах
        ``/api/modules`` и ``/api/modules/{name}``).

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | ``SELECT module_name, version, api_version, status, |
        |   | scan_phase, description FROM module_registry``.     |
        +---+-----------------------------------------------------+
        | 2 | Для каждой строки:                                  |
        |   | a. Попытка преобразования ``status`` в              |
        |   |    ``ModuleStatus``. При ``ValueError`` →           |
        |   |    ``ModuleStatus.REMOVED``.                        |
        |   | b. Попытка преобразования ``scan_phase`` в          |
        |   |    ``ScanPhase``. При ``ValueError`` →              |
        |   |    ``ScanPhase.SECONDARY``.                         |
        |   | c. Формирование :class:`ModuleInfo` с полями из     |
        |   |    строки реестра.                                  |
        +---+-----------------------------------------------------+
        | 3 | Возврат словаря ``{module_name: ModuleInfo}``.      |
        +---+-----------------------------------------------------+

        Returns:
            Словарь: ключ — ``module_name``, значение —
            :class:`ModuleInfo` с полями ``version``,
            ``api_version``, ``status``, ``scan_phase``,
            ``description``. Пустой словарь, если реестр пуст.
        """
        results = self._db.execute(
            "SELECT module_name, version, api_version, status, "
            "       scan_phase, description "
            "FROM module_registry"
        )

        infos: dict[str, ModuleInfo] = {}

        for row in results:
            name = str(row[0])

            try:
                status = ModuleStatus(str(row[3]))
            except ValueError:
                status = ModuleStatus.REMOVED

            try:
                scan_phase = ScanPhase(str(row[4]))
            except ValueError:
                scan_phase = ScanPhase.SECONDARY

            infos[name] = ModuleInfo(
                module_name=name,
                version=str(row[1]),
                api_version=str(row[2]),
                status=status,
                scan_phase=scan_phase,
                description=str(row[5]),
            )

        return infos

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _load_single_module(
        self,
        manifest: ModuleManifest,
        module_configs: dict[str, dict],
    ) -> None:
        """Загружает один модуль с повторными попытками.

        Операции:

        +----+----------------------------------------------------+
        | №  | Описание                                           |
        +====+====================================================+
        | 1  | Формирование ``ModuleConfig`` из параметров.       |
        +----+----------------------------------------------------+
        | 2  | Цикл повторных попыток                             |
        |    | (``config.MODULE_LOAD_RETRY_COUNT``):              |
        |    | a. ``self._loader.load_module(manifest, config)``. |
        |    | b. При успехе → ``return``.                        |
        |    | c. При ошибке → сохранение ошибки, ожидание,       |
        |    |    следующая попытка.                              |
        +----+----------------------------------------------------+
        | 3  | Если все попытки исчерпаны → проброс последней     |
        |    | ошибки.                                            |
        +----+----------------------------------------------------+

        Примечание:
            Между попытками выполняется ожидание
            ``config.MODULE_LOAD_RETRY_INTERVAL_SECONDS`` секунд.

        Args:
            manifest: Манифест модуля.
            module_configs: Словарь конфигураций модулей.

        Raises:
            Exception: Если загрузка не удалась после всех попыток.
        """
        params = module_configs.get(manifest.module_name, {})

        module_config = ModuleConfig(
            db_path=self._db.get_db_path(),
            parameters=params,
            event_bus=self._event_bus,  # передаём шину модулю
        )

        last_error: Exception | None = None

        for attempt in range(config.MODULE_LOAD_RETRY_COUNT):
            try:
                self._loader.load_module(manifest, module_config)
                return
            except Exception as e:
                last_error = e
                if attempt < config.MODULE_LOAD_RETRY_COUNT - 1:
                    time.sleep(config.MODULE_LOAD_RETRY_INTERVAL_SECONDS)

        if last_error is not None:
            raise last_error

    def _run_secondary_scan_for_module(
        self,
        module_name: str,
        processor: ISecondaryProcessor,
        documents: list[DocumentInfo],
        on_progress: object | None = None,
    ) -> int:
        """Запускает вторичную фазу для одного модуля.

        Операции:

        +----+----------------------------------------------------+
        | №  | Описание                                           |
        +====+====================================================+
        | 1  | ``processor.filter_documents(documents)`` →        |
        |    | список ``doc_id``.                                 |
        +----+----------------------------------------------------+
        | 2  | Если список пуст → возврат ``0``.                  |
        +----+----------------------------------------------------+
        | 3  | Формирование словаря ``doc_map`` для быстрого      |
        |    | доступа к ``DocumentInfo`` по ``doc_id``.          |
        +----+----------------------------------------------------+
        | 4  | Для каждого ``doc_id`` в списке:                   |
        |    | a. Поиск ``DocumentInfo`` в ``doc_map``.           |
        |    | b. Если не найден → пропуск.                       |
        |    | c. ``processor.process_document(...)``.             |
        |    | d. При успехе:                                     |
        |    |    - Сброс ``consecutive_errors``.                 |
        |    |    - Инкремент ``processed``.                      |
        |    | e. При ``Exception``:                              |
        |    |    - Инкремент ``consecutive_errors``.             |
        |    |    - Публикация ``ModuleError``.                   |
        |    |    - Если ``consecutive_errors >=                  |
        |    |      MAX_CONSECUTIVE_ERRORS``:                     |
        |    |      - Обновление статуса через                    |
        |    |        ``self._loader.update_module_status()``.    |
        |    |      - ``processor.request_shutdown()``.           |
        |    |      - ``break``.                                  |
        |    | f. Вызов ``on_progress`` callback (если задан).    |
        +----+----------------------------------------------------+
        | 5  | Возврат ``processed``.                             |
        +----+----------------------------------------------------+

        Примечание:
            При достижении порога ``MAX_CONSECUTIVE_ERRORS``
            модуль деактивируется через публичный метод
            ``ModuleLoader.update_module_status()``, после чего
            обработка прерывается.

        Args:
            module_name: Имя модуля.
            processor: Модуль, реализующий ``ISecondaryProcessor``.
            documents: Список документов для обработки.
            on_progress: Опциональный callback для обновления прогресса.

        Returns:
            Количество обработанных документов.
        """
        # Фильтрация документов
        doc_ids = processor.filter_documents(documents)

        if not doc_ids:
            return 0

        # Создать словарь doc_id -> DocumentInfo для быстрого доступа
        doc_map = {d.doc_id: d for d in documents}

        processed = 0
        consecutive_errors = 0

        for doc_id in doc_ids:
            doc_info = doc_map.get(doc_id)
            if doc_info is None:
                continue

            try:
                processor.process_document(
                    doc_id=doc_info.doc_id,
                    file_path=doc_info.file_path,
                    file_hash=doc_info.file_hash,
                )
                consecutive_errors = 0
                processed += 1

            except Exception as e:
                consecutive_errors += 1

                # Публикация события об ошибке
                self._event_bus.publish(
                    ModuleError(
                        correlation_id=str(uuid.uuid4()),
                        source="ModuleLifecycle",
                        stage="secondary_scan",
                        module_name=module_name,
                        error_type=type(e).__name__,
                        error_message=str(e),
                    )
                )

                if consecutive_errors >= config.MAX_CONSECUTIVE_ERRORS:
                    # Деактивировать модуль через публичный метод
                    self._loader.update_module_status(
                        module_name,
                        ModuleStatus.ERROR,
                        f"Деактивирован: {consecutive_errors} ошибок подряд.",
                    )

                    try:
                        processor.request_shutdown()
                    except Exception:
                        pass

                    break

            # Обновление прогресса
            if on_progress is not None and callable(on_progress):
                on_progress(module_name, processed, len(doc_ids), doc_id)

        return processed
