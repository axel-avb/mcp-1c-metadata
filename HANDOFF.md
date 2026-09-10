# Handoff — текущее состояние

## Релиз: pre-1 (`81a7860`, тег `pre-1`)

Запушен в https://github.com/axel-avb/mcp-1c-metadata (ветка `master`, она же дефолтная;
`main` со скелетом LICENSE смержен и удалён).

## Что сделано (ФАЗА 0 + ФАЗА 1)
- **ФАЗА 0** — индексация по `ОтчетПоКонфигурации.txt` (первичный источник).
- **XML-манифест** — `xml_manifest.py` парсит `ConfigDumpInfo.xml` → дерево + `source_key`/`config_version`/`english_type`. Слияние с `.txt`.
- **Конфигурация** (`xml_config.py`) — `DefaultRunMode`/`InterfaceCompatibilityMode`/`CompatibilityMode`.
- **Пообъектные чек-суммы** — `object_checksums`; `--object <имя>` — точечная переиндексация.
- **Легаси-флаг** — `is_legacy` (глобальный режим + `FormType`), flat-колонка `nodes.is_legacy`.
- **BSL-слой** — `infer_module_role` под layout 2.13; 133 модуля / 3046 символов.
- **3-я линия** — `payload_only`/`node_id_in_payload` + `--payload-only`/`--kind`.
- **Тесты** — pytest на парсеры/маппинг/легаси-флаг.
- **Документация** — `README.md` (рус.), `README.en.md` (англ.), `PLAN.md`.

## Текущее состояние индекса
- SQLite: 28939 узлов / 40746 рёбер (2975 объектов + 22785 элементов + 133 модуля + 3046 символов).
- Qdrant: 28805 точек — object 2975/2975, element 22785/22785, symbol 3045/3046.
  Один дубль-ID (`Печать` в `Документ.Протокол` — условная компиляция `#Если/#Иначе`,
  зафиксировано в README §Ограничения).

## Известные ограничения (см. PLAN.md §12)
- **Объекты вне `.txt`** (бизнес-процессы, общие модули/формы, веб-сервисы) — есть module/symbol, нет object-узла и `HAS_MODULE`.
- **Легаси-формы** (`FormType=Ordinary`, `Form.bin`) — ждут внешний бинарный парсер (`parse_legacy_object`, phase-2).
- **Qdrant RAM** — 6 КБ/вектор (1536 dims); для ЕРП 2.x — десятки ГБ.
- **Условная компиляция** (`#Если/#Иначе`) не раскрывается BSL-парсером → возможны дубли-ID символов.

## Инфраструктура
- Эмбеддер `192.168.31.34:8081` (`jina-code-embeddings-1.5b-GGUF`, dim 1536) — работает.
- Qdrant `192.168.31.31:6333` — работает.
- Реранкер `192.168.31.34:8082` (`jina-reranker-v2-base-multilingual-GGUF`) — работает.

## Флаги/переменные
| Флаг | Назначение |
|---|---|
| `payload_only` / `ONEC_PAYLOAD_ONLY` / `--payload-only` | обновить payload без переэмбеддинга |
| `node_id_in_payload` / `ONEC_NODE_ID_IN_PAYLOAD` | хранить `node_id` в payload |
| `--kind {object,element,symbol}` | эмбеддить только один слой |
| `--object <имя>` | переиндексировать один объект |

## Замечания по репозиторию
- Git author: `Alexey V. Barnukoff <239055699+axel-avb@users.noreply.github.com>` (noreply — иначе GitHub
  отклоняет push по privacy email).
- Токен из `.git_creditionals.md` — только в `.gitignore` и remote URL; в коммиты не попадает.

## Пауза / TODO (временно отложено)
1. ~~Внешний бинарный парсер легаси-форм (phase-2)~~ — **сделано**: git-субмодуль
   `v8_ordinary_unpack`, `parse_legacy_object` + `_collect_legacy_form_modules`
   извлекают BSL-код из `Form.bin`; 1067 legacy-символов / 56 legacy-модулей.
   Схема формы — в text-описании парсера, в граф не раскладывается.
2. Дополнить `.txt` отсутствующими типами объектов (business_process/common_module) из манифеста.
3. Полный переэмбеддинг legacy-символов в Qdrant после смены парсера.

## Расширение инструментария (дорожная карта — PLAN.md §13)
- **Слой A** (в туду, реализуемо сейчас): `get_metadata`, `inspect_metadata_object`,
  `find_metadata_objects/elements`, `get_metadata_object_structure`, `get_bsl_modules`,
  `search_bsl_routines`, `get_bsl_routine_body`, `get_bsl_call_graph`, `search_bsl_code`.
- **Слой B** (в плане, с усилиями): интеграция `predefined_parser`,
  `event_subscription_parser`, `role_rights_parser` → `find_predefined_values`,
  `get_event_subscriptions`, `get_access_rights`.
- **Слой C** (отложено, титанически): extension diff, form structure/links,
  dependency paths.
