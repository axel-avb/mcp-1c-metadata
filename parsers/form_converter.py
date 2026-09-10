"""
CLI: convert a legacy ordinary Form.bin into a managed Form.xml + web JSON
schema, and compare against a reference managed Form.xml.

Usage:
    python -m parsers.form_converter <legacy.bin> <out_dir> [--reference <managed.xml>]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from parsers.form_bin_parser_v2 import V8FormBinParser
from parsers.form_structure_parser import FormStructureParser
from parsers.form_xml_generator import FormXmlGenerator
from parsers.form_web_schema import FormWebSchemaGenerator
from parsers.form_xml_reader import FormXmlReader
from parsers.form_comparison import compare_forms, render_report


def convert(legacy_bin: Path, out_dir: Path, reference_xml: Path | None = None) -> int:
    bin_parser = V8FormBinParser()
    form_data = bin_parser.parse(legacy_bin)
    if form_data.header is None:
        print(f"Error: not a valid Form.bin: {legacy_bin}", file=sys.stderr)
        return 1

    parsed = FormStructureParser().parse_enriched(form_data.form_structure_text)

    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Managed Form.xml
    xml_path = out_dir / 'Form.xml'
    FormXmlGenerator().generate_to_file(parsed, xml_path)
    print(f"Managed Form.xml: {xml_path}")

    # 2. Web JSON schema
    json_path = out_dir / 'form.schema.json'
    FormWebSchemaGenerator().generate_to_file(parsed, json_path)
    print(f"Web JSON schema: {json_path}")

    # 3. Comparison report (optional)
    if reference_xml is not None:
        ref = FormXmlReader().read(reference_xml)
        report = compare_forms(parsed, ref, legacy_bin.stem)
        report_path = out_dir / 'comparison.txt'
        report_path.write_text(render_report(report), encoding='utf-8')
        print(f"Comparison report: {report_path}")
        print(render_report(report))

    return 0


def main():
    cli = argparse.ArgumentParser(description='Конвертация легаси Form.bin в управляемую форму.')
    cli.add_argument('legacy_bin', help='исходный легаси Form.bin')
    cli.add_argument('out_dir', help='выходной каталог')
    cli.add_argument('--reference', help='эталонная управляемая Form.xml для сравнения')
    args = cli.parse_args()

    sys.exit(convert(Path(args.legacy_bin), Path(args.out_dir),
                      Path(args.reference) if args.reference else None))


if __name__ == '__main__':
    main()
