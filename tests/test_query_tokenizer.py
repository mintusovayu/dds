"""
Тесты токенизации поискового запроса в discriminated union.

Назначение
----------
Проверка контракта
:func:`dds_core.application.query_tokenizer.tokenize_search_query`:
функция разбирает поисковый запрос с учётом синтаксиса поисковой
грамматики DDS (логические операторы, фразы в двойных кавычках,
круглые скобки) в список типизированных токенов.

Модуль введён в Фазе 3 (Domain cleanup) как часть разделения
удалённого ``dds_core.application.search_query_normalizer``:

- :mod:`dds_core.application.query_tokenizer` (этот модуль
  тестирует его) — структурный разбор запроса в токены.
- :mod:`dds_core.infrastructure.fts5.match_builder` — сборка
  FTS5 MATCH-выражения из токенов.

Тесты проверяют **только токенизацию**, без привязки к FTS5:
- распознавание четырёх видов токенов (``WordToken``,
  ``PhraseToken``, ``OperatorToken``, ``ParenToken``);
- сохранение оригинального текста (токенизатор **не нормализует**
  и не изменяет символы);
- сохранение порядка токенов;
- распознавание операторов без учёта регистра;
- краевые случаи регулярного выражения (пустые фразы, скобки без
  пробелов, спецсимволы).

Проверяемые сценарии
--------------------

+-----------------------------------+---------------------------------+
| Группа                            | Что проверяется                 |
+===================================+=================================+
| Пустой запрос                     | ``""`` и строка из пробелов →   |
|                                   | ``[]``.                         |
+-----------------------------------+---------------------------------+
| Одно слово                        | ``WordToken`` с оригинальным    |
|                                   | текстом.                        |
+-----------------------------------+---------------------------------+
| Несколько слов                    | Список ``WordToken`` в порядке  |
|                                   | появления.                      |
+-----------------------------------+---------------------------------+
| Операторы                         | ``AND`` / ``OR`` / ``NOT``      |
|                                   | распознаются без учёта          |
|                                   | регистра, приводятся к          |
|                                   | канонической форме.             |
+-----------------------------------+---------------------------------+
| Фразы в кавычках                  | ``PhraseToken`` с внутренними   |
|                                   | пробелами.                      |
+-----------------------------------+---------------------------------+
| Скобки                            | ``ParenToken`` с ``"("`` или    |
|                                   | ``")"``.                        |
+-----------------------------------+---------------------------------+
| Смешанные запросы                 | Комплексная последовательность  |
|                                   | из всех типов токенов.          |
+-----------------------------------+---------------------------------+
| Спецсимволы в словах              | ``*``, ``-``, ``.``, цифры —    |
|                                   | остаются частью ``WordToken``.  |
+-----------------------------------+---------------------------------+
| Сохранение оригинального текста   | Регистр и символы не            |
|                                   | изменяются.                     |
+-----------------------------------+---------------------------------+
| Порядок токенов                   | Соответствует порядку в         |
|                                   | исходной строке.                |
+-----------------------------------+---------------------------------+
| Краевые случаи                    | Пустая фраза, скобки без        |
|                                   | пробелов, кириллические         |
|                                   | «операторы» не распознаются     |
|                                   | как операторы.                  |
+-----------------------------------+---------------------------------+

Стратегия тестирования
----------------------
- **Проверка типов через ``isinstance`` и явное сравнение полей.**
  Токены — ``@dataclass(frozen=True)``; для проверки достаточно
  создать ожидаемый токен и сравнить через ``==`` (dataclass
  генерирует ``__eq__`` по полям). Это даёт точные сообщения
  об ошибках при расхождении.
- **Полное сравнение списков.** Каждый тест сравнивает
  результирующий список токенов целиком с ожидаемым. Это
  гарантирует, что токенизатор не создаёт лишних токенов и не
  пропускает существующие.
- **Точечные параметризованные тесты для операторов.** Три
  оператора × три регистра (``AND``, ``and``, ``AnD``) —
  девять проверок в одной параметризации.
- **Без нормализации в тестах.** Тесты намеренно используют
  строки с кириллицей и латиницей в оригинальном регистре:
  токенизатор не должен их менять.

Границы
-------
- **Сборка MATCH-выражения** — покрывается
  ``tests/test_fts5_match_builder.py``.
- **Нормализация текста** — покрывается
  ``tests/test_text_normalization_domain.py``.
- **Валидация синтаксиса** (парность скобок, сбалансированность
  кавычек) — не входит в контракт токенизатора; синтаксические
  ошибки выявляет FTS5 на этапе ``MATCH``.
- **Производительность** — не измеряется.

Запуск
------
::

    pytest tests/test_query_tokenizer.py -v

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

# =====================================================================
# Раздел 1. Пустой запрос
# =====================================================================


@pytest.mark.parametrize(
    "query",
    ["", "   ", "\t", "\n", "  \t\n  "],
    ids=["empty", "spaces", "tab", "newline", "mixed-whitespace"],
)
def test_empty_query_returns_empty_list(query: str) -> None:
    """Пустой запрос или запрос из пробелов → пустой список.

    Проверяет ранний выход в ``tokenize_search_query``: если
    ``query.strip()`` пуст, функция возвращает ``[]`` без обхода
    регулярным выражением.
    """
    assert tokenize_search_query(query) == []


# =====================================================================
# Раздел 2. Одно слово
# =====================================================================


def test_single_word() -> None:
    """Одно слово → список из одного :class:`WordToken`."""
    assert tokenize_search_query("гидрошпонка") == [WordToken(text="гидрошпонка")]


def test_single_word_latin() -> None:
    """Одно латинское слово → :class:`WordToken`."""
    assert tokenize_search_query("hydroseal") == [WordToken(text="hydroseal")]


def test_single_word_with_digits_and_hyphens() -> None:
    """Технический код ``EC-423-1`` — одно слово.

    Дефисы и цифры не являются разделителями для токенизатора:
    ``word``-группа регулярного выражения захватывает любые
    непробельные символы, кроме круглых скобок.
    """
    assert tokenize_search_query("EC-423-1") == [WordToken(text="EC-423-1")]


def test_single_word_with_asterisk() -> None:
    """Префиксный поиск ``гидро*`` — одно слово.

    Символ ``*`` не интерпретируется токенизатором: он остаётся
    частью текста и обрабатывается FTS5 на этапе ``MATCH``.
    """
    assert tokenize_search_query("гидро*") == [WordToken(text="гидро*")]


# =====================================================================
# Раздел 3. Несколько слов
# =====================================================================


def test_two_words() -> None:
    """Два слова без оператора → два :class:`WordToken`."""
    assert tokenize_search_query("гидрошпонка корпус") == [
        WordToken(text="гидрошпонка"),
        WordToken(text="корпус"),
    ]


def test_multiple_spaces_between_words() -> None:
    """Множественные пробелы между словами игнорируются."""
    assert tokenize_search_query("гидрошпонка    корпус") == [
        WordToken(text="гидрошпонка"),
        WordToken(text="корпус"),
    ]


def test_tabs_and_newlines_between_words() -> None:
    """Табы и переносы строк работают как разделители."""
    assert tokenize_search_query("гидрошпонка\tкорпус\nдробление") == [
        WordToken(text="гидрошпонка"),
        WordToken(text="корпус"),
        WordToken(text="дробление"),
    ]


# =====================================================================
# Раздел 4. Операторы
# =====================================================================


@pytest.mark.parametrize(
    ("raw", "expected_op"),
    [
        ("AND", "AND"),
        ("and", "AND"),
        ("And", "AND"),
        ("aNd", "AND"),
        ("OR", "OR"),
        ("or", "OR"),
        ("Or", "OR"),
        ("NOT", "NOT"),
        ("not", "NOT"),
        ("Not", "NOT"),
    ],
    ids=[
        "AND-upper",
        "AND-lower",
        "AND-mixed1",
        "AND-mixed2",
        "OR-upper",
        "OR-lower",
        "OR-mixed",
        "NOT-upper",
        "NOT-lower",
        "NOT-mixed",
    ],
)
def test_operator_case_insensitive(raw: str, expected_op: str) -> None:
    """Операторы распознаются без учёта регистра.

    Каноническая форма — верхний регистр (``AND``, ``OR``,
    ``NOT``). Сравнение идёт по ``word.upper()``.
    """
    assert tokenize_search_query(raw) == [OperatorToken(op=expected_op)]


def test_operator_between_words() -> None:
    """Оператор между словами распознаётся как :class:`OperatorToken`."""
    assert tokenize_search_query("гидрошпонка AND корпус") == [
        WordToken(text="гидрошпонка"),
        OperatorToken(op="AND"),
        WordToken(text="корпус"),
    ]


def test_operator_lowercase_between_words() -> None:
    """Оператор в нижнем регистре между словами → канонический верхний."""
    assert tokenize_search_query("гидрошпонка or корпус") == [
        WordToken(text="гидрошпонка"),
        OperatorToken(op="OR"),
        WordToken(text="корпус"),
    ]


def test_cyrillic_operator_like_word_is_not_operator() -> None:
    """Кириллическое слово ``И`` — :class:`WordToken`, не оператор.

    Операторы распознаются только в латинском алфавите. Кириллица
    остаётся обычным словом.
    """
    assert tokenize_search_query("И") == [WordToken(text="И")]


def test_word_containing_operator_substring_is_not_operator() -> None:
    """Слово ``ANDROID`` — :class:`WordToken`, не оператор.

    Операторы распознаются только при полном совпадении с
    ``AND`` / ``OR`` / ``NOT``. Слово ``ANDROID`` содержит
    подстроку ``AND``, но само оператором не является.
    """
    assert tokenize_search_query("ANDROID") == [WordToken(text="ANDROID")]


# =====================================================================
# Раздел 5. Фразы в кавычках
# =====================================================================


def test_single_phrase() -> None:
    """Одна фраза в кавычках → :class:`PhraseToken`."""
    assert tokenize_search_query('"корпус крупного дробления"') == [
        PhraseToken(text="корпус крупного дробления"),
    ]


def test_phrase_preserves_internal_spaces() -> None:
    """Внутренние пробелы фразы сохраняются."""
    assert tokenize_search_query('"a   b"') == [PhraseToken(text="a   b")]


def test_phrase_between_words() -> None:
    """Фраза между словами → :class:`WordToken`, :class:`PhraseToken`, :class:`WordToken`."""
    assert tokenize_search_query('гидрошпонка "корпус крупного дробления"') == [
        WordToken(text="гидрошпонка"),
        PhraseToken(text="корпус крупного дробления"),
    ]


def test_phrase_with_operator() -> None:
    """Фраза и оператор → корректная последовательность."""
    assert tokenize_search_query('"a b" AND "c d"') == [
        PhraseToken(text="a b"),
        OperatorToken(op="AND"),
        PhraseToken(text="c d"),
    ]


def test_phrase_with_asterisk_inside() -> None:
    """Символ ``*`` внутри фразы сохраняется.

    Токенизатор не интерпретирует содержимое фразы: символы
    остаются как есть.
    """
    assert tokenize_search_query('"гидро*"') == [PhraseToken(text="гидро*")]


# =====================================================================
# Раздел 6. Скобки
# =====================================================================


def test_opening_paren() -> None:
    """Открывающая скобка → :class:`ParenToken` с ``"("``."""
    assert tokenize_search_query("(") == [ParenToken(char="(")]


def test_closing_paren() -> None:
    """Закрывающая скобка → :class:`ParenToken` с ``")"``."""
    assert tokenize_search_query(")") == [ParenToken(char=")")]


def test_both_parens() -> None:
    """Открывающая и закрывающая скобки → два :class:`ParenToken`."""
    assert tokenize_search_query("()") == [
        ParenToken(char="("),
        ParenToken(char=")"),
    ]


def test_word_in_parens() -> None:
    """Слово в скобках → :class:`ParenToken`, :class:`WordToken`, :class:`ParenToken`."""
    assert tokenize_search_query("(гидрошпонка)") == [
        ParenToken(char="("),
        WordToken(text="гидрошпонка"),
        ParenToken(char=")"),
    ]


def test_word_adjacent_to_paren_no_space() -> None:
    """Слово, приклеенное к скобке без пробела, разбирается корректно.

    Регулярное выражение распознаёт скобку как отдельный токен
    даже без пробелов вокруг.
    """
    assert tokenize_search_query("a(b)") == [
        WordToken(text="a"),
        ParenToken(char="("),
        WordToken(text="b"),
        ParenToken(char=")"),
    ]


# =====================================================================
# Раздел 7. Смешанные запросы
# =====================================================================


def test_complex_query_with_all_token_types() -> None:
    """Комплексный запрос — все четыре типа токенов.

    Запрос ``(гидрошпонка OR "корпус крупного дробления") NOT арматура``
    содержит:

    - 2 :class:`ParenToken`;
    - 2 :class:`WordToken`;
    - 1 :class:`PhraseToken`;
    - 2 :class:`OperatorToken`.

    Порядок сохраняется в точности.
    """
    query = '(гидрошпонка OR "корпус крупного дробления") NOT арматура'
    assert tokenize_search_query(query) == [
        ParenToken(char="("),
        WordToken(text="гидрошпонка"),
        OperatorToken(op="OR"),
        PhraseToken(text="корпус крупного дробления"),
        ParenToken(char=")"),
        OperatorToken(op="NOT"),
        WordToken(text="арматура"),
    ]


def test_complex_query_with_latin_codes() -> None:
    """Комплексный запрос с техническими кодами и операторами."""
    query = "(EC-423-1 OR ec-423-1) NOT KM"
    assert tokenize_search_query(query) == [
        ParenToken(char="("),
        WordToken(text="EC-423-1"),
        OperatorToken(op="OR"),
        WordToken(text="ec-423-1"),
        ParenToken(char=")"),
        OperatorToken(op="NOT"),
        WordToken(text="KM"),
    ]


def test_complex_query_nested_parens() -> None:
    """Вложенные скобки разбираются послойно."""
    query = "((a OR b) AND c)"
    assert tokenize_search_query(query) == [
        ParenToken(char="("),
        ParenToken(char="("),
        WordToken(text="a"),
        OperatorToken(op="OR"),
        WordToken(text="b"),
        ParenToken(char=")"),
        OperatorToken(op="AND"),
        WordToken(text="c"),
        ParenToken(char=")"),
    ]


# =====================================================================
# Раздел 8. Сохранение оригинального текста
# =====================================================================


def test_word_preserves_case() -> None:
    """Регистр слова не изменяется токенизатором.

    Токенизатор не нормализует текст: ``ГидроШпонка`` остаётся
    ``ГидроШпонка``. Нормализация — ответственность сборщика
    MATCH-выражения.
    """
    assert tokenize_search_query("ГидроШпонка") == [WordToken(text="ГидроШпонка")]


def test_phrase_preserves_case() -> None:
    """Регистр фразы не изменяется токенизатором."""
    assert tokenize_search_query('"Крупный Корпус"') == [PhraseToken(text="Крупный Корпус")]


def test_word_preserves_cyrillic_characters() -> None:
    """Кириллические символы не заменяются латиницей.

    Токенизатор не применяет ``normalize_text``: символы остаются
    как есть.
    """
    assert tokenize_search_query("ес-423-1") == [WordToken(text="ес-423-1")]


# =====================================================================
# Раздел 9. Порядок токенов
# =====================================================================


def test_token_order_matches_source() -> None:
    """Порядок токенов соответствует порядку в исходной строке.

    Проверяется на запросе, в котором порядок мог бы быть
    перепутан при неправильном обходе (например, если бы
    ``finditer`` обрабатывал группы иначе).
    """
    query = "a AND b OR c NOT d"
    expected = [
        WordToken(text="a"),
        OperatorToken(op="AND"),
        WordToken(text="b"),
        OperatorToken(op="OR"),
        WordToken(text="c"),
        OperatorToken(op="NOT"),
        WordToken(text="d"),
    ]
    assert tokenize_search_query(query) == expected


def test_phrase_and_word_order_preserved() -> None:
    """Смешанные фразы и слова сохраняют порядок."""
    query = 'первое "вторая третья" четвёртое'
    assert tokenize_search_query(query) == [
        WordToken(text="первое"),
        PhraseToken(text="вторая третья"),
        WordToken(text="четвёртое"),
    ]


# =====================================================================
# Раздел 10. Возвращаемый тип
# =====================================================================


def test_returned_tokens_are_frozen_dataclasses() -> None:
    """Токены — иммутабельные ``frozen=True`` dataclasses.

    Проверяет, что попытка изменить поле токена выбрасывает
    ``FrozenInstanceError``. Это гарантирует безопасность передачи
    токенов между слоями без риска их модификации.

    Используется явное ``isinstance``-сужение: тип ``QueryToken``
    — discriminated union, и mypy не позволяет обращаться к
    ``.text`` без предварительного сужения до ``WordToken``.
    """
    import dataclasses

    tokens = tokenize_search_query("гидрошпонка")
    assert len(tokens) == 1

    # Сужение типа: гарантируем mypy, что элемент — WordToken
    # (фактически токенизатор вернул именно его для одного слова).
    token = tokens[0]
    assert isinstance(token, WordToken)

    with pytest.raises(dataclasses.FrozenInstanceError):
        # Проверяем иммутабельность через попытку присвоения.
        # mypy ругается на присваивание frozen-полю — это ожидаемо.
        token.text = "изменено"  # type: ignore[misc]


def test_tokens_are_hashable() -> None:
    """Токены хешируемы (следствие ``frozen=True``).

    Позволяет использовать токены в множествах и как ключи
    словарей — полезно при кэшировании и группировке.
    """
    tokens = tokenize_search_query("a AND b")
    token_set: set[QueryToken] = set(tokens)
    assert len(token_set) == len(tokens)


# =====================================================================
# Раздел 11. Краевые случаи регулярного выражения
# =====================================================================


def test_empty_phrase() -> None:
    """Пустая фраза ``""`` → :class:`PhraseToken` с пустым текстом.

    Регулярное выражение ``"(?P<phrase>[^"]*)"`` допускает
    пустую группу. Токенизатор не выполняет дополнительной
    валидации — пустая фраза становится токеном с ``text=""``.
    Синтаксическую корректность проверяет FTS5 на этапе ``MATCH``.
    """
    assert tokenize_search_query('""') == [PhraseToken(text="")]


def test_two_empty_phrases() -> None:
    """Две пустые фразы подряд → два :class:`PhraseToken`."""
    assert tokenize_search_query('"" ""') == [
        PhraseToken(text=""),
        PhraseToken(text=""),
    ]


def test_word_with_all_special_characters() -> None:
    """Слово из спецсимволов → одно :class:`WordToken`.

    ``word``-группа регулярного выражения захватывает любую
    последовательность непробельных символов, кроме ``(`` и ``)``.
    """
    assert tokenize_search_query("!@#$%^&*") == [WordToken(text="!@#$%^&*")]


def test_query_with_leading_and_trailing_spaces() -> None:
    """Ведущие и хвостовые пробелы не влияют на разбор."""
    assert tokenize_search_query("  гидрошпонка  ") == [WordToken(text="гидрошпонка")]
