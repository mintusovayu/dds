"""
Генератор эталонного JSON справочников из Excel-файла.

Модуль предназначен для автономного (вне ядра DDS) формирования
JSON-файла, используемого при инициализации справочников фильтрации
в новой базе данных. Источником служит книга Excel с тремя листами:

- ``wbs``        — справочник объектов;
- ``type``       — справочник типов документов;
- ``discipline`` — справочник дисциплин.

Каждый лист не содержит строки заголовка: первая строка уже содержит
данные. Структура листа (для всех трёх листов):

+-----------+-------------------------+------------------------+
| Столбец 1 | Столбец 2               | Столбец 3              |
+===========+=========================+========================+
| Код       | Псевдоним (английский)  | Псевдоним (русский)    |
+-----------+-------------------------+------------------------+

В каждой строке может быть указан один или два псевдонима; пустые
псевдонимы игнорируются. Псевдонимы из второго столбца относятся
к английскому языку, из третьего — к русскому. Они хранятся отдельно
и не смешиваются.

Выходной JSON имеет структуру, совместимую с API фильтрации:

.. code-block:: json

    {
        "objects": {
            "codes": ["7350"],
            "aliases_en": [
                {"alias": "fire_station", "code": "7350"}
            ],
            "aliases_ru": [
                {"alias": "пожарное депо", "code": "7350"}
            ]
        },
        "disciplines": { ... },
        "documentTypes": { ... }
    }

Использование:

.. code-block:: bash

    python utils/excel2json.py \
        --input "путь/к/справочники.xlsx" \
        --output "docs/default_references.json"

Принципы:
- Модуль не зависит от ядра DDS.
- Единственная внешняя зависимость — ``openpyxl``, которая не входит
  в состав runtime-приложения.
- Обеспечивается валидация данных и атомарная запись JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import Any

try:
    from openpyxl import load_workbook
except ImportError:  # pragma: no cover
    print("Для работы скрипта необходима библиотека openpyxl. Установите её: pip install openpyxl")
    sys.exit(1)


# ----------------------------------------------------------------------
# Константы соответствия листов категориям
# ----------------------------------------------------------------------

SHEET_CATEGORY_MAP: dict[str, str] = {
    "wbs": "objects",
    "type": "documentTypes",
    "discipline": "disciplines",
}
"""Соответствие имён листов Excel ключам выходного JSON."""


# ----------------------------------------------------------------------
# Функции обработки
# ----------------------------------------------------------------------


def _read_sheet_aliases(
    sheet: Any,
) -> tuple[set[str], dict[str, str], dict[str, str]]:
    """Читает один лист и возвращает коды и карты псевдонимов по языкам.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Итерация по строкам листа (``sheet.iter_rows``).    |
    +---+-----------------------------------------------------+
    | 2 | Для каждой строки извлекаются три значения:         |
    |   | код (первая ячейка), псевдоним EN (вторая),         |
    |   | псевдоним RU (третья).                              |
    +---+-----------------------------------------------------+
    | 3 | Код приводится к строке, пробелы обрезаются.        |
    |   | Пустые коды пропускаются с предупреждением.         |
    +---+-----------------------------------------------------+
    | 4 | Для каждого непустого псевдонима (EN и RU отдельно) |
    |   | выполняется проверка уникальности в пределах своего |
    |   | языка: один и тот же псевдоним не может быть связан |
    |   | с разными кодами. При конфликте вызывается          |
    |   | ``ValueError``.                                     |
    +---+-----------------------------------------------------+
    | 5 | Результаты сохраняются в ``codes`` (set),            |
    |   | ``alias_en_to_code`` (dict) и                        |
    |   | ``alias_ru_to_code`` (dict).                         |
    +---+-----------------------------------------------------+

    Args:
        sheet: Объект листа ``openpyxl``.

    Returns:
        Кортеж ``(codes, alias_en_to_code, alias_ru_to_code)``.

    Raises:
        ValueError: Если обнаружен конфликт псевдонимов в одном языке.
    """
    codes: set[str] = set()
    alias_en_to_code: dict[str, str] = {}
    alias_ru_to_code: dict[str, str] = {}

    for row in sheet.iter_rows(min_row=1, values_only=True):
        if not row or not row[0]:
            continue

        code = str(row[0]).strip()
        if not code:
            print("Предупреждение: пропущена строка с пустым кодом.")
            continue
        codes.add(code)

        # Английский псевдоним (второй столбец)
        if len(row) >= 2 and row[1] is not None:
            alias_en = str(row[1]).strip()
            if alias_en:
                existing_code = alias_en_to_code.get(alias_en)
                if existing_code is not None and existing_code != code:
                    raise ValueError(
                        f"Конфликт английского псевдонима '{alias_en}': "
                        f"он уже связан с кодом '{existing_code}', "
                        f"а теперь с кодом '{code}'."
                    )
                alias_en_to_code[alias_en] = code

        # Русский псевдоним (третий столбец)
        if len(row) >= 3 and row[2] is not None:
            alias_ru = str(row[2]).strip()
            if alias_ru:
                existing_code = alias_ru_to_code.get(alias_ru)
                if existing_code is not None and existing_code != code:
                    raise ValueError(
                        f"Конфликт русского псевдонима '{alias_ru}': "
                        f"он уже связан с кодом '{existing_code}', "
                        f"а теперь с кодом '{code}'."
                    )
                alias_ru_to_code[alias_ru] = code

    return codes, alias_en_to_code, alias_ru_to_code


def _build_category(
    codes: set[str],
    alias_en_to_code: dict[str, str],
    alias_ru_to_code: dict[str, str],
) -> dict[str, Any]:
    """Формирует объект категории для выходного JSON.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Сортировка множества кодов.                         |
    +---+-----------------------------------------------------+
    | 2 | Формирование списка ``aliases_en``: сортировка по    |
    |   | псевдониму, каждый элемент — ``{"alias": ...,        |
    |   | "code": ...}``.                                      |
    +---+-----------------------------------------------------+
    | 3 | Аналогично для ``aliases_ru``.                       |
    +---+-----------------------------------------------------+
    | 4 | Возврат словаря с ключами ``"codes"``,                |
    |   | ``"aliases_en"``, ``"aliases_ru"``.                 |
    +---+-----------------------------------------------------+

    Args:
        codes: Множество кодов.
        alias_en_to_code: Английские псевдонимы → код.
        alias_ru_to_code: Русские псевдонимы → код.

    Returns:
        Словарь категории.
    """
    sorted_codes = sorted(codes)
    aliases_en = [
        {"alias": alias, "code": alias_en_to_code[alias]}
        for alias in sorted(alias_en_to_code.keys())
    ]
    aliases_ru = [
        {"alias": alias, "code": alias_ru_to_code[alias]}
        for alias in sorted(alias_ru_to_code.keys())
    ]
    return {
        "codes": sorted_codes,
        "aliases_en": aliases_en,
        "aliases_ru": aliases_ru,
    }


def generate_references(excel_path: str) -> dict[str, Any]:
    """Читает Excel-файл и возвращает словарь справочников.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Загрузка книги Excel через ``load_workbook``.        |
    +---+-----------------------------------------------------+
    | 2 | Проверка наличия всех необходимых листов.            |
    +---+-----------------------------------------------------+
    | 3 | Для каждого листа из ``SHEET_CATEGORY_MAP`` вызывается|
    |   | ``_read_sheet_aliases``.                            |
    +---+-----------------------------------------------------+
    | 4 | Формирование итогового словаря с помощью             |
    |   | ``_build_category``.                                |
    +---+-----------------------------------------------------+

    Args:
        excel_path: Путь к файлу ``.xlsx``.

    Returns:
        Словарь справочников, готовый для сериализации в JSON.

    Raises:
        FileNotFoundError: Если файл не найден.
        ValueError: Если отсутствует один из листов или обнаружен
            конфликт псевдонимов.
    """
    if not os.path.isfile(excel_path):
        raise FileNotFoundError(f"Файл не найден: {excel_path}")

    workbook = load_workbook(filename=excel_path, read_only=True, data_only=True)

    result: dict[str, Any] = {}
    for sheet_name, category_key in SHEET_CATEGORY_MAP.items():
        if sheet_name not in workbook.sheetnames:
            raise ValueError(f"Лист '{sheet_name}' отсутствует в Excel-файле.")

        sheet = workbook[sheet_name]
        codes, alias_en_to_code, alias_ru_to_code = _read_sheet_aliases(sheet)
        result[category_key] = _build_category(codes, alias_en_to_code, alias_ru_to_code)

    workbook.close()
    return result


def _atomic_write_json(data: dict[str, Any], output_path: str) -> None:
    """Атомарно записывает JSON в файл.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Создание каталога для выходного файла (если нужно).  |
    +---+-----------------------------------------------------+
    | 2 | Запись JSON во временный файл в том же каталоге.      |
    +---+-----------------------------------------------------+
    | 3 | Перемещение временного файла на место целевого       |
    |   | (``os.replace``).                                   |
    +---+-----------------------------------------------------+

    Args:
        data: Словарь для сериализации.
        output_path: Путь к целевому JSON-файлу.

    Raises:
        OSError: Если запись не удалась.
    """
    output_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(output_dir, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=output_dir,
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as tmp_file:
        json.dump(data, tmp_file, ensure_ascii=False, indent=2)
        tmp_path = tmp_file.name

    os.replace(tmp_path, output_path)


def main() -> None:
    """Точка входа скрипта генерации JSON.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Парсинг аргументов командной строки.                 |
    +---+-----------------------------------------------------+
    | 2 | Вызов ``generate_references``.                      |
    +---+-----------------------------------------------------+
    | 3 | Атомарная запись результата в JSON.                 |
    +---+-----------------------------------------------------+
    | 4 | Вывод сводки о количестве записей.                   |
    +---+-----------------------------------------------------+
    """
    parser = argparse.ArgumentParser(description="Генерация эталонного JSON справочников из Excel.")
    parser.add_argument(
        "--input",
        required=True,
        help="Путь к Excel-файлу (.xlsx) с листами wbs, type, discipline.",
    )
    parser.add_argument(
        "--output",
        default="default_references.json",
        help="Путь к выходному JSON-файлу (по умолчанию default_references.json).",
    )
    args = parser.parse_args()

    try:
        references = generate_references(args.input)
        _atomic_write_json(references, args.output)
    except FileNotFoundError as e:
        print(f"Ошибка: {e}")
        sys.exit(1)
    except ValueError as e:
        print(f"Ошибка валидации: {e}")
        sys.exit(1)
    except OSError as e:
        print(f"Ошибка записи JSON: {e}")
        sys.exit(1)

    # Вывод сводки
    for category_key, category_name in [
        ("objects", "Объекты"),
        ("disciplines", "Дисциплины"),
        ("documentTypes", "Типы документов"),
    ]:
        codes_count = len(references[category_key]["codes"])
        en_count = len(references[category_key]["aliases_en"])
        ru_count = len(references[category_key]["aliases_ru"])
        print(
            f"{category_name}: кодов — {codes_count}, "
            f"псевдонимов EN — {en_count}, псевдонимов RU — {ru_count}"
        )

    print(f"JSON сохранён в: {os.path.abspath(args.output)}")


if __name__ == "__main__":
    main()
