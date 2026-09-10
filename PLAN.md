# План: многоисточниковая индексация 1С-выгрузки (ФАЗА 1)

> Статус: согласован, готов к реализации.
> ФАЗА 0 (текущая) — индексация по `ОтчетПоКонфигурации.txt` — завершена и остаётся в силе.

---

## 0. Итог согласования (решения владельца)

1. **`.txt` — истина.** `ОтчетПоКонфигурации.txt` — первичная база ФАЗЫ 0; по набору объектов и русским синонимам авторитетен именно он. Не заменяется.
2. **`.xml` — дополнение.** Манифест `ConfigDumpInfo.xml` + per-object XML дают: английские имена (`source_key`), `id`, `configVersion` и детали, которых нет в `.txt`. При конфликте набор объектов/синонимы берутся из `.txt`.
3. **Идентичность узлов — не трогаем.** `stable_id` от логических частей остаётся. Добавляется поле `source_key`.
4. **`object_checksum`** = `sha256(source_key ∥ config_version ∥ bsl_hashes)` — принято: `config_version` покрывает метаданные, `bsl_hashes` — код.
5. **Легаси-флаг — вернуть.** Добавляется флаг `is_legacy` (детекция в три уровня: конфигурация → форма → объект, см. §6).
6. **Шов легаси-парсера.** Внешний бинарный парсер пользователя на этом этапе **не интегрируется**. В коде оставляется единственный метод-заглушка вызова (чтобы не искать по проекту позже), помеченный `# TODO(phase-2)`. Данные легаси-форм пока хранятся в бинарном виде как есть.

---

## 1. Контекст и цели

**Текущее состояние (ФАЗА 0):**
- `src/report_parser.py` парсит `ОтчетПоКонфигурации.txt` (UTF-16) → объекты с синонимами/комментариями/типами/ref-ссылками.
- `src/config_parser.py` — deprecated XML-парсер под несуществующий формат `Info.xml`; для реальной выгрузки 2.13 непригоден (не трогаем).
- `src/indexer.py` — `collect_graph` жёстко читает один `.txt` + BSL из `config_root`; `run_index` считает `changed` по `node_hashes` (хэш эмбеддируемого текста), переэмбедит только изменившиеся узлы.
- `src/graph.py` — SQLite-граф (nodes/edges/node_hashes/meta); `props` — JSON-строка (поля добавляются без миграции).

**Реальная выгрузка `data/AKADA/code/` (формат XML 2.13):**
- `ConfigDumpInfo.xml` (6.4 МБ) — манифест: **34056** узлов, полная вложенность `Type.ObjectName.TabularSection.X.Attribute.Y`, у каждого `id` (UUID) + `configVersion` (контрольная сумма 1С).
- `Configuration.xml` (0.3 МБ) — корень (`DefaultRunMode`, `InterfaceCompatibilityMode`, `CompatibilityMode`).
- `Configuration_ID` — бинарная подпись.
- Per-object `<Type>.<Name>.xml` — докладываются пользователем; несут детали, которых нет в манифесте.

**Цели ФАЗЫ 1:**
1. `.txt` остаётся первичной базой; `.xml`-манифест дополняет её деревом, английскими именами, `id`, `configVersion`.
2. Пообъектная контрольная сумма → индексация «на лету» без переэмбеддинга всей конфы.
3. Переиндексация **по имени объекта** в CLI.
4. Неполная/сломанная выгрузка не ломает сервер.

---

## 2. Целевая архитектура — слои источников

Единая модель `ConfigObject`/`ConfigElement` собирается из слоёв, каждый опционален и изолирован по ошибкам:

```
Sources (все → ConfigObject/ConfigElement)
├─ ФАЗА 0  report_parser    ОтчетПоКонфигурации.txt   (синонимы, типы, ref, комментарии) — ИСТИНА
├─ манифест xml_manifest    ConfigDumpInfo.xml         (полное дерево, en-имена, id, configVersion)
├─ объекты  xml_object      code/<Folder>/<Type>.<Name>.xml  (детали, типы, свойства)
└─ BSL      bs_parser       code/**/*.bsl / *.bs             (процедуры, вызовы, USES)
        │
        ▼
   merge (по source_key) ──► graph_builder ──► SQLite + Qdrant
```

**Канонический ключ `source_key`** — английское имя из манифеста, напр. `Catalog.Колледжи`:
- совпадает с каталогом выгрузки (`Catalogs/Catalog.Колледжи/`);
- ключ слияния всех слоёв;
- хранится в `props.source_key` узла-объекта.

---

## 3. Модель данных и схема

### 3.1 Расширение `props` объекта (без миграции таблиц)

| Поле | Значение | Источник |
|---|---|---|
| `source_key` | `Catalog.Колледжи` | манифест |
| `english_type` | `Catalog` | манифест |
| `config_version` | `bbf14bc4…` | манифест (`configVersion`) |
| `is_legacy` | `true/false` | `Configuration.xml` + per-object `FormType` (см. §6) |

Плюс **flat-колонка `nodes.is_legacy`** (INTEGER 0/1) + индекс `idx_nodes_legacy` — для быстрой фильтрации на крупных конфигурациях (ЕРП2.x), где `json_extract(props)` по O(n)-скану просядет. Требует ленивой `ALTER TABLE`-миграции для существующих `graph.sqlite3`. На текущей микро-конфе влияние минимально, но закладываем сразу (см. туду 3-й линии).

### 3.2 Новая таблица `object_checksums`

```sql
CREATE TABLE IF NOT EXISTS object_checksums (
    source_key TEXT PRIMARY KEY,
    checksum   TEXT NOT NULL,        -- sha256(source_key ∥ config_version ∥ bsl_hashes)
    updated_at TEXT NOT NULL
);
```

Отличие от `node_hashes` (хэш эмбеддируемого текста узла): здесь — хэш **источника объекта**, грубый gate «изменился ли объект вообще».

### 3.3 `meta` — глобальные чек-суммы

- `config_checksum` — есть (хэш `.txt`).
- `dump_checksum` — хэш `ConfigDumpInfo.xml` (новое).
- `configuration_id_checksum` — хэш `Configuration_ID` (опционально).

---

## 4. Парсеры

### 4.1 `xml_manifest.py` (новый)
- Разбор `ConfigDumpInfo.xml` (`ConfigVersions/Metadata`), ns `{http://v8.1c.ru/8.3/xcf/dumpinfo}`.
- Строит `ConfigObject`/`ConfigElement`:
  `Attribute→attribute`, `TabularSection→tabular_section`, `Form→form`, `Resource→resource`, `Dimension→dimension`, `Template→template`, `Command→command`; модули `ObjectModule/ManagerModule/RecordSetModule/Module`.
- Даёт: полное дерево, `id`, `configVersion`, `source_key`, английские имена.
- **Устойчивость:** `ET.ParseError` → лог + пропуск; `FileNotFoundError` → пустой список.

### 4.2 `xml_object.py` (новый)
- Разбор per-object `<Type>.<Name>.xml`, ns `{http://v8.1c.ru/8.3/MDClasses}`.
- Извлекает `Properties` (Name/Synonym/Comment), типы реквизитов (`Type/Types/TypeDescription`).
- Обогащает объект из манифеста; битый/отсутствующий файл → пропуск, объект остаётся в каркасном виде.

### 4.3 Шов легаси-парсера (интегрирован, phase-2 ✅)

Вендорен `parsers/` (github.com/axel-avb/v8_ordinary_unpack, pure-stdlib). `parse_legacy_object`
извлекает BSL-код + структуру формы из `Form.bin`; `_collect_legacy_form_modules` в indexer
превращает каждую форму в виртуальный BSL-модуль (символы с `is_legacy=True`).
Схема формы (элементы управления) остаётся в text-описании парсера, в граф не раскладывается.

```python
def parse_legacy_object(source_key: str, dump_dir: Path) -> dict | None:
    # returns {"module_code": ..., "structure_text": ..., "form_count": N}
    ...
```

### 4.4 BSL — поправить источник
- Сейчас `config_root` указывает на `metadata` → `found 0 BSL files`.
- BSL искать в `project_data_dir/code/**` (`.bsl`/`.bs`), включая `Ext/`-подкаталоги и `Form/Module.bsl`.

---

## 5. Маппинг типов выгрузки (полный)

Дополнить `EN_TYPE_MAP` + генератор имён каталогов. Реально встречаются в `ConfigDumpInfo.xml`:

| en (folder) | normalized | ru-префикс имени |
|---|---|---|
| Catalog | catalog | Справочник |
| Document | document | Документ |
| AccumulationRegister | accumulation_register | РегистрНакопления |
| InformationRegister | information_register | РегистрСведений |
| AccountingRegister | accounting_register | РегистрБухгалтерии |
| ChartOfCharacteristicTypes | chart_of_characteristics | ПланВидовХарактеристик |
| ChartOfAccounts | chart_of_accounts | ПланСчетов |
| ExchangePlan | exchange_plan | ПланОбмена |
| Constant | constant | Константа |
| Enum | enumeration | Перечисление |
| Report | report | Отчет |
| DataProcessor | processing | Обработка |
| CommonModule | common_module | ОбщийМодуль |
| CommonForm | common_form | ОбщаяФорма |
| CommonTemplate | common_template | ОбщийМакет |
| Role | role | Роль |
| Subsystem | subsystem | Подсистема |
| Task | task | Задача |
| BusinessProcess | business_process | БизнесПроцесс |
| DocumentJournal | document_journal | ЖурналДокументов |
| ScheduledJob | scheduled_job | РегламентноеЗадание |
| HTTPService / WebService / WSReference / XDTOPackage / SettingsStorage / SessionParameter / EventSubscription / ExternalDataSource / Interface / Style / StyleItem / Language / CommonPicture / FilterCriterion | passthrough | как есть |

Генератор имён каталогов: `Catalog→Catalogs/`, `Enum→Enums/`, `DataProcessor→DataProcessors/`, …

---

## 6. Легаси-флаг (детекция)

Три уровня, слияние в единый `is_legacy`:

1. **Глобальный** — `Configuration.xml`: `DefaultRunMode` (`OrdinaryApplication` → legacy-конфиг; `ManagedApplication` → управляемый; `Auto` → совместимость).
2. **На форму** — per-object `FormType` (`Managed`/`Ordinary`/`Auto`) из `xml_object.py`; `Ordinary` → форма легаси (бинарная схема, парсится legacy-парсером).
3. **На объект** — если у объекта есть хотя бы одна обычная форма или `run_mode=ordinary` → `is_legacy=True`.

`InterfaceCompatibilityMode=Taxi` + `DefaultRunMode=OrdinaryApplication` в данной выгрузке = режим совместимости: часть объектов управляемые, часть легаси. Флаг хранится на каждом узле (`object`, `form`, `symbol`), чтобы поиск/граф различали.

---

## 7. Чек-суммы и инкрементальная индексация «на лету»

**Два уровня:**

1. **`object_checksums` (объект)** — `sha256(source_key ∥ config_version ∥ bsl_hashes)`.
   На старте `run_index` сравнивает с сохранённым: изменившийся объект переразбирается целиком, остальные пропускаются (не переразбираем весь манифест/txt).
2. **`node_hashes` (узел)** — существующий механизм: внутри изменившихся объектов переэмбедится только изменённый узел.

**Следствие:** изменение кода одного объекта → переразбор и переэмбеддинг только его.

**Механика:**
- `collect_graph` → пообъектный ленивый `parse_object(source_key)` вместо монолитного парсинга.
- `run_index` получает `--object <name>`: переразобрать только эти объекты (резолв `source_key`/русского имени через граф/манифест).
- `--full` — сброс `object_checksums` + `node_hashes`, полный пересбор.
- Запись `object_checksums` и `node_hashes` — **после** успешного upsert (паттерн уже есть для `node_hashes`; распространить на объектный уровень).

---

## 8. CLI индексера

```
python -m src.indexer [--config X] [--full] [--no-vectors]
                       [--object <name|source_key>] [--source {txt,xml,all}]
```

- `--object` — русское (`Справочник.Колледжи`) или английское (`Catalog.Колледжи`) имя; переиндексировать один объект.
- `--source` — ограничить слои (отладка).
- Без аргументов — инкрементально по чек-суммам (двухуровнево).

---

## 9. Устойчивость (сквозное требование)

- Каждый парсер оборачивает чтение/разбор в try/except: битый XML/файл → `log.warning` + пропуск **этого** источника; объект остаётся из других слоёв.
- Отсутствующий `.txt`, `ConfigDumpInfo.xml`, BSL-каталог → пустой результат слоя, не исключение.
- `merge` не падает на несовпадении слоёв: несогласованные объекты помечаются `props.incomplete=True`, не отбрасывают прогон.
- `validate()` в `load_config` уже вынесен; `config_root`/`project_data_dir` проверяются лениво.

---

## 10. Конфигурация

Новые поля `AppConfig` (env `ONEC_*`):

| Поле | Назначение |
|---|---|
| `project_data_dir` | (есть) корень `data/AKADA` |
| `xml_root` | (новое, default `project_data_dir/code`) корень XML-выгрузки |
| `txt_root` | (новое, default `project_data_dir/metadata`) корень TXT-выгрузки описания конфигурации 1С; env `ONEC_TXT_ROOT` |

Поправить: BSL-поиск → `xml_root/**`; `txt_root` используется для поиска `ОтчетПоКонфигурации.txt`; `config_root` оставить для `.txt`-режима совместимости.

---

## 11. Порядок реализации

1. **Маппинги типов** — `EN_TYPE_MAP` + генератор каталогов (§5).
2. **`xml_manifest.py`** — парсер `ConfigDumpInfo.xml` → дерево с `source_key`/`config_version`.
3. **Рефактор `collect_graph`** → пообъектный `parse_object` + слияние слоёв (`merge`).
4. **`object_checksums`** + двухуровневый инкремент + запись после upsert.
5. **CLI `--object`/`--source`**.
6. **`xml_object.py`** — детали per-object XML + `FormType`.
7. **Легаси-флаг `is_legacy`** — глобальный (`Configuration.xml`) + по форме (`FormType`) → на узел (object/form/symbol) (§6).
8. **BSL-источник** → `code/**`.
9. **Заглушка `parse_legacy_object`** (TODO phase-2).
10. **Устойчивость** — обёртки слоёв, `incomplete`-маркер.
11. **Тесты** (`pytest`) на парсеры/маппинг/чек-суммы/легаси-флаг.

---

## 12. Риски / заметки

- **Стабильность `stable_id`:** не менять логику формирования id, иначе весь индекс пересоберётся. `source_key` добавляется в `props`, не в id.
- **`configVersion` из манифеста** — готовая пообъектная чек-сумма 1С; входит в `object_checksum` и покрывает метаданные без повторного хэширования XML.
- **`.txt` vs манифест:** набор объектов и русские синонимы — из `.txt`; `en`-имена/`id`/`configVersion` и отсутствующие детали — из `.xml`.

### Известное ограничение: типы объектов, отсутствующие в `.txt`

`.txt`-отчёт (`report_parser._TYPE_MAP`) покрывает не все типы конфигурации. Следующие типы есть в XML-манифесте (`EN_TYPE_MAP`), но **нет** в `.txt`, поэтому для них **не создаются object-узлы** в графе: `BusinessProcess`, `CommonModule`, `CommonForm`, `CommonTemplate`, `HTTPService`, `WebService`, `WSReference`, `XDTOPackage`, `SettingsStorage`, `SessionParameter`, `EventSubscription`, `ExternalDataSource`, `Interface`, `Style`, `StyleItem`, `CommonPicture`, `FilterCriterion`, `AccountingRegister` (частично) и др.

**Следствия:**
- BSL-модули и символы этих объектов индексируются (как `module`/`symbol` с `object_name`), но:
  - нет object-узла → `get_references`/`get_object_elements` по таким объектам возвращают «not found»;
  - нет `HAS_MODULE`-связи → объект не связывается со своими модулями.
- Пример: `get_symbol('ПриЗаписи')` работает (символы индексируются), а `get_references('БизнесПроцесс.ТестированиеФункционала')` — «Object not found», т.к. бизнес-процессов нет в `.txt`.

**Варианты устранения (не реализовано):** при слиянии слоёв добавлять отсутствующие в `.txt` объекты из манифеста как object-узлы (с `props.incomplete=True`). Требует решения по приоритету: `.txt` — истина, но манифест дополняет *отсутствующие* типы.
