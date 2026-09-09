# Handoff — текущее состояние

## Что сделано (закоммичено, ФАЗА 1)
- **XML-манифест** (`ef34434`): `xml_manifest.py` парсит `ConfigDumpInfo.xml` → дерево + `source_key`/`config_version`/`english_type`. Слияние с `.txt` (ФАЗА 0 — истина по объектам/синонимам).
- **Конфигурация** (`xml_config.py`): `DefaultRunMode`/`InterfaceCompatibilityMode`/`CompatibilityMode`.
- **Пообъектные чек-суммы** (`5fd6e3d`): `object_checksums` = `sha256(source_key ∥ config_version ∥ bsl_hashes)`; `--object <имя>` — точечная переиндексация одного объекта.
- **Легаси-флаг** (`21e002e`): `is_legacy` (глобальный `DefaultRunMode` + `FormType`), flat-колонка `nodes.is_legacy` + индекс.
- **BSL-слой** (`4e2e0af`): `infer_module_role` под layout 2.13 (`Catalogs/ВУЗы/Ext/ObjectModule.bsl` → `Справочник.ВУЗы`), 133 модуля / 3046 символов; устойчивый `parse_report`.
- **Тесты** (`d9670c5`): pytest на парсеры/маппинг/легаси-флаг.
- **3-я линия** (`payload_only` + `node_id_in_payload`): управление через `ONEC_PAYLOAD_ONLY`/`ONEC_NODE_ID_IN_PAYLOAD` и `--payload-only`/`--kind`.

## Текущее состояние индекса
- SQLite: 28939 узлов / 40746 рёбер (2975 объектов + 22785 элементов + 133 модуля + 3046 символов).
- Qdrant: **в процессе добора** — символы эмбедятся `--kind symbol` (~1.1 vec/s, ETA ~30 мин).
  - object: 2975/2975, element: 22785/22785, symbol: частично (идёт фоном).

## Известные ограничения (см. PLAN.md §12)
- **Объекты вне `.txt`** (бизнес-процессы, общие модули/формы, веб-сервисы) — есть module/symbol, нет object-узла и `HAS_MODULE`.
- **Легаси-формы** (`FormType=Ordinary`, `Form.bin`) — ждут внешний бинарный парсер (`parse_legacy_object`, phase-2).
- **Qdrant RAM** — 6 КБ/вектор (1536 dims); для ЕРП 2.x — десятки ГБ (рычаг: модель с меньшей dim).

## Инфраструктура
- Эмбеддер `192.168.31.34:8081` (`jina-code-embeddings-1.5b-GGUF`, dim 1536) — работает.
- Qdrant `192.168.31.31:6333` — работает.
- Реранкер `192.168.31.34:8082` (`jina-reranker-v2-base-multilingual-GGUF`) — работает.

## Флаги/переменные (3-я линия)
| Флаг | Назначение |
|---|---|
| `payload_only` / `ONEC_PAYLOAD_ONLY` / `--payload-only` | обновить payload без переэмбеддинга |
| `node_id_in_payload` / `ONEC_NODE_ID_IN_PAYLOAD` | хранить `node_id` в payload (для `search_config`) |
| `--kind {object,element,symbol}` | эмбеддить только один слой |
| `--object <имя>` | переиндексировать один объект |

## Следующий шаг
1. Дождаться фонового `--kind symbol` (или перезапустить при прерывании — инкрементально докачает).
2. Подключить внешний бинарный парсер легаси-форм (phase-2) — контракт в `xml_object.parse_legacy_object`.
3. Опционально: добавить в `.txt` отсутствующие типы объектов (business_process/common_module) из манифеста.
