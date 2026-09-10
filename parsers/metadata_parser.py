"""
Parser module for metadata text files of 1C configuration
"""

from __future__ import annotations
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
import logging
from config import settings

logger = logging.getLogger(__name__)

# Keys whose values are typically long multiline text and should be joined
MULTILINE_TEXT_KEYS = {"Подсказка", "Комментарий", "Описание"}

# Keys whose values represent types and should be normalized to lists
TYPE_KEYS = {"Тип", "Type", "type", "ValueType", "ТипЗначения"}


@dataclass(slots=True)
class Attribute:
    """Represents an attribute (Реквизит, Ресурс, Измерение, ПризнакиУчета, ПризнакиУчетаСубконто) of a metadata object or tabular part"""
    name: str
    properties: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert attribute to dictionary, excluding the name"""
        return self.properties

@dataclass(slots=True)
class TabularPart:
    """Represents a tabular part (Табличную часть) within a metadata object"""
    name: str
    attributes: List[Attribute] = field(default_factory=list)
    properties: Dict[str, Any] = field(default_factory=dict)
    
    def add_attribute(self, attribute: Attribute):
        """Add an attribute to the tabular part"""
        self.attributes.append(attribute)

    def to_dict(self) -> Dict[str, Any]:
        """Convert tabular part properties to a dictionary."""
        return self.properties

@dataclass(slots=True)
class Form:
    """Represents a managed form (Форма) of a metadata object"""
    name: str
    properties: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert form properties to a dictionary."""
        return self.properties

@dataclass(slots=True)
class Command:
    """Represents a command (Команда) of a metadata object"""
    name: str
    properties: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert command properties to a dictionary."""
        return self.properties

@dataclass(slots=True)
class Layout:
    """Represents a layout (Макет) of a metadata object"""
    name: str
    properties: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert layout properties to a dictionary."""
        return self.properties

@dataclass(slots=True)
class EnumValue:
    """Represents a value of an enumeration (Перечисления)"""
    name: str
    properties: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert enum value to dictionary, excluding the name"""
        return self.properties


@dataclass(slots=True)
class MetadataObject:
    """Represents a metadata object (Document, Catalog, Report, Form, Layout etc.)"""
    name: str
    attributes: List[Attribute] = field(default_factory=list)
    tabular_parts: List[TabularPart] = field(default_factory=list)
    forms: List[Form] = field(default_factory=list)
    commands: List[Command] = field(default_factory=list)
    layouts: List[Layout] = field(default_factory=list)
    enum_values: List[EnumValue] = field(default_factory=list)
    # Accounting flags (ПризнакиУчета) at the object level
    account_flags: List[Attribute] = field(default_factory=list)
    # Subconto accounting flags (ПризнакиУчетаСубконто)
    subconto_flags: List[Attribute] = field(default_factory=list)
    # Semantic model additions for 1C Registers: Resources and Dimensions
    resources: List[Attribute] = field(default_factory=list)
    dimensions: List[Attribute] = field(default_factory=list)
    # Characteristic schemes metadata (parsed from "Характеристики" blocks)
    characteristic_schemes: List[Dict[str, Any]] = field(default_factory=list)
    properties: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert metadata object properties to a dictionary."""
        return self.properties
    
    def add_attribute(self, attribute: Attribute):
        """Add an attribute (Реквизит) to the metadata object"""
        self.attributes.append(attribute)
    
    def add_tabular_part(self, tabular_part: TabularPart):
        """Add a tabular part (Табличная часть) to the metadata object"""
        self.tabular_parts.append(tabular_part)

    def add_form(self, form: Form):
        """Add a Form (Форма) to the metadata object"""
        self.forms.append(form)

    def add_command(self, command: Command):
        """Add a Command (Команда) to the metadata object"""
        self.commands.append(command)

    def add_layout(self, layout: Layout):
        """Add a Layout (Макет) to the metadata object"""
        self.layouts.append(layout)

    def add_enum_value(self, enum_value: EnumValue):
        """Add an enum value (ЗначенияПеречисления) to the metadata object"""
        self.enum_values.append(enum_value)

    def add_resource(self, resource: Attribute):
        """Add a Resource (Ресурсы) to the metadata object"""
        self.resources.append(resource)

    def add_dimension(self, dimension: Attribute):
        """Add a Dimension (Измерения) to the metadata object"""
        self.dimensions.append(dimension)


@dataclass(slots=True)
class MetadataCategory:
    """Represents a category of metadata objects, like 'Справочники','Документы'."""
    name: str
    metadata_objects: List[MetadataObject] = field(default_factory=list)

    def add_metadata_object(self, obj: MetadataObject):
        """Add a metadata object to this category."""
        self.metadata_objects.append(obj)

    def to_dict(self) -> Dict[str, Any]:
        """Return a lightweight dict with contained object names (no 'name' field)."""
        return {"metadata_objects": [obj.name for obj in self.metadata_objects]}


@dataclass(slots=True)
class Configuration:
    """Represents a 1C configuration"""
    name: str
    file_path: Path
    properties: Dict[str, Any] = field(default_factory=dict)
    categories: List[MetadataCategory] = field(default_factory=list)
    
    def add_category(self, category: MetadataCategory):
        """Add a metadata category to the configuration"""
        self.categories.append(category)

    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration properties to a dictionary."""
        return self.properties


class MetadataParser:
    """Parser for 1C metadata text files"""

    @staticmethod
    def _normalize_type_to_list(value: Any) -> Optional[List[str]]:
        """
        Normalize type value to a list of type strings.

        Handles:
        - None -> None
        - Empty string -> None
        - List -> cleaned list (strip, filter empty)
        - String with commas (possibly multi-line) -> normalize whitespace, split by comma
        - String with newlines only -> split by newline, strip, filter empty
        - Simple string -> single-element list

        Returns:
            List of type strings or None if empty
        """
        if value is None:
            return None

        # If already a list, clean it up
        if isinstance(value, list):
            cleaned = [str(item).strip().rstrip(",").strip() for item in value if item]
            cleaned = [item for item in cleaned if item]
            return cleaned if cleaned else None

        # Convert to string and handle
        value_str = str(value).strip()
        if not value_str:
            return None

        # Check if contains comma - split by comma
        # First normalize whitespace (replace newlines/tabs with spaces) to handle multi-line comma-separated values
        if ',' in value_str:
            # Replace all newlines and tabs with spaces, then collapse multiple spaces
            normalized = ' '.join(value_str.split())
            types = [t.strip().rstrip(",").strip() for t in normalized.split(',')]
            types = [t for t in types if t]
            return types if types else None

        # Check if contains newline (but no comma) - split by newline
        if '\n' in value_str:
            types = [t.strip() for t in value_str.split('\n')]
            types = [t for t in types if t]
            return types if types else None

        # Single type
        return [value_str]

    def parse_directory(self, directory: Path) -> List[Configuration]:
        """Parse all text files in a directory"""
        configurations = []
        
        # Only .txt metadata files are supported
        patterns = ['*.txt']
        files = []
        
        for pattern in patterns:
            files.extend(directory.glob(pattern))
        
        if not files:
            logger.warning("No metadata files found in %s", directory)
            return configurations
        
        for file_path in files:
            try:
                logger.info("Parsing file: %s", file_path)
                config = self.parse_file(file_path)
                configurations.append(config)
                logger.info("Successfully parsed configuration: %s", config.name)
            except Exception as e:
                logger.error("Error parsing file %s: %s", file_path, str(e))
        
        return configurations

    def parse_file(self, file_path: Path) -> Configuration:
        """Parse a single 1C metadata text file"""
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        raw = file_path.read_bytes()
        if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
            encodings_to_try = ['utf-16']
        elif raw.startswith(b'\xef\xbb\xbf'):
            encodings_to_try = ['utf-8-sig']
        elif raw.count(b'\x00') > max(8, len(raw) // 20):
            encodings_to_try = ['utf-16-le', 'utf-16-be', 'utf-8-sig', 'utf-8', 'cp1251']
        else:
            encodings_to_try = ['utf-8-sig', 'utf-8', 'cp1251']

        last_err: Optional[Exception] = None
        lines: List[str] = []
        for enc in encodings_to_try:
            try:
                text = raw.decode(enc)
                candidate_lines = text.splitlines()
                header_candidates = [line.strip() for line in candidate_lines[:20] if line.strip()]
                if header_candidates and not any(
                    line.startswith('- Конфигурации.') or line.startswith('- Configuration')
                    for line in header_candidates
                ):
                    raise UnicodeError(f"decoded with {enc}, but metadata header was not found")
                lines = candidate_lines
                last_err = None
                break
            except UnicodeError as e:
                last_err = e
                continue
        if last_err is not None:
            raise last_err

        # Trim trailing whitespace/newlines (BOM handled by chosen encodings)
        lines = [line.rstrip() for line in lines]
        
        # Build node tree
        root = {'children': [], 'level': -1}
        path = [root]
        
        for line in lines:
            if not line.strip():
                continue

            # Compute indentation level accepting tabs and groups of 4 spaces
            i = 0
            level = 0
            while i < len(line):
                if line.startswith('\t', i):
                    level += 1
                    i += 1
                elif line.startswith('    ', i):
                    level += 1
                    i += 4
                else:
                    break
            line = line[i:]
            
            node_data = line.strip()
            node = {'data': node_data, 'children': [], 'level': level}
            
            while path[-1]['level'] >= level:
                path.pop()
            
            path[-1]['children'].append(node)
            path.append(node)

        return self._build_configuration_from_tree(root['children'], file_path)
        
    def _parse_key_value(self, data_string: str) -> Tuple[Optional[str], Optional[str]]:
        """Parse a key-value pair from a string."""
        parts = data_string.split(':', 1)
        if len(parts) == 2:
            key = parts[0].strip()
            value = parts[1].strip().strip('"')
            return key, value
        return None, None

    def _assemble_value(self, key: str, inline_value: Optional[str], children: List[Dict]) -> Any:
        """
        Assemble a property's value from its inline value and child nodes,
        preserving the existing has_kv heuristic.
        - If children contain no "key: value" items (per current has_kv check) and either
          inline_value is present or key is a known multiline text key, join inline and
          children's texts by newline.
        - Otherwise, aggregate children into list or single value; if empty, fallback to inline.
        """
        if not children:
            return inline_value

        # Keep the existing has_kv heuristic unchanged
        has_kv = any((':' in ch['data']) and ('"' in ch['data'].split(':', 1)[1]) for ch in children)

        # Collect non-empty child texts (trim quotes)
        nested_values: List[str] = []
        for ch in children:
            child_data = ch['data'].strip().strip('"')
            if child_data:
                nested_values.append(child_data)

        if not has_kv and (inline_value or key in MULTILINE_TEXT_KEYS):
            base = [inline_value] if inline_value else []
            return "\n".join(base + nested_values)
        else:
            if nested_values:
                return nested_values if len(nested_values) > 1 else nested_values[0]
            else:
                return inline_value

    def _fill_properties(self, children: List[Dict], target: Dict[str, Any]) -> None:
        """
        Fill target dictionary with properties parsed from child nodes.
        - Skips sub-item nodes whose data starts with '-'
        - Uses _parse_key_value for each child to get (key, inline_value)
        - If child has nested children, aggregates via _assemble_value; otherwise uses inline value
        - Normalizes type properties (Тип, Type, etc.) to lists
        """
        for child in (children or []):
            data = child.get('data', '')
            if isinstance(data, str) and data.startswith('-'):
                # Ignore sub-items; this helper only handles plain properties
                continue
            key, inline_value = self._parse_key_value(data)
            if key:
                if child.get('children'):
                    value = self._assemble_value(key, inline_value, child['children'])
                else:
                    value = inline_value

                # Normalize type properties to lists
                if key in TYPE_KEYS:
                    value = self._normalize_type_to_list(value)

                target[key] = value

    def _build_configuration_from_tree(self, tree: List[Dict], file_path: Path) -> Configuration:
        """Build a Configuration object from the parsed node tree."""
        if not tree:
            return Configuration(name="Unknown", file_path=file_path)
            
        config_node = tree[0]
        config_name = config_node['data'].split('.')[-1].strip()
        configuration = Configuration(name=config_name, file_path=file_path)

        # Parse configuration properties and metadata objects
        for child_node in config_node['children']:
            if child_node['data'].startswith('-'):
                # This is a metadata object (e.g., "- Справочники.Номенклатура")
                self._parse_metadata_object(child_node, configuration)
            else:
                # This is a configuration property
                key, value = self._parse_key_value(child_node['data'])
                if key:
                    if child_node['children']:
                        # Handle nested properties with multiline support:
                        # - If children have no "key: value" lines and there is an inline value,
                        #   treat children as continuation lines and join with "\n".
                        # - If inline value is empty but key is a known multiline text key
                        #   (e.g., "Подсказка"), also join children into a single text.
                        # - Otherwise, keep original list/single aggregation behavior.
                        value = self._assemble_value(key, value, child_node['children'])
                    configuration.properties[key] = value
        
        return configuration

    def _ensure_owner_attribute_enriched(self, parent_obj: MetadataObject) -> None:
        owners_val = parent_obj.properties.get("Владельцы")
        usage_val = parent_obj.properties.get("ИспользованиеПодчинения")
        owner_attr = next((a for a in parent_obj.attributes if a.name == "Владелец"), None)

        if owner_attr is not None:
            if owners_val is not None:
                # Normalize to list using the unified method
                normalized = self._normalize_type_to_list(owners_val)
                if normalized is not None and owner_attr.properties.get("Тип") != normalized:
                    owner_attr.properties["Тип"] = normalized
            if usage_val is not None and owner_attr.properties.get("ИспользованиеПодчинения") != usage_val:
                owner_attr.properties["ИспользованиеПодчинения"] = usage_val
            if owner_attr.properties.get("Стандартный") is not True:
                owner_attr.properties["Стандартный"] = True
        else:
            if owners_val is not None:
                owner_attr = Attribute(name="Владелец")
                # Normalize to list using the unified method
                normalized = self._normalize_type_to_list(owners_val)
                if normalized is not None:
                    owner_attr.properties["Тип"] = normalized
                if usage_val is not None:
                    owner_attr.properties["ИспользованиеПодчинения"] = usage_val
                owner_attr.properties["Стандартный"] = True
                parent_obj.add_attribute(owner_attr)

    def _parse_metadata_object(self, node: Dict, configuration: Configuration):
        """Parse a metadata object from a node starting with '-'."""
        # Remove the '-' prefix and parse the full name
        item_full_name = node['data'][1:].strip()
        item_parts = item_full_name.split('.', 1)
        
        if len(item_parts) != 2:
            return

        category_name, item_name = item_parts
        
        # Find or create category
        category = next((c for c in configuration.categories if c.name == category_name), None)
        if not category:
            category = MetadataCategory(name=category_name)
            configuration.add_category(category)
        
        # Create metadata object (normalize nested subsystem names and record hierarchy)
        if category_name == "Подсистемы":
            parts = item_full_name.split('.')
            chain = []
            i = 0
            while i < len(parts):
                if parts[i] == "Подсистемы" and i + 1 < len(parts):
                    chain.append(parts[i + 1])
                    i += 2
                else:
                    i += 1
            if chain:
                item_name = chain[-1]
            metadata_obj = MetadataObject(name=item_name)
            # Store path and immediate parent to be used by loader for hierarchy edges
            if chain:
                metadata_obj.properties["ПутьПодсистемы"] = chain
                if len(chain) > 1:
                    metadata_obj.properties["РодительскаяПодсистема"] = chain[-2]
        else:
            metadata_obj = MetadataObject(name=item_name)
        category.add_metadata_object(metadata_obj)
        
        # Parse children of the metadata object
        for child_node in node['children']:
            if child_node['data'].startswith('-'):
                tail = child_node['data'][1:].lstrip()
                if tail.startswith('Подсистемы.'):
                    # Nested subsystem: recurse as a separate metadata object
                    self._parse_metadata_object(child_node, configuration)
                    continue
                # This is a sub-item (attribute or tabular part)
                self._parse_sub_item(child_node, metadata_obj)
            else:
                # This is a property of the metadata object
                key, value = self._parse_key_value(child_node['data'])
                # Handle section headers without colon: "Характеристики" or "СтандартныеРеквизиты"
                raw_data = child_node['data'].strip()
                if raw_data == "Характеристики":
                    self._parse_characteristics(child_node, metadata_obj)
                    continue
                elif raw_data == "СтандартныеРеквизиты":
                    self._parse_standard_attributes(child_node, metadata_obj)
                    continue
                if key:
                    # Special handling for Characteristic Schemes ("Характеристики") when written with colon (rare)
                    if key == "Характеристики":
                        self._parse_characteristics(child_node, metadata_obj)
                        continue
                    # Handle "СтандартныеРеквизиты" when written with colon
                    if key == "СтандартныеРеквизиты":
                        self._parse_standard_attributes(child_node, metadata_obj)
                        continue

                    if child_node['children']:
                        value = self._assemble_value(key, value, child_node['children'])
                    if key in TYPE_KEYS:
                        value = self._normalize_type_to_list(value)
                    metadata_obj.properties[key] = value
                else:
                    # Bare header without colon that is not recognized
                    # Truncate raw_data to avoid huge debug logs
                    truncated_data = raw_data[:settings.debug_log_max_data_length] + "..." if len(raw_data) > settings.debug_log_max_data_length else raw_data
                    logger.debug(
                        "Unknown bare header without colon: %s (category=%s, object=%s, file=%s)",
                        truncated_data, category_name, metadata_obj.name, str(configuration.file_path)
                    )


        # Ensure/Enrich Owner attribute once after parsing all children
        self._ensure_owner_attribute_enriched(metadata_obj)

    def _parse_sub_item(self, node: Dict, parent_obj: MetadataObject):
        """Parse a sub-item that starts with '-'."""
        # Remove the '-' prefix
        item_full_name = node['data'][1:].strip()
        item_parts = item_full_name.split('.')
        
        if len(item_parts) < 2:
            return
        
        # Check if this is an attribute, resource, dimension, or tabular part
        if "Реквизиты" in item_parts:
            # This is an attribute
            # The name is the last part after "Реквизиты"
            req_index = item_parts.index("Реквизиты")
            if req_index < len(item_parts) - 1:
                attr_name = item_parts[-1]
                new_attr = Attribute(name=attr_name)
                parent_obj.add_attribute(new_attr)
                
                # Parse attribute properties
                for prop_node in node['children']:
                    self._parse_attribute_properties(prop_node, new_attr)

        elif "Ресурсы" in item_parts:
            # This is a Resource of a register
            res_index = item_parts.index("Ресурсы")
            if res_index < len(item_parts) - 1:
                res_name = item_parts[-1]
                new_res = Attribute(name=res_name)
                parent_obj.add_resource(new_res)

                # Parse resource properties
                for prop_node in node['children']:
                    self._parse_attribute_properties(prop_node, new_res)

        elif "Измерения" in item_parts:
            # This is a Dimension of a register
            dim_index = item_parts.index("Измерения")
            if dim_index < len(item_parts) - 1:
                dim_name = item_parts[-1]
                new_dim = Attribute(name=dim_name)
                parent_obj.add_dimension(new_dim)

                # Parse dimension properties
                for prop_node in node['children']:
                    self._parse_attribute_properties(prop_node, new_dim)
                    
        elif "РеквизитыАдресации" in item_parts:
            # This is an addressing requisite (Реквизит адресации)
            addr_index = item_parts.index("РеквизитыАдресации")
            if addr_index < len(item_parts) - 1:
                attr_name = item_parts[-1]
                new_attr = Attribute(name=attr_name)
                # Mark it explicitly as an addressing requisite (new property name allowed)
                new_attr.properties["ЭтоРеквизитАдресации"] = True
                parent_obj.add_attribute(new_attr)

                # Parse addressing attribute properties (keep original property names from source)
                for prop_node in node['children']:
                    self._parse_attribute_properties(prop_node, new_attr)
        elif "ПризнакиУчетаСубконто" in item_parts:
            # This is a Subconto Accounting Flag (ПризнакиУчетаСубконто)
            sc_idx = item_parts.index("ПризнакиУчетаСубконто")
            if sc_idx < len(item_parts) - 1:
                flag_name = item_parts[-1]
                new_flag = Attribute(name=flag_name)
                for prop_node in node['children']:
                    self._parse_attribute_properties(prop_node, new_flag)
                # Append to parent object's subconto flags
                parent_obj.subconto_flags.append(new_flag)
        elif "ПризнакиУчета" in item_parts:
            # This is an Accounting Flag (ПризнакиУчета)
            af_idx = item_parts.index("ПризнакиУчета")
            if af_idx < len(item_parts) - 1:
                flag_name = item_parts[-1]
                new_flag = Attribute(name=flag_name)
                for prop_node in node['children']:
                    self._parse_attribute_properties(prop_node, new_flag)
                # Append to parent object's account flags
                parent_obj.account_flags.append(new_flag)
        elif "ТабличныеЧасти" in item_parts:
            # This is a tabular part
            tab_index = item_parts.index("ТабличныеЧасти")
            if tab_index < len(item_parts) - 1:
                # Check if this is the tabular part itself or an attribute within it
                if "Реквизиты" not in item_parts[tab_index:]:
                    # This is the tabular part itself
                    tab_name = item_parts[tab_index + 1]
                    new_tabular = TabularPart(name=tab_name)
                    parent_obj.add_tabular_part(new_tabular)
                    
                    # Parse tabular part children
                    for child_node in node['children']:
                        if child_node['data'].startswith('-'):
                            # This is an attribute of the tabular part
                            self._parse_tabular_attribute(child_node, new_tabular)
                        else:
                            # This is a property of the tabular part
                            self._fill_properties([child_node], new_tabular.properties)

        elif "Команды" in item_parts:
            # This is a command
            cmd_index = item_parts.index("Команды")
            if cmd_index < len(item_parts) - 1:
                # Only treat the command node itself (exclude any nested attributes if they appear)
                if "Реквизиты" not in item_parts[cmd_index:]:
                    cmd_name = item_parts[cmd_index + 1]
                    new_cmd = Command(name=cmd_name)
                    parent_obj.add_command(new_cmd)

                    # Parse command properties (support nested/multiline values)
                    self._fill_properties(node.get('children', []), new_cmd.properties)

        elif "Макеты" in item_parts:
            # This is a layout (Макет)
            lay_index = item_parts.index("Макеты")
            if lay_index < len(item_parts) - 1:
                # Only treat the layout node itself (exclude any nested attributes if they appear)
                if "Реквизиты" not in item_parts[lay_index:]:
                    lay_name = item_parts[lay_index + 1]
                    new_layout = Layout(name=lay_name)
                    parent_obj.add_layout(new_layout)

                    # Parse layout properties (support nested/multiline values)
                    self._fill_properties(node.get('children', []), new_layout.properties)

        elif "Графы" in item_parts:
            graph_idx = item_parts.index("Графы")
            # Treat only the graph node itself (exclude any nested attributes if they appear)
            if graph_idx < len(item_parts) - 1 and "Реквизиты" not in item_parts[graph_idx:]:
                graph_name = item_parts[graph_idx + 1]
                graph_dict: Dict[str, Any] = {"Имя": graph_name}
                # Parse graph properties (support nested/multiline values)
                self._fill_properties(node.get('children', []), graph_dict)
                graphs_list = parent_obj.properties.get("ГрафыЖурнала")
                if not isinstance(graphs_list, list):
                    graphs_list = []
                    parent_obj.properties["ГрафыЖурнала"] = graphs_list
                graphs_list.append(graph_dict)

        elif "ШаблоныURL" in item_parts:
            url_idx = item_parts.index("ШаблоныURL")
            # Determine if node represents a Method or a Template
            is_method = "Методы" in item_parts[url_idx + 1:]

            # Ensure storage for templates on the parent object (RU keys)
            templates_list = parent_obj.properties.setdefault("ШаблоныURL", [])

            def _get_or_create_template(t_name: Optional[str]):
                if not t_name:
                    return None
                for t in templates_list:
                    if isinstance(t, dict) and t.get("Имя") == t_name:
                        return t
                new_t = {"Имя": t_name, "Свойства": {}, "Методы": []}
                templates_list.append(new_t)
                return new_t

            if is_method:
                # Node is a method like: - HTTPСервисы.<Service>.ШаблоныURL.<Template>.Методы.<Method>
                t_name = item_parts[url_idx + 1] if url_idx + 1 < len(item_parts) else None
                try:
                    m_idx = item_parts.index("Методы", url_idx + 1)
                except ValueError:
                    m_idx = -1
                m_name = item_parts[m_idx + 1] if (m_idx != -1 and m_idx + 1 < len(item_parts)) else None

                template_obj = _get_or_create_template(t_name)
                if template_obj is None:
                    # fallback create
                    template_obj = _get_or_create_template((t_name or "").strip())

                method_dict: Dict[str, Any] = {"Имя": m_name or "", "Свойства": {}}

                # Parse properties under the method node (ignore hyphen children)
                self._fill_properties(node.get('children', []), method_dict["Свойства"])

                methods = template_obj.setdefault("Методы", [])
                methods.append(method_dict)

            else:
                # Node is a template like: - HTTPСервисы.<Service>.ШаблоныURL.<Template>
                t_name = item_parts[url_idx + 1] if url_idx + 1 < len(item_parts) else None
                template_obj = _get_or_create_template(t_name)
                if template_obj is None:
                    template_obj = _get_or_create_template((t_name or "").strip())

                # Parse template-level properties and nested method blocks
                for child in node.get('children', []):
                    if child['data'].startswith('-'):
                        # Potential method under this template
                        child_full = child['data'][1:].strip()
                        child_parts = child_full.split('.')
                        if "Методы" in child_parts:
                            m_idx = child_parts.index("Методы")
                            if m_idx < len(child_parts) - 1:
                                m_name = child_parts[m_idx + 1]
                                m_dict: Dict[str, Any] = {"Имя": m_name, "Свойства": {}}
                                self._fill_properties(child.get('children', []), m_dict["Свойства"])
                                methods = template_obj.setdefault("Методы", [])
                                methods.append(m_dict)
                    else:
                        self._fill_properties([child], template_obj["Свойства"])
        elif "ЗначенияПеречисления" in item_parts:
            # This is an enum value of an Enumeration (Перечисления)
            val_index = item_parts.index("ЗначенияПеречисления")
            if val_index < len(item_parts) - 1:
                # Only treat the value node itself (exclude any nested attributes)
                if "Реквизиты" not in item_parts[val_index:]:
                    # Value name is typically the last segment
                    val_name = item_parts[-1]
                    new_val = EnumValue(name=val_name)
                    parent_obj.add_enum_value(new_val)

                    # Parse enum value properties (support nested/multiline values)
                    self._fill_properties(node.get('children', []), new_val.properties)

        elif "Формы" in item_parts:
            # This is a managed form
            form_index = item_parts.index("Формы")
            if form_index < len(item_parts) - 1:
                # Only treat the form node itself (exclude any nested attributes if they appear)
                if "Реквизиты" not in item_parts[form_index:]:
                    form_name = item_parts[form_index + 1]
                    new_form = Form(name=form_name)
                    parent_obj.add_form(new_form)

                    # Parse form properties
                    self._fill_properties(node.get('children', []), new_form.properties)
        else:
            # Unrecognized sub-item branch
            logger.debug(
                "Unknown sub-item kind: %s (object=%s)",
                item_full_name, parent_obj.name
            )
    
    def _parse_attribute_properties(self, node: Dict, attribute: Attribute):
        """Parse properties of an attribute."""
        self._fill_properties([node], attribute.properties)
    
    def _parse_tabular_attribute(self, node: Dict, tabular_part: TabularPart):
        """Parse an attribute within a tabular part."""
        # Remove the '-' prefix
        item_full_name = node['data'][1:].strip()
        item_parts = item_full_name.split('.')
        
        # Find the attribute name (last part after "Реквизиты")
        if "Реквизиты" in item_parts:
            req_index = item_parts.index("Реквизиты")
            if req_index < len(item_parts) - 1:
                attr_name = item_parts[-1]
                new_attr = Attribute(name=attr_name)
                tabular_part.add_attribute(new_attr)
                
                # Parse attribute properties
                for prop_node in node['children']:
                    self._parse_attribute_properties(prop_node, new_attr)

    def _parse_characteristics(self, node: Dict, parent_obj: MetadataObject):
        """Parse 'Характеристики' property into structured schemes stored on parent_obj"""
        schemes: List[Dict[str, Any]] = []
        for scheme_node in node.get('children', []):
            # Each scheme item is like: "- 0", "- 1", ...
            if not scheme_node['data'].startswith('-'):
                continue
            idx = scheme_node['data'][1:].strip()
            scheme: Dict[str, Any] = {'Индекс': idx}  # Keep original index as string

            # Parse properties inside the scheme block
            for prop_node in scheme_node.get('children', []):
                key, value = self._parse_key_value(prop_node['data'])
                if key:
                    if prop_node.get('children'):
                        scheme[key] = self._assemble_value(key, value, prop_node['children'])
                    else:
                        scheme[key] = value

            schemes.append(scheme)

        parent_obj.characteristic_schemes = schemes

    def _parse_standard_attributes(self, node: Dict, parent_obj: MetadataObject):
        """
        Parse 'СтандартныеРеквизиты' block and materialize ALL listed standard attributes
        as regular attributes linked by HAS_ATTRIBUTE. Special enrichment is applied for
        'Владелец' using object-level 'Владельцы' and 'ИспользованиеПодчинения'.
        - For every item '- <Имя>':
            - Create or merge Attribute(name=<Имя>)
            - Parse inline properties under the item (via _parse_attribute_properties)
            - Set property 'Стандартный' = True
        - For '- Владелец' only:
            - Add 'Тип' from object-level 'Владельцы' (as list)
            - Add 'ИспользованиеПодчинения' from object-level property if present
        Returns True if at least one standard attribute was created or updated.
        """
        created_or_updated = False
        # Build index of existing attributes by name to avoid duplicates
        existing_by_name = {a.name: a for a in parent_obj.attributes}
        for child in node.get('children', []) or []:
            data = child.get('data', '').strip()
            if not data.startswith('-'):
                continue
            std_name = data[1:].strip()
            if not std_name:
                continue

            # Get existing or create new attribute
            attr = existing_by_name.get(std_name)
            if attr is None:
                attr = Attribute(name=std_name)
                parent_obj.add_attribute(attr)
                existing_by_name[std_name] = attr
                created_or_updated = True

            # Parse inline properties for this standard attribute
            for prop_node in child.get('children', []) or []:
                before = dict(attr.properties)
                self._parse_attribute_properties(prop_node, attr)
                if attr.properties != before:
                    created_or_updated = True

            # Mark as standard
            if attr.properties.get("Стандартный") is not True:
                attr.properties["Стандартный"] = True
                created_or_updated = True


        return created_or_updated

