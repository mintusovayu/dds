"""
Сервис предоставления справочников для фильтров.

Модуль содержит класс :class:`ReferenceDataService`, который
агрегирует данные из трёх репозиториев справочников (объекты,
дисциплины, типы документов) и формирует структуру, пригодную
для передачи в веб-интерфейс.

Структура ответа:

.. code-block:: json

    {
        "objects": {
            "codes": ["7350", "7360"],
            "aliases_en": [
                {"alias": "fire_station", "code": "7350"},
                {"alias": "pump_station", "code": "7360"}
            ],
            "aliases_ru": [
                {"alias": "пожарное депо", "code": "7350"},
                {"alias": "насосная", "code": "7360"}
            ]
        },
        "disciplines": {
            "codes": ["265", "280"],
            "aliases_en": [
                {"alias": "automation", "code": "265"},
                {"alias": "electrical", "code": "280"}
            ],
            "aliases_ru": [
                {"alias": "автоматизация", "code": "265"},
                {"alias": "электрика", "code": "280"}
            ]
        },
        "documentTypes": {
            "codes": ["DTL", "SPC"],
            "aliases_en": [
                {"alias": "drawing", "code": "DTL"},
                {"alias": "specification", "code": "SPC"}
            ],
            "aliases_ru": [
                {"alias": "чертёж", "code": "DTL"},
                {"alias": "спецификация", "code": "SPC"}
            ]
        }
    }

Клиентский модуль ``FilterCoordinator`` использует эту структуру
для заполнения полей автодополнения и преобразования выбранного
псевдонима в соответствующий канонический код.

Принципы:
- Сервис зависит от абстракций ``ReferenceRepository``, а не от
  конкретных реализаций (DIP).
- Не содержит бизнес-логики, кроме агрегации данных.
- Потокобезопасен, так как не хранит изменяемого состояния.
"""

from __future__ import annotations

from ..domain.reference_repository import ReferenceRepository


class ReferenceDataService:
    """Агрегирует справочники для фильтрации по метаданным.

    Собирает коды и псевдонимы (с разделением на английские и русские)
    для объекта, дисциплины и типа документа из соответствующих
    репозиториев и возвращает их в виде, готовом для использования
    на клиенте.

    Example:
        service = ReferenceDataService(
            object_repo=object_repo,
            discipline_repo=discipline_repo,
            document_type_repo=document_type_repo,
        )
        data = service.get_references()
    """

    def __init__(
        self,
        object_repo: ReferenceRepository,
        discipline_repo: ReferenceRepository,
        document_type_repo: ReferenceRepository,
    ) -> None:
        """Инициализирует сервис справочников.

        Args:
            object_repo: Репозиторий справочника объектов.
            discipline_repo: Репозиторий справочника дисциплин.
            document_type_repo: Репозиторий справочника типов документов.
        """
        self._object_repo = object_repo
        self._discipline_repo = discipline_repo
        self._document_type_repo = document_type_repo

    def get_references(self) -> dict:
        """Возвращает справочники для всех трёх категорий.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Для каждой категории (объекты, дисциплины, типы)    |
        |   | вызывается вспомогательный метод                    |
        |   | :meth:`_build_category`, который формирует словарь  |
        |   | с кодами и псевдонимами, разделёнными по языкам.    |
        +---+-----------------------------------------------------+
        | 2 | Результаты объединяются в общий словарь с ключами    |
        |   | ``"objects"``, ``"disciplines"``, ``"documentTypes"``.|
        +---+-----------------------------------------------------+

        Returns:
            Словарь с тремя категориями справочников.
        """
        return {
            "objects": self._build_category(self._object_repo),
            "disciplines": self._build_category(self._discipline_repo),
            "documentTypes": self._build_category(self._document_type_repo),
        }

    def _build_category(self, repo: ReferenceRepository) -> dict:
        """Формирует справочник для одной категории.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Получение множества кодов через ``repo.get_codes()``.|
        +---+-----------------------------------------------------+
        | 2 | Получение отображения английских псевдонимов в коды  |
        |   | через ``repo.get_alias_to_code_map(lang="en")``.    |
        +---+-----------------------------------------------------+
        | 3 | Получение отображения русских псевдонимов в коды     |
        |   | через ``repo.get_alias_to_code_map(lang="ru")``.    |
        +---+-----------------------------------------------------+
        | 4 | Формирование списков ``aliases_en`` и ``aliases_ru``:|
        |   | каждый элемент — словарь с ключами ``"alias"`` и     |
        |   | ``"code"``.                                          |
        +---+-----------------------------------------------------+
        | 5 | Возврат словаря с ключами ``"codes"``,               |
        |   | ``"aliases_en"``, ``"aliases_ru"``.                |
        +---+-----------------------------------------------------+

        Args:
            repo: Репозиторий справочника.

        Returns:
            Словарь с кодами и псевдонимами категории.
        """
        codes = sorted(repo.get_codes())

        alias_en_map = repo.get_alias_to_code_map(lang="en")
        aliases_en = [
            {"alias": alias, "code": code} for alias, code in sorted(alias_en_map.items())
        ]

        alias_ru_map = repo.get_alias_to_code_map(lang="ru")
        aliases_ru = [
            {"alias": alias, "code": code} for alias, code in sorted(alias_ru_map.items())
        ]

        return {
            "codes": codes,
            "aliases_en": aliases_en,
            "aliases_ru": aliases_ru,
        }
