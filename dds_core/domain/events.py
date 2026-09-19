"""
События системы DDS.

Этот модуль определяет структуры данных для всех событий, которые
возникают в процессе работы системы. События используются для
логирования, мониторинга и обновления интерфейса в реальном времени
через шину событий.

Все события являются неизменяемыми (frozen dataclass) и
самодостаточными: они содержат всю необходимую информацию для
обработки подписчиками без дополнительных запросов к базе данных.

Иерархия событий:

    Event (базовый)
    ├── ApplicationStarted, ApplicationStopping, ApplicationStopped
    ├── ScanStarted, ScanCompleted, ScanProgressUpdated
    ├── ScanSecondaryStarted, ScanSecondaryProgressUpdated, ScanSecondaryCompleted
    ├── ScanCancelRequested, ScanCancelled
    ├── OperationTimedOut
    ├── FileProcessingFailed
    ├── DatabaseQuerySlow, DatabaseError
    ├── SearchPerformed
    ├── RequestStarted, RequestCompleted
    ├── AutoLoginPerformed
    ├── CoordinateSystemAnomalyDetected
    └── ModuleLoaded, ModuleLoadFailed, ModuleError

Всего 23 события.

Расширяемость:
Модули расширения могут определять собственные события,
наследующиеся от базового класса Event. Идентификаторы таких
событий должны иметь префикс 'module.<module_name>.' для
предотвращения конфликтов имён и упрощения фильтрации.

Принципы:
- События не содержат бизнес-логики, только данные.
- Все поля имеют значения по умолчанию для удобства создания.
- Метод to_dict() обеспечивает сериализацию в JSON-подобный словарь.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass(frozen=True)
class Event:
    """
    Базовый класс для всех событий системы.

    Атрибуты:
        event_type (ClassVar[str]): строковый идентификатор типа события.
            Используется для фильтрации подписчиками.
        correlation_id: уникальный идентификатор, связывающий события
            одного процесса (сканирования, запроса, инициализации).
        timestamp: время возникновения события в секундах с начала эпохи.
        source: имя компонента, породившего событие (например, "ScanPipeline").
        stage: этап обработки, на котором возникло событие.

    Пример:
        class MyEvent(Event):
            event_type = "custom.event"
            custom_field: str = ""
    """

    event_type: ClassVar[str] = "event"
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    source: str = ""
    stage: str = ""

    def to_dict(self) -> dict[str, Any]:
        """
        Преобразует событие в словарь для сериализации.

        Returns:
            Словарь с общими полями события.
        """
        return {
            "event_type": self.event_type,
            "correlation_id": self.correlation_id,
            "timestamp": self.timestamp,
            "source": self.source,
            "stage": self.stage,
        }


# ----------------------------------------------------------------------
# События жизненного цикла приложения
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ApplicationStarted(Event):
    """Событие успешного запуска приложения.

    Публикуется после завершения инициализации компонентов.

    Дополнительные атрибуты:
        version: версия DDS.
        config_path: путь к использованному файлу конфигурации.
    """

    event_type: ClassVar[str] = "app.started"
    version: str = ""
    config_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update({"version": self.version, "config_path": self.config_path})
        return data


@dataclass(frozen=True)
class ApplicationStopping(Event):
    """Событие начала процедуры остановки приложения.

    Публикуется в начале graceful shutdown.
    """

    event_type: ClassVar[str] = "app.stopping"


@dataclass(frozen=True)
class ApplicationStopped(Event):
    """Событие полной остановки приложения.

    Публикуется после освобождения всех ресурсов.
    """

    event_type: ClassVar[str] = "app.stopped"


# ----------------------------------------------------------------------
# События сканирования
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ScanStarted(Event):
    """Событие начала сканирования каталога.

    Публикуется при запуске первичного или вторичного сканирования.

    Дополнительные атрибуты:
        scan_id: идентификатор записи в таблице scan_state.
        phase: фаза сканирования ('primary' или 'secondary').
        rd_directory: путь к каталогу рабочей документации.
    """

    event_type: ClassVar[str] = "scan.started"
    scan_id: int = 0
    phase: str = "primary"
    rd_directory: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "scan_id": self.scan_id,
                "phase": self.phase,
                "rd_directory": self.rd_directory,
            }
        )
        return data


@dataclass(frozen=True)
class ScanCompleted(Event):
    """Событие завершения сканирования.

    Публикуется после завершения (успешного, с ошибкой или прерванного).

    Дополнительные атрибуты:
        scan_id: идентификатор записи в таблице scan_state.
        phase: фаза сканирования.
        status: итоговый статус ('completed', 'error', 'interrupted').
        processed_files: количество обработанных файлов.
        total_files: общее количество файлов.
        indexed_files: количество файлов, успешно извлеченных и записанных в БД.
        duplicate_files: количество файлов, пропущенных как дубликаты по хешу.
        skipped_files: количество файлов, пропущенных без хеширования (по метаданным).
        error_files: количество файлов, обработанных с ошибкой.
    """

    event_type: ClassVar[str] = "scan.completed"
    scan_id: int = 0
    phase: str = "primary"
    status: str = "completed"
    processed_files: int = 0
    total_files: int = 0
    indexed_files: int = 0
    duplicate_files: int = 0
    skipped_files: int = 0
    error_files: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "scan_id": self.scan_id,
                "phase": self.phase,
                "status": self.status,
                "processed_files": self.processed_files,
                "total_files": self.total_files,
                "indexed_files": self.indexed_files,
                "duplicate_files": self.duplicate_files,
                "skipped_files": self.skipped_files,
                "error_files": self.error_files,
            }
        )
        return data


@dataclass(frozen=True)
class ScanProgressUpdated(Event):
    """Периодическое обновление прогресса сканирования.

    Публикуется конвейером не чаще одного раза в секунду или
    после обработки определённого количества файлов (агрегированное).

    Дополнительные атрибуты:
        scan_id: идентификатор записи в таблице scan_state.
        phase: фаза сканирования.
        processed_files: обработано файлов на момент публикации.
        total_files: общее количество файлов.
        current_file: путь к файлу, обрабатываемому в данный момент.
        status: текущий статус ('running', 'completed' и т.д.).
        indexed_files: количество файлов, успешно извлеченных и записанных в БД.
        duplicate_files: количество файлов, пропущенных как дубликаты по хешу.
        skipped_files: количество файлов, пропущенных без хеширования (по метаданным).
        error_files: количество файлов, обработанных с ошибкой.
    """

    event_type: ClassVar[str] = "scan.progress"
    scan_id: int = 0
    phase: str = "primary"
    processed_files: int = 0
    total_files: int = 0
    current_file: str = ""
    status: str = "running"
    indexed_files: int = 0
    duplicate_files: int = 0
    skipped_files: int = 0
    error_files: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "scan_id": self.scan_id,
                "phase": self.phase,
                "processed_files": self.processed_files,
                "total_files": self.total_files,
                "current_file": self.current_file,
                "status": self.status,
                "indexed_files": self.indexed_files,
                "duplicate_files": self.duplicate_files,
                "skipped_files": self.skipped_files,
                "error_files": self.error_files,
            }
        )
        return data


# ----------------------------------------------------------------------
# События вторичного сканирования (индикация)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ScanSecondaryStarted(Event):
    """Событие начала вторичного сканирования.

    Публикуется при запуске обновления метаданных и модулей.

    Дополнительные атрибуты:
        scan_id: идентификатор записи в таблице scan_state.
        rd_directory: путь к каталогу рабочей документации.
    """

    event_type: ClassVar[str] = "scan.secondary.started"
    scan_id: int = 0
    rd_directory: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "scan_id": self.scan_id,
                "rd_directory": self.rd_directory,
            }
        )
        return data


@dataclass(frozen=True)
class ScanSecondaryProgressUpdated(Event):
    """Периодическое обновление прогресса вторичного сканирования.

    Публикуется при выполнении обновления метаданных или обработки
    модулей.

    Дополнительные атрибуты:
        scan_id: идентификатор записи в таблице scan_state.
        stage: этап вторичного сканирования
            ('metadata_update' или 'modules').
        processed_documents: количество обработанных документов.
        total_documents: общее количество документов для обработки.
        module_name: имя текущего модуля (если stage == 'modules').
        progress_percent: процент выполнения (0.0–100.0).
    """

    event_type: ClassVar[str] = "scan.secondary.progress"
    scan_id: int = 0
    stage: str = "metadata_update"
    processed_documents: int = 0
    total_documents: int = 0
    module_name: str = ""
    progress_percent: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "scan_id": self.scan_id,
                "stage": self.stage,
                "processed_documents": self.processed_documents,
                "total_documents": self.total_documents,
                "module_name": self.module_name,
                "progress_percent": self.progress_percent,
            }
        )
        return data


@dataclass(frozen=True)
class ScanSecondaryCompleted(Event):
    """Событие завершения вторичного сканирования.

    Публикуется после завершения обновления метаданных и обработки
    всех модулей (успешно или с ошибкой).

    Дополнительные атрибуты:
        scan_id: идентификатор записи в таблице scan_state.
        status: итоговый статус ('completed', 'error', 'interrupted').
        processed_documents: количество обработанных документов.
        total_documents: общее количество документов.
        errors: количество ошибок.
    """

    event_type: ClassVar[str] = "scan.secondary.completed"
    scan_id: int = 0
    status: str = "completed"
    processed_documents: int = 0
    total_documents: int = 0
    errors: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "scan_id": self.scan_id,
                "status": self.status,
                "processed_documents": self.processed_documents,
                "total_documents": self.total_documents,
                "errors": self.errors,
            }
        )
        return data


# ----------------------------------------------------------------------
# События отмены сканирования
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ScanCancelRequested(Event):
    """Событие запроса отмены сканирования.

    Публикуется при вызове ``cancel_scan()`` до фактического
    завершения конвейера. Позволяет подписчикам (логирование,
    мониторинг, веб-интерфейс) немедленно реагировать на запрос
    отмены, не дожидаясь полного завершения конвейера.

    Дополнительные атрибуты:
        scan_id: идентификатор записи в таблице ``scan_state``,
            для которой запрошена отмена. Значение ``0``
            означает, что идентификатор недоступен на момент
            публикации.
    """

    event_type: ClassVar[str] = "scan.cancel_requested"
    scan_id: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update({"scan_id": self.scan_id})
        return data


@dataclass(frozen=True)
class ScanCancelled(Event):
    """Событие отмены сканирования.

    Публикуется после фактического завершения конвейера
    в результате отмены — как через ``CancelledError``
    (принудительная отмена через ``task.cancel()``),
    так и через кооперативный флаг ``_cancel_requested``
    (воркеры завершаются самостоятельно).

    Дополнительные атрибуты:
        scan_id: идентификатор записи в таблице ``scan_state``,
            для которой выполнена отмена.
    """

    event_type: ClassVar[str] = "scan.cancelled"
    scan_id: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update({"scan_id": self.scan_id})
        return data


# ----------------------------------------------------------------------
# События обработки файлов
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class FileProcessingFailed(Event):
    """Ошибка при обработке файла.

    Дополнительные атрибуты:
        scan_id: идентификатор сканирования.
        file_path: путь к файлу.
        error_type: тип исключения (например, 'FileNotFoundError').
        error_message: сообщение об ошибке.
        traceback: полный стек ошибки (если доступен).
    """

    event_type: ClassVar[str] = "file.processing_failed"
    scan_id: int = 0
    file_path: str = ""
    error_type: str = ""
    error_message: str = ""
    traceback: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "scan_id": self.scan_id,
                "file_path": self.file_path,
                "error_type": self.error_type,
                "error_message": self.error_message,
                "traceback": self.traceback,
            }
        )
        return data


# ----------------------------------------------------------------------
# События базы данных
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class DatabaseQuerySlow(Event):
    """Медленный SQL-запрос (превышен порог).

    Дополнительные атрибуты:
        query: текст запроса (без параметров).
        duration_ms: длительность выполнения в миллисекундах.
        explain_plan: результат ``EXPLAIN QUERY PLAN`` (если доступен),
            содержащий план выполнения запроса.
    """

    event_type: ClassVar[str] = "db.query_slow"
    query: str = ""
    duration_ms: float = 0.0
    explain_plan: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "query": self.query,
                "duration_ms": self.duration_ms,
                "explain_plan": self.explain_plan,
            }
        )
        return data


@dataclass(frozen=True)
class DatabaseError(Event):
    """Ошибка при выполнении операции с базой данных.

    Дополнительные атрибуты:
        query: текст запроса (без параметров).
        error_type: тип исключения.
        error_message: сообщение об ошибке.
    """

    event_type: ClassVar[str] = "db.error"
    query: str = ""
    error_type: str = ""
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "query": self.query,
                "error_type": self.error_type,
                "error_message": self.error_message,
            }
        )
        return data


# ----------------------------------------------------------------------
# События таймаутов блокирующих операций
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class OperationTimedOut(Event):
    """Таймаут блокирующей операции.

    Публикуется на уровне WARNING при срабатывании любого
    таймаута блокирующей операции. Дескриптор ``operation``
    однозначно идентифицирует операцию и является ключом
    в реестре ``config.OPERATION_TIMEOUTS``.

    Дополнительные атрибуты:
        operation: строковый дескриптор операции
            (ключ в ``config.OPERATION_TIMEOUTS``).
            Например, ``"scan.extract"``, ``"search.query"``.
        timeout_seconds: установленный таймаут в секундах.
        elapsed_seconds: фактическое время до срабатывания
            таймаута в секундах.
        context: дополнительные данные для диагностики
            (путь файла, поисковый запрос и т.д.).
        recovery_action: описание действия, предпринятого
            после таймаута (например, «Файл помечен как
            ошибочный», «Поиск прерван»).
    """

    event_type: ClassVar[str] = "operation.timed_out"
    operation: str = ""
    timeout_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    context: str = ""
    recovery_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "operation": self.operation,
                "timeout_seconds": self.timeout_seconds,
                "elapsed_seconds": self.elapsed_seconds,
                "context": self.context,
                "recovery_action": self.recovery_action,
            }
        )
        return data


# ----------------------------------------------------------------------
# События поиска
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class SearchPerformed(Event):
    """Выполнен поисковый запрос.

    Дополнительные атрибуты:
        query: текст поискового запроса.
        result_count: количество найденных результатов.
        duration_ms: длительность выполнения запроса.
    """

    event_type: ClassVar[str] = "search.performed"
    query: str = ""
    result_count: int = 0
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "query": self.query,
                "result_count": self.result_count,
                "duration_ms": self.duration_ms,
            }
        )
        return data


# ----------------------------------------------------------------------
# События HTTP-запросов (middleware)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class RequestStarted(Event):
    """Начало обработки HTTP-запроса.

    Дополнительные атрибуты:
        method: HTTP-метод (GET, POST и т.д.).
        path: путь запроса.
    """

    event_type: ClassVar[str] = "http.request_started"
    method: str = ""
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update({"method": self.method, "path": self.path})
        return data


@dataclass(frozen=True)
class RequestCompleted(Event):
    """Завершение обработки HTTP-запроса.

    Дополнительные атрибуты:
        method: HTTP-метод.
        path: путь запроса.
        status_code: код ответа.
        duration_ms: длительность обработки в миллисекундах.
    """

    event_type: ClassVar[str] = "http.request_completed"
    method: str = ""
    path: str = ""
    status_code: int = 0
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "method": self.method,
                "path": self.path,
                "status_code": self.status_code,
                "duration_ms": self.duration_ms,
            }
        )
        return data


# ----------------------------------------------------------------------
# События аутентификации
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class AutoLoginPerformed(Event):
    """Событие автоматического входа по IP-адресу.

    Публикуется middleware
    :func:`~dds_web.auto_login.auto_login_middleware` при успешном
    создании сессии на основе IP-адреса клиента. Используется для
    аудита и мониторинга: администратор видит, какие клиенты
    авторизуются без пароля и с каких IP-адресов.

    Событие не публикуется при парольном входе через ``/login``
    (для этого случая отдельного события не предусмотрено) и
    при неудачных попытках автологина (IP не входит в подсеть,
    отсутствует в ``ip_user_map`` или пользователь не найден).

    Событие публикуется на уровне INFO (см.
    :func:`~dds_core.infrastructure.logging_subscriber.default_level_mapping`).

    Дополнительные атрибуты:
        username: имя пользователя, для которого создана сессия.
        client_ip: IP-адрес клиента, инициировавшего автологин.
            Для IPv4-mapped IPv6-адресов передаётся
            нормализованное значение (``::ffff:192.168.1.42`` →
            ``192.168.1.42``).
    """

    event_type: ClassVar[str] = "auth.auto_login"
    username: str = ""
    client_ip: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "username": self.username,
                "client_ip": self.client_ip,
            }
        )
        return data


# ----------------------------------------------------------------------
# События системы координат (предпросмотр документа)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class CoordinateSystemAnomalyDetected(Event):
    """Обнаружена аномальная система координат страницы.

    Публикуется при построении подсветки совпадений
    (``POST /api/documents/{doc_id}/pages/{page_number}/highlights``),
    когда авто-диагностика определила, что страница PDF использует
    ошибочную систему координат (bottom-left с перепутанными осями).
    Событие позволяет администратору отслеживать документы,
    требующие коррекции координат подсветки.

    Диагностика выполняется функцией
    :func:`~dds_core.application.coordinate_diagnostics.diagnose_word_index`
    и срабатывает при комбинации двух независимых признаков
    (доля слов за границами страницы и доля «вертикальных» слов).

    Событие публикуется на уровне WARNING (см.
    :func:`~dds_core.infrastructure.logging_subscriber.default_level_mapping`).

    Дополнительные атрибуты:
        doc_id: идентификатор документа.
        page_number: номер страницы (0-based).
        confidence: уровень уверенности диагностики:

            - ``"high"`` — сработали оба признака.
            - ``"medium"`` — сработал ровно один признак.

            Значение ``"none"`` не публикуется (событие не создаётся
            при отсутствии аномалии). Значение ``"manual"`` также
            не публикуется: событие отражает результат
            автоматической диагностики, а не ручного управления
            флагом трансформации.
    """

    event_type: ClassVar[str] = "coord.anomaly_detected"
    doc_id: str = ""
    page_number: int = 0
    confidence: str = "none"

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "doc_id": self.doc_id,
                "page_number": self.page_number,
                "confidence": self.confidence,
            }
        )
        return data


# ----------------------------------------------------------------------
# События модулей
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ModuleLoaded(Event):
    """Модуль успешно загружен и активирован.

    Дополнительные атрибуты:
        module_name: имя модуля.
        version: версия модуля.
    """

    event_type: ClassVar[str] = "module.loaded"
    module_name: str = ""
    version: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update({"module_name": self.module_name, "version": self.version})
        return data


@dataclass(frozen=True)
class ModuleLoadFailed(Event):
    """Ошибка при загрузке модуля.

    Дополнительные атрибуты:
        module_name: имя модуля.
        error_message: сообщение об ошибке.
    """

    event_type: ClassVar[str] = "module.load_failed"
    module_name: str = ""
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "module_name": self.module_name,
                "error_message": self.error_message,
            }
        )
        return data


@dataclass(frozen=True)
class ModuleError(Event):
    """Ошибка в работе модуля.

    Дополнительные атрибуты:
        module_name: имя модуля.
        error_type: тип исключения.
        error_message: сообщение об ошибке.
    """

    event_type: ClassVar[str] = "module.error"
    module_name: str = ""
    error_type: str = ""
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "module_name": self.module_name,
                "error_type": self.error_type,
                "error_message": self.error_message,
            }
        )
        return data
