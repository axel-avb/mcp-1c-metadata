from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Any, Optional
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


class RoleRightsParser:
    """
    Streaming parser for Roles/*/Ext/Rights.xml files.

    Produces rows suitable for Neo4j loader:
      {
        "role": str,
        "role_qn": str,
        "object": str,                 # short object name (e.g., "Сотрудники")
        "object_category": str,        # RU category (e.g., "РегистрыСведений")
        "object_qn": str,              # full qualified name in graph
        "object_full": str,            # verbatim Rights.xml object path, e.g., "InformationRegister.ТекущиеКадровыеДанныеСотрудников.TabularSection...."
        "right_ru": str,               # canonical Russian name
        "right_en": str,               # original English key from XML
        "allowed": bool,
        "condition": Optional[str]
      }
    """

    # Map EN right identifiers as they appear in Rights.xml -> canonical Russian strings
    RIGHT_NAME_MAP: Dict[str, str] = {
        # Base CRUD/Post
        "Read": "Чтение",
        "Insert": "Добавление",
        "Add": "Добавление",
        "Create": "Добавление",
        "Write": "Изменение",
        "Edit": "Изменение",
        "Update": "Изменение",
        "Delete": "Удаление",
        "Posting": "Проведение",
        "UndoPosting": "Отмена проведения",
        "View": "Просмотр",
        "Use": "Использование",
        "InputByString": "Ввод по строке",
        "Start": "Страт",

        # Interactive ops
        "InteractiveInsert": "Интерактивное добавление",
        "InteractiveEdit": "Интерактивное изменение",
        "InteractiveDelete": "Интерактивное удаление",
        "InteractiveSetDeletionMark": "Интерактивная пометка на удаление",
        "InteractiveClearDeletionMark": "Интерактивное снятие пометки на удаление",
        "InteractiveDeleteMarked": "Интерактивное удаление помеченных",
        "InteractiveDeletePredefined": "Интерактивное удаление предопределенных",
        "InteractiveSetDeletionMarkPredefinedData": "Интерактивная пометка на удаление предопределенных",
        "InteractiveClearDeletionMarkPredefinedData": "Интерактивное снятие пометки удаления предопределенных",
        "InteractiveDeleteMarkedPredefinedData": "Интерактивное удаление помеченных предопределенных",
        "InteractiveDeletePredefinedData": "Интерактивное удаление предопределенных",
        "InteractivePosting": "Интерактивное проведение",
        "InteractivePostingRegular": "Интерактивное проведение неоперативное",
        "InteractiveUndoPosting": "Интерактивная отмена проведения",
        "InteractiveChangeOfPosted": "Интерактивное изменение проведенных",
        "InteractiveStart": "Интерактивный страт",
        "InteractiveActivation": "Интерактивная активация",

        # Totals / Data history
        "TotalsControl": "Управление итогами",        

        "ReadDataHistory": "Чтение истории данных",
        "ReadDataHistoryOfMissingData": "Чтение истории данных отсутствующих данных",
        "UpdateDataHistory": "Изменение истории данных",
        "UpdateDataHistoryOfMissingData": "Изменение истории данных отсутствующих данных",
        "UpdateDataHistorySettings": "Изменение настроек истории данных",
        "UpdateDataHistoryVersionComment": "Изменение комментария версии истории данных",
        "ViewDataHistory": "Просмотр истории данных",
        "EditDataHistoryVersionComment": "Редактирование комментария версии истории данных",
        "SwitchToDataHistoryVersion": "Переход на версию истории данных",

        # Main window modes / client capabilities
        "MainWindowModeWorkplace": "Режим основного окна 'Рабочее место'",
        "MainWindowModeFullscreenWorkplace": "Режим основного окна 'Полноэкранное рабочее место'",
        "MainWindowModeEmbeddedWorkplace": "Режим основного окна 'Встроенное рабочее место'",
        "AnalyticsSystemClient": "Клиент системы аналитики",
        "MainWindowModeKiosk": "Режим основного окна 'Киоск'",
        "MainWindowModeNormal": "Режим основного окна 'Обычный'",

        # Additional rights found in logs
        "Execute": "Выполнение",
        "Administration": "Администрирование",
        "UpdateDataBaseConfiguration": "Обновление конфигурации базы данных",
        "ThickClient": "Толстый клиент",
        "ExternalConnection": "Внешнее соединение",
        "Automation": "Автоматизация",
        "TechnicalSpecialistMode": "Режим технического специалиста",
        "ConfigurationExtensionsAdministration": "Администрирование расширений конфигурации",
        "InteractiveOpenExtDataProcessors": "Интерактивное открытие внешних обработок",
        "InteractiveOpenExtReports": "Интерактивное открытие внешних отчетов",
        "SaveUserData": "Сохранение пользовательских данных",
        "Output": "Вывод",
        "Get": "Получение",
        "Set": "Установка",
        "WebClient": "Веб-клиент",
        "MobileClient": "Мобильный клиент",
        "ThinClient": "Тонкий клиент",
        "InteractiveActivate": "Интерактивная активация",
        "InteractiveExecute": "Интерактивное выполнение",
        "DataAdministration": "Администрирование данных",
        "ExclusiveMode": "Монопольный режим",
        "ActiveUsers": "Активные пользователи",
        "EventLog": "Журнал регистрации",
        "CollaborationSystemInfoBaseRegistration": "Регистрация информационной базы в системе взаимодействия",
    }

    # Map EN object type prefix -> RU metadata category used in the graph
    CATEGORY_MAP: Dict[str, str] = {
        "Catalog": "Справочники",
        "Document": "Документы",
        "InformationRegister": "РегистрыСведений",
        "AccumulationRegister": "РегистрыНакопления",
        "CalculationRegister": "РегистрыРасчета",
        "BusinessProcess": "БизнесПроцессы",
        "Task": "Задачи",
        "Enumeration": "Перечисления",
        "CommonForm": "ОбщиеФормы",
        "DataProcessor": "Обработки",
        "Report": "Отчеты",
        "ChartOfAccounts": "ПланыСчетов",
        "ChartOfCharacteristicTypes": "ПланыВидовХарактеристик",
        "Subsystem": "Подсистемы",
        "Constant": "Константы",
        "CommonCommand": "ОбщиеКоманды",
        "HTTPService": "HTTPСервисы",
        "WebService": "WebСервисы",
        "IntegrationService": "СервисыИнтеграции",
        "ExchangePlan": "ПланыОбмена",
        "SessionParameter": "ПараметрыСеанса",
        "DocumentJournal": "ЖурналыДокументов",
        "AccountingRegister": "РегистрыБухгалтерии",
        "FilterCriterion": "КритерииОтбора",
        "Sequence": "Последовательности",
        "ChartOfCalculationTypes": "ПланыВидовРасчета",
        "CommonAttribute": "ОбщиеРеквизиты",
        "CommonModule": "ОбщиеМодули",
        "CommonPicture": "ОбщиеКартинки",
        "CommonTemplate": "ОбщиеМакеты",
        "FunctionalOption": "ФункциональныеОпции",
        "FunctionalOptionsParameter": "ПараметрыФункциональныхОпций",
        "DefinedType": "ОпределяемыеТипы",
        "SettingsStorage": "ХранилищаНастроек",
        "Role": "Роли",
        "WSReference": "WSСсылки",
        "XDTOPackage": "ПакетыXDTO",
        "Bot": "Боты",
        "ExternalDataSource": "ВнешниеИсточникиДанных",
        "Interface": "Интерфейсы",
        "CommandGroup": "ГруппыКоманд",
        "Style": "Стили",
        "Language": "Языки",
        "ScheduledJob": "РегламентныеЗадания",
        "EventSubscription": "ПодпискиНаСобытия",
        "DocumentNumerator": "Нумераторы",
    }

    def __init__(self):
        pass

    def parse_all(self, code_root: Path, project_name: str, config_name: str) -> List[Dict[str, Any]]:
        """
        Scan code_root for Roles/*/Ext/Rights.xml and parse all into rows.

        Kept for backwards compatibility. New callers should build a file list
        via CodeFileIndexer and use parse_files() to avoid a second os.walk.
        """
        if not code_root or not code_root.exists():
            logger.warning("Code directory not found for Role rights parsing: %s", code_root)
            return []
        files = list((code_root / "Roles").glob("*/Ext/Rights.xml"))
        return self.parse_files(files, project_name, config_name)

    def parse_files(
        self,
        files,
        project_name: str,
        config_name: str,
    ) -> List[Dict[str, Any]]:
        """Parse a ready list of Roles/*/Ext/Rights.xml paths into rows.

        Used by the new pipeline where CodeFileIndexer already collected
        the file list during the single code walk.
        """
        rows: List[Dict[str, Any]] = []
        for xml_path in files:
            xml_path = Path(xml_path)
            try:
                role_dir = xml_path.parent.parent  # .../Roles/<RoleName>
                role_name = role_dir.name
                parsed = self._parse_file(xml_path, role_name, project_name, config_name)
                if parsed:
                    rows.extend(parsed)
            except Exception as e:
                logger.error("Failed to parse Rights.xml %s: %s", str(xml_path), str(e))

        logger.info("Parsed Role rights rows: %d", len(rows))
        return rows

    @staticmethod
    def _strip_ns(tag: str) -> str:
        if not tag:
            return tag
        if "}" in tag:
            return tag.split("}", 1)[1]
        return tag

    def _map_right_ru(self, right_en: str) -> str:
        ru = self.RIGHT_NAME_MAP.get(right_en)
        if ru:
            return ru
        # Fallback: log and keep original EN to avoid data loss
        logger.debug("Unmapped right name encountered: %s", right_en)
        return right_en

    def _map_object_to_category(self, object_full: str) -> Optional[str]:
        """
        From a full object path like:
          "Catalog.Сотрудники" or "InformationRegister.ТекущиеКадровыеДанныеСотрудников.TabularSection...."
        take the first token as type, map to RU category.
        """
        if not object_full or "." not in object_full:
            return None
        head = object_full.split(".", 1)[0]
        return self.CATEGORY_MAP.get(head)

    def _short_object_name(self, object_full: str) -> Optional[str]:
        """
        Extract the base object name (second segment) from object_full.
        E.g., "Catalog.Сотрудники.TabularSection...." -> "Сотрудники"
        """
        if not object_full or "." not in object_full:
            return None
        parts = object_full.split(".")
        return parts[1] if len(parts) >= 2 else None

    def _parse_file(self, xml_path: Path, role_name: str, project_name: str, config_name: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        # Parse header flags once: setForNewObjects, setForAttributesByDefault, independentRightsOfChildObjects
        try:
            tree = ET.parse(str(xml_path))
            root = tree.getroot()
            ns = "{http://v8.1c.ru/8.2/roles}"
            def _t(tag: str) -> str:
                try:
                    val = root.findtext(f"{ns}{tag}") or ""
                    return val
                except Exception:
                    return ""
            def _b(val: str) -> bool:
                v = (val or "").strip().lower()
                return v in ("true","1")
            set_new = _b(_t("setForNewObjects"))
            set_attr_default = _b(_t("setForAttributesByDefault"))
            indep_children = _b(_t("independentRightsOfChildObjects"))
        except Exception:
            # Safe defaults
            set_new = False
            set_attr_default = False
            indep_children = True

        # iterparse with end events over 'object' keeps memory low
        # The XML has default namespace (http://v8.1c.ru/8.2/roles), so strip namespaces while parsing.
        for event, elem in ET.iterparse(str(xml_path), events=("end",)):
            tag = self._strip_ns(elem.tag)
            if tag != "object":
                continue

            # Extract & normalize object block
            object_full: Optional[str] = None
            rights_local: List[Dict[str, Any]] = []

            for child in list(elem):
                ctag = self._strip_ns(child.tag)
                if ctag == "name":
                    object_full = (child.text or "").strip()
                elif ctag == "right":
                    right_en: Optional[str] = None
                    allowed: Optional[bool] = None
                    condition: Optional[str] = None

                    for rch in list(child):
                        rtag = self._strip_ns(rch.tag)
                        if rtag == "name":
                            right_en = (rch.text or "").strip()
                        elif rtag == "value":
                            val = (rch.text or "").strip().lower()
                            if val in ("true", "1"):
                                allowed = True
                            elif val in ("false", "0"):
                                allowed = False
                            else:
                                allowed = None
                        elif rtag == "restrictionByCondition":
                            # nested <condition>
                            for cc in list(rch):
                                if self._strip_ns(cc.tag) == "condition":
                                    condition = cc.text or None

                    if right_en is not None and allowed is not None:
                        rights_local.append({
                            "right_en": right_en,
                            "allowed": allowed,
                            "condition": condition
                        })

            # Build rows for this object
            if object_full and rights_local:
                head = object_full.split(".", 1)[0] if object_full else ""
                if head == "Configuration":
                    # Rights to the Configuration itself
                    object_qn = f"{project_name}/{config_name}"
                    role_qn = f"{project_name}/{config_name}/Роли/{role_name}"
                    for it in rights_local:
                        right_en = it["right_en"]
                        rows.append({
                            "role": role_name,
                            "role_qn": role_qn,
                            "object": "",
                            "object_category": "",
                            "object_qn": object_qn,
                            "object_full": object_full,
                            "right_ru": self._map_right_ru(right_en),
                            "right_en": right_en,
                            "allowed": it["allowed"],
                            "condition": it["condition"],
                            "setForNewObjects": set_new,
                            "setForAttributesByDefault": set_attr_default,
                            "independentRightsOfChildObjects": indep_children,
                        })
                else:
                    cat_ru = self._map_object_to_category(object_full)
                    obj_name = self._short_object_name(object_full)

                    if not cat_ru or not obj_name:
                        # Unsupported target — skip gracefully
                        logger.debug("Skip rights for unsupported object path: %s (file=%s)", object_full, xml_path)
                    else:
                        object_qn = f"{project_name}/{config_name}/{cat_ru}/{obj_name}"
                        role_qn = f"{project_name}/{config_name}/Роли/{role_name}"
                        for it in rights_local:
                            right_en = it["right_en"]
                            rows.append({
                                "role": role_name,
                                "role_qn": role_qn,
                                "object": obj_name,
                                "object_category": cat_ru,
                                "object_qn": object_qn,
                                "object_full": object_full,
                                "right_ru": self._map_right_ru(right_en),
                                "right_en": right_en,
                                "allowed": it["allowed"],
                                "condition": it["condition"],
                                "setForNewObjects": set_new,
                                "setForAttributesByDefault": set_attr_default,
                                "independentRightsOfChildObjects": indep_children,
                            })

            # Free element to save memory during streaming
            elem.clear()

        return rows