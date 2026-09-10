# 1C Configuration MCP Server

MCP-сервер для навигации по конфигурации 1С:Предприятие, выгруженной в исходный
код (XML-метаданные + BSL-модули). Предназначен для подключения LLM-агентов
(Claude, Cursor и любых MCP-клиентов) к «внутреннему устройству» конфигурации:
справочники, документы, регистры, реквизиты, процедуры модулей и граф вызовов.

Транспорт: **streamable HTTP** (FastMCP 4.x), эндпоинт `http://<host>:8765/mcp`.

## Возможности

- Разбор XML-экспорта конфигурации (объекты, элементы, типы данных, ссылки
  между объектами) — `src/config_parser.py`.
- Разбор BSL-модулей: процедуры/функции, видимость (`Экспорт`), вызовы,
  ссылки на объекты конфигурации (`Справочники.X`, `Документы.Y`, ...) —
  `src/bs_parser.py`.
- SQLite-граф (объекты → элементы, модули → символы, вызовы, ссылки) —
  `src/graph.py`.
- Семантический поиск: внешний эмбеддер (OpenAI-совместимый `/embeddings`) →
  ANN-поиск в Qdrant → внешний реранкер (Cohere/Jina-совместимый `/rerank`) —
  `src/embedder.py`, `src/reranker.py`.
- Инкрементальный индекс: хэши контента в SQLite, переэмбедятся только
  изменившиеся узлы; удалённые объекты чистятся из обоих хранилищ —
  `src/indexer.py`.

## Требования

- Python **3.11+**
- Qdrant (сервер по HTTP или локальный режим на диске)
- Эмбеддер с OpenAI-совместимым API: Ollama (`/v1`), vLLM, TEI, Jina, OpenAI,
  Azure — любой, кто отвечает на `POST /embeddings` в формате OpenAI.
- Реранкер (необязаtельно): Cohere/Jina-совместимый `POST /rerank`
  Без реранкера: `search_config` работает, возвращается порядок ANN-поиска (graceful fallback).

### Деградация без внешних сервисов

| Компонент | Что недоступно | Что работает |
|---|---|---|
| Без эмбеддера | `search_config` (семантический поиск) | Все структурные инструменты: `list_objects`, `get_object_elements`, `get_symbol`, `get_callers/callees`, `get_references`, `graph_stats` (SQLite) |
| Без реранкера | Переупорядочивание кандидатов | `search_config` работает в порядке ANN-поиска |
| Без Qdrant | `search_config`, векторный upsert при индексации | SQLite-граф полностью функционален; индексация с `--no-vectors` |

Индексация без эмбеддера: `python -m src.indexer --no-vectors` — строит только
SQLite-граф (объекты, элементы, рёбра). Семантический поиск появится после
заполнения `embedder.base_url` и повторного прогона индексации.

## Установка

```bash
git clone --recurse-submodules https://github.com/axel-avb/mcp-1c-metadata.git
# или, после обычного clone:
git submodule update --init --recursive

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Конфигурация

Приоритет (высший побеждает):

1. Переменные окружения (`ONEC_*`) — для секретов и деплоя.
2. JSON-файл конфигурации (путь из `ONEC_MCP_CONFIG`, по умолчанию `./config.json`).
3. Значения по умолчанию в `src/config.py`.

Шаблон: `config.json.example`. Скопируйте в `config.json` и заполните.

```json
{
  "config_root": "/path/to/onec/sources",
  "project_data_dir": "/path/to/onec/project-export",
  "xml_root": "/path/to/onec/code",
  "txt_root": "/path/to/onec/metadata",
  "qdrant": { "url": "http://localhost:6333", "collection": "onec_config" },
  "graph_db_path": "./data/graph.sqlite3",
  "embedder": {
    "base_url": "http://localhost:11434/v1",
    "api_key": "",
    "model": "bge-m3",
    "dimensions": 1024,
    "batch_size": 32
  },
  "reranker": {
    "endpoint": "",
    "api_key": "",
    "model": ""
  },
  "search": { "top_k": 5, "candidate_multiplier": 4 },
  "host": "0.0.0.0",
  "port": 8765,
  "auth_token": "",
  "payload_only": false,
  "node_id_in_payload": true
}
```

Если `auth_token` (или `ONEC_AUTH_TOKEN`) задан, сервер требует заголовок
`Authorization: Bearer <token>` на всех HTTP-запросах. Пустое значение —
аутентификация отключена.

### Переменные окружения

| Переменная | Описание |
|---|---|
| `ONEC_CONFIG_ROOT` | Корень исходников конфигурации 1С (XML/BSL) |
| `ONEC_QDRANT_URL` | URL Qdrant, напр. http://localhost:6333 |
| `ONEC_QDRANT_API_KEY` | API-ключ Qdrant (если включён) |
| `ONEC_QDRANT_COLLECTION` | Имя коллекции (по умолчанию onec_config) |
| `ONEC_GRAPH_DB_PATH` | Путь к SQLite-графу |
| `ONEC_EMBEDDER_BASE_URL` | Base URL эмбеддера (OpenAI-совместимый /embeddings) |
| `ONEC_EMBEDDER_API_KEY` | Ключ эмбеддера |
| `ONEC_EMBEDDER_MODEL` | Модель эмбеддинга (по умолчанию bge-m3) |
| `ONEC_EMBEDDER_DIMENSIONS` | Размерность вектора (должна совпадать с моделью) |
| `ONEC_EMBEDDER_BATCH_SIZE` | Пакетность эмбеддинга |
| `ONEC_RERANKER_ENDPOINT` | URL реранкера (Cohere/Jina-совместимый), пусто = выключен |
| `ONEC_RERANKER_API_KEY` | Ключ реранкера |
| `ONEC_RERANKER_MODEL` | Модель реранкера (необязательно) |
| `ONEC_SEARCH_TOP_K` | Сколько результатов возвращать |
| `ONEC_HOST` | Адрес HTTP-сервера (по умолчанию 0.0.0.0) |
| `ONEC_PORT` | Порт HTTP-сервера (по умолчанию: 8765) |
| `ONEC_AUTH_TOKEN` | Токен аутентификации: если задан, требуется заголовок `Authorization: Bearer <token>` |
| `ONEC_XML_ROOT` | Корень XML-выгрузки (`ConfigDumpInfo.xml` + per-object XML); по умолчанию `project_data_dir/code` |
| `ONEC_TXT_ROOT` | Корень TXT-отчёта (`ОтчетПоКонфигурации.txt`); по умолчанию `project_data_dir/metadata` |
| `ONEC_PAYLOAD_ONLY` | `true` — обновлять только payload в Qdrant без переэмбеддинга |
| `ONEC_NODE_ID_IN_PAYLOAD` | `true` (по умолчанию) — хранить `node_id` в payload Qdrant для `search_config` |

Полный точный список — в `_ENV_MAP` в `src/config.py`.

## Формат исходников конфигурации

Ожидается стандартная выгрузка «конфигурация в исходном коде»:

```
<config_root>/
  Config.xml                              # корневые метаданные
  Catalogs/Catalog.Номенклатура/
    Info.xml                              # метаданные объекта
    ObjectModule.bsl                      # модуль объекта
    ManagerModule.bsl                     # модуль менеджера
    Forms/FormНоменклатуры/FormModule.bsl # модуль формы
  Documents/Document.Реализация/...
  Constants/Constant.ИнформацияОКомпании/...
  Registers/AccumulationRegisters/Register.Обороты/...
  Registers/InformationRegisters/Register.ЦеноваяИнформация/...
```

Парсер терпим к вариациям: принимает и плоские элементы (`<Type>Catalog</Type>`),
и пары «свойство-значение». Имена объектов нормализуются к полной форме с
русским префиксом типа: `Каталог.Номенклатура`, `Документ.Реализация`.

В `tests/sample_config/` лежит минимальный образец для ручного прогона.

## Индексация

```bash
# инкрементальная (по умолчанию): переэмбедятся только изменившиеся узлы
python -m src.indexer

# полный пересбор
python -m src.indexer --full

# без векторов (только граф в SQLite) — для отладки парсеров
python -m src.indexer --no-vectors

# переиндексация одного объекта (русское или английское имя)
python -m src.indexer --object Справочник.Колледжи
python -m src.indexer --object Catalog.Колледжи

# эмбеддить только один слой (object|element|symbol); граф остаётся в SQLite
python -m src.indexer --kind symbol

# обновить только payload в Qdrant без переэмбеддинга (после смены флагов)
python -m src.indexer --payload-only

# другой файл конфигурации
python -m src.indexer --config /path/to/config.json
```

Вывод — статистика: число объектов, BSL-файлов, узлов/рёбер графа и
обновлённых векторов. Индексация идемпотентна; при удалении объектов из
конфигурации их узлы и векторы удаляются из хранилищ.

## Запуск MCP-сервера

```bash
python -m src.server
# INFO: Starting MCP server '1c-configuration' with transport 'streamable-http'
#       on http://0.0.0.0:8765/mcp
```

Сервер читает ту же конфигурацию и держит в памяти граф, клиент эмбеддера и
клиент реранкера. Векторные запросы к Qdrant выполняются на лету.

### Инструменты (tools)

**Инвентарь и структура**

| Инструмент | Назначение |
|---|---|
| `get_metadata(mode, category?, object_name?, object_match?, limit?, offset?)` | Инвентарь: `summary` (счётчики), `categories` (типы), `objects` (список с фильтром) |
| `inspect_metadata_object(object_ref, detail?, sections?)` | Досье объекта одним вызовом: счётчики, структура, формы, BSL-модули, использование |
| `get_metadata_object_structure(object_ref, sections?, tabular_part?)` | Структура объекта по секциям (attributes/tabular_parts/forms/commands/layouts/resources/dimensions) |
| `get_metadata_element_type(object_ref, element_type, container_ref?)` | Типизированные дети объекта (реквизиты/ресурсы/измерения/…) |
| `get_metadata_details(ref_type, ref, owner_ref?, mode?)` | Разрешение ссылки в карточку узла (object/element/symbol) |
| `list_objects(type?)` | Объекты по типам с числом элементов |
| `get_object_elements(object_name, include_children=true)` | Элементы объекта: реквизиты, табличные части, команды |

**Поиск**

| Инструмент | Назначение |
|---|---|
| `search_config(query, top_k=5, kind?)` | Семантический поиск по объектам/элементам/процедурам (embed → Qdrant → rerank) |
| `find_metadata_objects(search_by, search_text?, within_object?, limit?)` | Найти объекты по описанию или по имени дочернего элемента («где поле X») |
| `find_metadata_elements(element_type, element_name?, owner_object?, mode?, limit?)` | Дочерние элементы по всему проекту с контекстом владельца |
| `find_metadata_usages(target_ref, mode?)` | Кто ссылается на объект / какие модули его используют |

**BSL**

| Инструмент | Назначение |
|---|---|
| `search_bsl_code(query, top_k=5)` | Семантический поиск по телам процедур/функций |
| `get_symbol(name, object_name?)` | Процедура/функция: сигнатура, видимость, модуль, тело |
| `get_callers(name, object_name?)` | Кто вызывает процедуру (входные рёбра) |
| `get_callees(name, object_name?)` | Что вызывает процедура (выходные рёбра) |
| `get_bsl_call_graph(routine_ref, mode?, depth?, owner_ref?)` | Граф вызовов: `callees`/`callers`/`subtree` (BFS с глубиной) |
| `get_bsl_routine_body(routine_ref, owner_ref?, body_offset?, body_limit?)` | Тело рутины с пагинацией |
| `get_bsl_modules(mode, owner_ref?, module_ref?, routine_name?)` | Модули объекта и их рутины |
| `search_bsl_routines(name?, mode?, object_name?, exported_only?, limit?)` | Поиск рутин по имени/экспорту/сигнатуре |

**Ссылки и служебные**

| Инструмент | Назначение |
|---|---|
| `get_references(object_name, direction="both")` | Ссылки на объект и от объекта |
| `reindex(full=false)` | Пересбор индекса в фоне |
| `reindex_status` | Статус фоновой переиндексации |
| `graph_stats` | Статистика графа: узлы/рёбра по видам |

Дорожная карта оставшегося (полная — `PLAN.md` §13):

- **Слой B** (нужна интеграция парсеров из сабмодуля): `find_predefined_values`,
  `get_event_subscriptions`, `get_access_rights`.
- **Слой C** (отложено, нет модели): `get_extension_object_diff`,
  `get_form_structure`/`find_form_links`, `find_dependency_paths`.

### Расход токенов на инициализацию модели

Полный `tools/list` для всех 23 инструментов — **~17 770 символов** (~4.5–5 тыс.
токенов при латинском тексте). Эта сумма уходит на каждый старт/повторную
инициализацию клиента (загрузка схемы инструментов в контекст).

Самые тяжёлые инструменты (из-за длинных перечислений значений в docstring):

| Инструмент | Символов |
|---|---|
| `get_metadata` | 1059 |
| `get_metadata_object_structure` | 1031 |
| `find_metadata_elements` | 979 |
| `search_bsl_routines` | 935 |
| `get_metadata_element_type` | 913 |

Если расход критичен — срезать многословные перечисления (`sections`,
`element_type`, `search_by`) из docstring'ов (они дублируют JSON-schema), это даёт
~1–1.5 тыс. токенов экономии без потери функциональности.

Примеры вызовов (что видит LLM-агент):

```text
list_objects(type="catalog")
  → «Каталоги (catalog) — 2
       • Каталог.Номенклатура — Номенклатура [5 elem.]
       • Каталог.ПрайсЛист [12 elem.]»

search_config(query="где хранится цена товара", top_k=5)
  → [0.87] element: Цена [Число(10,2)] (объект: Каталог.Номенклатура)
    [0.74] object: РегистрСведений.ЦеноваяИнформация ...

get_callers(name="РассчитатьСуммуРеализации")
  → Callers of РассчитатьСуммуРеалиции:
       • Обработать — Документ.Реализация (Documents/Document.Реализация/ObjectModule.bsl)
```

## Подключение MCP-клиента

Эндпоинт: `http://<host>:<port>/mcp` (transport: streamable HTTP).

### Claude Desktop / Claude Code

```json
{
  "mcpServers": {
    "1c": {
      "url": "http://localhost:8765/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

Заголовок `headers` нужен только если задан `auth_token`.

### Python (FastMCP Client)

```python
from fastmcp import Client

async def main():
    async with Client("http://localhost:8765/mcp") as c:
        print(await c.call_tool("list_objects", {}))
        print(await c.call_tool("search_config", {"query": "цена товара"}))
```

### Проверка вручную

```bash
# список инструментов (MCP JSON-RPC)
curl -s http://localhost:8765/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Docker

`docker-compose.yml` поднимает Qdrant + сервер. Исходники конфигурации
монтируются в `/data/config`, эмбеддер ожидается по адресу
`ONEC_EMBEDDER_BASE_URL` (например, Ollama на хосте: `http://host.docker.internal:11434/v1`).

```bash
# 1. заполнить .env (см. .env.example) и config.json
# 2. загрузить модель эмбеддинга в Ollama (или другой сервис):
#    ollama pull bge-m3
docker compose up -d --build

# индексация при старте выполняется автоматически (entrypoint.sh)
docker compose logs -f mcp
```

Вручную: `docker compose run --rm mcp python -m src.indexer --full`.

## Архитектура

```
XML/BSL-экспорт 1С
        │
        ▼
src/config_parser.py ── объекты, элементы, ссылки (типы данных)
src/bs_parser.py ────── процедуры, вызовы, ссылки из кода
        │
        ▼
src/indexer.py ───────── инкрементальная сборка
   ├──► src/graph.py    SQLite: узлы (object/element/symbol/module),
   │                    рёбра (HAS_ELEMENT, CHILD_ELEMENT, REFERENCE,
   │                    DEFINES, CALLS, USES)
   └──► Qdrant          векторы (эмбеддер: OpenAI-совместимый API)
        │
        ▼
src/server.py (FastMCP, streamable HTTP :8765/mcp)
   list_objects / get_object_elements / search_config (embed→Qdrant→rerank)
   get_symbol / get_callers / get_callees / get_references / reindex / graph_stats
```

Виды рёбер графа:

- `HAS_ELEMENT` — объект → элемент (прямые дети)
- `CHILD_ELEMENT` — элемент → вложенный элемент
- `REFERENCE` — элемент → объект (типы данных, напр. реквизит → справочник)
- `DEFINES` — модуль → символ
- `CALLS` — символ → символ (1С имеет плоское глобальное пространство имён,
  поэтому вызов может вести к нескольким одноимённым символам — это норма)
- `USES` — модуль → объект (`Справочники.X` и т.п. в коде)

## Тесты

```bash
# unit-тесты (парсеры/маппинг/легаси-флаг/чек-суммы)
pytest tests/

# компиляция всех модулей
python -m compileall src
```

## Ограничения

- BSL-парсер строковый/regex'овый, а не полный грамматический: достаточен для
  графа вызовов и поиска, но не для строгой валидации кода.
- **Условная компиляция не раскрывается.** BSL-парсер не обрабатывает директивы
  препроцессора `#Если`/`#Иначе`/`#КонецЕсли`, поэтому процедура, объявленная в
  обеих ветках (напр. серверная `Печать` для обычных неуправляемых форм внутри
  `#Если ТолстыйКлиентОбычноеПриложение ... #Иначе ... #КонецЕсли`), даёт две
  декларации с одинаковым `stable_id`. На поиск/граф не влияет (upsert по id
  перезаписывает), но в счётчике символов возможен дубль.
- Вызовы разрешаются по имени (глобальное пространство имён 1С): при
  одноимённых процедурах в разных модулях `CALLS`-рёбра ведут на всех
  кандидатов; инструмент `get_symbol` принимает `object_name` для уточнения.
- Размерность вектора (`embedder.dimensions`) должна совпадать с моделью,
  иначе Qdrant отклонит upsert.
- **Объекты вне `.txt`-отчёта** (бизнес-процессы, общие модули/формы, веб-сервисы
  и др.) индексируются как module/symbol, но без object-узла и `HAS_MODULE`-связи
  (см. `PLAN.md` §12).
- **Легаси-формы** (`FormType=Ordinary`, `Form.bin`) — BSL-код извлекается
  вендоренным бинарным парсером (`parsers/`, из github.com/axel-avb/v8_ordinary_unpack);
  процедуры попадают в граф вызовов с `is_legacy=True`. Схема формы (элементы
  управления) — в text-описании парсера, не раскладывается в узлы графа.
- **Qdrant RAM** — единственное реальное ограничение по объёму: ~6 КБ/вектор
  (1536 dims float32), для ЕРП 2.x это десятки ГБ RAM (см. `PLAN.md` §12).
