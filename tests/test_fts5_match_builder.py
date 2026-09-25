"""
Тесты сборки FTS5 MATCH-выражения из токенов.

Назначение
----------
Проверка контракта модуля
:mod:`dds_core.infrastructure.fts5.match_builder`:

- :func:`build_match_expression` — сборка MATCH-выражения из
  списка :class:`~dds_core.application.query_tokenizer.QueryToken`;
- :func:`normalize_search_query` — обёртка над
  ``tokenize_search_query + build_match_expression``, сохраняющая
  привычный API для потребителя
  (``dds_core/infrastructure/fts5_search_backend.py``).

Модуль введён в Фазе 3 (Domain cleanup) как часть разделения
удалённого ``dds_core.application.search_query_normalizer``.
Здесь сосредоточена вся FTS5-специфика: префикс колонки
``normalized_text:``, нормализация текста через
:func:`dds_core.domain.text_normalization.normalize_text`,
форматирование фраз в двойных кавычках.

Ожидаемые значения: частичная транслитерация
--------------------------------------------

Ключевая особенность тестов — **ожидаемые значения содержат
смешанную кириллицу и латиницу**. Это не опечатка, а прямое
следствие контракта :func:`~dds_core.domain.text_normalization.normalize_text`.

``normalize_text`` выполняет **частичную** замену: только символы
из :data:`~dds_core.domain.text_normalization.CYRILLIC_TO_LATIN_MAP`
(12 пар — визуально похожие кириллические и латинские символы:
``а→a``, ``в→b``, ``е→e``, ``к→k``, ``м→m``, ``н→h``, ``о→o``,
``р→p``, ``с→c``, ``т→t``, ``у→y``, ``х→x``). Символы, отсутствующие
в таблице (``г``, ``д``, ``ш``, ``п``, ``б`` и др.), остаются
кириллическими.

Например::

    normalize_text("гидрошпонка")
    # г и д остаются кириллицей; р→p, о→o, о→o, н→h, к→k, а→a
    # → "гидpoшпohka"

    normalize_text("корпус крупного дробления")
    # → "kopпyc kpyпhoгo дpoблehия"

Такое поведение — осознанное решение задачи **нечувствительности
к раскладке клавиатуры**, а не «перевода на латиницу». Смысл в
том, что пользователь, набравший ``ЕС-423-1`` в русской раскладке
и ``EC-423-1`` в английской, получит одинаковые результаты поиска.
Полная транслитерация не требуется и не выполняется.

Ожидаемые значения в тестах соответствуют именно этому — частично
транслитерированному — виду. В строках, где кириллица и латиница
перемешаны без визуального разделителя, это выглядит необычно, но
семантически корректно. Дополнительное обоснование — ADR-003
(Domain Text Normalization).

Проверяемые сценарии
--------------------

+-------------------------------------+--------------------------------+
| Группа                              | Что проверяется                |
+=====================================+================================+
| ``build_match_expression``:         | Пустой список токенов → пустая |
| пустой вход                         | строка.                        |
+-------------------------------------+--------------------------------+
| ``build_match_expression``:         | Один ``WordToken`` → префикс   |
| одно слово                          | колонки + нормализованный      |
|                                     | текст.                         |
+-------------------------------------+--------------------------------+
| ``build_match_expression``:         | Одна ``PhraseToken`` → префикс |
| одна фраза                          | колонки + кавычки +            |
|                                     | нормализованный текст.         |
+-------------------------------------+--------------------------------+
| ``build_match_expression``:         | Оператор переносится как есть. |
| операторы                           |                                |
+-------------------------------------+--------------------------------+
| ``build_match_expression``:         | Скобка переносится как есть    |
| скобки                              | (префикс колонки применяется   |
|                                     | только к словам и фразам).     |
+-------------------------------------+--------------------------------+
| ``build_match_expression``:         | Комплексные последовательности |
| смешанные последовательности        | токенов — корректный порядок.  |
+-------------------------------------+--------------------------------+
| ``build_match_expression``:         | Кириллица частично             |
| нормализация                        | нормализуется; латиница        |
|                                     | остаётся как есть.             |
+-------------------------------------+--------------------------------+
| ``normalize_search_query``:         | Примеры из docstring.          |
| docstring                           |                                |
+-------------------------------------+--------------------------------+
| ``normalize_search_query``:         | Эквивалентность композиции     |
| эквивалентность                     | tokenize + build.              |
+-------------------------------------+--------------------------------+
| ``normalize_search_query``:         | Пустой запрос → пустая строка. |
| пустой вход                         |                                |
+-------------------------------------+--------------------------------+
| ``normalize_search_query``:         | Совпадение с поведением        |
| регресс от старой функции           | удалённой функции из           |
|                                     | ``search_query_normalizer``.   |
+-------------------------------------+--------------------------------+

Стратегия тестирования
----------------------
- **Явная проверка обеих функций.** ``build_match_expression``
  тестируется отдельно от ``normalize_search_query`` — это
  позволяет локализовать ошибку: если сломан токенизатор,
  падает ``normalize_search_query``; если сломан сборщик,
  падает ``build_match_expression``.
- **Точные ожидаемые строки.** MATCH-выражение — строка с
  жёстко зафиксированным форматом (пробелы, кавычки, префикс,
  частичная транслитерация). Сравнение идёт по точной строке,
  не по regex. Если формат или правило нормализации изменится,
  тесты поймают расхождение.
- **Примеры из docstring дублируются.** Защита от расхождения
  между описанием и поведением.
- **Эквивалентность старой функции.** Совпадение с удалённой
  ``search_query_normalizer.normalize_search_query`` проверяется
  на наборе репрезентативных запросов — защита от регресса
  при переносе.
- **Тесты не читают/не пишут файлы, не имеют моков.**
  Функции чистые.

Границы
-------
- **Токенизация** — покрывается ``tests/test_query_tokenizer.py``.
- **Нормализация текста** — покрывается
  ``tests/test_text_normalization_domain.py``.
- **Интеграция с FTS5-бэкендом** — покрывается тестами
  ``fts5_search_backend`` (реальные SQL-запросы).
- **Валидация синтаксиса FTS5** — вне области: сборщик
  формирует корректную по форме строку, семантику проверяет
  SQLite.

Запуск
------
::

    pytest tests/test_fts5_match_builder.py -v

Принципы:
    - модуль не выполняет логирования;
    - не читает и не пишет файлы;
    - не имеет побочных эффектов при импорте;
    - каждый тест изолирован, не зависит от порядка выполнения;
    - тесты детерминированы: одинаковый вход → одинаковый результат.
"""

from __future__ import annotations

import pytest
from dds_core.application.query_tokenizer import (
    OperatorToken,
    ParenToken,
    PhraseToken,
    QueryToken,
    WordToken,
    tokenize_search_query,
)
from dds_core.infrastructure.fts5.match_builder import (
    build_match_expression,
    normalize_search_query,
)

# =====================================================================
# Раздел 1. build_match_expression: пустой вход
# =====================================================================


def test_build_match_expression_empty_list() -> None:
    """Пустой список токенов → пустая строка.

    Соответствует семантике «пустой запрос»: MATCH-выражение
    не формируется, поиск не выполняется.
    """
    assert build_match_expression([]) == ""


# =====================================================================
# Раздел 2. build_match_expression: одно слово
# =====================================================================


def test_build_match_expression_single_word() -> None:
    """Одно слово → префикс колонки + нормализованный текст.

    ``гидрошпонка`` проходит частичную нормализацию: ``р→p``,
    ``о→o``, ``н→h``, ``к→k``, ``а→a``; символы ``г``, ``и``, ``д``,
    ``ш``, ``п`` не входят в :data:`CYRILLIC_TO_LATIN_MAP` и
    остаются кириллическими. Результат — ``гидpoшпohka``
    (визуально смешанная кириллица/латиница).

    Тест фиксирует именно это поведение — контракт
    «нечувствительность к раскладке», не «транслитерация».
    """
    assert build_match_expression([WordToken(text="гидрошпонка")]) == (
        "normalized_text: гидpoшпohka"
    )


def test_build_match_expression_single_word_latin() -> None:
    """Латинское слово не меняется (кроме lower).

    ``HYDROSEAL`` → ``hydroseal`` (только lower, замены по словарю
    не применяются — латинские символы не входят в ключи
    :data:`CYRILLIC_TO_LATIN_MAP`).
    """
    assert build_match_expression([WordToken(text="HYDROSEAL")]) == ("normalized_text: hydroseal")


def test_build_match_expression_single_word_with_asterisk() -> None:
    """Символ ``*`` внутри слова сохраняется.

    Префиксный поиск FTS5: ``гидро*`` → ``normalized_text: гидpo*``.
    Нормализация ``normalize_text`` не удаляет ``*`` (символ
    отсутствует в таблице замен).
    """
    assert build_match_expression([WordToken(text="гидро*")]) == ("normalized_text: гидpo*")


def test_build_match_expression_word_case_insensitive() -> None:
    """Регистр слова не важен: ``normalize_text`` приводит к lower.

    ``ГИДРОШПОНКА`` после lower и частичной замены даёт то же,
    что и ``гидрошпонка``.
    """
    assert build_match_expression([WordToken(text="ГИДРОШПОНКА")]) == (
        "normalized_text: гидpoшпohka"
    )


# =====================================================================
# Раздел 3. build_match_expression: одна фраза
# =====================================================================


def test_build_match_expression_single_phrase() -> None:
    """Одна фраза → префикс колонки + кавычки + нормализованный текст.

    Фраза оборачивается в двойные кавычки после префикса колонки:
    ``normalized_text: "..."``. Пробел между ``:`` и открывающей
    кавычкой обязателен — так FTS5 интерпретирует префикс колонки.

    Внутренний текст проходит частичную нормализацию:
    ``корпус крупного дробления`` → ``kopпyc kpyпhoгo дpoблehия``.
    """
    assert (
        build_match_expression([PhraseToken(text="корпус крупного дробления")])
        == 'normalized_text: "kopпyc kpyпhoгo дpoблehия"'
    )


def test_build_match_expression_phrase_preserves_internal_spaces() -> None:
    """Внутренние пробелы фразы сохраняются после нормализации."""
    assert build_match_expression([PhraseToken(text="a   b")]) == ('normalized_text: "a   b"')


def test_build_match_expression_phrase_case_insensitive() -> None:
    """Регистр фразы не важен: ``normalize_text`` приводит к lower.

    ``Крупный Корпус`` → ``kpyпhый kopпyc``.
    """
    assert build_match_expression([PhraseToken(text="Крупный Корпус")]) == (
        'normalized_text: "kpyпhый kopпyc"'
    )


def test_build_match_expression_empty_phrase() -> None:
    """Пустая фраза → префикс колонки + пустые кавычки.

    Формально корректная строка (с точки зрения сборщика);
    семантику проверяет FTS5 на этапе ``MATCH``.
    """
    assert build_match_expression([PhraseToken(text="")]) == 'normalized_text: ""'


# =====================================================================
# Раздел 4. build_match_expression: операторы
# =====================================================================


@pytest.mark.parametrize(
    "op",
    ["AND", "OR", "NOT"],
    ids=["and", "or", "not"],
)
def test_build_match_expression_operator(op: str) -> None:
    """Оператор переносится как есть (каноническая форма).

    Токенизатор уже привёл оператор к верхнему регистру, поэтому
    сборщик просто передаёт его в результирующую строку.
    """
    assert build_match_expression([OperatorToken(op=op)]) == op


def test_build_match_expression_word_and_operator() -> None:
    """Слово + оператор → префикс колонки у слова, оператор как есть.

    Слово ``a`` — латинское, нормализация не меняет его.

    Список токенов аннотирован ``list[QueryToken]``: гетерогенный
    список ``[WordToken, OperatorToken, WordToken]`` иначе выводится
    mypy как ``list[object]``, что несовместимо с ``list[QueryToken]``
    (list инвариантен).
    """
    tokens: list[QueryToken] = [
        WordToken(text="a"),
        OperatorToken(op="AND"),
        WordToken(text="b"),
    ]
    assert build_match_expression(tokens) == "normalized_text: a AND normalized_text: b"


# =====================================================================
# Раздел 5. build_match_expression: скобки
# =====================================================================


@pytest.mark.parametrize(
    "char",
    ["(", ")"],
    ids=["open", "close"],
)
def test_build_match_expression_paren(char: str) -> None:
    """Скобка переносится как есть.

    Префикс колонки ``normalized_text:`` применяется только к
    словам и фразам. Скобки и операторы остаются «сырыми».
    """
    assert build_match_expression([ParenToken(char=char)]) == char


def test_build_match_expression_word_in_parens() -> None:
    """Слово в скобках → корректная последовательность.

    Префикс колонки применяется к слову, а не к скобкам:
    ``(`` + ``normalized_text: a`` + ``)``, разделённые пробелами.
    FTS5 корректно интерпретирует такой запрос.

    Список токенов аннотирован ``list[QueryToken]`` (см. комментарий
    в :func:`test_build_match_expression_word_and_operator`).
    """
    tokens: list[QueryToken] = [
        ParenToken(char="("),
        WordToken(text="a"),
        ParenToken(char=")"),
    ]
    assert build_match_expression(tokens) == "( normalized_text: a )"


# =====================================================================
# Раздел 6. build_match_expression: смешанные последовательности
# =====================================================================


def test_build_match_expression_complex_query() -> None:
    """Комплексный запрос с операторами, фразами и скобками.

    Эмулирует реальный запрос пользователя
    ``(гидрошпонка OR "корпус крупного дробления") NOT арматура``.
    Проверяет порядок токенов и точный формат MATCH-выражения
    (включая частичную нормализацию слов и фраз).

    Список токенов аннотирован ``list[QueryToken]``: гетерогенный
    список из четырёх типов токенов иначе выводится mypy как
    ``list[object]``.
    """
    tokens: list[QueryToken] = [
        ParenToken(char="("),
        WordToken(text="гидрошпонка"),
        OperatorToken(op="OR"),
        PhraseToken(text="корпус крупного дробления"),
        ParenToken(char=")"),
        OperatorToken(op="NOT"),
        WordToken(text="арматура"),
    ]
    expected = (
        "( normalized_text: гидpoшпohka "
        'OR normalized_text: "kopпyc kpyпhoгo дpoблehия" '
        ") NOT normalized_text: apmatypa"
    )
    assert build_match_expression(tokens) == expected


def test_build_match_expression_latin_codes() -> None:
    """Технические коды ``EC-423-1`` экранируются как phrase-термин.

    Фаза 8 (ADR-008): слова со спецсимволами FTS5 (в данном случае
    дефис) оборачиваются в двойные кавычки — устраняет ошибку
    ``no such column: 423``, которая возникала при парсинге
    запроса ``EC-423-1`` как ``ec`` ``-`` ``423`` ``-`` ``1``.

    Латиница не меняется (lower + кириллица → латиница);
    дефисы и цифры сохраняются внутри кавычек.

    Список токенов аннотирован ``list[QueryToken]``: гетерогенный
    список ``[WordToken, OperatorToken, WordToken]`` иначе выводится
    mypy как ``list[object]``.
    """
    tokens: list[QueryToken] = [
        WordToken(text="EC-423-1"),
        OperatorToken(op="OR"),
        WordToken(text="ec-423-1"),
    ]
    expected = 'normalized_text: "ec-423-1" OR normalized_text: "ec-423-1"'
    assert build_match_expression(tokens) == expected


# =====================================================================
# Раздел 7. normalize_search_query: примеры из docstring
# =====================================================================


def test_normalize_search_query_docstring_example_1() -> None:
    """Пример 1 из docstring: слово + AND + фраза.

    Проверяет согласованность между описанием и поведением.
    """
    assert normalize_search_query('гидрошпонка AND "корпус крупного дробления"') == (
        'normalized_text: гидpoшпohka AND normalized_text: "kopпyc kpyпhoгo дpoблehия"'
    )


def test_normalize_search_query_docstring_example_2() -> None:
    """Пример 2 из docstring: скобки, OR, NOT, коды.

    Фаза 8 (ADR-008): слова с дефисами экранируются как
    phrase-термины. Латиница не меняется; скобки и операторы
    переносятся как есть.
    """
    assert normalize_search_query("(ЕС-423-1 OR EC-423-1) NOT KM") == (
        '( normalized_text: "ec-423-1" OR normalized_text: "ec-423-1" ) NOT normalized_text: km'
    )


def test_normalize_search_query_docstring_example_3() -> None:
    """Пример 3 из docstring: пустой запрос → пустая строка."""
    assert normalize_search_query("") == ""


# =====================================================================
# Раздел 8. normalize_search_query: эквивалентность композиции
# =====================================================================


@pytest.mark.parametrize(
    "query",
    [
        "гидрошпонка",
        "гидрошпонка AND корпус",
        "гидрошпонка OR корпус NOT арматура",
        '"корпус крупного дробления"',
        'гидрошпонка AND "корпус крупного дробления"',
        "(a OR b) AND c",
        "((a OR b) NOT c) OR d",
        "EC-423-1",
        "EC-423-1 OR ec-423-1",
        "гидро*",
        '"гидро*"',
        "",
        "   ",
    ],
    ids=[
        "single-word",
        "two-words-and",
        "three-words-or-not",
        "single-phrase",
        "word-and-phrase",
        "parens-simple",
        "parens-nested",
        "latin-code",
        "latin-codes-or",
        "prefix-search",
        "phrase-prefix",
        "empty",
        "spaces-only",
    ],
)
def test_normalize_search_query_equivalent_to_composition(query: str) -> None:
    """``normalize_search_query`` эквивалентен композиции tokenize + build.

    Инвариант:
    ``normalize_search_query(q) ==
      build_match_expression(tokenize_search_query(q))``
    для любого запроса. Защищает от случайного расхождения между
    обёрткой и её составными частями.
    """
    assert normalize_search_query(query) == build_match_expression(tokenize_search_query(query))


# =====================================================================
# Раздел 9. normalize_search_query: пустой вход
# =====================================================================


@pytest.mark.parametrize(
    "query",
    ["", "   ", "\t\n"],
    ids=["empty", "spaces", "whitespace"],
)
def test_normalize_search_query_empty_input(query: str) -> None:
    """Пустой запрос → пустая строка MATCH-выражения."""
    assert normalize_search_query(query) == ""


# =====================================================================
# Раздел 10. normalize_search_query: защита от регресса
# =====================================================================


def test_normalize_search_query_equivalence_with_removed_module() -> None:
    """Поведение эквивалентно удалённой ``search_query_normalizer``.

    Защита от регресса при переносе из
    ``dds_core.application.search_query_normalizer`` в
    ``dds_core.infrastructure.fts5.match_builder``. Проверяется
    на репрезентативных запросах, охватывающих все ветки
    форматирования: слово, фраза, оператор, скобка, кириллица,
    латиница, смешанные.

    Ожидаемые значения соответствуют **частичной** нормализации:
    только символы из :data:`CYRILLIC_TO_LATIN_MAP` заменяются
    на латинские. Остальные кириллические символы остаются как
    есть. Это контракт ``normalize_text`` (нечувствительность к
    раскладке, а не транслитерация), и он не изменился при переносе.
    """
    cases = {
        # Слово с частичной нормализацией: р→p, о→o, н→h, к→k, а→a.
        "гидрошпонка": "normalized_text: гидpoшпohka",
        "Гидрошпонка": "normalized_text: гидpoшпohka",
        # Латиница остаётся как есть.
        "hydroseal": "normalized_text: hydroseal",
        # Фраза → префикс + кавычки; внутри — частичная нормализация.
        '"a b"': 'normalized_text: "a b"',
        # Оператор в нижнем регистре → верхний.
        "a and b": "normalized_text: a AND normalized_text: b",
        # Смешанный запрос: скобка, слово, оператор OR, слово,
        # скобка, оператор NOT, слово.
        "(a or b) not c": ("( normalized_text: a OR normalized_text: b ) NOT normalized_text: c"),
        # Префиксный поиск: ``*`` сохраняется, кириллица нормализуется.
        "гидро*": "normalized_text: гидpo*",
    }
    for query, expected in cases.items():
        assert normalize_search_query(query) == expected, (
            f"Регресс для запроса {query!r}: "
            f"ожидалось {expected!r}, получено {normalize_search_query(query)!r}"
        )


# =====================================================================
# Раздел 11. Свойства функции (идемпотентность, чистота)
# =====================================================================


@pytest.mark.parametrize(
    "query",
    [
        "гидрошпонка",
        "гидрошпонка AND корпус",
        '"корпус крупного дробления"',
        "(a OR b) NOT c",
        "",
    ],
    ids=["word", "words-and", "phrase", "complex", "empty"],
)
def test_normalize_search_query_is_idempotent(query: str) -> None:
    """Повторный вызов даёт тот же результат.

    Инвариант: ``normalize_search_query(q)`` — детерминированная
    функция без побочных эффектов. Повторный вызов возвращает
    идентичную строку.
    """
    once = normalize_search_query(query)
    twice = normalize_search_query(query)
    assert once == twice


def test_build_match_expression_does_not_mutate_tokens() -> None:
    """``build_match_expression`` не изменяет входной список.

    Токены — ``frozen=True`` dataclasses; сборщик формирует
    новую строку, не модифицируя ни список, ни его элементы.
    Проверяется сравнением списка до и после вызова.

    Список токенов аннотирован ``list[QueryToken]`` (см. комментарий
    в :func:`test_build_match_expression_word_and_operator`).
    """
    tokens: list[QueryToken] = [
        WordToken(text="гидрошпонка"),
        OperatorToken(op="AND"),
        WordToken(text="корпус"),
    ]
    snapshot = list(tokens)

    _ = build_match_expression(tokens)

    assert tokens == snapshot


# ----------------------------------------------------------------------
# Раздел: Экранирование FTS5-спецсимволов (Фаза 8, ADR-008)
# ----------------------------------------------------------------------
#
# До Фазы 8 функция build_match_expression не экранировала токены,
# содержащие FTS5-спецсимволы. Запрос EC-423-1 нормализовался в
# "normalized_text: ec-423-1", что FTS5 парсил как ec - 423 - 1
# и падал с ошибкой "no such column: 423".
#
# Функция _build_word_match оборачивает такие токены в двойные
# кавычки (FTS5-quoting) с удвоением внутренних ". Trailing *
# сохраняет prefix-семантику для токенов без спецсимволов.


def test_word_with_hyphen_is_quoted() -> None:
    """Слово с дефисом оборачивается в двойные кавычки."""
    tokens = [WordToken(text="EC-423-1")]
    result = build_match_expression(tokens)
    assert result == 'normalized_text: "ec-423-1"'


def test_word_with_plus_is_quoted() -> None:
    """Слово с плюсом оборачивается в двойные кавычки."""
    tokens = [WordToken(text="A+B")]
    result = build_match_expression(tokens)
    assert result == 'normalized_text: "a+b"'


def test_word_with_colon_is_quoted() -> None:
    """Слово с двоеточием оборачивается в двойные кавычки."""
    tokens = [WordToken(text="col:value")]
    result = build_match_expression(tokens)
    assert result == 'normalized_text: "col:value"'


def test_word_with_caret_is_quoted() -> None:
    """Слово с циркумфлексом оборачивается в двойные кавычки."""
    tokens = [WordToken(text="^EC")]
    result = build_match_expression(tokens)
    assert result == 'normalized_text: "^ec"'


def test_word_with_braces_is_quoted() -> None:
    """Слово с фигурными скобками оборачивается в двойные кавычки."""
    tokens = [WordToken(text="a{b}c")]
    result = build_match_expression(tokens)
    assert result == 'normalized_text: "a{b}c"'


def test_word_with_inner_quote_is_escaped() -> None:
    """Внутренние " удваиваются при оборачивании в кавычки.

    Согласно синтаксису FTS5, чтобы включить двойную кавычку
    внутрь phrase-термина, её нужно удвоить ("").
    """
    tokens = [WordToken(text='a"b')]
    result = build_match_expression(tokens)
    assert result == 'normalized_text: "a""b"'


def test_plain_cyrillic_word_not_quoted() -> None:
    """Слово без спецсимволов не оборачивается в кавычки."""
    tokens = [WordToken(text="гидрошпонка")]
    result = build_match_expression(tokens)
    assert result == "normalized_text: гидpoшпohka"


def test_word_with_trailing_star_keeps_prefix_semantics() -> None:
    """Trailing * сохраняет prefix-семантику для слова без спецсимволов."""
    tokens = [WordToken(text="гидро*")]
    result = build_match_expression(tokens)
    assert result == "normalized_text: гидpo*"


def test_word_with_hyphen_and_trailing_star_drops_prefix() -> None:
    """Trailing * отбрасывается при наличии спецсимволов в токене.

    Осознанное ограничение (см. docstring _build_word_match):
    prefix-поиск после unicode61-токенизации токена со
    спецсимволами не имеет однозначной семантики.
    """
    tokens = [WordToken(text="EC-423-*")]
    result = build_match_expression(tokens)
    assert result == 'normalized_text: "ec-423-"'


def test_phrase_with_hyphen_not_modified() -> None:
    """PhraseToken с дефисом не экранируется — фраза уже в кавычках.

    Дополнительное экранирование внутри фразы не применяется:
    токенизатор не позволяет " внутри PhraseToken.text.
    """
    tokens = [PhraseToken(text="EC-423-1")]
    result = build_match_expression(tokens)
    assert result == 'normalized_text: "ec-423-1"'


def test_normalize_search_query_with_hyphenated_code() -> None:
    """Интеграционная проверка: normalize_search_query не падает на EC-423-1."""
    result = normalize_search_query("EC-423-1")
    assert result == 'normalized_text: "ec-423-1"'


def test_normalize_search_query_with_prefix_and_special_chars() -> None:
    """Префиксный поиск без спецсимволов работает как раньше."""
    result = normalize_search_query("гидро*")
    assert result == "normalized_text: гидpo*"


def test_normalize_search_query_parens_with_hyphenated_codes() -> None:
    """Комбинированный запрос: скобки + дефисные коды + оператор NOT."""
    result = normalize_search_query("(EC-423-1 OR EC-423-1) NOT KM")
    assert result == (
        '( normalized_text: "ec-423-1" OR normalized_text: "ec-423-1" ) NOT normalized_text: km'
    )
