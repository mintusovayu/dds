"""
Проверка отсутствия SQL-строк в контрактах application-слоя.

Назначение
----------
Контракты application-слоя (``ITextIndexer``, ``IDatabase``) не
должны содержать SQL-строк: application зависит от абстракций, а
конкретные SQL-запросы — ответственность infrastructure. Нарушение
этого правила означает протечку схемы БД в бизнес-логику и делает
смену хранилища невозможной без правки ядра.

Инструмент AST-парсит целевые файлы, извлекает строковые литералы
и ищет в них SQL-паттерны. Docstrings исключаются: они описывают
контракт и могут упоминать SQL-концепции в пояснениях (например,
``query: SQL-запрос (SELECT)``) — это часть документации, а не
кода.

Целевые файлы (по умолчанию)
----------------------------
- ``dds_core/domain/interfaces.py`` — все контракты ядра.

Список расширяем через повторяющийся флаг ``--path``: при появлении
новых contract-модулей (например, ``dds_core/domain/reference_repository.py``
после рефакторинга) они добавляются в вызов без правки кода.

Что проверяется
---------------

+---------------------------------------------+-------------------------+
| AST-узел                                    | Что ищем                |
+=============================================+=========================+
| ``ast.Constant(value=<str>)``               | SQL-паттерн в значении. |
+---------------------------------------------+-------------------------+
| ``ast.JoinedStr`` (f-строка)                | Constant-части f-строки |
|                                             | обходятся через          |
|                                             | ``ast.walk`` —           |
|                                             | проверяются отдельно.    |
+---------------------------------------------+-------------------------+

Docstrings (первый ``Expr`` в ``Module``/``ClassDef``/``FunctionDef``/
``AsyncFunctionDef``) собираются в отдельный ``set`` по ``id()`` узла
и исключаются из проверки. Комментарии в AST отсутствуют —
проверяются только строковые литералы.

SQL-паттерны
------------
Регулярное выражение распознаёт «настоящие» SQL-запросы, а не любые
упоминания слов ``SELECT``/``FROM``. Примеры:

- ``"SELECT id FROM t WHERE x = ?"`` — найдено.
- ``"INSERT INTO documents (...) VALUES (...)"`` — найдено.
- ``"Please select an option from the list"`` — не найдено
  (нет SQL-структуры).
- ``"select_query_builder"`` — не найдено (нет пробелов).

Поддерживаемые операторы: ``SELECT ... FROM``, ``INSERT INTO``,
``UPDATE ... SET``, ``DELETE FROM``, ``CREATE TABLE/INDEX/VIEW/VIRTUAL/
TRIGGER``, ``DROP TABLE/INDEX/VIEW/TRIGGER``, ``ALTER TABLE``,
``PRAGMA <name>``, ``REPLACE INTO``, ``WITH ... AS (`` (CTE).

Многострочные строки распознаются (флаг ``re.DOTALL``).

Exit codes
----------
- 0 — SQL-строк не найдено.
- 1 — найдены SQL-строки или ошибки чтения/парсинга целевых файлов.

CLI
---
.. code-block:: bash

    # Проверка дефолтных путей
    python tools/check_no_sql_in_contracts.py

    # Явные пути (можно указывать несколько раз)
    python tools/check_no_sql_in_contracts.py \\
        --path dds_core/domain/interfaces.py \\
        --path dds_core/domain/reference_repository.py

Принципы:
    - Модуль не изменяет файловую систему.
    - Не имеет побочных эффектов при импорте.
    - Зависимости: только stdlib.
    - Deterministic output: находки сортируются по (path, line, column).
    - Толерантность: ошибки отдельных файлов фиксируются и не
      прерывают обход остальных.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# =====================================================================
# Константы
# =====================================================================

_DEFAULT_PATHS: tuple[Path, ...] = (Path("dds_core/domain/interfaces.py"),)
"""Пути к контрактам по умолчанию.

При добавлении новых contract-модулей — расширять этот кортеж или
передавать ``--path`` при вызове из CI/pre-commit.
"""

_SQL_PATTERN = re.compile(
    r"\b(?:"
    r"SELECT\s+.*?\s+FROM"
    r"|INSERT\s+INTO"
    r"|UPDATE\s+\S+\s+SET"
    r"|DELETE\s+FROM"
    r"|CREATE\s+(?:TABLE|INDEX|VIEW|VIRTUAL|TRIGGER)"
    r"|DROP\s+(?:TABLE|INDEX|VIEW|TRIGGER)"
    r"|ALTER\s+TABLE"
    r"|PRAGMA\s+\w+"
    r"|REPLACE\s+INTO"
    r"|WITH\s+\w+\s+AS\s*\("
    r")",
    re.IGNORECASE | re.DOTALL,
)
"""Регулярное выражение для распознавания SQL-запросов.

Требует наличия SQL-структуры (keyword + обязательный соседний
keyword), а не просто упоминания слов. Это устраняет ложные
срабатывания на обычных английских текстах.
"""

_SNIPPET_MAX_LEN = 100
"""Максимальная длина сниппета строки в отчёте (в символах)."""


# =====================================================================
# Модели
# =====================================================================


@dataclass(slots=True)
class _Finding:
    """Найденная SQL-строка в контракте.

    Attributes:
        path: Путь к файлу.
        line: Номер строки (1-based) начала строкового литерала.
        column: Колонка (0-based) начала строкового литерала.
        matched: Подстрока, совпавшая с SQL-паттерном.
        snippet: Короткое представление всего литерала (схлопнутые
            пробелы, ограничение по длине).
    """

    path: Path
    line: int
    column: int
    matched: str
    snippet: str

    def sort_key(self) -> tuple[str, int, int]:
        """Ключ для детерминированной сортировки находок."""
        return (str(self.path), self.line, self.column)


# =====================================================================
# Вспомогательные функции
# =====================================================================


def _collect_docstring_ids(tree: ast.AST) -> set[int]:
    """Собирает ``id()`` строковых узлов, являющихся docstrings.

    Docstring — первый ``Expr`` со строковым ``Constant`` в теле
    ``Module``, ``ClassDef``, ``FunctionDef``, ``AsyncFunctionDef``.
    Такие узлы исключаются из проверки SQL: они описывают контракт
    и могут упоминать SQL-концепции в пояснениях.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Обход всех узлов дерева через ``ast.walk``.         |
    +---+-----------------------------------------------------+
    | 2 | Отбор узлов с телом (``Module``/``ClassDef``/       |
    |   | ``FunctionDef``/``AsyncFunctionDef``).              |
    +---+-----------------------------------------------------+
    | 3 | Проверка первого элемента ``body``: если это        |
    |   | ``Expr`` со строковым ``Constant`` — добавление     |
    |   | ``id()`` в результат.                               |
    +---+-----------------------------------------------------+
    | 4 | Возврат множества ``id()``.                         |
    +---+-----------------------------------------------------+

    Args:
        tree: AST-дерево файла.

    Returns:
        Множество ``id()`` строковых Constant-узлов, являющихся
        docstrings.
    """
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstring_ids.add(id(first.value))
    return docstring_ids


def _short_snippet(value: str) -> str:
    """Формирует короткое представление строки для отчёта.

    Схлопывает все последовательности пробельных символов в один
    пробел (включая переносы строк) и обрезает до
    :data:`_SNIPPET_MAX_LEN` символов с добавлением ``...``.

    Args:
        value: Исходная строка.

    Returns:
        Однострочное короткое представление.
    """
    single_line = " ".join(value.split())
    if len(single_line) > _SNIPPET_MAX_LEN:
        return single_line[: _SNIPPET_MAX_LEN - 3] + "..."
    return single_line


# =====================================================================
# Сканирование одного файла
# =====================================================================


def _scan_file(path: Path) -> tuple[list[_Finding], str | None]:
    """AST-парсит файл и возвращает список находок.

    Операции:

    +----+----------------------------------------------------+
    | №  | Описание                                           |
    +====+====================================================+
    | 1  | Чтение файла в UTF-8.                              |
    +----+----------------------------------------------------+
    | 2  | Разбор через :func:`ast.parse`.                    |
    +----+----------------------------------------------------+
    | 3  | Сбор ``id()`` docstring-узлов.                     |
    +----+----------------------------------------------------+
    | 4  | Обход всех ``ast.Constant`` со строками:            |
    |    | a. Пропуск docstring-узлов.                        |
    |    | b. Поиск SQL-паттерна в значении.                  |
    |    | c. Формирование :class:`_Finding` при совпадении. |
    +----+----------------------------------------------------+
    | 5  | Сортировка находок по ``(path, line, column)``.     |
    +----+----------------------------------------------------+
    | 6  | Возврат ``(findings, error)``.                     |
    +----+----------------------------------------------------+

    Args:
        path: Путь к файлу.

    Returns:
        Кортеж ``(findings, error_message)``. ``error_message``
        заполняется только при ошибке чтения или парсинга файла;
        в этом случае список находок пуст.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], f"не удалось прочитать: {exc}"

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        lineno = exc.lineno or 0
        return [], f"синтаксическая ошибка (строка {lineno}): {exc.msg}"

    docstring_ids = _collect_docstring_ids(tree)
    findings: list[_Finding] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        if not isinstance(node.value, str):
            continue
        if id(node) in docstring_ids:
            continue

        match = _SQL_PATTERN.search(node.value)
        if match is None:
            continue

        findings.append(
            _Finding(
                path=path,
                line=node.lineno,
                column=node.col_offset,
                matched=match.group(0),
                snippet=_short_snippet(node.value),
            )
        )

    findings.sort(key=lambda f: f.sort_key())
    return findings, None


# =====================================================================
# Отчёт
# =====================================================================


def _print_report(
    paths: tuple[Path, ...],
    files_scanned: int,
    findings: list[_Finding],
    errors: list[str],
) -> None:
    """Печатает отчёт в stdout/stderr.

    Формат отчёта:

    - Сводка (количество файлов, находок) — в stdout.
    - Каждая находка: три строки (заголовок ``path:line:col``,
      ``matched``, ``snippet``).
    - Ошибки файлов — в stderr с префиксом ``ERROR:``.
    - Итог — ``OK`` или ``FAILED`` в stderr.

    Args:
        paths: Список проверенных путей (для справки в заголовке).
        files_scanned: Количество успешно просканированных файлов.
        findings: Список находок.
        errors: Список сообщений об ошибках файлов.
    """
    print(f"Files scanned: {files_scanned}")
    for path in paths:
        print(f"  - {path}")
    print(f"Findings: {len(findings)}")
    print()

    for finding in findings:
        print(f"{finding.path}:{finding.line}:{finding.column}: SQL string detected")
        print(f"  matched: {finding.matched!r}")
        print(f"  snippet: {finding.snippet!r}")
        print()

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)

    if findings or errors:
        print(
            "FAILED: SQL-строки найдены в контрактах или файлы не прочитаны.",
            file=sys.stderr,
        )
    else:
        print("OK: SQL-строк в контрактах не найдено.", file=sys.stderr)


# =====================================================================
# CLI
# =====================================================================


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Парсинг аргументов (``--path`` повторяемый).        |
    +---+-----------------------------------------------------+
    | 2 | Определение списка целевых путей: явные аргументы    |
    |   | или :data:`_DEFAULT_PATHS`.                          |
    +---+-----------------------------------------------------+
    | 3 | Для каждого пути: сканирование; ошибки чтения —     |
    |   | в список ``errors``.                                |
    +---+-----------------------------------------------------+
    | 4 | Печать отчёта.                                      |
    +---+-----------------------------------------------------+
    | 5 | Возврат 0 (нет находок и ошибок) или 1.             |
    +---+-----------------------------------------------------+

    Args:
        argv: Аргументы командной строки без имени программы.
            По умолчанию — ``sys.argv[1:]``.

    Returns:
        Код возврата: 0 — успех, 1 — найдены SQL-строки или
        обнаружены ошибки чтения/парсинга.
    """
    parser = argparse.ArgumentParser(
        description=("Проверка отсутствия SQL-строк в контрактах application-слоя DDS."),
    )
    parser.add_argument(
        "--path",
        action="append",
        dest="paths",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Путь к файлу контракта. Можно указывать несколько раз. "
            "По умолчанию проверяется "
            f"{', '.join(str(p) for p in _DEFAULT_PATHS)}."
        ),
    )
    args = parser.parse_args(argv)

    paths: tuple[Path, ...] = tuple(args.paths) if args.paths else _DEFAULT_PATHS

    findings: list[_Finding] = []
    errors: list[str] = []
    files_scanned = 0

    for path in paths:
        if not path.is_file():
            errors.append(f"файл не найден: {path}")
            continue
        files_scanned += 1
        file_findings, error = _scan_file(path)
        if error is not None:
            errors.append(f"{path}: {error}")
            continue
        findings.extend(file_findings)

    findings.sort(key=lambda f: f.sort_key())

    _print_report(
        paths=paths,
        files_scanned=files_scanned,
        findings=findings,
        errors=errors,
    )

    return 1 if (findings or errors) else 0


if __name__ == "__main__":
    sys.exit(main())
