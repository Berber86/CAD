# Образцы страниц ACRIS (Шаг 0)

Сохранил пользователь из своего браузера 2026-11-23 (Chrome, «сохранить страницу»):

- `ACRIS Search By Parcel Identifier.mhtml` — форма поиска по BBL
  (район / блок / лот), https://a836-acris.nyc.gov/DS/DocumentSearch/BBL
- `ACRIS Search Results By Parcel Identifier2.mhtml` — результаты по
  Manhattan, block 731, lot 53 (6 записей, MORTGAGE/DEED и др.)

Вынутые из них HTML лежат фикстурами в `tests/data/`
(`acris_search_form.html`, `acris_results_manhattan_731_53.html`) — на них
проверяется разбор в `tests/test_acris.py`. Скрипты страниц в MHTML не
попали, поэтому как кнопки DET/IMG внутри вызывают документ (doc_id),
по этим образцам не видно — нужен ещё один образец: страница по кнопке DET
или IMG одного документа.
