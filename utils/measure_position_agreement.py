"""
Скрипт измерения согласованности PyMuPDF API ``get_text("text")``
и ``get_text("words")``.

Назначение (Фаза 0 плана рефакторинга подсветки):

Скрипт проверяет фундаментальное допущение offsets-подхода:
порядок слов, возвращаемых ``page.get_text("words")``, совпадает
с порядком слов в ``page.get_text("text")``. Если допущение
выполняется для страницы, то char-позиции, полученные из
FTS5-функции ``offsets()``, можно корректно спроецировать на
bbox'ы из ``WordIndex``. Если не выполняется — offsets-подход
для этой страницы неприменим, и подсветка должна использовать
fuzzy-фолбэк.

Алгоритм:

1. Загрузка списка PDF (из каталога или из ``manifest.json``).
2. Для каждого PDF: открытие через ``pymupdf``, обход страниц.
3. Для каждой страницы:

   a. ``text_content = page.get_text("text")``
   b. ``words = page.get_text("words")``
   c. Прогон ``locate_words_in_text(words, text_content)``:
      последовательный поиск каждого слова в тексте с
      word-boundary check.
   d. Запись строки в CSV: пути, счётчики, флаг ``reliable``,
      позиция первого сбоя (если был).

4. Вывод агрегированных метрик в stdout: доля ``reliable=True``
   страниц, распределение по категориям, средний процент
   найденных слов.

Ограничения:

- Скрипт автономен: зависит только от ``pymupdf``, ``argparse``,
  ``csv``, ``json``, стандартной библиотеки. Не импортирует
  модули DDS — это осознанное решение, чтобы скрипт можно было
  запускать на dev-машине без полного окружения.
- Word-boundary check — упрощённый (``str.isalnum()``). На
  практике этого достаточно для технических документов; сложные
  случаи (точки в аббревиатурах, дефисы в кодах) могут давать
  ложные срабатывания при ``reliable=True``. Для целей Фазы 0
  это допустимо.
- Скрипт не предназначен для production: это исследовательский
  инструмент для сбора метрик. Логирование — через ``print``.
- Результат — CSV-файл с одной строкой на страницу и агрегаты
  в stdout. Формат CSV стабилен и пригоден для последующего
  анализа (``pandas``, ``awk``, ручной осмотр).

Формат CSV:

    doc_path,doc_id,page_number,total_words,matched_words,
    reliable,first_failure_pos,first_failure_word,category,error

- ``doc_path`` — абсолютный путь к PDF.
- ``doc_id`` — имя файла без расширения (для сопоставления с БД).
- ``page_number`` — 0-based.
- ``total_words`` — сколько слов вернул ``get_text("words")``
  (после ``.strip()``).
- ``matched_words`` — сколько слов найдено в ``text_content``
  последовательным поиском.
- ``reliable`` — ``True``, если все слова найдены; ``False`` иначе.
- ``first_failure_pos`` — индекс первого несогласованного слова
  (0-based) или пусто.
- ``first_failure_word`` — текст первого несогласованного слова
  или пусто.
- ``category`` — категория документа из ``manifest.json`` или
  ``"unknown"``.
- ``error`` — пустая строка для успешно обработанных страниц;
  для страниц с ошибкой — краткое описание.

Пример использования::

    # Обработка каталога с PDF (без разметки категорий):
    python scripts/measure_position_agreement.py \\
        --input /mnt/rd_documents \\
        --output /tmp/position_agreement.csv \\
        --limit 500

    # Обработка по manifest.json с категориями:
    python scripts/measure_position_agreement.py \\
        --input tests/fixtures/pdf_corpus/manifest.json \\
        --output /tmp/position_agreement.csv

После завершения скрипт печатает сводку по метрикам:
- сколько PDF и страниц обработано;
- доля ``reliable=True`` страниц;
- распределение по категориям (если заданы);
- топ-5 причин сбоев (по ``first_failure_word``).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf  # type: ignore[no-redef]
    except ImportError:
        print(
            "Ошибка: требуется библиотека pymupdf. Установите: pip install pymupdf",
            file=sys.stderr,
        )
        sys.exit(1)


# ----------------------------------------------------------------------
# Модель результата одной страницы
# ----------------------------------------------------------------------


@dataclass
class PageAgreement:
    """Результат анализа одной страницы PDF.

    Attributes:
        doc_path: Абсолютный путь к PDF.
        doc_id: Имя файла без расширения.
        page_number: Номер страницы (0-based).
        total_words: Количество непустых слов из ``get_text("words")``.
        matched_words: Количество слов, найденных в ``text_content``.
        reliable: ``True``, если все слова найдены в правильном порядке.
        first_failure_pos: Индекс первого несогласованного слова
            (0-based) или ``None``.
        first_failure_word: Текст первого несогласованного слова
            или пустая строка.
        category: Категория документа (из manifest.json) или ``"unknown"``.
        error: Описание ошибки при обработке страницы; пустая строка
            при успешной обработке.
    """

    doc_path: str
    doc_id: str
    page_number: int
    total_words: int = 0
    matched_words: int = 0
    reliable: bool = False
    first_failure_pos: int | None = None
    first_failure_word: str = ""
    category: str = "unknown"
    error: str = ""


# ----------------------------------------------------------------------
# Алгоритм последовательного поиска слов
# ----------------------------------------------------------------------


def _find_word_at_or_after(
    text: str,
    word: str,
    cursor: int,
) -> int:
    """Ищет слово в тексте, начиная с позиции ``cursor``.

    Word-boundary check: символ непосредственно перед словом и
    символ непосредственно после слова должны быть не-буквенно-цифровыми
    (или границы строки). Это предотвращает ложные совпадения вида
    ``"the"`` в ``"there"``.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | ``text.find(word, cursor)`` — первое вхождение      |
    |   | слова от ``cursor``.                                |
    +---+-----------------------------------------------------+
    | 2 | Если вхождение не найдено → возврат ``-1``.         |
    +---+-----------------------------------------------------+
    | 3 | Проверка границы слева: символ ``text[idx-1]``      |
    |   | не является буквенно-цифровым (или ``idx == 0``).   |
    +---+-----------------------------------------------------+
    | 4 | Проверка границы справа: символ                     |
    |   | ``text[idx+len(word)]`` не является                 |
    |   | буквенно-цифровым (или ``idx + len == len(text)``). |
    +---+-----------------------------------------------------+
    | 5 | Если обе границы валидны → возврат ``idx``.         |
    +---+-----------------------------------------------------+
    | 6 | Иначе — поиск следующего вхождения со смещением     |
    |   | ``idx + 1``; повтор до исчерпания вхождений.         |
    +---+-----------------------------------------------------+

    Args:
        text: Текст страницы (``get_text("text")``).
        word: Искомое слово (непустое, после ``.strip()``).
        cursor: Позиция, с которой начинать поиск (монотонно растёт
            по мере обработки слов страницы).

    Returns:
        Позиция начала найденного слова или ``-1``, если слово
        не найдено при заданных границах.
    """
    idx = text.find(word, cursor)
    while idx != -1:
        before_ok = idx == 0 or not text[idx - 1].isalnum()
        after_idx = idx + len(word)
        after_ok = after_idx >= len(text) or not text[after_idx].isalnum()
        if before_ok and after_ok:
            return idx
        idx = text.find(word, idx + 1)
    return -1


def locate_words_in_text(
    words: list[tuple],
    text_content: str,
) -> tuple[int, int | None, str]:
    """Проверяет согласованность слов с текстом страницы.

    Проходит по словам из ``get_text("words")`` и последовательно
    ищет каждое из них в ``text_content``. Курсор поиска монотонно
    растёт: каждое следующее слово ищется с позиции конца
    предыдущего. Это гарантирует проверку порядка.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Инициализация ``cursor = 0``, ``matched = 0``.      |
    +---+-----------------------------------------------------+
    | 2 | Для каждого элемента из ``words`` (формат PyMuPDF:  |
    |   | ``(x0, y0, x1, y1, text, block, line, word)``):     |
    |   | a. ``text_stripped = text.strip()``.                |
    |   | b. Пропуск пустых токенов.                          |
    |   | c. ``idx = _find_word_at_or_after(...)``.           |
    |   | d. Если ``idx == -1`` → возврат неудачи.            |
    |   | e. ``cursor = idx + len(text_stripped)``.           |
    |   | f. ``matched += 1``.                                |
    +---+-----------------------------------------------------+
    | 3 | Возврат ``(matched, None, "")``.                    |
    +---+-----------------------------------------------------+

    Args:
        words: Список кортежей из ``page.get_text("words")``.
        text_content: Текст страницы из ``page.get_text("text")``.

    Returns:
        Кортеж ``(matched_words, first_failure_pos, first_failure_word)``:

        - ``matched_words`` — количество успешно найденных слов.
        - ``first_failure_pos`` — индекс первого несогласованного
          слова в исходном списке ``words`` (с учётом пропуска
          пустых токенов) или ``None``, если все слова найдены.
        - ``first_failure_word`` — текст первого несогласованного
          слова или пустая строка.
    """
    cursor = 0
    matched = 0
    normalized_index = 0

    for item in words:
        if len(item) < 5:
            # Некорректный формат — пропускаем (не должно быть).
            continue
        text_stripped = str(item[4]).strip()
        if not text_stripped:
            continue

        idx = _find_word_at_or_after(text_content, text_stripped, cursor)
        if idx == -1:
            return matched, normalized_index, text_stripped

        cursor = idx + len(text_stripped)
        matched += 1
        normalized_index += 1

    return matched, None, ""


# ----------------------------------------------------------------------
# Загрузка входных данных
# ----------------------------------------------------------------------


def _load_manifest(manifest_path: Path) -> dict[str, str]:
    """Загружает карту ``абсолютный путь → категория`` из manifest.json.

    Ожидаемый формат::

        {
            "files": [
                {"path": "abs/or/rel/path.pdf", "category": "drawing"},
                {"path": "abs/or/rel/path2.pdf", "category": "spec"}
            ]
        }

    Относительные пути резолвятся относительно каталога, в котором
    находится manifest.json.

    Args:
        manifest_path: Путь к файлу manifest.json.

    Returns:
        Словарь ``{абсолютный_путь: категория}``. Пустой словарь,
        если файл не содержит ключа ``"files"``.
    """
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    base_dir = manifest_path.parent
    result: dict[str, str] = {}
    for entry in data.get("files", []):
        raw_path = entry.get("path", "")
        if not raw_path:
            continue
        p = Path(raw_path)
        if not p.is_absolute():
            p = (base_dir / p).resolve()
        result[str(p)] = str(entry.get("category", "unknown"))
    return result


def _discover_pdfs(
    input_path: Path,
    manifest_map: dict[str, str],
) -> list[tuple[Path, str]]:
    """Возвращает список ``(pdf_path, category)`` для обработки.

    Логика:

    - Если ``input_path`` — файл ``.json``: пути берутся из
      ``manifest_map`` (все ключи), категории — из манифеста.
    - Если ``input_path`` — каталог: рекурсивно обходит и собирает
      все ``.pdf``. Категория для каждого — из ``manifest_map``,
      если путь совпал; иначе ``"unknown"``.
    - Если ``input_path`` — файл ``.pdf``: единственный PDF.

    Args:
        input_path: Каталог, manifest.json или одиночный PDF.
        manifest_map: Карта из :func:`_load_manifest`.

    Returns:
        Список пар ``(путь, категория)``, отсортированный по пути.
    """
    if input_path.is_file() and input_path.suffix.lower() == ".pdf":
        category = manifest_map.get(str(input_path.resolve()), "unknown")
        return [(input_path, category)]

    if input_path.is_file() and input_path.suffix.lower() == ".json":
        items: list[tuple[Path, str]] = []
        for path_str, category in manifest_map.items():
            items.append((Path(path_str), category))
        items.sort(key=lambda x: str(x[0]))
        return items

    if input_path.is_dir():
        items = []
        for root, _dirs, files in os.walk(input_path):
            for name in sorted(files):
                if not name.lower().endswith(".pdf"):
                    continue
                p = Path(root) / name
                category = manifest_map.get(str(p.resolve()), "unknown")
                items.append((p, category))
        items.sort(key=lambda x: str(x[0]))
        return items

    return []


# ----------------------------------------------------------------------
# Обработка одного PDF
# ----------------------------------------------------------------------


def _process_pdf(
    pdf_path: Path,
    category: str,
) -> list[PageAgreement]:
    """Обрабатывает один PDF и возвращает список результатов по страницам.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Открытие PDF через ``pymupdf.open``.                |
    +---+-----------------------------------------------------+
    | 2 | Для каждой страницы:                                |
    |   | a. ``text_content = page.get_text("text")``.        |
    |   | b. ``words = page.get_text("words")``.              |
    |   | c. Если ``words`` пуст: результат с ``reliable=True``|
    |   |    и нулевыми счётчиками (нечего сверять).          |
    |   | d. Иначе: ``locate_words_in_text``.                 |
    +---+-----------------------------------------------------+
    | 3 | Возврат списка ``PageAgreement``.                   |
    +---+-----------------------------------------------------+

    Ошибки на уровне отдельной страницы или всего PDF перехватываются
    и записываются в поле ``error`` соответствующей страницы.
    Если PDF не открывается, создаётся одна запись с
    ``page_number=-1`` и описанием ошибки.

    Args:
        pdf_path: Путь к PDF.
        category: Категория документа.

    Returns:
        Список результатов по страницам (или одна запись с ошибкой).
    """
    doc_id = pdf_path.stem
    doc_path_str = str(pdf_path.resolve())
    results: list[PageAgreement] = []

    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as e:  # noqa: BLE001
        results.append(
            PageAgreement(
                doc_path=doc_path_str,
                doc_id=doc_id,
                page_number=-1,
                category=category,
                error=f"open_error: {type(e).__name__}: {e}",
            )
        )
        return results

    try:
        page_count = len(doc)
        for page_num in range(page_count):
            try:
                page = doc.load_page(page_num)
                text_content = page.get_text("text") or ""
                words = page.get_text("words") or []

                non_empty_words = [w for w in words if str(w[4]).strip() if len(w) >= 5]

                if not non_empty_words:
                    results.append(
                        PageAgreement(
                            doc_path=doc_path_str,
                            doc_id=doc_id,
                            page_number=page_num,
                            total_words=0,
                            matched_words=0,
                            reliable=True,
                            first_failure_pos=None,
                            first_failure_word="",
                            category=category,
                            error="",
                        )
                    )
                    continue

                matched, failure_pos, failure_word = locate_words_in_text(words, text_content)
                reliable = failure_pos is None

                results.append(
                    PageAgreement(
                        doc_path=doc_path_str,
                        doc_id=doc_id,
                        page_number=page_num,
                        total_words=len(non_empty_words),
                        matched_words=matched,
                        reliable=reliable,
                        first_failure_pos=failure_pos,
                        first_failure_word=failure_word,
                        category=category,
                        error="",
                    )
                )
            except Exception as e:  # noqa: BLE001
                results.append(
                    PageAgreement(
                        doc_path=doc_path_str,
                        doc_id=doc_id,
                        page_number=page_num,
                        category=category,
                        error=(f"page_error: {type(e).__name__}: {e}"),
                    )
                )
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001, S110
            pass

    return results


# ----------------------------------------------------------------------
# Запись CSV
# ----------------------------------------------------------------------


_CSV_FIELDS = [
    "doc_path",
    "doc_id",
    "page_number",
    "total_words",
    "matched_words",
    "reliable",
    "first_failure_pos",
    "first_failure_word",
    "category",
    "error",
]


def _write_csv(results: list[PageAgreement], output_path: Path) -> None:
    """Записывает результаты в CSV.

    Формат: см. module docstring. Используется ``csv.writer``
    с экранированием по умолчанию (RFC 4180).

    Args:
        results: Список результатов.
        output_path: Путь к выходному CSV.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for r in results:
            writer.writerow(
                {
                    "doc_path": r.doc_path,
                    "doc_id": r.doc_id,
                    "page_number": r.page_number,
                    "total_words": r.total_words,
                    "matched_words": r.matched_words,
                    "reliable": "1" if r.reliable else "0",
                    "first_failure_pos": (
                        "" if r.first_failure_pos is None else r.first_failure_pos
                    ),
                    "first_failure_word": r.first_failure_word,
                    "category": r.category,
                    "error": r.error,
                }
            )


# ----------------------------------------------------------------------
# Сводка
# ----------------------------------------------------------------------


def _print_summary(results: list[PageAgreement]) -> None:
    """Печатает агрегированные метрики в stdout.

    Метрики:

    - Всего PDF (уникальных ``doc_path``).
    - Всего страниц (исключая записи с ``page_number == -1``).
    - Страниц с ``reliable=True`` и ``reliable=False``, доли.
    - Распределение по категориям: доля reliable по каждой.
    - Топ-5 самых частых ``first_failure_word``.

    Args:
        results: Список результатов по страницам.
    """
    total_records = len(results)
    pdfs = {r.doc_path for r in results}
    valid_pages = [r for r in results if r.page_number >= 0 and not r.error]
    errored_pages = [r for r in results if r.error]

    reliable = [r for r in valid_pages if r.reliable]
    unreliable = [r for r in valid_pages if not r.reliable]

    print()
    print("=" * 72)
    print("Сводка measure_position_agreement")
    print("=" * 72)
    print(f"Всего записей в CSV:        {total_records}")
    print(f"Уникальных PDF:             {len(pdfs)}")
    print(f"Успешно обработанных стр.:  {len(valid_pages)}")
    print(f"Страниц с ошибкой:          {len(errored_pages)}")

    if valid_pages:
        ratio_reliable = len(reliable) / len(valid_pages) * 100
        print()
        print(f"reliable=True:              {len(reliable)} ({ratio_reliable:.1f}%)")
        print(f"reliable=False:             {len(unreliable)} ({100 - ratio_reliable:.1f}%)")

        if reliable:
            matched_ratio = (
                sum((r.matched_words / r.total_words) if r.total_words else 1.0 for r in reliable)
                / len(reliable)
                * 100
            )
            print(f"Средний % найденных слов (на reliable-страницах):  {matched_ratio:.1f}%")

    # Распределение по категориям.
    categories: dict[str, list[PageAgreement]] = {}
    for r in valid_pages:
        categories.setdefault(r.category, []).append(r)

    if categories:
        print()
        print("По категориям:")
        for cat, items in sorted(categories.items()):
            rel = sum(1 for r in items if r.reliable)
            total = len(items)
            pct = (rel / total * 100) if total else 0.0
            print(f"  {cat:20s} {rel}/{total} reliable ({pct:.1f}%)")

    # Топ причин сбоев.
    if unreliable:
        failure_counter: dict[str, int] = {}
        for r in unreliable:
            key = r.first_failure_word or "(empty)"
            failure_counter[key] = failure_counter.get(key, 0) + 1
        top_failures = sorted(
            failure_counter.items(),
            key=lambda kv: (-kv[1], kv[0]),
        )[:5]
        print()
        print("Топ-5 первых несогласованных слов:")
        for word, count in top_failures:
            display = word if len(word) <= 40 else word[:37] + "..."
            print(f"  {display!r}: {count} раз")

    print("=" * 72)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    """Создаёт парсер аргументов командной строки."""
    parser = argparse.ArgumentParser(
        description=(
            "Измерение согласованности PyMuPDF get_text('text') и "
            "get_text('words'). Используется в Фазе 0 плана "
            "рефакторинга подсветки."
        ),
    )
    parser.add_argument(
        "--input",
        required=True,
        help=("Каталог с PDF, одиночный PDF или manifest.json (с ключом 'files' и категориями)."),
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Путь к выходному CSV.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help=("Ограничить количество обрабатываемых PDF (0 — без ограничения)."),
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=25,
        help="Печатать прогресс каждые N PDF (по умолчанию 25).",
    )
    return parser


def main() -> None:
    """Точка входа скрипта.

    Операции:

    +----+----------------------------------------------------+
    | №  | Описание                                           |
    +====+====================================================+
    | 1  | Парсинг аргументов командной строки.               |
    +----+----------------------------------------------------+
    | 2  | Определение типа входа: manifest.json, каталог     |
    |    | или одиночный PDF.                                 |
    +----+----------------------------------------------------+
    | 3  | Формирование списка PDF с категориями.             |
    +----+----------------------------------------------------+
    | 4  | Обработка каждого PDF, сбор результатов.           |
    +----+----------------------------------------------------+
    | 5  | Запись CSV.                                        |
    +----+----------------------------------------------------+
    | 6  | Печать сводки.                                     |
    +----+----------------------------------------------------+
    | 7  | Обработка ``KeyboardInterrupt`` (сохранение         |
    |    | промежуточных результатов).                         |
    +----+----------------------------------------------------+
    """
    parser = _build_arg_parser()
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        print(f"Ошибка: путь не существует: {input_path}", file=sys.stderr)
        sys.exit(1)

    # Загрузка manifest.
    manifest_map: dict[str, str] = {}
    if input_path.is_file() and input_path.suffix.lower() == ".json":
        try:
            manifest_map = _load_manifest(input_path)
        except Exception as e:  # noqa: BLE001
            print(
                f"Ошибка загрузки manifest.json: {e}",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Загружено записей из manifest: {len(manifest_map)}")
    elif input_path.is_dir():
        manifest_candidate = input_path / "manifest.json"
        if manifest_candidate.is_file():
            try:
                manifest_map = _load_manifest(manifest_candidate)
                print(f"Найден manifest.json, загружено записей: {len(manifest_map)}")
            except Exception as e:  # noqa: BLE001
                print(
                    f"Предупреждение: не удалось загрузить manifest.json: {e}",
                    file=sys.stderr,
                )

    items = _discover_pdfs(input_path, manifest_map)
    if not items:
        print("Не найдено PDF для обработки.", file=sys.stderr)
        sys.exit(1)

    if args.limit > 0:
        items = items[: args.limit]

    print(f"К обработке: {len(items)} PDF")

    results: list[PageAgreement] = []
    try:
        for idx, (pdf_path, category) in enumerate(items, start=1):
            page_results = _process_pdf(pdf_path, category)
            results.extend(page_results)

            if args.progress_every > 0 and idx % args.progress_every == 0:
                total_pages = sum(1 for r in results if r.page_number >= 0)
                print(f"  Обработано {idx}/{len(items)} PDF, всего страниц: {total_pages}")
    except KeyboardInterrupt:
        print(
            "\nПрервано пользователем. Сохраняю промежуточные результаты...",
            file=sys.stderr,
        )

    _write_csv(results, output_path)
    print(f"Результаты записаны: {output_path.resolve()}")

    _print_summary(results)


if __name__ == "__main__":
    main()
