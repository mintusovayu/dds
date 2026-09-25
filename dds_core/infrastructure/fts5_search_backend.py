"""
Поисковый бэкенд через SQLite FTS5.

Этот модуль реализует интерфейс ``ISearchBackend`` доменного слоя,
обеспечивая полнотекстовый поиск через SQLite FTS5. Модуль
находится в инфраструктурном слое и является единственной
точкой реализации полнотекстового поиска через FTS5. Доменный слой
не знает о FTS5 — он использует только интерфейс ``ISearchBackend``.

Возможности бэкенда:

+----------------------------------+----------------------------------+
| Возможность                      | Описание                         |
+==================================+==================================+
| ``"basic_search"``               | Полнотекстовый поиск             |
|                                  | (обязательна).                   |
+----------------------------------+----------------------------------+
| ``"count"``                      | Подсчёт результатов              |
|                                  | (обязательна).                   |
+----------------------------------+----------------------------------+
| ``"snippet"``                    | Фрагменты текста в результатах.  |
+----------------------------------+----------------------------------+
| ``"relevance"``                  | Ранжирование по релевантности    |
|                                  | (BM25).                          |
+----------------------------------+----------------------------------+

Серверная группировка результатов:

Поиск возвращает по одной строке на документ. Все страницы
документа с совпадениями агрегируются в массив ``pages``.
Группировка выполняется на Python после **двух последовательных
SQL-запросов**:

1. **Query 1.** Выборка всех ``(doc_id, bm25)`` пар для страниц,
   совпавших с запросом. Без агрегатов в SQL: ``bm25()`` вызывается
   в одном SELECT-блоке с ``MATCH`` (требование FTS5). Группировка
   по ``doc_id`` с вычислением ``MIN(rank)`` и пагинация
   (``offset``/``limit`` по документам) выполняются в Python.
2. **Query 2.** Для выбранных ``doc_id`` выбираются все страницы
   с совпадениями и их сниппеты. Сниппеты извлекаются из
   колонки ``normalized_text`` (индекс 3) и денормализуются
   на Python.

**Почему не агрегат над bm25.** SQLite FTS5 запрещает
использование ``bm25()`` внутри агрегатных функций (``MIN``,
``MAX`` и т.п.) — функция ожидает «сырой» контекст FTS5-курсора,
который разрушается агрегацией. Попытка приводит к ошибке
``unable to use function bm25 in the requested context``.
Поэтому агрегация выполняется на Python.

**Почему не CTE.** Использование CTE с ``bm25()``/``snippet()``,
на который ссылаются дважды, также приводит к потере контекста
FTS5. Двухзапросный подход этой проблемы лишён.

**Research-шаг 3.1 плана рефакторинга v5.0.** Проверялась
возможность применить оптимизацию SQL ``LIMIT`` к Query 1 через
подзапрос с ``GROUP BY doc_id`` и ``MIN(bm25(...))``:

.. code-block:: sql

    SELECT doc_id, MIN(rank) AS best_rank
    FROM (
        SELECT fts.doc_id, bm25(text_index_fts) AS rank
        FROM text_index_fts fts
        JOIN documents d ON fts.doc_id = d.doc_id
        WHERE text_index_fts MATCH ?
    )
    GROUP BY doc_id
    ORDER BY best_rank, doc_id
    LIMIT ? OFFSET ?

Проверка на SQLite 3.40+ (целевая версия) дала ошибку
``unable to use function bm25 in the requested context``: FTS5
требует вызова ``bm25()`` в том же SELECT-блоке, где присутствует
``MATCH``, и не позволяет использовать функцию внутри подзапроса
с агрегацией. Оптимизация **не применяется**; двухзапросный
подход с Python-side агрегацией остаётся оптимальным.

Порядок сортировки:

- Документы: по возрастанию ``best_rank`` (лучшая релевантность),
  затем по ``doc_id`` (детерминированная пагинация).
- Страницы внутри документа: по возрастанию ``page_number``.

FTS5-функции ``bm25()`` и ``snippet()`` принимают **имя FTS5-таблицы**
(``text_index_fts``), а не её алиас. Это документированное поведение
SQLite.

Безопасность (скорректированный план):

+----------------------------------+----------------------------------+
| Мера                             | Описание                         |
+==================================+==================================+
| Нейтральные маркеры подсветки    | Вместо HTML-тегов ``<b>``/``</b>``|
|                                  | используются нейтральные маркеры |
|                                  | из ``config``. Это предотвращает |
|                                  | XSS при выводе сниппетов.        |
+----------------------------------+----------------------------------+
| Валидация идентификаторов        | Имена представлений и полей в    |
|                                  | ``module_filters`` проверяются   |
|                                  | на допустимость. Недопустимые    |
|                                  | идентификаторы вызывают          |
|                                  | ``ValueError``.                  |
+----------------------------------+----------------------------------+
| Обёртывание ошибок FTS5          | ``sqlite3.OperationalError``     |
|                                  | (некорректный синтаксис FTS5)   |
|                                  | оборачивается в ``ValueError``,  |
|                                  | что позволяет API вернуть ``400``|
|                                  | вместо ``500``.                  |
+----------------------------------+----------------------------------+

Обработка пустого запроса:

- Если поисковый запрос пустой (``query == ""``), не используется
  FTS5 MATCH. Вместо этого запрос строится напрямую по таблице
  ``documents`` с учётом всех фильтров. Результаты содержат один
  ``SearchResult`` на документ с одной «виртуальной» страницей
  (``page_number=0``, ``snippet=""``, ``terms=()``). Клиент
  отобразит такие документы без раскрытия.
- Метод ``count`` при пустом запросе также считает документы,
  а не страницы.

Нормализация текста:

Для нечувствительного к раскладке поиска используется колонка
``normalized_text`` в ``text_index_fts``. Пользовательский запрос
преобразуется в FTS5 MATCH-выражение функцией
:func:`~dds_core.infrastructure.fts5.match_builder.normalize_search_query`,
которая объединяет структурный разбор запроса
(:func:`~dds_core.application.query_tokenizer.tokenize_search_query`)
и FTS5-специфичную сборку
(:func:`~dds_core.infrastructure.fts5.match_builder.build_match_expression`).
Сниппеты формируются из ``normalized_text`` (индекс 3) и
денормализуются функцией
:func:`~dds_core.domain.text_normalization.denormalize_text`.

Термины подсветки (Фаза 6, ADR-006):

При формировании каждого :class:`PageHit` сервер извлекает
уникальные термины подсветки из **денормализованного** сниппета
через :func:`~dds_core.application.snippet_terms_extractor.extract_terms_from_snippet`
и сохраняет их в поле ``PageHit.terms``. Клиент получает готовый
список терминов через API и не парсит сниппеты самостоятельно —
это устраняет протечку FTS5-специфики (формат маркеров) в
presentation layer.

Принципы:
- Модуль реализует интерфейс доменного слоя (инверсия зависимостей).
- Модуль является единственной точкой реализации поиска через FTS5.
- Модуль не содержит бизнес-логики индексирования.
- Модуль не выполняет логирование. Логирование — ответственность
  application layer.

Реализуемые интерфейсы:
    ``ISearchBackend`` — абстракция поискового бэкенда.
"""

from __future__ import annotations

import re
import sqlite3

from ..application.metadata_filter_query_builder import MetadataFilterQueryBuilder
from ..application.snippet_terms_extractor import extract_terms_from_snippet
from ..domain import config
from ..domain.interfaces import IDatabase
from ..domain.models import PageHit, SearchFilters, SearchResult
from ..domain.text_normalization import denormalize_text
from .fts5.match_builder import normalize_search_query

# ----------------------------------------------------------------------
# Валидация SQL-идентификаторов
# ----------------------------------------------------------------------

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
"""Регулярное выражение для проверки допустимых SQL-идентификаторов."""


def _validate_identifier(name: str, kind: str) -> None:
    """Проверяет, что строка является допустимым SQL-идентификатором.

    Args:
        name: Проверяемое имя.
        kind: Описание типа идентификатора (для сообщения об ошибке).

    Raises:
        ValueError: Если имя содержит недопустимые символы.
    """
    if not _IDENTIFIER_PATTERN.match(name):
        raise ValueError(
            f"Недопустимый {kind}: '{name}'. Допустимы только буквы, цифры и подчёркивания."
        )


# ----------------------------------------------------------------------
# Поисковый бэкенд FTS5
# ----------------------------------------------------------------------


class FTS5SearchBackend:
    """Поисковый бэкенд через SQLite FTS5.

    Реализует интерфейс ``ISearchBackend``. Использует SQLite FTS5
    для полнотекстового поиска с ранжированием BM25 и фрагментами
    текста. Поддерживает фильтрацию по метаданным имени файла.
    Результаты группируются по документу: одна строка на документ
    с массивом страниц.

    Безопасность сниппетов:
    Вместо HTML-тегов ``<b>``/``</b>`` используются нейтральные
    маркеры из ``config``. Веб-слой экранирует текст и заменяет
    маркеры на безопасные теги.

    Валидация идентификаторов:
    Имена представлений и полей в ``module_filters`` проверяются
    на допустимость перед использованием в SQL.

    Обработка ошибок:
    Ошибки синтаксиса FTS5 оборачиваются в ``ValueError``.

    Фильтрация по метаданным:
    В методах ``search()`` и ``count()`` выполняется ``JOIN``
    с таблицей ``documents``. Условия фильтрации по кодам
    (object_code, discipline_code, document_type_code) и режим
    «только несоответствующие» добавляются через
    :class:`MetadataFilterQueryBuilder`.

    Нормализация раскладки:
    Для непустых запросов используется колонка ``normalized_text``.
    Выражение для MATCH формируется функцией
    :func:`~dds_core.infrastructure.fts5.match_builder.normalize_search_query`.

    Двухзапросный подход:
    ``search()`` выполняет два SQL-запроса:

    1. Плоская выборка ``(doc_id, rank)`` для всех страниц,
       совпавших с запросом. Агрегация ``MIN(rank)`` и пагинация
       выполняются на Python.
    2. Выборка страниц и сниппетов для выбранных ``doc_id``.

    Такой подход гарантирует корректную работу FTS5-функции
    ``bm25()``: в каждом SQL-запросе она вызывается в одном
    SELECT-блоке с ``MATCH``. Оптимизация SQL ``LIMIT`` в Query 1
    (см. research-шаг 3.1 плана v5.0) не применима: FTS5 запрещает
    ``bm25()`` в подзапросах с агрегацией.

    Термины подсветки (Фаза 6):
    При формировании :class:`PageHit` сервер извлекает уникальные
    термины из денормализованного сниппета и сохраняет их в поле
    ``PageHit.terms``. Это устраняет парсинг сниппетов на клиенте
    (см. ADR-006).

    Пример использования::

        backend = FTS5SearchBackend(db_adapter)
        results = backend.search("гидрошпонка", limit=20)
        total = backend.count("гидрошпонка")
        results = backend.search("", filters=SearchFilters(object_code="7350"))

    Attributes:
        _db: Абстракция базы данных.
        _metadata_filter_builder: Построитель условий фильтрации.
    """

    def __init__(self, db: IDatabase) -> None:
        """Инициализирует поисковый бэкенд.

        Args:
            db: Реализация ``IDatabase`` для выполнения SQL-запросов.
        """
        self._db = db
        self._metadata_filter_builder = MetadataFilterQueryBuilder()

    def get_capabilities(self) -> set[str]:
        """Возвращает набор поддерживаемых возможностей."""
        return {"basic_search", "count", "snippet", "relevance"}

    # ------------------------------------------------------------------
    # Публичные методы
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        filters: SearchFilters | None = None,
        limit: int = config.SEARCH_DEFAULT_LIMIT,
        offset: int = 0,
    ) -> list[SearchResult]:
        """Выполняет поиск документов с серверной группировкой.

        Использует двухзапросный подход:

        1. **Query 1** — плоская выборка ``(doc_id, rank)`` для
           всех совпавших страниц. Без агрегатов в SQL: ``bm25()``
           вызывается в одном SELECT-блоке с ``MATCH``.
           Группировка по ``doc_id`` (``MIN(rank)``) и пагинация
           выполняются на Python.
        2. **Query 2** — для выбранных ``doc_id`` выбираются
           страницы и их сниппеты (из колонки ``normalized_text``).

        Порядок документов: по возрастанию ``best_rank``, затем
        по ``doc_id``. Страницы внутри документа — по
        ``page_number``.

        Термины подсветки (Фаза 6):
        Для каждой страницы формируется :class:`PageHit` с полем
        ``terms`` — кортеж уникальных терминов, извлечённых из
        денормализованного сниппета. Клиент использует готовые
        термины без парсинга (см. ADR-006).

        Для пустого запроса вызывается ``_search_documents_only``.

        Args:
            query: Поисковый запрос. Может быть пустой строкой.
            filters: Фильтры поиска. Может быть ``None``.
            limit: Максимальное количество документов.
            offset: Смещение по документам.

        Returns:
            Список ``SearchResult``, по одному на документ.

        Raises:
            ValueError: Если SQL-запрос некорректен.
        """
        query = query.strip() if query else ""

        if not query:
            return self._search_documents_only(filters, limit, offset)

        match_expression = normalize_search_query(query)

        # ── Query 1: плоская выборка (doc_id, rank) без агрегатов ──
        # Порядок параметров:
        #   1. match_expression (в MATCH)
        #   2. ... параметры фильтров
        #
        # Без MIN(bm25(...)): FTS5 запрещает агрегаты над bm25().
        # Агрегация MIN(rank) выполняется на Python.
        q1_params: list[object] = [match_expression]
        filter_join_sql, where_body = self._build_filter_sql(filters, q1_params)
        filter_where_and = (" AND " + where_body) if where_body else ""

        q1_sql = (
            "SELECT fts.doc_id AS doc_id, "
            "       bm25(text_index_fts) AS rank "
            "FROM text_index_fts fts "
            "JOIN documents d ON fts.doc_id = d.doc_id"
            + filter_join_sql
            + " WHERE text_index_fts MATCH ?"
            + filter_where_and
        )

        try:
            rank_rows = self._db.execute(q1_sql, tuple(q1_params))
        except sqlite3.OperationalError as e:
            raise ValueError(f"Некорректный поисковый запрос: {e}") from e

        # Группировка по doc_id: минимальный rank на документ.
        # Порядок обхода строк не важен — сравнение по min().
        doc_best_rank: dict[str, float] = {}
        for row in rank_rows:
            doc_id = str(row[0])
            rank = float(row[1])
            if doc_id not in doc_best_rank or rank < doc_best_rank[doc_id]:
                doc_best_rank[doc_id] = rank

        if not doc_best_rank:
            return []

        # Сортировка: (best_rank, doc_id) и пагинация по документам.
        sorted_docs = sorted(
            doc_best_rank.items(),
            key=lambda item: (item[1], item[0]),
        )
        paginated = sorted_docs[offset : offset + limit]

        if not paginated:
            return []

        doc_ids = [doc_id for doc_id, _ in paginated]
        doc_ranks = dict(paginated)

        # ── Query 2: страницы выбранных документов с сниппетами ──
        # Порядок параметров:
        #   1. SNIPPET_HIGHLIGHT_START  (в snippet)
        #   2. SNIPPET_HIGHLIGHT_END    (в snippet)
        #   3. SEARCH_SNIPPET_LENGTH    (в snippet)
        #   4. match_expression         (в MATCH)
        #   5. ... doc_ids              (в IN)
        placeholders = ", ".join("?" for _ in doc_ids)
        q2_params: list[object] = [
            config.SNIPPET_HIGHLIGHT_START,
            config.SNIPPET_HIGHLIGHT_END,
            config.SEARCH_SNIPPET_LENGTH,
            match_expression,
        ]
        q2_params.extend(doc_ids)

        q2_sql = (
            "SELECT fts.doc_id AS doc_id, "
            "       d.file_path AS file_path, "
            "       fts.page_number AS page_number, "
            "       snippet(text_index_fts, 3, ?, ?, '...', ?) AS snippet "
            "FROM text_index_fts fts "
            "JOIN documents d ON fts.doc_id = d.doc_id "
            "WHERE text_index_fts MATCH ? "
            f"  AND fts.doc_id IN ({placeholders}) "
            "ORDER BY fts.doc_id, fts.page_number"
        )

        try:
            page_rows = self._db.execute(q2_sql, tuple(q2_params))
        except sqlite3.OperationalError as e:
            raise ValueError(f"Некорректный поисковый запрос: {e}") from e

        # ── Группировка страниц по doc_id с дедупликацией ──
        # Дедупликация защищает от JOIN с модульными VIEW,
        # где одна страница документа может породить несколько строк.
        pages_by_doc: dict[str, list[PageHit]] = {}
        file_path_by_doc: dict[str, str] = {}
        seen_pages: dict[str, set[int]] = {}

        for row in page_rows:
            doc_id = str(row[0])
            file_path = str(row[1])
            page_number = int(row[2])
            snippet_raw = str(row[3])

            file_path_by_doc[doc_id] = file_path

            if doc_id not in pages_by_doc:
                pages_by_doc[doc_id] = []
                seen_pages[doc_id] = set()

            if page_number in seen_pages[doc_id]:
                continue
            seen_pages[doc_id].add(page_number)

            # Денормализация сниппета: клиент получает читаемый
            # кириллический текст (не нормализованный).
            denormalized_snippet = denormalize_text(snippet_raw)

            # Термины подсветки извлекаются из денормализованного
            # сниппета — в той же форме, что отображается
            # пользователю в подсказках (см. ADR-006).
            page_terms = extract_terms_from_snippet(denormalized_snippet)

            pages_by_doc[doc_id].append(
                PageHit(
                    page_number=page_number,
                    snippet=denormalized_snippet,
                    terms=page_terms,
                )
            )

        # ── Построение результатов в порядке отобранных doc_id ──
        results: list[SearchResult] = []
        for doc_id in doc_ids:
            pages = pages_by_doc.get(doc_id)
            if not pages:
                # Документ выбран в Query 1, но в Query 2 не оказалось
                # страниц. Это маловероятно (MATCH одинаковый), но
                # защищаемся от пустых результатов.
                continue
            results.append(
                SearchResult(
                    doc_id=doc_id,
                    file_path=file_path_by_doc.get(doc_id, ""),
                    pages=pages,
                    relevance_score=doc_ranks.get(doc_id, 0.0),
                )
            )

        return results

    def count(
        self,
        query: str,
        filters: SearchFilters | None = None,
    ) -> int:
        """Подсчитывает количество уникальных документов.

        При пустом ``query`` делегирует в ``_count_documents_only``.
        При непустом — возвращает количество уникальных ``doc_id``,
        содержащих совпадения, с учётом фильтров.

        Args:
            query: Поисковый запрос. Может быть пустой строкой.
            filters: Фильтры поиска. Может быть ``None``.

        Returns:
            Количество уникальных документов.

        Raises:
            ValueError: Если SQL-запрос некорректен.
        """
        query = query.strip() if query else ""

        if not query:
            return self._count_documents_only(filters)

        match_expression = normalize_search_query(query)

        # Порядок параметров:
        #   1. match_expression
        #   2. ... параметры фильтров
        params: list[object] = [match_expression]

        filter_join_sql, where_body = self._build_filter_sql(filters, params)
        filter_where_and = (" AND " + where_body) if where_body else ""

        sql = (
            "SELECT COUNT(DISTINCT fts.doc_id) "
            "FROM text_index_fts fts "
            "JOIN documents d ON fts.doc_id = d.doc_id"
            + filter_join_sql
            + " WHERE text_index_fts MATCH ?"
            + filter_where_and
        )

        try:
            rows = self._db.execute(sql, tuple(params))
        except sqlite3.OperationalError as e:
            raise ValueError(f"Некорректный поисковый запрос: {e}") from e

        return int(rows[0][0]) if rows else 0

    # ------------------------------------------------------------------
    # Методы для пустого запроса
    # ------------------------------------------------------------------

    def _search_documents_only(
        self,
        filters: SearchFilters | None,
        limit: int,
        offset: int,
    ) -> list[SearchResult]:
        """Возвращает документы по фильтрам без MATCH.

        Использует ``SELECT DISTINCT`` — защита от дубликатов
        документов при JOIN с модульными VIEW (когда одна строка
        документа порождает несколько строк в VIEW).

        Каждый результат содержит одну «виртуальную» страницу
        (``page_number=0``, пустой ``snippet``, пустой ``terms``),
        чтобы структура :class:`SearchResult` оставалась единой
        для обоих режимов. Клиент отобразит такие документы
        без кнопки раскрытия (``pages.length == 1``).

        Args:
            filters: Фильтры поиска.
            limit: Максимальное количество документов.
            offset: Смещение по документам.

        Returns:
            Список ``SearchResult`` — по одному на документ.

        Raises:
            ValueError: Если SQL-запрос некорректен.
        """
        params: list[object] = []
        filter_join_sql, where_body = self._build_filter_sql(filters, params)
        filter_where = (" WHERE " + where_body) if where_body else ""

        params.append(limit)
        params.append(offset)

        sql = (
            "SELECT DISTINCT d.doc_id, d.file_path "
            "FROM documents d"
            + filter_join_sql
            + filter_where
            + " ORDER BY d.file_path LIMIT ? OFFSET ?"
        )

        try:
            rows = self._db.execute(sql, tuple(params))
        except sqlite3.OperationalError as e:
            raise ValueError(f"Некорректный запрос: {e}") from e

        results: list[SearchResult] = []
        for row in rows:
            results.append(
                SearchResult(
                    doc_id=str(row[0]),
                    file_path=str(row[1]),
                    pages=[PageHit(page_number=0, snippet="")],
                    relevance_score=0.0,
                )
            )
        return results

    def _count_documents_only(
        self,
        filters: SearchFilters | None,
    ) -> int:
        """Подсчитывает количество документов по фильтрам без MATCH.

        Использует ``COUNT(DISTINCT d.doc_id)`` — защита от
        завышения при JOIN с модульными VIEW.

        Args:
            filters: Фильтры поиска.

        Returns:
            Количество уникальных документов.

        Raises:
            ValueError: Если SQL-запрос некорректен.
        """
        params: list[object] = []
        filter_join_sql, where_body = self._build_filter_sql(filters, params)
        filter_where = (" WHERE " + where_body) if where_body else ""

        sql = "SELECT COUNT(DISTINCT d.doc_id) FROM documents d" + filter_join_sql + filter_where

        try:
            rows = self._db.execute(sql, tuple(params))
        except sqlite3.OperationalError as e:
            raise ValueError(f"Некорректный запрос: {e}") from e

        return int(rows[0][0]) if rows else 0

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _build_filter_sql(
        self,
        filters: SearchFilters | None,
        params: list[object],
    ) -> tuple[str, str]:
        """Формирует SQL-фрагменты фильтрации.

        Побочный эффект: дополняет ``params`` параметрами фильтров
        в порядке, соответствующем порядку ``?`` в возвращаемых
        фрагментах. Порядок параметров совпадает с порядком,
        в котором :meth:`_add_filter_conditions` добавляет условия.

        Метод следует вызывать ровно один раз на один список
        ``params`` — повторный вызов задвоит параметры.

        Args:
            filters: Объект фильтров. Может быть ``None``.
            params: Список параметров SQL. Будет дополнен.

        Returns:
            Кортеж ``(join_sql, where_body)``:

            - ``join_sql`` — строка с ведущим пробелом
              (например, ``" JOIN view mv ON d.doc_id = mv.doc_id"``)
              или пустая строка, если фильтров нет.
            - ``where_body`` — условия WHERE, объединённые ``" AND "``,
              без префикса ``" AND "`` и без ключевого слова ``WHERE``.
              Пустая строка, если условий нет.
        """
        join_parts: list[str] = []
        where_parts: list[str] = []

        if filters is not None:
            self._add_filter_conditions(
                filters=filters,
                documents_alias="d",
                source_alias="d",
                join_parts=join_parts,
                where_parts=where_parts,
                params=params,
            )

        join_sql = (" " + " ".join(join_parts)) if join_parts else ""
        where_body = " AND ".join(where_parts) if where_parts else ""
        return join_sql, where_body

    def _add_filter_conditions(
        self,
        filters: SearchFilters,
        documents_alias: str,
        source_alias: str,
        join_parts: list[str],
        where_parts: list[str],
        params: list[object],
    ) -> None:
        """Добавляет условия фильтрации в списки SQL-частей.

        Объединяет логику фильтрации по пути файла, модульным
        представлениям и метаданным документа. Используется
        в :meth:`_build_filter_sql`, который вызывается всеми
        публичными и приватными методами поиска.

        Побочный эффект: дополняет ``params`` параметрами фильтров
        в порядке, соответствующем порядку ``?`` в
        ``join_parts``/``where_parts``.

        Args:
            filters: Объект фильтров.
            documents_alias: Алиас таблицы документов (всегда ``d``).
            source_alias: Алиас источника записей, к которому
                присоединяются модульные VIEW. В текущей реализации
                всегда ``d`` (унификация).
            join_parts: Список JOIN-частей SQL (будет изменён).
            where_parts: Список условий WHERE (будет изменён).
            params: Список параметров (будет изменён).
        """
        if filters.file_path_pattern is not None:
            where_parts.append(f"{documents_alias}.file_path LIKE ?")
            params.append(filters.file_path_pattern)

        if filters.module_filters:
            for view_name, conditions in filters.module_filters.items():
                _validate_identifier(view_name, "имя представления")
                if not self._db.table_exists(view_name):
                    continue
                join_parts.append(f"JOIN {view_name} mv ON {source_alias}.doc_id = mv.doc_id")
                for field_name, field_value in conditions.items():
                    _validate_identifier(field_name, "имя поля")
                    where_parts.append(f"mv.{field_name} = ?")
                    params.append(field_value)

        metadata_conditions, metadata_params = self._metadata_filter_builder.build(filters)
        where_parts.extend(metadata_conditions)
        params.extend(metadata_params)
