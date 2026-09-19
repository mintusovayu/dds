"""
Парсинг имени файла для извлечения кодов фильтрации.

Модуль содержит чистую функцию :func:`parse_file_name`, которая
разбирает имя PDF-файла в соответствии с принятым форматом:

    <общий_шифр>-<шифр_объекта>-<номер_дисциплины>-<тип_документа>-<порядковый_номер>-<ревизия>[произвольный текст].pdf

Пример: ``"1800-7350-265-DTL-007-5 - копия.pdf"``.

Из имени извлекаются коды, используемые для фильтрации:
- ``object_code`` — второй сегмент (например, ``"7350"``).
- ``discipline_code`` — третий сегмент (например, ``"265"``).
- ``document_type_code`` — четвёртый сегмент (например, ``"DTL"``).

Псевдонимы и ревизия не сохраняются. Если имя не соответствует
формату (недостаточно сегментов, ревизия не является одиночной
буквой или цифрой и т.п.), функция возвращает объект
:class:`ParsedFileName` с ``is_valid=False`` и пустыми значениями.

Модуль не зависит от базы данных, справочников и других
компонентов системы, что обеспечивает инверсию зависимостей
и лёгкость тестирования.

Принципы:
- Чистая функция без побочных эффектов.
- Не содержит бизнес-логики, кроме разбора строки.
- Полностью типизирована.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ParsedFileName:
    """Результат парсинга имени файла.

    Attributes:
        object_code: Код объекта (второй сегмент имени).
            ``None``, если имя невалидно или код отсутствует.
        discipline_code: Код дисциплины (третий сегмент).
            ``None``, если имя невалидно или код отсутствует.
        document_type_code: Код типа документа (четвёртый сегмент).
            ``None``, если имя невалидно или код отсутствует.
        is_valid: ``True``, если имя файла соответствует ожидаемому
            формату и все три кода были извлечены. ``False`` в
            противном случае.
    """

    object_code: str | None = None
    discipline_code: str | None = None
    document_type_code: str | None = None
    is_valid: bool = False


# Регулярное выражение для разбора имени файла.
# Формат: <seg1>-<seg2>-<seg3>-<seg4>-<seg5>-<rev>[suffix]
# где seg1..seg5 не содержат пробелов и дефисов,
# rev — ровно один алфавитно-цифровой символ,
# suffix — любой текст (включая пустоту), который игнорируется.
_FILE_NAME_PATTERN = re.compile(
    r"^([^\s-]+)-([^\s-]+)-([^\s-]+)-([^\s-]+)-([^\s-]+)-([A-Za-z0-9])(.*)$"
)


def parse_file_name(file_path: str) -> ParsedFileName:
    """Разбирает имя файла и извлекает коды для фильтрации.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Получение базового имени файла из полного пути      |
    |   | (``file_path``) через ``Path(file_path).name``.     |
    +---+-----------------------------------------------------+
    | 2 | Удаление расширения (``.pdf`` и т.п.) через          |
    |   | ``Path(basename).stem``.                            |
    +---+-----------------------------------------------------+
    | 3 | Применение регулярного выражения                     |
    |   | :data:`_FILE_NAME_PATTERN` к имени без расширения.  |
    +---+-----------------------------------------------------+
    | 4 | Если шаблон не совпал или какие-либо извлечённые    |
    |   | коды пусты, возвращается ``ParsedFileName`` с        |
    |   | ``is_valid=False``.                                  |
    +---+-----------------------------------------------------+
    | 5 | В случае успеха формируется и возвращается объект    |
    |   | ``ParsedFileName`` с заполненными полями             |
    |   | ``object_code``, ``discipline_code``,                |
    |   | ``document_type_code`` и ``is_valid=True``.          |
    +---+-----------------------------------------------------+

    Args:
        file_path: Полный или относительный путь к файлу.
            Может содержать каталоги.

    Returns:
        Объект :class:`ParsedFileName` с извлечёнными кодами
        и признаком валидности.

    Examples:
        >>> parse_file_name("/data/1800-7350-265-DTL-007-5 - копия.pdf")
        ParsedFileName(object_code='7350', discipline_code='265',
                       document_type_code='DTL', is_valid=True)

        >>> parse_file_name("bad_name.pdf")
        ParsedFileName(object_code=None, discipline_code=None,
                       document_type_code=None, is_valid=False)
    """
    # 1. Базовое имя файла
    basename = Path(file_path).name
    # 2. Имя без расширения
    stem = Path(basename).stem

    # 3. Сопоставление с шаблоном
    match = _FILE_NAME_PATTERN.match(stem)
    if not match:
        return ParsedFileName()

    # Группы: 1=общий шифр, 2=объект, 3=дисциплина, 4=тип, 5=номер, 6=ревизия
    object_code = match.group(2)
    discipline_code = match.group(3)
    document_type_code = match.group(4)

    # Дополнительная проверка: коды не должны быть пустыми (хотя шаблон
    # гарантирует непустоту, но оставлено для ясности).
    if not object_code or not discipline_code or not document_type_code:
        return ParsedFileName()

    return ParsedFileName(
        object_code=object_code,
        discipline_code=discipline_code,
        document_type_code=document_type_code,
        is_valid=True,
    )
