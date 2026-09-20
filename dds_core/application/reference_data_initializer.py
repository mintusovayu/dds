"""
Инициализатор справочников из эталонного JSON-файла.

Модуль содержит класс :class:`ReferenceDataInitializer`, который
заполняет таблицы справочников (коды и псевдонимы объектов,
дисциплин и типов документов) на основе JSON-файла, сформированного
внешней утилитой (``exel2json.py``). Инициализация выполняется
только в том случае, если справочные таблицы пусты, что
гарантирует идемпотентность и предотвращает дублирование данных.

Используется при старте приложения после создания схемы БД,
чтобы новая база данных сразу содержала актуальные справочники
для фильтрации документов. Если JSON-файл отсутствует или
повреждён, приложение продолжает работу без справочников
(фильтры будут недоступны), в лог выводится предупреждение.

Поддерживается языковое разделение псевдонимов: в JSON-файле
для каждой категории присутствуют отдельные списки ``aliases_en``
(английские псевдонимы) и ``aliases_ru`` (русские псевдонимы).
При вставке в таблицы псевдонимов дополнительно указывается
язык (колонка ``lang``).

Принципы:
- Зависит от абстракции ``IDatabase`` и пути к файлу (DIP).
- Не содержит бизнес-логики, кроме загрузки и валидации данных.
- Идемпотентен: повторный вызов не изменяет данные, если
  справочники уже заполнены.
"""

from __future__ import annotations

import json

from ..domain.interfaces import IDatabase


class ReferenceDataInitializer:
    """Загружает справочники из JSON в пустую базу данных.

    Ожидаемый формат JSON:

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
                "codes": ["265"],
                "aliases_en": [
                    {"alias": "automation", "code": "265"}
                ],
                "aliases_ru": [
                    {"alias": "автоматизация", "code": "265"}
                ]
            },
            "documentTypes": {
                "codes": ["DTL"],
                "aliases_en": [
                    {"alias": "drawing", "code": "DTL"}
                ],
                "aliases_ru": [
                    {"alias": "чертёж", "code": "DTL"}
                ]
            }
        }

    Ключи верхнего уровня: ``objects``, ``disciplines``,
    ``documentTypes``. Каждая категория содержит список ``codes``
    и два списка псевдонимов: ``aliases_en`` и ``aliases_ru``.
    Каждый элемент псевдонима — объект с ключами ``alias`` и ``code``.

    Example:
        initializer = ReferenceDataInitializer(
            db=db_adapter,
            json_path="docs/default_references.json",
        )
        stats = initializer.initialize()
        print(stats)  # {'codes_inserted': 3, 'aliases_inserted': 6}
    """

    def __init__(self, db: IDatabase, json_path: str) -> None:
        """Инициализирует загрузчик справочников.

        Args:
            db: Реализация ``IDatabase`` для выполнения запросов.
            json_path: Путь к JSON-файлу с эталонными данными.
        """
        self._db = db
        self._json_path = json_path

    def initialize(self) -> dict[str, int]:
        """Выполняет загрузку справочников, если база пуста.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Проверка, пусты ли справочные таблицы. Если хотя бы |
        |   | одна из основных таблиц содержит записи, метод     |
        |   | завершается без изменений (идемпотентность).        |
        +---+-----------------------------------------------------+
        | 2 | Чтение и разбор JSON-файла. При ошибках (отсутствие |
        |   | файла, повреждённый JSON, неверная структура)      |
        |   | выводится предупреждение, и метод завершается       |
        |   | без изменений.                                      |
        +---+-----------------------------------------------------+
        | 3 | Валидация структуры: наличие ключей ``objects``,    |
        |   | ``disciplines``, ``documentTypes``; внутри каждой   |
        |   | категории — списки ``codes``, ``aliases_en``,        |
        |   | ``aliases_ru``.                                     |
        +---+-----------------------------------------------------+
        | 4 | Формирование SQL-запросов: сначала вставка всех     |
        |   | кодов (``INSERT OR IGNORE``), затем вставка всех    |
        |   | псевдонимов с указанием языка (``INSERT OR IGNORE``).|
        +---+-----------------------------------------------------+
        | 5 | Выполнение всех запросов одной транзакцией через    |
        |   | ``execute_write_many``.                             |
        +---+-----------------------------------------------------+
        | 6 | Возврат словаря со статистикой вставленных записей. |
        +---+-----------------------------------------------------+

        Returns:
            Словарь с ключами ``"codes_inserted"`` и
            ``"aliases_inserted"``, содержащий количество
            фактически добавленных строк (без учёта игнорированных
            дубликатов). Если инициализация не выполнялась,
            возвращается словарь с нулями.
        """
        # 1. Проверка на пустоту справочных таблиц
        if not self._is_reference_tables_empty():
            print("INFO:     Справочники уже содержат данные. Инициализация из JSON пропущена.")
            return {"codes_inserted": 0, "aliases_inserted": 0}

        # 2. Чтение JSON
        try:
            with open(self._json_path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            print(
                f"WARNING:  JSON справочников не найден: {self._json_path}. "
                f"Таблицы останутся пустыми."
            )
            return {"codes_inserted": 0, "aliases_inserted": 0}
        except json.JSONDecodeError as e:
            print(f"WARNING:  Ошибка разбора JSON справочников: {e}. Таблицы останутся пустыми.")
            return {"codes_inserted": 0, "aliases_inserted": 0}

        # 3. Валидация структуры
        categories = ("objects", "disciplines", "documentTypes")
        for category in categories:
            if category not in data:
                print(
                    f"WARNING:  В JSON отсутствует категория '{category}'. Инициализация прервана."
                )
                return {"codes_inserted": 0, "aliases_inserted": 0}
            if not isinstance(data[category], dict):
                print(
                    f"WARNING:  Категория '{category}' должна быть объектом. "
                    f"Инициализация прервана."
                )
                return {"codes_inserted": 0, "aliases_inserted": 0}
            required_keys = ("codes", "aliases_en", "aliases_ru")
            if not all(key in data[category] for key in required_keys):
                print(
                    f"WARNING:  Категория '{category}' должна содержать "
                    f"ключи 'codes', 'aliases_en', 'aliases_ru'. "
                    f"Инициализация прервана."
                )
                return {"codes_inserted": 0, "aliases_inserted": 0}

        # 4. Формирование запросов
        code_queries: list[tuple[str, tuple]] = []
        alias_queries: list[tuple[str, tuple]] = []

        table_map = {
            "objects": (
                "object_reference",
                "object_reference_alias",
            ),
            "disciplines": (
                "discipline_reference",
                "discipline_reference_alias",
            ),
            "documentTypes": (
                "document_type_reference",
                "document_type_reference_alias",
            ),
        }

        for category in categories:
            main_table, alias_table = table_map[category]

            # Коды
            codes = data[category].get("codes", [])
            if not isinstance(codes, list):
                print(
                    f"WARNING:  В категории '{category}' поле 'codes' "
                    f"должно быть списком. Пропуск категории."
                )
                continue
            for code in codes:
                code_str = str(code).strip()
                if not code_str:
                    continue
                code_queries.append(
                    (
                        f"INSERT OR IGNORE INTO {main_table} (code, description) VALUES (?, '')",
                        (code_str,),
                    )
                )

            # Английские псевдонимы
            aliases_en = data[category].get("aliases_en", [])
            if not isinstance(aliases_en, list):
                print(
                    f"WARNING:  В категории '{category}' поле 'aliases_en' "
                    f"должно быть списком. Пропуск английских псевдонимов."
                )
                aliases_en = []
            for item in aliases_en:
                if not isinstance(item, dict):
                    print(
                        f"WARNING:  Некорректный элемент английского псевдонима "
                        f"в категории '{category}': ожидался объект. Пропуск."
                    )
                    continue
                alias = str(item.get("alias", "")).strip()
                code = str(item.get("code", "")).strip()
                if not alias or not code:
                    print(
                        f"WARNING:  Пустой псевдоним или код в категории "
                        f"'{category}' (EN): {item}. Пропуск."
                    )
                    continue
                if code not in {str(c) for c in codes}:
                    print(
                        f"WARNING:  Английский псевдоним '{alias}' ссылается на "
                        f"несуществующий код '{code}' в категории "
                        f"'{category}'. Пропуск."
                    )
                    continue
                alias_queries.append(
                    (
                        (
                            f"INSERT OR IGNORE INTO {alias_table} (alias, code, lang) "
                            "VALUES (?, ?, 'en')"
                        ),
                        (alias, code),
                    )
                )

            # Русские псевдонимы
            aliases_ru = data[category].get("aliases_ru", [])
            if not isinstance(aliases_ru, list):
                print(
                    f"WARNING:  В категории '{category}' поле 'aliases_ru' "
                    f"должно быть списком. Пропуск русских псевдонимов."
                )
                aliases_ru = []
            for item in aliases_ru:
                if not isinstance(item, dict):
                    print(
                        f"WARNING:  Некорректный элемент русского псевдонима "
                        f"в категории '{category}': ожидался объект. Пропуск."
                    )
                    continue
                alias = str(item.get("alias", "")).strip()
                code = str(item.get("code", "")).strip()
                if not alias or not code:
                    print(
                        f"WARNING:  Пустой псевдоним или код в категории "
                        f"'{category}' (RU): {item}. Пропуск."
                    )
                    continue
                if code not in {str(c) for c in codes}:
                    print(
                        f"WARNING:  Русский псевдоним '{alias}' ссылается на "
                        f"несуществующий код '{code}' в категории "
                        f"'{category}'. Пропуск."
                    )
                    continue
                alias_queries.append(
                    (
                        (
                            f"INSERT OR IGNORE INTO {alias_table} (alias, code, lang) "
                            "VALUES (?, ?, 'ru')"
                        ),
                        (alias, code),
                    )
                )

        # 5. Выполнение одной транзакцией (сначала коды, потом псевдонимы)
        total_queries = code_queries + alias_queries
        if total_queries:
            self._db.execute_write_many(total_queries)

        # 6. Возврат статистики
        return {
            "codes_inserted": len(code_queries),
            "aliases_inserted": len(alias_queries),
        }

    def _is_reference_tables_empty(self) -> bool:
        """Проверяет, что все основные справочные таблицы пусты.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Для каждой из таблиц ``object_reference``,           |
        |   | ``discipline_reference``,                            |
        |   | ``document_type_reference`` выполняется запрос       |
        |   | ``SELECT COUNT(*)``.                                |
        +---+-----------------------------------------------------+
        | 2 | Если суммарное количество записей больше нуля,       |
        |   | возвращается ``False``.                             |
        +---+-----------------------------------------------------+

        Returns:
            ``True``, если все три таблицы пусты, иначе ``False``.
        """
        tables = (
            "object_reference",
            "discipline_reference",
            "document_type_reference",
        )
        total = 0
        for table in tables:
            rows = self._db.execute(f"SELECT COUNT(*) FROM {table}")
            if rows:
                total += int(rows[0][0])
        return total == 0
