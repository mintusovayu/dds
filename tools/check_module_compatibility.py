"""
Проверка совместимости внешних модулей ``dds_modules/`` с ``DEPRECATIONS.yaml``.

Назначение
----------
Внешние модули-расширения (``dds_modules/*``) используют публичный
API ядра DDS. При рефакторинге некоторые символы ядра удаляются
напрямую (``type: direct_removal``) или помечаются как deprecation-
обёртки (``type: deprecation``) в ``docs/DEPRECATIONS.yaml``.

Этот модуль автоматически проверяет, что внешние модули не используют
устаревшие символы. Все ``.py`` файлы под ``dds_modules/`` разбираются
через :mod:`ast`; найденные импорты и обращения по полному пути
сопоставляются со списком отслеживаемых символов.

Обнаруживаемые случаи
---------------------

+----+--------------------------------------------------------+
| №  | Случай                                                 |
+====+========================================================+
| 1  | ``import X.Y.Z`` — импорт модуля                       |
+----+--------------------------------------------------------+
| 2  | ``import X.Y.Z as alias`` — импорт с алиасом           |
+----+--------------------------------------------------------+
| 3  | ``from X.Y import Z`` — импорт символа                 |
+----+--------------------------------------------------------+
| 4  | ``from X.Y import Z as alias`` — импорт с алиасом      |
+----+--------------------------------------------------------+
| 5  | ``from X.Y.Z import *`` — star-импорт из модуля        |
+----+--------------------------------------------------------+
| 6  | Обращение ``X.Y.Z.W`` в коде — полный путь атрибута,   |
|    | включая случаи через alias (``from X import Y``        |
|    | + ``Y.Z.W()``)                                          |
+----+--------------------------------------------------------+

Что НЕ проверяется (осознанные ограничения)
-------------------------------------------

- **Динамические импорты** (``__import__``, ``importlib.import_module``).
  Не видны в AST. Для модулей расширения такая практика не поощряется;
  при её использовании — ответственность ревьюера.
- **Reflection** (``getattr(obj, "name")``, ``globals()[...]``).
- **Экземплярные методы** (``db.execute_write_batched_documents(...)``,
  где ``db`` — экземпляр). Требует вывода типов; ограничение применимо
  и к mypy — при необходимости используйте статическую типизацию.
- **Shadowing** модульных имён в локальной области видимости
  (``def f(dds_core): ...``). Редкий сценарий; может дать ложное
  срабатывание.

Классификация результатов
-------------------------

- **ERROR**: символ типа ``direct_removal`` с
  ``removed_in_phase <= current_phase``, либо символ типа
  ``deprecation`` с ``remove_in_phase <= current_phase``.
  Использование сломает модуль при следующем запуске.
- **WARNING**: символ типа ``deprecation`` с
  ``deprecated_in_phase <= current_phase < remove_in_phase``.
  Использование пока работает, но модуль должен быть обновлён.
- **INFO**: модули отсутствуют или не используют устаревшие символы.

Exit codes
----------
- 0 — все проверки пройдены (возможны warnings).
- 1 — обнаружены ERROR (использование удалённых символов), либо
  конфигурация не читается.

CLI
---
.. code-block:: bash

    # Проверка (по умолчанию)
    python tools/check_module_compatibility.py

    # Явные пути
    python tools/check_module_compatibility.py \\
        --modules dds_modules \\
        --deprecations docs/DEPRECATIONS.yaml \\
        --current-phase docs/architecture-decisions/current_phase.txt

Принципы:
    - Модуль не изменяет файловую систему.
    - Не имеет побочных эффектов при импорте.
    - Зависимости: stdlib + PyYAML (см. ``requirements-dev.txt``).
    - Deterministic output: файлы и находки сортируются.
    - Толерантность: ошибки чтения/парсинга отдельных файлов
      фиксируются как ERROR и не прерывают обход.
    - Ленивая валидация YAML: структурные проблемы проверяет
      ``check_deprecations.py``; этот модуль разбирает только
      необходимый минимум полей.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass
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

_DEFAULT_MODULES_DIR = Path("dds_modules")
"""Путь к каталогу внешних модулей по умолчанию."""

_DEFAULT_DEPRECATIONS_PATH = Path("docs/DEPRECATIONS.yaml")
"""Путь к ``DEPRECATIONS.yaml`` по умолчанию."""

_DEFAULT_CURRENT_PHASE_PATH = Path("docs/architecture-decisions/current_phase.txt")
"""Путь к файлу с номером текущей фазы по умолчанию."""

_TYPE_DIRECT_REMOVAL = "direct_removal"
_TYPE_DEPRECATION = "deprecation"

_STATUS_ERROR = "ERROR"
_STATUS_WARNING = "WARNING"

_SKIPPED_DIR_PARTS = frozenset({".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".ruff_cache"})
"""Служебные каталоги, исключаемые из обхода."""


# =====================================================================
# Исключения
# =====================================================================


class ConfigError(Exception):
    """Ошибка конфигурации (нечитаемый ``current_phase.txt`` и т.п.).

    Не используется для сообщений о находках — для них применяется
    собственный механизм (список :class:`_Finding`).
    """


# =====================================================================
# Загрузка конфигурации
# =====================================================================


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


def load_deprecations(path: Path) -> dict[str, Any]:
    """Читает ``DEPRECATIONS.yaml`` без глубокой валидации.

    Полная структурная валидация записей выполняется
    ``check_deprecations.py``. Здесь проверяется только, что файл
    читается, является YAML-словарём и содержит список ``entries``
    (если поле присутствует). Некорректные записи игнорируются
    при разборе — их всё равно пометит `check_deprecations`.

    Args:
        path: Путь к YAML-файлу.

    Returns:
        Словарь с данными файла.

    Raises:
        ConfigError: файл не найден, не читается, не является
            валидным YAML или корень не словарь.
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
        # Пустой файл — это не ошибка для нашей задачи:
        # нет отслеживаемых символов, находок не будет.
        return {"entries": []}

    if not isinstance(data, dict):
        raise ConfigError(f"Корень {path} должен быть словарём, получено: {type(data).__name__}.")

    return data


# =====================================================================
# Модели
# =====================================================================


@dataclass(slots=True)
class _TrackedSymbol:
    """Отслеживаемый символ из ``DEPRECATIONS.yaml``.

    Хранит минимально необходимую информацию для классификации:
    тип (``direct_removal`` / ``deprecation``) и фазы, определяющие,
    когда использование символа считается ошибкой или
    предупреждением.

    Attributes:
        symbol: Полный путь символа (``package.module.attr``).
        kind: ``"direct_removal"`` или ``"deprecation"``.
        removed_in_phase: Фаза удаления (для ``direct_removal``).
        deprecated_in_phase: Фаза ввода deprecation.
        remove_in_phase: Фаза удаления deprecation-обёртки.
    """

    symbol: str
    kind: str
    removed_in_phase: int | None = None
    deprecated_in_phase: int | None = None
    remove_in_phase: int | None = None

    def classify(self, current_phase: int) -> str | None:
        """Классифицирует статус символа для текущей фазы.

        Args:
            current_phase: Номер текущей фазы.

        Returns:
            :data:`_STATUS_ERROR`, :data:`_STATUS_WARNING` или
            ``None``, если символ ещё не вступил в силу (или уже
            не отслеживается).
        """
        if self.kind == _TYPE_DIRECT_REMOVAL:
            assert self.removed_in_phase is not None  # инвариант разбора
            if current_phase >= self.removed_in_phase:
                return _STATUS_ERROR
            return None

        if self.kind == _TYPE_DEPRECATION:
            assert self.deprecated_in_phase is not None
            assert self.remove_in_phase is not None
            if current_phase >= self.remove_in_phase:
                return _STATUS_ERROR
            if current_phase >= self.deprecated_in_phase:
                return _STATUS_WARNING
            return None

        return None

    def phase_description(self) -> str:
        """Возвращает человекочитаемое описание фаз символа."""
        if self.kind == _TYPE_DIRECT_REMOVAL:
            return f"{_TYPE_DIRECT_REMOVAL}, removed in phase {self.removed_in_phase}"
        return (
            f"{_TYPE_DEPRECATION}, deprecated in phase "
            f"{self.deprecated_in_phase}, remove by phase "
            f"{self.remove_in_phase}"
        )


@dataclass(slots=True)
class _Finding:
    """Одно использование отслеживаемого символа в модуле.

    Attributes:
        path: Путь к файлу модуля.
        line: Номер строки (1-based); 0 — если строку определить
            не удалось (ошибка чтения).
        symbol: Полный путь использованного символа.
        status: :data:`_STATUS_ERROR` или :data:`_STATUS_WARNING`.
        phase_description: Описание фаз символа.
        usage: Краткое описание использования (строка кода).
    """

    path: Path
    line: int
    symbol: str
    status: str
    phase_description: str
    usage: str

    def sort_key(self) -> tuple[str, int, str]:
        """Ключ для детерминированной сортировки находок."""
        return (str(self.path), self.line, self.symbol)


# =====================================================================
# Разбор DEPRECATIONS.yaml
# =====================================================================


def collect_tracked_symbols(data: dict[str, Any]) -> list[_TrackedSymbol]:
    """Извлекает отслеживаемые символы из ``DEPRECATIONS.yaml``.

    Ленивая валидация: некорректные записи пропускаются молча —
    их структурную проверку выполняет ``check_deprecations.py``.
    Задача этого модуля — обнаружить использования, а не
    дублировать валидацию схемы.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Итерация по записям ``entries``.                    |
    +---+-----------------------------------------------------+
    | 2 | Проверка типов ``symbol`` и ``type``.               |
    +---+-----------------------------------------------------+
    | 3 | Для ``direct_removal``: проверка ``removed_in_``    |
    |   | ``phase`` — целое неотрицательное.                  |
    +---+-----------------------------------------------------+
    | 4 | Для ``deprecation``: проверка ``deprecated_in_``    |
    |   | ``phase`` и ``remove_in_phase`` — целые             |
    |   | неотрицательные, причём ``deprecated < remove``.    |
    +---+-----------------------------------------------------+
    | 5 | Формирование :class:`_TrackedSymbol`.               |
    +---+-----------------------------------------------------+

    Args:
        data: словарь из :func:`load_deprecations`.

    Returns:
        Список отслеживаемых символов. Порядок соответствует
        порядку записей в YAML (детерминирован).
    """
    result: list[_TrackedSymbol] = []
    raw_entries = data.get("entries") or []
    if not isinstance(raw_entries, list):
        return result

    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue

        symbol = raw.get("symbol")
        kind = raw.get("type")
        if not isinstance(symbol, str) or not symbol.strip():
            continue
        if kind not in (_TYPE_DIRECT_REMOVAL, _TYPE_DEPRECATION):
            continue
        symbol = symbol.strip()

        if kind == _TYPE_DIRECT_REMOVAL:
            phase = raw.get("removed_in_phase")
            if not isinstance(phase, int) or phase < 0:
                continue
            result.append(
                _TrackedSymbol(
                    symbol=symbol,
                    kind=kind,
                    removed_in_phase=phase,
                )
            )
        else:  # _TYPE_DEPRECATION
            dep = raw.get("deprecated_in_phase")
            rem = raw.get("remove_in_phase")
            if not isinstance(dep, int) or not isinstance(rem, int) or dep < 0 or rem <= dep:
                continue
            result.append(
                _TrackedSymbol(
                    symbol=symbol,
                    kind=kind,
                    deprecated_in_phase=dep,
                    remove_in_phase=rem,
                )
            )

    return result


# =====================================================================
# AST-утилиты
# =====================================================================


def _dotted_name(node: ast.AST) -> str | None:
    """Восстанавливает точечное имя из цепочки ``Attribute``/``Name``.

    Пример: для узла ``dds_core.application.text_normalizer``
    возвращает строку ``"dds_core.application.text_normalizer"``.
    Для узлов с нетривиальной базой (вызов функции, subscript,
    литерал) возвращает ``None``.

    Args:
        node: AST-узел.

    Returns:
        Точечное имя или ``None``.
    """
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    parts.reverse()
    return ".".join(parts)


def _collect_aliases(tree: ast.AST) -> dict[str, str]:
    """Собирает карту «локальное имя → полный путь» из импортов.

    Используется для разрешения обращений по атрибутам, когда
    модуль импортирован через ``from X import Y`` или с алиасом.

    Правила:

    +----------------------------------+--------------------------------+
    | Импорт                           | Запись в карте                 |
    +==================================+================================+
    | ``import X``                     | ``X → X``                      |
    +----------------------------------+--------------------------------+
    | ``import X.Y.Z``                 | ``X → X``                      |
    |                                  | (в коде ``X.Y.Z`` — через       |
    |                                  | атрибуты; локальное имя X)     |
    +----------------------------------+--------------------------------+
    | ``import X.Y.Z as A``            | ``A → X.Y.Z``                  |
    +----------------------------------+--------------------------------+
    | ``from X import Y``              | ``Y → X.Y``                    |
    +----------------------------------+--------------------------------+
    | ``from X import Y as A``         | ``A → X.Y``                    |
    +----------------------------------+--------------------------------+
    | ``from X import *``              | (пропуск)                      |
    +----------------------------------+--------------------------------+

    Относительные импорты (``level > 0``) пропускаются: они не
    разрешают внешние пути.

    Args:
        tree: AST-дерево файла.

    Returns:
        Словарь соответствия локальных имён полным путям.
    """
    aliases: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    head = alias.name.split(".", 1)[0]
                    aliases[head] = head

        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                continue
            module = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                full = f"{module}.{alias.name}" if module else alias.name
                aliases[local] = full

    return aliases


def _import_matches_symbol(imported: str, symbol: str) -> bool:
    """Проверяет соответствие импортированного пути отслеживаемому символу.

    Соответствие выполняется по одному из правил:

    +---------------------------------------------+---------------------+
    | Условие                                     | Пример              |
    +=============================================+=====================+
    | ``imported == symbol``                      | ``X.Y == X.Y``      |
    +---------------------------------------------+---------------------+
    | ``imported`` начинается с ``symbol + "."``  | ``X.Y.Z`` для       |
    |                                             | ``symbol=X.Y``      |
    +---------------------------------------------+---------------------+

    Второе правило ловит импорт подмодуля из удалённого пакета:
    ``import X.Y.Z`` при удалённом ``X.Y``.

    Args:
        imported: Импортированный полный путь.
        symbol: Отслеживаемый символ.

    Returns:
        ``True`` при соответствии.
    """
    if imported == symbol:
        return True
    return imported.startswith(symbol + ".")


def _render_import_node(node: ast.AST) -> str:
    """Формирует краткое строковое представление импорта для отчёта.

    Используется только для диагностики; не претендует на
    полное восстановление исходного текста (например,
    многострочные импорты со скобками будут отображены
    в одну строку).

    Args:
        node: узел ``ast.Import`` или ``ast.ImportFrom``.

    Returns:
        Строковое представление, например
        ``from X.Y import Z as A``.
    """
    if isinstance(node, ast.Import):
        parts = []
        for alias in node.names:
            if alias.asname:
                parts.append(f"{alias.name} as {alias.asname}")
            else:
                parts.append(alias.name)
        return "import " + ", ".join(parts)

    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        prefix = "." * (node.level or 0)
        parts = []
        for alias in node.names:
            if alias.asname:
                parts.append(f"{alias.name} as {alias.asname}")
            else:
                parts.append(alias.name)
        return f"from {prefix}{module} import " + ", ".join(parts)

    return "<unknown import>"


# =====================================================================
# Поиск использований
# =====================================================================


def find_usages_in_file(
    path: Path,
    symbols: list[_TrackedSymbol],
    current_phase: int,
) -> tuple[list[_Finding], str | None]:
    """AST-парсит файл и возвращает список находок.

    Операции:

    +----+----------------------------------------------------+
    | №  | Описание                                           |
    +====+====================================================+
    | 1  | Чтение файла в UTF-8.                              |
    +----+----------------------------------------------------+
    | 2  | Разбор через :func:`ast.parse`.                    |
    +----+----------------------------------------------------+
    | 3  | Построение карты алиасов импортов.                 |
    +----+----------------------------------------------------+
    | 4  | Проход по узлам ``Import`` и ``ImportFrom``:       |
    |    | для каждого импортированного пути — проверка       |
    |    | соответствия отслеживаемым символам.               |
    +----+----------------------------------------------------+
    | 5  | Проход по узлам ``Attribute``:                     |
    |    | a. Отбор только «внешних» атрибутов (родитель —     |
    |    |    не ``Attribute`` или он не обращается к         |
    |    |    текущему через ``.value``).                     |
    |    | b. Восстановление точечного имени.                 |
    |    | c. Разрешение через карту алиасов.                 |
    |    | d. Сравнение с отслеживаемыми символами.           |
    +----+----------------------------------------------------+
    | 6  | Дедупликация по ``(line, symbol, status)``.        |
    +----+----------------------------------------------------+
    | 7  | Возврат списка находок и сообщения об ошибке       |
    |    | (``None`` при успешном разборе).                   |
    +----+----------------------------------------------------+

    Args:
        path: Путь к файлу.
        symbols: Список отслеживаемых символов.
        current_phase: Номер текущей фазы (для классификации).

    Returns:
        Кортеж ``(findings, error_message)``. ``error_message``
        заполняется только при ошибке чтения или парсинга файла.
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

    aliases = _collect_aliases(tree)
    findings: list[_Finding] = []
    # Дедупликация по (line, symbol, status): одна строка может
    # содержать импорт и обращение к тому же символу (редкий
    # случай) — это одно логическое использование.
    seen: set[tuple[int, str, str]] = set()

    # --- Проход 1: импорты ---------------------------------------------
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for sym in symbols:
                    status = sym.classify(current_phase)
                    if status is None:
                        continue
                    if _import_matches_symbol(alias.name, sym.symbol):
                        key = (node.lineno, sym.symbol, status)
                        if key in seen:
                            continue
                        seen.add(key)
                        findings.append(
                            _Finding(
                                path=path,
                                line=node.lineno,
                                symbol=sym.symbol,
                                status=status,
                                phase_description=sym.phase_description(),
                                usage=_render_import_node(node),
                            )
                        )

        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                continue
            module = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    # Star-import: проверяем сам модуль.
                    for sym in symbols:
                        status = sym.classify(current_phase)
                        if status is None:
                            continue
                        if _import_matches_symbol(module, sym.symbol):
                            key = (node.lineno, sym.symbol, status)
                            if key in seen:
                                continue
                            seen.add(key)
                            findings.append(
                                _Finding(
                                    path=path,
                                    line=node.lineno,
                                    symbol=sym.symbol,
                                    status=status,
                                    phase_description=sym.phase_description(),
                                    usage=_render_import_node(node),
                                )
                            )
                    continue

                full = f"{module}.{alias.name}" if module else alias.name
                for sym in symbols:
                    status = sym.classify(current_phase)
                    if status is None:
                        continue
                    if full == sym.symbol:
                        key = (node.lineno, sym.symbol, status)
                        if key in seen:
                            continue
                        seen.add(key)
                        findings.append(
                            _Finding(
                                path=path,
                                line=node.lineno,
                                symbol=sym.symbol,
                                status=status,
                                phase_description=sym.phase_description(),
                                usage=_render_import_node(node),
                            )
                        )

    # --- Проход 2: обращения по атрибутам ------------------------------
    # Карта «id(node) → родитель» для отбора только внешних
    # атрибутов (у которых родитель — не Attribute через .value).
    parent_map: dict[int, ast.AST] = {}
    for parent_node in ast.walk(tree):
        for child in ast.iter_child_nodes(parent_node):
            parent_map[id(child)] = parent_node

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        parent = parent_map.get(id(node))
        if isinstance(parent, ast.Attribute) and parent.value is node:
            # Не внешний атрибут: часть более длинной цепочки.
            continue

        dotted = _dotted_name(node)
        if dotted is None or "." not in dotted:
            continue

        # Разрешение через карту алиасов.
        head, _, tail = dotted.partition(".")
        resolved = f"{aliases[head]}.{tail}" if head in aliases else dotted

        for sym in symbols:
            status = sym.classify(current_phase)
            if status is None:
                continue
            if resolved == sym.symbol:
                key = (node.lineno, sym.symbol, status)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    _Finding(
                        path=path,
                        line=node.lineno,
                        symbol=sym.symbol,
                        status=status,
                        phase_description=sym.phase_description(),
                        usage=resolved,
                    )
                )

    findings.sort(key=lambda f: f.sort_key())
    return findings, None


# =====================================================================
# Обход модулей
# =====================================================================


def iter_module_files(root: Path) -> Iterator[Path]:
    """Итерирует ``.py`` файлы под ``root`` в детерминированном порядке.

    Исключаются служебные каталоги из :data:`_SKIPPED_DIR_PARTS`
    (``__pycache__``, ``.git`` и т.п.). Файлы сортируются по
    пути — для воспроизводимости вывода.

    Args:
        root: Корневой каталог модулей.

    Yields:
        Пути к ``.py`` файлам.
    """
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIPPED_DIR_PARTS for part in path.parts):
            continue
        yield path


# =====================================================================
# Оркестрация
# =====================================================================


def scan_modules(
    root: Path,
    symbols: list[_TrackedSymbol],
    current_phase: int,
) -> tuple[list[_Finding], int]:
    """Сканирует все модули и возвращает агрегированные находки.

    Ошибки чтения или парсинга отдельных файлов превращаются
    в находки со статусом ERROR и специальным символом
    (``<read error>`` / ``<syntax error>``); обход продолжается.

    Args:
        root: Корневой каталог модулей.
        symbols: Список отслеживаемых символов.
        current_phase: Номер текущей фазы.

    Returns:
        Кортеж ``(findings, files_scanned)``.
    """
    findings: list[_Finding] = []
    files_scanned = 0

    for path in iter_module_files(root):
        files_scanned += 1
        file_findings, error = find_usages_in_file(
            path=path,
            symbols=symbols,
            current_phase=current_phase,
        )
        if error is not None:
            findings.append(
                _Finding(
                    path=path,
                    line=0,
                    symbol="<parse error>",
                    status=_STATUS_ERROR,
                    phase_description="cannot parse module",
                    usage=error,
                )
            )
            continue
        findings.extend(file_findings)

    findings.sort(key=lambda f: f.sort_key())
    return findings, files_scanned


# =====================================================================
# Отчёт
# =====================================================================


def print_report(
    current_phase: int,
    symbols: list[_TrackedSymbol],
    findings: list[_Finding],
    files_scanned: int,
    modules_root: Path,
) -> None:
    """Печатает отчёт в stdout/stderr.

    Сводка и список находок — в stdout; итоговое сообщение о
    статусе — в stderr. Формат находки: три строки (заголовок
    с путём и строкой, символ с описанием фаз, краткое
    использование).

    Args:
        current_phase: Номер текущей фазы.
        symbols: Отслеживаемые символы (для сводки).
        findings: Список находок.
        files_scanned: Количество просканированных файлов.
        modules_root: Корневой каталог модулей (для отображения путей).
    """
    errors = [f for f in findings if f.status == _STATUS_ERROR]
    warnings = [f for f in findings if f.status == _STATUS_WARNING]

    print(f"Current phase: {current_phase}")
    print(f"Tracked symbols: {len(symbols)}")
    print(f"Module files scanned: {files_scanned} ({modules_root})")
    print(f"Findings: {len(findings)} (errors: {len(errors)}, warnings: {len(warnings)})")
    print()

    for finding in findings:
        print(f"{finding.status}: {finding.path}:{finding.line}")
        print(f"  symbol: {finding.symbol}")
        print(f"  status: {finding.phase_description}")
        print(f"  usage:  {finding.usage}")
        print()

    if errors:
        print(
            "FAILED: обнаружены использования удалённых символов.",
            file=sys.stderr,
        )
    else:
        print("OK: совместимость модулей в порядке.", file=sys.stderr)


# =====================================================================
# CLI
# =====================================================================


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Парсинг аргументов (``--modules``, ``--deprecations``,|
    |   | ``--current-phase``).                               |
    +---+-----------------------------------------------------+
    | 2 | Загрузка ``current_phase.txt``.                     |
    +---+-----------------------------------------------------+
    | 3 | Загрузка ``DEPRECATIONS.yaml``.                     |
    +---+-----------------------------------------------------+
    | 4 | Разбор отслеживаемых символов.                      |
    +---+-----------------------------------------------------+
    | 5 | Сканирование ``dds_modules/``.                      |
    +---+-----------------------------------------------------+
    | 6 | Печать отчёта; возврат 0/1.                         |
    +---+-----------------------------------------------------+

    Args:
        argv: Аргументы командной строки без имени программы.

    Returns:
        Код возврата: 0 — успех, 1 — обнаружены ERROR или ошибки
        конфигурации.
    """
    parser = argparse.ArgumentParser(
        description=("Проверка совместимости внешних модулей dds_modules/ с DEPRECATIONS.yaml."),
    )
    parser.add_argument(
        "--modules",
        type=Path,
        default=_DEFAULT_MODULES_DIR,
        help=(f"Каталог внешних модулей (по умолчанию: {_DEFAULT_MODULES_DIR})."),
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

    # 1. Текущая фаза.
    try:
        current_phase = load_current_phase(args.current_phase)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # 2. DEPRECATIONS.yaml.
    try:
        data = load_deprecations(args.deprecations)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # 3. Разбор символов.
    symbols = collect_tracked_symbols(data)

    # 4. Сканирование модулей.
    if not args.modules.is_dir():
        print(f"INFO: каталог модулей отсутствует: {args.modules}. Проверка пропущена.")
        return 0

    findings, files_scanned = scan_modules(
        root=args.modules,
        symbols=symbols,
        current_phase=current_phase,
    )

    # 5. Отчёт.
    print_report(
        current_phase=current_phase,
        symbols=symbols,
        findings=findings,
        files_scanned=files_scanned,
        modules_root=args.modules,
    )

    has_errors = any(f.status == _STATUS_ERROR for f in findings)
    return 1 if has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
