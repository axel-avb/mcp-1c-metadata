"""
Parser for 1C Event Subscription XML files
"""

from __future__ import annotations
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
import xml.etree.ElementTree as ET
import logging

logger = logging.getLogger(__name__)


@dataclass
class EventSubscription:
    """Represents an Event Subscription from 1C XML metadata"""
    uuid: str
    name: str
    synonym: str
    comment: str
    source_objects: List[str]  # Источник - объекты метаданных
    event: str                # Событие
    handler: str              # Обработчик
    object_belonging: str = ""                              # "Adopted" или "" (собственный)
    modified_properties: List[str] = field(default_factory=list) # только для Adopted

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Neo4j loading"""
        result: Dict[str, Any] = {
            "uuid": self.uuid,
            "name": self.name,
            "synonym": self.synonym,
            "comment": self.comment,
            "Источник": self.source_objects,
            "Событие": self.event,
            "Обработчик": self.handler,
            "ПринадлежностьОбъекта": self.object_belonging,
        }
        if self.modified_properties:
            result["modified_properties"] = self.modified_properties
        return result


class EventSubscriptionParser:
    """Parser for 1C Event Subscription XML files"""

    def parse_directory(self, directory: Path) -> List[EventSubscription]:
        """Parse all Event Subscription XML files in directory"""
        subscriptions = []

        if not directory.exists():
            logger.warning("Event subscriptions directory not found: %s", directory)
            return subscriptions

        xml_files = list(directory.glob("*.xml"))
        logger.info("Found %d Event Subscription XML files in %s", len(xml_files), directory)

        for xml_file in xml_files:
            try:
                subscription = self.parse_file(xml_file)
                if subscription:
                    subscriptions.append(subscription)
                    logger.debug("Parsed subscription: %s from %s", subscription.name, xml_file.name)
            except Exception as e:
                logger.error("Error parsing %s: %s", xml_file, str(e))

        logger.info("Successfully parsed %d Event Subscriptions", len(subscriptions))
        return subscriptions

    def parse_file(self, file_path: Path) -> Optional[EventSubscription]:
        """Parse single Event Subscription XML file"""
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()

            # Найти EventSubscription элемент
            event_sub = root.find(".//{http://v8.1c.ru/8.3/MDClasses}EventSubscription")
            if event_sub is None:
                logger.warning("No EventSubscription element found in %s", file_path)
                return None

            # Извлечь Properties
            properties = event_sub.find(".//{http://v8.1c.ru/8.3/MDClasses}Properties")
            if properties is None:
                logger.warning("No Properties element found in %s", file_path)
                return None

            # Парсинг основных свойств
            name = self._get_text(properties.find(".//{http://v8.1c.ru/8.3/MDClasses}Name"))
            synonym_elem = properties.find(".//{http://v8.1c.ru/8.3/MDClasses}Synonym")
            synonym = self._get_synonym_text(synonym_elem) if synonym_elem is not None else ""
            comment = self._get_text(properties.find(".//{http://v8.1c.ru/8.3/MDClasses}Comment"))
            uuid = event_sub.get("uuid", "")

            if not name:
                logger.warning("No name found in EventSubscription: %s", file_path)
                return None

            # Парсинг источников (Source)
            source_objects = []
            source_elem = properties.find(".//{http://v8.1c.ru/8.3/MDClasses}Source")
            if source_elem is not None:
                # В EventSubscription типы указываются как v8:Type (http://v8.1c.ru/8.1/data/core)
                for type_elem in source_elem.findall(".//{http://v8.1c.ru/8.1/data/core}Type"):
                    type_text = type_elem.text
                    if type_text:
                        source_objects.append(type_text.strip())
                # Fallback: иногда встречается MDClasses:Type, обработаем и его
                if not source_objects:
                    for type_elem in source_elem.findall(".//{http://v8.1c.ru/8.3/MDClasses}Type"):
                        type_text = type_elem.text
                        if type_text:
                            source_objects.append(type_text.strip())

            # Парсинг события и обработчика
            event = self._get_text(properties.find(".//{http://v8.1c.ru/8.3/MDClasses}Event"))
            handler = self._get_text(properties.find(".//{http://v8.1c.ru/8.3/MDClasses}Handler"))

            # Парсинг принадлежности объекта (для расширений)
            object_belonging = self._get_text(
                properties.find(".//{http://v8.1c.ru/8.3/MDClasses}ObjectBelonging")
            )

            # Для заимствованных объектов определяем изменённые свойства
            modified_properties: List[str] = []
            if object_belonging == "Adopted":
                modified_properties = self._get_modified_properties(properties)

            return EventSubscription(
                uuid=uuid,
                name=name,
                synonym=synonym,
                comment=comment,
                source_objects=source_objects,
                event=event,
                handler=handler,
                object_belonging=object_belonging,
                modified_properties=modified_properties,
            )

        except Exception as e:
            logger.error("Error parsing event subscription file %s: %s", file_path, str(e))
            return None

    def _get_text(self, element) -> str:
        """Get text content from XML element"""
        return element.text.strip() if element is not None and element.text else ""

    # Теги свойств, которые могут быть изменены в заимствованной подписке
    _MODIFIABLE_PROPS = {
        "Synonym": "Синоним",
        "Comment": "Комментарий",
        "Source": "Источник",
        "Event": "Событие",
        "Handler": "Обработчик",
    }

    def _get_modified_properties(self, properties) -> List[str]:
        """
        Вычисляет список изменённых свойств для заимствованной EventSubscription.
        Непустые явно заданные теги (кроме служебных) считаются изменёнными.
        """
        ns = "{http://v8.1c.ru/8.3/MDClasses}"
        modified = []
        for tag_en, tag_ru in self._MODIFIABLE_PROPS.items():
            elem = properties.find(f"{ns}{tag_en}")
            if elem is None:
                continue
            # Непустой = есть текст или дочерние элементы
            has_content = bool(elem.text and elem.text.strip()) or len(list(elem)) > 0
            if has_content:
                modified.append(tag_ru)
        return modified

    def _get_synonym_text(self, synonym_elem) -> str:
        """Get synonym text from v8:item structure"""
        if synonym_elem is None:
            return ""
        # Предпочитаем RU локализацию, затем берем первый доступный элемент
        first_val = None
        try:
            # Поиск всех v8:item
            for it in synonym_elem.findall(".//{http://v8.1c.ru/8.1/data/core}item"):
                lang = it.find("{http://v8.1c.ru/8.1/data/core}lang")
                content = it.find("{http://v8.1c.ru/8.1/data/core}content")
                ltxt = lang.text.strip() if (lang is not None and lang.text) else ""
                ctxt = content.text.strip() if (content is not None and content.text) else ""
                if ltxt.lower() == "ru" and ctxt:
                    return ctxt
                if first_val is None and ctxt:
                    first_val = ctxt
        except Exception:
            pass
        return first_val or ""