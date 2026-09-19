"""
Проверка жизненного цикла устаревших символов ``DEPRECATIONS.yaml``.

Назначение
----------
``docs/DEPRECATIONS.yaml`` — учёт символов, которые:

- либо удалены напрямую (``type: direct_removal``), потому что
  внешние модули их не использовали;
- либо сохранены как deprecation-обёртки (``type: deprecation``)
  до тех пор, пока их используют модули из ``dds_modules/``.

Этот модуль проверяет структурную корректность YAML и жизненный
цикл записей: не просрочено ли удаление, актуальны ли записи,
заполнен ли список ``module_users`` осмысленно.

Источник данных
---------------
``docs/DEPRECATIONS.yaml``. Структура:

.. code-block:: yaml

    entries:
      # Прямое удаление — символ удалён без deprecation-периода,
      # так как dds_modules/ его не использовали.
      - symbol: dds_core.application.text_normalizer.normalize_text
        type: direct_removal
        removed_in_phase: 3
        module_users: []
        note: "Перенесено в dds_core.domain.text_normalization."

      # Deprecation — символ сохранён как обёртка, модули используют.
      - symbol: dds_web.api.set_context
        type: deprecation
        deprecated_in_phase: 2
        remove_in_phase: 10
        module_users:
          - dds_stamp_extractor
        note: "Сохраняется для совместимости с dds_modules/."

Обязательные поля: ``symbol``, ``type``, ``module_users``.
Дополнительные: ``removed_in_phase`` (для ``direct_removal``),
``deprecated_in_phase`` + ``remove_in_phase`` (для ``deprecation``),
``note`` (опциональное).

Текущая фаза проекта
--------------------
Номер текущей фазы читается из
``docs/architecture-decisions/current_phase.txt``
(общий файл с ``check_import_linter_exceptions.py``).

Правила проверки
----------------

Структурные ошибки (ERROR):
    - отсутствует обязательное поле;
    - некорректный тип поля;
    - отрицательное значение фазы;
    - неизвестный ``type``;
    - ``deprecation`` с ``deprecated_in_phase >= remove_in_phase``;
    - ``direct_removal`` с непустым ``module_users`` (противоречие:
      символы прямого удаления по определению не используются
      внешними модулями).

Жизненный цикл:
    - ERROR: ``deprecation`` с ``current_phase >= remove_in_phase`` —
      символ должен быть удалён, но запись осталась.
    - ERROR: ``direct_removal`` с ``removed_in_phase > current_phase`` —
      удаление запланировано на будущее, но помечено как
      уже произошедшее.
    - WARNING: ``deprecation`` с ``current_phase == remove_in_phase - 1`` —
      удалить в следующей фазе.
    - WARNING: ``deprecation`` с ``deprecated_in_phase > current_phase`` —
      deprecation запланирован на будущую фазу.
    - WARNING: ``deprecation`` с пустым ``module_users`` — deprecation
      без внешних потребителей, вероятно, стоило сделать
      ``direct_removal``.
    - WARNING: ``direct_removal`` с ``current_phase > removed_in_phase + 2`` —
      запись устарела, можно удалить из YAML.

Exit codes
----------
- 0 — все проверки пройдены (возможны warnings).
- 1 — обнаружены ERROR.

CLI
---
.. code-block:: bash

    # Проверка (по умолчанию)
    python tools/check_deprecations.py

    # Явные пути
    python tools/check_deprecations.py \
        --deprecations docs/DEPRECATIONS.yaml \
        --current-phase docs/architecture-decisions/current_phase.txt

Принципы:
    - Модуль не изменяет файловую систему.
    - Не имеет побочных эффектов при импорте.
    - Зависимости: stdlib + PyYAML (requirements-dev.txt).
    - Понятные сообщения об ошибках с указанием пути внутри YAML
      (``entries[2].remove_in_phase``).
    - Deterministic output: порядок отчёта соответствует порядку
      записей в YAML.
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

_DEFAULT_DEPRECATIONS_PATH = Path("docs/DEPRECATIONS.yaml")
"""Путь к ``DEPRECATIONS.yaml`` по умолчанию."""

_DEFAULT_CURRENT_PHASE_PATH = Path("docs/architecture-decisions/current_phase.txt")
"""Путь к файлу с номером текущей фазы по умолчанию."""

_TYPE_DIRECT_REMOVAL = "direct_removal"
_TYPE_DEPRECATION = "deprecation"

_SUPPORTED_TYPES = frozenset({_TYPE_DIRECT_REMOVAL, _TYPE_DEPRECATION})
"""Допустимые значения поля ``type``."""

_DIRECT_REMOVAL_GRACE_PHASES = 2
"""Сколько фаз запись ``direct_removal`` может оставаться после удаления.

По истечении — WARNING: запись устарела, удалите из YAML.
Grace нужен для аудита: две фазы — разумный компромисс между
«увидеть историю удаления» и «не засорять YAML».
"""


# =====================================================================
# Исключения
# =====================================================================


class ConfigError(Exception):
    """Ошибка структуры ``DEPRECATIONS.yaml`` или ``current_phase.txt``.

    Поднимается при некорректных типах, отсутствующих обязательных
    полях и т.п. Не используется для сообщений о нарушениях
    жизненного цикла — для них применяется собственный механизм
    (список ``errors``).
    """


# =====================================================================
# Загрузка
# =====================================================================


def load_deprecations(path: Path) -> dict[str, Any]:
    """Читает ``DEPRECATIONS.yaml`` без глубокой валидации полей.

    Проверяет только:

    - файл существует и читается;
    - содержимое — валидный YAML;
    - корень — словарь;
    - ``entries`` (если есть) — список.

    Полная структурная валидация записей выполняется
    :func:`validate_structure`.

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

    entries = data.get("entries")
    if entries is None:
        # Файл без записей допустим: проект может не иметь
        # ни одного устаревшего символа.
        return data

    if not isinstance(entries, list):
        raise ConfigError(
            f"'entries' в {path} должен быть списком, получено: {type(entries).__name__}."
        )

    return data


def load_current_phase(path: Path) -> int:
    """Читает номер текущей фазы из текстового файла.

    Файл содержит одно неотрицательное целое число. Пробелы и
    переводы строк по краям игнорируются. Допускаются строки-
    комментарии, начинающиеся с ``#``.

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
# Модель записи
# =====================================================================


class _DeprecationEntry:
    """Разобранная запись об устаревшем символе.

    Хранит нормализованные данные для последующей проверки.
    Создаётся в :func:`validate_structure`; используется
    в :func:`check_lifecycle` и :func:`print_report`.

    Attributes:
        symbol: Полный путь к символу.
        type: ``direct_removal`` или ``deprecation``.
        module_users: Список имён модулей, использующих символ.
            Для ``direct_removal`` — пустой.
        removed_in_phase: Фаза удаления (для ``direct_removal``).
        deprecated_in_phase: Фаза ввода deprecation.
        remove_in_phase: Фаза удаления deprecation.
        note: Опциональное пояснение.
    """

    __slots__ = (
        "deprecated_in_phase",
        "module_users",
        "note",
        "remove_in_phase",
        "removed_in_phase",
        "symbol",
        "type",
    )

    def __init__(
        self,
        *,
        symbol: str,
        type: str,
        module_users: list[str],
        removed_in_phase: int | None = None,
        deprecated_in_phase: int | None = None,
        remove_in_phase: int | None = None,
        note: str = "",
    ) -> None:
        self.symbol = symbol
        self.type = type
        self.module_users = module_users
        self.removed_in_phase = removed_in_phase
        self.deprecated_in_phase = deprecated_in_phase
        self.remove_in_phase = remove_in_phase
        self.note = note


# =====================================================================
# Структурная валидация
# =====================================================================


def validate_structure(
    data: dict[str, Any],
) -> tuple[list[_DeprecationEntry], list[str]]:
    """Валидирует структуру записей ``DEPRECATIONS.yaml``.

    Все ошибки собираются в список (не fail-fast), чтобы
    разработчик видел полную картину за один прогон.

    Операции:

    +----+----------------------------------------------------+
    | №  | Описание                                           |
    +====+====================================================+
    | 1  | Итерация по записям ``entries``.                   |
    +----+----------------------------------------------------+
    | 2  | Проверка обязательных полей (``symbol``, ``type``, |
    |    | ``module_users``).                                 |
    +----+----------------------------------------------------+
    | 3  | Проверка значения ``type`` против ``_SUPPORTED_``  |
    |    | ``TYPES``.                                         |
    +----+----------------------------------------------------+
    | 4  | Для ``direct_removal``: проверка ``removed_in_``   |
    |    | ``phase`` и требования пустого ``module_users``.   |
    +----+----------------------------------------------------+
    | 5  | Для ``deprecation``: проверка ``deprecated_in_``   |
    |    | ``phase``, ``remove_in_phase``,                    |
    |    | ``deprecated_in_phase < remove_in_phase``.         |
    +----+----------------------------------------------------+
    | 6  | Формирование :class:`_DeprecationEntry`.           |
    +----+----------------------------------------------------+

    Args:
        data: словарь из :func:`load_deprecations`.

    Returns:
        Кортеж ``(entries, errors)``. ``entries`` — разобранные
        записи; ``errors`` — текстовые сообщения о структурных
        проблемах. При непустом ``errors`` список ``entries``
        может быть неполным.
    """
    entries: list[_DeprecationEntry] = []
    errors: list[str] = []

    raw_entries = data.get("entries") or []
    if not isinstance(raw_entries, list):
        # Это уже проверено в load_deprecations, но оставляем
        # защиту на случай прямого вызова с произвольным словарём.
        errors.append("'entries' должен быть списком.")
        return entries, errors

    for index, raw in enumerate(raw_entries):
        prefix = f"entries[{index}]"

        if not isinstance(raw, dict):
            errors.append(f"{prefix}: ожидался словарь, получено {type(raw).__name__}.")
            continue

        # --- symbol ---
        symbol = raw.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            errors.append(f"{prefix}.symbol: обязательное непустое поле-строка.")
            continue
        symbol = symbol.strip()

        # --- type ---
        entry_type = raw.get("type")
        if entry_type not in _SUPPORTED_TYPES:
            errors.append(
                f"{prefix}.type: ожидалось одно из "
                f"{sorted(_SUPPORTED_TYPES)}, получено {entry_type!r}."
            )
            continue

        # --- module_users ---
        module_users = raw.get("module_users", [])
        if not isinstance(module_users, list):
            errors.append(
                f"{prefix}.module_users: ожидался список, получено {type(module_users).__name__}."
            )
            continue
        if not all(isinstance(u, str) and u.strip() for u in module_users):
            errors.append(f"{prefix}.module_users: все элементы должны быть непустыми строками.")
            continue
        module_users = [u.strip() for u in module_users]

        # --- note (опционально) ---
        note = raw.get("note", "")
        if not isinstance(note, str):
            errors.append(f"{prefix}.note: ожидалась строка, получено {type(note).__name__}.")
            continue

        if entry_type == _TYPE_DIRECT_REMOVAL:
            removed_in_phase = raw.get("removed_in_phase")
            if not isinstance(removed_in_phase, int) or removed_in_phase < 0:
                errors.append(
                    f"{prefix}.removed_in_phase: ожидалось "
                    f"неотрицательное целое, получено "
                    f"{removed_in_phase!r}."
                )
                continue
            if module_users:
                errors.append(
                    f"{prefix}: 'direct_removal' требует пустого "
                    f"'module_users' (символ удалён напрямую, так как "
                    f"внешние модули его не использовали), получено "
                    f"{module_users!r}."
                )
                continue

            entries.append(
                _DeprecationEntry(
                    symbol=symbol,
                    type=entry_type,
                    module_users=[],
                    removed_in_phase=removed_in_phase,
                    note=note.strip(),
                )
            )

        elif entry_type == _TYPE_DEPRECATION:
            deprecated_in_phase = raw.get("deprecated_in_phase")
            remove_in_phase = raw.get("remove_in_phase")

            if not isinstance(deprecated_in_phase, int) or deprecated_in_phase < 0:
                errors.append(
                    f"{prefix}.deprecated_in_phase: ожидалось "
                    f"неотрицательное целое, получено "
                    f"{deprecated_in_phase!r}."
                )
                continue
            if not isinstance(remove_in_phase, int) or remove_in_phase < 0:
                errors.append(
                    f"{prefix}.remove_in_phase: ожидалось "
                    f"неотрицательное целое, получено "
                    f"{remove_in_phase!r}."
                )
                continue
            if deprecated_in_phase >= remove_in_phase:
                errors.append(
                    f"{prefix}: 'deprecated_in_phase' "
                    f"({deprecated_in_phase}) должен быть строго меньше "
                    f"'remove_in_phase' ({remove_in_phase})."
                )
                continue

            entries.append(
                _DeprecationEntry(
                    symbol=symbol,
                    type=entry_type,
                    module_users=module_users,
                    deprecated_in_phase=deprecated_in_phase,
                    remove_in_phase=remove_in_phase,
                    note=note.strip(),
                )
            )

    return entries, errors


# =====================================================================
# Жизненный цикл
# =====================================================================


def check_lifecycle(
    entries: list[_DeprecationEntry],
    current_phase: int,
) -> tuple[list[str], list[str]]:
    """Проверяет жизненный цикл записей.

    Все проверки собираются в два списка: ``warnings`` и ``errors``.
    Тексты сообщений содержат символ и номер фазы, что позволяет
    сразу понять источник проблемы.

    Проверки (см. также docstring модуля):

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | ``deprecation`` с ``current_phase >= remove_in_``   |
    |   | ``phase`` → ERROR (просрочено).                     |
    +---+-----------------------------------------------------+
    | 2 | ``deprecation`` с                                     |
    |   | ``current_phase == remove_in_phase - 1`` →          |
    |   | WARNING (истекает в следующей фазе).                |
    +---+-----------------------------------------------------+
    | 3 | ``deprecation`` с пустым ``module_users`` →         |
    |   | WARNING (нет внешних потребителей).                 |
    +---+-----------------------------------------------------+
    | 4 | ``deprecation`` с                                     |
    |   | ``deprecated_in_phase > current_phase`` →           |
    |   | WARNING (запланировано на будущее).                 |
    +---+-----------------------------------------------------+
    | 5 | ``direct_removal`` с                                  |
    |   | ``removed_in_phase > current_phase`` → ERROR        |
    |   | (удаление не наступило, но помечено как сделанное). |
    +---+-----------------------------------------------------+
    | 6 | ``direct_removal`` с                                  |
    |   | ``current_phase > removed_in_phase + grace`` →      |
    |   | WARNING (запись устарела).                          |
    +---+-----------------------------------------------------+

    Args:
        entries: разобранные записи.
        current_phase: текущая фаза.

    Returns:
        Кортеж ``(warnings, errors)``.
    """
    warnings: list[str] = []
    errors: list[str] = []

    for entry in entries:
        if entry.type == _TYPE_DEPRECATION:
            assert entry.deprecated_in_phase is not None
            assert entry.remove_in_phase is not None

            # 1. Просроченное deprecation.
            if current_phase >= entry.remove_in_phase:
                errors.append(
                    f"Просроченное deprecation: {entry.symbol!r} "
                    f"(удалить к фазе {entry.remove_in_phase}, "
                    f"текущая фаза {current_phase})."
                )
            # 2. Истекает в следующей фазе.
            elif current_phase == entry.remove_in_phase - 1:
                warnings.append(
                    f"Deprecation истекает в следующей фазе: "
                    f"{entry.symbol!r} (удалить к фазе "
                    f"{entry.remove_in_phase})."
                )

            # 3. Пустой module_users.
            if not entry.module_users:
                warnings.append(
                    f"Deprecation без внешних потребителей: "
                    f"{entry.symbol!r} — рассмотрите перевод в "
                    f"'direct_removal'."
                )

            # 4. Запланировано на будущее.
            if entry.deprecated_in_phase > current_phase:
                warnings.append(
                    f"Deprecation запланирован на будущую фазу: "
                    f"{entry.symbol!r} (deprecated_in_phase="
                    f"{entry.deprecated_in_phase}, текущая фаза "
                    f"{current_phase})."
                )

        elif entry.type == _TYPE_DIRECT_REMOVAL:
            assert entry.removed_in_phase is not None

            # 5. Удаление не наступило.
            if entry.removed_in_phase > current_phase:
                errors.append(
                    f"Прямое удаление запланировано на будущую фазу, "
                    f"но помечено как сделанное: {entry.symbol!r} "
                    f"(removed_in_phase={entry.removed_in_phase}, "
                    f"текущая фаза {current_phase})."
                )
            # 6. Запись устарела.
            elif current_phase > entry.removed_in_phase + _DIRECT_REMOVAL_GRACE_PHASES:
                warnings.append(
                    f"Устаревшая запись direct_removal: {entry.symbol!r} "
                    f"(удалён в фазе {entry.removed_in_phase}, "
                    f"текущая фаза {current_phase}) — можно удалить из YAML."
                )

    return warnings, errors


# =====================================================================
# Отчёт
# =====================================================================


def print_report(
    current_phase: int,
    entries: list[_DeprecationEntry],
    warnings: list[str],
    errors: list[str],
) -> None:
    """Печатает отчёт в stdout/stderr.

    Формат:

    - Сводка и список записей — в stdout.
    - Warnings и errors — в stderr с префиксами ``WARNING:`` /
      ``ERROR:``.
    - Итог — ``OK`` или ``FAILED`` в stderr.

    Args:
        current_phase: текущая фаза.
        entries: разобранные записи.
        warnings: список предупреждений.
        errors: список ошибок.
    """
    direct = sum(1 for e in entries if e.type == _TYPE_DIRECT_REMOVAL)
    dep = sum(1 for e in entries if e.type == _TYPE_DEPRECATION)

    print(f"Current phase: {current_phase}")
    print(f"Entries: {len(entries)} (direct_removal: {direct}, deprecation: {dep})")

    for entry in entries:
        if entry.type == _TYPE_DIRECT_REMOVAL:
            label = f"removed in phase {entry.removed_in_phase}"
        else:
            label = (
                f"deprecated in phase {entry.deprecated_in_phase}, "
                f"remove by phase {entry.remove_in_phase}"
            )
            if entry.module_users:
                label += f", users: {', '.join(entry.module_users)}"
        print(f"  - [{entry.type}] {entry.symbol}  ({label})")

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
        print("OK: жизненный цикл deprecations в порядке.", file=sys.stderr)


# =====================================================================
# CLI
# =====================================================================


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Парсинг аргументов (``--deprecations``,              |
    |   | ``--current-phase``).                               |
    +---+-----------------------------------------------------+
    | 2 | Загрузка ``current_phase.txt``.                     |
    +---+-----------------------------------------------------+
    | 3 | Загрузка ``DEPRECATIONS.yaml``.                     |
    +---+-----------------------------------------------------+
    | 4 | Структурная валидация записей.                      |
    +---+-----------------------------------------------------+
    | 5 | Проверка жизненного цикла.                          |
    +---+-----------------------------------------------------+
    | 6 | Печать отчёта; возврат 0/1.                         |
    +---+-----------------------------------------------------+

    Args:
        argv: Аргументы командной строки без имени программы.

    Returns:
        Код возврата: 0 — успех, 1 — ошибки или структурные проблемы.
    """
    parser = argparse.ArgumentParser(
        description=("Проверка жизненного цикла устаревших символов в docs/DEPRECATIONS.yaml."),
    )
    parser.add_argument(
        "--deprecations",
        type=Path,
        default=_DEFAULT_DEPRECATIONS_PATH,
        help=(f"Путь к DEPRECATIONS.yaml (по умолчанию: {_DEFAULT_DEPRECATIONS_PATH})."),
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

    # 2. Загрузка DEPRECATIONS.yaml.
    try:
        data = load_deprecations(args.deprecations)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # 3. Структурная валидация.
    entries, structure_errors = validate_structure(data)

    if structure_errors:
        print(f"Current phase: {current_phase}")
        print(f"Entries parsed: {len(entries)} (structural errors detected)")
        print(file=sys.stderr)
        for e in structure_errors:
            print(f"ERROR: {e}", file=sys.stderr)
        print(file=sys.stderr)
        print("FAILED: структурные ошибки в DEPRECATIONS.yaml.", file=sys.stderr)
        return 1

    # 4. Проверка жизненного цикла.
    warnings, errors = check_lifecycle(entries, current_phase)

    # 5. Отчёт и код возврата.
    print_report(
        current_phase=current_phase,
        entries=entries,
        warnings=warnings,
        errors=errors,
    )

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
