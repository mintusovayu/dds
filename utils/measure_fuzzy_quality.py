"""
Скрипт оценки качества fuzzy-фолбэка для подсветки совпадений.

Назначение (Фаза 0 плана рефакторинга подсветки):

Скрипт измеряет precision/recall fuzzy-поиска на наборе пар
``(запрос, страница PDF)``. Используется для калибровки трёх
параметров fuzzy из ``dds_core.domain.config``:

- ``FUZZY_RATIO_THRESHOLD`` — порог схожести
  (``difflib.SequenceMatcher.ratio``).
- ``FUZZY_LENGTH_DELTA`` — допустимое расхождение по длине
  слова.
- ``FUZZY_MIN_LENGTH`` — минимальная длина слова, для которого
  fuzzy применяется (защита от ложных срабатываний на коротких
  словах).

Задача скрипта — дать ответ на вопрос: при каких значениях этих
параметров fuzzy-фолбэк даёт наибольшее F1-значение на реальном
каталоге пользователя.

Режимы работы:

+----------------------------------+----------------------------------+
| Режим                            | Назначение                       |
+==================================+==================================+
| ``--input manifest.json``        | Явный список пар                 |
|                                  | ``(query, doc_path, page_number)``.|
|                                  | Используется для оценки на       |
|                                  | проблемных запросах, заданных    |
|                                  | вручную.                          |
+----------------------------------+----------------------------------+
| ``--pdf doc.pdf --auto-generate N``| Автоматическая генерация       |
|                                  | кейсов: N случайных слов из      |
|                                  | каждой страницы PDF.             |
|                                  | Используется для оценки на       |
|                                  | «обычных» словах (base          |
|                                  | quality fuzzy).                  |
+----------------------------------+----------------------------------+

Эталон (ground truth):

- В явном режиме: для каждой пары ``(query, page)`` скрипт
  пытается вычислить эталонные bbox'ы через
  ``page.search_for(query)``. Если в manifest заданы явные
  ``expected_bboxes`` — используются они. Если ни то, ни другое
  не даёт результата — кейс помечается ``etl_missing`` и
  исключается из расчёта precision/recall.
- В авто-режиме: ``query = слово из get_text("words")``,
  ``expected = page.search_for(word)``.

Сетка параметров:

По умолчанию тестируется набор комбинаций:

- ``ratio_threshold`` ∈ {0.75, 0.80, 0.85, 0.90, 0.95}
- ``length_delta`` ∈ {1, 2, 3}
- ``min_length`` ∈ {3, 4, 5}

Всего 9 комбинаций. Переопределяется через ``--grid`` в формате
``"0.75:1:3,0.80:2:4,..."``.

Согласование систем координат bbox'ов:

И эталонные, и найденные bbox'ы приводятся к **нормализованным
координатам ``[0..1]``** (делением на ширину/высоту страницы).
Это позволяет сравнивать их через IoU независимо от размера
страницы и DPI. Ранее найденные bbox'ы оставались в
PDF-points, а эталонные — нормализовались; сравнение IoU между
диапазонами ``[0..1]`` и ``[0..600]`` всегда давало 0, что
приводило к ``TP=0`` во всех комбинациях. Исправлено в текущей
версии.

Метрики:

Для каждой комбинации параметров и каждой пары ``(query, page)``
считаются:

- **TP**: bbox, найденный fuzzy, пересекается по IoU с хотя бы
  одним эталонным bbox (при ``IoU > --iou-threshold``).
- **FP**: bbox, найденный fuzzy, не пересекается ни с одним
  эталонным.
- **FN**: эталонный bbox, с которым не пересекается ни один
  bbox fuzzy.

Precision = TP / (TP + FP); Recall = TP / (TP + FN);
F1 = 2·P·R / (P + R).

Агрегация:

- По каждой комбинации параметров: суммарные TP/FP/FN по всем
  кейсам → средние P/R/F1.
- По каждой категории (если задана в manifest).
- По каждому запросу (если в явном режиме).

Ограничения:

- Fuzzy реализован локально (не импортирует DDS), чтобы измерять
  параметры вне контекста конкретной реализации
  ``HighlightsService``.
- Поддерживаются только однословные термины (без пробелов). Для
  терминов с пробелами скрипт разбивает их по пробелам и
  обрабатывает каждое слово независимо. Смежность слов фразы
  не проверяется — это верхняя оценка fuzzy.
- Скрипт автономен: при отсутствии ``dds_core`` в ``sys.path``
  используется локальная копия ``normalize_text``. При наличии —
  предпочитается DDS-версия (с проверкой соответствия).

Пример использования::

    # Оценка на проблемных запросах:
    python scripts/measure_fuzzy_quality.py \\
        --input problems.json \\
        --output /tmp/fuzzy_quality.csv

    # Оценка на «обычных» словах:
    python scripts/measure_fuzzy_quality.py \\
        --pdf /mnt/rd_documents/example.pdf \\
        --auto-generate 20 \\
        --output /tmp/fuzzy_quality_auto.csv

    # Оценка с явной сеткой параметров:
    python scripts/measure_fuzzy_quality.py \\
        --input problems.json \\
        --grid "0.80:2:4,0.85:2:4,0.90:2:4" \\
        --output /tmp/fuzzy_grid.csv
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import random
import sys
from collections.abc import Callable
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
# Нормализация текста (fallback при отсутствии DDS)
# ----------------------------------------------------------------------

_CYRILLIC_TO_LATIN: dict[str, str] = {
    "а": "a",
    "в": "b",
    "е": "e",
    "к": "k",
    "м": "m",
    "н": "h",
    "о": "o",
    "р": "p",
    "с": "c",
    "т": "t",
    "у": "y",
    "х": "x",
}
"""Локальная копия таблицы из ``dds_core.application.text_normalizer``.

Используется, если DDS-модуль недоступен. При наличии DDS
предпочитается его версия (см. :func:`_get_normalize_text`).
"""


def _fallback_normalize_text(text: str) -> str:
    """Локальная копия ``normalize_text`` из DDS.

    Применяется, если скрипт запускается вне контекста проекта
    (например, из другого каталога). При изменении DDS-версии
    эту копию нужно синхронизировать.

    Args:
        text: Исходная строка.

    Returns:
        Нормализованная строка (lowercase + замена кириллицы
        на визуально похожие латинские символы).
    """
    lowered = text.lower()
    for cyrillic, latin in _CYRILLIC_TO_LATIN.items():
        lowered = lowered.replace(cyrillic, latin)
    return lowered


def _get_normalize_text() -> Callable[[str], str]:
    """Возвращает функцию нормализации текста.

    Пытается импортировать ``normalize_text`` из
    ``dds_core.application.text_normalizer``. При успехе
    проверяет, что DDS-версия совпадает с локальной копией на
    контрольных строках. При несовпадении — предупреждение в
    stderr; используется всё равно DDS-версия (это источник
    истины).

    При неудаче импорта — используется локальная копия.

    Returns:
        Callable, принимающий строку и возвращающий
        нормализованную строку.
    """
    try:
        from dds_core.application.text_normalizer import (  # type: ignore[import-not-found]
            normalize_text,
        )

        test_inputs = ["Тест ЕС-423-1", "ГидроШпонка", "abc"]
        for s in test_inputs:
            if normalize_text(s) != _fallback_normalize_text(s):
                print(
                    "Предупреждение: DDS-версия normalize_text "
                    "отличается от локальной копии на строке "
                    f"{s!r}. Используется DDS-версия.",
                    file=sys.stderr,
                )
                break
        return normalize_text
    except ImportError:
        return _fallback_normalize_text


# ----------------------------------------------------------------------
# Модели
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class FuzzyParams:
    """Комбинация параметров fuzzy-поиска.

    Attributes:
        ratio_threshold: Порог схожести (0.0–1.0).
        length_delta: Допустимое расхождение по длине между
            термином и словом.
        min_length: Минимальная длина термина для применения fuzzy.
    """

    ratio_threshold: float
    length_delta: int
    min_length: int


@dataclass
class EvalCase:
    """Один кейс оценки.

    Attributes:
        query: Искомый термин (может содержать пробелы).
        doc_path: Абсолютный путь к PDF.
        page_number: Номер страницы (0-based).
        category: Категория (``"problem"`` / ``"control"`` / любая).
        expected_bboxes: Опциональный список эталонных bbox'ов в
            нормализованных координатах ``[x0, y0, x1, y1]``.
            Если ``None``, эталон вычисляется через
            ``page.search_for(query)``.
    """

    query: str
    doc_path: str
    page_number: int
    category: str = "unknown"
    expected_bboxes: list[tuple[float, float, float, float]] | None = None


@dataclass
class EvalResult:
    """Результат оценки одного кейса с одной комбинацией параметров.

    Attributes:
        query, doc_path, page_number, category: Копия из ``EvalCase``.
        params: Комбинация fuzzy-параметров.
        etl_count: Количество эталонных bbox'ов (ground truth).
        found_count: Количество bbox'ов, найденных fuzzy.
        tp, fp, fn: TP/FP/FN.
        etl_missing: ``True``, если эталон не определён
            (ни ``search_for``, ни manifest не дали результата).
    """

    query: str
    doc_path: str
    page_number: int
    category: str
    params: FuzzyParams
    etl_count: int = 0
    found_count: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    etl_missing: bool = False

    @property
    def precision(self) -> float:
        """Precision: TP / (TP + FP). ``0.0``, если TP + FP = 0."""
        denom = self.tp + self.fp
        return (self.tp / denom) if denom > 0 else 0.0

    @property
    def recall(self) -> float:
        """Recall: TP / (TP + FN). ``0.0``, если TP + FN = 0."""
        denom = self.tp + self.fn
        return (self.tp / denom) if denom > 0 else 0.0

    @property
    def f1(self) -> float:
        """F1: гармоническое среднее precision и recall."""
        p, r = self.precision, self.recall
        return (2 * p * r / (p + r)) if (p + r) > 0 else 0.0


# ----------------------------------------------------------------------
# Fuzzy-поиск (локальная реализация)
# ----------------------------------------------------------------------


def _fuzzy_search_in_words(
    words: list[tuple],
    term: str,
    params: FuzzyParams,
    normalize_text: Callable[[str], str],
    page_width: float,
    page_height: float,
) -> list[tuple[float, float, float, float, str]]:
    """Ищет слова, похожие на термин, среди слов страницы.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | ``normalized_term = normalize_text(term)``.         |
    +---+-----------------------------------------------------+
    | 2 | Если ``len(normalized_term) < min_length`` →        |
    |   | возврат пустого списка.                             |
    +---+-----------------------------------------------------+
    | 3 | Для каждого слова из ``words``:                     |
    |   | a. ``stripped = text.strip()``.                     |
    |   | b. Пропуск пустых.                                  |
    |   | c. ``norm_word = normalize_text(stripped)``.        |
    |   | d. Если ``abs(len(norm_word) - len(normalized_term))|
    |   |    > length_delta`` → пропуск.                      |
    |   | e. ``ratio = SequenceMatcher.ratio``.               |
    |   | f. Если ``ratio < ratio_threshold`` → пропуск.      |
    |   | g. Нормализация bbox делением на размеры страницы   |
    |   |    и добавление в результат.                        |
    +---+-----------------------------------------------------+
    | 4 | Возврат списка нормализованных bbox'ов.             |
    +---+-----------------------------------------------------+

    Примечание:
        Нормализация координат обязательна: эталонные bbox'ы,
        полученные через ``page.search_for``, также нормализуются
        в :func:`_compute_expected_via_search_for`. Сравнение IoU
        между bbox'ами в PDF-points и нормализованными в ``[0..1]``
        всегда даёт 0, что делает метрики бессмысленными.

    Args:
        words: Список кортежей из ``page.get_text("words")``.
            Формат элемента: ``(x0, y0, x1, y1, text, block, line, word)``.
        term: Термин для поиска.
        params: Комбинация параметров.
        normalize_text: Функция нормализации.
        page_width: Ширина страницы в PDF-points
            (``page.rect.width``).
        page_height: Высота страницы в PDF-points
            (``page.rect.height``).

    Returns:
        Список кортежей ``(x0, y0, x1, y1, normalized_word)``
        в нормализованных координатах ``[0..1]``.
    """
    normalized_term = normalize_text(term)
    if len(normalized_term) < params.min_length:
        return []

    safe_width = page_width if page_width > 0 else 1.0
    safe_height = page_height if page_height > 0 else 1.0

    results: list[tuple[float, float, float, float, str]] = []
    for item in words:
        if len(item) < 5:
            continue
        text = str(item[4]).strip()
        if not text:
            continue
        norm_word = normalize_text(text)
        if abs(len(norm_word) - len(normalized_term)) > params.length_delta:
            continue
        ratio = difflib.SequenceMatcher(None, normalized_term, norm_word).ratio()
        if ratio < params.ratio_threshold:
            continue
        results.append(
            (
                float(item[0]) / safe_width,
                float(item[1]) / safe_height,
                float(item[2]) / safe_width,
                float(item[3]) / safe_height,
                norm_word,
            )
        )
    return results


# ----------------------------------------------------------------------
# Сравнение bbox'ов
# ----------------------------------------------------------------------


def _iou(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    """IoU двух нормализованных bbox'ов.

    Args:
        a: Кортеж ``(x0, y0, x1, y1)`` в координатах ``[0..1]``.
        b: То же.

    Returns:
        IoU в диапазоне ``[0.0, 1.0]``. ``0.0``, если пересечение пусто.
    """
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = (ax1 - ax0) * (ay1 - ay0)
    area_b = (bx1 - bx0) * (by1 - by0)
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return inter / union


def _match_bboxes(
    found: list[tuple[float, float, float, float]],
    expected: list[tuple[float, float, float, float]],
    iou_threshold: float,
) -> tuple[int, int, int]:
    """Сопоставляет найденные bbox'ы с эталонными.

    Используется критерий «пересечение по IoU > threshold» с
    жадным сопоставлением: для каждого найденного bbox ищется
    лучший непокрытый эталон.

    Args:
        found: Bbox'ы, найденные fuzzy.
        expected: Эталонные bbox'ы.
        iou_threshold: Порог IoU для признания совпадения.

    Returns:
        Кортеж ``(tp, fp, fn)``.
    """
    if not found and not expected:
        return 0, 0, 0
    if not found:
        return 0, 0, len(expected)
    if not expected:
        return 0, len(found), 0

    matched_expected: set[int] = set()
    tp = 0
    for f in found:
        best_match_idx = -1
        best_iou = 0.0
        for i, e in enumerate(expected):
            if i in matched_expected:
                continue
            v = _iou(f, e)
            if v > best_iou:
                best_iou = v
                best_match_idx = i
        if best_match_idx >= 0 and best_iou >= iou_threshold:
            tp += 1
            matched_expected.add(best_match_idx)
    fp = len(found) - tp
    fn = len(expected) - len(matched_expected)
    return tp, fp, fn


# ----------------------------------------------------------------------
# Определение эталона
# ----------------------------------------------------------------------


def _compute_expected_via_search_for(
    page: pymupdf.Page,
    query: str,
) -> list[tuple[float, float, float, float]]:
    """Вычисляет эталонные bbox'ы через ``page.search_for(query)``.

    Пробует три варианта запроса: как есть, lowercase, uppercase.
    Возвращает первый непустой результат. Если все пусты —
    возвращает пустой список.

    Args:
        page: Страница PyMuPDF.
        query: Термин.

    Returns:
        Список нормализованных bbox'ов ``[0..1]``.
    """
    rect = page.rect
    width = float(rect.width) or 1.0
    height = float(rect.height) or 1.0

    variants = [query, query.lower(), query.upper()]
    for variant in variants:
        if not variant:
            continue
        try:
            rects = page.search_for(variant)
        except Exception:  # noqa: BLE001
            rects = []
        if rects:
            return [
                (
                    float(r.x0) / width,
                    float(r.y0) / height,
                    float(r.x1) / width,
                    float(r.y1) / height,
                )
                for r in rects
            ]
    return []


# ----------------------------------------------------------------------
# Обработка одного кейса
# ----------------------------------------------------------------------


def _evaluate_case(
    case: EvalCase,
    params_list: list[FuzzyParams],
    normalize_text: Callable[[str], str],
    iou_threshold: float,
) -> list[EvalResult]:
    """Обрабатывает один кейс с полным набором параметров.

    Открывает PDF, извлекает слова и (при отсутствии явных
    ``expected_bboxes``) вычисляет эталон через ``search_for``.
    Прогоняет fuzzy-поиск для каждой комбинации параметров.

    Args:
        case: Кейс.
        params_list: Список комбинаций параметров.
        normalize_text: Функция нормализации.
        iou_threshold: Порог IoU для сопоставления bbox'ов.

    Returns:
        Список результатов по одному на каждую комбинацию.
    """
    results: list[EvalResult] = []

    try:
        doc = pymupdf.open(case.doc_path)
    except Exception as e:  # noqa: BLE001
        print(
            f"  Ошибка открытия {case.doc_path}: {e}",
            file=sys.stderr,
        )
        for params in params_list:
            results.append(
                EvalResult(
                    query=case.query,
                    doc_path=case.doc_path,
                    page_number=case.page_number,
                    category=case.category,
                    params=params,
                    etl_missing=True,
                )
            )
        return results

    try:
        if case.page_number < 0 or case.page_number >= len(doc):
            for params in params_list:
                results.append(
                    EvalResult(
                        query=case.query,
                        doc_path=case.doc_path,
                        page_number=case.page_number,
                        category=case.category,
                        params=params,
                        etl_missing=True,
                    )
                )
            return results

        page = doc.load_page(case.page_number)
        words = page.get_text("words") or []

        # Эталон.
        if case.expected_bboxes is not None:
            expected = case.expected_bboxes
        else:
            expected = _compute_expected_via_search_for(page, case.query)

        # Если эталон пуст — исключаем из метрик.
        if not expected:
            for params in params_list:
                results.append(
                    EvalResult(
                        query=case.query,
                        doc_path=case.doc_path,
                        page_number=case.page_number,
                        category=case.category,
                        params=params,
                        etl_missing=True,
                    )
                )
            return results

        # Размеры страницы для нормализации bbox'ов, найденных fuzzy.
        page_width = float(page.rect.width) or 1.0
        page_height = float(page.rect.height) or 1.0

        # Прогон fuzzy-поиска для каждой комбинации параметров.
        for params in params_list:
            found_raw = _fuzzy_search_in_words(
                words,
                case.query,
                params,
                normalize_text,
                page_width,
                page_height,
            )
            found_bboxes = [(x0, y0, x1, y1) for x0, y0, x1, y1, _ in found_raw]

            tp, fp, fn = _match_bboxes(found_bboxes, expected, iou_threshold)
            results.append(
                EvalResult(
                    query=case.query,
                    doc_path=case.doc_path,
                    page_number=case.page_number,
                    category=case.category,
                    params=params,
                    etl_count=len(expected),
                    found_count=len(found_bboxes),
                    tp=tp,
                    fp=fp,
                    fn=fn,
                    etl_missing=False,
                )
            )
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001, S110
            pass

    return results


# ----------------------------------------------------------------------
# Загрузка кейсов
# ----------------------------------------------------------------------


def _load_cases_from_manifest(manifest_path: Path) -> list[EvalCase]:
    """Загружает список кейсов из manifest.json.

    Формат::

        {
            "cases": [
                {
                    "query": "3600мсс1012-р3а",
                    "doc_path": "abs/or/rel/path.pdf",
                    "page_number": 5,
                    "category": "problem",
                    "expected_bboxes": [[0.1, 0.2, 0.3, 0.4], ...]
                },
                ...
            ]
        }

    Поле ``expected_bboxes`` опционально. Если не задано — эталон
    вычисляется через ``page.search_for(query)``.

    Относительные пути резолвятся относительно каталога манифеста.

    Args:
        manifest_path: Путь к manifest.json.

    Returns:
        Список кейсов.
    """
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    base_dir = manifest_path.parent
    cases: list[EvalCase] = []
    for entry in data.get("cases", []):
        query = str(entry.get("query", "")).strip()
        doc_path_raw = str(entry.get("doc_path", "")).strip()
        if not query or not doc_path_raw:
            continue
        p = Path(doc_path_raw)
        if not p.is_absolute():
            p = (base_dir / p).resolve()
        page_number = int(entry.get("page_number", 0))
        category = str(entry.get("category", "unknown"))

        expected_raw = entry.get("expected_bboxes")
        expected: list[tuple[float, float, float, float]] | None = None
        if expected_raw is not None:
            expected = []
            for bbox in expected_raw:
                if len(bbox) != 4:
                    continue
                expected.append((float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])))

        cases.append(
            EvalCase(
                query=query,
                doc_path=str(p),
                page_number=page_number,
                category=category,
                expected_bboxes=expected,
            )
        )
    return cases


def _generate_auto_cases(
    pdf_path: Path,
    n_per_page: int,
    category: str,
    seed: int,
    min_length: int,
) -> list[EvalCase]:
    """Генерирует кейсы автоматически: N случайных слов с каждой страницы.

    Для каждой страницы извлекает слова через
    ``get_text("words")``, фильтрует по длине и берёт ``n_per_page``
    случайных. Каждое слово становится кейсом с запросом, равным
    самому слову. Эталон вычисляется через ``page.search_for``.

    Args:
        pdf_path: Путь к PDF.
        n_per_page: Количество слов с каждой страницы.
        category: Категория для созданных кейсов.
        seed: Seed для воспроизводимости.
        min_length: Минимальная длина слова (фильтр).

    Returns:
        Список кейсов.
    """
    rng = random.Random(seed)
    cases: list[EvalCase] = []

    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as e:  # noqa: BLE001
        print(f"Ошибка открытия {pdf_path}: {e}", file=sys.stderr)
        return cases

    try:
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            words = page.get_text("words") or []
            candidates: list[str] = []
            for item in words:
                if len(item) < 5:
                    continue
                text = str(item[4]).strip()
                if len(text) < min_length:
                    continue
                if not all(c.isprintable() for c in text):
                    continue
                candidates.append(text)

            if not candidates:
                continue

            rng.shuffle(candidates)
            selected = candidates[:n_per_page]
            for word in selected:
                cases.append(
                    EvalCase(
                        query=word,
                        doc_path=str(pdf_path.resolve()),
                        page_number=page_num,
                        category=category,
                        expected_bboxes=None,
                    )
                )
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001, S110
            pass

    return cases


# ----------------------------------------------------------------------
# Агрегация и вывод
# ----------------------------------------------------------------------


def _aggregate(
    results: list[EvalResult],
) -> dict[FuzzyParams, dict[str, float]]:
    """Агрегирует результаты по комбинациям параметров.

    Для каждой комбинации суммирует TP/FP/FN по всем кейсам
    (кроме ``etl_missing=True``) и вычисляет общие
    precision/recall/F1.

    Args:
        results: Список результатов.

    Returns:
        Словарь ``{params: {"tp": ..., "fp": ..., "fn": ...,
        "precision": ..., "recall": ..., "f1": ..., "cases": ...}}``.
    """
    agg: dict[FuzzyParams, dict[str, float]] = {}
    for r in results:
        if r.etl_missing:
            continue
        bucket = agg.setdefault(
            r.params,
            {"tp": 0, "fp": 0, "fn": 0, "cases": 0},
        )
        bucket["tp"] += r.tp
        bucket["fp"] += r.fp
        bucket["fn"] += r.fn
        bucket["cases"] += 1

    for b in agg.values():
        tp = b["tp"]
        fp = b["fp"]
        fn = b["fn"]
        p = (tp / (tp + fp)) if (tp + fp) > 0 else 0.0
        r = (tp / (tp + fn)) if (tp + fn) > 0 else 0.0
        f1 = (2 * p * r / (p + r)) if (p + r) > 0 else 0.0
        b["precision"] = p
        b["recall"] = r
        b["f1"] = f1
    return agg


def _write_csv(results: list[EvalResult], output_path: Path) -> None:
    """Записывает результаты в CSV (одна строка на результат).

    Формат полей:

        query, doc_path, page_number, category,
        ratio_threshold, length_delta, min_length,
        etl_count, found_count, tp, fp, fn,
        precision, recall, f1, etl_missing

    Args:
        results: Список результатов.
        output_path: Путь к CSV.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "query",
        "doc_path",
        "page_number",
        "category",
        "ratio_threshold",
        "length_delta",
        "min_length",
        "etl_count",
        "found_count",
        "tp",
        "fp",
        "fn",
        "precision",
        "recall",
        "f1",
        "etl_missing",
    ]
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in results:
            writer.writerow(
                {
                    "query": r.query,
                    "doc_path": r.doc_path,
                    "page_number": r.page_number,
                    "category": r.category,
                    "ratio_threshold": r.params.ratio_threshold,
                    "length_delta": r.params.length_delta,
                    "min_length": r.params.min_length,
                    "etl_count": r.etl_count,
                    "found_count": r.found_count,
                    "tp": r.tp,
                    "fp": r.fp,
                    "fn": r.fn,
                    "precision": f"{r.precision:.4f}",
                    "recall": f"{r.recall:.4f}",
                    "f1": f"{r.f1:.4f}",
                    "etl_missing": "1" if r.etl_missing else "0",
                }
            )


def _print_summary(
    results: list[EvalResult],
    agg: dict[FuzzyParams, dict[str, float]],
) -> None:
    """Печатает сводку: лучшая комбинация параметров и полная таблица.

    Args:
        results: Список результатов.
        agg: Агрегированные метрики из :func:`_aggregate`.
    """
    total = len(results)
    missing = sum(1 for r in results if r.etl_missing)

    print()
    print("=" * 78)
    print("Сводка measure_fuzzy_quality")
    print("=" * 78)
    print(f"Всего оценок (кейс × params): {total}")
    print(f"Исключено (etl_missing):      {missing}")
    print(f"Учтено в метриках:            {total - missing}")

    if not agg:
        print("Нет данных для агрегации.")
        return

    print()
    print("Полная таблица параметров (по убыванию F1):")
    header = (
        f"  {'thr':>5s} {'dlen':>5s} {'mlen':>5s} "
        f"{'TP':>6s} {'FP':>6s} {'FN':>6s} "
        f"{'P':>7s} {'R':>7s} {'F1':>7s} {'cases':>6s}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    sorted_params = sorted(
        agg.items(),
        key=lambda kv: (-kv[1]["f1"], kv[0].ratio_threshold),
    )
    for params, b in sorted_params:
        print(
            f"  {params.ratio_threshold:5.2f} "
            f"{params.length_delta:5d} "
            f"{params.min_length:5d} "
            f"{int(b['tp']):6d} {int(b['fp']):6d} {int(b['fn']):6d} "
            f"{b['precision']:7.3f} "
            f"{b['recall']:7.3f} "
            f"{b['f1']:7.3f} "
            f"{int(b['cases']):6d}"
        )

    best_params, best = sorted_params[0]
    print()
    print("Лучшая комбинация:")
    print(f"  ratio_threshold = {best_params.ratio_threshold}")
    print(f"  length_delta    = {best_params.length_delta}")
    print(f"  min_length      = {best_params.min_length}")
    print(f"  P = {best['precision']:.4f}, R = {best['recall']:.4f}, F1 = {best['f1']:.4f}")
    print("=" * 78)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


_DEFAULT_GRID = [
    FuzzyParams(0.75, 1, 3),
    FuzzyParams(0.75, 2, 4),
    FuzzyParams(0.80, 1, 3),
    FuzzyParams(0.80, 2, 4),
    FuzzyParams(0.85, 2, 4),
    FuzzyParams(0.85, 3, 4),
    FuzzyParams(0.90, 2, 4),
    FuzzyParams(0.90, 3, 5),
    FuzzyParams(0.95, 2, 5),
]
"""Комбинации параметров по умолчанию для тестирования."""


def _parse_grid(grid_str: str) -> list[FuzzyParams]:
    """Парсит строку сетки ``"thr:dlen:mlen,..."``.

    Args:
        grid_str: Строка сетки.

    Returns:
        Список ``FuzzyParams``.

    Raises:
        ValueError: Если строка некорректна.
    """
    items = [s.strip() for s in grid_str.split(",") if s.strip()]
    params_list: list[FuzzyParams] = []
    for item in items:
        parts = item.split(":")
        if len(parts) != 3:
            raise ValueError(f"Некорректная комбинация: {item!r}")
        params_list.append(
            FuzzyParams(
                ratio_threshold=float(parts[0]),
                length_delta=int(parts[1]),
                min_length=int(parts[2]),
            )
        )
    return params_list


def _build_arg_parser() -> argparse.ArgumentParser:
    """Создаёт парсер аргументов командной строки."""
    parser = argparse.ArgumentParser(
        description=(
            "Измерение качества fuzzy-поиска для подсветки совпадений. Фаза 0 плана рефакторинга."
        ),
    )
    parser.add_argument(
        "--input",
        help="Путь к manifest.json со списком кейсов.",
    )
    parser.add_argument(
        "--pdf",
        help=("Путь к PDF для режима --auto-generate. Взаимоисключающий с --input."),
    )
    parser.add_argument(
        "--auto-generate",
        type=int,
        default=0,
        help=("Количество случайных слов с каждой страницы PDF (требует --pdf)."),
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Путь к выходному CSV.",
    )
    parser.add_argument(
        "--grid",
        default="",
        help=(
            "Сетка параметров в формате 'thr1:dlen1:mlen1,"
            "thr2:dlen2:mlen2'. Пусто — используется сетка "
            "по умолчанию."
        ),
    )
    parser.add_argument(
        "--iou-threshold",
        type=float,
        default=0.5,
        help="Порог IoU для признания bbox'ов совпавшими (по умолчанию 0.5).",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=20,
        help="Печатать прогресс каждые N кейсов.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Seed для генерации случайных слов (--auto-generate).",
    )
    parser.add_argument(
        "--category",
        default="auto",
        help="Категория для auto-generate кейсов (по умолчанию 'auto').",
    )
    return parser


def main() -> None:
    """Точка входа скрипта.

    Операции:

    +----+----------------------------------------------------+
    | №  | Описание                                           |
    +====+====================================================+
    | 1  | Парсинг аргументов.                                |
    +----+----------------------------------------------------+
    | 2  | Формирование списка кейсов (manifest или auto).    |
    +----+----------------------------------------------------+
    | 3  | Прогон оценки для каждой комбинации параметров.    |
    +----+----------------------------------------------------+
    | 4  | Запись CSV.                                        |
    +----+----------------------------------------------------+
    | 5  | Агрегация и вывод сводки.                          |
    +----+----------------------------------------------------+
    | 6  | Обработка ``KeyboardInterrupt``.                    |
    +----+----------------------------------------------------+
    """
    parser = _build_arg_parser()
    args = parser.parse_args()

    if not args.input and not args.pdf:
        print("Ошибка: требуется --input или --pdf.", file=sys.stderr)
        sys.exit(1)
    if args.input and args.pdf:
        print(
            "Ошибка: --input и --pdf взаимоисключающие.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.grid:
        try:
            params_list = _parse_grid(args.grid)
        except ValueError as e:
            print(f"Ошибка парсинга --grid: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        params_list = list(_DEFAULT_GRID)

    print(f"Комбинаций параметров: {len(params_list)}")

    normalize_text = _get_normalize_text()

    # Формирование списка кейсов.
    cases: list[EvalCase] = []
    if args.input:
        manifest_path = Path(args.input)
        if not manifest_path.is_file():
            print(
                f"Ошибка: файл не найден: {manifest_path}",
                file=sys.stderr,
            )
            sys.exit(1)
        cases = _load_cases_from_manifest(manifest_path)
    else:
        pdf_path = Path(args.pdf)
        if not pdf_path.is_file():
            print(
                f"Ошибка: файл не найден: {pdf_path}",
                file=sys.stderr,
            )
            sys.exit(1)
        if args.auto_generate <= 0:
            print(
                "Ошибка: --auto-generate должен быть > 0 при --pdf.",
                file=sys.stderr,
            )
            sys.exit(1)
        cases = _generate_auto_cases(
            pdf_path=pdf_path,
            n_per_page=args.auto_generate,
            category=args.category,
            seed=args.seed,
            min_length=min(p.min_length for p in params_list),
        )

    if not cases:
        print("Нет кейсов для обработки.", file=sys.stderr)
        sys.exit(1)

    print(f"Кейсов: {len(cases)}")

    # Оценка.
    results: list[EvalResult] = []
    try:
        for idx, case in enumerate(cases, start=1):
            case_results = _evaluate_case(
                case=case,
                params_list=params_list,
                normalize_text=normalize_text,
                iou_threshold=args.iou_threshold,
            )
            results.extend(case_results)

            if args.progress_every > 0 and idx % args.progress_every == 0:
                print(f"  Обработано кейсов: {idx}/{len(cases)}")
    except KeyboardInterrupt:
        print(
            "\nПрервано пользователем. Сохраняю промежуточные результаты...",
            file=sys.stderr,
        )

    _write_csv(results, Path(args.output))
    print(f"Результаты записаны: {Path(args.output).resolve()}")

    agg = _aggregate(results)
    _print_summary(results, agg)


if __name__ == "__main__":
    main()
