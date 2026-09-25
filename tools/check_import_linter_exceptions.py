"""
Проверка жизненного цикла исключений ``import-linter``.

Назначение
----------
``import-linter`` позволяет ослаблять архитектурные контракты через
``ignore_imports`` — перечисление импортов, которые не проверяются
контрактом. Такие ослабления — долг. Каждое должно иметь:

- обоснование (``rationale``),
- срок жизни: либо ``phase_removed_by: N`` (удалить к началу фазы N),
  либо ``permanent: true`` (ослабление неустранимо).

Этот модуль проверяет, что все ослабления учтены, обоснованы и не
просрочены. Он запускается в pre-commit и CI; при нарушении
возвращает код 1.

Источник данных
---------------
``docs/architecture-decisions/exceptions.yaml`` — единый источник
истины. Структура:

.. code-block:: yaml

    root_packages:
      - dds_core
      - dds_web

    contracts:
      - name: subprocess-tasks-isolation
        type: forbidden
        source_modules: [...]
        forbidden_modules: [...]
        ignore_imports:
          - "dds_web.lifespan -> dds_core.subprocess_tasks.pdf_workers"

    exception_lifecycle:
      - contract: subprocess-tasks-isolation
        import: "dds_web.lifespan -> dds_core.subprocess_tasks.pdf_workers"
        permanent: true
        rationale: >
          Composition root: единственная точка доступа к subprocess-
          worker'ам; не может быть удалена без отказа от DI.

Секция ``exception_lifecycle`` обязательна, если хотя бы один
контракт содержит ``ignore_imports``. Каждое ослабление должно
иметь ровно одну запись в ``exception_lifecycle``. Обратное тоже
верно: каждая запись должна ссылаться на существующее ослабление.

Текущая фаза проекта
--------------------
Текущая фаза читается из ``docs/architecture-decisions/current_phase.txt``
(содержит одно целое число, например ``4``). Этот файл увеличивается
разработчиком при переходе между фазами плана рефакторинга.

Правила проверки
----------------

+--------------------------------------+-----------------------------+
| Условие                              | Результат                   |
+======================================+=============================+
| Все ослабления покрыты lifecycle     | OK                          |
+--------------------------------------+-----------------------------+
| Запись lifecycle без ослабления      | ERROR (устаревшая запись)   |
+--------------------------------------+-----------------------------+
| Ослабление без записи lifecycle      | ERROR (не учтено)           |
+--------------------------------------+-----------------------------+
| ``permanent: true`` без ``rationale``| ERROR                       |
+--------------------------------------+-----------------------------+
| ``current_phase >= phase_removed_by``| ERROR (просрочено)          |
+--------------------------------------+-----------------------------+
| ``current_phase == phase_removed_by-1`` | WARNING (истекает)       |
+--------------------------------------+-----------------------------+

Exit codes
----------
- 0 — все проверки пройдены (возможны warnings).
- 1 — обнаружены ERROR.

CLI
---
.. code-block:: bash

    # Проверка (по умолчанию)
    python tools/check_import_linter_exceptions.py

    # Явные пути
    python tools/check_import_linter_exceptions.py \
        --exceptions docs/architecture-decisions/exceptions.yaml \
        --current-phase docs/architecture-decisions/current_phase.txt

Принципы:
    - Модуль не изменяет файловую систему.
    - Не имеет побочных эффектов при импорте.
    - Зависимости: stdlib + PyYAML (requirements-dev.txt).
    - Понятные сообщения об ошибках с указанием пути внутри YAML
      (``contracts[2].ignore_imports[0]``).
    - Deterministic output: порядок записей соответствует порядку
      в YAML.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as _yaml_import_error:  # pragma: no cover - инфраструктурная ошибка
    print(
        "ERROR: PyYAML не установлен. Установите зависимости: pip install -r requirements-dev.txt",
        file=sys.stderr,
    )
    raise SystemExit(1) from _yaml_import_error


# =====================================================================
# Константы
# =====================================================================

_DEFAULT_EXCEPTIONS_PATH = Path("docs/architecture-decisions/exceptions.yaml")
"""Путь к ``exceptions.yaml`` по умолчанию."""

_DEFAULT_CURRENT_PHASE_PATH = Path("docs/architecture-decisions/current_phase.txt")
"""Путь к файлу с номером текущей фазы по умолчанию."""

_IGNORE_IMPORT_ARROW = "->"
"""Разделитель в записи ``ignore_imports``: ``source -> target``."""


# =====================================================================
# Исключения
# =====================================================================


class ConfigError(Exception):
    """Ошибка структуры ``exceptions.yaml`` или ``current_phase.txt``.

    Поднимается при некорректных типах, отсутствующих обязательных
    полях, дублирующихся записях и т.п. Не используется для
    сообщений о нарушениях жизненного цикла — для них применяется
    собственный механизм (список ``errors``).
    """


# =====================================================================
# Загрузка
# =====================================================================


def load_exceptions(path: Path) -> dict[str, Any]:
    """Читает ``exceptions.yaml`` без глубокой валидации.

    Полная структурная валидация контрактов выполняется
    ``generate_importlinter_config.py``. Здесь проверяется только
    то, что нужно этому модулю:

    - файл читается и парсится как YAML;
    - корень — словарь;
    - ``contracts`` — список (если присутствует);
    - ``exception_lifecycle`` — список (если присутствует).

    Args:
        path: Путь к YAML-файлу.

    Returns:
        Словарь с данными файла.

    Raises:
        ConfigError: файл не найден, не читается или не проходит
            минимальную структурную проверку.
    """
    if not path.is_file():
        raise ConfigError(f"Файл не найден: {path}")

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Не удалось прочитать {path}: {exc}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Некорректный YAML в {path}: {exc}") from exc

    if data is None:
        raise ConfigError(f"Файл {path} пуст.")

    if not isinstance(data, dict):
        raise ConfigError(f"Корень {path} должен быть словарём, получено: {type(data).__name__}.")

    contracts = data.get("contracts")
    if contracts is not None and not isinstance(contracts, list):
        raise ConfigError(
            f"'contracts' в {path} должен быть списком, получено: {type(contracts).__name__}."
        )

    lifecycle = data.get("exception_lifecycle")
    if lifecycle is not None and not isinstance(lifecycle, list):
        raise ConfigError(
            f"'exception_lifecycle' в {path} должен быть списком, "
            f"получено: {type(lifecycle).__name__}."
        )

    return data


def load_current_phase(path: Path) -> int:
    """Читает номер текущей фазы из текстового файла.

    Файл содержит одно неотрицательное целое число. Пробелы и
    переводы строк по краям игнорируются. Дополнительно допускаются
    строки-комментарии, начинающиеся с ``#``.

    Args:
        path: Путь к файлу с номером фазы.

    Returns:
        Номер текущей фазы.

    Raises:
        ConfigError: файл не найден, пуст, содержит более одного
            значимого токена или токен не является неотрицательным
            целым числом.
    """
    if not path.is_file():
        raise ConfigError(f"Файл с текущей фазой не найден: {path}")

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Не удалось прочитать {path}: {exc}") from exc

    lines = [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    if len(lines) != 1:
        raise ConfigError(
            f"{path}: ожидается ровно одно значимое значение "
            f"(номер фазы), найдено {len(lines)} строк."
        )

    try:
        value = int(lines[0])
    except ValueError as exc:
        raise ConfigError(f"{path}: значение {lines[0]!r} не является целым числом.") from exc

    if value < 0:
        raise ConfigError(f"{path}: номер фазы не может быть отрицательным (получено {value}).")

    return value


# =====================================================================
# Модели
# =====================================================================


class _ExceptionRecord:
    """Запись об ослаблении контракта.

    Объединяет пару (контракт, import) с метаданными жизненного
    цикла. Создаётся при анализе ``exceptions.yaml``; используется
    для формирования отчёта.

    Attributes:
        contract: Имя контракта (``name``).
        import_spec: Спецификация импорта в формате
            ``"source -> target"``.
        permanent: ``True``, если ослабление неустранимо.
        phase_removed_by: Номер фазы, к началу которой ослабление
            должно быть удалено. ``None`` при ``permanent=True``.
        rationale: Обоснование (обязательно для ``permanent``).
    """

    __slots__ = ("contract", "import_spec", "permanent", "phase_removed_by", "rationale")

    def __init__(
        self,
        *,
        contract: str,
        import_spec: str,
        permanent: bool,
        phase_removed_by: int | None,
        rationale: str,
    ) -> None:
        self.contract = contract
        self.import_spec = import_spec
        self.permanent = permanent
        self.phase_removed_by = phase_removed_by
        self.rationale = rationale

    def key(self) -> tuple[str, str]:
        """Возвращает ключ для сопоставления (контракт, импорт)."""
        return (self.contract, self.import_spec)


# =====================================================================
# Анализ
# =====================================================================


def collect_ignore_imports(data: dict[str, Any]) -> list[tuple[str, str]]:
    """Собирает все ``ignore_imports`` из контрактов.

    Пробегает по всем контрактам и извлекает пары
    ``(contract_name, import_spec)``. Порядок соответствует порядку
    в YAML: контракты обходятся слева-направо, импорты внутри
    контракта — тоже.

    Args:
        data: словарь из :func:`load_exceptions`.

    Returns:
        Список кортежей ``(contract_name, import_spec)``. Пустой
        список, если ни один контракт не имеет ``ignore_imports``.
    """
    result: list[tuple[str, str]] = []
    for contract in data.get("contracts") or []:
        if not isinstance(contract, dict):
            continue
        name = contract.get("name")
        if not isinstance(name, str):
            continue
        ignore = contract.get("ignore_imports") or []
        if not isinstance(ignore, list):
            continue
        for spec in ignore:
            # Формат — либо строка "source -> target" (см. ранее
            # созданный exceptions.yaml), либо словарь с полем
            # import. Поддерживаем оба варианта для совместимости
            # с будущими расширениями схемы.
            if isinstance(spec, str):
                result.append((name, spec.strip()))
            elif isinstance(spec, dict):
                raw_import = spec.get("import")
                if isinstance(raw_import, str):
                    result.append((name, raw_import.strip()))
    return result


def collect_lifecycle(
    data: dict[str, Any],
) -> tuple[list[_ExceptionRecord], list[str]]:
    """Разбирает секцию ``exception_lifecycle``.

    Проверяет типы полей и обязательность ``rationale`` для
    ``permanent``-записей. Возвращает список записей и список
    структурных ошибок. Если список ошибок непуст — валидация
    завершается с ошибкой, но все ошибки собираются за один проход
    (не fail-fast), чтобы разработчик видел полную картину.

    Операции:

    +----+----------------------------------------------------+
    | №  | Описание                                           |
    +====+====================================================+
    | 1  | Итерация по записям ``exception_lifecycle``.       |
    +----+----------------------------------------------------+
    | 2  | Проверка обязательных полей ``contract`` и         |
    |    | ``import``.                                         |
    +----+----------------------------------------------------+
    | 3  | Проверка взаимоисключения ``permanent`` и          |
    |    | ``phase_removed_by``.                              |
    +----+----------------------------------------------------+
    | 4  | Проверка наличия ``rationale`` для ``permanent``.  |
    +----+----------------------------------------------------+
    | 5  | Проверка типа и значения ``phase_removed_by``.     |
    +----+----------------------------------------------------+
    | 6  | Формирование :class:`_ExceptionRecord`.            |
    +----+----------------------------------------------------+

    Args:
        data: словарь из :func:`load_exceptions`.

    Returns:
        Кортеж ``(records, errors)``. ``records`` — записи для
        дальнейшей проверки; ``errors`` — список текстовых
        сообщений о структурных проблемах. При непустом ``errors``
        список ``records`` может быть неполным.
    """
    records: list[_ExceptionRecord] = []
    errors: list[str] = []

    lifecycle = data.get("exception_lifecycle") or []
    for index, entry in enumerate(lifecycle):
        prefix = f"exception_lifecycle[{index}]"

        if not isinstance(entry, dict):
            errors.append(f"{prefix}: ожидался словарь, получено {type(entry).__name__}.")
            continue

        contract = entry.get("contract")
        if not isinstance(contract, str) or not contract.strip():
            errors.append(f"{prefix}.contract: обязательное непустое поле-строка.")
            continue

        import_spec = entry.get("import")
        if not isinstance(import_spec, str) or not import_spec.strip():
            errors.append(f"{prefix}.import: обязательное непустое поле-строка.")
            continue

        permanent = bool(entry.get("permanent", False))
        phase_removed_by = entry.get("phase_removed_by")
        rationale = entry.get("rationale", "")

        if permanent and phase_removed_by is not None:
            errors.append(f"{prefix}: поля 'permanent' и 'phase_removed_by' взаимоисключающие.")
            continue

        if not permanent and phase_removed_by is None:
            errors.append(
                f"{prefix}: необходимо указать либо 'permanent: true', "
                f"либо 'phase_removed_by: <int>'."
            )
            continue

        if permanent:
            if not isinstance(rationale, str) or not rationale.strip():
                errors.append(f"{prefix}: 'rationale' обязателен для permanent-ослаблений.")
                continue
        else:
            if not isinstance(phase_removed_by, int) or phase_removed_by < 0:
                errors.append(
                    f"{prefix}.phase_removed_by: ожидалось "
                    f"неотрицательное целое, получено "
                    f"{phase_removed_by!r}."
                )
                continue

        records.append(
            _ExceptionRecord(
                contract=contract.strip(),
                import_spec=import_spec.strip(),
                permanent=permanent,
                phase_removed_by=phase_removed_by,
                rationale=rationale.strip() if isinstance(rationale, str) else "",
            )
        )

    return records, errors


def check_lifecycle(
    ignore_imports: list[tuple[str, str]],
    records: list[_ExceptionRecord],
    current_phase: int,
) -> tuple[list[str], list[str]]:
    """Проверяет соответствие ``ignore_imports`` и lifecycle.

    Выполняет три группы проверок:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Каждый ``ignore_imports`` покрыт записью lifecycle. |
    |   | Иначе — ERROR.                                      |
    +---+-----------------------------------------------------+
    | 2 | Каждая запись lifecycle ссылается на существующий   |
    |   | ``ignore_imports``. Иначе — ERROR (устаревшая       |
    |   | запись).                                            |
    +---+-----------------------------------------------------+
    | 3 | Для не-``permanent`` записей:                       |
    |   | a. ``current_phase >= phase_removed_by`` → ERROR.   |
    |   | b. ``current_phase == phase_removed_by - 1`` →      |
    |   |    WARNING (истекает в следующей фазе).             |
    +---+-----------------------------------------------------+

    Args:
        ignore_imports: список пар ``(contract, import_spec)`` из
            :func:`collect_ignore_imports`.
        records: список записей из :func:`collect_lifecycle`.
        current_phase: номер текущей фазы.

    Returns:
        Кортеж ``(warnings, errors)`` — списки текстовых сообщений.
    """
    warnings: list[str] = []
    errors: list[str] = []

    ignore_set = set(ignore_imports)
    record_by_key = {rec.key(): rec for rec in records}

    # 1. Все ignore_imports должны иметь запись lifecycle.
    for contract, import_spec in ignore_imports:
        if (contract, import_spec) not in record_by_key:
            errors.append(
                f"Ослабление без lifecycle-записи: contract={contract!r}, import={import_spec!r}."
            )

    # 2. Все записи lifecycle должны ссылаться на существующее ослабление.
    for rec in records:
        if rec.key() not in ignore_set:
            errors.append(
                f"Устаревшая lifecycle-запись: "
                f"contract={rec.contract!r}, import={rec.import_spec!r}."
            )

    # 3. Проверка сроков.
    for rec in records:
        if rec.permanent:
            continue
        assert rec.phase_removed_by is not None  # инвариант после collect_lifecycle
        if current_phase >= rec.phase_removed_by:
            errors.append(
                f"Просроченное ослабление: contract={rec.contract!r}, "
                f"import={rec.import_spec!r} "
                f"(удалить к фазе {rec.phase_removed_by}, "
                f"текущая фаза {current_phase})."
            )
        elif current_phase == rec.phase_removed_by - 1:
            warnings.append(
                f"Ослабление истекает в следующей фазе: "
                f"contract={rec.contract!r}, import={rec.import_spec!r} "
                f"(удалить к фазе {rec.phase_removed_by})."
            )

    return warnings, errors


# =====================================================================
# Отчёт
# =====================================================================


def print_report(
    current_phase: int,
    records: list[_ExceptionRecord],
    warnings: list[str],
    errors: list[str],
) -> None:
    """Печатает отчёт в stdout/stderr.

    Формат отчёта:

    - Сводка по всем записям (stdout): количество, разбивка по типу.
    - Warnings (stderr, префикс ``WARNING:``).
    - Errors (stderr, префикс ``ERROR:``).
    - Итог: ``OK`` или ``FAILED``.

    Args:
        current_phase: текущая фаза.
        records: разобранные записи lifecycle.
        warnings: список предупреждений.
        errors: список ошибок.
    """
    total = len(records)
    permanent = sum(1 for r in records if r.permanent)
    temporary = total - permanent

    print(f"Current phase: {current_phase}")
    print(f"Exceptions: {total} (permanent: {permanent}, temporary: {temporary})")
    for rec in records:
        label = "permanent" if rec.permanent else f"removed by phase {rec.phase_removed_by}"
        print(f"  - [{rec.contract}] {rec.import_spec}  ({label})")

    if warnings:
        print(file=sys.stderr)
        for w in warnings:
            print(f"WARNING: {w}", file=sys.stderr)

    if errors:
        print(file=sys.stderr)
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        print(file=sys.stderr)
        print("FAILED: обнаружены ошибки жизненного цикла.", file=sys.stderr)
    else:
        print(file=sys.stderr)
        print("OK: жизненный цикл исключений в порядке.", file=sys.stderr)


# =====================================================================
# CLI
# =====================================================================


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Парсинг аргументов (``--exceptions``,                |
    |   | ``--current-phase``).                               |
    +---+-----------------------------------------------------+
    | 2 | Загрузка ``current_phase.txt``.                     |
    +---+-----------------------------------------------------+
    | 3 | Загрузка ``exceptions.yaml``.                       |
    +---+-----------------------------------------------------+
    | 4 | Разбор секции ``exception_lifecycle``.              |
    +---+-----------------------------------------------------+
    | 5 | Проверка соответствия с ``ignore_imports``.         |
    +---+-----------------------------------------------------+
    | 6 | Печать отчёта; возврат 0/1.                         |
    +---+-----------------------------------------------------+

    Args:
        argv: Аргументы командной строки без имени программы.

    Returns:
        Код возврата: 0 — успех, 1 — ошибки или структурные проблемы.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Проверка жизненного цикла исключений import-linter "
            "в docs/architecture-decisions/exceptions.yaml."
        ),
    )
    parser.add_argument(
        "--exceptions",
        type=Path,
        default=_DEFAULT_EXCEPTIONS_PATH,
        help=(f"Путь к exceptions.yaml (по умолчанию: {_DEFAULT_EXCEPTIONS_PATH})."),
    )
    parser.add_argument(
        "--current-phase",
        type=Path,
        default=_DEFAULT_CURRENT_PHASE_PATH,
        help=(
            f"Путь к файлу с номером текущей фазы (по умолчанию: {_DEFAULT_CURRENT_PHASE_PATH})."
        ),
    )
    args = parser.parse_args(argv)

    # 1. Загрузка текущей фазы.
    try:
        current_phase = load_current_phase(args.current_phase)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # 2. Загрузка exceptions.yaml.
    try:
        data = load_exceptions(args.exceptions)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # 3. Сбор данных.
    ignore_imports = collect_ignore_imports(data)
    records, structure_errors = collect_lifecycle(data)

    # 4. Структурные ошибки — фатальные, дальнейшие проверки
    #    могут быть неполными.
    if structure_errors:
        print(f"Current phase: {current_phase}")
        print(f"Exceptions: {len(records)} (structural errors detected)")
        print(file=sys.stderr)
        for e in structure_errors:
            print(f"ERROR: {e}", file=sys.stderr)
        print(file=sys.stderr)
        print("FAILED: структурные ошибки в exception_lifecycle.", file=sys.stderr)
        return 1

    # 5. Проверка жизненного цикла.
    warnings, errors = check_lifecycle(
        ignore_imports=ignore_imports,
        records=records,
        current_phase=current_phase,
    )

    # 6. Отчёт и код возврата.
    print_report(
        current_phase=current_phase,
        records=records,
        warnings=warnings,
        errors=errors,
    )

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
