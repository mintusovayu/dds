"""
Генератор конфигурации ``.importlinter`` из ``exceptions.yaml``.

Назначение
----------
``.importlinter`` — производный файл: его содержимое полностью
определяется ``docs/architecture-decisions/exceptions.yaml``.
Ручные правки ``.importlinter`` теряются при следующей регенерации;
источник истины — YAML-файл.

Генератор используется в трёх режимах:

- **Разработчик**: ``python tools/generate_importlinter_config.py`` —
  пересоздать ``.importlinter`` после изменения ``exceptions.yaml``.
- **pre-commit hook** (``generate-importlinter-config``): запускается
  автоматически при изменении ``exceptions.yaml``; зафиксированное
  изменение файла требует повторного ``git add``.
- **CI** (``--check``): не пишет файл, но возвращает код 1, если
  ``.importlinter`` устарел. Гарантирует, что CI не работает на
  устаревших контрактах.

Формат ``exceptions.yaml``
--------------------------
.. code-block:: yaml

    root_packages:
      - dds_core
      - dds_web

    contracts:
      - name: domain-isolation
        description: Domain layer isolation
        type: forbidden
        source_modules:
          - dds_core.domain
        forbidden_modules:
          - dds_core.application
          - dds_core.infrastructure
          - dds_core.subprocess_tasks
          - dds_web

      - name: subprocess-tasks-isolation
        description: Subprocess tasks isolation
        type: forbidden
        source_modules:
          - dds_core.domain
          - dds_core.application
          - dds_core.infrastructure
          - dds_web
        forbidden_modules:
          - dds_core.subprocess_tasks
        ignore_imports:
          - dds_web.lifespan -> dds_core.subprocess_tasks.pdf_workers

Обязательные поля контракта: ``name``, ``type``, ``source_modules``,
``forbidden_modules``. Опциональные: ``description``,
``ignore_imports``. Поддерживается только ``type: forbidden``
(контракты этого вида покрывают все архитектурные запреты DDS;
``layers`` и ``independence`` не используются).

CLI
---
.. code-block:: bash

    # Генерация (перезапись .importlinter)
    python tools/generate_importlinter_config.py

    # Проверка актуальности (exit 1, если .importlinter устарел)
    python tools/generate_importlinter_config.py --check

    # Указание путей (по умолчанию — стандартные для DDS)
    python tools/generate_importlinter_config.py \
        --exceptions docs/architecture-decisions/exceptions.yaml \
        --output .importlinter

Принципы:
    - Модуль — инструмент разработки, не часть runtime-приложения.
    - Зависимости: stdlib + PyYAML (см. ``requirements-dev.txt``).
    - Никаких побочных эффектов при импорте: вся работа в :func:`main`.
    - Валидация YAML с понятными сообщениями об ошибках.
    - Режим ``--check`` не изменяет файловую систему.
    - Атомарная запись: читатель никогда не видит частично
      записанный файл.
    - Deterministic output: одинаковый YAML → одинаковый ``.importlinter``.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
import tempfile
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
"""Путь к источнику истины по умолчанию."""

_DEFAULT_OUTPUT_PATH = Path(".importlinter")
"""Путь к генерируемому файлу по умолчанию."""

_CONTRACT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
"""Регулярное выражение для имени контракта (совместимо с INI-секцией)."""

_SUPPORTED_CONTRACT_TYPES = frozenset({"forbidden"})
"""Поддерживаемые типы контрактов import-linter."""

_HEADER = """\
# =====================================================================
# Deep Doc Search — конфигурация import-linter.
#
# ⚠  АВТОГЕНЕРИРУЕМЫЙ ФАЙЛ. РУЧНЫЕ ПРАВКИ БУДУТ ПОТЕРЯНЫ. ⚠
#
# Источник истины: docs/architecture-decisions/exceptions.yaml
# Генератор:       tools/generate_importlinter_config.py
#
# Регенерация после правки exceptions.yaml:
#   python tools/generate_importlinter_config.py
#
# Проверка актуальности (CI-job, pre-commit hook):
#   python tools/generate_importlinter_config.py --check
#
# Назначение:
#   Автоматическая проверка архитектурных контрактов DDS:
#     - направление зависимостей между слоями
#       (domain → application → infrastructure → dds_web);
#     - изоляция composition-root пакета dds_core.subprocess_tasks;
#     - запрет импорта infrastructure из application;
#     - запрет «раздувания» subprocess_tasks stateful-объектами,
#       не переносимыми через forkserver.
#
# Запуск полной проверки:
#   lint-imports
# =====================================================================
"""


# =====================================================================
# Исключения
# =====================================================================


class ConfigError(Exception):
    """Ошибка валидации ``exceptions.yaml``.

    Поднимается :func:`load_config` и :func:`_validate` при
    структурных проблемах: отсутствующие обязательные поля,
    некорректные типы, дублирующиеся имена контрактов.
    """


# =====================================================================
# Загрузка и валидация
# =====================================================================


def load_config(path: Path) -> dict[str, Any]:
    """Читает и валидирует ``exceptions.yaml``.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Проверка существования файла.                       |
    +---+-----------------------------------------------------+
    | 2 | Чтение содержимого в кодировке UTF-8.               |
    +---+-----------------------------------------------------+
    | 3 | Разбор YAML через ``yaml.safe_load``.               |
    +---+-----------------------------------------------------+
    | 4 | Проверка, что корень — словарь.                     |
    +---+-----------------------------------------------------+
    | 5 | Вызов :func:`_validate` для структурной проверки.   |
    +---+-----------------------------------------------------+
    | 6 | Возврат валидированного словаря.                    |
    +---+-----------------------------------------------------+

    Args:
        path: Путь к YAML-файлу.

    Returns:
        Валидированный словарь с ключами ``root_packages``
        и ``contracts``.

    Raises:
        ConfigError: Файл не найден, не является валидным YAML,
            структура не соответствует ожидаемой, либо в YAML
            обнаружены некорректные значения.
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
        raise ConfigError(f"Файл {path} пуст или содержит только комментарии.")

    if not isinstance(data, dict):
        raise ConfigError(f"Корень {path} должен быть словарём, получено: {type(data).__name__}.")

    _validate(data)
    return data


def _validate(data: dict[str, Any]) -> None:
    """Валидирует структуру конфигурации.

    Проверяет наличие и корректность ``root_packages`` и ``contracts``,
    а также каждого контракта через :func:`_validate_contract`.

    Args:
        data: словарь из :func:`load_config`.

    Raises:
        ConfigError: при нарушении структуры.
    """
    root = data.get("root_packages")
    if not isinstance(root, list) or not root:
        raise ConfigError("'root_packages' должен быть непустым списком.")
    for item in root:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"'root_packages' содержит некорректный элемент: {item!r}")

    contracts = data.get("contracts")
    if not isinstance(contracts, list) or not contracts:
        raise ConfigError("'contracts' должен быть непустым списком.")

    seen_names: set[str] = set()
    for index, contract in enumerate(contracts):
        if not isinstance(contract, dict):
            raise ConfigError(
                f"contracts[{index}] должен быть словарём, получено: {type(contract).__name__}."
            )
        _validate_contract(contract, index, seen_names)


def _validate_contract(
    contract: dict[str, Any],
    index: int,
    seen_names: set[str],
) -> None:
    """Валидирует один контракт.

    Args:
        contract: словарь контракта.
        index: позиция в списке ``contracts`` (для сообщений об ошибках).
        seen_names: множество уже использованных имён контрактов;
            дополняется именем текущего контракта.

    Raises:
        ConfigError: при нарушении структуры контракта.
    """
    prefix = f"contracts[{index}]"

    name = contract.get("name")
    if not isinstance(name, str) or not _CONTRACT_NAME_PATTERN.match(name):
        raise ConfigError(
            f"{prefix}.name должен быть непустой строкой из [A-Za-z0-9_-]+, получено: {name!r}"
        )
    if name in seen_names:
        raise ConfigError(f"Дублирующееся имя контракта: {name!r}")
    seen_names.add(name)

    contract_type = contract.get("type")
    if contract_type not in _SUPPORTED_CONTRACT_TYPES:
        raise ConfigError(
            f"{prefix}.type должен быть одним из "
            f"{sorted(_SUPPORTED_CONTRACT_TYPES)}, "
            f"получено: {contract_type!r}"
        )

    description = contract.get("description", "")
    if not isinstance(description, str):
        raise ConfigError(f"{prefix}.description должен быть строкой.")

    _validate_string_list(
        contract,
        "source_modules",
        prefix,
        required=True,
    )
    _validate_string_list(
        contract,
        "forbidden_modules",
        prefix,
        required=True,
    )
    _validate_string_list(
        contract,
        "ignore_imports",
        prefix,
        required=False,
    )


def _validate_string_list(
    contract: dict[str, Any],
    key: str,
    prefix: str,
    *,
    required: bool,
) -> None:
    """Валидирует список строк в поле контракта.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Если поля нет и оно необязательно — выход.          |
    +---+-----------------------------------------------------+
    | 2 | Если поля нет, но оно обязательно — ошибка.         |
    +---+-----------------------------------------------------+
    | 3 | Проверка, что значение — список.                    |
    +---+-----------------------------------------------------+
    | 4 | Проверка, что список не пуст (если обязательно).    |
    +---+-----------------------------------------------------+
    | 5 | Проверка каждого элемента на непустую строку.       |
    +---+-----------------------------------------------------+

    Args:
        contract: словарь контракта.
        key: имя поля (например, ``source_modules``).
        prefix: префикс для сообщений об ошибках.
        required: если ``True``, поле обязательно и непусто.

    Raises:
        ConfigError: при нарушении структуры.
    """
    value = contract.get(key)
    if value is None:
        if required:
            raise ConfigError(f"{prefix}.{key} обязателен.")
        return

    if not isinstance(value, list):
        raise ConfigError(f"{prefix}.{key} должен быть списком строк.")

    if required and not value:
        raise ConfigError(f"{prefix}.{key} не должен быть пустым.")

    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{prefix}.{key} содержит некорректный элемент: {item!r}")


# =====================================================================
# Рендеринг
# =====================================================================


def render(config: dict[str, Any]) -> str:
    """Генерирует текст ``.importlinter``.

    Формат — INI с секциями ``[importlinter]`` и
    ``[importlinter:contract:<name>]``. Многострочные значения
    (списки модулей) оформляются через ключ ``key =`` и отступ
    в 4 пробела на каждый элемент — синтаксис, совместимый с
    ``configparser``, который использует import-linter.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Заголовок :data:`_HEADER` (без завершающего \\n).   |
    +---+-----------------------------------------------------+
    | 2 | Секция ``[importlinter]`` с ``root_packages``.      |
    +---+-----------------------------------------------------+
    | 3 | Для каждого контракта:                              |
    |   | a. Секция ``[importlinter:contract:<name>]``.       |
    |   | b. ``name = <description или name>``.               |
    |   | c. ``type = forbidden``.                            |
    |   | d. ``source_modules`` (многострочно).               |
    |   | e. ``forbidden_modules`` (многострочно).            |
    |   | f. ``ignore_imports`` (если задан).                 |
    +---+-----------------------------------------------------+
    | 4 | Сборка строк с разделителями и завершающим \\n.     |
    +---+-----------------------------------------------------+

    Args:
        config: валидированный словарь из :func:`load_config`.

    Returns:
        Полный текст ``.importlinter`` с завершающим ``\\n``.
    """
    parts: list[str] = [_HEADER.rstrip("\n"), ""]

    # Корневой блок.
    parts.append("[importlinter]")
    parts.extend(_render_list_value("root_packages", config["root_packages"]))
    parts.append("")

    # Контракты.
    for contract in config["contracts"]:
        name = contract["name"]
        description = contract.get("description") or name

        parts.append(f"[importlinter:contract:{name}]")
        parts.append(f"name = {description}")
        parts.append(f"type = {contract['type']}")
        parts.extend(_render_list_value("source_modules", contract["source_modules"]))
        parts.extend(_render_list_value("forbidden_modules", contract["forbidden_modules"]))
        ignore_imports = contract.get("ignore_imports")
        if ignore_imports:
            parts.extend(_render_list_value("ignore_imports", ignore_imports))
        parts.append("")

    return "\n".join(parts).rstrip("\n") + "\n"


def _render_list_value(key: str, values: list[str]) -> list[str]:
    """Формирует многострочное значение INI.

    Формат::

        key =
            item1
            item2

    Отступ в 4 пробела — стандарт ``configparser`` для
    многострочных значений. Пустые строки между ключами
    добавляются вызывающим кодом.

    Args:
        key: имя ключа (например, ``source_modules``).
        values: список значений.

    Returns:
        Список строк без завершающего перевода.
    """
    lines = [f"{key} ="]
    for value in values:
        lines.append(f"    {value}")
    return lines


# =====================================================================
# Файловые операции
# =====================================================================


def _atomic_write(path: Path, content: str) -> None:
    """Атомарно записывает ``content`` в ``path``.

    Файл сначала пишется во временный файл в том же каталоге,
    затем заменяется через ``os.replace``. Гарантирует, что
    читатель никогда не увидит частично записанный файл —
    критично для pre-commit: hook может быть прерван
    пользователем (Ctrl+C), но ``.importlinter`` останется
    в согласованном состоянии (старая или новая версия, но
    не «полу-записанный» файл).

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Создание каталога (если не существует).             |
    +---+-----------------------------------------------------+
    | 2 | Создание временного файла через ``mkstemp``.        |
    +---+-----------------------------------------------------+
    | 3 | Запись содержимого.                                 |
    +---+-----------------------------------------------------+
    | 4 | Замена целевого файла через ``os.replace``.         |
    +---+-----------------------------------------------------+
    | 5 | Удаление временного файла при любой ошибке          |
    |   | (finally-семантика через ``except``).               |
    +---+-----------------------------------------------------+

    Args:
        path: Целевой путь.
        content: Полное содержимое файла.

    Raises:
        OSError: При ошибке записи или замены.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{path.name}.",
        suffix=".tmp",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(content)
        os.replace(tmp_name, path)
    except Exception:
        # Очистка временного файла при любой ошибке. Подавляем
        # ошибки удаления — исходное исключение важнее.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def check(path: Path, expected: str) -> bool:
    """Проверяет актуальность ``.importlinter``.

    Сравнивает содержимое файла с ожидаемым. При расхождении
    печатает unified diff в stderr — это позволяет разработчику
    сразу увидеть, что именно изменится.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Проверка существования файла.                       |
    +---+-----------------------------------------------------+
    | 2 | Чтение содержимого.                                 |
    +---+-----------------------------------------------------+
    | 3 | Сравнение с ``expected``.                           |
    +---+-----------------------------------------------------+
    | 4 | При расхождении — печать unified diff в stderr.     |
    +---+-----------------------------------------------------+

    Args:
        path: Путь к существующему ``.importlinter``.
        expected: Ожидаемое содержимое.

    Returns:
        ``True``, если файл существует и содержимое совпадает
        с ``expected``. ``False`` в противном случае.
    """
    if not path.is_file():
        print(f"ERROR: файл не найден: {path}", file=sys.stderr)
        return False

    try:
        actual = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: не удалось прочитать {path}: {exc}", file=sys.stderr)
        return False

    if actual == expected:
        return True

    diff = difflib.unified_diff(
        actual.splitlines(keepends=True),
        expected.splitlines(keepends=True),
        fromfile=f"{path} (current)",
        tofile=f"{path} (expected)",
    )
    sys.stderr.writelines(diff)
    print(
        f"\nERROR: {path} устарел. Запустите: python tools/generate_importlinter_config.py",
        file=sys.stderr,
    )
    return False


# =====================================================================
# CLI
# =====================================================================


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Парсинг аргументов (``--exceptions``, ``--output``,  |
    |   | ``--check``).                                       |
    +---+-----------------------------------------------------+
    | 2 | Загрузка и валидация YAML.                          |
    +---+-----------------------------------------------------+
    | 3 | Рендеринг ``.importlinter``.                        |
    +---+-----------------------------------------------------+
    | 4 | Если ``--check`` — сравнение, возврат 0/1.          |
    +---+-----------------------------------------------------+
    | 5 | Иначе — атомарная запись, возврат 0/1.              |
    +---+-----------------------------------------------------+

    Args:
        argv: Аргументы командной строки без имени программы.
            По умолчанию — ``sys.argv[1:]``.

    Returns:
        Код возврата: 0 — успех, 1 — ошибка.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Генерация .importlinter из exceptions.yaml. "
            "Ручные правки .importlinter будут потеряны."
        ),
    )
    parser.add_argument(
        "--exceptions",
        type=Path,
        default=_DEFAULT_EXCEPTIONS_PATH,
        help=(
            f"Путь к YAML-файлу с описанием контрактов (по умолчанию: {_DEFAULT_EXCEPTIONS_PATH})."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT_PATH,
        help=(f"Путь к выходному .importlinter (по умолчанию: {_DEFAULT_OUTPUT_PATH})."),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Не писать файл, а проверить, что .importlinter актуален. "
            "Exit 1 при расхождении. Используется в CI."
        ),
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.exceptions)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    rendered = render(config)

    if args.check:
        if check(args.output, rendered):
            print(f"OK: {args.output} актуален.")
            return 0
        return 1

    try:
        _atomic_write(args.output, rendered)
    except OSError as exc:
        print(f"ERROR: не удалось записать {args.output}: {exc}", file=sys.stderr)
        return 1

    print(f"OK: {args.output} сгенерирован из {args.exceptions}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
