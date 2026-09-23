"""
REST API для веб-интерфейса DDS.

Этот модуль содержит REST API endpoints для взаимодействия
веб-интерфейса с ядром DDS. Используется фреймворк FastAPI
для маршрутизации, валидации запросов и автоматической
генерации OpenAPI-документации.

Модуль находится в presentation layer и не содержит бизнес-логики.
Все операции делегируются компонентам application layer
(SearchEngine, ScanOrchestrator, ModuleLifecycle, HighlightsService).

Удаление Service Locator (скорректированный план, Фаза 2):
Модульные глобалы ``_context`` и функции ``set_context()`` /
``get_context()`` без аргументов удалены. FastAPI dependency
:func:`get_context` принимает :class:`~fastapi.Request` и читает
контейнер из ``request.app.state.api_context``; значение
записывается в lifespan при старте приложения. Это устраняет
скрытую глобальную связь между эндпоинтами и делает зависимости
явными через ``CtxDep``. Тесты могут подменять контекст через
``app.dependency_overrides[get_context]``.

Асинхронная модель (Фаза 4):
Все обработчики являются асинхронными (``async def``).
Блокирующие операции (чтение из БД, чтение из файловой системы,
хеширование файлов, рендер PDF) выполняются через ``run_in_executor``
в пулах потоков, что предотвращает блокировку event loop и
обеспечивает обработку нескольких запросов параллельно.

Разделение пулов потоков (скорректированный план):
- ``api_executor`` — веб-запросы (чтение БД, файловой системы).
- ``scan_executor`` — сканирование, рендер PDF, построение
  индексов слов.

Таймауты блокирующих операций (скорректированный план):
Все критичные блокирующие операции обёрнуты в ``run_with_timeout``.

Опциональные зависимости ``APIContext`` и хелпер ``_require_event_bus``
(скорректированный план по типизации):

``APIContext.event_bus`` имеет тип ``IEventBus | None``: в тестах
и сценариях, где шина не требуется, допускается ``None``. В
production-пути ``lifespan.py`` всегда передаёт полноценную шину.
Обработчики, которым событие критично (публикация ``OperationTimedOut``,
``CoordinateSystemAnomalyDetected`` и т. п.), вызывают
:func:`_require_event_bus` в начале функции — это явный 503 вместо
неконтролируемого ``AttributeError``, если шина не инициализирована.

Диагностический метод ``get_queue_stats`` не входит в базовый
протокол ``IEventBus``: это опциональная возможность конкретной
реализации (``AsyncEventBus``). В ``/api/diagnostics`` используется
структурная проверка через ``_EventBusWithStats`` (runtime-checkable
Protocol), что не сужает диагностику до конкретного класса.

Фильтрация по метаданным:
Эндпоинт ``/api/search`` принимает дополнительные параметры:
``object_code``, ``discipline_code``, ``document_type_code``,
``unmatched_only``. Они передаются в ``SearchEngine`` через
``SearchFilters``. Эндпоинт ``/api/filters/metadata`` отдаёт
справочники для фильтров. Эндпоинт ``/api/scan/refresh-metadata``
запускает повторное обновление метаданных документов.

Серверная группировка результатов поиска:

Эндпоинт ``/api/search`` возвращает сгруппированные результаты:
одна запись на документ с массивом страниц. Формат:

.. code-block:: json

    {
      "query": "...",
      "total": 42,
      "results": [
        {
          "doc_id": "...",
          "file_path": "раздел_01/чертёж_001.pdf",
          "relevance_score": -3.14,
          "pages": [
            {"page_number": 1, "snippet": "...", "terms": ["...", "..."]},
            {"page_number": 4, "snippet": "...", "terms": ["..."]}
          ]
        }
      ]
    }

Семантика ``total`` — количество **уникальных документов**,
соответствующих запросу и фильтрам. Параметры ``limit`` и
``offset`` также отсчитываются по документам.

Термины подсветки в ``pages[i].terms`` (Фаза 6, ADR-006):

Сервер извлекает уникальные термины совпадений из сниппета
и передаёт их клиенту вместе с результатами поиска. До Фазы 6
термины извлекались на клиенте (``search.js``) через регулярные
выражения по маркерам ``[[DDS_HIGHLIGHT_START]]`` /
``[[DDS_HIGHLIGHT_END]]``. Теперь клиент получает готовый список
и не парсит сниппеты — это устраняет протечку FTS5-специфики
в presentation layer и делает сервер единственной точкой
контроля формата терминов.

Поддержка пустого запроса:
Параметр ``q`` необязателен. Если он не указан или пуст,
поиск выполняется только по фильтрам (или по всем документам,
если фильтры также отсутствуют).

Скачивание документа:
Эндпоинт ``/api/documents/{doc_id}/download`` возвращает исходный
PDF-файл документа из каталога РД. Авторизация не требуется.
Защита от path traversal обеспечивается общей функцией
``_resolve_document_file``.

Предпросмотр документа (рендер и подсветка):

Реализованы три независимых эндпоинта:

- ``GET /api/documents/{doc_id}/pages/{page_number}/render`` —
  PNG-рендер страницы. Возвращает ``image/png`` с заголовками
  ``ETag`` (по ``file_hash`` + ``page`` + ``dpi``) и
  ``Cache-Control: private, max-age=86400``.
- ``POST /api/documents/{doc_id}/pages/{page_number}/highlights`` —
  поиск прямоугольников совпадений для списка терминов.
- ``POST /api/documents/pages/batch`` — пакетная загрузка текста
  страницы для нескольких документов. Используется UI при пустом
  поисковом запросе: вместо N HTTP-запросов выполняется один.

Поиск подсветки выполняется в **нормализованном** пространстве
(``WordIndex``), что делает его устойчивым к смешению кириллицы и
латиницы в PDF и сниппетах FTS5.

LRU-кэш текста страниц (скорректированный план, шаг 3.2):

``SearchEngine.get_page_text`` кэширует результаты чтения из
``text_index_fts`` с ключом ``(doc_id, page_number, file_hash)``.
В обработчиках, читающих текст страницы, вызывающий код обязан
передать ``doc.file_hash``, полученный из ``get_document_info``.
При удалении документа кэш инвалидируется через
``invalidate_page_text_cache`` (см. ``delete_document``).

Таймауты блокирующих операций (скорректированный план, шаг 4.1):

Чтение текста страницы (одиночное и batch) обёрнуто в
``run_with_timeout`` с дескриптором ``document.page_load``.
При срабатывании таймаута публикуется событие
``OperationTimedOut`` на уровне WARNING, а клиенту возвращается
HTTP 504. При этом поток в ``ThreadPoolExecutor`` продолжит
выполнение до фактического завершения (см. примечание в
``timeout_guard.py``).

Диагностика и коррекция системы координат (скорректированный план):

Эндпоинт ``POST /highlights`` принимает дополнительное поле
``apply_transform`` (``bool | None``), управляющее трансформацией
координат для страниц с аномальной системой координат. Ответ
содержит ``applied_transform`` и ``transform_confidence``.
При уверенности ``"high"`` или ``"medium"`` публикуется событие
``CoordinateSystemAnomalyDetected`` уровня WARNING.

Темы оформления:
Эндпоинт ``/api/themes`` сканирует каталог ``dds_web/static/css``
на наличие файлов ``theme-*.css`` и возвращает список доступных
тем.

Индикация вторичного сканирования:
Эндпоинт ``/api/scan/progress`` подписывается на события
``scan.secondary.started``, ``scan.secondary.progress`` и
``scan.secondary.completed``.

Отладочный эндпоинт:
``POST /api/debug/filter-events`` принимает события от клиентской
части фильтров и выводит их в stdout сервера.

Принципы:
- Модуль находится в presentation layer и не содержит бизнес-логики.
- Зависимости передаются через :class:`APIContext` (Dependency
  Injection); контейнер читается из ``request.app.state.api_context``
  через FastAPI dependency :func:`get_context`. Модульные глобалы
  (Service Locator) не используются.
- Все запросы валидируются через Pydantic.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import tempfile
import uuid
from concurrent.futures import Executor
from pathlib import Path
from typing import Annotated, Any, Callable, Protocol, runtime_checkable

try:
    from fastapi import APIRouter, Depends, HTTPException, Query, Request
    from fastapi.responses import FileResponse, Response, StreamingResponse
    from pydantic import BaseModel, Field, field_validator
except ImportError:
    raise ImportError(
        "Для запуска веб-интерфейса DDS необходимо установить "
        "fastapi и pydantic: pip install fastapi pydantic"
    )

from dds_core.application.highlights_service import HighlightsService
from dds_core.application.module_lifecycle import ModuleLifecycle
from dds_core.application.reference_data_service import ReferenceDataService
from dds_core.application.scan_orchestrator import ScanOrchestrator
from dds_core.application.search_engine import SearchEngine
from dds_core.application.timeout_guard import run_with_timeout
from dds_core.application.word_index_cache import WordIndexCache
from dds_core.domain import config as core_config
from dds_core.domain.events import (
    CoordinateSystemAnomalyDetected,
    ScanCompleted,
    ScanProgressUpdated,
    ScanSecondaryCompleted,
    ScanSecondaryProgressUpdated,
    ScanSecondaryStarted,
)
from dds_core.domain.interfaces import IEventBus, ITextExtractor
from dds_core.domain.models import (
    ModuleStatus,
    ScanStatus,
    SearchFilters,
    WordIndex,
)

from .auth import AdminSessionDep, AuthDep

# ----------------------------------------------------------------------
# Pydantic-модели для запросов и ответов
# ----------------------------------------------------------------------


class PageHitResponse(BaseModel):
    """Одно совпадение на странице документа.

    Используется внутри :class:`SearchResultItem` для представления
    страницы, на которой найдено совпадение, её сниппета и терминов
    подсветки.

    Attributes:
        page_number: Номер страницы (0-based).
        snippet: Фрагмент текста страницы с нейтральными маркерами
            подсветки. Оригинальные символы (денормализованный текст)
            передаются клиенту в исходном виде.
        terms: Уникальные термины подсветки, извлечённые сервером
            из сниппета (см. ADR-006). Порядок — порядок первого
            появления. Пустой список, если сниппет пуст или маркеров
            в нём нет (например, для виртуальной страницы пустого
            запроса в режиме «показать все»). Клиент использует
            готовый список терминов для запроса подсветки
            (``POST /highlights``) и не парсит сниппеты.
    """

    page_number: int = Field(description="Номер страницы (0-based)")
    snippet: str = Field(description="Фрагмент текста с маркерами подсветки")
    terms: list[str] = Field(
        default_factory=list,
        description="Уникальные термины подсветки на этой странице",
    )


class SearchResultItem(BaseModel):
    """Сгруппированный результат поиска по документу.

    Представляет один документ и все страницы этого документа,
    на которых найдены совпадения. Соответствует
    :class:`~dds_core.domain.models.SearchResult`.

    Attributes:
        doc_id: Идентификатор документа.
        file_path: Относительный путь к файлу документа.
        relevance_score: Лучшая (минимальная) оценка BM25 среди
            страниц документа. Чем меньше значение, тем выше
            релевантность. Для пустого запроса — ``0.0``.
        pages: Список страниц с совпадениями. Отсортирован по
            возрастанию ``page_number``.
    """

    doc_id: str = Field(description="Идентификатор документа")
    file_path: str = Field(description="Относительный путь к файлу")
    relevance_score: float = Field(description="Лучшая оценка BM25 (меньше — релевантнее)")
    pages: list[PageHitResponse] = Field(description="Страницы документа с совпадениями")


class SearchResponse(BaseModel):
    """Ответ на запрос поиска с серверной группировкой.

    Каждый элемент ``results`` — это документ со вложенным массивом
    страниц. Поле ``total`` — количество уникальных документов
    (не страниц), соответствующих запросу и фильтрам.

    Attributes:
        query: Фактически использованный поисковый запрос.
        total: Общее количество уникальных документов.
        results: Список сгруппированных результатов.
    """

    query: str = Field(description="Поисковый запрос")
    total: int = Field(description="Общее количество документов")
    results: list[SearchResultItem] = Field(description="Сгруппированные результаты поиска")


class DocumentResponse(BaseModel):
    """Ответ на запрос информации о документе."""

    doc_id: str
    file_path: str
    file_hash: str
    file_size: int
    page_count: int
    indexed_at: str
    last_modified: str


class PageTextResponse(BaseModel):
    """Ответ на запрос текста страницы."""

    doc_id: str
    page_number: int
    text: str


class PageTextBatchRequest(BaseModel):
    """Запрос на пакетную загрузку текста страницы.

    Используется UI при пустом поисковом запросе (режим «показать
    все»): вместо N отдельных GET-запросов к
    ``/api/documents/{id}/pages/{n}`` клиент отправляет один
    POST с массивом ``doc_ids``.

    Ограничение ``max_length=20`` выбрано с учётом размера пула
    ``api_executor`` (4 воркера) и типичного количества документов
    на страницу пагинации (16). Пакеты больше 20 дают лишь
    незначительный выигрыш по сравнению с риском исчерпания пула
    при массовых таймаутах.

    Attributes:
        doc_ids: Список идентификаторов документов (1–20).
        page_number: Номер страницы (0-based). По умолчанию 0.
    """

    doc_ids: list[str] = Field(
        min_length=1,
        max_length=20,
        description="Список идентификаторов документов (1–20).",
    )
    page_number: int = Field(
        default=0,
        ge=0,
        description="Номер страницы (0-based).",
    )


class PageTextBatchResponse(BaseModel):
    """Ответ на пакетную загрузку текста страницы.

    Документы и страницы, для которых текст получен, попадают в
    ``pages``. Отсутствующие документы и страницы вне диапазона
    попадают в ``errors`` — клиент может отобразить «текст
    недоступен» с указанием причины.

    Attributes:
        pages: Словарь ``doc_id → текст``. Отсутствующие документы
            не включаются.
        errors: Словарь ``doc_id → сообщение об ошибке``. Пустой,
            если все документы обработаны успешно.
    """

    pages: dict[str, str] = Field(
        default_factory=dict,
        description="doc_id → текст. Отсутствующие doc_id не включены.",
    )
    errors: dict[str, str] = Field(
        default_factory=dict,
        description="doc_id → сообщение об ошибке.",
    )


class HighlightsRequest(BaseModel):
    """Запрос на получение подсветки для страницы документа.

    Attributes:
        terms: Список терминов для поиска. Каждый термин может быть
            одним словом или фразой. Регистр и раскладка не важны —
            нормализация выполняется на сервере.
        apply_transform: Управление трансформацией координат для
            страниц с аномальной системой координат. Три режима:

            - ``None`` (по умолчанию) — авто-диагностика;
            - ``True`` — применить трансформацию принудительно;
            - ``False`` — не применять трансформацию.
    """

    terms: list[str] = Field(
        default_factory=list,
        max_length=200,
        description="Термины для поиска (до 200 терминов)",
    )
    apply_transform: bool | None = Field(
        default=None,
        description=(
            "Управление трансформацией координат: "
            "null — авто-диагностика, "
            "true — применить, "
            "false — не применять."
        ),
    )

    @field_validator("terms")
    @classmethod
    def _validate_term_shape(cls, value: list[str]) -> list[str]:
        """Проверяет длину и количество слов в каждом термине.

        Защита от abuse: термины длиной более 500 символов или
        содержащие более 20 слов отклоняются с ``422``.

        Args:
            value: Список терминов.

        Returns:
            Тот же список, если все термины валидны.

        Raises:
            ValueError: Если какой-либо термин слишком длинный
                или содержит слишком много слов.
        """
        for term in value:
            if len(term) > 500:
                raise ValueError(f"Термин длиной {len(term)} символов превышает лимит 500.")
            if len(term.split()) > 20:
                raise ValueError("Термин содержит более 20 слов.")
        return value


class HighlightBox(BaseModel):
    """Один прямоугольник подсветки в нормализованных координатах.

    Attributes:
        x: Левая граница (доля от ширины страницы, ``0..1``).
        y: Верхняя граница (доля от высоты страницы, ``0..1``).
        w: Ширина (доля от ширины страницы, ``0..1``).
        h: Высота (доля от высоты страницы, ``0..1``).
        term: Термин, породивший подсветку. Используется для
            всплывающей подсказки (``title``) на элементе подсветки.
    """

    x: float
    y: float
    w: float
    h: float
    term: str


class HighlightsResponse(BaseModel):
    """Ответ на запрос подсветки для страницы.

    Attributes:
        highlights: Список прямоугольников подсветки. Порядок не
            определён — клиент не должен полагаться на конкретный
            порядок.
        applied_transform: Фактически применённая трансформация
            координат. ``True``, если координаты bbox были
            преобразованы из аномальной системы в top-left.
        transform_confidence: Уровень уверенности диагностики:

            - ``"high"`` — сработали оба признака аномалии.
            - ``"medium"`` — сработал ровно один признак.
            - ``"none"`` — аномалия не обнаружена, диагностика
              не выполнялась или отключена.
            - ``"manual"`` — флаг задан явно через
              ``apply_transform`` запроса.
    """

    highlights: list[HighlightBox]
    applied_transform: bool = Field(
        default=False,
        description="Была ли применена трансформация координат.",
    )
    transform_confidence: str = Field(
        default="none",
        description=("Уровень уверенности диагностики: 'high' | 'medium' | 'none' | 'manual'."),
    )


class ScanStatusResponse(BaseModel):
    """Ответ на запрос статуса сканирования."""

    phase: str
    status: str
    total_files: int
    processed_files: int
    current_file: str
    progress_percent: float
    indexed_files: int = Field(description="Количество проиндексированных файлов")
    duplicate_files: int = Field(description="Количество дубликатов")
    skipped_files: int = Field(description="Количество пропущенных файлов")
    error_files: int = Field(description="Количество файлов с ошибками")


class ScanStartResponse(BaseModel):
    """Ответ на запуск сканирования."""

    task_id: str
    message: str


class IndexStatusResponse(BaseModel):
    """Ответ на запрос состояния индексации."""

    state: str
    total_files_in_directory: int
    indexed_files: int
    duplicate_files_on_disk: int
    new_files: int
    modified_files: int
    unindexed_files: int
    search_available: bool
    message: str
    progress_percent: float
    is_fully_indexed: bool


class ModuleInfoResponse(BaseModel):
    """Ответ на запрос информации о модуле."""

    module_name: str
    version: str
    api_version: str
    status: str
    description: str
    scan_phase: str


class ModuleListResponse(BaseModel):
    """Ответ на запрос списка модулей."""

    modules: list[ModuleInfoResponse]


class SettingsResponse(BaseModel):
    """Ответ с текущими настройками DDS."""

    rd_directory: str
    db_path: str
    modules_directory: str


class SettingsUpdateRequest(BaseModel):
    """Запрос на обновление настроек DDS."""

    rd_directory: str | None = Field(default=None)
    db_path: str | None = Field(default=None)


class SettingsUpdateResponse(BaseModel):
    """Ответ на обновление настроек DDS."""

    message: str
    requires_restart: bool
    settings: SettingsResponse


class ChangePasswordRequest(BaseModel):
    """Запрос на смену пароля пользователя."""

    username: str
    old_password: str
    new_password: str


class ChangePasswordResponse(BaseModel):
    """Ответ на запрос смены пароля."""

    message: str


class DebugEvent(BaseModel):
    """Отладочное событие от клиентской части фильтров."""

    event_type: str
    category: str | None = None
    value: str | None = None
    details: dict | None = None


# ----------------------------------------------------------------------
# Контейнер зависимостей
# ----------------------------------------------------------------------


class APIContext:
    """Контейнер зависимостей для API.

    Хранит ссылки на компоненты application layer, а также
    сервис получения справочников для фильтров и сервисы
    предпросмотра документа.

    Поля ``event_bus``, ``text_extractor``, ``word_index_cache``,
    ``highlights_service``, ``reference_data_service``,
    ``api_executor``, ``scan_executor`` объявлены как опциональные
    (``| None``): в тестах и сценариях, где функциональность
    предпросмотра/диагностики не нужна, допускается ``None``.
    Обработчики, требующие конкретную зависимость, обязаны
    проверить её наличие и вернуть HTTP 503 при отсутствии.
    """

    def __init__(
        self,
        search_engine: SearchEngine,
        scan_orchestrator: ScanOrchestrator,
        module_lifecycle: ModuleLifecycle,
        rd_directory: str,
        config_path: str = "config.json",
        event_bus: IEventBus | None = None,
        api_executor: Executor | None = None,
        scan_executor: Executor | None = None,
        reference_data_service: ReferenceDataService | None = None,
        text_extractor: ITextExtractor | None = None,
        word_index_cache: WordIndexCache | None = None,
        highlights_service: HighlightsService | None = None,
    ) -> None:
        self.search_engine = search_engine
        self.scan_orchestrator = scan_orchestrator
        self.module_lifecycle = module_lifecycle
        self.rd_directory = rd_directory
        self.config_path = config_path
        self.event_bus = event_bus
        self.api_executor = api_executor
        self.scan_executor = scan_executor
        self.reference_data_service = reference_data_service
        self.text_extractor = text_extractor
        self.word_index_cache = word_index_cache
        self.highlights_service = highlights_service
        self._scan_task: asyncio.Task[None] | None = None
        self._scan_lock = asyncio.Lock()

    def get_scan_task(self) -> asyncio.Task[None] | None:
        """Возвращает текущую задачу сканирования."""
        return self._scan_task

    @property
    def scan_lock(self) -> asyncio.Lock:
        """Асинхронная блокировка для сериализации запуска сканирования."""
        return self._scan_lock


def get_context(request: Request) -> APIContext:
    """Возвращает контекст API из ``request.app.state``.

    FastAPI dependency для доступа к :class:`APIContext`, созданному
    в ``lifespan`` при старте приложения. Значение сохраняется
    в ``app.state.api_context`` (см.
    ``dds_web/lifespan.py::create_app_with_lifespan``), что
    обеспечивает инверсию зависимостей: presentation layer
    (эндпоинты) получает контейнер через DI, а не через модульный
    глобал. Это устраняет скрытую глобальную связь и позволяет
    тестам подменять контекст через
    ``app.dependency_overrides[get_context]``.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Чтение ``request.app.state.api_context`` через      |
    |   | ``getattr(..., None)`` — защита от отсутствия       |
    |   | атрибута (например, при вызове эндпоинта без        |
    |   | lifespan в unit-тесте).                             |
    +---+-----------------------------------------------------+
    | 2 | Если значение ``None`` — ``HTTPException 503``.     |
    +---+-----------------------------------------------------+
    | 3 | Возврат контекста.                                  |
    +---+-----------------------------------------------------+

    Args:
        request: HTTP-запрос FastAPI. ``request.app.state`` —
            пространство имён приложения, заполняемое в lifespan.

    Returns:
        Экземпляр :class:`APIContext`, созданный при старте.

    Raises:
        HTTPException 503: Если приложение не инициализировано
            (lifespan не выполнен, либо API вызван до startup).
    """
    ctx = getattr(request.app.state, "api_context", None)
    if ctx is None:
        raise HTTPException(
            status_code=503,
            detail="Приложение не инициализировано.",
        )
    return ctx


CtxDep = Annotated[APIContext, Depends(get_context)]

# ----------------------------------------------------------------------
# Расширения протоколов
# ----------------------------------------------------------------------


@runtime_checkable
class _EventBusWithStats(Protocol):
    """Диагностическое расширение ``IEventBus``.

    Базовый протокол ``IEventBus`` описывает контракт публикации
    и подписки. Диагностика (``get_queue_stats``) — опциональная
    возможность конкретной реализации (``AsyncEventBus``), не
    входящая в базовый контракт. Этот расширенный Protocol
    позволяет проверить наличие метода через ``isinstance``,
    не сужая работу до конкретного класса шины.

    Используется в ``/api/diagnostics``.
    """

    def get_queue_stats(self) -> dict[str, int]:
        """Возвращает статистику очередей шины."""
        ...


# ----------------------------------------------------------------------
# Вспомогательные функции
# ----------------------------------------------------------------------


def _read_config_file(config_path: str) -> dict:
    """Читает файл конфигурации. Блокирующая операция."""
    try:
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_config_file(config_path: str, config_data: dict) -> None:
    """Атомарно записывает файл конфигурации. Блокирующая операция."""
    config_dir = os.path.dirname(os.path.abspath(config_path))
    os.makedirs(config_dir, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=config_dir,
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        json.dump(config_data, tmp, ensure_ascii=False, indent=4)
        tmp_path = tmp.name
    os.replace(tmp_path, config_path)


def _scan_themes_sync() -> list[str]:
    """Сканирует каталог тем и возвращает список имён.

    Блокирующая операция. Выполняется в отдельном потоке через
    ``run_in_executor``.

    Returns:
        Отсортированный список имён тем (без префикса ``theme-``
        и расширения ``.css``).
    """
    css_dir = Path(__file__).resolve().parent / "static" / "css"
    theme_pattern = re.compile(r"^theme-([a-zA-Z0-9-]+)\.css$")

    themes: list[str] = []
    for entry in os.listdir(css_dir):
        match = theme_pattern.match(entry)
        if match:
            themes.append(match.group(1))
    return sorted(themes)


def _require_event_bus(ctx: APIContext) -> IEventBus:
    """Возвращает шину событий из контекста или поднимает HTTP 503.

    ``APIContext.event_bus`` объявлен как ``IEventBus | None``:
    в production-пути ``lifespan.py`` всегда передаёт полноценную
    шину, но в тестах и сценариях, где функциональность шины не
    требуется, допускается ``None``. Обработчики, использующие
    ``run_with_timeout`` с ``event_bus=``, обязаны вызвать эту
    функцию в начале — тогда mypy видит non-None тип, а клиент
    получает осмысленный 503 вместо ``AttributeError``.

    Args:
        ctx: Контекст API.

    Returns:
        Шина событий (non-None).

    Raises:
        HTTPException 503: Если шина событий не инициализирована.
    """
    if ctx.event_bus is None:
        raise HTTPException(
            status_code=503,
            detail="Шина событий не инициализирована.",
        )
    return ctx.event_bus


def _resolve_document_file(ctx: APIContext, doc) -> Path:
    """Разрешает путь к файлу документа с защитой от path traversal.

    Используется эндпоинтами ``/download``, ``/render`` и
    ``/highlights`` для единообразной проверки: итоговый путь
    должен находиться внутри ``ctx.rd_directory``, и файл должен
    существовать.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Резолвинг ``ctx.rd_directory`` через ``Path.resolve``.|
    +---+-----------------------------------------------------+
    | 2 | Резолвинг ``base_dir / doc.file_path``.             |
    +---+-----------------------------------------------------+
    | 3 | Проверка ``is_relative_to(base_dir)`` — защита      |
    |   | от path traversal.                                  |
    +---+-----------------------------------------------------+
    | 4 | Проверка ``is_file()`` — файл существует.           |
    +---+-----------------------------------------------------+
    | 5 | Возврат абсолютного пути.                           |
    +---+-----------------------------------------------------+

    Args:
        ctx: Контекст API (для доступа к ``rd_directory``).
        doc: Метаданные документа (с полем ``file_path``).

    Returns:
        Абсолютный путь к файлу.

    Raises:
        HTTPException 403: Если путь выходит за пределы
            ``rd_directory``.
        HTTPException 404: Если файл не существует на диске.
    """
    base_dir = Path(ctx.rd_directory).resolve()
    file_path = (base_dir / doc.file_path).resolve()
    if not file_path.is_relative_to(base_dir):
        raise HTTPException(status_code=403, detail="Недопустимый путь к файлу")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден на диске")
    return file_path


def _build_or_get_word_index(
    ctx: APIContext,
    doc_id: str,
    page_number: int,
    file_hash: str,
    abs_path: Path,
) -> WordIndex:
    """Возвращает ``WordIndex`` из кэша или строит новый.

    Блокирующая операция: открывает PDF, парсит слова страницы,
    закрывает. Выполняется в ``scan_executor`` через
    ``run_in_executor``.

    Проверки предусловий (не ``assert``, а явные исключения —
    модуль может работать в production с ``python -O``, где
    ``assert`` отключается):

    - ``ctx.word_index_cache`` должен быть инициализирован;
    - ``ctx.text_extractor`` должен быть инициализирован.

    Если предусловие нарушено — возвращается ``HTTPException 503``.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Проверка наличия ``word_index_cache`` и             |
    |   | ``text_extractor``. Иначе — 503.                    |
    +---+-----------------------------------------------------+
    | 2 | Попытка получить индекс из ``word_index_cache``.    |
    +---+-----------------------------------------------------+
    | 3 | При попадании — возврат закэшированного индекса.    |
    +---+-----------------------------------------------------+
    | 4 | Иначе — открытие PDF через ``ctx.text_extractor``.  |
    +---+-----------------------------------------------------+
    | 5 | Построение индекса через ``document.build_word_index``.|
    +---+-----------------------------------------------------+
    | 6 | Сохранение индекса в кэш.                           |
    +---+-----------------------------------------------------+
    | 7 | Возврат индекса.                                    |
    +---+-----------------------------------------------------+

    Args:
        ctx: Контекст API (доступ к ``word_index_cache`` и
            ``text_extractor``).
        doc_id: Идентификатор документа.
        page_number: Номер страницы (0-based).
        file_hash: Хеш файла — часть ключа кэша, обеспечивает
            автоинвалидацию при переиндексации.
        abs_path: Абсолютный путь к PDF-файлу.

    Returns:
        :class:`WordIndex` с нормализованными словами страницы.

    Raises:
        HTTPException 503: Если ``word_index_cache`` или
            ``text_extractor`` не инициализированы.
        HTTPException 500: Если PDF не открывается или парсинг
            страницы падает.
        HTTPException 400: Если номер страницы вне диапазона
            (пробрасывается из ``build_word_index``).
    """
    if ctx.word_index_cache is None or ctx.text_extractor is None:
        raise HTTPException(status_code=503, detail="Подсветка недоступна.")

    cached = ctx.word_index_cache.get(doc_id, page_number, file_hash)
    if cached is not None:
        return cached

    try:
        document = ctx.text_extractor.open_document(str(abs_path))
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось открыть документ: {e}",
        ) from e

    try:
        index = document.build_word_index(page_number)
    except IndexError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Ошибка парсинга страницы: {e}",
        ) from e
    finally:
        document.close()

    ctx.word_index_cache.put(doc_id, page_number, file_hash, index)
    return index


def _on_scan_done(
    ctx: APIContext,
) -> Callable[[asyncio.Future[Any]], None]:
    """Возвращает callback завершения задачи сканирования.

    Фабрика callback'а для ``asyncio.Task.add_done_callback``.
    Возвращаемая функция проверяет, что завершилась именно
    текущая задача контекста (а не устаревшая), сбрасывает
    ссылку ``ctx._scan_task`` и извлекает исключение (если было),
    чтобы asyncio не логировал его как «необработанное».

    Args:
        ctx: Контекст API.

    Returns:
        Callable, совместимый с ``add_done_callback``: принимает
        завершившийся :class:`asyncio.Future`, не возвращает
        значимого значения.
    """

    def _callback(task: asyncio.Future[Any]) -> None:
        if ctx._scan_task is not task:
            return
        ctx._scan_task = None
        if task.cancelled():
            return
        # Извлечение исключения подавляет вывод «Task exception was
        # never retrieved» в логах asyncio. Обработка ошибки — в
        # самом корутине _scan_coroutine (публикация событий).
        _ = task.exception()

    return _callback


# ----------------------------------------------------------------------
# API Router
# ----------------------------------------------------------------------

router = APIRouter(prefix="/api", tags=["DDS API"])

# ----------------------------------------------------------------------
# Отладочный эндпоинт
# ----------------------------------------------------------------------


@router.post("/debug/filter-events")
async def debug_filter_events(event: DebugEvent) -> dict[str, str]:
    """Отладочный эндпоинт: печатает события фильтров в stdout сервера."""
    print(
        f"[DEBUG FILTER] type={event.event_type} "
        f"category={event.category or '-'} "
        f"value={event.value or '-'} "
        f"details={event.details or {}}"
    )
    return {"status": "ok"}


# ----------------------------------------------------------------------
# Аутентификация: смена пароля
# ----------------------------------------------------------------------


@router.post("/auth/change-password", response_model=ChangePasswordResponse)
async def change_password(
    request_body: ChangePasswordRequest,
    *,
    auth: AuthDep,
    ctx: CtxDep,
) -> ChangePasswordResponse:
    """Меняет пароль аутентифицированного пользователя."""
    event_bus = _require_event_bus(ctx)
    loop = asyncio.get_running_loop()
    try:
        await run_with_timeout(
            loop.run_in_executor(
                ctx.api_executor,
                auth.change_password,
                request_body.username,
                request_body.old_password,
                request_body.new_password,
            ),
            operation="auth.login",
            event_bus=event_bus,
            context=request_body.username,
            recovery_action="Смена пароля прервана по таймауту.",
        )
    except TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="Сервер перегружен. Попробуйте позже.",
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e),
        )
    return ChangePasswordResponse(message="Пароль изменён.")


# ----------------------------------------------------------------------
# Поиск
# ----------------------------------------------------------------------


@router.get("/search", response_model=SearchResponse)
async def search(
    q: str | None = Query(default=None, description="Поисковый запрос (может быть пустым)"),
    file_path: str | None = Query(default=None, description="Фильтр по пути файла (SQL LIKE)"),
    object_code: str | None = Query(default=None, description="Фильтр по коду объекта"),
    discipline_code: str | None = Query(default=None, description="Фильтр по коду дисциплины"),
    document_type_code: str | None = Query(
        default=None, description="Фильтр по коду типа документа"
    ),
    unmatched_only: bool = Query(default=False, description="Только несоответствующие документы"),
    limit: int = Query(
        default=50,
        ge=1,
        le=500,
        description="Максимальное количество документов",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Смещение по документам для постраничного вывода",
    ),
    *,
    ctx: CtxDep,
) -> SearchResponse:
    """Выполняет полнотекстовый поиск с серверной группировкой.

    Результаты группируются по документу: одна запись на документ
    с вложенным массивом страниц. Поле ``total`` в ответе — это
    количество уникальных документов (не страниц). Параметры
    ``limit`` и ``offset`` также отсчитываются по документам.

    Каждая страница в ``results[i].pages`` содержит поле
    ``terms`` — список уникальных терминов подсветки, извлечённых
    сервером из сниппета (см. ADR-006). Клиент использует готовый
    список для запроса подсветки и не парсит сниппеты.

    Если ``q`` пустой или не указан, поиск выполняется только
    по фильтрам (или по всем документам, если фильтров нет).
    В этом случае каждый документ содержит одну виртуальную
    страницу (``page_number=0``, пустой сниппет, ``terms=[]``).
    """
    event_bus = _require_event_bus(ctx)

    # Нормализация запроса: пустая строка вместо None
    query = q.strip() if q and q.strip() else ""

    filters = SearchFilters(
        file_path_pattern=file_path,
        object_code=object_code,
        discipline_code=discipline_code,
        document_type_code=document_type_code,
        unmatched_only=unmatched_only,
    )

    loop = asyncio.get_running_loop()
    try:
        results, total = await run_with_timeout(
            loop.run_in_executor(
                ctx.api_executor,
                lambda: ctx.search_engine.search_with_count(
                    query=query,
                    filters=filters,
                    limit=limit,
                    offset=offset,
                ),
            ),
            operation="search.query",
            event_bus=event_bus,
            context=query or "(пустой запрос)",
            recovery_action="Поиск прерван по таймауту.",
        )
    except TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="Поиск не завершился. Попробуйте позже.",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Строим ответ через Pydantic-модели (валидация + корректная
    # генерация OpenAPI-схемы).
    items: list[SearchResultItem] = [
        SearchResultItem(
            doc_id=r.doc_id,
            file_path=r.file_path,
            relevance_score=r.relevance_score,
            pages=[
                PageHitResponse(
                    page_number=p.page_number,
                    snippet=p.snippet,
                    terms=list(p.terms),
                )
                for p in r.pages
            ],
        )
        for r in results
    ]

    return SearchResponse(
        query=query,
        total=total,
        results=items,
    )


# ----------------------------------------------------------------------
# Темы оформления
# ----------------------------------------------------------------------


@router.get("/themes", response_model=list[str])
async def list_themes(
    *,
    ctx: CtxDep,
) -> list[str]:
    """Возвращает список доступных тем оформления.

    Сканирует каталог ``dds_web/static/css`` на наличие файлов,
    соответствующих шаблону ``theme-<имя>.css``, и возвращает
    отсортированный список имён тем. Сканирование каталога
    выполняется в отдельном потоке через ``run_in_executor``,
    чтобы не блокировать event loop.

    Returns:
        Список имён тем (без префикса ``theme-`` и расширения ``.css``).
        Например: ``["dark", "purple"]``.

    Raises:
        HTTPException 500: Если каталог тем недоступен или произошла
            ошибка при сканировании.
    """
    loop = asyncio.get_running_loop()
    try:
        themes = await loop.run_in_executor(ctx.api_executor, _scan_themes_sync)
        return themes
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось просканировать каталог тем: {e}",
        )


# ----------------------------------------------------------------------
# Справочники для фильтров
# ----------------------------------------------------------------------


@router.get("/filters/metadata")
async def get_filter_metadata(
    *,
    ctx: CtxDep,
) -> dict:
    """Возвращает справочники кодов и псевдонимов для фильтров."""
    if ctx.reference_data_service is None:
        raise HTTPException(status_code=503, detail="Сервис справочников не инициализирован.")

    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(
            ctx.api_executor,
            ctx.reference_data_service.get_references,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка получения справочников: {e}")

    return data


# ----------------------------------------------------------------------
# Документы
# ----------------------------------------------------------------------


@router.get("/documents/{doc_id}", response_model=DocumentResponse)
async def get_document(
    doc_id: str,
    *,
    ctx: CtxDep,
) -> DocumentResponse:
    """Возвращает метаданные документа."""
    loop = asyncio.get_running_loop()
    doc = await loop.run_in_executor(
        ctx.api_executor,
        ctx.search_engine.get_document_info,
        doc_id,
    )
    if doc is None:
        raise HTTPException(
            status_code=404,
            detail=f"Документ не найден: {doc_id}",
        )
    return DocumentResponse(
        doc_id=doc.doc_id,
        file_path=doc.file_path,
        file_hash=doc.file_hash,
        file_size=doc.file_size,
        page_count=doc.page_count,
        indexed_at=doc.indexed_at,
        last_modified=doc.last_modified,
    )


@router.get(
    "/documents/{doc_id}/pages/{page_number}",
    response_model=PageTextResponse,
)
async def get_page_text(
    doc_id: str,
    page_number: int,
    *,
    ctx: CtxDep,
) -> PageTextResponse:
    """Возвращает текст страницы документа.

    Обёрнут в ``run_with_timeout`` с дескриптором
    ``document.page_load`` (шаг 4.1 плана v5.0). При срабатывании
    таймаута публикуется ``OperationTimedOut`` (WARNING), клиенту
    возвращается HTTP 504.

    Вызов ``search_engine.get_page_text`` передаёт ``doc.file_hash``
    как часть ключа LRU-кэша (шаг 3.2): это обеспечивает
    автоинвалидацию кэша при переиндексации файла.
    """
    event_bus = _require_event_bus(ctx)
    loop = asyncio.get_running_loop()
    doc = await loop.run_in_executor(
        ctx.api_executor,
        ctx.search_engine.get_document_info,
        doc_id,
    )
    if doc is None:
        raise HTTPException(
            status_code=404,
            detail=f"Документ не найден: {doc_id}",
        )
    if page_number < 0 or page_number >= doc.page_count:
        raise HTTPException(
            status_code=400,
            detail=f"Номер страницы вне диапазона: 0–{doc.page_count - 1}",
        )

    try:
        text = await run_with_timeout(
            loop.run_in_executor(
                ctx.api_executor,
                ctx.search_engine.get_page_text,
                doc_id,
                page_number,
                doc.file_hash,
            ),
            operation="document.page_load",
            event_bus=event_bus,
            context=f"{doc_id}:{page_number}",
            recovery_action="Загрузка текста страницы прервана.",
        )
    except TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="Загрузка текста страницы не завершилась. Попробуйте позже.",
        )

    return PageTextResponse(
        doc_id=doc_id,
        page_number=page_number,
        text=text,
    )


@router.post(
    "/documents/pages/batch",
    response_model=PageTextBatchResponse,
)
async def get_pages_text_batch(
    request_body: PageTextBatchRequest,
    *,
    ctx: CtxDep,
) -> PageTextBatchResponse:
    """Пакетная загрузка текста страницы для нескольких документов.

    Используется UI при пустом поисковом запросе (режим «показать
    все»): вместо N HTTP-запросов к
    ``/api/documents/{id}/pages/{n}`` клиент отправляет один POST.

    Порядок обработки одного документа:

    1. ``get_document_info`` — получение метаданных (включая
       ``file_hash`` и ``page_count``).
    2. Проверка диапазона ``page_number``.
    3. ``get_page_text`` — чтение текста с ``file_hash`` в ключе
       LRU-кэша (шаг 3.2).

    Последовательный вызов ``get_document_info`` → ``get_page_text``
    для одного документа гарантирует, что ``file_hash``,
    переданный в кэш, соответствует тексту (нет race condition
    с переиндексацией). Между разными документами шаги выполняются
    параллельно через ``asyncio.gather``.

    Чтение текста обёрнуто в ``run_with_timeout`` с дескриптором
    ``document.page_load``. При таймауте для конкретного документа
    его идентификатор попадает в ``errors``, остальные документы
    обрабатываются независимо.

    Документы, отсутствующие в БД, и страницы вне диапазона
    попадают в ``errors`` (не в ``pages``).

    Исполнитель: ``api_executor`` (чтение из БД, не CPU-интенсивное;
    согласовано с одиночным GET-эндпоинтом ``/documents/{id}/pages/{n}``).
    """
    event_bus = _require_event_bus(ctx)
    loop = asyncio.get_running_loop()
    pages: dict[str, str] = {}
    errors: dict[str, str] = {}

    async def load_one(doc_id: str) -> None:
        """Обрабатывает один документ: метаданные → текст."""
        doc = await loop.run_in_executor(
            ctx.api_executor,
            ctx.search_engine.get_document_info,
            doc_id,
        )
        if doc is None:
            errors[doc_id] = "Документ не найден"
            return
        if request_body.page_number >= doc.page_count:
            errors[doc_id] = "Страница вне диапазона"
            return
        try:
            text = await run_with_timeout(
                loop.run_in_executor(
                    ctx.api_executor,
                    ctx.search_engine.get_page_text,
                    doc_id,
                    request_body.page_number,
                    doc.file_hash,
                ),
                operation="document.page_load",
                event_bus=event_bus,
                context=f"{doc_id}:{request_body.page_number}",
                recovery_action="Загрузка текста страницы прервана.",
            )
            pages[doc_id] = text
        except TimeoutError:
            errors[doc_id] = "Таймаут загрузки"

    results = await asyncio.gather(
        *[load_one(d) for d in request_body.doc_ids],
        return_exceptions=True,
    )
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            errors[request_body.doc_ids[i]] = str(r)

    return PageTextBatchResponse(pages=pages, errors=errors)


@router.get("/documents/{doc_id}/download")
async def download_document(
    doc_id: str,
    *,
    ctx: CtxDep,
) -> FileResponse:
    """Скачивает исходный PDF-файл документа.

    Файл ищется в каталоге РД, указанном в конфигурации.
    Авторизация не требуется.

    Защита от path traversal обеспечивается общей функцией
    ``_resolve_document_file``: итоговый путь должен находиться
    внутри ``rd_directory``.
    """
    loop = asyncio.get_running_loop()
    doc = await loop.run_in_executor(
        ctx.api_executor,
        ctx.search_engine.get_document_info,
        doc_id,
    )
    if doc is None:
        raise HTTPException(
            status_code=404,
            detail=f"Документ не найден: {doc_id}",
        )

    file_path = _resolve_document_file(ctx, doc)

    media_type, _ = mimetypes.guess_type(str(file_path))
    if media_type is None:
        media_type = "application/octet-stream"

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=file_path.name,
        content_disposition_type="attachment",
    )


@router.get("/documents/{doc_id}/pages/{page_number}/render")
async def render_document_page(
    doc_id: str,
    page_number: int,
    dpi: int = Query(default=300, ge=72, le=300),
    *,
    ctx: CtxDep,
) -> Response:
    """Возвращает PNG-рендер страницы документа.

    Рендер выполняется в ``scan_executor`` (не в ``api_executor``),
    чтобы не блокировать пул веб-запросов. Обёрнут в
    ``run_with_timeout`` с дескриптором ``render.page``.

    HTTP-кэш:

    - ``ETag: "<file_hash>-<page_number>-<dpi>"`` — при
      переиндексации файла ``file_hash`` меняется, кэш браузера
      инвалидируется автоматически.
    - ``Cache-Control: private, max-age=86400`` — приватный кэш
      браузера на 24 часа.

    Args:
        doc_id: Идентификатор документа.
        page_number: Номер страницы (0-based).
        dpi: Разрешение рендера (72..300).

    Returns:
        ``image/png`` с заголовками ``ETag`` и ``Cache-Control``.

    Raises:
        HTTPException 404: Документ или файл не найден.
        HTTPException 400: Номер страницы вне диапазона.
        HTTPException 500: Ошибка рендера.
        HTTPException 503: ``text_extractor`` не инициализирован.
        HTTPException 504: Таймаут рендера.
    """
    event_bus = _require_event_bus(ctx)
    loop = asyncio.get_running_loop()
    doc = await loop.run_in_executor(
        ctx.api_executor,
        ctx.search_engine.get_document_info,
        doc_id,
    )
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Документ не найден: {doc_id}")
    if page_number < 0 or page_number >= doc.page_count:
        raise HTTPException(
            status_code=400,
            detail=f"Номер страницы вне диапазона: 0–{doc.page_count - 1}",
        )
    abs_path = _resolve_document_file(ctx, doc)

    # Сужение типа: mypy не сохраняет narrowing атрибута ``ctx.text_extractor``
    # внутри вложенной функции ``_do_render``. Копируем в локальную
    # переменную — после ``is None``-проверки она имеет тип non-None.
    text_extractor = ctx.text_extractor
    if text_extractor is None:
        raise HTTPException(status_code=503, detail="Рендер недоступен.")

    def _do_render() -> bytes:
        document = text_extractor.open_document(str(abs_path))
        try:
            return document.render_page(page_number, dpi)
        finally:
            document.close()

    try:
        png_bytes = await run_with_timeout(
            loop.run_in_executor(ctx.scan_executor, _do_render),
            operation="render.page",
            event_bus=event_bus,
            context=f"{doc_id}:{page_number}",
            recovery_action="Рендер прерван по таймауту.",
        )
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Рендер не завершился.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка рендера: {e}") from e

    etag = f'"{doc.file_hash}-{page_number}-{dpi}"'
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={
            "ETag": etag,
            "Cache-Control": "private, max-age=86400",
        },
    )


@router.post(
    "/documents/{doc_id}/pages/{page_number}/highlights",
    response_model=HighlightsResponse,
)
async def get_page_highlights(
    doc_id: str,
    page_number: int,
    request_body: HighlightsRequest,
    *,
    ctx: CtxDep,
) -> HighlightsResponse:
    """Возвращает прямоугольники подсветки для терминов на странице.

    Логика:

    1. Валидация документа и страницы.
    2. Проверка наличия всех необходимых сервисов.
    3. Получение или построение ``WordIndex`` (кэш в ``scan_executor``).
    4. Поиск терминов в нормализованном пространстве с опциональной
       диагностикой и коррекцией системы координат.
    5. При уверенности диагностики ``"high"`` или ``"medium"``
       публикуется событие ``CoordinateSystemAnomalyDetected``.
    6. Возврат списка bbox-ов в нормализованных координатах.

    Ответ **не кэшируется** на уровне HTTP: тело запроса меняется.
    Внутренний кэш ``WordIndexCache`` ускоряет повторные запросы.

    Raises:
        HTTPException 404: Документ или файл не найден.
        HTTPException 400: Номер страницы вне диапазона.
        HTTPException 422: Термин слишком длинный или содержит
            более 20 слов.
        HTTPException 500: Ошибка парсинга PDF.
        HTTPException 503: Сервис подсветки или ``text_extractor``
            не инициализирован.
        HTTPException 504: Таймаут построения индекса.
    """
    event_bus = _require_event_bus(ctx)
    loop = asyncio.get_running_loop()
    doc = await loop.run_in_executor(
        ctx.api_executor,
        ctx.search_engine.get_document_info,
        doc_id,
    )
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Документ не найден: {doc_id}")
    if page_number < 0 or page_number >= doc.page_count:
        raise HTTPException(
            status_code=400,
            detail=f"Номер страницы вне диапазона: 0–{doc.page_count - 1}",
        )
    abs_path = _resolve_document_file(ctx, doc)

    # Проверка наличия всех трёх сервисов, необходимых для подсветки.
    # Отсутствие любого из них — 503, а не падение с AttributeError.
    if ctx.word_index_cache is None or ctx.highlights_service is None or ctx.text_extractor is None:
        raise HTTPException(status_code=503, detail="Подсветка недоступна.")

    try:
        index = await run_with_timeout(
            loop.run_in_executor(
                ctx.scan_executor,
                _build_or_get_word_index,
                ctx,
                doc_id,
                page_number,
                doc.file_hash,
                abs_path,
            ),
            operation="render.highlights",
            event_bus=event_bus,
            context=f"{doc_id}:{page_number}",
            recovery_action="Подсветка недоступна.",
        )
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Подсветка не завершилась.")

    highlights, applied_transform, transform_confidence = ctx.highlights_service.search_highlights(
        index,
        request_body.terms,
        request_body.apply_transform,
    )

    # Публикация события об аномалии для аудита.
    if transform_confidence in ("high", "medium"):
        event_bus.publish(
            CoordinateSystemAnomalyDetected(
                correlation_id="",
                source="API",
                stage="highlights",
                doc_id=doc_id,
                page_number=page_number,
                confidence=transform_confidence,
            )
        )

    boxes = [HighlightBox(x=h.x, y=h.y, w=h.w, h=h.h, term=h.term) for h in highlights]
    return HighlightsResponse(
        highlights=boxes,
        applied_transform=applied_transform,
        transform_confidence=transform_confidence,
    )


@router.delete("/documents/{doc_id}")
async def delete_document(
    doc_id: str,
    confirm: bool = Query(
        default=False,
        description="Подтверждение удаления",
    ),
    *,
    ctx: CtxDep,
    session: AdminSessionDep,
) -> dict:
    """Удаляет документ из индекса (только для администраторов).

    После удаления документа из БД инвалидируется LRU-кэш текста
    страниц (шаг 3.2 плана v5.0) через
    ``search_engine.invalidate_page_text_cache(doc_id)``. Это
    необходимо, так как после удаления ``file_hash`` документа
    более не доступен, и автоинвалидация по ключу кэша не
    сработает: оставшиеся записи могли бы быть возвращены
    при последующих обращениях.
    """
    # ``session`` используется как зависимость FastAPI для проверки
    # прав администратора; сама переменная не требуется в теле.
    _ = session

    if not confirm:
        return {
            "message": (
                f"Документ {doc_id} будет удалён из индекса. "
                f"Файл на диске останется. "
                f"Передайте confirm=true для подтверждения."
            ),
            "confirmed": False,
        }

    event_bus = _require_event_bus(ctx)
    loop = asyncio.get_running_loop()
    current_status = await loop.run_in_executor(
        ctx.api_executor,
        ctx.scan_orchestrator.get_scan_status,
    )
    if current_status.status.value == "running":
        raise HTTPException(
            status_code=409,
            detail="Удаление недоступно во время сканирования.",
        )

    try:
        await run_with_timeout(
            loop.run_in_executor(
                ctx.scan_executor,
                ctx.scan_orchestrator.remove_document,
                doc_id,
            ),
            operation="document.delete",
            event_bus=event_bus,
            context=doc_id,
            recovery_action="Удаление прервано по таймауту.",
        )
    except TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="Удаление документа не завершилось.",
        )

    # Инвалидация LRU-кэша текста страниц для удалённого документа.
    # См. docstring выше.
    ctx.search_engine.invalidate_page_text_cache(doc_id)

    return {"message": f"Документ {doc_id} удалён из индекса.", "confirmed": True}


# ----------------------------------------------------------------------
# Сканирование
# ----------------------------------------------------------------------


@router.get("/scan/status", response_model=ScanStatusResponse)
async def get_scan_status(
    *,
    ctx: CtxDep,
) -> ScanStatusResponse:
    """Возвращает статус текущего или последнего сканирования."""
    loop = asyncio.get_running_loop()
    progress = await loop.run_in_executor(
        ctx.api_executor,
        ctx.scan_orchestrator.get_scan_status,
    )
    return ScanStatusResponse(
        phase=progress.phase.value,
        status=progress.status.value,
        total_files=progress.total_files,
        processed_files=progress.processed_files,
        current_file=progress.current_file,
        progress_percent=progress.progress_percent,
        indexed_files=progress.indexed_files,
        duplicate_files=progress.duplicate_files,
        skipped_files=progress.skipped_files,
        error_files=progress.error_files,
    )


@router.post("/scan/start", response_model=ScanStartResponse)
async def start_scan(
    *,
    ctx: CtxDep,
) -> ScanStartResponse:
    """Запускает сканирование каталога РД в основном event loop."""
    async with ctx.scan_lock:
        if ctx._scan_task is not None and not ctx._scan_task.done():
            raise HTTPException(
                status_code=409,
                detail="Сканирование уже выполняется.",
            )

        loop = asyncio.get_running_loop()
        current_status = await loop.run_in_executor(
            ctx.api_executor,
            ctx.scan_orchestrator.get_scan_status,
        )
        if current_status.status.value == "running":
            raise HTTPException(
                status_code=409,
                detail="Сканирование уже выполняется.",
            )

        correlation_id = str(uuid.uuid4())

        async def _scan_coroutine() -> None:
            try:
                progress = await ctx.scan_orchestrator.run_primary_scan(
                    ctx.rd_directory,
                    correlation_id=correlation_id,
                )
            except asyncio.CancelledError:
                return

            if progress.status == ScanStatus.COMPLETED:
                scan_loop = asyncio.get_running_loop()
                await scan_loop.run_in_executor(
                    ctx.scan_executor,
                    ctx.scan_orchestrator.run_secondary_scans,
                )

        task: asyncio.Task[None] = asyncio.create_task(_scan_coroutine())
        ctx._scan_task = task
        task.add_done_callback(_on_scan_done(ctx))
        task_id = str(uuid.uuid4())

    return ScanStartResponse(
        task_id=task_id,
        message="Сканирование запущено.",
    )


@router.post("/scan/cancel")
async def cancel_scan(
    *,
    ctx: CtxDep,
) -> dict:
    """Отменяет текущее сканирование."""
    task = ctx.get_scan_task()
    if task is None or task.done():
        raise HTTPException(
            status_code=409,
            detail="Нет активного сканирования для отмены.",
        )

    ctx.scan_orchestrator.cancel_scan()
    task.cancel()

    return {"message": "Отмена сканирования запрошена."}


@router.post("/scan/refresh-metadata")
async def refresh_document_metadata(
    *,
    ctx: CtxDep,
    session: AdminSessionDep,
) -> dict:
    """Запускает повторное обновление метаданных документов."""
    _ = session
    loop = asyncio.get_running_loop()
    try:
        count = await loop.run_in_executor(
            ctx.scan_executor,
            ctx.scan_orchestrator.refresh_document_metadata,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка обновления метаданных: {e}")

    return {
        "message": "Метаданные документов обновлены.",
        "processed": count,
    }


# ----------------------------------------------------------------------
# Прогресс сканирования (SSE через события)
# ----------------------------------------------------------------------


@router.get("/scan/progress")
async def scan_progress_sse(
    request: Request,
    *,
    ctx: CtxDep,
) -> StreamingResponse:
    """Возвращает прогресс сканирования через SSE (Server-Sent Events).

    Подписывается на события первичного и вторичного сканирования:
    ``scan.progress``, ``scan.completed``, ``scan.secondary.started``,
    ``scan.secondary.progress``, ``scan.secondary.completed``.

    После отправки начального состояния проверяется наличие активной
    задачи сканирования. Если задача отсутствует или завершена,
    поток закрывается, не оставляя открытых соединений.

    Примечание по ``event_bus``:
        В отличие от большинства обработчиков, SSE-поток **не
        использует** :func:`_require_event_bus`: если шина не
        инициализирована, клиент получает пустой поток (одно
        событие ``{}`` и закрытие), а не 503. Это осознанное
        решение: SSE — длительное соединение, и 503 после
        установки соединения бесполезен для клиента.
    """
    # Захват шины в локальную переменную до создания замыкания:
    # mypy не сохраняет narrowing ``ctx.event_bus`` внутри
    # вложенной корутины, но локальная переменная имеет явный тип.
    event_bus: IEventBus | None = ctx.event_bus

    async def event_generator():
        if event_bus is None:
            yield "data: {}\n\n"
            return

        # Отправка начального состояния сканирования
        loop = asyncio.get_running_loop()
        progress = await loop.run_in_executor(
            ctx.api_executor,
            ctx.scan_orchestrator.get_scan_status,
        )
        initial_data = json.dumps(
            {
                "scan_id": 0,
                "phase": progress.phase.value,
                "processed_files": progress.processed_files,
                "total_files": progress.total_files,
                "current_file": progress.current_file,
                "status": progress.status.value,
                "progress_percent": progress.progress_percent,
                "indexed_files": progress.indexed_files,
                "duplicate_files": progress.duplicate_files,
                "skipped_files": progress.skipped_files,
                "error_files": progress.error_files,
            }
        )
        yield f"data: {initial_data}\n\n"

        # Проверка: если задача сканирования не активна (нет незавершённой задачи),
        # завершаем поток. Это покрывает и первичное, и вторичное сканирование.
        scan_task = ctx.get_scan_task()
        if scan_task is None or scan_task.done():
            return

        # Подписка на все события сканирования
        subscription = event_bus.subscribe(
            event_types={
                "scan.progress",
                "scan.completed",
                "scan.secondary.started",
                "scan.secondary.progress",
                "scan.secondary.completed",
            },
            max_queue_size=10,
        )
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(
                        subscription.get(),
                        timeout=core_config.OPERATION_TIMEOUTS["sse.subscription_wait"],
                    )
                except TimeoutError:
                    # Heartbeat
                    yield 'data: {"heartbeat": true}\n\n'
                    continue

                # Сериализация событий в JSON
                if isinstance(event, ScanProgressUpdated):
                    data = json.dumps(
                        {
                            "scan_id": event.scan_id,
                            "phase": event.phase,
                            "processed_files": event.processed_files,
                            "total_files": event.total_files,
                            "current_file": event.current_file,
                            "status": event.status,
                            "progress_percent": (
                                (event.processed_files / event.total_files * 100)
                                if event.total_files > 0
                                else 0.0
                            ),
                            "indexed_files": event.indexed_files,
                            "duplicate_files": event.duplicate_files,
                            "skipped_files": event.skipped_files,
                            "error_files": event.error_files,
                        }
                    )
                elif isinstance(event, ScanCompleted):
                    data = json.dumps(
                        {
                            "scan_id": event.scan_id,
                            "phase": event.phase,
                            "status": event.status,
                            "processed_files": event.processed_files,
                            "total_files": event.total_files,
                            "indexed_files": event.indexed_files,
                            "duplicate_files": event.duplicate_files,
                            "skipped_files": event.skipped_files,
                            "error_files": event.error_files,
                        }
                    )
                elif isinstance(event, ScanSecondaryStarted):
                    data = json.dumps(
                        {
                            "scan_id": event.scan_id,
                            "phase": "secondary",
                            "status": "running",
                            "stage": "started",
                            "rd_directory": event.rd_directory,
                        }
                    )
                elif isinstance(event, ScanSecondaryProgressUpdated):
                    data = json.dumps(
                        {
                            "scan_id": event.scan_id,
                            "phase": "secondary",
                            "status": "running",
                            "stage": event.stage,
                            "processed_documents": event.processed_documents,
                            "total_documents": event.total_documents,
                            "module_name": event.module_name,
                            "progress_percent": event.progress_percent,
                        }
                    )
                elif isinstance(event, ScanSecondaryCompleted):
                    data = json.dumps(
                        {
                            "scan_id": event.scan_id,
                            "phase": "secondary",
                            "status": event.status,
                            "stage": "completed",
                            "processed_documents": event.processed_documents,
                            "total_documents": event.total_documents,
                            "errors": event.errors,
                        }
                    )
                else:
                    data = json.dumps(event.to_dict())
                yield f"data: {data}\n\n"

                # Завершение потока после завершения вторичного сканирования
                if isinstance(event, (ScanCompleted, ScanSecondaryCompleted)):
                    break
        finally:
            subscription.unsubscribe()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ----------------------------------------------------------------------
# Состояние индексации
# ----------------------------------------------------------------------


@router.get("/index/status", response_model=IndexStatusResponse)
async def get_index_status(
    refresh: bool = Query(
        default=False,
        description="Принудительный пересчёт состояния индексации.",
    ),
    *,
    ctx: CtxDep,
) -> IndexStatusResponse:
    """Возвращает фактическое состояние индексации системы."""
    event_bus = _require_event_bus(ctx)
    try:
        loop = asyncio.get_running_loop()
        if refresh:
            current_status = await loop.run_in_executor(
                ctx.api_executor,
                ctx.scan_orchestrator.get_scan_status,
            )
            if current_status.status.value == "running":
                raise HTTPException(
                    status_code=409,
                    detail="Пересчёт состояния недоступен во время сканирования.",
                )

            try:
                index_status = await run_with_timeout(
                    loop.run_in_executor(
                        ctx.scan_executor,
                        ctx.scan_orchestrator.refresh_index_status,
                        ctx.rd_directory,
                    ),
                    operation="index.refresh",
                    event_bus=event_bus,
                    context=ctx.rd_directory,
                    recovery_action="Пересчёт прерван по таймауту.",
                )
            except TimeoutError:
                raise HTTPException(
                    status_code=504,
                    detail="Пересчёт не завершился. Попробуйте позже.",
                )
        else:
            index_status = await loop.run_in_executor(
                ctx.api_executor,
                ctx.scan_orchestrator.get_cached_index_status,
                ctx.rd_directory,
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось получить состояние индексации: {e}",
        )

    return IndexStatusResponse(
        state=index_status.state.value,
        total_files_in_directory=index_status.total_files_in_directory,
        indexed_files=index_status.indexed_files,
        duplicate_files_on_disk=index_status.duplicate_files_on_disk,
        new_files=index_status.new_files,
        modified_files=index_status.modified_files,
        unindexed_files=index_status.unindexed_files,
        search_available=index_status.search_available,
        message=index_status.message,
        progress_percent=index_status.progress_percent,
        is_fully_indexed=index_status.is_fully_indexed,
    )


# ----------------------------------------------------------------------
# Модули
# ----------------------------------------------------------------------


@router.get("/modules", response_model=ModuleListResponse)
async def list_modules(
    *,
    ctx: CtxDep,
) -> ModuleListResponse:
    """Возвращает список всех модулей из реестра."""
    loop = asyncio.get_running_loop()
    infos = await loop.run_in_executor(
        ctx.api_executor,
        ctx.module_lifecycle.get_all_module_info,
    )
    modules: list[ModuleInfoResponse] = []
    for info in infos.values():
        modules.append(
            ModuleInfoResponse(
                module_name=info.module_name,
                version=info.version,
                api_version=info.api_version,
                status=info.status.value,
                description=info.description,
                scan_phase=info.scan_phase.value,
            )
        )
    return ModuleListResponse(modules=modules)


@router.get("/modules/{module_name}", response_model=ModuleInfoResponse)
async def get_module_status(
    module_name: str,
    *,
    ctx: CtxDep,
) -> ModuleInfoResponse:
    """Возвращает статус модуля."""
    loop = asyncio.get_running_loop()
    infos = await loop.run_in_executor(
        ctx.api_executor,
        ctx.module_lifecycle.get_all_module_info,
    )
    info = infos.get(module_name)
    if info is None or info.status == ModuleStatus.REMOVED:
        raise HTTPException(
            status_code=404,
            detail=f"Модуль не найден: {module_name}",
        )
    return ModuleInfoResponse(
        module_name=info.module_name,
        version=info.version,
        api_version=info.api_version,
        status=info.status.value,
        description=info.description,
        scan_phase=info.scan_phase.value,
    )


# ----------------------------------------------------------------------
# Настройки (только для администраторов)
# ----------------------------------------------------------------------


@router.get("/settings", response_model=SettingsResponse)
async def get_settings(
    *,
    ctx: CtxDep,
    session: AdminSessionDep,
) -> SettingsResponse:
    """Возвращает текущие настройки DDS."""
    _ = session
    loop = asyncio.get_running_loop()
    config = await loop.run_in_executor(
        ctx.api_executor,
        _read_config_file,
        ctx.config_path,
    )
    dds_config = config.get("dds", {})
    return SettingsResponse(
        rd_directory=dds_config.get("rd_directory", ctx.rd_directory),
        db_path=dds_config.get("db_path", "dds_database.db"),
        modules_directory=dds_config.get("modules_directory", "dds_modules"),
    )


@router.post("/settings", response_model=SettingsUpdateResponse)
async def update_settings(
    request: SettingsUpdateRequest,
    *,
    ctx: CtxDep,
    session: AdminSessionDep,
) -> SettingsUpdateResponse:
    """Обновляет настройки DDS."""
    _ = session
    loop = asyncio.get_running_loop()

    current_status = await loop.run_in_executor(
        ctx.api_executor,
        ctx.scan_orchestrator.get_scan_status,
    )
    if current_status.status.value == "running":
        raise HTTPException(
            status_code=409,
            detail="Изменение настроек недоступно во время сканирования.",
        )

    config = await loop.run_in_executor(
        ctx.api_executor,
        _read_config_file,
        ctx.config_path,
    )
    if not config:
        raise HTTPException(
            status_code=500,
            detail="Файл конфигурации повреждён.",
        )

    dds_config = config.setdefault("dds", {})
    requires_restart = False

    if request.rd_directory is not None:
        is_dir = await loop.run_in_executor(
            ctx.api_executor,
            os.path.isdir,
            request.rd_directory,
        )
        if not is_dir:
            raise HTTPException(
                status_code=400,
                detail=f"Каталог не существует: {request.rd_directory}",
            )
        dds_config["rd_directory"] = request.rd_directory
        ctx.rd_directory = request.rd_directory

    if request.db_path is not None:
        dds_config["db_path"] = request.db_path
        requires_restart = True

    try:
        await loop.run_in_executor(
            ctx.api_executor,
            _write_config_file,
            ctx.config_path,
            config,
        )
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Ошибка записи конфигурации: {e}",
        )

    message = "Настройки обновлены."
    if requires_restart:
        message += " Требуется перезапуск DDS для применения изменений db_path."

    return SettingsUpdateResponse(
        message=message,
        requires_restart=requires_restart,
        settings=SettingsResponse(
            rd_directory=dds_config.get("rd_directory", ""),
            db_path=dds_config.get("db_path", ""),
            modules_directory=dds_config.get("modules_directory", ""),
        ),
    )


# ----------------------------------------------------------------------
# Диагностика (только для администраторов)
# ----------------------------------------------------------------------


@router.get("/diagnostics")
async def get_diagnostics(
    *,
    ctx: CtxDep,
    session: AdminSessionDep,
) -> dict:
    """Возвращает диагностическую информацию для администраторов.

    Собирает статистику из трёх источников:

    - ``event_bus`` — через расширенный протокол
      :class:`_EventBusWithStats` (не входит в базовый ``IEventBus``;
      проверяется ``isinstance`` для безопасности на случай
      альтернативных реализаций без диагностики);
    - ``search_engine`` — возможности поискового бэкенда;
    - ``scan_orchestrator`` — статистика пула БД;
    - ``word_index_cache`` — попадания/промахи кэша (если есть).
    """
    _ = session
    result: dict = {}

    # Диагностика шины событий: ``get_queue_stats`` объявлен не в
    # базовом ``IEventBus``, а в расширении ``_EventBusWithStats``.
    # ``isinstance`` с runtime-checkable Protocol проверяет наличие
    # метода на уровне структуры типа, не сужая до конкретной
    # реализации (например, до ``AsyncEventBus``).
    if isinstance(ctx.event_bus, _EventBusWithStats):
        result["event_bus"] = ctx.event_bus.get_queue_stats()

    result["search_capabilities"] = sorted(ctx.search_engine.get_backend_capabilities())
    result["db_pool"] = ctx.scan_orchestrator.get_pool_stats()

    if ctx.word_index_cache is not None:
        result["word_index_cache"] = ctx.word_index_cache.get_stats()

    return result
