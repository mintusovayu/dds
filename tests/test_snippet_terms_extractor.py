"""
Тесты извлечения терминов подсветки из сниппета FTS5.

Модуль покрывает функцию
:func:`dds_core.application.snippet_terms_extractor.extract_terms_from_snippet`:

- пустые входы (пустая строка, сниппет без маркеров, только пробелы);
- один и несколько терминов;
- дедупликация с сохранением порядка первого появления;
- удаление пробелов по краям термина;
- пустые фрагменты между маркерами;
- непарные ``START`` / ``END``;
- вложенные ``START``;
- сохранение пунктуации (точки, дефисы, многоточия);
- комплексный сниппет с текстом и несколькими парами маркеров;
- тип возвращаемого значения (иммутабельный ``tuple``).

Стратегия:
    Функция чистая и синхронная; тесты — параметризованные и
    простые ассерты. Никаких fixtures и mocks: только ``assert``.
    Маркеры берутся из :mod:`dds_core.domain.config`, чтобы
    тесты были устойчивы к возможному изменению их значений
    (регресс-защита при смене формата маркеров в будущих фазах).

Границы покрытия:
    Не проверяется взаимодействие с FTS5 (SQL, ``MATCH``,
    ``snippet()``) — это область тестов
    ``test_fts5_search_backend.py`` (Шаг 7). Здесь только
    контракт функции извлечения терминов на подготовленных
    строках.
"""

from __future__ import annotations

from dds_core.application.snippet_terms_extractor import (
    extract_terms_from_snippet,
)
from dds_core.domain import config as core_config

# ----------------------------------------------------------------------
# Константы
# ----------------------------------------------------------------------

_START = core_config.SNIPPET_HIGHLIGHT_START
"""Начальный маркер подсветки из production-конфигурации.

Значение по умолчанию — ``"[[DDS_HIGHLIGHT_START]]"``.
Тесты не хардкодят литерал, а используют конфигурацию:
при изменении значения в :mod:`config` тесты продолжат
работать без правок.
"""

_END = core_config.SNIPPET_HIGHLIGHT_END
"""Конечный маркер подсветки из production-конфигурации.

Значение по умолчанию — ``"[[DDS_HIGHLIGHT_END]]"``.
"""


# ----------------------------------------------------------------------
# Пустые входы
# ----------------------------------------------------------------------


def test_empty_string_returns_empty_tuple() -> None:
    """Пустая строка возвращает пустой кортеж.

    Не должно быть исключений или ``None`` — только ``()``.
    """
    assert extract_terms_from_snippet("") == ()


def test_snippet_without_markers_returns_empty_tuple() -> None:
    """Сниппет без маркеров подсветки возвращает пустой кортеж.

    Типичный случай — сниппет, где нет совпадений (не должно
    происходить в production, но защищаемся от такого входа).
    """
    snippet = "обычный текст без подсветки совпадений"
    assert extract_terms_from_snippet(snippet) == ()


def test_whitespace_only_snippet_returns_empty_tuple() -> None:
    """Сниппет только из пробельных символов возвращает пустой кортеж."""
    assert extract_terms_from_snippet("   \t\n  ") == ()


# ----------------------------------------------------------------------
# Один термин
# ----------------------------------------------------------------------


def test_single_term_returns_single_tuple() -> None:
    """Один маркер-пара с непустым содержимым → кортеж из одного термина."""
    snippet = "... текст ... " + _START + "корпус" + _END + " ... текст ..."
    assert extract_terms_from_snippet(snippet) == ("корпус",)


def test_single_term_at_string_start() -> None:
    """Термин в самом начале строки (без префикса) корректно извлекается."""
    snippet = _START + "гидрошпонка" + _END + " и продолжение текста"
    assert extract_terms_from_snippet(snippet) == ("гидрошпонка",)


def test_single_term_at_string_end() -> None:
    """Термин в самом конце строки (без суффикса) корректно извлекается."""
    snippet = "текст и " + _START + "дробление" + _END
    assert extract_terms_from_snippet(snippet) == ("дробление",)


# ----------------------------------------------------------------------
# Несколько терминов
# ----------------------------------------------------------------------


def test_multiple_terms_preserve_order() -> None:
    """Несколько терминов возвращаются в порядке появления в сниппете."""
    snippet = (
        _START
        + "первый"
        + _END
        + " ... текст ... "
        + _START
        + "второй"
        + _END
        + " ... текст ... "
        + _START
        + "третий"
        + _END
    )
    assert extract_terms_from_snippet(snippet) == (
        "первый",
        "второй",
        "третий",
    )


def test_adjacent_marker_pairs_no_separator() -> None:
    """Пары маркеров без разделителя между ними корректно обрабатываются."""
    snippet = _START + "a" + _END + _START + "b" + _END
    assert extract_terms_from_snippet(snippet) == ("a", "b")


# ----------------------------------------------------------------------
# Дедупликация
# ----------------------------------------------------------------------


def test_duplicates_are_deduplicated_preserving_first_order() -> None:
    """Повторяющиеся термины удаляются; порядок первого сохранён."""
    snippet = (
        _START
        + "x"
        + _END
        + " ... "
        + _START
        + "y"
        + _END
        + " ... "
        + _START
        + "x"
        + _END
        + " ... "
        + _START
        + "z"
        + _END
        + " ... "
        + _START
        + "y"
        + _END
    )
    assert extract_terms_from_snippet(snippet) == ("x", "y", "z")


def test_all_duplicates_returns_single_item() -> None:
    """Если все термины одинаковые, возвращается один элемент."""
    snippet = _START + "one" + _END + _START + "one" + _END + _START + "one" + _END
    assert extract_terms_from_snippet(snippet) == ("one",)


# ----------------------------------------------------------------------
# Краевые случаи
# ----------------------------------------------------------------------


def test_term_with_surrounding_whitespace_is_stripped() -> None:
    """Пробелы по краям термина удаляются (``.strip()``)."""
    snippet = _START + "   корпус   " + _END
    assert extract_terms_from_snippet(snippet) == ("корпус",)


def test_empty_content_between_markers_is_skipped() -> None:
    """Пустой фрагмент между ``START`` и ``END`` не попадает в результат."""
    snippet = _START + _END + " ... " + _START + "a" + _END
    assert extract_terms_from_snippet(snippet) == ("a",)


def test_whitespace_only_content_between_markers_is_skipped() -> None:
    """Фрагмент из одних пробелов не попадает в результат."""
    snippet = _START + "   \t  " + _END + _START + "b" + _END
    assert extract_terms_from_snippet(snippet) == ("b",)


def test_unpaired_start_marker_is_ignored() -> None:
    """Непарный ``START`` без ``END`` прерывает обход, возвращается ``()``."""
    snippet = "текст " + _START + "незакрытый термин"
    assert extract_terms_from_snippet(snippet) == ()


def test_unpaired_start_after_valid_pair_is_ignored() -> None:
    """Валидная пара забирается, непарный ``START`` в конце игнорируется."""
    snippet = _START + "ok" + _END + " ... " + _START + "незакрытый"
    assert extract_terms_from_snippet(snippet) == ("ok",)


def test_lone_end_marker_returns_empty_tuple() -> None:
    """Одиночный ``END`` без ``START`` не порождает терминов."""
    snippet = "текст " + _END + " ещё текст"
    assert extract_terms_from_snippet(snippet) == ()


def test_nested_start_markers_pick_first_pair() -> None:
    """Вложенные ``START START ... END`` → берётся первая пара.

    FTS5 ``snippet()`` не порождает вложенных маркеров, но
    функция не должна падать на таком входе. Алгоритм
    (``str.find``) забирает первый ``START`` и ближайший ``END``
    как пару; второй ``START`` остаётся непарным и обход
    завершается.
    """
    snippet = _START + _START + "inner" + _END
    # Первый START формирует пару с первым END; содержимое между
    # ними — "[[DDS_HIGHLIGHT_START]]inner". Оно непустое и
    # попадает в результат целиком.
    assert extract_terms_from_snippet(snippet) == (_START + "inner",)


def test_term_with_punctuation_preserved() -> None:
    """Пунктуация (точки, дефисы, многоточия) сохраняется как есть."""
    snippet = _START + "EC-423-1" + _END + " ... " + _START + "ГОСТ 2.105-2019" + _END
    assert extract_terms_from_snippet(snippet) == (
        "EC-423-1",
        "ГОСТ 2.105-2019",
    )


def test_term_with_ellipsis_inside_is_preserved() -> None:
    """Многоточие внутри термина не считается разделителем."""
    snippet = _START + "a...b" + _END
    assert extract_terms_from_snippet(snippet) == ("a...b",)


# ----------------------------------------------------------------------
# Комплексные сценарии
# ----------------------------------------------------------------------


def test_complex_snippet_with_three_terms() -> None:
    """Комплексный сниппет с префиксом, суффиксом и тремя парами.

    Проверяет типичный production-сниппет FTS5 с разделителями
    ``"..."`` между фрагментами.
    """
    snippet = (
        "начало текста ... "
        + _START
        + "гидрошпонка"
        + _END
        + " ... фрагмент ... "
        + _START
        + "дробление"
        + _END
        + " ... фрагмент ... "
        + _START
        + "EC-423-1"
        + _END
        + " ... конец текста"
    )
    assert extract_terms_from_snippet(snippet) == (
        "гидрошпонка",
        "дробление",
        "EC-423-1",
    )


def test_snippet_only_markers_no_content_returns_empty() -> None:
    """Сниппет только из маркеров без содержимого → пустой кортеж."""
    snippet = _START + _END + _START + _END
    assert extract_terms_from_snippet(snippet) == ()


# ----------------------------------------------------------------------
# Тип и иммутабельность результата
# ----------------------------------------------------------------------


def test_returns_tuple_not_list() -> None:
    """Возвращается ``tuple``, не ``list``.

    Важно для использования в ``PageHit.terms: tuple[str, ...]``
    (domain-модель): тип должен совпадать без конвертаций.
    """
    snippet = _START + "x" + _END
    result = extract_terms_from_snippet(snippet)
    assert isinstance(result, tuple)


def test_empty_result_is_tuple() -> None:
    """Пустой результат — именно ``tuple``, не ``list`` и не ``None``."""
    result = extract_terms_from_snippet("no markers here")
    assert result == ()
    assert isinstance(result, tuple)


def test_result_elements_are_strings() -> None:
    """Все элементы результата — строки."""
    snippet = _START + "abc" + _END + _START + "123" + _END
    result = extract_terms_from_snippet(snippet)
    assert all(isinstance(term, str) for term in result)
