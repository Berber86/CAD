# 4.81.12 — Список Hydra дополнен до полного: добавлено семейство GPT-6 и остальные Vision-модели с рассуждениями

В 4.81.11 список моделей Hydra был ошибочно обрезан до 14 позиций. Каталог
провайдера (`GET https://api.hydraai.ru/v1/models`) перепроверен целиком
(2026-11-23): моделей с входом текст+картинка и настраиваемым
`reasoning_effort` там 29. Все они теперь в списке, семейство GPT-6 — сразу
после модели по умолчанию.

Полный список (цена — рубли за 1 млн **выходных** токенов по каталогу Hydra):

| Модель | Рассуждения: дефолт / уровни | Выход, ₽/1M |
|---|---|---|
| grok-4.7 | none / none, low, medium, high, xhigh | 240 |
| gpt-6.2-sol | low / low, medium, high, xhigh | 400 |
| gpt-6.2-astra | low / low, medium, high, xhigh | 1600 |
| gpt-6.1-sol | low / low, medium, high, xhigh | 400 |
| gpt-6.1-terra | none / none, low, medium, high, xhigh | 200 |
| gpt-6-sol | none / none, low, medium, high, xhigh | 400 |
| gpt-6-luna | none / none, low, medium, high, xhigh | 20 |
| gpt-6-astra | low / low, medium, high, xhigh | 2000 |
| gpt-5.6-sol | none / none..xhigh | 1200 |
| gpt-5.6-terra | none / none..xhigh | 480 |
| gpt-5.6-luna | none / none..xhigh | 48 |
| gpt-5.5 | none / none..xhigh | 1200 |
| gpt-5.4 | none / none..xhigh | 600 |
| gpt-5.4-mini | none / none..xhigh | 180 |
| gpt-5.4-nano | none / none..xhigh | 50 |
| gpt-5.3-codex | none / none, low, medium, high | 560 |
| gpt-5.2 | none / none..xhigh | 520 |
| gpt-5.1 | none / none, low, medium, high | 400 |
| gpt-5-mini | minimal / minimal, low, medium, high | 80 |
| gemini-3.1-pro | low / low, medium, high | 480 |
| gemini-3.8-flash | low / low, medium, high | 150 |
| gemini-3-flash | minimal / minimal..high | 120 |
| gemini-3.5-flash-lite | minimal / minimal..high | 100 |
| gemini-3.1-flash-lite | minimal / minimal..high | 60 |
| claude-sonnet-5 | none / none..xhigh | 470 |
| claude-sonnet-5.5 | none / none..xhigh | 470 |
| minimax-m3.1-flash | low / low, medium, high, xhigh, max | 24 |
| pro/deepseek-v4.1-flash-1017 | none / none, low, high, max | 48 |
| pro/glm-5.3-flash | low / low, high, max | 36 |

Что изменено:

- `llm_parser.py`: список `PROVIDERS_CATALOG.hydra.models` расширен до 29;
  в `HYDRA_MODEL_REASONING` добавлены уровни новых моделей и поле
  `out_rub_per_m` (цена миллиона выходных токенов) для каждой.
- Миграция `load_config` теперь выстраивает список Hydra в порядке каталога
  (новые модели не тонут в хвосте), ручные имена пользователя сохраняет после.
  Установкам с 4.81.11 новые модели подтянутся сами при первом запуске.
- `config.json` репозитория приведён к тому же списку.

Оговорки из самого каталога Hydra:

- У `claude-sonnet-5.5` карточка противоречива: поле `reasoning_effort` есть,
  а в описании сказано «Управлять уровнем рассуждений нельзя!». Модель
  оставлена по формальному полю; если провайдер отклонит параметр, запрос
  повторится без него автоматически.
- У `gpt-5-mini` в каталоге стоит `delete_at: 10.12.2026` — провайдер
  планирует её убрать.
- У `grok-4.7` цена выхода 240 ₽ — базовая; при входе свыше 200k токенов
  каталог показывает 480 ₽.

Проверки: обновлён `tests/test_hydra_reasoning.py` (состав списка, семейство
GPT-6 на месте, цены записаны). Живые платные запросы не запускались.
