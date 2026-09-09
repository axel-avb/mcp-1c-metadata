# Код-ревью: 1C Configuration MCP Server

Дата: 2026-09-08
Объект: `/workspace/1c-mcp-server` (FastMCP 4.0.3, Python 3.11, qdrant-client 1.19.0, httpx 0.28.1)

## Вывод

Дизайн и структура проекта хорошие (инкрементальный индекс по хэшам, терпимый
XML-парсер, корректная модель плоского глобального пространства имён 1С для
CALLS-рёбер, graceful degradation реранкера, чистое разделение конфигурации
JSON + env `ONEC_*`).

**Но в текущем состоянии сервер не работает end-to-end.** Два независимых
критических бага (кросс-тредовый SQLite на каждом вызове инструмента и
`_coerce`-баг, ломающий `load_config()` для любого реального деплоя) блокируют
работу, плюс sample-данные повреждены — даже работоспособный сервер вернул бы
пустые результаты.

Всё перечисленное ниже проверено: C1, C2, C3, H1, H2 (отсутствие), H3, H4 и
поведение `_coerce` воспроизведены выполнением реального кода в venv проекта;
остальное — прямое чтение кода.

---

## CRITICAL

### C1. `GraphStore` используется из разных потоков → падает каждый вызов инструмента

- FastMCP 4.x диспатчит синхронные инструменты в worker-потоки
  (`FunctionTool.run` → `call_sync_fn_in_threadpool`); все 9 тулзов в
  `src/server.py` синхронные.
- `App.__init__` открывает `sqlite3.connect()` в основном потоке
  (`src/server.py:45`), инструменты затем вызывают `app.graph.*` из
  worker-потока.
- `src/graph.py:74` — `sqlite3.connect(self.path)` без
  `check_same_thread=False`.
- Воспроизведено реальным вызовом `fastmcp.Client` против `build_server`:
  `list_objects`, `graph_stats`, `reindex`, `search_config` падают с
  «SQLite objects created in a thread can only be used in that same thread».
  Т.е. **все 9 инструментов сломаны**.

**Фикс:** `sqlite3.connect(self.path, check_same_thread=False)` +
`threading.RLock` на execute/commit (или per-call соединение, или async-тулзы
с соединением в потоке event-loop).

### C2. `_coerce` не конвертирует в `Path` → `load_config()` падает при любой реальной конфигурации

- `src/config.py:83-90` проверяет `target_type is int / is float / is Path`.
- Целевой тип берётся как `type(getattr(obj, leaf))` (`src/config.py:93`), что
  для `Path`-полей даёт конкретный `PosixPath`; `PosixPath is Path == False`.
- `_coerce` «проваливается» и возвращает **сырую строку**; затем
  `validate()` вызывает `self.config_root.is_dir()` на `str` →
  `AttributeError: 'str' object has no attribute 'is_dir'`.
- Воспроизведено: падает и при установке `config_root` через JSON, и через
  `ONEC_CONFIG_ROOT`. Работает только дефолт без конфига. Задокументированный
  в README способ деплоя не стартует.

**Фикс:** `if target_type is Path or (isinstance(target_type, type) and
issubclass(target_type, Path)): return Path(value).expanduser()`.

### C3. Sample-файлы `Info.xml` повреждены (микс кириллицы/латиницы в тегах) → 0 элементов

- В `tests/sample_config/**/Info.xml` и `Config.xml` теги с перемешанными
  кириллическими и латинскими lookalike-символами: `<Cаталог>`, `<Nаме>`,
  `<Туре>`, `<Sунонум>`, `<DатаIтемТуре>` (подтверждено `od -c`: латинская
  `C` + кириллица `аталог` и т.п.).
- Поиск `_strip_ns`/`find("Name")`/`find("Type")` не находит эти теги:
  все 6 объектов парсятся с **0 элементов**, без синонимов и комментариев,
  0 REFERENCE-рёбер.
- Чистый шаблон в `_gen_sample.py` (корректные ASCII-теги) парсится
  нормально: 17 элементов, REFERENCE-рёбра работают.

**Фикс:** перегенерировать `tests/sample_config/` из `_gen_sample.py`,
закоммитить чистый XML; добавить тест с assert `len(obj.elements) > 0`.

---

## HIGH

### H1. `references_to` инвертирована → «кто ссылается на объект» всегда пусто — **FIXED ✅**

`src/graph.py:178-185`: `JOIN edges e ON e.dst = n.id WHERE e.src = ? AND
kind='REFERENCE'`. Для REFERENCE-ребра `элемент → объект` объект — это
`e.dst`, поэтому `WHERE e.src = object_id` ничего не находит. Воспроизведено
на изолированном хранилище: `references_to(obj_A)` → `[]`. Входная часть
`get_references(direction="in"/"both")` мертва.

**Фикс:** `JOIN edges e ON e.src = n.id WHERE e.dst = ?`.

### H2. Устаревшие узлы удаляются из SQLite, но никогда из Qdrant — **FIXED ✅**

`src/indexer.py:196` вызывает `graph.delete_stale_nodes(keep_ids)`, но
`client.delete(...)` нигде нет (в indexer'е только `collection_exists`,
`create_collection`, `upsert`, `close`). Docstring модуля и README утверждают,
что удалённые объекты «чистятся из обоих хранилищ». Удалённые
объекты/элементы/символы остаются в `search_config` навсегда.

**Фикс:** после вычисления drop-ид — `client.delete(collection,
points_selector=...)` по явным point id.

### H3. `get_symbol` обещает тело процедуры, но не возвращает его — **FIXED ✅**

Docstring: «signature, visibility, module, **body**» (`src/server.py:231`);
`_fmt_node` имеет ветку `with_body`, читающую `p.get("body")`
(`src/server.py:74`). Но props символа содержат только
`visibility/line/params/module` (`src/indexer.py:141-144`); `with_body`
никогда не передаётся `True`. Ключевая фича инструмента отсутствует.

**Фикс:** сохранять `body` в props символа (или отдельный столбец) и
выводить в `get_symbol`.

### H4. `delete_stale_nodes` строит неограниченный `IN (...)` → падение на больших конфигурациях — **FIXED ✅**

`src/graph.py:108-124`: `",".join("?"*len(keep_ids))` и `drop+drop` (удвоенно)
без чанкинга, в отличие от `_neighbors`, который чанкует по
`_SQLITE_PARAM_LIMIT=900`. Лимит SQLite 3.40 — 2000 переменных: конфигурация
с >~1000 узлов упрётся в `OperationalError: too many SQL variables`.
Реальная конфигурация 1С — тысячи объектов/элементов.

**Фикс:** переиспользовать чанкинг из `_neighbors`.

### H5. `search_config`: утечка `QdrantClient` на ошибках + нет валидации входа — **FIXED ✅**

`src/server.py:180-226`: `client.close()` только на happy path, нет
`try/finally` — при исключении из `query_points` (Qdrant down, некорректный
фильтр) утекает HTTP-пул на каждый вызов. `top_k` не проверяется
(отрицательное/огромное значение уходит в `limit`; `top_k=0` → `candidates=0`),
`kind` не ограничивается документированным набором — невалидный `kind`
тихо отфильтровывает всё.

**Фикс:** `try/finally: client.close()`; clamp `top_k >= 1`; валидация `kind`.

### H6. `reindex` (и `search_config`) могут повесить запрос MCP без ограничения — **FIXED ✅**

`reindex` синхронно в тулзе выполняет полный `run_index` (parse + embed всех
изменённых узлов; таймаут эмбеддера 120 с, `src/embedder.py:24`). На
тысячах узлов — минуты; MCP-клиент упрётся в таймаут, пока работа
продолжается в фоне.

**Фикс:** `reindex` теперь запускает `run_index` в фоновом daemon-потоке
(`src/server.py`), статус в `_ReindexState` (thread-safe); добавлен
инструмент `reindex_status`. `search_config` ограничен таймаутом эмбеддера.

### H7. Нет аутентификации на streamable HTTP, привязка к `0.0.0.0` — **FIXED ✅**

`main()` → `mcp.run(transport="streamable-http", host=cfg.host, ...)` с
дефолтом `host="0.0.0.0"` (`src/config.py:47`). Любой, кто видит порт:
читает всю конфигурацию и дергает `reindex` (векторная/стоимостная DoS).
FastMCP 4.x поддерживает `auth`.

**Фикс:** `auth_token` (env `ONEC_AUTH_TOKEN`) → `StaticTokenVerifier` в
`FastMCP(auth=...)`; пустое значение отключает auth. Проверено end-to-end:
без/с неверным токеном — 401, с верным — запрос проходит.

---

## MEDIUM

- **M1.** Ресурсы `App` не закрываются: нет `@mcp.on_shutdown`/lifespan для
  `app.graph` (sqlite), `app.embedder` (httpx), `app.reranker` (httpx).
- **M2.** `Reranker._get_client` (`src/reranker.py:44-51`) — ленивая
  инициализация без lock; при конкурентных вызовах два потока могут создать
  два клиента (один утёкает).
- **M3.** `bs_parser._PROC_RE` (`src/bs_parser.py:52-55`) привязан к одной
  строке (`^...$`): процедуры с переносом списка параметров молча не
  индексируются. `line_offsets` (`src/bs_parser.py:116-120`) вычисляется, но
  не используется (мёртвый код).
- **M4.** Нормализация имён в `config_parser` (`src/config_parser.py:238-242`)
  префиксует `RU_TYPE_BY_KEY[obj_type]`, тогда как идентификация объектов в
  остальных местах (ref_object, USES) опирается на имя из папки
  (`Catalog.Номенклатура`). REFERENCE/USES-рёбра стыкуются только когда обе
  стороны случайно используют одну форму. Нужен единый канонический ключ.
- **M5.** `object_by_name`/`symbols_by_name` (`src/graph.py:150,166`) — точное
  case-sensitive совпадение `name=?`, хотя идентификаторы 1С
  регистронезависимы; fallback по короткому имени
  (`endswith("." + object_name)`, `src/server.py:135,300`) тоже
  case-sensitive.
- **M6.** N+1-запросы: `list_objects` (`src/server.py:121`) вызывает
  `elements_of` на каждый объект в цикле; `get_object_elements` — `_children`
  на каждый элемент. Заменить одним чанкованным `WHERE src IN (...)`.
- **M7.** Связка с H5: `kind` в `search_config` идёт точным `MatchValue`
  в payload без валидации; `candidates = top_k * multiplier` может быть 0.
- **M8.** `_apply_json` (`src/config.py:140-158`) молча игнорирует
  неизвестные ключи (опечатка `baseurl` → ключ отброшен, env-значение
  неожиданно побеждает). Нужен warning.
- **M9.** `validate()` выполняется в `load_config()`: сервер отказывается
  стартовать, если `config_root` смонтирован некорректно, даже для
  read-only граф-тулзов. Проверить существование `config_root` лениво
  (только при индексации).

---

## LOW

- **L1.** Мусор в корне репо: `_fix_graph.py`, `_fix2.py` — разовые
  repair-скрипты (с комментариями об артефактах генерации) — удалить.
  `_gen_sample.py` полезен, но должен жить в `scripts/` и документироваться
  как источник истины для `tests/sample_config/`.
- **L2.** Нет unit-тестов: `tests/` содержит только (повреждённые) sample-данные;
  `pytest` в requirements отсутствует. Все C1/C2/H1/H4 ловились бы
  небольшим набором тестов на `bs_parser`, `config_parser`, `graph`, `config`.
- **L3.** Нет `.gitignore` в репо, где есть `data/` и `.venv/`.
  Добавить: `data/`, `.venv/`, `__pycache__/`, `*.sqlite3`, `.env`.
- **L4.** В репо лежит реальный дамп конфигурации:
  `data/AKADA/metadata/ОтчетПоКонфигурации.txt` — 36 МБ, UTF-16, выглядит как
  продакшн-конфигурация (возможно, чувствительные данные). Убрать из
  контроля версий.
- **L5.** `search_config` создаёт `QdrantClient` на каждый вызов
  (`src/server.py:180`) — лучше общий клиент с закрытием на shutdown.
- **L6.** httpx-клиенты без возможности настроить CA для self-signed
  внутренних эндпоинтов (verify по умолчанию True — ок).
- **L7.** Непоследовательный язык вывода тулзов (русские заголовки секций +
  английские имена) — выбрать один.
- **L8.** `stable_id` на SHA-1 (не security-критично); разделитель `|`
  теоретически может дать коллизию, если часть содержит `|`. Задокументировать.

---

## Рекомендуемые фиксы (file:line)

1. `src/graph.py:74` → `sqlite3.connect(self.path, check_same_thread=False)` +
   `threading.RLock` вокруг execute/commit. (C1)
2. `src/config.py:83-90` → в `_coerce`:
   `if target_type is Path or (isinstance(target_type, type) and
   issubclass(target_type, Path)): return Path(value).expanduser()`. (C2)
3. Перегенерировать `tests/sample_config/` из `_gen_sample.py`, закоммитить
   чистый XML; тест `len(obj.elements) > 0`. (C3)
4. `src/graph.py:178-185` → `references_to`: `JOIN edges e ON e.src = n.id
   WHERE e.dst = ? AND e.kind='REFERENCE'`. (H1)
5. `src/indexer.py:196` → после `delete_stale_nodes` удалять drop-иды из
   Qdrant. (H2)
6. `src/indexer.py:141-144` → `body` в props символа; `src/server.py:231-249` →
   выводить в `get_symbol`. (H3)
7. `src/graph.py:108-124` → чанкинг `IN`-списков в `delete_stale_nodes`
   по образцу `_neighbors`. (H4)
8. `src/server.py:178-226` → `try/finally: client.close()`; clamp `top_k`;
   валидация `kind`. (H5)
9. `src/server.py` → `@mcp.on_shutdown` (закрыть graph/embedder/reranker);
   lock в `Reranker._get_client`. (M1, M2)
10. `src/config.py:140-158` → warning на неизвестные JSON-ключи; перенести
    проверку `config_root` из стартового validate. (M8, M9)
11. Удалить `_fix_graph.py`, `_fix2.py`; перенести `_gen_sample.py` в
    `scripts/`; добавить `.gitignore`; убрать `data/AKADA/`. (L1, L3, L4)
12. Добавить `pytest` + unit-тесты на парсеры/граф/config. (L2)

## Рекомендуемый порядок работ

1. C1 + C2 (блокирующий минимум — без них ни один тулз-вызов не работает).
2. C3 (перегенерация sample-данных).
3. H1–H5 (каждый — 10–20 строк).
4. H6, H7 (дизайн-решения: бэкаграунд-реиндекс, auth).
5. M1–M9 по мере возможности.
6. L-хвост: уборка репо, `.gitignore`, тесты.
