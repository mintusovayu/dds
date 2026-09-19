"""
Сервис обновления метаданных документов на основе парсинга имён файлов.

Модуль предоставляет класс :class:`DocumentMetadataService`, который
заполняет поля фильтрации (``object_code``, ``discipline_code``,
``document_type_code``) и признак ``unmatched_flag`` в таблице
``documents``. Обновление выполняется на основе анализа имени файла
и справочников кодов/псевдонимов.

Сервис используется в рамках вторичной фазы сканирования, которая
запускается после успешного завершения первичного индексирования,
не блокируя доступность полнотекстового поиска. Также может быть
запущен повторно по требованию для актуализации данных при
изменении справочников.

Батчевая запись (скорректированный план):

Метод :meth:`DocumentMetadataService.update_all_documents` выполняет
обновление всех документов **одним** вызовом
``IDatabase.execute_write_many`` вместо N отдельных
``execute_write``. Это устраняет N транзакций и N ``COMMIT``,
что критично при больших каталогах (10 000+ документов). Также
устраняется повторная загрузка кодов справочников: множества кодов
загружаются один раз на весь вызов, а не по одному разу на
документ × три категории.

Изоляция:

Метод :meth:`update_all_documents` не защищён от параллельных
изменений таблицы ``documents``. Вызывающий код
(``ScanOrchestrator.run_secondary_scans``) должен гарантировать
отсутствие параллельных записей в таблице на время вызова.
В текущей архитектуре это обеспечивается тем, что
``run_secondary_scans`` синхронный и не пересекается с другими
записями.

Принципы:
- Зависит от абстракций: ``IDatabase`` и протокола
  ``ReferenceRepository`` из доменного слоя, а не от конкретных
  реализаций (DIP).
- Не содержит бизнес-логики, кроме сопоставления имён файлов со
  справочниками и формирования SQL-обновлений.
- Потокобезопасность обеспечивается базой данных (``SQLiteAdapter``),
  сам сервис не хранит изменяемого состояния.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..domain.interfaces import IDatabase
from ..domain.reference_repository import ReferenceRepository
from .file_name_parser import ParsedFileName, parse_file_name

# ----------------------------------------------------------------------
# Результат разрешения одного кода
# ----------------------------------------------------------------------


@dataclass
class _ResolvedCode:
    """Внутренний результат разрешения кода.

    Attributes:
        code: Канонический код или ``None``, если не удалось
            разрешить значение.
        is_matched: ``True``, если значение совпало либо с кодом,
            либо с псевдонимом из справочника.
    """

    code: str | None
    is_matched: bool


# ----------------------------------------------------------------------
# Сервис обновления метаданных
# ----------------------------------------------------------------------


class DocumentMetadataService:
    """Обновляет метаданные фильтрации для документов.

    Сервис перебирает все документы из таблицы ``documents``,
    парсит их ``file_path`` и заполняет колонки ``object_code``,
    ``discipline_code``, ``document_type_code``, а также
    ``unmatched_flag``.

    Если имя файла не соответствует ожидаемому формату или хотя бы
    один из трёх кодов не найден в соответствующем справочнике,
    документ помечается как ``unmatched`` (``unmatched_flag = 1``),
    а соответствующие поля кодов остаются пустыми (``NULL``).
    Если имя распознано, сохраняются канонические коды, полученные
    из справочников (псевдонимы преобразуются в коды).

    Батчевая запись:

    Все обновления выполняются одним вызовом
    ``IDatabase.execute_write_many``. Множества кодов справочников
    загружаются один раз за вызов ``update_all_documents``.

    Пример использования::

        metadata_service = DocumentMetadataService(
            db=db_adapter,
            object_repo=object_reference_repo,
            discipline_repo=discipline_reference_repo,
            document_type_repo=document_type_reference_repo,
        )
        metadata_service.update_all_documents()
    """

    def __init__(
        self,
        db: IDatabase,
        object_repo: ReferenceRepository,
        discipline_repo: ReferenceRepository,
        document_type_repo: ReferenceRepository,
        parser: Callable[[str], ParsedFileName] = parse_file_name,
    ) -> None:
        """Инициализирует сервис.

        Args:
            db: Реализация ``IDatabase`` для выполнения запросов.
            object_repo: Репозиторий справочника объектов.
            discipline_repo: Репозиторий справочника дисциплин.
            document_type_repo: Репозиторий справочника типов документов.
            parser: Функция парсинга имени файла. По умолчанию
                используется :func:`parse_file_name`. Замена возможна
                для тестирования.
        """
        self._db = db
        self._object_repo = object_repo
        self._discipline_repo = discipline_repo
        self._document_type_repo = document_type_repo
        self._parser = parser

    def update_all_documents(self) -> int:
        """Обновляет метаданные всех документов одним батчем.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Однократная загрузка кодов из трёх справочников      |
        |   | (три вызова ``get_codes()``).                        |
        +---+-----------------------------------------------------+
        | 2 | Выборка всех пар ``(doc_id, file_path)`` из          |
        |   | таблицы ``documents`` (один SELECT).                 |
        +---+-----------------------------------------------------+
        | 3 | Для каждого документа строится UPDATE-запрос         |
        |   | через :meth:`_build_update_query` без выполнения.    |
        +---+-----------------------------------------------------+
        | 4 | Все построенные запросы выполняются одним вызовом    |
        |   | ``execute_write_many`` (одна транзакция).            |
        +---+-----------------------------------------------------+
        | 5 | Возврат количества обработанных документов.          |
        +---+-----------------------------------------------------+

        Примечание:
            Изоляция: метод не защищён от параллельных изменений
            таблицы ``documents``. Вызывающий код должен
            гарантировать отсутствие параллельных записей.

        Returns:
            Количество обработанных документов.
        """
        # 1. Однократная загрузка кодов (защита от O(N×3) запросов).
        object_codes = self._object_repo.get_codes()
        discipline_codes = self._discipline_repo.get_codes()
        document_type_codes = self._document_type_repo.get_codes()

        # 2. Выборка всех документов.
        rows = self._db.execute("SELECT doc_id, file_path FROM documents")

        # 3. Построение списка UPDATE-запросов без выполнения.
        queries: list[tuple[str, tuple]] = [
            self._build_update_query(
                str(row[0]),
                str(row[1]),
                object_codes,
                discipline_codes,
                document_type_codes,
            )
            for row in rows
        ]

        # 4. Один batch-write вместо N отдельных транзакций.
        if queries:
            self._db.execute_write_many(queries)

        return len(rows)

    def _build_update_query(
        self,
        doc_id: str,
        file_path: str,
        object_codes: set[str],
        discipline_codes: set[str],
        document_type_codes: set[str],
    ) -> tuple[str, tuple]:
        """Строит UPDATE-запрос для одного документа без выполнения.

        Метод вынесен отдельно, чтобы :meth:`update_all_documents`
        мог аккумулировать список запросов и выполнить их одним
        батчем через ``execute_write_many``. Это устраняет N
        отдельных транзакций и N ``COMMIT``.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Вызов парсера для ``file_path``.                    |
        +---+-----------------------------------------------------+
        | 2 | Если ``is_valid`` ложно — формируется UPDATE,       |
        |   | устанавливающий все коды в ``NULL``, а              |
        |   | ``unmatched_flag`` в ``1``.                         |
        +---+-----------------------------------------------------+
        | 3 | Если имя валидно — для каждого из трёх кодов         |
        |   | выполняется разрешение через                        |
        |   | :meth:`_resolve_code` с предзагруженными            |
        |   | множествами кодов.                                  |
        +---+-----------------------------------------------------+
        | 4 | Формируется UPDATE с полученными значениями         |
        |   | и ``unmatched_flag`` = 1, если хотя бы один код      |
        |   | не разрешён.                                        |
        +---+-----------------------------------------------------+

        Args:
            doc_id: Идентификатор документа.
            file_path: Относительный путь к файлу.
            object_codes: Предзагруженное множество кодов объектов.
            discipline_codes: Предзагруженное множество кодов
                дисциплин.
            document_type_codes: Предзагруженное множество кодов
                типов документов.

        Returns:
            Кортеж ``(SQL-запрос, параметры)`` для передачи в
            ``execute_write_many``.
        """
        parsed = self._parser(file_path)

        if not parsed.is_valid:
            return (
                (
                    "UPDATE documents SET object_code = NULL, "
                    "discipline_code = NULL, document_type_code = NULL, "
                    "unmatched_flag = 1 WHERE doc_id = ?"
                ),
                (doc_id,),
            )

        object_resolved = self._resolve_code(
            parsed.object_code,
            self._object_repo,
            codes=object_codes,
        )
        discipline_resolved = self._resolve_code(
            parsed.discipline_code,
            self._discipline_repo,
            codes=discipline_codes,
        )
        document_type_resolved = self._resolve_code(
            parsed.document_type_code,
            self._document_type_repo,
            codes=document_type_codes,
        )

        # Флаг несоответствия, если хотя бы одно разрешение не удалось.
        unmatched = (
            0
            if (
                object_resolved.is_matched
                and discipline_resolved.is_matched
                and document_type_resolved.is_matched
            )
            else 1
        )

        return (
            (
                "UPDATE documents SET object_code = ?, discipline_code = ?, "
                "document_type_code = ?, unmatched_flag = ? WHERE doc_id = ?"
            ),
            (
                object_resolved.code,
                discipline_resolved.code,
                document_type_resolved.code,
                unmatched,
                doc_id,
            ),
        )

    def _resolve_code(
        self,
        value: str | None,
        repo: ReferenceRepository,
        *,
        codes: set[str] | None = None,
    ) -> _ResolvedCode:
        """Разрешает значение в канонический код.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Если ``value`` пустое, возвращается                  |
        |   | ``_ResolvedCode(None, False)``.                     |
        +---+-----------------------------------------------------+
        | 2 | Определение множества кодов: если ``codes`` задано — |
        |   | используется оно; иначе загружается через           |
        |   | ``repo.get_codes()``.                               |
        +---+-----------------------------------------------------+
        | 3 | Если ``value`` содержится в кодах, возвращается      |
        |   | ``_ResolvedCode(value, True)``.                     |
        +---+-----------------------------------------------------+
        | 4 | Иначе вызывается ``repo.resolve_alias(value)``.     |
        |   | Если псевдоним найден, возвращается                 |
        |   | ``_ResolvedCode(alias_resolved, True)``.            |
        +---+-----------------------------------------------------+
        | 5 | Если ничего не найдено, возвращается                 |
        |   | ``_ResolvedCode(None, False)``.                     |
        +---+-----------------------------------------------------+

        Примечание:
            Параметр ``codes`` — keyword-only. Это позволяет
            вызывать метод как с предзагруженным множеством
            (для батчевой обработки), так и без него (для
            одиночного разрешения).

        Args:
            value: Исходное значение из имени файла.
            repo: Репозиторий справочника.
            codes: Предзагруженное множество кодов. Если ``None`` —
                загружается через ``repo.get_codes()``.

        Returns:
            Результат разрешения.
        """
        if not value:
            return _ResolvedCode(None, False)

        if codes is None:
            codes = repo.get_codes()

        if value in codes:
            return _ResolvedCode(value, True)

        alias_code = repo.resolve_alias(value)
        if alias_code is not None:
            return _ResolvedCode(alias_code, True)

        return _ResolvedCode(None, False)
