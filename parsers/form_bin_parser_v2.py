"""
V8FormBinParser: binary parser for 1C:Enterprise 8.x Form.bin files.

Parses the binary format directly instead of regex-scraping text.
Extracts: form structure, module code, and form element hierarchy.

Format structure:
  [Header 16B] [IndexTable 512B] [Section1] [Section2] ... [SectionN]

Header:
  0x00: ff ff ff 7f  (magic)
  0x04: format_version (LE uint32, typically 0x200)
  0x08: section_count (LE uint32)
  0x0C: reserved (LE uint32)

IndexTable:
  128 x LE uint32 values (512 bytes)
  Pairs of (offset, length?) separated by 0x7fffffff sentinels
  Points to section boundaries

Sections are delimited by ASCII headers:
  \\r\\n<8hex> <8hex> 7fffffff \\r\\n
"""
from __future__ import annotations

import base64
import re
import struct
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple

logger = logging.getLogger(__name__)

MAGIC = b'\xff\xff\xff\x7f'
SENTINEL = 0x7FFFFFFF
HEADER_SIZE = 16
INDEX_TABLE_SIZE = 512

# Regex for section header lines in the file
# Format: \r\n<8hex> <8hex> <8hex> \r\n
# The third hex value is often 7fffffff (sentinel) but can be other values
_SECTION_HEADER_RE = re.compile(
    rb'\r\n([0-9a-fA-F]{8}) ([0-9a-fA-F]{8}) ([0-9a-fA-F]{8}) \r\n'
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FormBinHeader:
    magic: bytes
    format_version: int
    section_count: int
    reserved: int


@dataclass
class SectionDescriptor:
    """Describes one section found in the file."""
    index: int
    header_offset: int          # file offset where the ASCII header line starts
    data_offset: int            # file offset where section data starts (after header)
    data_end: int               # file offset where section data ends
    hex1: int                   # first hex value from the header line (logical size)
    hex2: int                   # second hex value from the header line (data size)
    hex3: int = SENTINEL        # third hex value: continuation offset (0x7FFFFFFF = none)
    content_type: str = "unknown"
    data: bytes = b''


@dataclass
class FormData:
    """Complete parsed data from a Form.bin file."""
    file_path: str = ""
    header: Optional[FormBinHeader] = None
    index_values: List[int] = field(default_factory=list)
    sections: List[SectionDescriptor] = field(default_factory=list)
    form_structure_text: str = ""
    module_code: str = ""
    module_code_section_index: int = -1


# ---------------------------------------------------------------------------
# Binary reader
# ---------------------------------------------------------------------------

class BinaryReader:
    """Low-level binary reader with position tracking."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read_bytes(self, n: int) -> bytes:
        result = self.data[self.pos:self.pos + n]
        self.pos += n
        return result

    def read_uint32(self) -> int:
        val = struct.unpack_from('<I', self.data, self.pos)[0]
        self.pos += 4
        return val

    def read_uint16(self) -> int:
        val = struct.unpack_from('<H', self.data, self.pos)[0]
        self.pos += 2
        return val

    def peek(self, n: int = 16) -> bytes:
        return self.data[self.pos:self.pos + n]

    def seek(self, offset: int):
        self.pos = offset

    def tell(self) -> int:
        return self.pos

    def remaining(self) -> int:
        return len(self.data) - self.pos


# ---------------------------------------------------------------------------
# Section content type detection
# ---------------------------------------------------------------------------

def _detect_section_content(data: bytes) -> str:
    """Detect the content type of a section by examining its bytes."""
    if len(data) == 0:
        return "empty"

    # UTF-16LE strings (check before UTF-8 BOM since markers are short)
    if len(data) >= 4:
        # Check for "form" in UTF-16LE
        if b'f\x00o\x00r\x00m\x00' in data[:64]:
            return "form_marker"
        # Check for "module" in UTF-16LE
        if b'm\x00o\x00d\x00u\x00l\x00e\x00' in data[:64]:
            return "module_marker"

    # UTF-8 BOM
    if data[:3] == b'\xef\xbb\xbf':
        # Check if it's mostly zeros (empty BSL section)
        sample = data[3:200]
        non_zero = sum(1 for b in sample if b != 0)
        if non_zero < 5:
            return "empty_bsl_section"
        
        # Check if it's form structure text (starts with '{')
        # Form structure starts with curly braces, even if it contains Cyrillic
        first_non_zero = None
        for b in sample:
            if b != 0:
                first_non_zero = b
                break
        if first_non_zero == ord('{'):
            return "form_structure"
        
        # Check if it looks like BSL code (Cyrillic keywords)
        if (b'\xd0' in sample or b'\xd1' in sample):  # Cyrillic UTF-8 range
            return "bsl_code"
        
        return "utf8_text"

    # Check if mostly zeros (binary index/table)
    if len(data) >= 32:
        non_zero = sum(1 for b in data[:64] if b != 0)
        if non_zero < 10:
            return "binary_index"

    # Check for curly brace text format (form structure) without BOM
    if len(data) >= 4:
        if data[:1] == b'{':
            return "form_structure"

    return "unknown"


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

class V8FormBinParser:
    """
    Binary parser for 1C:Enterprise 8.x Form.bin files.

    Usage:
        parser = V8FormBinParser()
        form_data = parser.parse(file_path)
        # or
        form_data = parser.parse_bytes(data, file_path="Form.bin")
    """

    def __init__(self):
        pass

    def parse(self, file_path: Path) -> FormData:
        """Parse a Form.bin file from disk."""
        try:
            data = file_path.read_bytes()
        except Exception as e:
            logger.error(f"Error reading {file_path}: {e}")
            return FormData(file_path=str(file_path))
        return self.parse_bytes(data, str(file_path))

    def parse_bytes(self, data: bytes, file_path: str = "") -> FormData:
        """Parse Form.bin from raw bytes."""
        result = FormData(file_path=file_path)
        reader = BinaryReader(data)

        # 1. Parse header
        result.header = self._parse_header(reader)
        if result.header is None:
            return result

        # 2. Parse index table
        result.index_values = self._parse_index_table(reader)

        # 3. Find all section headers
        result.sections = self._find_sections(data)

        # 4. Classify and extract content from each section
        self._classify_sections(result.sections, data)

        # 5. Extract form structure text
        self._extract_form_structure(result)

        # 6. Extract module code
        self._extract_module_code(result)

        return result

    def export_json_dict(self, result: FormData, mode: str = "semantic") -> dict:
        """Export parsed Form.bin data to a JSON-serializable dict."""
        if mode not in ("semantic", "lossless"):
            raise ValueError(f"Unsupported export mode: {mode}")

        header = None
        if result.header is not None:
            header = {
                'magic_b64': base64.b64encode(result.header.magic).decode('ascii'),
                'format_version': result.header.format_version,
                'section_count': result.header.section_count,
                'reserved': result.header.reserved,
            }

        semantic = {
            'file': result.file_path,
            'header': header,
            'sections': [
                {
                    'index': sec.index,
                    'type': sec.content_type,
                    'hex1': sec.hex1,
                    'hex2': sec.hex2,
                    'hex3': sec.hex3,
                    'size': len(sec.data),
                }
                for sec in result.sections
            ],
            'module_code': result.module_code,
            'form_structure_text': result.form_structure_text,
        }

        lossless = {
            'header': header,
            'sections': [
                {
                    'index': sec.index,
                    'type': sec.content_type,
                    'hex1': sec.hex1,
                    'hex2': sec.hex2,
                    'hex3': sec.hex3,
                    'data_b64': base64.b64encode(sec.data).decode('ascii'),
                }
                for sec in result.sections
            ],
            'module_code_b64': base64.b64encode(result.module_code.encode('utf-8')).decode('ascii') if result.module_code else '',
            'form_structure_text_b64': base64.b64encode(result.form_structure_text.encode('utf-8')).decode('ascii') if result.form_structure_text else '',
        }

        semantic['lossless'] = lossless
        semantic['kind'] = 'semantic'
        return semantic

    def export_json_text(self, result: FormData, mode: str = "semantic") -> str:
        import json
        return json.dumps(self.export_json_dict(result, mode), ensure_ascii=False, indent=2)

    def import_json_payload(self, payload: dict) -> FormData:
        """Build FormData-like payload from a JSON export."""
        kind = payload.get('kind', 'semantic')
        lossless = payload.get('lossless')
        source = lossless if kind == 'semantic' and isinstance(lossless, dict) else payload
        source_dict = source if isinstance(source, dict) else {}

        if 'sections' not in source_dict:
            raise ValueError('JSON payload must contain sections')

        header_src = source_dict.get('header') or {}
        header = FormBinHeader(
            magic=base64.b64decode(header_src.get('magic_b64', '')) if header_src.get('magic_b64') else MAGIC,
            format_version=int(header_src.get('format_version', 0x200)),
            section_count=int(header_src.get('section_count', len(source_dict['sections']))),
            reserved=int(header_src.get('reserved', 0)),
        )

        sections = []
        for sec in source_dict.get('sections', []):
            data_b64 = sec.get('data_b64', '')
            data = base64.b64decode(data_b64) if data_b64 else b''
            sections.append(SectionDescriptor(
                index=int(sec.get('index', len(sections))),
                header_offset=0,
                data_offset=0,
                data_end=0,
                hex1=int(sec.get('hex1', len(data))),
                hex2=int(sec.get('hex2', len(data))),
                hex3=int(sec.get('hex3', SENTINEL)),
                content_type=sec.get('type', 'unknown'),
                data=data,
            ))

        module_code = ''
        form_structure_text = ''
        if 'module_code_b64' in source_dict:
            module_b64 = source_dict.get('module_code_b64', '')
            module_code = base64.b64decode(module_b64).decode('utf-8') if module_b64 else ''
            struct_b64 = source_dict.get('form_structure_text_b64', '')
            form_structure_text = base64.b64decode(struct_b64).decode('utf-8') if struct_b64 else ''
        else:
            module_code = source_dict.get('module_code', '') or ''
            form_structure_text = source_dict.get('form_structure_text', '') or ''

        return FormData(
            file_path=payload.get('file', ''),
            header=header,
            sections=sections,
            form_structure_text=form_structure_text,
            module_code=module_code,
        )

    # -----------------------------------------------------------------------
    # Header parsing
    # -----------------------------------------------------------------------

    def _parse_header(self, reader: BinaryReader) -> Optional[FormBinHeader]:
        """Parse the 16-byte file header."""
        if reader.remaining() < HEADER_SIZE:
            logger.error("File too small for header")
            return None

        magic = reader.read_bytes(4)
        if magic != MAGIC:
            logger.error(f"Invalid magic: {magic.hex()} (expected {MAGIC.hex()})")
            return None

        format_version = reader.read_uint32()
        section_count = reader.read_uint32()
        reserved = reader.read_uint32()

        return FormBinHeader(
            magic=magic,
            format_version=format_version,
            section_count=section_count,
            reserved=reserved,
        )

    # -----------------------------------------------------------------------
    # Index table parsing
    # -----------------------------------------------------------------------

    def _parse_index_table(self, reader: BinaryReader) -> List[int]:
        """Parse the 512-byte index table (128 x LE uint32)."""
        reader.seek(HEADER_SIZE)
        values = []
        for _ in range(INDEX_TABLE_SIZE // 4):
            values.append(reader.read_uint32())
        return values

    # -----------------------------------------------------------------------
    # Section finding
    # -----------------------------------------------------------------------

    def _find_sections(self, data: bytes) -> List[SectionDescriptor]:
        """Find all section headers in the file."""
        sections = []
        for i, m in enumerate(_SECTION_HEADER_RE.finditer(data)):
            hex1 = int(m.group(1), 16)
            hex2 = int(m.group(2), 16)
            hex3 = int(m.group(3), 16)
            sections.append(SectionDescriptor(
                index=i,
                header_offset=m.start(),
                data_offset=m.end(),
                data_end=0,  # filled below
                hex1=hex1,
                hex2=hex2,
                hex3=hex3,
            ))

        # Set data_end for each section
        for i in range(len(sections) - 1):
            sections[i].data_end = sections[i + 1].header_offset
        if sections:
            sections[-1].data_end = len(data)

        return sections

    # -----------------------------------------------------------------------
    # Section classification
    # -----------------------------------------------------------------------

    def _classify_sections(self, sections: List[SectionDescriptor], data: bytes):
        """Classify each section by its content type."""
        for sec in sections:
            sec.data = data[sec.data_offset:sec.data_end]
            sec.content_type = _detect_section_content(sec.data)

    # -----------------------------------------------------------------------
    # Form structure extraction
    # -----------------------------------------------------------------------

    def _extract_form_structure(self, result: FormData):
        """Extract form structure text from the form_structure section."""
        for sec in result.sections:
            if sec.content_type == "form_structure":
                # Find the UTF-8 BOM if present
                data = sec.data
                bom_pos = data.find(b'\xef\xbb\xbf')
                if bom_pos >= 0:
                    text_data = data[bom_pos + 3:]
                else:
                    text_data = data
                try:
                    result.form_structure_text = text_data.decode('utf-8', errors='replace')
                except Exception as e:
                    logger.warning(f"Error decoding form structure: {e}")
                # Concatenate trailing section if present (hex3 != sentinel)
                if sec.hex3 != SENTINEL:
                    for trailing_sec in result.sections:
                        if trailing_sec.header_offset == sec.hex3:
                            trailing_data = trailing_sec.data
                            if trailing_data[:3] == b'\xef\xbb\xbf':
                                trailing_data = trailing_data[3:]
                            try:
                                result.form_structure_text += trailing_data.decode(
                                    'utf-8', errors='replace'
                                )
                            except Exception as e:
                                logger.warning(f"Error decoding trailing section: {e}")
                            break
                break
            elif sec.content_type == "utf8_text":
                # Some files have the structure without BOM
                data = sec.data
                bom_pos = data.find(b'\xef\xbb\xbf')
                if bom_pos >= 0:
                    text_data = data[bom_pos + 3:]
                else:
                    text_data = data
                try:
                    text = text_data.decode('utf-8', errors='replace')
                    if text.startswith('{'):
                        result.form_structure_text = text
                        sec.content_type = "form_structure"
                except:
                    pass

    # -----------------------------------------------------------------------
    # Module code extraction
    # -----------------------------------------------------------------------

    def _extract_module_code(self, result: FormData):
        """Extract BSL module code from the bsl_code section."""
        for i, sec in enumerate(result.sections):
            if sec.content_type == "bsl_code":
                data = sec.data
                # Remove UTF-8 BOM if present
                if data[:3] == b'\xef\xbb\xbf':
                    data = data[3:]
                try:
                    code = data.decode('utf-8', errors='replace')
                    # Strip NUL bytes and trailing whitespace
                    code = code.replace('\x00', '')
                    code = code.rstrip('\r\n \t')
                    if code:
                        result.module_code = code
                        result.module_code_section_index = i
                except Exception as e:
                    logger.warning(f"Error decoding module code: {e}")
                break


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    import sys
    import json

    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <Form.bin> [--json] [--code-only] [--structure-only]")
        sys.exit(1)

    file_path = Path(sys.argv[1])
    code_only = '--code-only' in sys.argv
    structure_only = '--structure-only' in sys.argv
    json_output = '--json' in sys.argv

    parser = V8FormBinParser()
    result = parser.parse(file_path)

    if result.header is None:
        print(f"Error: not a valid Form.bin (bad header/magic): {file_path}",
              file=sys.stderr)
        sys.exit(1)

    if json_output:
        output = {
            'file': result.file_path,
            'header': {
                'format_version': result.header.format_version,
                'section_count': result.header.section_count,
            },
            'sections': [
                {
                    'index': sec.index,
                    'type': sec.content_type,
                    'hex1': f"0x{sec.hex1:04x}",
                    'hex2': f"0x{sec.hex2:04x}",
                    'size': sec.data_end - sec.data_offset,
                }
                for sec in result.sections
            ],
            'module_code': result.module_code if not structure_only else None,
            'form_structure': result.form_structure_text[:500] if not code_only else None,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))

    elif code_only:
        if result.module_code:
            print(result.module_code)
        else:
            print("No module code found")

    elif structure_only:
        if result.form_structure_text:
            print(result.form_structure_text[:2000])
        else:
            print("No form structure found")

    else:
        print(f"=== Form: {file_path.name} ===")
        print()

        print(f"Header: version=0x{result.header.format_version:04x}, sections={result.header.section_count}")
        print()

        print(f"Sections ({len(result.sections)}):")
        for sec in result.sections:
            size = sec.data_end - sec.data_offset
            print(f"  [{sec.index}] {sec.content_type:20s} 0x{sec.header_offset:06x} ({size:6d} bytes) hex1=0x{sec.hex1:04x} hex2=0x{sec.hex2:04x}")
        print()

        if result.module_code:
            print(f"Module code ({len(result.module_code)} chars):")
            print(result.module_code[:500])
            if len(result.module_code) > 500:
                print("  ...")
        else:
            print("No module code found")


if __name__ == '__main__':
    main()
