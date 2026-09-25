"""
Быстрые бенчмарки производительности DDS.

Назначение
----------
Замер времени выполнения критичных операций ядра DDS, которые
укладываются в секунды: поиск через FTS5, рендер страниц PDF
(A4/A1 @ 300 DPI), построение WordIndex, поиск совпадений для
подсветки, fork + IPC round-trip через ProcessTaskRunner.

Результаты сравниваются с baseline в CI (``bench.yml``) через
Mann-Whitney U test. Порог срабатывания: WARNING при деградации
медианы > 15% (p < 0.05), FAIL при > 30%.

Метрики (имена соответствуют ключам в ``baseline.json``)
--------------------------------------------------------

+--------------------------+----------------------------------------+
| Метрика                  | Что измеряет                           |
+==========================+========================================+
| ``test_search_query``    | Один FTS5-запрос по 50K страниц.       |
+--------------------------+----------------------------------------+
| ``test_render_page_a4``  | PNG-рендер A4 @ 300 DPI.               |
+--------------------------+----------------------------------------+
| ``test_render_page_a1``  | PNG-рендер A1 @ 300 DPI.               |
+--------------------------+----------------------------------------+
| ``test_build_word_index_small`` | WordIndex для страницы < 100 слов.|
+--------------------------+----------------------------------------+
| ``test_highlights_500_words`` | Поиск 5 терминов на странице     |
|                          | с 500 словами.                         |
+--------------------------+----------------------------------------+
| ``test_fork_latency``    | Проход ProcessTaskRunner.run: fork +  |
|                          | IPC + Queue round-trip.                |
+--------------------------+----------------------------------------+

Датасеты
--------
- **Search DB**: 5000 документов × 10 страниц = 50000 FTS5-записей.
  Генерируется через direct ``sqlite3.executemany`` в одной
  транзакции — быстро и детерминированно. Количество настраивается
  через ``DDS_BENCH_SEARCH_DOCS`` / ``DDS_BENCH_SEARCH_PAGES_PER_DOC``.
- **PDF-файлы**: генерируются на лету через PyMuPDF в
  ``tmp_path_factory``. Параметры (размер страницы, количество слов,
  seed) фиксированы; содержимое детерминировано.
- **PDF-файлы НЕ коммитятся** в репозиторий — экономия ~10 МБ.

Язык корпуса — латиница
-----------------------
Все текстовые данные (слова для PDF и FTS5) — латинские. Причины:

1. **Встроенный шрифт PyMuPDF.** ``page.insert_text`` без явного
   ``fontname`` использует Helvetica (base-14 PDF font), которая
   поддерживает Latin-1, но не кириллицу. При попытке вставить
   кириллицу глифы отсутствуют, и в текстовый слой PDF попадают
   placeholder-символы. WordIndex строится по этим placeholder'ам,
   термины подсветки не находятся.
2. **Содержание бенчмарка не важно.** Замеряется работа FTS5,
   рендера и IPC — они не зависят от языка. Важна только
   детерминированность и попадание в те же code paths.
3. **Нормализация всё равно применяется.** ``normalize_text``
   вызывается на обеих сторонах (индексация и запрос), чтобы
   fixture работала по production-пути. Для латиницы функция
   идемпотентна (понижение регистра), поэтому нормализованная
   форма совпадает с оригиналом — это корректно.

Кириллическая нормализация покрыта отдельными unit-тестами
(``tests/test_text_normalization_domain.py``,
``tests/test_fts5_match_builder.py``) и интеграционными тестами
(``tests/test_query_tokenizer.py``). Бенчмарк измеряет производительность,
а не корректность нормализации.

Методология замеров
-------------------
Все бенчмарки используют ``benchmark(func)`` в auto-calibrate-режиме.
Управление через CLI-флаги workflow:

- ``--benchmark-min-rounds=10`` — минимум 10 прогонов;
- ``--benchmark-max-time=5`` — авто-масштабирование до 5 секунд;
- ``--benchmark-warmup=on`` — прогрев перед замерами;
- ``--benchmark-disable-gc`` — отключить GC во время замера.

Запуск локально::

    pytest tests/benchmarks/bench_fast.py \\
        --benchmark-min-rounds=10 \\
        --benchmark-warmup=on \\
        --benchmark-disable-gc

Ограничения и допущения
-----------------------
1. **Все ресурсы — временные.** Никаких записей в production-БД
   или ``rd_directory``; используется ``tmp_path_factory``.
2. **Детерминизм датасетов.** Random с фиксированным seed; параметры
   генерации передаются через env-переменные ``DDS_BENCH_*``.
3. **Никаких внешних сервисов.** Все зависимости — локальные.
4. **Session-scoped fixtures.** Дорогостоящая подготовка (schema
   + 50K записей, генерация PDF, построение WordIndex) выполняется
   один раз на всю сессию. Каждая метрика получает чистые входные
   данные.
5. **Async fork_latency**: используется session-scoped event loop,
   потому что ``asyncio.Semaphore`` внутри ``ProcessTaskRunner``
   привязывается к первому loop'у; создание нового loop'а для
   каждого вызова привело бы к ``RuntimeError`` на второй итерации.
6. **``len`` как picklable-функция для fork_latency.** Встроенная
   функция всегда импортируема в дочернем процессе (в отличие от
   функций, определённых в этом модуле, которые требуют
   корректного ``__init__.py`` во всех родительских пакетах).

Зависимости
-----------
- ``pytest-benchmark>=4.0.0`` — инфраструктура замеров;
- ``pymupdf>=1.23.0`` — генерация PDF и рендер;
- ``dds_core`` — тестируемые компоненты.

Принципы:
    - Модуль не выполняет логирования (это ответственность CI);
    - модуль не содержит assert'ов бизнес-логики;
    - assert'ы допустимы только для валидации setup;
    - модуль не читает и не пишет production-файлы.
"""

from __future__ import annotations

import asyncio
import os
import random
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pymupdf
import pytest
from dds_core.application.highlights_service import HighlightsService
from dds_core.application.search_engine import SearchEngine
from dds_core.domain import config as core_config
from dds_core.domain.models import WordIndex
from dds_core.domain.text_normalization import normalize_text
from dds_core.infrastructure.database import DatabaseManager
from dds_core.infrastructure.fts5_search_backend import FTS5SearchBackend
from dds_core.infrastructure.process_task_runner import ProcessTaskRunner
from dds_core.infrastructure.pymupdf_text_extractor import PyMuPDFTextExtractor
from dds_core.infrastructure.sqlite_adapter import SQLiteAdapter
from dds_core.infrastructure.sqlite_document_repository import (
    SqliteDocumentRepository,
)

# =====================================================================
# Константы
# =====================================================================

_SEARCH_DOCS_DEFAULT = 5000
"""Количество документов в датасете поиска по умолчанию."""

_SEARCH_PAGES_PER_DOC_DEFAULT = 10
"""Количество страниц на документ в датасете поиска по умолчанию."""

_SEARCH_QUERY = "corpus"
"""Поисковый запрос для бенчмарка search.query.

Выбран так, чтобы:
    - встречаться в генерируемом тексте (гарантированный non-empty
      результат);
    - не быть слишком общим (иначе BM25 даст низкую избирательность);
    - не быть слишком редким (иначе search завершится мгновенно).
"""

_HIGHLIGHTS_TERMS: tuple[str, ...] = (
    "corpus",
    "sealant",
    "crushing",
    "foundation",
    "insulation",
)
"""Термины для бенчмарка highlights. Все присутствуют в тексте
генерируемых страниц."""

_HIGHLIGHTS_PAGE_WORDS = 500
"""Количество слов на странице для бенчмарка highlights."""

_SMALL_PAGE_WORDS = 50
"""Количество слов на странице для бенчмарка build_word_index_small."""

_MEDIUM_WORDS = (
    # Набор слов, среди которых есть все термины из _HIGHLIGHTS_TERMS.
    # Служит источником текста для PDF-генерации и FTS5-записей.
    # Латиница — см. раздел «Язык корпуса — латиница» в модульном
    # docstring.
    "corpus",
    "sealant",
    "crushing",
    "foundation",
    "insulation",
    "mounting",
    "scheme",
    "node",
    "section",
    "cutaway",
    "plan",
    "project",
    "drawing",
    "spec",
    "register",
)

_PDF_GENERATION_SEED = 42
"""Seed для random при генерации PDF. Фиксирован для детерминизма."""

_TEXT_GENERATION_SEED = 42
"""Seed для random при генерации текстовых данных для FTS5."""

_FORK_LATENCY_TIMEOUT = 10.0
"""Таймаут для ``ProcessTaskRunner.run`` в бенчмарке fork_latency.

Достаточно велик, чтобы forkserver успел стартовать (первый fork
медленный, ~200–500 мс на холодную) и задача выполнилась. Если
тест превышает этот таймаут — это баг, а не медленная машина.
"""

_FORK_LATENCY_PAYLOAD = "hello"
"""Аргумент для ``len`` в бенчмарке fork_latency.

Минимальный pickle-пейлоад: короткая строка. Pickle overhead не
доминирует над fork, но остаётся ненулевым — как в production.
"""


# =====================================================================
# Утилиты генерации
# =====================================================================


def _make_sentence(rng: random.Random, words_count: int) -> str:
    """Генерирует строку из ``words_count`` случайных слов.

    Слова берутся из :data:`_MEDIUM_WORDS` с равномерным
    распределением. Используется для заполнения PDF-страниц
    и FTS5-записей детерминированным текстом.

    Args:
        rng: Экземпляр ``random.Random`` с зафиксированным seed.
        words_count: Количество слов в строке.

    Returns:
        Строка из слов, разделённых пробелами.
    """
    return " ".join(rng.choice(_MEDIUM_WORDS) for _ in range(words_count))


def _generate_pdf(
    path: Path,
    width_pt: float,
    height_pt: float,
    words: int,
    seed: int,
) -> None:
    """Создаёт PDF-файл с одной страницей заданного размера.

    Текст размещается сверху страницы блоками по ~10 слов на строку;
    координаты фиксированы, чтобы структура PDF (block_no / line_no)
    была стабильна между запусками. Используется встроенный шрифт
    Helvetica (base-14 PDF font), поддерживающий латиницу. См.
    раздел «Язык корпуса — латиница» в модульном docstring.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Создание нового PDF-документа.                      |
    +---+-----------------------------------------------------+
    | 2 | Добавление страницы заданного размера.              |
    +---+-----------------------------------------------------+
    | 3 | Генерация слов через :func:`_make_sentence`.        |
    +---+-----------------------------------------------------+
    | 4 | Разбиение на строки по ~10 слов (вставка через       |
    |   | ``insert_text``).                                   |
    +---+-----------------------------------------------------+
    | 5 | Сохранение файла на диск.                           |
    +---+-----------------------------------------------------+

    Args:
        path: Путь для сохранения PDF.
        width_pt: Ширина страницы в PDF-points.
        height_pt: Высота страницы в PDF-points.
        words: Количество слов на странице.
        seed: Seed для random.
    """
    rng = random.Random(seed)
    text = _make_sentence(rng, words)

    doc = pymupdf.open()
    page = doc.new_page(width=width_pt, height=height_pt)

    # Разбиение на строки по ~10 слов. Координаты фиксированы,
    # чтобы структура PDF (block_no / line_no) была предсказуемой.
    tokens = text.split()
    line_size = 10
    x0 = 40.0
    y0 = 60.0
    line_height = 12.0
    max_y = height_pt - 40.0
    y = y0
    for i in range(0, len(tokens), line_size):
        if y > max_y:
            break
        line = " ".join(tokens[i : i + line_size])
        page.insert_text((x0, y), line, fontsize=9)
        y += line_height

    doc.save(str(path))
    doc.close()


def _bulk_populate_search_db(db_path: Path) -> None:
    """Наполняет search-БД записями через bulk-insert.

    Использует прямое соединение ``sqlite3`` и ``executemany`` —
    это в разы быстрее, чем построчный ``execute_write_many`` из
    ``SQLiteAdapter``. Применяется только в fixture setup; в
    production-коде запрещено (нарушает инкапсуляцию адаптера).

    В колонку ``normalized_text`` записывается результат
    :func:`normalize_text` — та же операция, что выполняет
    ``query_builder.build_document_queries`` при индексации. Без
    этого FTS5-запрос, преобразованный через
    ``normalize_search_query`` (добавляет префикс
    ``normalized_text:`` и нормализует термин), не нашёл бы
    совпадений.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Открытие прямого соединения с БД (без адаптера).    |
    +---+-----------------------------------------------------+
    | 2 | Генерация списка документов и страниц.              |
    +---+-----------------------------------------------------+
    | 3 | ``executemany`` для ``documents``.                  |
    +---+-----------------------------------------------------+
    | 4 | ``executemany`` для ``text_index_fts``.             |
    +---+-----------------------------------------------------+
    | 5 | ``COMMIT`` в одной транзакции.                      |
    +---+-----------------------------------------------------+
    | 6 | Закрытие соединения (в ``finally``).                |
    +---+-----------------------------------------------------+

    Args:
        db_path: Путь к файлу БД, схема которого уже создана.
    """
    num_docs = int(os.environ.get("DDS_BENCH_SEARCH_DOCS", str(_SEARCH_DOCS_DEFAULT)))
    pages_per_doc = int(
        os.environ.get(
            "DDS_BENCH_SEARCH_PAGES_PER_DOC",
            str(_SEARCH_PAGES_PER_DOC_DEFAULT),
        )
    )

    rng = random.Random(_TEXT_GENERATION_SEED)

    doc_rows: list[tuple[str, str, str]] = []
    fts_rows: list[tuple[str, int, str, str]] = []

    for i in range(num_docs):
        doc_id = f"bench_doc_{i:06d}"
        file_path = f"bench_dir_{i % 100:03d}/file_{i:06d}.pdf"
        file_hash = f"bench_hash_{i:06d}"
        doc_rows.append((doc_id, file_path, file_hash))

        for p in range(pages_per_doc):
            text = _make_sentence(rng, 60)
            fts_rows.append((doc_id, p, text, normalize_text(text)))

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO documents (doc_id, file_path, file_hash) VALUES (?, ?, ?)",
            doc_rows,
        )
        conn.executemany(
            "INSERT INTO text_index_fts "
            "(doc_id, page_number, text_content, normalized_text) "
            "VALUES (?, ?, ?, ?)",
            fts_rows,
        )
        conn.commit()
    finally:
        conn.close()


# =====================================================================
# Fixtures: базовое окружение
# =====================================================================


@pytest.fixture(scope="session")
def bench_tmp_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Корневой временный каталог для всех ресурсов бенчмарков.

    Создаётся один раз на сессию через ``tmp_path_factory``.
    pytest автоматически очищает каталог по завершении сессии.

    Args:
        tmp_path_factory: Встроенная фикстура pytest.

    Returns:
        Путь к временному каталогу.
    """
    return tmp_path_factory.mktemp("dds_bench")


# =====================================================================
# Fixtures: search
# =====================================================================


@pytest.fixture(scope="session")
def search_engine(bench_tmp_root: Path) -> Iterator[SearchEngine]:
    """Возвращает SearchEngine с наполненным FTS5-индексом.

    Схема создаётся через production-путь
    (:class:`DatabaseManager`), данные загружаются bulk-insert'ом
    (:func:`_bulk_populate_search_db`). После завершения сессии
    адаптер закрывается.

    Read-side доступ к документам делегируется
    ``SqliteDocumentRepository`` (Фаза 8, ADR-008): SQL-строки для
    чтения метаданных и текста страниц выведены из application-слоя.

    Args:
        bench_tmp_root: Временный каталог.

    Yields:
        Настроенный :class:`SearchEngine`.
    """
    db_path = bench_tmp_root / "search.db"

    # 1. Схема через production-путь.
    schema_adapter = SQLiteAdapter(str(db_path))
    try:
        DatabaseManager(schema_adapter).ensure_all()
    finally:
        schema_adapter.close()

    # 2. Данные через bulk-insert.
    _bulk_populate_search_db(db_path)

    # 3. Адаптер + движок для бенчмарка.
    adapter = SQLiteAdapter(str(db_path))
    backend = FTS5SearchBackend(adapter)
    engine = SearchEngine(backend, SqliteDocumentRepository(adapter))
    try:
        yield engine
    finally:
        adapter.close()


# =====================================================================
# Fixtures: PDF-файлы
# =====================================================================


@pytest.fixture(scope="session")
def a4_pdf_path(bench_tmp_root: Path) -> Path:
    """PDF A4 (595 × 842 pt) с :data:`_HIGHLIGHTS_PAGE_WORDS` словами.

    Используется и для рендера (test_render_page_a4), и для
    построения WordIndex (medium_word_index).

    Args:
        bench_tmp_root: Временный каталог.

    Returns:
        Путь к сгенерированному PDF.
    """
    path = bench_tmp_root / "a4.pdf"
    _generate_pdf(
        path,
        width_pt=595.0,
        height_pt=842.0,
        words=_HIGHLIGHTS_PAGE_WORDS,
        seed=_PDF_GENERATION_SEED,
    )
    return path


@pytest.fixture(scope="session")
def a1_pdf_path(bench_tmp_root: Path) -> Path:
    """PDF A1 (1684 × 2384 pt) с ~200 словами.

    A1 выбран как представитель крупных чертёжных форматов, для
    которых overhead fork'а заметнее.

    Args:
        bench_tmp_root: Временный каталог.

    Returns:
        Путь к сгенерированному PDF.
    """
    path = bench_tmp_root / "a1.pdf"
    _generate_pdf(
        path,
        width_pt=1684.0,
        height_pt=2384.0,
        words=200,
        seed=_PDF_GENERATION_SEED,
    )
    return path


@pytest.fixture(scope="session")
def small_pdf_path(bench_tmp_root: Path) -> Path:
    """PDF A4 с :data:`_SMALL_PAGE_WORDS` словами.

    Используется для бенчмарка build_word_index_small: маленькая
    страница, где fork overhead доминирует над временем
    построения индекса.

    Args:
        bench_tmp_root: Временный каталог.

    Returns:
        Путь к сгенерированному PDF.
    """
    path = bench_tmp_root / "small.pdf"
    _generate_pdf(
        path,
        width_pt=595.0,
        height_pt=842.0,
        words=_SMALL_PAGE_WORDS,
        seed=_PDF_GENERATION_SEED,
    )
    return path


# =====================================================================
# Fixtures: PyMuPDF
# =====================================================================


@pytest.fixture(scope="session")
def mupdf_extractor() -> PyMuPDFTextExtractor:
    """Экземпляр PyMuPDFTextExtractor для бенчмарков рендера.

    Экстрактор не хранит состояния (каждый вызов open_document
    возвращает независимый PyMuPDFTextDocument), поэтому
    переиспользование одного экземпляра между прогонами безопасно.

    Returns:
        :class:`PyMuPDFTextExtractor`.
    """
    return PyMuPDFTextExtractor()


# =====================================================================
# Fixtures: highlights
# =====================================================================


@pytest.fixture(scope="session")
def medium_word_index(
    mupdf_extractor: PyMuPDFTextExtractor,
    a4_pdf_path: Path,
) -> WordIndex:
    """Предварительно построенный WordIndex для A4-страницы.

    WordIndex строится один раз — это setup, а не часть замера.
    Бенчмарк highlights измеряет только время поиска совпадений
    (HighlightsService.search_highlights), не построения индекса.

    Args:
        mupdf_extractor: Экстрактор PyMuPDF.
        a4_pdf_path: Путь к A4 PDF.

    Returns:
        Построенный :class:`WordIndex`.
    """
    doc = mupdf_extractor.open_document(str(a4_pdf_path))
    try:
        return doc.build_word_index(0)
    finally:
        doc.close()


@pytest.fixture(scope="session")
def highlights_service() -> HighlightsService:
    """Экземпляр HighlightsService с настройками по умолчанию.

    Возвращает сервис, готовый к использованию в бенчмарке.

    Returns:
        :class:`HighlightsService`.
    """
    return HighlightsService()


# =====================================================================
# Fixtures: process runner
# =====================================================================


@pytest.fixture(scope="session")
def process_runner() -> Iterator[ProcessTaskRunner]:
    """ProcessTaskRunner с production-конфигурацией.

    Создаётся один раз на сессию. Параметры (max_concurrent,
    batch_slots, shutdown_timeout) берутся из
    ``dds_core.domain.config`` — те же, что в ``lifespan.py``.

    Закрывается в teardown через ``asyncio.run`` в изолированном
    loop'е: session-scoped event loop (``benchmark_event_loop``)
    закрывается отдельной fixture, но ``close()`` не использует
    семафоры runner'а — они создаются отдельно для бенчмарка.

    Yields:
        Настроенный :class:`ProcessTaskRunner`.
    """
    runner = ProcessTaskRunner()
    try:
        yield runner
    finally:
        asyncio.run(
            runner.close(
                shutdown_timeout=core_config.PROCESS_RUNNER_SHUTDOWN_TIMEOUT,
            )
        )


@pytest.fixture(scope="session")
def benchmark_event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Session-scoped event loop для async-бенчмарков.

    Все async-бенчмарки используют один и тот же loop, потому что
    ``asyncio.Semaphore`` внутри ``ProcessTaskRunner`` привязывается
    к первому loop'у при использовании. Создание нового loop'а для
    каждого вызова ``asyncio.run`` привело бы к ``RuntimeError``
    на второй итерации.

    Yields:
        Открытый event loop.
    """
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


# =====================================================================
# Бенчмарки
# =====================================================================


def test_search_query(
    benchmark: Any,
    search_engine: SearchEngine,
) -> None:
    """FTS5-поиск по датасету 50K страниц.

    Замеряет полный путь ``SearchEngine.search``: разбор запроса,
    MATCH по FTS5, JOIN с ``documents``, группировку страниц по
    документам на Python-стороне.

    Валидация setup: результат непустой (в генерируемом тексте
    гарантированно присутствует :data:`_SEARCH_QUERY`).

    Args:
        benchmark: Фикстура pytest-benchmark.
        search_engine: Движок поиска с наполненной БД.
    """

    def run() -> list:
        return search_engine.search(_SEARCH_QUERY, limit=50, offset=0)

    result = benchmark(run)

    # Валидация setup: запрос должен давать результаты.
    assert isinstance(result, list)
    assert len(result) > 0


def test_render_page_a4(
    benchmark: Any,
    mupdf_extractor: PyMuPDFTextExtractor,
    a4_pdf_path: Path,
) -> None:
    """PNG-рендер A4 @ 300 DPI.

    Замеряет полный цикл ``open_document → render_page → close``.
    Открытие файла включено, потому что в production-коде
    ``/render`` каждый запрос открывает документ заново.

    Валидация setup: результат — непустой PNG (первые байты —
    PNG-сигнатура).

    Args:
        benchmark: Фикстура pytest-benchmark.
        mupdf_extractor: Экстрактор PyMuPDF.
        a4_pdf_path: Путь к A4 PDF.
    """
    path_str = str(a4_pdf_path)

    def run() -> bytes:
        doc = mupdf_extractor.open_document(path_str)
        try:
            return doc.render_page(0, dpi=300)
        finally:
            doc.close()

    png = benchmark(run)

    # Валидация setup.
    assert isinstance(png, bytes)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_render_page_a1(
    benchmark: Any,
    mupdf_extractor: PyMuPDFTextExtractor,
    a1_pdf_path: Path,
) -> None:
    """PNG-рендер A1 @ 300 DPI.

    A1 — крупный чертёжный формат. Время рендера растёт
    квадратично относительно линейных размеров; A1 занимает в
    ~8 раз больше пикселей, чем A4.

    Args:
        benchmark: Фикстура pytest-benchmark.
        mupdf_extractor: Экстрактор PyMuPDF.
        a1_pdf_path: Путь к A1 PDF.
    """
    path_str = str(a1_pdf_path)

    def run() -> bytes:
        doc = mupdf_extractor.open_document(path_str)
        try:
            return doc.render_page(0, dpi=300)
        finally:
            doc.close()

    png = benchmark(run)
    assert isinstance(png, bytes)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_build_word_index_small(
    benchmark: Any,
    mupdf_extractor: PyMuPDFTextExtractor,
    small_pdf_path: Path,
) -> None:
    """Построение WordIndex для страницы с малым числом слов.

    Маленькая страница — «худший случай» для измерения относительного
    overhead'а fork'а: время построения индекса мало, поэтому
    фиксированные издержки доминируют.

    Args:
        benchmark: Фикстура pytest-benchmark.
        mupdf_extractor: Экстрактор PyMuPDF.
        small_pdf_path: Путь к PDF с малым числом слов.
    """
    path_str = str(small_pdf_path)

    def run() -> WordIndex:
        doc = mupdf_extractor.open_document(path_str)
        try:
            return doc.build_word_index(0)
        finally:
            doc.close()

    index = benchmark(run)

    # Валидация setup.
    assert isinstance(index, WordIndex)
    assert len(index.by_normalized) > 0


def test_highlights_500_words(
    benchmark: Any,
    highlights_service: HighlightsService,
    medium_word_index: WordIndex,
) -> None:
    """Поиск 5 терминов на странице с 500 словами.

    Индекс построен заранее (fixture medium_word_index), поэтому
    замеряется только HighlightsService.search_highlights — именно
    эта операция выполняется в production при вызове
    ``POST /highlights`` (после того как WordIndex извлечён из
    кэша).

    Args:
        benchmark: Фикстура pytest-benchmark.
        highlights_service: Сервис подсветки.
        medium_word_index: Предварительно построенный WordIndex.
    """
    terms = list(_HIGHLIGHTS_TERMS)

    def run() -> tuple:
        return highlights_service.search_highlights(
            medium_word_index,
            terms,
            apply_transform=None,
        )

    highlights, _flip, _confidence = benchmark(run)

    # Валидация setup: все термины присутствуют в тексте.
    assert isinstance(highlights, list)
    assert len(highlights) >= len(_HIGHLIGHTS_TERMS)


def test_fork_latency(
    benchmark: Any,
    process_runner: ProcessTaskRunner,
    benchmark_event_loop: asyncio.AbstractEventLoop,
) -> None:
    """Fork + IPC + Queue round-trip через ProcessTaskRunner.

    Замеряет полный цикл process-per-task runner'а:

    1. Создание процесса через forkserver (первый вызов —
       старт forkserver-сервера, последующие — fork).
    2. Pickle-сериализация ``(len, ("hello",))``.
    3. Выполнение ``len("hello")`` в дочернем процессе.
    4. Возврат результата через ``multiprocessing.Queue``.
    5. Ожидание завершения процесса родителем.

    **Функция.** Встроенная ``len`` — picklable без дополнительных
    import'ов в дочернем процессе. Возвращает целое число —
    минимальный pickle-пейлоад. Это изолирует именно измерение
    накладных расходов fork'а, не смешивая их с полезной работой.

    **Event loop.** Используется session-scoped
    ``benchmark_event_loop``: ``asyncio.Semaphore`` внутри
    ``ProcessTaskRunner`` привязывается к первому loop'у при первом
    использовании. Новый loop на каждый прогон → ``RuntimeError``.

    **Прогрев.** Первый вызов включает старт forkserver-сервера
    (~200–500 мс). Последующие — только fork (~30–50 мс).
    pytest-benchmark с ``--benchmark-warmup=on`` прогревает
    перед замерами; при локальном запуске без warmup первая
    итерация будет выбросом.

    Валидация setup: результат равен длине ``"hello"``.

    Args:
        benchmark: Фикстура pytest-benchmark. Тип не известен
            статически (pytest-benchmark не публикует stubs),
            аннотирован как ``Any``.
        process_runner: ProcessTaskRunner с production-конфигурацией.
        benchmark_event_loop: Session-scoped event loop.
    """
    runner = process_runner
    loop = benchmark_event_loop

    def run() -> int:
        return loop.run_until_complete(
            runner.run(
                len,
                _FORK_LATENCY_PAYLOAD,
                timeout=_FORK_LATENCY_TIMEOUT,
            )
        )

    result = benchmark(run)

    # Валидация setup: результат корректен.
    assert result == len(_FORK_LATENCY_PAYLOAD)
