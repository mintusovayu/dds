"""
Синхронизация ``docs/deployment.md`` с ``dds_core/domain/config.py``.

Назначение
----------
Документация развёртывания содержит таблицу «Дополнительные параметры
ядра», где для каждой константы указано её значение по умолчанию.
Со временем эта таблица рассинхронизируется с реальными значениями
в ``dds_core/domain/config.py``: разработчик меняет константу, но
забывает обновить docs (или наоборот — копирует старое значение).

Инструмент устраняет этот класс ошибок: он читает таблицу из
``docs/deployment.md``, извлекает документированные значения и
сравнивает их с фактическими в конфиг-модуле. Любое расхождение —
ERROR, блокирующий merge.

Модель данных
-------------
Инструмент **data-driven**: он не имеет фиксированного списка
констант. Источник данных — таблица в ``docs/deployment.md``.
Для каждой строки вида::

    | `CONSTANT_NAME` | Описание | `VALUE` |

инструмент ищет ``CONSTANT_NAME`` в конфиг-модуле и сверяет
форматированное значение с ``VALUE``. Это устраняет дублирование:
разработчик правит docs и config, а не третий hard-coded список
в тулинге.

Дополнительно проверяется реестр ``OPERATION_TIMEOUTS``. Он
документируется отдельной таблицей с колонками
``Дескриптор | Таймаут, с | Операция``: инструмент извлекает все
пары (descriptor, значение) и сравнивает с содержимым словаря
``config.OPERATION_TIMEOUTS``. Проверяются все три множества:
ключи только в config, ключи только в docs, несовпадения значений.

Что проверяется
---------------

+----------------------------------+----------------------------------+
| Проверка                         | Источник данных                  |
+==================================+==================================+
| Наличие константы в config       | Каждая строка таблицы docs.      |
+----------------------------------+----------------------------------+
| Совпадение скалярного значения   | Значение в 3-й колонке таблицы   |
|                                  | (обёрнуто в `` `` `` `` ``).     |
+----------------------------------+----------------------------------+
| Совпадение значений              | Таблица «Дескриптор | Таймаут».  |
| ``OPERATION_TIMEOUTS``           |                                  |
+----------------------------------+----------------------------------+
| Наличие всех ключей              | Множества ключей config vs docs  |
| ``OPERATION_TIMEOUTS`` в docs    | для реестра таймаутов.           |
+----------------------------------+----------------------------------+
| Отсутствие лишних ключей         | Множества ключей config vs docs  |
| ``OPERATION_TIMEOUTS`` в config  | для реестра таймаутов.           |
+----------------------------------+----------------------------------+

Форматирование значений
-----------------------
Скалярные значения сравниваются после нормализации через
:func:`_format_config_value`:

+----------------------------------+----------------------------------+
| Тип                              | Формат                           |
+==================================+==================================+
| ``bool``                         | ``True`` / ``False``             |
+----------------------------------+----------------------------------+
| ``str``                          | ``"<value>"`` (в двойных кавычках)|
+----------------------------------+----------------------------------+
| ``int``                          | ``str(value)``                   |
+----------------------------------+----------------------------------+
| ``float``                        | ``str(value)``                   |
+----------------------------------+----------------------------------+
| прочее                           | ``repr(value)``                  |
+----------------------------------+----------------------------------+

Значения ``OPERATION_TIMEOUTS`` сравниваются как ``float`` —
устраняет различие ``300`` (docs) vs ``300.0`` (config).

Что НЕ проверяется
------------------
- **Полнота docs**: если в config есть константа, отсутствующая в
  таблице docs, это не ошибка. Причина: не все константы нужно
  документировать (например, внутренние служебные).
- **Константы вне таблицы**: упоминания в прозе (``SCAN_COMMIT_INTERVAL
  = 500``) игнорируются; проверяется только структурированная
  таблица.
- **Значения составных типов** (tuple, dict, list) — сравниваются
  через :func:`_format_config_value`; при несовпадении диагностика
  может быть неинформативной. Рекомендуется не документировать
  составные типы в таблице констант.

Exit codes
----------
- 0 — расхождений нет.
- 1 — обнаружены расхождения или ошибки чтения/парсинга.

CLI
---
.. code-block:: bash

    # Проверка по умолчанию
    python tools/verify_docs_consistency.py

    # Явные пути
    python tools/verify_docs_consistency.py \\
        --docs docs/deployment.md \\
        --config dds_core/domain/config.py

Принципы:
    - Модуль не изменяет файловую систему.
    - Не имеет побочных эффектов при импорте.
    - Зависимости: только stdlib.
    - Deterministic output: находки сортируются по (path, line).
    - Конфиг-модуль загружается через ``importlib``; ожидается, что
      ``config.py`` не имеет внутренних импортов проекта (в текущей
      версии — так и есть).
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

# =====================================================================
# Константы
# =====================================================================

_DEFAULT_DOCS_PATH = Path("docs/deployment.md")
"""Путь к документации развёртывания по умолчанию."""

_DEFAULT_CONFIG_PATH = Path("dds_core/domain/config.py")
"""Путь к конфиг-модулю по умолчанию."""

_IDENTIFIER_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
"""Регулярное выражение для имён констант (верхний регистр, подчёркивания)."""

_FULLY_BACKTICKED = re.compile(r"^`([^`]+)`$")
"""Регулярное выражение для ячейки, полностью обёрнутой в `` `` ``."""

_TIMEOUTS_TABLE_HEADER_TRIGGERS = ("Дескриптор", "Таймаут")
"""Признаки заголовка таблицы реестра таймаутов.

Заголовок считается найденным, если в строке присутствуют все
перечисленные подстроки. Для DDS это единственная таблица с таким
сочетанием.
"""


# =====================================================================
# Исключения
# =====================================================================


class ConfigError(Exception):
    """Ошибка загрузки конфиг-модуля или docs-файла.

    Поднимается при проблемах, делающих проверку невозможной:
    файл не найден, не читается, конфиг-модуль не загружается.
    Не используется для сообщений о расхождениях — они собираются
    в список ``errors`` в :func:`main`.
    """


# =====================================================================
# Модели
# =====================================================================


@dataclass(slots=True)
class _DocumentedValue:
    """Документированное значение из таблицы docs.

    Attributes:
        raw: Сырое значение, извлечённое из ячейки (без бэктиков).
        line: Номер строки в файле docs (1-based).
    """

    raw: str
    line: int


# =====================================================================
# Загрузка конфиг-модуля
# =====================================================================


def load_config_module(path: Path) -> ModuleType:
    """Загружает конфиг-модуль через ``importlib``.

    Модуль ``config.py`` — набор констант без внутренних импортов
    проекта (только ``from __future__ import annotations``). Это
    позволяет загрузить его изолированно через
    ``spec_from_file_location``, без изменения ``sys.path`` и без
    побочных эффектов для других инструментов.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Проверка существования файла.                       |
    +---+-----------------------------------------------------+
    | 2 | Создание spec через ``spec_from_file_location``.    |
    +---+-----------------------------------------------------+
    | 3 | Создание модуля через ``module_from_spec``.         |
    +---+-----------------------------------------------------+
    | 4 | Выполнение модуля через ``exec_module``.            |
    +---+-----------------------------------------------------+
    | 5 | Возврат загруженного модуля.                        |
    +---+-----------------------------------------------------+

    Args:
        path: Путь к ``config.py``.

    Returns:
        Загруженный модуль. Атрибуты доступны через ``getattr``.

    Raises:
        ConfigError: файл не найден, не читается, не компилируется
            или выбрасывает исключение при выполнении.
    """
    if not path.is_file():
        raise ConfigError(f"Конфиг-модуль не найден: {path}")

    # Уникальное имя модуля, чтобы не конфликтовать с реальным
    # dds_core.domain.config при его параллельной загрузке.
    module_name = "_dds_docs_check_config"

    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
    except (ValueError, ImportError) as exc:
        raise ConfigError(f"Не удалось создать spec для {path}: {exc}") from exc

    if spec is None or spec.loader is None:
        raise ConfigError(f"Некорректный spec для {path}.")

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ConfigError(f"Ошибка импорта {path}: {exc}") from exc

    return module


# =====================================================================
# Утилиты парсинга ячеек
# =====================================================================


def _parse_value_cell(cell: str) -> str | None:
    """Извлекает значение из ячейки, если она полностью обёрнута в бэктики.

    Обрабатывает bold-wrapping (``**``6``**``), который используется
    для акцентирования значений в docs. Нормализация:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Обрезка пробелов.                                   |
    +---+-----------------------------------------------------+
    | 2 | Обрезка ``*`` по краям (снятие bold).               |
    +---+-----------------------------------------------------+
    | 3 | Проверка полного совпадения с ``^`([^`]+)`$``.      |
    +---+-----------------------------------------------------+
    | 4 | Возврат содержимого между бэктиками либо ``None``.  |
    +---+-----------------------------------------------------+

    Args:
        cell: Содержимое ячейки Markdown-таблицы.

    Returns:
        Значение без бэктиков или ``None``, если ячейка не является
        полностью обёрнутым значением (например, ``см. `config.py```).
    """
    normalized = cell.strip().strip("*").strip()
    match = _FULLY_BACKTICKED.match(normalized)
    if match is None:
        return None
    return match.group(1)


def _split_markdown_row(line: str) -> list[str] | None:
    """Разбивает строку Markdown-таблицы на ячейки.

    Возвращает ``None``, если строка не является строкой таблицы
    (не начинается и не заканчивается символом ``|``).

    Args:
        line: Строка файла.

    Returns:
        Список ячеек (без обёртки ``|``) или ``None``.
    """
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    # Убираем крайние ``|`` и делим по внутренним.
    inner = stripped[1:-1]
    return [cell.strip() for cell in inner.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    """Проверяет, является ли строка таблицы разделителем.

    Разделитель Markdown-таблицы состоит из ячеек вида ``---``
    (опционально с ``:`` по краям для выравнивания).

    Args:
        cells: Ячейки строки.

    Returns:
        ``True``, если все ячейки — разделители.
    """
    if not cells:
        return False
    for cell in cells:
        cleaned = cell.replace(":", "").strip()
        if cleaned and set(cleaned) != {"-"}:
            return False
    return True


# =====================================================================
# Парсинг документации
# =====================================================================


def extract_documented_constants(
    text: str,
) -> dict[str, _DocumentedValue]:
    """Извлекает документированные константы из таблицы.

    Ищет строки вида::

        | `CONSTANT_NAME` | Описание | `VALUE` |

    где ``CONSTANT_NAME`` соответствует :data:`_IDENTIFIER_PATTERN`,
    а ``VALUE`` полностью обёрнуто в бэктики. Строки, где значение
    не является полностью обёрнутым (например, ``см. `config.py```),
    пропускаются — они документируют константу без скалярного
    значения (например, ``OPERATION_TIMEOUTS``).

    При дублировании имени в таблице последнее значение
    перезаписывает предыдущее (соответствует семантике словаря
    Python).

    Args:
        text: Полный текст ``docs/deployment.md``.

    Returns:
        Словарь ``name → _DocumentedValue``.
    """
    result: dict[str, _DocumentedValue] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        cells = _split_markdown_row(line)
        if cells is None or len(cells) < 2:
            continue
        if _is_separator_row(cells):
            continue

        name_match = _FULLY_BACKTICKED.match(cells[0])
        if name_match is None:
            continue
        name = name_match.group(1)
        if not _IDENTIFIER_PATTERN.match(name):
            continue

        value = _parse_value_cell(cells[-1])
        if value is None:
            continue

        result[name] = _DocumentedValue(raw=value, line=lineno)

    return result


def extract_documented_timeouts(text: str) -> dict[str, float]:
    """Извлекает документированные таймауты операций.

    Ищет таблицу с заголовком, содержащим ``Дескриптор`` и
    ``Таймаут``. Внутри таблицы обрабатываются строки вида::

        | `descriptor.name` | 300 | Описание... |

    где первая ячейка полностью обёрнута в бэктики, а вторая —
    числовое значение (int или float).

    Таблица считается завершённой при первой строке, не начинающейся
    с ``|``. Это позволяет корректно обрабатывать несколько таблиц
    подряд.

    Args:
        text: Полный текст ``docs/deployment.md``.

    Returns:
        Словарь ``descriptor → timeout_seconds``. Пустой словарь,
        если таблица не найдена.
    """
    result: dict[str, float] = {}
    in_timeouts_table = False

    for line in text.splitlines():
        cells = _split_markdown_row(line)
        if cells is None:
            # Строка не является частью таблицы. Если мы были
            # внутри таблицы таймаутов — она завершена.
            in_timeouts_table = False
            continue

        if not in_timeouts_table:
            # Проверка заголовка таблицы таймаутов.
            header_text = " ".join(cells)
            if all(t in header_text for t in _TIMEOUTS_TABLE_HEADER_TRIGGERS):
                in_timeouts_table = True
            continue

        # Мы внутри таблицы таймаутов.
        if _is_separator_row(cells):
            continue
        if len(cells) < 2:
            continue

        name_match = _FULLY_BACKTICKED.match(cells[0])
        if name_match is None:
            # Строка без ожидаемого формата — конец таблицы.
            in_timeouts_table = False
            continue

        try:
            value = float(cells[1])
        except ValueError:
            continue

        result[name_match.group(1)] = value

    return result


# =====================================================================
# Сравнение значений
# =====================================================================


def format_config_value(value: Any) -> str:
    """Форматирует значение константы для сравнения с docs.

    Правила форматирования соответствуют тому, как значения
    записываются в ``docs/deployment.md``:

    +----------------------------------+----------------------------------+
    | Тип Python                       | Результат                        |
    +==================================+==================================+
    | ``bool``                         | ``True`` / ``False``             |
    +----------------------------------+----------------------------------+
    | ``str``                          | ``"<value>"`` (в двойных кавычках)|
    +----------------------------------+----------------------------------+
    | ``int``                          | ``str(value)``                   |
    +----------------------------------+----------------------------------+
    | ``float``                        | ``str(value)``                   |
    +----------------------------------+----------------------------------+
    | прочее                           | ``repr(value)``                  |
    +----------------------------------+----------------------------------+

    Примечание: ``bool`` проверяется **до** ``int``, потому что
    ``bool`` является подклассом ``int`` в Python; без явной
    проверки ``True`` был бы отформатирован как ``1``.

    Args:
        value: Значение из конфиг-модуля.

    Returns:
        Строковое представление для сравнения.
    """
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, (int, float)):
        return str(value)
    return repr(value)


# =====================================================================
# Проверки
# =====================================================================


def check_scalar_constants(
    config: ModuleType,
    documented: dict[str, _DocumentedValue],
) -> list[str]:
    """Проверяет скалярные константы из таблицы docs против config.

    Для каждой документированной константы:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Проверка наличия атрибута в config-модуле.          |
    |   | Отсутствует → ERROR.                                |
    +---+-----------------------------------------------------+
    | 2 | Форматирование фактического значения через          |
    |   | :func:`format_config_value`.                        |
    +---+-----------------------------------------------------+
    | 3 | Сравнение с документированным значением.            |
    |   | Расхождение → ERROR.                                |
    +---+-----------------------------------------------------+

    ``OPERATION_TIMEOUTS`` пропускается — он проверяется отдельно
    через :func:`check_operation_timeouts`.

    Args:
        config: Загруженный конфиг-модуль.
        documented: Результат :func:`extract_documented_constants`.

    Returns:
        Список сообщений об ошибках. Пустой список, если расхождений
        нет.
    """
    errors: list[str] = []
    for name in sorted(documented):
        if name == "OPERATION_TIMEOUTS":
            # Проверяется отдельно — это словарь, а не скаляр.
            continue

        doc = documented[name]
        if not hasattr(config, name):
            errors.append(
                f"{name}: документировано в docs (строка {doc.line}), но отсутствует в config."
            )
            continue

        actual = getattr(config, name)
        actual_str = format_config_value(actual)
        if actual_str != doc.raw:
            errors.append(
                f"{name}: расхождение (docs строка {doc.line}): "
                f"docs={doc.raw!r}, config={actual_str!r}."
            )

    return errors


def check_operation_timeouts(
    config: ModuleType,
    documented: dict[str, float],
) -> list[str]:
    """Проверяет словарь ``OPERATION_TIMEOUTS`` против docs.

    Сравниваются три множества:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Ключи в ``config.OPERATION_TIMEOUTS``, отсутствующие |
    |   | в docs → ERROR (docs неполны).                      |
    +---+-----------------------------------------------------+
    | 2 | Ключи в docs, отсутствующие в config → ERROR        |
    |   | (устаревшая запись в docs).                         |
    +---+-----------------------------------------------------+
    | 3 | Значения различаются → ERROR.                       |
    +---+-----------------------------------------------------+

    Значения сравниваются как ``float`` — устраняет различие
    ``300`` (docs) vs ``300.0`` (config).

    Если в docs не найдена таблица таймаутов (пустой ``documented``)
    и в config отсутствует ``OPERATION_TIMEOUTS`` — ошибок нет.
    Если в config есть словарь, но в docs — нет таблицы, это ERROR:
    реестр должен быть документирован.

    Args:
        config: Загруженный конфиг-модуль.
        documented: Результат :func:`extract_documented_timeouts`.

    Returns:
        Список сообщений об ошибках. Пустой список, если расхождений
        нет.
    """
    errors: list[str] = []

    config_timeouts = getattr(config, "OPERATION_TIMEOUTS", None)
    if config_timeouts is None:
        # В config нет реестра. Если в docs он документирован —
        # это ошибка (docs ссылается на несуществующий атрибут).
        if documented:
            errors.append("docs документирует OPERATION_TIMEOUTS, но в config нет такого атрибута.")
        return errors

    if not isinstance(config_timeouts, dict):
        errors.append(
            f"config.OPERATION_TIMEOUTS должен быть словарём, "
            f"получено {type(config_timeouts).__name__}."
        )
        return errors

    if not documented:
        errors.append(
            "config содержит OPERATION_TIMEOUTS, но в docs нет "
            "таблицы с колонками 'Дескриптор' и 'Таймаут'."
        )
        return errors

    config_keys = set(config_timeouts.keys())
    doc_keys = set(documented.keys())

    for key in sorted(config_keys - doc_keys):
        errors.append(
            f"OPERATION_TIMEOUTS: ключ {key!r} есть в config, но не документирован в docs."
        )

    for key in sorted(doc_keys - config_keys):
        errors.append(
            f"OPERATION_TIMEOUTS: ключ {key!r} документирован в docs, но отсутствует в config."
        )

    for key in sorted(config_keys & doc_keys):
        config_value = config_timeouts[key]
        try:
            config_value_float = float(config_value)
        except (TypeError, ValueError):
            errors.append(
                f"OPERATION_TIMEOUTS[{key!r}]: значение в config "
                f"не является числом ({config_value!r})."
            )
            continue

        doc_value = documented[key]
        if config_value_float != doc_value:
            errors.append(
                f"OPERATION_TIMEOUTS[{key!r}]: расхождение — "
                f"docs={doc_value}, config={config_value_float}."
            )

    return errors


# =====================================================================
# Отчёт
# =====================================================================


def print_report(
    config_path: Path,
    docs_path: Path,
    documented_constants_count: int,
    documented_timeouts_count: int,
    errors: list[str],
) -> None:
    """Печатает отчёт в stdout/stderr.

    Формат:

    - Сводка (пути, количество найденных записей) — в stdout.
    - Список ошибок — в stderr с префиксом ``ERROR:``.
    - Итог — ``OK`` или ``FAILED`` в stderr.

    Args:
        config_path: Путь к конфиг-модулю (для справки).
        docs_path: Путь к docs-файлу (для справки).
        documented_constants_count: Количество найденных констант.
        documented_timeouts_count: Количество найденных таймаутов.
        errors: Список сообщений об ошибках.
    """
    print(f"Config: {config_path}")
    print(f"Docs:   {docs_path}")
    print(f"Documented constants: {documented_constants_count}")
    print(f"Documented timeouts:  {documented_timeouts_count}")
    print(f"Errors: {len(errors)}")
    print()

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(file=sys.stderr)
        print(
            "FAILED: обнаружены расхождения между docs и config.",
            file=sys.stderr,
        )
    else:
        print("OK: docs и config синхронизированы.", file=sys.stderr)


# =====================================================================
# CLI
# =====================================================================


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Парсинг аргументов (``--docs``, ``--config``).      |
    +---+-----------------------------------------------------+
    | 2 | Загрузка docs-файла.                                |
    +---+-----------------------------------------------------+
    | 3 | Загрузка config-модуля.                             |
    +---+-----------------------------------------------------+
    | 4 | Извлечение документированных констант и таймаутов.  |
    +---+-----------------------------------------------------+
    | 5 | Проверка скалярных констант.                        |
    +---+-----------------------------------------------------+
    | 6 | Проверка OPERATION_TIMEOUTS.                        |
    +---+-----------------------------------------------------+
    | 7 | Печать отчёта; возврат 0/1.                         |
    +---+-----------------------------------------------------+

    Args:
        argv: Аргументы командной строки без имени программы.

    Returns:
        Код возврата: 0 — расхождений нет, 1 — есть ошибки.
    """
    parser = argparse.ArgumentParser(
        description=("Проверка синхронизации docs/deployment.md с dds_core/domain/config.py."),
    )
    parser.add_argument(
        "--docs",
        type=Path,
        default=_DEFAULT_DOCS_PATH,
        help=f"Путь к docs-файлу (по умолчанию: {_DEFAULT_DOCS_PATH}).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG_PATH,
        help=f"Путь к config-модулю (по умолчанию: {_DEFAULT_CONFIG_PATH}).",
    )
    args = parser.parse_args(argv)

    # 1. Загрузка docs.
    if not args.docs.is_file():
        print(f"ERROR: файл не найден: {args.docs}", file=sys.stderr)
        return 1
    try:
        docs_text = args.docs.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: не удалось прочитать {args.docs}: {exc}", file=sys.stderr)
        return 1

    # 2. Загрузка config-модуля.
    try:
        config = load_config_module(args.config)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # 3. Парсинг.
    documented_constants = extract_documented_constants(docs_text)
    documented_timeouts = extract_documented_timeouts(docs_text)

    # 4. Проверки.
    errors: list[str] = []
    errors.extend(check_scalar_constants(config, documented_constants))
    errors.extend(check_operation_timeouts(config, documented_timeouts))

    # 5. Отчёт.
    print_report(
        config_path=args.config,
        docs_path=args.docs,
        documented_constants_count=len(documented_constants),
        documented_timeouts_count=len(documented_timeouts),
        errors=errors,
    )

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
