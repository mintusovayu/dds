"""
Тесты построения плана индексации документа.

Назначение
----------
Проверка контракта
:func:`dds_core.application.index_plan_builder.build_index_plan`:
функция открывает PDF через абстракцию ``ITextExtractor``,
извлекает текстовое содержимое всех страниц, применяет нормализацию
и формирует ``DocumentIndexPlan``.

Модуль введён в Фазе 5 (DocumentIndexPlan) как замена
``query_builder.build_document_queries``: вместо формирования
SQL-строк функция возвращает доменную модель, а SQL-специфика
вынесена в ``SqliteIndexWriter``.

Проверяемые сценарии
--------------------

+-------------------------------------+--------------------------------+
| Группа                              | Что проверяется                |
+=====================================+================================+
| Успешный сценарий                   | PDF открывается, план имеет    |
|                                     | корректные метаданные и        |
|                                     | страницы.                      |
+-------------------------------------+--------------------------------+
| Ошибка открытия                     | ``open_document`` бросает →    |
|                                     | ``None``.                      |
+-------------------------------------+--------------------------------+
| Ошибка ``page_count``               | ``document.page_count()``      |
|                                     | бросает → ``None``.            |
+-------------------------------------+--------------------------------+
| Пропуск пустых страниц              | При ``SKIP_EMPTY_TEXT_PAGES=    |
|                                     | True`` пустые страницы не      |
|                                     | попадают в ``pages``.          |
+-------------------------------------+--------------------------------+
| Включение пустых страниц            | При ``SKIP_EMPTY_TEXT_PAGES=    |
|                                     | False`` пустые страницы         |
|                                     | включаются с текстом ``""``.   |
+-------------------------------------+--------------------------------+
| Нормализация                        | ``normalized_text ==            |
|                                     | normalize_text(text)``.        |
+-------------------------------------+--------------------------------+
| Порядок страниц                     | ``pages`` отсортирован по      |
|                                     | ``page_number``.               |
+-------------------------------------+--------------------------------+
| Пустой PDF                          | ``page_count=0`` →             |
|                                     | ``pages=()``.                  |
+-------------------------------------+--------------------------------+
| Все страницы пустые                 | Все пропущены → ``pages=()``,  |
|                                     | ``page_count > 0``.            |
+-------------------------------------+--------------------------------+
| Гарантированное закрытие документа  | ``document.close()``           |
|                                     | вызывается в ``finally``, даже |
|                                     | при исключении.                |
+-------------------------------------+--------------------------------+
| Инварианты плана                    | Поля плана совпадают с         |
|                                     | входными аргументами.          |
+-------------------------------------+--------------------------------+
| ``indexed_at``                      | Установлен, ISO 8601, UTC.     |
+-------------------------------------+--------------------------------+

Стратегия тестирования
----------------------
- **Заглушки вместо PyMuPDF.** ``_StubTextExtractor`` и
  ``_StubTextDocument`` реализуют Protocol'ы ``ITextExtractor`` /
  ``ITextDocument``. Тесты изолированы от PDF-библиотеки и
  детерминированы. Согласовано с ``test_highlights_transform_matrix.py``.
- **Параметризация через ``pytest.mark.parametrize``** для
  сценариев с разными комбинациями страниц / флагов.
- **Spy на ``document.close()``** — проверяет гарантию
  освобождения ресурсов.
- **Проверка типа** через ``isinstance(plan, DocumentIndexPlan)``
  перед доступом к полям (mypy-safety).

Границы
-------
- **Реальное извлечение текста** через PyMuPDF — покрывается
  smoke-тестами и бенчмарками (``bench_slow.py``).
- **Запись в БД** — покрывается ``test_sqlite_index_writer.py``.
- **Обработка ошибок ``get_page_text``** — не в области: заглушка
  возвращает то, что вернула бы реальная реализация; логика
  изоляции ошибок в ``PyMuPDFTextDocument`` покрыта косвенно
  через smoke-тесты.

Запуск
------
::

    pytest tests/test_index_plan_builder.py -v

Принципы:
    - модуль не выполняет логирования;
    - не читает и не пишет файлы;
    - не имеет побочных эффектов при импорте;
    - каждый тест изолирован, не зависит от порядка выполнения;
    - тесты детерминированы: одинаковый вход → одинаковый результат.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from dds_core.application.index_plan_builder import build_index_plan
from dds_core.domain import config as core_config
from dds_core.domain.index_plan import DocumentIndexPlan, PageRecord
from dds_core.domain.text_normalization import normalize_text

# =====================================================================
# Константы
# =====================================================================

_DOC_ID = "doc-abc-123"
_FILE_PATH = "раздел_01/чертёж_001.pdf"
_FILE_HASH = "a3f2" + "0" * 60
_FILE_SIZE = 102_400
_LAST_MODIFIED = "2024-01-15T10:30:00+00:00"
_ABS_FILE_PATH = "/mnt/rd/раздел_01/чертёж_001.pdf"


# =====================================================================
# Заглушки
# =====================================================================


class _StubTextDocument:
    """Заглушка ``ITextDocument``.

    Возвращает заранее заданный текст по страницам. Фиксирует
    вызов ``close()`` через атрибут ``close_called``.

    Attributes:
        _pages: Список текстов страниц (индекс = номер страницы).
        _page_count_override: Если задан — ``page_count()`` вернёт
            это значение. Иначе — ``len(_pages)``.
        _page_count_error: Если задан — ``page_count()`` бросит
            это исключение.
        _get_page_text_error: Если задан — ``get_page_text()``
            бросит это исключение для любой страницы.
        close_called: Флаг вызова ``close()``.
    """

    def __init__(
        self,
        pages: list[str] | None = None,
        *,
        page_count_override: int | None = None,
        page_count_error: Exception | None = None,
        get_page_text_error: Exception | None = None,
    ) -> None:
        self._pages = pages if pages is not None else []
        self._page_count_override = page_count_override
        self._page_count_error = page_count_error
        self._get_page_text_error = get_page_text_error
        self.close_called = False

    def page_count(self) -> int:
        if self._page_count_error is not None:
            raise self._page_count_error
        if self._page_count_override is not None:
            return self._page_count_override
        return len(self._pages)

    def get_page_text(self, page_index: int) -> str:
        if self._get_page_text_error is not None:
            raise self._get_page_text_error
        if page_index < 0 or page_index >= len(self._pages):
            raise IndexError(f"page_index={page_index} вне диапазона")
        return self._pages[page_index]

    def render_page(self, page_index: int, dpi: int) -> bytes:
        raise NotImplementedError("render_page не используется в build_index_plan")

    def build_word_index(self, page_index: int) -> Any:
        raise NotImplementedError("build_word_index не используется в build_index_plan")

    def close(self) -> None:
        self.close_called = True


class _StubTextExtractor:
    """Заглушка ``ITextExtractor``.

    Возвращает заранее заданный ``_StubTextDocument`` или бросает
    заданное исключение.

    Attributes:
        _document: Документ, возвращаемый при ``open_document``.
        _open_error: Если задан — ``open_document`` бросит его.
        open_called: Флаг вызова ``open_document``.
        last_open_path: Путь, переданный в ``open_document``.
    """

    def __init__(
        self,
        document: _StubTextDocument | None = None,
        *,
        open_error: Exception | None = None,
    ) -> None:
        self._document = document
        self._open_error = open_error
        self.open_called = False
        self.last_open_path: str | None = None

    def open_document(self, file_path: str) -> _StubTextDocument:
        self.open_called = True
        self.last_open_path = file_path
        if self._open_error is not None:
            raise self._open_error
        assert self._document is not None, "Stub configured without document"
        return self._document


# =====================================================================
# Helpers
# =====================================================================


def _make_params() -> dict[str, Any]:
    """Возвращает словарь валидных входных параметров.

    Используется как база для ``**kwargs`` в вызовах
    :func:`build_index_plan`. Снижает дублирование в тестах.

    Returns:
        Словарь с ключами ``doc_id``, ``file_path``, ``file_hash``,
        ``file_size``, ``last_modified``, ``abs_file_path``.
    """
    return {
        "doc_id": _DOC_ID,
        "file_path": _FILE_PATH,
        "file_hash": _FILE_HASH,
        "file_size": _FILE_SIZE,
        "last_modified": _LAST_MODIFIED,
        "abs_file_path": _ABS_FILE_PATH,
    }


# =====================================================================
# Раздел 1. Успешный сценарий
# =====================================================================


def test_successful_plan_with_two_pages() -> None:
    """Валидный PDF с двумя непустыми страницами → план с 2 страницами.

    Проверяет:
        - возврат ``DocumentIndexPlan``;
        - поля плана совпадают с входными аргументами;
        - ``page_count == 2``;
        - ``len(pages) == 2``;
        - ``pages[0].page_number == 0``, ``pages[1].page_number == 1``.
    """
    doc = _StubTextDocument(pages=["First page text", "Second page text"])
    extractor = _StubTextExtractor(doc)

    plan = build_index_plan(**_make_params(), text_extractor=extractor)

    assert plan is not None
    assert isinstance(plan, DocumentIndexPlan)
    assert plan.doc_id == _DOC_ID
    assert plan.file_path == _FILE_PATH
    assert plan.file_hash == _FILE_HASH
    assert plan.file_size == _FILE_SIZE
    assert plan.last_modified == _LAST_MODIFIED
    assert plan.page_count == 2
    assert len(plan.pages) == 2
    assert plan.pages[0].page_number == 0
    assert plan.pages[1].page_number == 1
    assert plan.pages[0].text == "First page text"
    assert plan.pages[1].text == "Second page text"


def test_extractor_open_document_called_with_abs_path() -> None:
    """``open_document`` вызывается с корректным абсолютным путём."""
    doc = _StubTextDocument(pages=["text"])
    extractor = _StubTextExtractor(doc)

    _ = build_index_plan(**_make_params(), text_extractor=extractor)

    assert extractor.open_called is True
    assert extractor.last_open_path == _ABS_FILE_PATH


def test_document_closed_after_success() -> None:
    """``document.close()`` вызывается после успешного построения."""
    doc = _StubTextDocument(pages=["text"])
    extractor = _StubTextExtractor(doc)

    _ = build_index_plan(**_make_params(), text_extractor=extractor)

    assert doc.close_called is True


def test_indexed_at_is_valid_iso8601_utc() -> None:
    """``indexed_at`` — валидный ISO 8601 с UTC.

    ``datetime.fromisoformat`` должен распарсить значение;
    результат — aware-datetime (timezone не ``None``).
    """
    doc = _StubTextDocument(pages=["text"])
    extractor = _StubTextExtractor(doc)

    plan = build_index_plan(**_make_params(), text_extractor=extractor)
    assert plan is not None

    parsed = datetime.fromisoformat(plan.indexed_at)
    assert parsed.tzinfo is not None


# =====================================================================
# Раздел 2. Ошибка открытия
# =====================================================================


@pytest.mark.parametrize(
    "open_error",
    [
        FileNotFoundError("file not found"),
        OSError("permission denied"),
        RuntimeError("MuPDF error while opening"),
    ],
    ids=["file-not-found", "os-error", "runtime-error"],
)
def test_open_document_error_returns_none(open_error: Exception) -> None:
    """Любая ошибка ``open_document`` → ``None``.

    Параметризация покрывает типичные типы ошибок, которые может
    бросить ``PyMuPDFTextExtractor.open_document``.
    """
    extractor = _StubTextExtractor(open_error=open_error)

    plan = build_index_plan(**_make_params(), text_extractor=extractor)

    assert plan is None


# =====================================================================
# Раздел 3. Ошибка page_count
# =====================================================================


def test_page_count_error_returns_none() -> None:
    """Ошибка ``document.page_count()`` → ``None`` и закрытие документа.

    Проверяет, что ``finally`` гарантирует ``close()`` даже при
    исключении в теле.
    """
    doc = _StubTextDocument(page_count_error=RuntimeError("damaged PDF"))
    extractor = _StubTextExtractor(doc)

    plan = build_index_plan(**_make_params(), text_extractor=extractor)

    assert plan is None
    assert doc.close_called is True


def test_get_page_text_error_returns_none_and_closes() -> None:
    """Ошибка ``document.get_page_text()`` → ``None`` и закрытие.

    В реальной реализации ``PyMuPDFTextDocument.get_page_text``
    изолирует типичные ошибки страницы и возвращает ``""``. Но
    если реализация всё-таки пробросит исключение (например,
    ``RuntimeError`` при критической ошибке PyMuPDF), функция
    должна вернуть ``None`` и закрыть документ.
    """
    doc = _StubTextDocument(
        pages=["page 1", "page 2"],
        get_page_text_error=RuntimeError("critical MuPDF error"),
    )
    extractor = _StubTextExtractor(doc)

    plan = build_index_plan(**_make_params(), text_extractor=extractor)

    assert plan is None
    assert doc.close_called is True


# =====================================================================
# Раздел 4. Пропуск пустых страниц
# =====================================================================


def test_skip_empty_pages_when_config_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SKIP_EMPTY_TEXT_PAGES=True`` → пустые страницы не в плане.

    Сценарий: 5 страниц, из них 2 пустые (индексы 1 и 3).
    Ожидается ``page_count=5``, ``len(pages)=3``.
    """
    monkeypatch.setattr(core_config, "SKIP_EMPTY_TEXT_PAGES", True)

    doc = _StubTextDocument(
        pages=["Page 0", "", "Page 2", "   ", "Page 4"],
    )
    extractor = _StubTextExtractor(doc)

    plan = build_index_plan(**_make_params(), text_extractor=extractor)
    assert plan is not None

    assert plan.page_count == 5
    assert len(plan.pages) == 3
    assert [p.page_number for p in plan.pages] == [0, 2, 4]


def test_include_empty_pages_when_config_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SKIP_EMPTY_TEXT_PAGES=False`` → пустые страницы в плане.

    Сценарий: 5 страниц, из них 2 пустые. Ожидается
    ``page_count=5``, ``len(pages)=5``, пустые страницы имеют
    ``text=""``.
    """
    monkeypatch.setattr(core_config, "SKIP_EMPTY_TEXT_PAGES", False)

    doc = _StubTextDocument(
        pages=["Page 0", "", "Page 2", "   ", "Page 4"],
    )
    extractor = _StubTextExtractor(doc)

    plan = build_index_plan(**_make_params(), text_extractor=extractor)
    assert plan is not None

    assert plan.page_count == 5
    assert len(plan.pages) == 5
    assert [p.page_number for p in plan.pages] == [0, 1, 2, 3, 4]
    assert plan.pages[1].text == ""
    assert plan.pages[3].text == "   "


def test_whitespace_only_pages_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Страницы из пробелов/табов/новых строк считаются пустыми.

    Проверяет использование ``.strip()`` перед проверкой пустоты.
    """
    monkeypatch.setattr(core_config, "SKIP_EMPTY_TEXT_PAGES", True)

    doc = _StubTextDocument(
        pages=["   ", "\t\n", "  \n  \t  ", "Real text"],
    )
    extractor = _StubTextExtractor(doc)

    plan = build_index_plan(**_make_params(), text_extractor=extractor)
    assert plan is not None

    assert plan.page_count == 4
    assert len(plan.pages) == 1
    assert plan.pages[0].page_number == 3
    assert plan.pages[0].text == "Real text"


# =====================================================================
# Раздел 5. Нормализация
# =====================================================================


def test_normalized_text_matches_normalize_text() -> None:
    """``normalized_text == normalize_text(text)`` для каждой страницы.

    Сценарий с кириллицей: проверяет, что нормализация применена
    и согласована с функцией domain-слоя.
    """
    doc = _StubTextDocument(
        pages=[
            "ЕС-423-1",
            "Пример текста",
            "hydroseal",
        ],
    )
    extractor = _StubTextExtractor(doc)

    plan = build_index_plan(**_make_params(), text_extractor=extractor)
    assert plan is not None

    for page in plan.pages:
        assert page.normalized_text == normalize_text(page.text)


def test_normalized_text_idempotent_and_partial() -> None:
    """Нормализация — частичная (не транслитерация).

    Проверяет контракт ``normalize_text``: ``"ЕС-423-1"`` →
    ``"ec-423-1"`` (заменены только символы из
    ``CYRILLIC_TO_LATIN_MAP``).
    """
    doc = _StubTextDocument(pages=["ЕС-423-1"])
    extractor = _StubTextExtractor(doc)

    plan = build_index_plan(**_make_params(), text_extractor=extractor)
    assert plan is not None

    assert plan.pages[0].normalized_text == "ec-423-1"
    assert plan.pages[0].text == "ЕС-423-1"  # оригинал сохранён


# =====================================================================
# Раздел 6. Порядок страниц
# =====================================================================


def test_pages_order_preserved() -> None:
    """Страницы идут в порядке обхода (0, 1, 2, ...).

    Даже если часть страниц пропущена (пустые), порядок оставшихся
    сохраняется.
    """
    doc = _StubTextDocument(
        pages=["P0", "", "P2", "", "P4", "P5"],
    )
    extractor = _StubTextExtractor(doc)

    plan = build_index_plan(**_make_params(), text_extractor=extractor)
    assert plan is not None

    page_numbers = [p.page_number for p in plan.pages]
    assert page_numbers == sorted(page_numbers)


# =====================================================================
# Раздел 7. Пустой PDF
# =====================================================================


def test_empty_pdf_zero_pages() -> None:
    """PDF с 0 страниц → план с ``page_count=0`` и ``pages=()``."""
    doc = _StubTextDocument(pages=[])
    extractor = _StubTextExtractor(doc)

    plan = build_index_plan(**_make_params(), text_extractor=extractor)
    assert plan is not None

    assert plan.page_count == 0
    assert plan.pages == ()


def test_all_pages_empty_returns_plan_with_no_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Все страницы пусты → план с ``page_count > 0`` и ``pages=()``.

    Инвариант: ``len(pages) <= page_count``, причём ``len(pages)``
    может быть 0.
    """
    monkeypatch.setattr(core_config, "SKIP_EMPTY_TEXT_PAGES", True)

    doc = _StubTextDocument(pages=["", "  ", "\t"])
    extractor = _StubTextExtractor(doc)

    plan = build_index_plan(**_make_params(), text_extractor=extractor)
    assert plan is not None

    assert plan.page_count == 3
    assert plan.pages == ()


# =====================================================================
# Раздел 8. Инварианты возвращаемого плана
# =====================================================================


def test_plan_is_frozen_dataclass() -> None:
    """``DocumentIndexPlan`` — иммутабельный (``frozen=True``).

    Проверяет через попытку присваивания — должно быть
    ``FrozenInstanceError``.
    """
    import dataclasses

    doc = _StubTextDocument(pages=["text"])
    extractor = _StubTextExtractor(doc)

    plan = build_index_plan(**_make_params(), text_extractor=extractor)
    assert plan is not None

    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.doc_id = "changed"  # type: ignore[misc]


def test_pages_is_tuple() -> None:
    """``plan.pages`` — ``tuple`` (не ``list``).

    Инвариант из Шага 1: ``pages: tuple[PageRecord, ...]``.
    Проверяет корректный тип для pickle-сериализации и hashability.
    """
    doc = _StubTextDocument(pages=["text"])
    extractor = _StubTextExtractor(doc)

    plan = build_index_plan(**_make_params(), text_extractor=extractor)
    assert plan is not None

    assert isinstance(plan.pages, tuple)


def test_page_records_are_frozen() -> None:
    """``PageRecord`` — иммутабельный."""
    import dataclasses

    doc = _StubTextDocument(pages=["text"])
    extractor = _StubTextExtractor(doc)

    plan = build_index_plan(**_make_params(), text_extractor=extractor)
    assert plan is not None

    page: PageRecord = plan.pages[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        page.text = "changed"  # type: ignore[misc]


# =====================================================================
# Раздел 9. Picklability
# =====================================================================


def test_plan_is_picklable() -> None:
    """``DocumentIndexPlan`` сериализуется через pickle.

    Критично для передачи через ``ProcessTaskRunner`` из subprocess
    в родительский процесс.
    """
    import pickle

    doc = _StubTextDocument(pages=["page 1", "page 2"])
    extractor = _StubTextExtractor(doc)

    plan = build_index_plan(**_make_params(), text_extractor=extractor)
    assert plan is not None

    restored = pickle.loads(pickle.dumps(plan))
    assert restored == plan
    assert isinstance(restored, DocumentIndexPlan)
    assert isinstance(restored.pages, tuple)
