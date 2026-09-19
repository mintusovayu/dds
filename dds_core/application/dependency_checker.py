"""
Проверка зависимостей модулей DDS.

Этот модуль содержит класс DependencyChecker, который проверяет
зависимости модулей перед загрузкой: совместимость версий ядра DDS,
наличие внешних библиотек, наличие зависимых модулей. Также выполняет
топологическую сортировку модулей для определения порядка загрузки.

Принципы:
    - Модуль находится в application layer и оркестрирует проверки.
    - Модуль зависит от абстракции IDatabase для проверки реестра.
    - Модуль не содержит бизнес-логики модулей.
    - Модуль не выполняет логирование. Логирование — ответственность
      presentation layer.

Классы:
    DependencyChecker — проверка зависимостей модулей.
"""

from __future__ import annotations

import importlib
import re

from ..domain.interfaces import IDatabase
from ..domain.models import ModuleManifest

# ----------------------------------------------------------------------
# Проверка зависимостей
# ----------------------------------------------------------------------


class DependencyChecker:
    """Проверка зависимостей модулей DDS.

    Проверяет совместимость версий, наличие внешних библиотек
    и зависимых модулей. Выполняет топологическую сортировку
    для определения порядка загрузки.

    Attributes:
        _db: Абстракция базы данных для проверки реестра модулей.
        _dds_version: Текущая версия ядра DDS.
    """

    def __init__(self, db: IDatabase, dds_version: str) -> None:
        """Инициализирует checker зависимостей.

        Args:
            db: Реализация IDatabase для проверки module_registry.
            dds_version: Текущая версия ядра DDS.
                Например, "1.0.0".
        """
        self._db = db
        self._dds_version = dds_version

    def check_core_version(self, required_version: str) -> bool:
        """Проверяет совместимость версии ядра DDS.

        Сравнивает текущую версию DDS с требуемой версией
        из manifest.json модуля.

        Args:
            required_version: Требуемая версия в формате
                семантического версионирования с оператором
                сравнения. Например, ">=1.0.0", "==2.1.0", ">=1.0,<2.0".
                Пустая строка — любая версия.

        Returns:
            True, если текущая версия DDS удовлетворяет требованию.
            False в противном случае.
        """
        if not required_version:
            return True

        return self._check_version_constraint(
            self._dds_version,
            required_version,
        )

    def check_external_libraries(
        self,
        libraries: list[str],
    ) -> list[str]:
        """Проверяет наличие внешних библиотек.

        Пытается импортировать каждую библиотеку из списка.
        Возвращает список библиотек, которые не удалось импортировать.

        Args:
            libraries: Список имён библиотек для проверки.
                Например, ["pymupdf", "pillow"].

        Returns:
            Список отсутствующих библиотек. Пустой список,
            если все библиотеки установлены.
        """
        missing: list[str] = []

        for lib_name in libraries:
            try:
                importlib.import_module(lib_name)
            except ImportError:
                missing.append(lib_name)

        return missing

    def check_module_dependencies(
        self,
        manifests: list[ModuleManifest],
    ) -> list[str]:
        """Проверяет зависимости между модулями.

        Проверяет, что все модули, от которых зависят другие модули,
        присутствуют в списке манифестов.

        Args:
            manifests: Список манифестов модулей для проверки.

        Returns:
            Список отсутствующих модулей-зависимостей. Пустой список,
            если все зависимости удовлетворены.
        """
        available_names = {m.module_name for m in manifests}
        missing: list[str] = []

        for manifest in manifests:
            for dep in manifest.other_modules:
                if dep.module_name not in available_names:
                    missing.append(f"{manifest.module_name} -> {dep.module_name}")

        return missing

    def check_api_version_compatibility(
        self,
        manifests: list[ModuleManifest],
    ) -> list[str]:
        """Проверяет совместимость api_version между модулями.

        Для каждой зависимости проверяет, что api_version
        модуля-зависимости удовлетворяет требованию.

        Args:
            manifests: Список манифестов модулей для проверки.

        Returns:
            Список несовместимых зависимостей в формате
            "{модуль} -> {зависимость}: требуется {версия}, найдена {версия}".
            Пустой список, если все совместимо.
        """
        api_versions = {m.module_name: m.api_version for m in manifests}
        incompatible: list[str] = []

        for manifest in manifests:
            for dep in manifest.other_modules:
                if not dep.api_version:
                    continue

                dep_api_version = api_versions.get(dep.module_name, "")
                if not dep_api_version:
                    continue

                if not self._check_version_constraint(
                    dep_api_version,
                    dep.api_version,
                ):
                    incompatible.append(
                        f"{manifest.module_name} -> {dep.module_name}: "
                        f"требуется {dep.api_version}, "
                        f"найдена {dep_api_version}"
                    )

        return incompatible

    def resolve_load_order(
        self,
        manifests: list[ModuleManifest],
    ) -> list[ModuleManifest]:
        """Определяет порядок загрузки модулей.

        Выполняет топологическую сортировку модулей по зависимостям.
        Модули без зависимостей загружаются первыми.

        Args:
            manifests: Список манифестов модулей.

        Returns:
            Список манифестов в порядке загрузки.

        Raises:
            ValueError: Если обнаружен цикл зависимостей.
        """
        # Построить граф зависимостей
        name_to_manifest = {m.module_name: m for m in manifests}
        in_degree: dict[str, int] = {m.module_name: 0 for m in manifests}
        dependents: dict[str, list[str]] = {m.module_name: [] for m in manifests}

        for manifest in manifests:
            for dep in manifest.other_modules:
                if dep.module_name in name_to_manifest:
                    in_degree[manifest.module_name] += 1
                    dependents[dep.module_name].append(manifest.module_name)

        # Топологическая сортировка (алгоритм Кана)
        queue: list[str] = [name for name, degree in in_degree.items() if degree == 0]
        result: list[ModuleManifest] = []

        while queue:
            # Сортировка для детерминированного порядка
            queue.sort()
            current = queue.pop(0)
            result.append(name_to_manifest[current])

            for dependent in dependents[current]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        # Проверка на цикл зависимостей
        if len(result) != len(manifests):
            cyclic = [name for name, degree in in_degree.items() if degree > 0]
            raise ValueError(f"Обнаружен цикл зависимостей между модулями: {cyclic}")

        return result

    def validate_all(
        self,
        manifests: list[ModuleManifest],
    ) -> tuple[list[ModuleManifest], list[str]]:
        """Выполняет полную проверку зависимостей.

        Проверяет совместимость версий ядра, внешние библиотеки,
        зависимости между модулями, совместимость api_version
        и определяет порядок загрузки.

        Args:
            manifests: Список манифестов модулей для проверки.

        Returns:
            Кортеж (манифесты в порядке загрузки, список ошибок).
            Если список ошибок пуст, все модули могут быть загружены.
            Если список ошибок не пуст, загрузка не рекомендуется.
        """
        errors: list[str] = []

        # Проверка совместимости версий ядра
        compatible: list[ModuleManifest] = []
        for manifest in manifests:
            if not self.check_core_version(manifest.dds_core_version):
                errors.append(
                    f"Модуль '{manifest.module_name}': требует "
                    f"dds_core {manifest.dds_core_version}, "
                    f"установлена {self._dds_version}."
                )
            else:
                compatible.append(manifest)

        # Проверка внешних библиотек
        for manifest in compatible:
            missing_libs = self.check_external_libraries(
                manifest.external_libraries,
            )
            if missing_libs:
                errors.append(
                    f"Модуль '{manifest.module_name}': отсутствуют "
                    f"библиотеки: {', '.join(missing_libs)}."
                )

        # Проверка зависимостей между модулями
        missing_deps = self.check_module_dependencies(compatible)
        if missing_deps:
            for dep in missing_deps:
                errors.append(f"Отсутствует модуль-зависимость: {dep}.")

        # Проверка совместимости api_version
        incompatible = self.check_api_version_compatibility(compatible)
        for item in incompatible:
            errors.append(f"Несовместимость api_version: {item}.")

        # Топологическая сортировка
        try:
            ordered = self.resolve_load_order(compatible)
        except ValueError as e:
            errors.append(str(e))
            ordered = compatible

        return ordered, errors

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _check_version_constraint(
        self,
        version: str,
        constraint: str,
    ) -> bool:
        """Проверяет, удовлетворяет ли версия ограничению.

        Поддерживаемые операторы: ==, !=, >=, <=, >, <.
        Поддерживаются составные ограничения через запятую:
        ">=1.0,<2.0".

        Args:
            version: Версия для проверки. Например, "1.2.3".
            constraint: Ограничение. Например, ">=1.0.0".

        Returns:
            True, если версия удовлетворяет ограничению.
        """
        version_tuple = self._parse_version(version)

        for part in constraint.split(","):
            part = part.strip()
            if not part:
                continue

            # Извлечь оператор и версию
            match = re.match(r"^(>=|<=|!=|==|>|<)?\s*(.+)$", part)
            if not match:
                return False

            operator = match.group(1) or "=="
            constraint_version = self._parse_version(match.group(2))

            if operator == "==":
                if version_tuple != constraint_version:
                    return False
            elif operator == "!=":
                if version_tuple == constraint_version:
                    return False
            elif operator == ">=":
                if version_tuple < constraint_version:
                    return False
            elif operator == "<=":
                if version_tuple > constraint_version:
                    return False
            elif operator == ">":
                if version_tuple <= constraint_version:
                    return False
            elif operator == "<" and version_tuple >= constraint_version:
                return False

        return True

    def _parse_version(self, version: str) -> tuple[int, ...]:
        """Разбирает строку версии в кортеж чисел.

        Args:
            version: Строка версии. Например, "1.2.3".

        Returns:
            Кортеж чисел. Например, (1, 2, 3).
            Нечисловые компоненты заменяются на 0.
        """
        parts: list[int] = []
        for part in version.split("."):
            try:
                parts.append(int(part))
            except ValueError:
                parts.append(0)
        return tuple(parts)
