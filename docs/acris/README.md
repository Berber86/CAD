# Образцы страниц ACRIS (Шаг 0)

Сохранил пользователь из своего браузера 2026-11-23 (Chrome, «сохранить страницу»):

- `ACRIS Search By Parcel Identifier.mhtml` — форма поиска по BBL
  (район / блок / лот), https://a836-acris.nyc.gov/DS/DocumentSearch/BBL
- `ACRIS Search Results By Parcel Identifier2.mhtml` — результаты по
  Manhattan, block 731, lot 53 (6 записей, MORTGAGE/DEED и др.)
- `New York Web Public Inquiry.mhtml` — обёртка документа по кнопке IMG:
  `DocumentImageView?doc_id=2024101000085001`, внутри читалка Acordex VTU
  (страницы по одной, кнопки Prev/Next, Print и Save-дискета).

Вынутые из них HTML лежат фикстурами в `tests/data/`
(`acris_search_form.html`, `acris_results_manhattan_731_53.html`,
`acris_document_image_view.html`) — на них проверяется разбор в
`tests/test_acris.py`.

## Как устроена цепочка (установлено по образцам)

1. Список: прямой POST на `BBLResult` (`hid_*`, сразу `hid_max_rows=99`)
   -> строки с CRFN — `acris.fetch_results_html` / `parse_result_page`.
2. CRFN -> `document_id`: официальный NYC Open Data (ACRIS Real Property
   Master, `bnx9-e6tj`) — без стены для роботов. Проверено: CRFN
   2024000265906 -> 2024101000085001 (тот самый doc_id из образца IMG).
   `acris.fetch_document_id(s)` / `attach_document_ids`.
3. Документ: `DocumentImageView?doc_id=...` — `acris.document_image_url`.
   Внутри VTU-читалка: гулять по страницам нам НЕ надо.
4. Скачивание (как делает человек): дискета Save в читалке -> в диалоге
   «All» уже выбран -> OK -> браузер качает весь документ PDF в «Загрузки».
   Эти два клика автоматизированы в `acris_viewer.py` (снимок окна + OCR:
   готовность читалки по «DOCUMENT ID», дискета — по калибровке от «73%»,
   OK — по тексту диалога; не нашлось — честный ручной шаг со ссылкой).
   Отсюда сценарии сходятся с Ричмондом в одно русло:
   `deed_intake.adopt_downloads` забирает свежий PDF из «Загрузок» и кладёт
   по схеме имён с паспортом.

## Что осталось

Подключить ACRIS-ветку в проход Шага 0 (`deed_intake.collect_deeds` по
району: Клерк — только Staten Island, остальные четыре — сюда) и в окно
программы; живую проверку кликов сделать на машине пользователя.
