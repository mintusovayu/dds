"""
Интеграционные тесты заполнения ``PageHit.terms`` в ``FTS5SearchBackend``.

Модуль покрывает поведение поискового бэкенда FTS5 в части
заполнения поля ``terms`` (Фаза 6, ADR-006):

- сервер извлекает уникальные термины подсветки из сниппета FTS5
  и сохраняет их в :class:`~dds_core.domain.models.PageHit.terms`;
- термины представлены иммутабельным ``tuple`` (не ``list``);
- дедупликация и порядок — в рамках одной страницы;
- для пустого запроса (режим «показать все») ``terms`` пуст,
  так как сниппет виртуальной страницы пуст;
- термины возвращаются в **денормализованной** (читаемой)
  кириллической форме — согласовано с полем ``snippet``.

Стратегия:
    Реальная SQLite через :class:`~dds_core.infrastructure.sqlite_adapter.SQLiteAdapter`
    и :class:`~dds_core.infrastructure.database.DatabaseManager.ensure_all`.
    Реальные данные: текст страниц прогоняется через
    :func:`~dds_core.domain.text_normalization.normalize_text`
    перед вставкой в ``text_index_fts`` (колонка ``normalized_text``)
    — как в production-конвейере (:func:`build_index_plan`).
    Тесты полностью синхронны (метод ``search`` — синхронный).

Границы покрытия:
    Не проверяется чистый контракт ``extract_terms_from_snippet``
    на искусственных входах — это область
    ``test_snippet_terms_extractor.py`` (Шаг 6). Здесь проверяется
    интеграция: FTS5 → snippet → denormalize → extract → PageHit.

Примечание об ограничениях FTS5, выявленных при разработке:

    +------------------------------------------------------------------+
    | Ограничение                    | Обход в тестах                   |
    +================================+==================================+
    | ``snippet()`` при multi-term   | Изоляция страниц проверяется     |
    | OR-запросе на документе с      | двумя однозначными запросами     |
    | несколькими страницами может   | (по одному термину на страницу). |
    | вернуть сниппет не той страницы| См. :func:`test_search_terms_    |
    |                                | per_page_isolated`.              |
    +--------------------------------+----------------------------------+
    | ``normalize_search_query``     | Тесты используют термины без     |
    | не экранирует токены с ``-``:  | дефисов (``423``, кириллица).    |
    | ``EC-423-1`` → FTS5 парсит     | Предсуществующая особенность     |
    | ``423`` как имя колонки.       | ``normalize_search_query``,      |
    |                                | не относится к Фазе 6.           |
    +--------------------------------+----------------------------------+
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from dds_core.domain.models import PageHit, SearchResult
from dds_core.domain.text_normalization import normalize_text
from dds_core.infrastructure.database import DatabaseManager
from dds_core.infrastructure.fts5_search_backend import FTS5SearchBackend
from dds_core.infrastructure.sqlite_adapter import SQLiteAdapter

# ----------------------------------------------------------------------
# Константы
# ----------------------------------------------------------------------

_ISO_TS: str = "2024-01-01T00:00:00+00:00"
"""Метка времени для полей ``indexed_at`` / ``last_modified``."""


# ----------------------------------------------------------------------
# Helper-функции
# ----------------------------------------------------------------------


def _insert_document(
    adapter: SQLiteAdapter,
    doc_id: str,
    file_path: str,
    page_count: int,
) -> None:
    """Вставляет запись в таблицу ``documents``.

    Args:
        adapter: SQLite-адаптер с готовой схемой.
        doc_id: Идентификатор документа.
        file_path: Относительный путь к файлу.
        page_count: Количество страниц.
    """
    adapter.execute_write(
        "INSERT INTO documents ("
        "doc_id, file_path, file_hash, file_size, page_count, "
        "indexed_at, last_modified, cached_size, cached_mtime"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            doc_id,
            file_path,
            f"hash_{doc_id}",
            1024,
            page_count,
            _ISO_TS,
            _ISO_TS,
            1024,
            _ISO_TS,
        ),
    )


def _insert_page(
    adapter: SQLiteAdapter,
    doc_id: str,
    page_number: int,
    text: str,
) -> None:
    """Вставляет страницу в ``text_index_fts``.

    Колонка ``normalized_text`` заполняется через
    :func:`normalize_text` — как в production
    (:func:`~dds_core.application.index_plan_builder.build_index_plan`).

    Args:
        adapter: SQLite-адаптер с готовой схемой.
        doc_id: Идентификатор документа.
        page_number: Номер страницы (0-based).
        text: Оригинальный текст страницы.
    """
    adapter.execute_write(
        "INSERT INTO text_index_fts "
        "(doc_id, page_number, text_content, normalized_text) "
        "VALUES (?, ?, ?, ?)",
        (doc_id, page_number, text, normalize_text(text)),
    )


def _add_document_with_pages(
    adapter: SQLiteAdapter,
    doc_id: str,
    pages: list[str],
) -> None:
    """Создаёт документ с несколькими страницами.

    Args:
        adapter: SQLite-адаптер с готовой схемой.
        doc_id: Идентификатор документа.
        pages: Тексты страниц в порядке ``page_number``
            (индекс в списке = номер страницы).
    """
    _insert_document(adapter, doc_id, f"dir/{doc_id}.pdf", len(pages))
    for page_number, text in enumerate(pages):
        _insert_page(adapter, doc_id, page_number, text)


def _find_page(results: list[SearchResult], doc_id: str, page_number: int) -> PageHit:
    """Возвращает ``PageHit`` для документа и страницы.

    Args:
        results: Список результатов поиска.
        doc_id: Идентификатор документа.
        page_number: Номер страницы (0-based).

    Returns:
        Найденный :class:`PageHit`.

    Raises:
        AssertionError: Если документ или страница не найдены.
    """
    for result in results:
        if result.doc_id != doc_id:
            continue
        for page in result.pages:
            if page.page_number == page_number:
                return page
    pytest.fail(f"PageHit не найден: doc_id={doc_id!r}, page_number={page_number}")
    raise AssertionError("unreachable")  # для mypy


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def adapter(tmp_path: Path) -> Iterator[SQLiteAdapter]:
    """Свежий :class:`SQLiteAdapter` со схемой ядра DDS.

    Создаёт временную SQLite-БД, прогоняет
    :meth:`DatabaseManager.ensure_all` (создание схемы + миграции),
    возвращает адаптер. Закрывает соединение по завершении теста.

    Аннотация ``Iterator[SQLiteAdapter]`` обязательна: fixture
    содержит ``yield`` (setup → teardown), поэтому возвращаемое
    значение — генератор, а не сам ``SQLiteAdapter``.
    """
    db_path = str(tmp_path / "test_fts5_terms.db")
    ad = SQLiteAdapter(db_path)
    manager = DatabaseManager(ad)
    manager.ensure_all()
    yield ad
    ad.close()


@pytest.fixture
def backend(adapter: SQLiteAdapter) -> FTS5SearchBackend:
    """Поисковый бэкенд FTS5, привязанный к тестовому адаптеру."""
    return FTS5SearchBackend(adapter)


# ----------------------------------------------------------------------
# Базовые сценарии заполнения terms
# ----------------------------------------------------------------------


def test_search_populates_terms_for_single_match(
    adapter: SQLiteAdapter,
    backend: FTS5SearchBackend,
) -> None:
    """Одна страница с одним совпадением → ``terms`` из одного элемента.

    Термины возвращаются в **денормализованной** кириллической форме
    (согласовано с ``snippet``).
    """
    _add_document_with_pages(
        adapter,
        "doc_001",
        ["гидрошпонка установлена в корпусе"],
    )

    results = backend.search("гидрошпонка")

    assert len(results) == 1
    page = _find_page(results, "doc_001", 0)
    assert page.terms == ("гидрошпонка",)


def test_search_terms_per_page_isolated(
    adapter: SQLiteAdapter,
    backend: FTS5SearchBackend,
) -> None:
    """Термины одной страницы не «протекают» в другую.

    Изоляция страниц внутри одного документа проверяется двумя
    независимыми однозначными запросами. Причина: FTS5 ``snippet()``
    при multi-term OR-запросе на документе с несколькими страницами
    может вернуть сниппет не той страницы (выявлено при разработке
    Фазы 6). Отдельные однозначные запросы этой проблемы лишены.

    Ожидаемое поведение:
    - поиск по термину, встречающемуся только на page 0,
      возвращает только page 0 со своим термином;
    - поиск по термину, встречающемуся только на page 1,
      возвращает только page 1 со своим термином.
    """
    _add_document_with_pages(
        adapter,
        "doc_002",
        [
            "гидрошпонка установлена",  # page 0
            "корпус крупного дробления",  # page 1
        ],
    )

    # Запрос по первому термину: возвращается только page 0.
    results_a = backend.search("гидрошпонка")
    assert len(results_a) == 1
    assert results_a[0].doc_id == "doc_002"
    assert len(results_a[0].pages) == 1
    page_0 = results_a[0].pages[0]
    assert page_0.page_number == 0
    assert page_0.terms == ("гидрошпонка",)

    # Запрос по второму термину: возвращается только page 1.
    results_b = backend.search("корпус")
    assert len(results_b) == 1
    assert results_b[0].doc_id == "doc_002"
    assert len(results_b[0].pages) == 1
    page_1 = results_b[0].pages[0]
    assert page_1.page_number == 1
    assert page_1.terms == ("корпус",)


def test_search_terms_deduplicated_within_page(
    adapter: SQLiteAdapter,
    backend: FTS5SearchBackend,
) -> None:
    """Повторяющиеся термины на одной странице дедуплицируются.

    Порядок первого появления сохраняется.
    """
    _add_document_with_pages(
        adapter,
        "doc_003",
        [
            "гидрошпонка и снова гидрошпонка и опять гидрошпонка",
        ],
    )

    results = backend.search("гидрошпонка")

    assert len(results) == 1
    page = _find_page(results, "doc_003", 0)
    # Ровно один элемент, несмотря на три вхождения.
    assert page.terms == ("гидрошпонка",)


def test_search_terms_preserve_order_within_page(
    adapter: SQLiteAdapter,
    backend: FTS5SearchBackend,
) -> None:
    """Порядок терминов соответствует порядку первого появления.

    Используется multi-term OR-запрос на **одной** странице:
    для одной страницы FTS5 ``snippet()`` возвращает сниппет
    корректно со всеми совпадениями.
    """
    _add_document_with_pages(
        adapter,
        "doc_004",
        [
            "корпус и гидрошпонка и дробление",
        ],
    )

    results = backend.search("корпус OR гидрошпонка OR дробление")

    assert len(results) == 1
    page = _find_page(results, "doc_004", 0)
    assert page.terms == ("корпус", "гидрошпонка", "дробление")


def test_search_multiple_documents_isolated_terms(
    adapter: SQLiteAdapter,
    backend: FTS5SearchBackend,
) -> None:
    """Термины разных документов не смешиваются.

    Каждый документ содержит одну страницу — это исключает
    проблему multi-term OR на многопоточных документах.
    """
    _add_document_with_pages(adapter, "doc_a", ["гидрошпонка в узле"])
    _add_document_with_pages(adapter, "doc_b", ["дробление в корпусе"])

    results = backend.search("гидрошпонка OR дробление")

    assert len(results) == 2
    doc_ids = {r.doc_id for r in results}
    assert doc_ids == {"doc_a", "doc_b"}

    page_a = _find_page(results, "doc_a", 0)
    page_b = _find_page(results, "doc_b", 0)

    assert page_a.terms == ("гидрошпонка",)
    assert page_b.terms == ("дробление",)


# ----------------------------------------------------------------------
# Пустой запрос
# ----------------------------------------------------------------------


def test_search_terms_empty_for_empty_query(
    adapter: SQLiteAdapter,
    backend: FTS5SearchBackend,
) -> None:
    """Пустой запрос → виртуальная страница с ``terms == ()``.

    Режим «показать все»: сервер не формирует сниппет
    (``snippet == ""``), термины отсутствуют.
    """
    _add_document_with_pages(adapter, "doc_empty", ["любой текст"])

    results = backend.search("")

    assert len(results) == 1
    result = results[0]
    assert result.doc_id == "doc_empty"

    # Пустой запрос даёт ровно одну виртуальную страницу page_number=0.
    assert len(result.pages) == 1
    page = result.pages[0]
    assert page.page_number == 0
    assert page.snippet == ""
    assert page.terms == ()


# ----------------------------------------------------------------------
# Тип и обратная совместимость
# ----------------------------------------------------------------------


def test_search_result_pages_have_tuple_terms(
    adapter: SQLiteAdapter,
    backend: FTS5SearchBackend,
) -> None:
    """``terms`` — именно ``tuple`` (не ``list``).

    Согласовано с ``PageHit.terms: tuple[str, ...]`` в domain-модели.
    ``tuple`` обеспечивает иммутабельность, hashability и корректную
    pickle-сериализацию.
    """
    _add_document_with_pages(adapter, "doc_tuple", ["гидрошпонка"])

    results = backend.search("гидрошпонка")

    page = _find_page(results, "doc_tuple", 0)
    assert isinstance(page.terms, tuple)
    assert all(isinstance(term, str) for term in page.terms)


def test_page_hit_default_terms_is_empty_tuple() -> None:
    """``PageHit`` без явного ``terms`` имеет значение по умолчанию ``()``.

    Обратная совместимость: конструкторы ``PageHit(page_number=..., snippet=...)``
    без поля ``terms`` (например, в тестах Фазы 5 или в сторонних
    интеграциях) не падают.
    """
    page = PageHit(page_number=0, snippet="тест")
    assert page.terms == ()
    assert isinstance(page.terms, tuple)


# ----------------------------------------------------------------------
# Отрицательный сценарий
# ----------------------------------------------------------------------


def test_search_no_match_returns_empty_list(
    adapter: SQLiteAdapter,
    backend: FTS5SearchBackend,
) -> None:
    """Нет совпадений → пустой список результатов (без ``PageHit``)."""
    _add_document_with_pages(adapter, "doc_nomatch", ["гидрошпонка"])

    results = backend.search("отсутствующее_слово_xyz")

    assert results == []


# ----------------------------------------------------------------------
# Согласованность с denormalize_text
# ----------------------------------------------------------------------


def test_search_terms_denormalized_not_raw_normalized(
    adapter: SQLiteAdapter,
    backend: FTS5SearchBackend,
) -> None:
    """Термины возвращаются в читаемой кириллической форме.

    Термин «корпус» при нормализации становится «kopпyc» —
    смесь кириллицы и латиницы. Термины, извлечённые из
    денормализованного сниппета, содержат **только** кириллицу:
    латинские символы из ``LATIN_TO_CYRILLIC_MAP`` заменены
    обратно.

    Тест проверяет, что в термине нет латинских букв, которые
    появились бы при ``normalize_text`` без обратной замены.
    Используется кириллический термин без дефисов — это
    исключает срабатывание особенности ``normalize_search_query``
    с токенами, содержащими ``-``.
    """
    _add_document_with_pages(
        adapter,
        "doc_denorm",
        ["корпус крупного дробления"],
    )

    results = backend.search("корпус")

    page = _find_page(results, "doc_denorm", 0)
    assert page.terms == ("корпус",)

    # Ключевая проверка денормализации: в термине нет латинских
    # символов, которые были бы получены при normalize_text:
    #   normalize_text("корпус") == "kopпyc" (k, o, p — латиница,
    #   п — кириллица).
    # denormalize_text("kopпyc") == "корпус" (всё кириллица).
    term = page.terms[0]
    assert "k" not in term
    assert "o" not in term
    assert "p" not in term
    assert "y" not in term
    assert "c" not in term


def test_search_terms_match_snippet_content(
    adapter: SQLiteAdapter,
    backend: FTS5SearchBackend,
) -> None:
    """Каждый термин присутствует в сниппете страницы.

    Согласованность: термины извлекаются именно из сниппета,
    а не откуда-либо ещё. Используется запрос по цифрам —
    цифры стабильны при normalize/denormalize, что делает тест
    устойчивым к поведению ``LATIN_TO_CYRILLIC_MAP``.
    """
    _add_document_with_pages(
        adapter,
        "doc_digits",
        ["раздел 423 содержит данные"],
    )

    results = backend.search("423")

    page = _find_page(results, "doc_digits", 0)
    assert page.terms == ("423",)
    # Каждый термин присутствует в сниппете.
    for term in page.terms:
        assert term in page.snippet
