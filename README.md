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
- Реранкер (необязательно): Cohere/Jina-совместимый `POST /rerank`.
  Без реранкера поиск работает — возвращается порядок ANN-поиска.

## Установка

```bash
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
  "port": 8765
}
```

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

| Инструмент | Назначение |
|---|---|
| `list_objects(type?)` | Объекты конфигурации сгруппированы по типам, число элементов |
| `get_object_elements(object_name, include_children=true)` | Все элементы объекта: реквизиты, табличные части, команды; типы данных и ссылки |
| `search_config(query, top_k=5, kind?)` | Семантический поиск по объектам/элементам/процедурам (embed → Qdrant → rerank) |
| `get_symbol(name, object_name?)` | Процедура/функция: сигнатура, видимость, модуль, строка |
| `get_callers(name, object_name?)` | Кто вызывает данную процедуру (входные рёбра графа вызовов) |
| `get_callees(name, object_name?)` | Что вызывает данная процедура (выходные рёбра) |
| `get_references(object_name, direction="both")` | Ссылки на объект и от объекта (типы данных реквизитов) |
| `reindex(full=false)` | Пересбор индекса прямо из MCP-инструмента |
| `graph_stats` | Статистика графа: узлы/рёбра по видам |

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
      "url": "http://localhost:8765/mcp"
    }
  }
}
```

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
# компиляция всех модулей
python -m compileall src

# минимальный образец конфигурации для ручного прогона:
# tests/sample_config/ (Config.xml + каталоги)
ONEC_CONFIG_ROOT=tests/sample_config ONEC_EMBEDDER_BASE_URL=http://localhost:11434/v1 \
  python -m src.indexer --no-vectors
```

## Структура проекта

```
src/config.py         загрузка конфигурации (JSON + env), валидация
src/config_parser.py  парсер XML-метаданных конфигурации
src/bs_parser.py      парсер BSL-модулей (процедуры, вызовы, ссылки)
src/graph.py          SQLite-граф: схема, upsert, запросы соседей
src/indexer.py        сборка графа + векторов, инкрементальность
src/embedder.py       клиент эмбеддера (OpenAI-совместимый)
src/reranker.py       клиент реранкера (Cohere/Jina-совместимый), fallback
src/server.py         FastMCP-сервер: инструменты, streamable HTTP
tests/sample_config/  минимальный образец конфигурации 1С
data/                 SQLite-граф (создаётся при индексации)
```

## Ограничения

- BSL-парсер строковый/regex'овый, а не полный грамматический: достаточен для
  графа вызовов и поиска, но не для строгой валидации кода.
- Вызовы разрешаются по имени (глобальное пространство имён 1С): при
  одноимённых процедурах в разных модулях `CALLS`-рёбра ведут на всех
  кандидатов; инструмент `get_symbol` принимает `object_name` для уточнения.
- Размерность вектора (`embedder.dimensions`) должна совпадать с моделью,
  иначе Qdrant отклонит upsert.
