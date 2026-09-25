"""
Медленный бенчмарк полного сканирования каталога DDS.

Назначение
----------
Замер времени выполнения полного цикла ``ScanPipeline.run_async``:
обход каталога, ленивое хеширование, формирование планов индексации
через ``ProcessTaskRunner`` (process-per-task + forkserver), батчевая
запись в SQLite. Датасет — каталог PDF-файлов, сгенерированный
на лету через PyMuPDF.

Метрика: ``test_scan_full_1000_pdfs`` (соответствует ключу
``scan_full`` в ``baseline.json``).

Статус: активен (Фаза 5)
------------------------

Тест активирован в **Фазе 5** (DocumentIndexPlan). До этого момента
был помечен ``@pytest.mark.skip`` из-за зависимости от API,
введённого в Фазах 4–5:

- ``ProcessTaskRunner`` (Фаза 4) — process-per-task исполнитель
  (``forkserver`` на Linux).
- ``DocumentIndexPlan`` и ``build_index_plan_in_subprocess``
  (Фаза 5) — доменная модель плана индексации и worker-функция
  для subprocess.
- ``ITextIndexer.prepare_index_plan`` /
  ``ITextIndexer.write_index_plans`` (Фаза 5) — методы,
  заменившие ``prepare_document_queries`` /
  ``write_documents_batch``.
- ``IIndexWriter`` и ``SqliteIndexWriter`` (Фаза 5) — абстракция
  записи планов и её реализация через SQLite FTS5.

Почему отдельный файл от ``bench_fast.py``
------------------------------------------
- **Длительность.** Один прогон ~2 минуты; 3 прогона + setup
  ≈ 10–15 минут. Это в 5–10 раз дороже быстрых бенчмарков.
- **Триггеры.** Запускается только на push в main, по расписанию
  (еженедельно) и opt-in через метку ``bench-slow`` на PR.
  Fast-benchmarks запускаются на каждый PR.
- **Бюджет времени.** Отдельный ``timeout-minutes: 60`` в workflow
  против 25 у ``bench.yml``.

Датасет
-------
Каталог с ``DDS_BENCH_SCAN_DOCS`` (по умолчанию 1000) PDF-файлами,
сгенерированными в ``tmp_path_factory``. Каждый файл — одна страница
A4 с текстом из 40–80 слов (детерминированный random с seed
``DDS_BENCH_SCAN_SEED``). Содержимое файлов различается, что
гарантирует уникальность хешей и отсутствие ложных срабатываний
duplicate-detection.

Датасет **не коммитится** в репозиторий — экономия ~50–100 МБ.
Параметры генерации фиксированы через env-переменные; между
прогонами датасет переиспользуется (fixture session-scoped).

Ключевая особенность замеров
----------------------------
Свежая БД для каждого прогона. После генерации датасета файлы
сохраняют метаданные (mtime, размер), но каждый вызов
``scan_once`` создаёт новую SQLite-БД и новую схему. Это гарантирует:

1. **Ленивое хеширование не пропускает файлы.** Нет cached
   metadata → все файлы хешируются.
2. **Формирование планов выполняется для всех файлов.** Нет
   зарегистрированных хешей → все файлы «новые».
3. **Полная запись в БД.** Ничего не пропускается как duplicate.

Без сброса БД второй прогон увидел бы все файлы как
``SKIPPED_UNCHANGED`` и завершился бы за секунды, а не за минуты —
замер стал бы бессмысленным.

Event bus
---------
``AsyncEventBus`` создаётся в session-fixture **не запущенным**
(``start()`` не вызывается). Публикация событий в unstarted bus —
no-op (см. ``AsyncEventBus.publish``). Это осознанное решение:

- **Изоляция workload.** Замеряется работа ``ScanPipeline``,
  а не overhead публикации событий (в production подписчик
  ``LoggingSubscriber`` создаёт дополнительную нагрузку, но её
  измерение — отдельная задача).
- **Упрощение fixture.** Не требуется асинхронный
  ``start()``/``stop()`` в setup/teardown.
- **Детерминизм.** Отсутствие подписчиков исключает влияние
  порядка обработки событий на замер.

Методология замеров
-------------------
``benchmark.pedantic(scan_once, rounds=3, iterations=1)`` —
детерминированное число прогонов (3). CLI-workflow дополнительно
передаёт ``--benchmark-min-rounds=3``, но pedantic устанавливает
значение независимо от CLI (важно для запуска локально без флагов).

Почему не авто-калибровка (обычный ``benchmark(fn)``): она
вызывает функцию несколько раз подряд для оценки стабильности.
При 2 минутах на прогон это неприемлемо — потеря 10–20 минут на
калибровку.

Warmup выключен (``--benchmark-warmup=off`` в workflow): scan.full
включает I/O и межпроцессное взаимодействие; прогрев не устранит
дисперсию от файлового кэша ОС.

GC отключён во время замера (``--benchmark-disable-gc``).

Пороги деградации (проверяются в CI)
------------------------------------
- ≤ 15% (или p ≥ 0.05) — OK.
- > 15%, p < 0.05 — WARNING (не блокирует merge).
- > 30%, p < 0.05 — FAIL (блокирует merge).

Сравнение через Mann-Whitney U test (``scipy.stats.mannwhitneyu``),
two-sided. При n = 3 с каждой стороны — минимально допустимый
размер выборки для непараметрического теста.

Ограничения
-----------
1. **Все ресурсы временные.** PDF-датасет и БД — в ``tmp_path``.
   Никаких записей в production-``rd_directory`` или
   ``dds_database.db``.
2. **Детерминированный датасет.** Random с фиксированным seed;
   параметры через ``DDS_BENCH_*``.
3. **Event bus не запущен.** См. выше.
4. **Session-scoped event loop.** ``ProcessTaskRunner`` использует
   ``asyncio.Semaphore``, которые привязываются к первому loop'у
   при первом использовании. Все прогоны используют один loop.
5. **One-shot процесс.** Раз в сессию создаётся ``ProcessTaskRunner``
   с production-конфигурацией из ``config``; закрывается в teardown
   fixture.
6. **Reference-справочники не заполняются.** Бенчмарк не использует
   ``DocumentMetadataService`` (обновление метаданных — отдельный
   этап вторичного сканирования, замеряется вне этого файла).
7. **Модуль не выполняет assert'ов бизнес-логики.** Assert'ы —
   только для валидации setup (status, total_files).

Запуск
------
::

    pytest tests/benchmarks/bench_slow.py \\
        --benchmark-min-rounds=3 \\
        --benchmark-warmup=off \\
        --benchmark-disable-gc

Локально для быстрой отладки можно уменьшить датасет::

    DDS_BENCH_SCAN_DOCS=50 pytest tests/benchmarks/bench_slow.py

Принципы:
    - Модуль не выполняет логирования;
    - не читает и не пишет production-файлы;
    - не содержит изменяемого состояния между вызовами;
    - все ассерты — валидация setup, не бизнес-логики.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import random
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pymupdf
import pytest
from dds_core.application.indexer import TextIndexer
from dds_core.application.scan_pipeline import ScanPipeline
from dds_core.domain import config
from dds_core.domain.models import ScanStatus
from dds_core.infrastructure.database import DatabaseManager
from dds_core.infrastructure.event_bus import AsyncEventBus
from dds_core.infrastructure.file_hasher import FileHasher
from dds_core.infrastructure.file_scanner import DirectoryScanner
from dds_core.infrastructure.process_task_runner import ProcessTaskRunner
from dds_core.infrastructure.pymupdf_text_extractor import PyMuPDFTextExtractor
from dds_core.infrastructure.sqlite_adapter import SQLiteAdapter
from dds_core.infrastructure.sqlite_document_repository import (
    SqliteDocumentRepository,
)
from dds_core.infrastructure.sqlite_index_writer import SqliteIndexWriter
from dds_core.subprocess_tasks.pdf_workers import build_index_plan_in_subprocess

# =====================================================================
# Константы
# =====================================================================

_SCAN_DOCS_DEFAULT = 1000
"""Количество PDF-файлов в датасете по умолчанию."""

_SCAN_SEED_DEFAULT = 42
"""Seed генератора датасета по умолчанию (детерминизм)."""

_WORDS_MIN = 40
"""Минимальное число слов на странице PDF."""

_WORDS_MAX = 80
"""Максимальное число слов на странице PDF."""

_A4_WIDTH_PT = 595.0
"""Ширина страницы A4 в PDF-points."""

_A4_HEIGHT_PT = 842.0
"""Высота страницы A4 в PDF-points."""

_WORDS_CORPUS: tuple[str, ...] = (
    # Набор слов для генерации текста. Разнообразие необходимо,
    # чтобы содержимое PDF различалось и хеши были уникальны.
    "корпус",
    "гидрошпонка",
    "дробление",
    "фундамент",
    "изоляция",
    "монтаж",
    "схема",
    "узел",
    "сечение",
    "разрез",
    "план",
    "проект",
    "чертёж",
    "спецификация",
    "ведомость",
    "арматура",
    "бетон",
    "опалубка",
    "стык",
    "шов",
)


# =====================================================================
# Утилиты генерации датасета
# =====================================================================


def _make_sentence(rng: random.Random, words_count: int) -> str:
    """Генерирует строку из ``words_count`` случайных слов.

    Слова берутся из :data:`_WORDS_CORPUS` с равномерным
    распределением. Используется как содержимое PDF-страницы.

    Args:
        rng: Экземпляр ``random.Random`` с фиксированным seed.
        words_count: Количество слов в строке.

    Returns:
        Строка из слов, разделённых пробелами.
    """
    return " ".join(rng.choice(_WORDS_CORPUS) for _ in range(words_count))


def _generate_pdf_corpus(directory: Path, count: int, seed: int) -> None:
    """Создаёт каталог с ``count`` PDF-файлами.

    Каждый файл — одна страница A4 с текстом из
    :data:`_WORDS_MIN`–:data:`_WORDS_MAX` слов. Текст размещается
    блоками по 10 слов на строку; координаты фиксированы. Random
    с seed ``seed`` обеспечивает детерминизм между прогонами.

    Файлы именуются ``doc_000000.pdf``… ``doc_<count-1>.pdf`` и
    располагаются в подкаталогах по 100 штук
    (``dir_000/``, ``dir_001/``, …) — имитация реального каталога
    РД с разделами.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Создание корневого каталога.                        |
    +---+-----------------------------------------------------+
    | 2 | Создание подкаталогов по 100 файлов (при count>100).|
    +---+-----------------------------------------------------+
    | 3 | Цикл по ``count``:                                   |
    |   | a. Создание документа PyMuPDF.                      |
    |   | b. Добавление страницы A4.                          |
    |   | c. Генерация текста (детерминированный rng).        |
    |   | d. Разбиение на строки по 10 слов.                  |
    |   | e. Сохранение файла.                                |
    |   | f. Закрытие документа.                              |
    +---+-----------------------------------------------------+

    Args:
        directory: Корневой каталог для датасета.
        count: Количество PDF-файлов.
        seed: Seed для генератора случайных чисел.

    Raises:
        OSError: При ошибке записи файла.
        pymupdf.FileDataError: При ошибке создания PDF.
    """
    directory.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    subdirs: list[Path] = []
    for i in range(count):
        if i % 100 == 0:
            sub = directory / f"dir_{i // 100:03d}"
            sub.mkdir(exist_ok=True)
            subdirs.append(sub)
        path = subdirs[-1] / f"doc_{i:06d}.pdf"

        words_count = rng.randint(_WORDS_MIN, _WORDS_MAX)
        text = _make_sentence(rng, words_count)

        doc = pymupdf.open()
        try:
            page = doc.new_page(
                width=_A4_WIDTH_PT,
                height=_A4_HEIGHT_PT,
            )
            tokens = text.split()
            x0 = 40.0
            y = 60.0
            line_height = 12.0
            for chunk_start in range(0, len(tokens), 10):
                line = " ".join(tokens[chunk_start : chunk_start + 10])
                page.insert_text((x0, y), line, fontsize=9)
                y += line_height
            doc.save(str(path))
        finally:
            doc.close()


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture(scope="session")
def bench_tmp_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Корневой временный каталог для бенчмарка.

    Создаётся один раз на сессию через ``tmp_path_factory``.
    pytest автоматически очищает каталог по завершении сессии.

    Args:
        tmp_path_factory: Встроенная фикстура pytest.

    Returns:
        Путь к временному каталогу.
    """
    return tmp_path_factory.mktemp("dds_bench_slow")


@pytest.fixture(scope="session")
def bench_event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Session-scoped event loop для async-бенчмарков и fixtures.

    Все прогоны используют один и тот же loop, потому что
    ``ProcessTaskRunner`` создаёт ``asyncio.Semaphore``, которые
    привязываются к первому loop'у при использовании. Создание
    нового loop'а для каждого прогона привело бы к
    ``RuntimeError: ... is bound to a different event loop``.

    Дополнительно loop используется для graceful shutdown
    ``ProcessTaskRunner`` в teardown одноимённой fixture.

    Yields:
        Открытый event loop.
    """
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


@pytest.fixture(scope="session")
def scan_docs_count() -> int:
    """Количество PDF-файлов в датасете.

    Читается из ``DDS_BENCH_SCAN_DOCS`` (по умолчанию
    :data:`_SCAN_DOCS_DEFAULT`). Используется также в тесте
    для валидации setup (``progress.total_files``).

    Returns:
        Число PDF-файлов.
    """
    return int(os.environ.get("DDS_BENCH_SCAN_DOCS", str(_SCAN_DOCS_DEFAULT)))


@pytest.fixture(scope="session")
def scan_seed() -> int:
    """Seed генератора датасета.

    Читается из ``DDS_BENCH_SCAN_SEED`` (по умолчанию
    :data:`_SCAN_SEED_DEFAULT`). Детерминизм между прогонами —
    критично для сопоставимости baseline-сравнений.

    Returns:
        Seed.
    """
    return int(os.environ.get("DDS_BENCH_SCAN_SEED", str(_SCAN_SEED_DEFAULT)))


@pytest.fixture(scope="session")
def pdf_corpus(
    bench_tmp_root: Path,
    scan_docs_count: int,
    scan_seed: int,
) -> Path:
    """Каталог PDF-файлов для сканирования.

    Генерируется один раз на сессию. Между прогонами не
    пересоздаётся: файлы и их метаданные (mtime, размер) стабильны,
    что позволяет корректно тестировать lazy hashing (metadata
    совпадают между прогонами; отличается только содержимое БД).

    Args:
        bench_tmp_root: Временный каталог.
        scan_docs_count: Количество файлов.
        scan_seed: Seed генератора.

    Returns:
        Путь к каталогу с PDF-файлами.
    """
    corpus_dir = bench_tmp_root / "rd_corpus"
    _generate_pdf_corpus(corpus_dir, count=scan_docs_count, seed=scan_seed)
    return corpus_dir


@pytest.fixture(scope="session")
def scan_executor() -> Iterator[ThreadPoolExecutor]:
    """ThreadPoolExecutor для блокирующих операций ScanPipeline.

    Используется для: обхода каталога, хеширования файлов, записей
    в БД. Размер — :data:`config.SCAN_EXECUTOR_MAX_WORKERS` (по
    умолчанию 6). Согласовано с production-конфигурацией, чтобы
    издержки совпадали.

    Yields:
        Настроенный ThreadPoolExecutor.
    """
    executor = ThreadPoolExecutor(
        max_workers=config.SCAN_EXECUTOR_MAX_WORKERS,
        thread_name_prefix="bench-scan",
    )
    try:
        yield executor
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


@pytest.fixture(scope="session")
def process_runner() -> Iterator[ProcessTaskRunner]:
    """ProcessTaskRunner с production-конфигурацией.

    Создаётся один раз на сессию. Параметры (``max_concurrent``,
    ``batch_slots``, ``shutdown_timeout``) берутся из
    ``dds_core.domain.config`` — те же, что в ``lifespan.py``.

    Закрывается в teardown через ``asyncio.run`` в изолированном
    loop'е: session-scoped event loop (``bench_event_loop``)
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
                shutdown_timeout=config.PROCESS_RUNNER_SHUTDOWN_TIMEOUT,
            )
        )


@pytest.fixture(scope="session")
def event_bus() -> AsyncEventBus:
    """AsyncEventBus в неработающем состоянии.

    Публикация событий в unstarted bus — no-op (см.
    ``AsyncEventBus.publish``: ранний ``return`` при
    ``self._loop is None or not self._running``). Это осознанное
    решение для изоляции замеряемого workload'а от overhead'а
    публикации событий и обработки подписчиками.

    Подробнее — в модульном docstring, раздел «Event bus».

    Returns:
        Экземпляр AsyncEventBus без запущенного loop'а.
    """
    return AsyncEventBus(max_queue_size=1000)


@pytest.fixture(scope="session")
def directory_scanner() -> DirectoryScanner:
    """Сканер каталога для ScanPipeline."""
    return DirectoryScanner()


@pytest.fixture(scope="session")
def file_hasher() -> FileHasher:
    """Хешер файлов для ScanPipeline."""
    return FileHasher()


@pytest.fixture(scope="session")
def mupdf_extractor() -> PyMuPDFTextExtractor:
    """Экземпляр PyMuPDFTextExtractor для TextIndexer и ScanPipeline.

    Экстрактор не хранит состояния (каждый вызов ``open_document``
    возвращает независимый ``PyMuPDFTextDocument``), поэтому
    переиспользование одного экземпляра между прогонами безопасно.

    В бенчмарке ``scan.full`` путь через ``process_runner``
    используется всегда, поэтому ``mupdf_extractor`` не вызывается
    напрямую во время замера — он требуется только для
    конструкторов ``TextIndexer`` и ``ScanPipeline`` (обязательные
    параметры контракта).

    Returns:
        :class:`PyMuPDFTextExtractor`.
    """
    return PyMuPDFTextExtractor()


# =====================================================================
# Бенчмарк
# =====================================================================


def test_scan_full_1000_pdfs(
    benchmark: Any,
    pdf_corpus: Path,
    scan_docs_count: int,
    bench_event_loop: asyncio.AbstractEventLoop,
    scan_executor: ThreadPoolExecutor,
    process_runner: ProcessTaskRunner,
    event_bus: AsyncEventBus,
    directory_scanner: DirectoryScanner,
    file_hasher: FileHasher,
    mupdf_extractor: PyMuPDFTextExtractor,
    tmp_path: Path,
) -> None:
    """Полное сканирование каталога PDF (по умолчанию 1000 файлов).

    Замеряет полный цикл ``ScanPipeline.run_async`` — обход каталога,
    ленивое хеширование, формирование планов индексации через
    ``ProcessTaskRunner`` (process-per-task + forkserver), батчевая
    запись в SQLite через ``SqliteIndexWriter``.

    Каждый прогон создаёт **свежую БД** (см. модульный docstring,
    раздел «Ключевая особенность замеров»). Без сброса БД второй
    прогон увидел бы все файлы как ``SKIPPED_UNCHANGED`` и
    завершился бы за секунды.

    Датасет PDF переиспользуется между прогонами (session-scoped
    fixture ``pdf_corpus``): метаданные файлов стабильны, что
    изолирует влияние файлового кэша ОС на замер.

    Используется ``benchmark.pedantic(rounds=3, iterations=1)`` —
    детерминированное число прогонов. Авто-калибровка (обычный
    ``benchmark(fn)``) неприемлема: при 2 минутах на прогон
    она тратила бы 10–20 минут.

    Валидация setup (assert'ы не измеряются, выполняются после
    бенчмарка):

    - ``status == COMPLETED`` — сканирование не упало и не
      было прервано;
    - ``total_files == scan_docs_count`` — все файлы датасета
      обработаны.

    Args:
        benchmark: Фикстура pytest-benchmark. Тип не известен
            статически (pytest-benchmark не публикует stubs),
            аннотирован как ``Any``.
        pdf_corpus: Каталог с PDF-файлами.
        scan_docs_count: Ожидаемое количество файлов.
        bench_event_loop: Session-scoped event loop.
        scan_executor: ThreadPoolExecutor для блокирующих операций.
        process_runner: ProcessTaskRunner с production-конфигурацией.
        event_bus: AsyncEventBus (не запущен).
        directory_scanner: Сканер каталога.
        file_hasher: Хешер файлов.
        mupdf_extractor: Экстрактор PyMuPDF (обязательный параметр
            ``TextIndexer`` и ``ScanPipeline``).
        tmp_path: Функционально-уникальный временный каталог pytest.
    """
    counter = itertools.count()
    loop = bench_event_loop

    def scan_once() -> ScanStatus:
        """Один прогон сканирования на свежей БД.

        Создаёт новый ``SQLiteAdapter``, ``SqliteIndexWriter``,
        ``SqliteDocumentRepository``, ``TextIndexer`` и
        ``ScanPipeline`` для текущего прогона. Каждый прогон
        изолирован: своя БД, свой ``doc_id``-namespace.

        Read-side доступ к документам делегируется
        ``SqliteDocumentRepository`` (Фаза 8, ADR-008): SQL-строки
        для чтения метаданных выведены из application-слоя.
        Репозиторий создаётся заново на каждый прогон — БД
        свежая, и репозиторий привязан к её адаптеру.

        Returns:
            Итоговый ``ScanStatus`` сканирования.
        """
        idx = next(counter)
        db_path = tmp_path / f"scan_{idx:03d}.db"

        adapter = SQLiteAdapter(str(db_path))
        try:
            DatabaseManager(adapter).ensure_all()
            index_writer = SqliteIndexWriter(adapter)
            document_repository = SqliteDocumentRepository(adapter)
            indexer = TextIndexer(
                text_extractor=mupdf_extractor,
                index_writer=index_writer,
                document_repository=document_repository,
            )
            pipeline = ScanPipeline(
                db=adapter,
                scanner=directory_scanner,
                hasher=file_hasher,
                text_extractor=mupdf_extractor,
                indexer=indexer,
                event_bus=event_bus,
                process_runner=process_runner,
                index_plan_worker=build_index_plan_in_subprocess,
                max_hash_workers=4,
                max_extract_workers=6,
                scan_executor=scan_executor,
                document_cache=None,
            )
            progress = loop.run_until_complete(
                pipeline.run_async(
                    rd_directory=str(pdf_corpus),
                    scan_id=idx + 1,
                    correlation_id=f"bench-scan-{idx}",
                )
            )
            return progress.status
        finally:
            adapter.close()

    status = benchmark.pedantic(scan_once, rounds=3, iterations=1)
    assert status == ScanStatus.COMPLETED
