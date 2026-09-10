"""
FormBinCompiler: compiles 1C Form.bin files from component parts.

Produces binary files compatible with 1C:Enterprise ordinary forms.
Supports Layout A (split form structure) and Layout B (single section)
with automatic selection based on form structure size.
"""
from __future__ import annotations

import base64
import os
import struct
from pathlib import Path
from typing import Optional

from parsers.form_bin_parser_v2 import FormBinHeader, FormData, SectionDescriptor


class FormBinCompiler:
    """Compiles 1C Form.bin files from module code and form structure text."""

    MAGIC = b'\xff\xff\xff\x7f'
    SENTINEL = 0x7FFFFFFF
    FORMAT_VERSION = 0x200       # 512
    SECTION_COUNT = 2
    HEADER_SIZE = 16
    INDEX_DATA_SIZE = 512
    SECTION_HEADER_SIZE = 31    # \r\n + 8 hex + ' ' + 8 hex + ' ' + 8 hex + ' \r\n'
    FORM_MARKER_SIZE = 32
    MODULE_MARKER_SIZE = 36
    UTF8_BOM = b'\xef\xbb\xbf'
    SPLIT_THRESHOLD = 50_000    # bytes: use Layout A if form_structure exceeds this

    def compile(
        self,
        module_code: str,
        form_structure_text: str,
        form_marker_guid: Optional[bytes] = None,
        module_marker_guid: Optional[bytes] = None,
        source_form_data: Optional[FormData] = None,
    ) -> bytes:
        """
        Compile Form.bin from component parts.

        Args:
            module_code: 1C BSL module source code.
            form_structure_text: Curly-brace form structure text.
            form_marker_guid: 16-byte GUID for form marker (auto-generated if None).
            module_marker_guid: 16-byte GUID for module marker (auto-generated if None).

        Returns:
            Complete Form.bin file contents as bytes.
        """
        if source_form_data is not None:
            return self._compile_from_form_data(source_form_data)

        if form_marker_guid is None:
            form_marker_guid = self._generate_guid()
        if module_marker_guid is None:
            module_marker_guid = self._generate_guid()

        # Prepare BSL code
        code_bytes = self._prepare_bsl_code(module_code)

        # Prepare form structure
        struct_bytes = form_structure_text.encode('utf-8')
        if not struct_bytes.startswith(self.UTF8_BOM):
            struct_bytes = self.UTF8_BOM + struct_bytes

        # Decide layout
        use_split = len(struct_bytes) > self.SPLIT_THRESHOLD

        # Build sections and record header offsets
        buf = bytearray()
        offsets = {}  # section_name -> header_offset (start of \r\n)

        # 1. File header (16 bytes)
        buf.extend(self._build_file_header())

        # 2. Index section header + placeholder data
        idx_header_offset = len(buf)
        buf.extend(self._build_section_header(self.INDEX_DATA_SIZE))
        offsets['index'] = idx_header_offset
        idx_data_start = len(buf)
        buf.extend(b'\x00' * self.INDEX_DATA_SIZE)

        # 3. Form marker section
        fm_header_offset = len(buf)
        buf.extend(self._build_section_header(self.FORM_MARKER_SIZE))
        offsets['form_marker'] = fm_header_offset
        buf.extend(self._build_form_marker(form_marker_guid))

        if use_split:
            # Layout A: form structure is split
            # 4. Form structure (first part)
            fs_header_offset = len(buf)
            offsets['form_structure'] = fs_header_offset
            split_point = len(struct_bytes) // 2
            # Find a safe split point (don't split a UTF-8 multi-byte sequence)
            while split_point < len(struct_bytes) and (struct_bytes[split_point] & 0xC0) == 0x80:
                split_point += 1
            part1 = struct_bytes[:split_point]
            part2 = struct_bytes[split_point:]
            # hex3 will be patched later to point to trailing section
            buf.extend(self._build_section_header(len(part1), logical_size=len(struct_bytes)))
            buf.extend(part1)

            # 5. Module marker
            mm_header_offset = len(buf)
            buf.extend(self._build_section_header(self.MODULE_MARKER_SIZE))
            offsets['module_marker'] = mm_header_offset
            buf.extend(self._build_module_marker(module_marker_guid))

            # 6. BSL code
            bs_header_offset = len(buf)
            buf.extend(self._build_section_header(len(code_bytes)))
            offsets['bsl_code'] = bs_header_offset
            buf.extend(code_bytes)

            # 7. Trailing section (rest of form structure)
            tr_header_offset = len(buf)
            buf.extend(self._build_section_header(len(part2), logical_size=0))
            buf.extend(part2)

            # Patch hex3 in form structure header to point to trailing section
            self._patch_section_header(buf, fs_header_offset,
                                       logical_size=len(struct_bytes),
                                       data_size=len(part1),
                                       continuation_offset=tr_header_offset)
        else:
            # Layout B: no split
            # 4. Module marker
            mm_header_offset = len(buf)
            buf.extend(self._build_section_header(self.MODULE_MARKER_SIZE))
            offsets['module_marker'] = mm_header_offset
            buf.extend(self._build_module_marker(module_marker_guid))

            # 5. BSL code
            bs_header_offset = len(buf)
            buf.extend(self._build_section_header(len(code_bytes)))
            offsets['bsl_code'] = bs_header_offset
            buf.extend(code_bytes)

            # 6. Form structure (complete)
            fs_header_offset = len(buf)
            buf.extend(self._build_section_header(len(struct_bytes)))
            offsets['form_structure'] = fs_header_offset
            buf.extend(struct_bytes)

        # Patch index table
        self._patch_index(buf, idx_data_start, offsets)

        return bytes(buf)

    def compile_to_file(
        self,
        output_path: Path,
        module_code: str,
        form_structure_text: str,
        form_marker_guid: Optional[bytes] = None,
        module_marker_guid: Optional[bytes] = None,
        source_form_data: Optional[FormData] = None,
    ) -> None:
        """Compile and write Form.bin to a file."""
        data = self.compile(
            module_code,
            form_structure_text,
            form_marker_guid,
            module_marker_guid,
            source_form_data,
        )
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(data)

    def compile_from_json_dict(self, payload: dict) -> bytes:
        """Compile Form.bin from a JSON export payload."""
        kind = payload.get('kind', 'semantic')
        if kind == 'semantic' and isinstance(payload.get('lossless'), dict):
            return self._compile_from_lossless_json(payload['lossless'])
        if kind == 'lossless' and isinstance(payload.get('lossless'), dict):
            return self._compile_from_lossless_json(payload['lossless'])
        if kind == 'lossless':
            return self._compile_from_lossless_json(payload)

        return self.compile(
            module_code=payload.get('module_code', ''),
            form_structure_text=payload.get('form_structure_text', ''),
        )

    def compile_from_json_file(self, input_path: Path) -> bytes:
        import json
        payload = json.loads(Path(input_path).read_text(encoding='utf-8'))
        return self.compile_from_json_dict(payload)

    def _compile_from_form_data(self, form_data: FormData) -> bytes:
        """Rebuild Form.bin by replaying parsed sections verbatim."""
        if form_data.header is None:
            raise ValueError('form_data.header is required for exact rebuild')

        buf = bytearray()
        buf.extend(self._build_file_header(form_data.header))

        for section in form_data.sections:
            buf.extend(
                self._build_section_header(
                    section.hex2,
                    logical_size=section.hex1,
                    continuation_offset=None if section.hex3 == self.SENTINEL else section.hex3,
                )
            )
            buf.extend(section.data)

        return bytes(buf)

    def _compile_from_lossless_json(self, payload: dict) -> bytes:
        """Rebuild Form.bin from a lossless JSON export."""
        header = payload.get('header') or {}
        sections = payload.get('sections') or []

        header_obj = FormBinHeader(
            magic=base64.b64decode(header.get('magic_b64', '')) if header.get('magic_b64') else self.MAGIC,
            format_version=int(header.get('format_version', self.FORMAT_VERSION)),
            section_count=int(header.get('section_count', len(sections))),
            reserved=int(header.get('reserved', 0)),
        )

        form_data = FormData(
            file_path=payload.get('file', ''),
            header=header_obj,
            sections=[
                SectionDescriptor(
                    index=int(sec.get('index', idx)),
                    header_offset=0,
                    data_offset=0,
                    data_end=0,
                    hex1=int(sec.get('hex1', 0)),
                    hex2=int(sec.get('hex2', 0)),
                    hex3=int(sec.get('hex3', self.SENTINEL)),
                    content_type=sec.get('type', 'unknown'),
                    data=base64.b64decode(sec.get('data_b64', '')) if sec.get('data_b64') else b'',
                )
                for idx, sec in enumerate(sections)
            ],
            form_structure_text=base64.b64decode(payload.get('form_structure_text_b64', '')).decode('utf-8') if payload.get('form_structure_text_b64') else '',
            module_code=base64.b64decode(payload.get('module_code_b64', '')).decode('utf-8') if payload.get('module_code_b64') else '',
        )

        return self._compile_from_form_data(form_data)

    # ------------------------------------------------------------------
    # Section builders
    # ------------------------------------------------------------------

    def _build_file_header(self, header: Optional[FormBinHeader] = None) -> bytes:
        """Build the 16-byte file header."""
        if header is None:
            section_count = self.SECTION_COUNT
            format_version = self.FORMAT_VERSION
            reserved = 0
        else:
            section_count = header.section_count
            format_version = header.format_version
            reserved = header.reserved

        return (
            self.MAGIC
            + struct.pack('<I', format_version)
            + struct.pack('<I', section_count)
            + struct.pack('<I', reserved)
        )

    def _build_section_header(
        self,
        data_size: int,
        logical_size: Optional[int] = None,
        continuation_offset: Optional[int] = None,
    ) -> bytes:
        """Build a 31-byte section header."""
        hex1 = logical_size if logical_size is not None else data_size
        hex2 = data_size
        hex3 = continuation_offset if continuation_offset is not None else self.SENTINEL

        return (
            b'\r\n'
            + f'{hex1:08x}'.encode('ascii')
            + b' '
            + f'{hex2:08x}'.encode('ascii')
            + b' '
            + f'{hex3:08x}'.encode('ascii')
            + b' \r\n'
        )

    def _build_form_marker(self, guid: bytes) -> bytes:
        """Build the 32-byte form marker section data."""
        # guid is 16 bytes, duplicated
        return (
            guid[:16]
            + guid[:16]
            + b'\x00\x00\x00\x00'
            + 'form'.encode('utf-16-le')
            + b'\x00\x00\x00\x00'
        )

    def _build_module_marker(self, guid: bytes) -> bytes:
        """Build the 36-byte module marker section data."""
        return (
            guid[:16]
            + guid[:16]
            + b'\x00\x00\x00\x00'
            + 'module'.encode('utf-16-le')
            + b'\x00\x00'
        )

    def _prepare_bsl_code(self, code: str) -> bytes:
        """Encode BSL code to bytes with UTF-8 BOM."""
        # Strip NUL bytes
        clean = code.replace('\x00', '')
        raw = clean.encode('utf-8')
        if not raw.startswith(self.UTF8_BOM):
            raw = self.UTF8_BOM + raw
        return raw

    def _generate_guid(self) -> bytes:
        """Generate a random 16-byte GUID."""
        return os.urandom(16)

    # ------------------------------------------------------------------
    # Patching
    # ------------------------------------------------------------------

    def _patch_index(
        self,
        buf: bytearray,
        idx_data_start: int,
        offsets: dict,
    ) -> None:
        """Write section header offsets into the index table."""
        base = idx_data_start
        fmt = '<I'

        # Group A: form_marker, form_structure, SENTINEL
        struct.pack_into(fmt, buf, base + 0, offsets['form_marker'])
        struct.pack_into(fmt, buf, base + 4, offsets['form_structure'])
        struct.pack_into(fmt, buf, base + 8, self.SENTINEL)

        # Group B: module_marker, bsl_code, SENTINEL
        struct.pack_into(fmt, buf, base + 12, offsets['module_marker'])
        struct.pack_into(fmt, buf, base + 16, offsets['bsl_code'])
        struct.pack_into(fmt, buf, base + 20, self.SENTINEL)

    def _patch_section_header(
        self,
        buf: bytearray,
        offset: int,
        logical_size: int,
        data_size: int,
        continuation_offset: int,
    ) -> None:
        """Overwrite a section header at the given offset."""
        new_header = self._build_section_header(
            data_size, logical_size, continuation_offset
        )
        buf[offset:offset + self.SECTION_HEADER_SIZE] = new_header


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    import sys
    import json
    import argparse

    from parsers.form_bin_parser_v2 import V8FormBinParser

    parser = argparse.ArgumentParser(
        description='Сборка Form.bin по JSON-описанию или точная перепаковка исходного Form.bin.'
    )
    parser.add_argument('input_path', help='input.json или исходный Form.bin')
    parser.add_argument('output_path', help='выходной Form.bin')
    parser.add_argument(
        '--json-mode',
        choices=('semantic', 'lossless'),
        help='экспорт JSON в semantic или lossless режиме',
    )
    parser.add_argument(
        '--json-export',
        action='store_true',
        help='устаревший алиас для --json-mode',
    )
    parser.add_argument(
        '--json-import',
        action='store_true',
        help='собрать Form.bin из JSON-экспорта',
    )
    parser.add_argument(
        '--exact',
        action='store_true',
        help='точная перепаковка исходного Form.bin 1:1',
    )
    args = parser.parse_args()

    input_path = Path(args.input_path)
    output_path = Path(args.output_path)
    compiler = FormBinCompiler()

    if args.json_mode or args.json_export:
        parser_v2 = V8FormBinParser()
        result = parser_v2.parse(input_path)
        json_mode = args.json_mode or ('lossless' if args.exact else 'semantic')
        payload = parser_v2.export_json_text(result, mode=json_mode)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload, encoding='utf-8')
        print(f"Exported {output_path} ({output_path.stat().st_size} bytes)")
        return

    if args.json_import or input_path.suffix.lower() == '.json':
        data = compiler.compile_from_json_file(input_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(data)
        print(f"Imported {output_path} ({output_path.stat().st_size} bytes)")
        return

    if args.exact:
        parser_v2 = V8FormBinParser()
        source_form_data = parser_v2.parse(input_path)
        data = compiler.compile(
            module_code=source_form_data.module_code,
            form_structure_text=source_form_data.form_structure_text,
            source_form_data=source_form_data,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(data)
        print(f"Repacked {output_path} ({output_path.stat().st_size} bytes)")
        return

    with open(input_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    compiler.compile_to_file(
        output_path,
        module_code=data.get('module_code', ''),
        form_structure_text=data.get('form_structure_text', ''),
    )
    print(f"Compiled {output_path} ({output_path.stat().st_size} bytes)")


if __name__ == '__main__':
    main()
