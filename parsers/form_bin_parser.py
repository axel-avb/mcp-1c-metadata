"""
FormBinParser: extracts 1C form module code from Form.bin files (ordinary forms).

Wrapper around V8FormBinParser (binary parser) that provides the legacy API
used by bsl_worker.py, indexer/workers.py, and incremental/bsl_parse_only.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, List, Tuple
import re
import logging

from parsers.form_bin_parser_v2 import V8FormBinParser

logger = logging.getLogger(__name__)


class FormBinParser:
    def __init__(self, code_path: Path):
        self.code_path = code_path

    def parse(self, file_path: Path, content: Optional[str] = None) -> Tuple[List[str], str]:
        """
        Parses a Form.bin file to extract 1C form module code for indexing.

        Args:
            file_path: Path to the file
            content: Pre-read file content (ignored, kept for API compat)

        Returns:
            Tuple of (code_chunks: List[str], module_path_line: str)
        """
        try:
            parser = V8FormBinParser()
            result = parser.parse(file_path)
        except Exception as e:
            logger.error(f"Error parsing form file {file_path}: {e}")
            return [], ""

        code_text = result.module_code
        if not code_text or not code_text.strip():
            return [], ""

        # Generate module path line
        module_path_line = self._translate_form_path_to_module_path(file_path)

        # Combine module path + code
        full_content = module_path_line + "\n" + code_text

        return [full_content], module_path_line

    def _translate_form_path_to_module_path(self, file_path: Path) -> str:
        """
        Translate Form.bin file path to a readable module path comment.

        Example:
            /app/code/Catalogs/Банки/Forms/ФормаЭлемента/Ext/Form.bin
            -> // Модуль формы: Справочники.Банки.Форма.ФормаЭлемента (обычная)
        """
        try:
            rel = file_path.relative_to(self.code_path)
            parts = list(rel.parts)

            if "CommonForms" in parts:
                idx = parts.index("CommonForms")
                if idx + 1 < len(parts):
                    form_name = parts[idx + 1]
                    return f"// Модуль формы: ОбщиеФормы.{form_name} (обычная)"

            if "Forms" in parts:
                idx = parts.index("Forms")
                if idx >= 2 and idx + 1 < len(parts):
                    cat_folder = parts[idx - 2]
                    obj_name = parts[idx - 1]
                    form_name = parts[idx + 1]

                    from xcf_utils import ru_category_from_folder
                    cat_ru = ru_category_from_folder(cat_folder)

                    return f"// Модуль формы: {cat_ru}.{obj_name}.Форма.{form_name} (обычная)"

            return f"// Модуль обычной формы: {file_path.name}"
        except Exception as e:
            logger.debug(f"Failed to translate form path {file_path}: {e}")
            return f"// Модуль обычной формы: {file_path.name}"
