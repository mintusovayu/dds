"""
Построитель SQL-условий для фильтрации по метаданным документов.

Модуль предоставляет класс :class:`MetadataFilterQueryBuilder`, который
преобразует поля фильтрации из :class:`~dds_core.domain.models.SearchFilters`
в готовые фрагменты SQL-условий и соответствующие параметры.

Фильтрация выполняется по колонкам таблицы ``documents``:
``object_code``, ``discipline_code``, ``document_type_code``,
а также по признаку ``unmatched_flag`` для режима отбора только
несоответствующих документов.

Компонент изолирует логику построения WHERE-условий от поискового
бэкенда (:class:`~dds_core.infrastructure.fts5_search_backend.FTS5SearchBackend`),
обеспечивая соблюдение принципа единственной ответственности (SRP)
и упрощая тестирование.

Принципы:
- Не зависит от конкретной базы данных или поискового движка.
- Не выполняет запросы, только формирует их фрагменты.
- Полностью типизирован.
"""

from __future__ import annotations

from ..domain.models import SearchFilters


class MetadataFilterQueryBuilder:
    """Строит SQL-фрагменты для фильтрации по метаданным документов.

    Используется в поисковом бэкенде при формировании запроса
    полнотекстового поиска с дополнительными фильтрами.

    Логика работы:

    - Если ``unmatched_only`` истинно, добавляется только условие
      ``d.unmatched_flag = 1``; все прочие фильтры игнорируются.
      Это соответствует режиму контроля полноты справочников.
    - В противном случае для каждого из заданных кодов
      (``object_code``, ``discipline_code``, ``document_type_code``)
      добавляется условие сравнения с параметром.
    - Условия объединяются через ``AND``.

    Example:
        builder = MetadataFilterQueryBuilder()
        filters = SearchFilters(
            object_code="7350",
            discipline_code=None,
            document_type_code="DTL",
            unmatched_only=False,
        )
        conditions, params = builder.build(filters)
        # conditions -> ["d.object_code = ?", "d.document_type_code = ?"]
        # params     -> ("7350", "DTL")
    """

    def build(self, filters: SearchFilters) -> tuple[list[str], tuple]:
        """Возвращает список SQL-условий и кортеж параметров.

        Args:
            filters: Объект фильтров поиска, содержащий поля
                ``object_code``, ``discipline_code``,
                ``document_type_code``, ``unmatched_only``.

        Returns:
            Кортеж из двух элементов:
            - список строк SQL-условий (без ``WHERE`` и ``AND``);
            - кортеж параметров, соответствующих плейсхолдерам ``?``.
        """
        conditions: list[str] = []
        params: list[object] = []

        # Режим «только несоответствующие» – приоритетный.
        if filters.unmatched_only:
            conditions.append("d.unmatched_flag = 1")
            return conditions, tuple(params)

        # Обычные фильтры по кодам.
        if filters.object_code:
            conditions.append("d.object_code = ?")
            params.append(filters.object_code)

        if filters.discipline_code:
            conditions.append("d.discipline_code = ?")
            params.append(filters.discipline_code)

        if filters.document_type_code:
            conditions.append("d.document_type_code = ?")
            params.append(filters.document_type_code)

        return conditions, tuple(params)
