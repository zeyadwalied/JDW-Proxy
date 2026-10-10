# JustDoWork (api.justwoker.icu) — аудит Anthropic-протокола /v1/messages

Дата: 2026-10-03. Ключ: sk-zHvq... (JDW). Endpoint: https://api.justwoker.icu/v1
Единственная модель: `claude-opus-4-8` (по /v1/models, поддерживает "anthropic" и "openai" форматы).

## Вердикт

Эндпоинт `/v1/messages` — НЕ протоколо-совместимый релей. Это обёртка-агент (data-analysis harness
с тулами `read_tabular` и `system_todo_write`), которая подменяет запрос целиком и гоняет собственный
агентный цикл на апстрим-Claude (апстрим — настоящий Claude с extended thinking: в подписях
thinking-блоков виден internal-codename `claude-quince`, формат подписи — Anthropic). Claude Code /
Cline / ZCode работать с ним не могут.

## Подтверждённые баги (все 6 наблюдений из жалоб подтверждены)

### 1. Streaming: текст теряется полностью
`stream:true` → события `message_start` → (иногда `content_block_*` ТОЛЬКО для thinking) → `message_delta` → `message_stop`.
Текстовые content_block-события отсутствуют всегда, при этом `output_tokens` в `message_delta`
содержит реальные токены текста (586 токенов истории, которую клиент так и не увидел).
- message_start содержит `input_tokens: 0, output_tokens: 0` (по спецификации там реальные числа).
- ping-события отсутствуют.
- Для t2 (короткий ответ без thinking) не пришло НИ ОДНОГО content_block.

### 2. Клиентские tools и tool_choice выбрасываются
- `tools: [get_weather]` + `tool_choice: auto` → модель отвечает текстом: «I don't have access to a
  get_weather tool. The only tools available to me are read_tabular and system_todo_write».
- `tool_choice: {type: "tool", name: "get_weather"}` (принудительный вызов) → тоже текстовый отказ.
  Спецификация требует вернуть tool_use-блок или 400-ошибку.
- Блоки `tool_use`/`tool_result` в ИСТОРИИ сообщений парсятся корректно (200 OK, модель читает результат).

### 3. max_tokens игнорируется
`max_tokens: 1` → 60 output_tokens полного ответа, `stop_reason: "end_turn"` вместо `"max_tokens"`.
Клиенты типа Cline опираются на stop_reason=max_tokens для авто-компрессии контекста — это ломается.

### 4. stop_sequences игнорируются
Стоп-слово целиком присутствует в ответе: `...15
STOPWORD-XYZ`, `stop_reason: "end_turn"`, `stop_sequence: null`.
По спецификации: текст обрезан ДО стоп-слова, `stop_reason: "stop_sequence"`, `stop_sequence: "STOPWORD-XYZ"`.

### 5. ~10.4k токенов фиксированного контекста инжектится
`input_tokens: 10363–10440` даже на «hi». Инжект доминирует над клиентским system prompt:
на пиратский промпт модель ответила «data-analyzin' buccaneer ... pandas cutlass», т.е. живёт в
персоне data-analysis-агента. Плюс релей исполняет собственный агентный цикл: для запроса с
system_todo_write `input_tokens` = 21158 = два апстрим-вызова (cache_read 10402 + cache_creation 10752).

### 6. Non-stream текст и system prompt работают
Да, но с оговорками: в контент могут просачиваться `thinking`-блоки с `signature` (t5), которые
клиент не запрашивал (thinking-config не передавался). Сам text-контент в non-stream приходит.

## Дополнительные находки (не из исходного списка)

7. **OpenAI /v1/chat/completions заблокирован Cloudflare WAF** — HTTP 403, HTML-страница «Sorry,
   you have been blocked», ray `a44c9427ac84c017-WAW`. Блок по типу запроса, не по ключу.
8. **`/v1/messages/count_tokens` → 404** «Invalid URL» — не реализован (Claude Code его использует).
9. **Валидация модели сломана**: неизвестная модель → 403 с пустым телом `application/octet-stream`
   вместо JSON-ошибки 400 invalid_request_error.
10. **max_tokens обязателен по спецификации, но здесь опционален** (200 OK без него).
11. **Ошибки апстрима проксируются сырыми**: invalid role order → 400 с
    `"type":"bad_response_status_code"` и текстом «upstream returned 400: error parsing input
    messages...» — не формат Anthropic, и утекают внутренности апстрима.
12. **Мультимодальный ввод работает**: image block (base64 PNG) принят, модель корректно ответила «Green».
13. **Релей эмитит tool_use-блоки только для СВОИХ тулов** (system_todo_write прошёл в ответе
    non-stream с корректным id `toolu_bdrk_...` и input). В стриминге даже эти события не приходят.
14. **temperature прокидывается** (t11 вернул точный ответ при temperature: 0).

## Что это значит для Cline (и почему он не работает)

Cline = Anthropic-клиент: `/v1/messages`, `stream: true`, свой набор тулов (read_file,
execute_command, ...), system prompt, max_tokens. На JDW он получает:
- пустые ответы в стриме (текст не эмитится) → «no content» у пользователя;
- свои тулы модель не видит → агент не может выполнять действия;
- stop_reason max_tokens не приходит → авто-компрессия истории не срабатывает;
- счётчик контекста завышен на ~10.4k фантомных токенов.

## Гипотеза архитектуры

NewAPI-слой → провайдер-обёртка «аналитический агент» (prompt ~10.4k + tools read_tabular /
system_todo_write) → настоящий Claude (thinking). Конвертер стрима Anthropic→Anthropic пишет в SSE
только thinking-дельты (маппинг reasoning_content → thinking), а text-дельты и tool-события
потребляются агентным циклом и не переэмитятся. Классический баг кастомного релея.

## Рекомендации владельцу сервиса (если цель — совместимость)

1. Прокидывать клиентские `tools`/`tool_choice` в апстрим вместо подмены (агентный харнесс должен
   работать только когда клиент не передал свои тулы).
2. Прокидывать `max_tokens` и `stop_sequences` в апстрим (Anthropic апстрим их нативно поддерживает).
3. Починить стрим-конвертер: эмитить `content_block_start/delta/stop` для text, `input_json_delta`
   для tool_use, реальные usage в message_start, ping-события.
4. Реализовать `/v1/messages/count_tokens`.
5. Снять WAF-блок с `/v1/chat/completions` (или вернуть осмысленную ошибку).
6. Валидация запросов: 400 invalid_request_error на неверную модель / отсутствие max_tokens /
   неверный порядок ролей.
