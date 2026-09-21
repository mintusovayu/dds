"""
Матричные тесты трансформации координат в ``HighlightsService``.

Назначение
----------
Полное покрытие ветвлений метода ``HighlightsService.search_highlights``
по двум осям:

1. **``apply_transform``** ∈ {``None``, ``True``, ``False``} — режим
   управления трансформацией координат.
2. **``coord_diagnostics_enabled``** ∈ {``True``, ``False``} — флаг
   авто-диагностики, задаваемый в конструкторе.

Дополнительно проверяется взаимодействие с ``diagnose_word_index``:
когда он вызывается, какие пороги получает и как его результат
(``(flip, confidence)``) влияет на возвращаемое значение метода.

Проверяемая логика
------------------

+-------------------+-------------+----------------------------+
| ``apply_transform`` | diagnostics | Результат                  |
+===================+=============+============================+
| ``None``          | ``True``    | Авто-диагностика; ``flip`` |
|                   |             | и ``confidence`` берутся из |
|                   |             | ``diagnose_word_index``.   |
+-------------------+-------------+----------------------------+
| ``None``          | ``False``   | ``(False, "none")`` без    |
|                   |             | вызова диагностики.        |
+-------------------+-------------+----------------------------+
| ``True``          | любое       | ``flip = True``,           |
|                   |             | ``confidence = "manual"``; |
|                   |             | диагностика не вызывается. |
+-------------------+-------------+----------------------------+
| ``False``         | любое       | ``flip = False``,          |
|                   |             | ``confidence = "manual"``; |
|                   |             | диагностика не вызывается. |
+-------------------+-------------+----------------------------+

Отдельный случай: пустая страница (``page_width <= 0`` или
``page_height <= 0``) → ``([], False, "none")`` — ранний выход
до любых проверок ``apply_transform``.

Что ещё проверяется
-------------------
- **Трансформация координат bbox.** При ``flip=True`` координаты
  преобразуются через ``transform_bbox`` (bottom-left → top-left).
  При ``flip=False`` — остаются без изменений.
- **Трансформация объединённого bbox фразы.** Для многословных
  терминов сначала объединяются bbox слов в один охватывающий,
  затем (при ``flip=True``) трансформируется объединённый bbox —
  а не отдельные bbox'ы.
- **Пороги диагностики.** ``diagnose_word_index`` получает
  threshold-параметры из конструктора ``HighlightsService``,
  а не из глобального конфига (инверсия зависимостей).

Стратегия тестирования
----------------------
- **Синтетические ``WordIndex`` без реального PDF.** Тесты
  конструируют ``WordEntry`` напрямую и собирают ``WordIndex``
  вручную. Это изолирует ``HighlightsService`` от PyMuPDF и
  обеспечивает полный контроль над координатами.
- **Мок ``diagnose_word_index``.** Диагностика имеет собственные
  тесты; здесь проверяется **взаимодействие** сервиса с ней.
  Мок позволяет точно задать ``(flip, confidence)`` и зафиксировать
  факт вызова.
- **Точность float через ``pytest.approx``.** Нормализованные
  координаты — результат деления, поэтому сравнение с эталоном
  идёт с допуском.
- **Хелперы конструируют ``WordEntry`` со всеми полями модели.**
  После Фазы 3 (ADR-003) в ``WordEntry`` добавлено поле
  ``block_no``. Хелпер ``_make_entry`` принимает его как
  keyword-only параметр с дефолтом ``0``; ключи ``by_line``
  формируются согласованно (``(block_no, line_no)``).

Границы
-------
- **Логика диагностики** (признаки OOB и Dominance, пороги)
  тестируется в отдельном файле (``test_coordinate_diagnostics.py``,
  если он есть). Этот файл проверяет только интеграцию.
- **Полный путь через ``process_runner``** (рендер + подсветка в
  subprocess) — вне области smoke/unit-тестов; см.
  ``tests/smoke/test_smoke_playwright.py``.
- **Производительность** — вне области (см. ``bench_fast.py``).

Запуск
------
::

    pytest tests/test_highlights_transform_matrix.py -v

Принципы:
    - модуль не выполняет логирования;
    - не читает и не пишет файлы (все данные — in-memory);
    - каждый тест изолирован, не зависит от порядка выполнения;
    - моки нацелены на модуль-потребитель
      (``dds_core.application.highlights_service``), а не на
      модуль-источник.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest
from dds_core.application.highlights_service import HighlightsService
from dds_core.domain.models import PageHighlight, WordEntry, WordIndex

# =====================================================================
# Константы
# =====================================================================

_PAGE_WIDTH = 100.0
"""Ширина страницы для синтетических тестов."""

_PAGE_HEIGHT = 200.0
"""Высота страницы для синтетических тестов."""

_DIAGNOSTICS_ENABLED_DEFAULT = True
"""Значение флага авто-диагностики по умолчанию для тестов."""

_DEFAULT_OOB_THRESHOLD = 0.05
"""Порог OOB, передаваемый в конструктор по умолчанию."""

_DEFAULT_DOMINANCE_THRESHOLD = 0.7
"""Порог Dominance, передаваемый в конструктор по умолчанию."""

_DEFAULT_ASPECT_T = 2.0
"""Порог aspect ratio, передаваемый в конструктор по умолчанию."""


# =====================================================================
# Helpers: построение синтетического WordIndex
# =====================================================================


def _make_entry(
    *,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    text: str,
    block_no: int = 0,
    line_no: int = 0,
    word_no: int = 0,
) -> WordEntry:
    """Создаёт ``WordEntry`` с заданными координатами и позицией.

    Нормализованная форма совпадает с оригиналом: используемые в
    тестах слова — латиница в нижнем регистре, ``normalize_text``
    оставляет их без изменений.

    Поле ``block_no`` добавлено в ``WordEntry`` в Фазе 3 (ADR-003).
    Значение по умолчанию ``0`` подходит для всех синтетических
    сценариев: тесты используют одну строку в одном блоке, и
    ключи ``by_line`` — ``(block_no, line_no)`` — формируются как
    ``(0, <line_no>)``.

    Args:
        x0: Левая граница bbox в PDF-points.
        y0: Верхняя граница bbox.
        x1: Правая граница bbox.
        y1: Нижняя граница bbox.
        text: Оригинальный текст слова.
        block_no: Номер блока. По умолчанию ``0`` — все
            синтетические тесты используют один блок.
        line_no: Номер строки.
        word_no: Позиция слова в строке.

    Returns:
        ``WordEntry`` с заданными полями.
    """
    return WordEntry(
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        original=text,
        normalized=text,
        block_no=block_no,
        line_no=line_no,
        word_no=word_no,
    )


def _make_single_word_index(word: str) -> WordIndex:
    """Создаёт ``WordIndex`` с одним словом на странице.

    Слово ``word`` расположено в bbox ``(10, 20, 30, 25)`` в
    PDF-points — координаты выбраны так, чтобы трансформация
    (bottom-left → top-left) давала отличный от исходного
    результат, легко проверяемый.

    Args:
        word: Текст слова (латиница, нижний регистр).

    Returns:
        :class:`WordIndex` с одним словом.
    """
    entry = _make_entry(x0=10.0, y0=20.0, x1=30.0, y1=25.0, text=word)
    return WordIndex(
        by_normalized={word: [entry]},
        by_line={(0, 0): [entry]},
        page_width=_PAGE_WIDTH,
        page_height=_PAGE_HEIGHT,
        rotation=0,
    )


def _make_two_word_phrase_index(word1: str, word2: str) -> WordIndex:
    """Создаёт ``WordIndex`` с фразой из двух слов в одной строке.

    Слова расположены горизонтально друг за другом:
    ``word1`` в ``(10, 20, 30, 25)``, ``word2`` в ``(35, 20, 55, 25)``.
    Объединённый bbox фразы — ``(10, 20, 55, 25)``; при трансформации
    должен получиться ``(20, 145, 25, 190)``.

    Args:
        word1: Первое слово фразы.
        word2: Второе слово фразы.

    Returns:
        :class:`WordIndex` с двумя словами в одной строке.
    """
    entry1 = _make_entry(
        x0=10.0,
        y0=20.0,
        x1=30.0,
        y1=25.0,
        text=word1,
        line_no=0,
        word_no=0,
    )
    entry2 = _make_entry(
        x0=35.0,
        y0=20.0,
        x1=55.0,
        y1=25.0,
        text=word2,
        line_no=0,
        word_no=1,
    )
    return WordIndex(
        by_normalized={word1: [entry1], word2: [entry2]},
        by_line={(0, 0): [entry1, entry2]},
        page_width=_PAGE_WIDTH,
        page_height=_PAGE_HEIGHT,
        rotation=0,
    )


def _make_empty_page_index(word: str) -> WordIndex:
    """Создаёт ``WordIndex`` с нулевыми размерами страницы.

    Слово присутствует в ``by_normalized``, но ``page_width``
    установлен в ``0.0`` — это активирует ранний выход в
    ``search_highlights``.

    Args:
        word: Текст слова.

    Returns:
        :class:`WordIndex` с ``page_width=0.0``.
    """
    entry = _make_entry(x0=10.0, y0=20.0, x1=30.0, y1=25.0, text=word)
    return WordIndex(
        by_normalized={word: [entry]},
        by_line={(0, 0): [entry]},
        page_width=0.0,
        page_height=_PAGE_HEIGHT,
        rotation=0,
    )


# =====================================================================
# Fixture: spy-мок диагностики
# =====================================================================


class _DiagnoseSpy:
    """Spy-обёртка вокруг ``diagnose_word_index``.

    Записывает аргументы вызовов и возвращает заданный результат.
    Позволяет одновременно проверить:

    - факт и количество вызовов;
    - переданные значения threshold-параметров;
    - возвращаемое значение (через мок).

    Attributes:
        calls: Список словарей с аргументами каждого вызова.
        result: Кортеж ``(flip, confidence)`` для возврата.
    """

    def __init__(self, result: tuple[bool, str]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result

    def __call__(
        self,
        index: WordIndex,
        *,
        text_oob_threshold: float,
        text_dominance_threshold: float,
        aspect_t: float,
    ) -> tuple[bool, str]:
        """Фиксирует вызов и возвращает заданный результат."""
        self.calls.append(
            {
                "index_page_width": index.page_width,
                "index_page_height": index.page_height,
                "text_oob_threshold": text_oob_threshold,
                "text_dominance_threshold": text_dominance_threshold,
                "aspect_t": aspect_t,
            }
        )
        return self._result


@pytest.fixture
def install_diagnose_spy(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[tuple[bool, str]], _DiagnoseSpy]:
    """Устанавливает spy на ``diagnose_word_index``.

    Мок нацелен на модуль-потребитель
    (``dds_core.application.highlights_service``), где имя
    ``diagnose_word_index`` импортировано через ``from ... import``.
    Патч на модуль-источник
    (``dds_core.application.coordinate_diagnostics``) не сработал бы.

    Использование::

        spy = install_diagnose_spy((True, "high"))
        # ... вызов service.search_highlights(...)
        assert len(spy.calls) == 1

    Args:
        monkeypatch: Встроенная фикстура pytest.

    Returns:
        Callable, принимающий ``(flip, confidence)`` и возвращающий
        установленный ``_DiagnoseSpy``.
    """

    def _install(result: tuple[bool, str]) -> _DiagnoseSpy:
        spy = _DiagnoseSpy(result=result)
        monkeypatch.setattr(
            "dds_core.application.highlights_service.diagnose_word_index",
            spy,
        )
        return spy

    return _install


# =====================================================================
# Матрица режимов apply_transform × diagnostics_enabled × diag_result
# =====================================================================


@pytest.mark.parametrize(
    (
        "apply_transform",
        "diagnostics_enabled",
        "diag_result",
        "expected_flip",
        "expected_confidence",
        "expect_diagnose_called",
    ),
    [
        # apply_transform=True — forced transform; диагностика НЕ вызывается.
        (True, True, None, True, "manual", False),
        (True, False, None, True, "manual", False),
        # apply_transform=False — forced no-transform; диагностика НЕ вызывается.
        (False, True, None, False, "manual", False),
        (False, False, None, False, "manual", False),
        # apply_transform=None, diagnostics=True — авто-диагностика.
        (None, True, (True, "high"), True, "high", True),
        (None, True, (True, "medium"), True, "medium", True),
        (None, True, (False, "none"), False, "none", True),
        # apply_transform=None, diagnostics=False — fallback (False, "none").
        (None, False, None, False, "none", False),
    ],
    ids=[
        "forced-true-diag-on",
        "forced-true-diag-off",
        "forced-false-diag-on",
        "forced-false-diag-off",
        "auto-diag-high",
        "auto-diag-medium",
        "auto-diag-none",
        "auto-diag-disabled",
    ],
)
def test_transform_matrix(
    install_diagnose_spy: Callable[[tuple[bool, str]], _DiagnoseSpy],
    apply_transform: bool | None,
    diagnostics_enabled: bool,
    diag_result: tuple[bool, str] | None,
    expected_flip: bool,
    expected_confidence: str,
    expect_diagnose_called: bool,
) -> None:
    """Матрица: ``apply_transform`` × ``diagnostics_enabled`` × результат.

    Проверяет, что:

    - возвращаемый ``flip`` соответствует ожидаемому;
    - возвращаемый ``confidence`` соответствует ожидаемому;
    - ``diagnose_word_index`` вызывается тогда и только тогда, когда
      ``apply_transform is None and diagnostics_enabled``.

    Используется spy для отслеживания вызовов; результат
    диагностики задаётся через ``diag_result``.

    Args:
        install_diagnose_spy: Фикстура установки spy.
        apply_transform: Значение параметра ``apply_transform``.
        diagnostics_enabled: Флаг в конструкторе ``HighlightsService``.
        diag_result: Ожидаемый результат ``diagnose_word_index``.
            ``None`` — не должен вызываться.
        expected_flip: Ожидаемый ``flip`` в результате.
        expected_confidence: Ожидаемый ``confidence`` в результате.
        expect_diagnose_called: Флаг «ожидается вызов диагностики».
    """
    # Установка spy с ожидаемым результатом (если он предусмотрен).
    spy = install_diagnose_spy(diag_result if diag_result is not None else (False, "none"))

    service = HighlightsService(
        coord_diagnostics_enabled=diagnostics_enabled,
        coord_text_oob_threshold=_DEFAULT_OOB_THRESHOLD,
        coord_text_dominance_threshold=_DEFAULT_DOMINANCE_THRESHOLD,
        coord_aspect_t=_DEFAULT_ASPECT_T,
    )

    index = _make_single_word_index("alpha")

    _, flip, confidence = service.search_highlights(
        index,
        ["alpha"],
        apply_transform=apply_transform,
    )

    assert flip is expected_flip, f"flip={flip!r}, ожидался {expected_flip!r}"
    assert (
        confidence == expected_confidence
    ), f"confidence={confidence!r}, ожидался {expected_confidence!r}"
    if expect_diagnose_called:
        assert (
            len(spy.calls) == 1
        ), f"diagnose_word_index должен быть вызван 1 раз, фактически {len(spy.calls)}"
    else:
        assert (
            len(spy.calls) == 0
        ), f"diagnose_word_index не должен вызываться, фактически {len(spy.calls)} раз(а)"


# =====================================================================
# Пустая страница (ранний выход)
# =====================================================================


@pytest.mark.parametrize(
    "apply_transform",
    [None, True, False],
    ids=["auto", "forced-true", "forced-false"],
)
def test_empty_page_returns_empty_result_regardless_of_apply_transform(
    install_diagnose_spy: Callable[[tuple[bool, str]], _DiagnoseSpy],
    apply_transform: bool | None,
) -> None:
    """Пустая страница → ``([], False, "none")`` для всех режимов.

    Ранний выход при ``page_width <= 0`` происходит **до** проверки
    ``apply_transform``: даже ``apply_transform=True`` не приводит
    к ``flip=True``. Это осознанное поведение (защита от деления
    на ноль).

    ``diagnose_word_index`` не вызывается — диагностика на пустой
    странице бессмысленна.

    Args:
        install_diagnose_spy: Фикстура установки spy.
        apply_transform: Значение параметра ``apply_transform``.
    """
    spy = install_diagnose_spy((True, "high"))

    service = HighlightsService(coord_diagnostics_enabled=True)
    index = _make_empty_page_index("alpha")

    highlights, flip, confidence = service.search_highlights(
        index,
        ["alpha"],
        apply_transform=apply_transform,
    )

    assert highlights == [], f"Ожидался пустой список highlights, получено {highlights!r}"
    assert flip is False
    assert confidence == "none"
    assert len(spy.calls) == 0, "diagnose_word_index не должен вызываться на пустой странице"


# =====================================================================
# Трансформация координат bbox
# =====================================================================


def test_bbox_transformed_when_flip_true() -> None:
    """При ``flip=True`` координаты bbox трансформируются.

    Исходный bbox слова ``alpha`` — ``(10, 20, 30, 25)`` на странице
    ``100×200``. После ``transform_bbox`` → ``(20, 170, 25, 190)``.
    После нормализации:

    - ``x = 20 / 100 = 0.20``;
    - ``y = 170 / 200 = 0.85``;
    - ``w = (25 - 20) / 100 = 0.05``;
    - ``h = (190 - 170) / 200 = 0.10``.
    """
    service = HighlightsService(coord_diagnostics_enabled=False)
    index = _make_single_word_index("alpha")

    highlights, flip, _ = service.search_highlights(
        index,
        ["alpha"],
        apply_transform=True,
    )

    assert flip is True
    assert len(highlights) == 1
    h: PageHighlight = highlights[0]
    assert h.x == pytest.approx(0.20, abs=1e-9)
    assert h.y == pytest.approx(0.85, abs=1e-9)
    assert h.w == pytest.approx(0.05, abs=1e-9)
    assert h.h == pytest.approx(0.10, abs=1e-9)
    assert h.term == "alpha"


def test_bbox_unchanged_when_flip_false() -> None:
    """При ``flip=False`` координаты bbox не трансформируются.

    Исходный bbox слова ``alpha`` — ``(10, 20, 30, 25)`` на странице
    ``100×200``. После нормализации:

    - ``x = 10 / 100 = 0.10``;
    - ``y = 20 / 200 = 0.10``;
    - ``w = (30 - 10) / 100 = 0.20``;
    - ``h = (25 - 20) / 200 = 0.025``.
    """
    service = HighlightsService(coord_diagnostics_enabled=False)
    index = _make_single_word_index("alpha")

    highlights, flip, _ = service.search_highlights(
        index,
        ["alpha"],
        apply_transform=False,
    )

    assert flip is False
    assert len(highlights) == 1
    h: PageHighlight = highlights[0]
    assert h.x == pytest.approx(0.10, abs=1e-9)
    assert h.y == pytest.approx(0.10, abs=1e-9)
    assert h.w == pytest.approx(0.20, abs=1e-9)
    assert h.h == pytest.approx(0.025, abs=1e-9)
    assert h.term == "alpha"


def test_phrase_bbox_transformed_as_whole() -> None:
    """Трансформация применяется к объединённому bbox фразы.

    Фраза ``alpha beta`` на странице ``100×200``:

    - ``alpha`` в bbox ``(10, 20, 30, 25)``;
    - ``beta`` в bbox ``(35, 20, 55, 25)``;
    - объединённый bbox — ``(10, 20, 55, 25)``;
    - после ``transform_bbox`` с ``page_height=200`` →
      ``(20, 145, 25, 190)``;
    - после нормализации:

      - ``x = 20 / 100 = 0.20``;
      - ``y = 145 / 200 = 0.725``;
      - ``w = 5 / 100 = 0.05``;
      - ``h = 45 / 200 = 0.225``.

    Важно: если бы трансформация применялась к каждому bbox отдельно,
    результат был бы другим (fragmentized). Тест проверяет именно
    применение к объединённому bbox.
    """
    service = HighlightsService(coord_diagnostics_enabled=False)
    index = _make_two_word_phrase_index("alpha", "beta")

    highlights, flip, _ = service.search_highlights(
        index,
        ["alpha beta"],
        apply_transform=True,
    )

    assert flip is True
    assert len(highlights) == 1
    h: PageHighlight = highlights[0]
    assert h.x == pytest.approx(0.20, abs=1e-9)
    assert h.y == pytest.approx(0.725, abs=1e-9)
    assert h.w == pytest.approx(0.05, abs=1e-9)
    assert h.h == pytest.approx(0.225, abs=1e-9)
    assert h.term == "alpha beta"


# =====================================================================
# Передача порогов диагностики в diagnose_word_index
# =====================================================================


def test_diagnose_receives_configured_thresholds(
    install_diagnose_spy: Callable[[tuple[bool, str]], _DiagnoseSpy],
) -> None:
    """Threshold-параметры передаются в диагностику из конструктора.

    Проверяет инверсию зависимостей: ``HighlightsService`` не читает
    глобальный ``config`` внутри ``search_highlights``, а использует
    значения, переданные в конструктор.

    Конкретные значения (``0.11``, ``0.22``, ``3.33``) выбраны
    отличными от дефолтных, чтобы гарантировать передачу именно
    сконфигурированных значений, а не случайное совпадение.
    """
    spy = install_diagnose_spy((False, "none"))

    service = HighlightsService(
        coord_diagnostics_enabled=True,
        coord_text_oob_threshold=0.11,
        coord_text_dominance_threshold=0.22,
        coord_aspect_t=3.33,
    )
    index = _make_single_word_index("alpha")

    service.search_highlights(index, ["alpha"], apply_transform=None)

    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["text_oob_threshold"] == pytest.approx(0.11)
    assert call["text_dominance_threshold"] == pytest.approx(0.22)
    assert call["aspect_t"] == pytest.approx(3.33)
    assert call["index_page_width"] == pytest.approx(_PAGE_WIDTH)
    assert call["index_page_height"] == pytest.approx(_PAGE_HEIGHT)


def test_diagnose_not_called_when_manual_transform(
    install_diagnose_spy: Callable[[tuple[bool, str]], _DiagnoseSpy],
) -> None:
    """Ручное управление ``apply_transform`` отключает диагностику.

    Дополняет матричный тест: даже при ``diagnostics_enabled=True``
    явный ``apply_transform`` (``True``/``False``) полностью
    подавляет вызов ``diagnose_word_index``.
    """
    spy = install_diagnose_spy((True, "high"))

    service = HighlightsService(coord_diagnostics_enabled=True)
    index = _make_single_word_index("alpha")

    # Явный apply_transform=True.
    service.search_highlights(index, ["alpha"], apply_transform=True)
    # Явный apply_transform=False.
    service.search_highlights(index, ["alpha"], apply_transform=False)

    assert (
        len(spy.calls) == 0
    ), "diagnose_word_index не должен вызываться при ручном apply_transform"
