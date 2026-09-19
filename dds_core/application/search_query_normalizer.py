"""
Модуль нормализации поискового запроса для FTS5 с учётом синтаксиса.

Позволяет использовать операторы FTS5 (AND, OR, NOT, скобки, кавычки)
вместе с нечувствительностью к раскладке клавиатуры. Для каждого
слова или фразы добавляется префикс ``normalized_text:``, чтобы поиск
выполнялся только по колонке с нормализованным текстом.

Модуль не содержит бизнес-логики, только преобразование строки.
Является чистой функцией без побочных эффектов.

Принципы:
- Архитектурная чистота: находится в application layer, не зависит
  от инфраструктурных компонентов.
- Инверсия зависимостей: импортирует только функцию нормализации
  текста из соседнего модуля.
- Полная документированность.
"""

from __future__ import annotations

import re

from .text_normalizer import normalize_text

# Регулярное выражение для выделения токенов FTS5-запроса.
# Поддерживаются:
#   - фразы в двойных кавычках: "..." (включая пробелы)
#   - отдельные слова (не пробелы и не скобки)
#   - скобки: ( )
# Пробелы игнорируются.
_TOKEN_PATTERN = re.compile(
    r'"(?P<phrase>[^"]*)"'  # фраза в кавычках
    r"|(?P<word>[^\s()]+)"  # обычное слово (может содержать спецсимволы FTS5)
    r"|(?P<paren>[()])"  # скобка
)

# Операторы FTS5, которые не должны получать префикс колонки.
_FTS_OPERATORS = {"AND", "OR", "NOT"}


def normalize_search_query(query: str) -> str:
    """Преобразует поисковый запрос, добавляя префикс колонки к токенам.

    Функция разбирает строку ``query``, содержащую синтаксис FTS5
    (операторы AND/OR/NOT, скобки, фразы в кавычках), и возвращает
    новую строку, в которой каждый значимый токен (слово или фраза)
    нормализован с помощью :func:`~dds_core.application.text_normalizer.normalize_text`
    и снабжён префиксом ``normalized_text:``. Операторы и скобки
    сохраняются без изменений, при этом операторы приводятся
    к верхнему регистру.

    Args:
        query: Исходный поисковый запрос.

    Returns:
        Строка, пригодная для использования в MATCH-выражении FTS5.

    Raises:
        Ничего не выбрасывает; при пустом запросе возвращает пустую строку.

    Example:
        >>> normalize_search_query('гидрошпонка AND "корпус крупного дробления"')
        'normalized_text: gidroshponka AND normalized_text: "korpus krupnogo drobleniia"'
        >>> normalize_search_query('(ЕС-423-1 OR EC-423-1) NOT KM')
        'normalized_text: (ec-423-1 OR normalized_text: ec-423-1) NOT normalized_text: km'
    """
    if not query.strip():
        return ""

    result_parts: list[str] = []

    for match in _TOKEN_PATTERN.finditer(query):
        if match.group("phrase") is not None:
            # Фраза в кавычках: нормализуем внутренний текст.
            phrase = match.group("phrase")
            normalized_phrase = normalize_text(phrase)
            result_parts.append(f'normalized_text: "{normalized_phrase}"')
        elif match.group("word") is not None:
            word = match.group("word")
            upper_word = word.upper()
            if upper_word in _FTS_OPERATORS:
                # Оператор FTS5 — оставляем как есть, но приводим к верхнему регистру.
                result_parts.append(upper_word)
            else:
                # Обычное слово — нормализуем и добавляем префикс.
                normalized_word = normalize_text(word)
                result_parts.append(f"normalized_text: {normalized_word}")
        elif match.group("paren") is not None:
            # Скобка — добавляем как есть.
            result_parts.append(match.group("paren"))

    return " ".join(result_parts)
