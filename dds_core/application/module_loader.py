"""
Загрузчик модулей DDS.

Этот модуль содержит класс ``ModuleLoader``, который отвечает за
обнаружение, валидацию и загрузку модулей DDS из каталога ``dds_modules/``.

Алгоритм загрузки модуля:

1. Сканирование каталога модулей.
2. Чтение и валидация ``manifest.json``.
3. Проверка уникальности ``module_name``.
4. Проверка совместимости версий.
5. Проверка внешних библиотек.
6. Импорт ``entry_point``.
7. Создание экземпляра модуля.
8. Подготовка конфигурации.
9. Инициализация модуля.
10. Проверка ``managed_tables``.
11. Применение миграций.
12. Регистрация в ``module_registry``.

Публичный доступ к обновлению статуса (скорректированный план):

Для обновления статуса модуля в реестре используется публичный
метод ``update_module_status()``. Приватный метод
``_update_module_status()`` был удалён, так как дублировал
публичный метод и не вызывался.

Встроенные состояния (скорректированный план):

- ``LOADING`` — записывается в начале ``load_module`` перед
  фактической загрузкой, чтобы отразить, что модуль находится
  в процессе загрузки.
- ``MIGRATION_ERROR`` — записывается в ``_apply_migrations``
  при ошибке применения миграции.

Удалённый метод ``get_loaded_module()`` (скорректированный план,
шаг 4.2 рефакторинга v5.0):

Ранее в классе был публичный метод ``get_loaded_module(name)``,
возвращавший единственный загруженный модуль по имени или
``None``. Он был удалён как неиспользуемый: единственный
потребитель в application layer (``ModuleLifecycle``) использует
``get_loaded_modules()`` (plural), возвращающий весь словарь
загруженных модулей. Дополнительно, ``get_loaded_module``
не использовался ни в presentation layer, ни в тестах.
Удаление устраняет мёртвый код.

Принципы:
- Модуль находится в application layer и оркестрирует загрузку.
- Модуль зависит от абстракции ``IDatabase`` для записи в реестр.
- Модуль не содержит бизнес-логики модулей.
- Модуль не выполняет логирование. Логирование — ответственность
  presentation layer.

Классы:
    ``ModuleLoader`` — загрузчик модулей.
"""

from __future__ import annotations

import importlib
import json
import os

from ..domain.interfaces import IDatabase, IModule
from ..domain.models import (
    ModuleConfig,
    ModuleDependency,
    ModuleManifest,
    ModuleStatus,
    ScanPhase,
)

# ----------------------------------------------------------------------
# Загрузчик модулей
# ----------------------------------------------------------------------


class ModuleLoader:
    """Загрузчик модулей DDS.

    Обнаруживает модули в каталоге ``dds_modules/``, валидирует
    манифесты, загружает модули и регистрирует их в БД.

    Публичный доступ к обновлению статуса (скорректированный план):

    Для обновления статуса модуля в реестре ``module_registry``
    используется публичный метод ``update_module_status()``.
    Приватный метод ``_update_module_status()`` удалён.

    Публичный доступ к загруженным модулям:

    Метод ``get_loaded_modules()`` (plural) возвращает копию
    словаря всех загруженных модулей. Ранее существовавший
    метод ``get_loaded_module(name)`` (singular) удалён в шаге 4.2
    рефакторинга v5.0 как неиспользуемый.

    Пример использования::

        loader = ModuleLoader(db_adapter, "/path/to/dds_modules")

        manifests = loader.scan_modules_directory()

        for manifest in manifests:
            module = loader.load_module(manifest, module_config)

        # Получение всех загруженных модулей.
        modules = loader.get_loaded_modules()

        # Обновление статуса модуля
        loader.update_module_status(
            "dds_stamp_extractor",
            ModuleStatus.ERROR,
            "Ошибка при обработке документа.",
        )

        # Выгрузка модуля с указанием финального статуса
        loader.unload_module("dds_stamp_extractor", final_status=ModuleStatus.DEACTIVATED)

    Attributes:

    +---------------------+------------------------------------------+
    | Атрибут             | Описание                                 |
    +=====================+==========================================+
    | ``_db``             | Абстракция базы данных для записи        |
    |                     | в ``module_registry``.                   |
    +---------------------+------------------------------------------+
    | ``_modules_directory`` | Путь к каталогу модулей.              |
    +---------------------+------------------------------------------+
    | ``_loaded_modules`` | Словарь загруженных модулей.             |
    |                     | Ключ — ``module_name``, значение —       |
    |                     | экземпляр модуля.                        |
    +---------------------+------------------------------------------+
    """

    def __init__(
        self,
        db: IDatabase,
        modules_directory: str,
    ) -> None:
        """Инициализирует загрузчик модулей.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Сохранение ссылки на ``IDatabase``.                 |
        +---+-----------------------------------------------------+
        | 2 | Сохранение пути к каталогу модулей.                 |
        +---+-----------------------------------------------------+
        | 3 | Инициализация пустого словаря ``_loaded_modules``.  |
        +---+-----------------------------------------------------+

        Args:
            db: Реализация ``IDatabase`` для записи в ``module_registry``.
            modules_directory: Путь к каталогу модулей.
        """
        self._db = db
        self._modules_directory = modules_directory
        self._loaded_modules: dict[str, IModule] = {}

    # ------------------------------------------------------------------
    # Сканирование каталога модулей
    # ------------------------------------------------------------------

    def scan_modules_directory(self) -> list[ModuleManifest]:
        """Сканирует каталог модулей и возвращает список манифестов.

        Обходит подкаталоги каталога модулей и читает ``manifest.json``
        из каждого. Манифесты валидируются перед возвратом.

        Операции:

        +----+----------------------------------------------------+
        | №  | Описание                                           |
        +====+====================================================+
        | 1  | Проверка, что ``_modules_directory`` является       |
        |    | каталогом. Если нет → возврат пустого списка.       |
        +----+----------------------------------------------------+
        | 2  | ``sorted(os.listdir(...))`` → обход подкаталогов    |
        |    | в алфавитном порядке (детерминизм).                 |
        +----+----------------------------------------------------+
        | 3  | Для каждого подкаталога:                            |
        |    | a. Проверка, что это каталог (не файл).             |
        |    | b. Проверка наличия ``manifest.json``.              |
        |    | c. Разбор манифеста через ``_parse_manifest()``.    |
        |    | d. При ошибке разбора → пропуск модуля.             |
        |    | e. Проверка уникальности ``module_name``.           |
        |    | f. Добавление манифеста в список.                   |
        +----+----------------------------------------------------+
        | 4  | Возврат списка манифестов.                          |
        +----+----------------------------------------------------+

        Примечание:
            Манифесты с ошибками разбора пропускаются молча.
            Логирование ошибок разбора — ответственность
            вызывающего кода (``ModuleLifecycle``).

        Returns:
            Список манифестов модулей. Пустой список, если каталог
            не существует или не содержит модулей.
        """
        if not os.path.isdir(self._modules_directory):
            return []

        manifests: list[ModuleManifest] = []
        seen_names: set[str] = set()

        for entry in sorted(os.listdir(self._modules_directory)):
            module_dir = os.path.join(self._modules_directory, entry)

            if not os.path.isdir(module_dir):
                continue

            manifest_path = os.path.join(module_dir, "manifest.json")

            if not os.path.isfile(manifest_path):
                continue

            try:
                manifest = self._parse_manifest(manifest_path)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                # Манифест невалиден — пропускаем модуль.
                # Логирование — ответственность вызывающего кода.
                continue

            # Проверка уникальности module_name
            if manifest.module_name in seen_names:
                continue

            seen_names.add(manifest.module_name)
            manifests.append(manifest)

        return manifests

    # ------------------------------------------------------------------
    # Загрузка и выгрузка модулей
    # ------------------------------------------------------------------

    def load_module(
        self,
        manifest: ModuleManifest,
        module_config: ModuleConfig,
    ) -> IModule:
        """Загружает и инициализирует модуль.

        Выполняет все шаги загрузки: импорт ``entry_point``,
        создание экземпляра, инициализация, проверка ``managed_tables``,
        применение миграций, регистрация в реестре.

        Перед началом загрузки в реестр записывается статус ``LOADING``.

        Операции:

        +----+----------------------------------------------------+
        | №  | Описание                                           |
        +====+====================================================+
        | 1  | Запись статуса ``LOADING`` в ``module_registry``.   |
        +----+----------------------------------------------------+
        | 2  | Импорт ``entry_point`` через                       |
        |    | ``_import_entry_point()`` → класс модуля.           |
        +----+----------------------------------------------------+
        | 3  | Создание экземпляра модуля.                        |
        +----+----------------------------------------------------+
        | 4  | Вызов ``module.initialize(module_config)``.         |
        |    | При ошибке → откат через ``_rollback_module()``,    |
        |    | выброс ``RuntimeError``.                            |
        +----+----------------------------------------------------+
        | 5  | Проверка ``managed_tables``: для каждого объекта    |
        |    | → ``_db.table_exists()``. При отсутствии →          |
        |    | откат, выброс ``RuntimeError``.                     |
        +----+----------------------------------------------------+
        | 6  | Применение миграций через ``_apply_migrations()``.  |
        +----+----------------------------------------------------+
        | 7  | Регистрация в ``module_registry`` через             |
        |    | ``_register_module()`` со статусом ``ACTIVE``.      |
        +----+----------------------------------------------------+
        | 8  | Сохранение в ``_loaded_modules``.                  |
        +----+----------------------------------------------------+
        | 9  | Возврат экземпляра модуля.                         |
        +----+----------------------------------------------------+

        Args:
            manifest: Манифест модуля.
            module_config: Конфигурация модуля.

        Returns:
            Загруженный и инициализированный экземпляр модуля.

        Raises:
            ImportError: Если ``entry_point`` не может быть импортирован.
            RuntimeError: Если инициализация не удалась или
                проверка ``managed_tables`` не прошла.
        """
        # Зафиксировать начало загрузки
        self.update_module_status(manifest.module_name, ModuleStatus.LOADING)

        # Шаг 2: Импорт entry_point
        module_class = self._import_entry_point(manifest.entry_point)

        # Шаг 3: Создание экземпляра
        module = module_class()

        # Шаг 4: Инициализация
        try:
            module.initialize(module_config)
        except Exception:
            # Откат: попытка cleanup
            self._rollback_module(module, manifest)
            raise RuntimeError(f"Модуль '{manifest.module_name}': ошибка инициализации.")

        # Шаг 5: Проверка managed_tables
        managed_tables = module.get_managed_tables()
        for table_name in managed_tables:
            if not self._db.table_exists(table_name):
                self._rollback_module(module, manifest)
                raise RuntimeError(
                    f"Модуль '{manifest.module_name}': объект БД "
                    f"'{table_name}' не найден после инициализации."
                )

        # Шаг 6: Применение миграций
        self._apply_migrations(module, manifest)

        # Шаг 7: Регистрация в реестре
        self._register_module(manifest, ModuleStatus.ACTIVE)

        self._loaded_modules[manifest.module_name] = module
        return module

    def unload_module(
        self,
        module_name: str,
        final_status: ModuleStatus = ModuleStatus.SHUTDOWN,
    ) -> None:
        """Отключает модуль.

        Вызывает ``shutdown()`` модуля и удаляет его из реестра
        с указанным финальным статусом (по умолчанию ``SHUTDOWN``).
        Если ``shutdown()`` выбрасывает исключение, ошибка подавляется
        (модуль всё равно удаляется из реестра).

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Поиск модуля в ``_loaded_modules``.                 |
        +---+-----------------------------------------------------+
        | 2 | Если модуль найден:                                 |
        |   | a. Вызов ``module.shutdown()``.                     |
        |   | b. При ошибке → подавление (модуль всё равно        |
        |   |    выгружается).                                    |
        |   | c. Удаление из ``_loaded_modules``.                 |
        +---+-----------------------------------------------------+
        | 3 | Обновление статуса в реестре через                  |
        |   | ``update_module_status()`` → ``final_status``.      |
        +---+-----------------------------------------------------+

        Args:
            module_name: Имя модуля для отключения.
            final_status: Финальный статус, записываемый в реестр.
                По умолчанию ``ModuleStatus.SHUTDOWN``.
        """
        module = self._loaded_modules.get(module_name)

        if module is not None:
            try:
                module.shutdown()
            except Exception:
                # Подавление ошибки: модуль выгружается
                # независимо от результата shutdown.
                pass

            del self._loaded_modules[module_name]

        self.update_module_status(module_name, final_status)

    def get_loaded_modules(self) -> dict[str, IModule]:
        """Возвращает словарь всех загруженных модулей.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Создание копии словаря ``_loaded_modules``          |
        |   | (защита от модификации внешним кодом).              |
        +---+-----------------------------------------------------+
        | 2 | Возврат копии.                                      |
        +---+-----------------------------------------------------+

        Примечание:
            Метод возвращает **копию** словаря, чтобы вызывающий
            код не мог случайно повредить внутреннее состояние
            загрузчика. Экземпляры модулей при этом передаются
            по ссылке — их изменение видно всем потребителям,
            но это соответствует контракту ``IModule`` (модуль
            управляет своим состоянием сам).

            Ранее существовал метод ``get_loaded_module(name)``
            (singular), возвращавший один модуль. Он удалён в
            шаге 4.2 рефакторинга v5.0 как неиспользуемый.

        Returns:
            Словарь модулей. Ключ — ``module_name``, значение —
            экземпляр модуля. Пустой словарь, если модули
            не загружены.
        """
        return dict(self._loaded_modules)

    # ------------------------------------------------------------------
    # Публичное обновление статуса модуля
    # ------------------------------------------------------------------

    def update_module_status(
        self,
        module_name: str,
        status: ModuleStatus,
        error_message: str = "",
    ) -> None:
        """Обновляет статус модуля в ``module_registry``.

        Публичный метод для обновления статуса модуля в реестре.
        Внешние компоненты (например, ``ModuleLifecycle``) должны
        использовать этот метод вместо приватного
        ``_update_module_status()``.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | ``UPDATE module_registry SET status = ?,             |
        |   | error_message = ? WHERE module_name = ?``.           |
        +---+-----------------------------------------------------+

        Примечание:
            Если модуль не найден в реестре, операция ``UPDATE``
            не изменяет ни одной записи и не выбрасывает
            исключение. Это поведение допустимо: обновление
            статуса несуществующего модуля является холостой
            операцией.

            Метод идемпотентен: повторный вызов с теми же
            параметрами не изменяет данные.

        Потокобезопасность:
            Операция записи сериализуется ``SQLiteAdapter``
            через ``threading.Lock``.

        Args:
            module_name: Имя модуля.
            status: Новый статус модуля.
            error_message: Сообщение об ошибке (если есть).
                Пустая строка по умолчанию.
        """
        self._db.execute_write(
            "UPDATE module_registry SET status = ?, error_message = ? WHERE module_name = ?",
            (status.value, error_message, module_name),
        )

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _parse_manifest(self, manifest_path: str) -> ModuleManifest:
        """Разбирает ``manifest.json``.

        Операции:

        +----+----------------------------------------------------+
        | №  | Описание                                           |
        +====+====================================================+
        | 1  | ``open(manifest_path, "r")`` → ``json.load()``.     |
        +----+----------------------------------------------------+
        | 2  | Извлечение обязательных полей:                     |
        |    | ``module_name``, ``version``, ``api_version``,      |
        |    | ``entry_point``, ``scan_phase``.                    |
        +----+----------------------------------------------------+
        | 3  | Извлечение зависимостей из ``dependencies``:        |
        |    | ``dds_core``, ``external_libraries``,               |
        |    | ``other_modules``.                                  |
        +----+----------------------------------------------------+
        | 4  | Валидация: если ``managed_tables`` не пуст,         |
        |    | ``data_version`` должен быть > 0.                   |
        +----+----------------------------------------------------+
        | 5  | Формирование и возврат ``ModuleManifest``.          |
        +----+----------------------------------------------------+

        Args:
            manifest_path: Путь к файлу ``manifest.json``.

        Returns:
            ``ModuleManifest`` с данными манифеста.

        Raises:
            ValueError: Если обязательное поле отсутствует или
                имеет неверный тип.
            json.JSONDecodeError: Если файл не является валидным JSON.
            KeyError: Если обязательный ключ отсутствует.
        """
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)

        # Обязательные поля
        module_name = data["module_name"]
        version = data["version"]
        api_version = data["api_version"]
        entry_point = data["entry_point"]
        scan_phase_str = data.get("scan_phase", "secondary")
        scan_phase = ScanPhase(scan_phase_str)

        # Зависимости
        deps = data.get("dependencies", {})
        dds_core_version = deps.get("dds_core", ">=1.0.0")
        external_libraries = deps.get("external_libraries", [])

        other_modules: list[ModuleDependency] = []
        for dep in deps.get("other_modules", []):
            if isinstance(dep, dict):
                other_modules.append(
                    ModuleDependency(
                        module_name=dep["module_name"],
                        api_version=dep.get("api_version", ""),
                    )
                )
            elif isinstance(dep, str):
                other_modules.append(
                    ModuleDependency(
                        module_name=dep,
                    )
                )

        # Валидация взаимосвязей
        managed_tables = data.get("managed_tables", [])
        data_version = data.get("data_version", 0)

        if managed_tables and data_version == 0:
            raise ValueError(
                f"Модуль '{module_name}': managed_tables не пуст, но data_version не указан."
            )

        return ModuleManifest(
            module_name=str(module_name),
            version=str(version),
            api_version=str(api_version),
            entry_point=str(entry_point),
            scan_phase=scan_phase,
            description=str(data.get("description", "")),
            dds_core_version=str(dds_core_version),
            external_libraries=list(external_libraries),
            other_modules=other_modules,
            managed_tables=list(managed_tables),
            data_version=int(data_version),
            config_schema=data.get("config_schema", {}),
        )

    def _import_entry_point(self, entry_point: str) -> type:
        """Импортирует класс модуля по ``entry_point``.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | ``entry_point.rsplit(".", 1)`` → разделение на      |
        |   | путь модуля и имя класса.                           |
        +---+-----------------------------------------------------+
        | 2 | Проверка формата: если частей не 2 → ``ImportError``.|
        +---+-----------------------------------------------------+
        | 3 | ``importlib.import_module(module_path)`` → объект   |
        |   | модуля.                                             |
        +---+-----------------------------------------------------+
        | 4 | ``getattr(module_obj, class_name)`` → класс модуля. |
        +---+-----------------------------------------------------+

        Args:
            entry_point: Точка входа в формате ``"package.module.Class"``.

        Returns:
            Класс модуля.

        Raises:
            ImportError: Если импорт не удался или формат неверный.
        """
        parts = entry_point.rsplit(".", 1)

        if len(parts) != 2:
            raise ImportError(
                f"Неверный формат entry_point: '{entry_point}'. Ожидается 'package.module.Class'."
            )

        module_path, class_name = parts
        module_obj = importlib.import_module(module_path)
        return getattr(module_obj, class_name)

    def _rollback_module(
        self,
        module: IModule,
        manifest: ModuleManifest,
    ) -> None:
        """Откатывает изменения модуля при ошибке загрузки.

        Вызывает ``cleanup()`` модуля. Если ``cleanup()`` не удался,
        удаляет объекты из ``managed_tables`` напрямую через
        ``DROP TABLE IF EXISTS``.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Попытка вызова ``module.cleanup()``.                |
        +---+-----------------------------------------------------+
        | 2 | При ошибке ``cleanup()``:                           |
        |   | a. Для каждого объекта в ``manifest.managed_tables``|
        |   |    → ``DROP TABLE IF EXISTS {table_name}``.         |
        |   | b. Ошибки удаления подавляются.                     |
        +---+-----------------------------------------------------+

        Args:
            module: Экземпляр модуля.
            manifest: Манифест модуля.
        """
        try:
            module.cleanup()
        except Exception:
            # cleanup() не удался — удаляем таблицы напрямую
            for table_name in manifest.managed_tables:
                try:
                    self._db.execute_write(f"DROP TABLE IF EXISTS {table_name}")
                except Exception:
                    pass

    def _apply_migrations(
        self,
        module: IModule,
        manifest: ModuleManifest,
    ) -> None:
        """Применяет миграции данных модуля.

        Сравнивает ``data_version`` в ``manifest.json`` с версией
        в БД и применяет необходимые миграции. Каждая миграция
        выполняется как отдельный запрос.

        При ошибке миграции в реестр записывается статус
        ``MIGRATION_ERROR`` перед выбрасыванием исключения.

        Операции:

        +----+----------------------------------------------------+
        | №  | Описание                                           |
        +====+====================================================+
        | 1  | Если ``managed_tables`` пуст → выход.              |
        +----+----------------------------------------------------+
        | 2  | ``module.get_migrations()`` → словарь миграций.    |
        +----+----------------------------------------------------+
        | 3  | Если миграций нет → выход.                         |
        +----+----------------------------------------------------+
        | 4  | Чтение текущей ``data_version`` из                 |
        |    | ``module_registry``.                               |
        +----+----------------------------------------------------+
        | 5  | Для каждой миграции (по возрастанию версии):       |
        |    | a. Если целевая версия <= текущей → пропуск.       |
        |    | b. Выполнение SQL миграции.                        |
        |    | c. При ошибке → запись ``MIGRATION_ERROR``,        |
        |    |    выброс ``RuntimeError``.                        |
        +----+----------------------------------------------------+

        Args:
            module: Экземпляр модуля.
            manifest: Манифест модуля.

        Raises:
            RuntimeError: Если миграция не удалась.
        """
        if not manifest.managed_tables:
            return

        migrations = module.get_migrations()

        if not migrations:
            return

        # Получить текущую версию данных модуля из БД
        results = self._db.execute(
            "SELECT data_version FROM module_registry WHERE module_name = ?",
            (manifest.module_name,),
        )
        current_version = int(results[0][0]) if results else 0

        # Применить миграции
        for target_version in sorted(migrations.keys()):
            if target_version <= current_version:
                continue

            try:
                self._db.execute_write(migrations[target_version])
            except Exception:
                # Записать статус MIGRATION_ERROR
                self.update_module_status(
                    manifest.module_name,
                    ModuleStatus.MIGRATION_ERROR,
                    f"Ошибка миграции до версии {target_version}",
                )
                raise RuntimeError(
                    f"Модуль '{manifest.module_name}': ошибка миграции до версии {target_version}."
                )

    def _register_module(
        self,
        manifest: ModuleManifest,
        status: ModuleStatus,
    ) -> None:
        """Регистрирует модуль в ``module_registry``.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Сериализация ``managed_tables`` в JSON.             |
        +---+-----------------------------------------------------+
        | 2 | ``INSERT OR REPLACE INTO module_registry`` с        |
        |   | полями манифеста и статусом.                        |
        +---+-----------------------------------------------------+

        Примечание:
            Используется ``INSERT OR REPLACE``, что обеспечивает
            идемпотентность: повторная регистрация модуля
            обновляет существующую запись.

        Args:
            manifest: Манифест модуля.
            status: Статус модуля.
        """
        managed_tables_json = json.dumps(manifest.managed_tables)

        self._db.execute_write(
            "INSERT OR REPLACE INTO module_registry "
            "(module_name, version, api_version, status, scan_phase, "
            " description, managed_tables, data_version, error_message, "
            " initialized_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (
                manifest.module_name,
                manifest.version,
                manifest.api_version,
                status.value,
                manifest.scan_phase.value,
                manifest.description,
                managed_tables_json,
                manifest.data_version,
                "",
            ),
        )
