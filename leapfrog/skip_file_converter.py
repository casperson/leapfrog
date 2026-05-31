"""Convert timestamp text, HTML, and .skp files into Leapfrog sidecars."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx

from .adapters.runtime_common import _parse_time_value, normalize_sidecar_segments
from .domain import SUPPORTED_CATEGORIES
from .segment_export import read_sidecar_file, write_sidecar_file

_RANGE_RE = re.compile(
    r"^\s*(?P<start>\d+(?::\d+){1,2}(?:\.\d+)?)\s*-->\s*(?P<end>\d+(?::\d+){1,2}(?:\.\d+)?)\s*$"
)
_POINT_RE = re.compile(r"^\s*(?P<point>\d+(?::\d+){1,2}(?:\.\d+)?)\s*$")


@dataclass(slots=True)
class ImportedSegment:
    start_time: float
    end_time: float
    text_excerpt: str


@dataclass(slots=True)
class BatchConversionResult:
    title: str
    output_path: str
    status: str
    detail: str = ""


class _TextExtractor(HTMLParser):
    """Extract visible text lines from basic HTML."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        cleaned = data.strip()
        if cleaned:
            self._chunks.append(cleaned)

    def lines(self) -> list[str]:
        return self._chunks


def _default_category() -> str:
    return SUPPORTED_CATEGORIES[0] if SUPPORTED_CATEGORIES else "sex_nudity_immodesty"


def _guess_media_id(source: str, title: str | None = None) -> str:
    stem = Path(source).stem.strip()
    if stem:
        return stem
    if title:
        return title.strip().replace(" ", "_")
    return "imported-media"


def _is_metadata_line(value: str) -> bool:
    cleaned = value.strip()
    return cleaned.startswith("data:image/") or (cleaned.startswith("{") and cleaned.endswith("}"))


def _parse_timestamp_segments(lines: list[str]) -> list[ImportedSegment]:
    """Parse timestamp blocks where a time line is followed by descriptive text."""
    segments: list[ImportedSegment] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or _is_metadata_line(line):
            index += 1
            continue

        range_match = _RANGE_RE.match(line)
        point_match = _POINT_RE.match(line)
        if not range_match and not point_match:
            index += 1
            continue

        if range_match:
            start_time = _parse_time_value(range_match.group("start"))
            end_time = _parse_time_value(range_match.group("end"))
        else:
            start_time = _parse_time_value(point_match.group("point"))
            end_time = start_time

        index += 1
        description_lines: list[str] = []
        while index < len(lines):
            candidate = lines[index].strip()
            if not candidate:
                if description_lines:
                    index += 1
                    break
                index += 1
                continue
            if _RANGE_RE.match(candidate) or _POINT_RE.match(candidate) or _is_metadata_line(candidate):
                break
            description_lines.append(candidate)
            index += 1

        segments.append(
            ImportedSegment(
                start_time=start_time,
                end_time=end_time,
                text_excerpt=" ".join(description_lines).strip(),
            )
        )
    return segments


def parse_skp_text(raw_text: str) -> list[ImportedSegment]:
    """Parse a .skp file into imported timestamp segments."""
    return _parse_timestamp_segments(raw_text.splitlines())


def parse_timestamp_text(raw_text: str) -> list[ImportedSegment]:
    """Parse plain text timestamp blocks into imported segments."""
    return _parse_timestamp_segments(raw_text.splitlines())


def parse_timestamp_html(raw_html: str) -> list[ImportedSegment]:
    """Parse HTML that contains visible timestamp blocks into imported segments."""
    parser = _TextExtractor()
    parser.feed(raw_html)
    return _parse_timestamp_segments(parser.lines())


def build_sidecar_payload_from_import(
    *,
    media_id: str,
    title: str,
    segments: list[ImportedSegment],
    category: str,
    source: str,
) -> dict[str, Any]:
    """Return a Leapfrog sidecar payload from imported timestamp segments."""
    return {
        "format": "leapfrog.segment.sidecar/v1",
        "media_id": media_id,
        "title": title,
        "external_ids": {"import_source": source},
        "segments": [
            {
                "media_id": media_id,
                "start_time": segment.start_time,
                "end_time": segment.end_time,
                "category": category,
                "source": source,
                "confidence": None,
                "labels": "",
                "text_excerpt": segment.text_excerpt or None,
                "created_at": None,
                "updated_at": None,
                "review_status": "pending",
            }
            for segment in segments
        ],
    }


def sidecar_to_skp_text(payload: dict[str, Any], *, append_alignment: str | None = None) -> str:
    """Convert a Leapfrog sidecar payload into .skp-style plain text."""
    media_id = str(payload.get("media_id") or "")
    segments = normalize_sidecar_segments(payload, default_media_id=media_id)
    blocks: list[str] = []
    for segment in segments:
        start = _format_skp_time(segment.start_time)
        end = _format_skp_time(segment.end_time)
        if segment.end_time > segment.start_time:
            time_line = f"{start} --> {end}"
        else:
            time_line = start
        description = (segment.text_excerpt or segment.labels or segment.category or "skip").strip()
        blocks.append(f"{time_line}\n{description}")
    if append_alignment:
        blocks.append(append_alignment.strip())
    return "\n\n".join(blocks).strip() + "\n"


def _format_skp_time(value: float) -> str:
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    seconds = value - (hours * 3600) - (minutes * 60)
    if abs(seconds - round(seconds)) < 0.001:
        return f"{hours}:{minutes:02d}:{int(round(seconds)):02d}"
    trimmed = f"{seconds:05.2f}".rstrip("0").rstrip(".")
    if seconds < 10:
        trimmed = f"0{trimmed}"
    return f"{hours}:{minutes:02d}:{trimmed}"


async def _write_text(path: Path, content: str) -> Path:
    await asyncio.to_thread(path.write_text, content, "utf-8")
    return path


async def convert_to_sidecar(
    *,
    input_path: str | None,
    input_url: str | None,
    output_path: str,
    input_format: str,
    title: str | None,
    media_id: str | None,
    category: str | None,
    source: str,
) -> Path:
    """Import .skp or timestamp page text into a Leapfrog sidecar file."""
    if input_url:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(input_url)
            response.raise_for_status()
            raw = response.text
        raw_source = input_url
    elif input_path:
        raw = await asyncio.to_thread(Path(input_path).read_text, "utf-8")
        raw_source = input_path
    else:
        raise ValueError("An input path or URL is required.")

    normalized_format = input_format.lower()
    if normalized_format == "skp":
        imported_segments = parse_skp_text(raw)
    elif normalized_format == "text":
        imported_segments = parse_timestamp_text(raw)
    elif normalized_format == "html":
        imported_segments = parse_timestamp_html(raw)
    else:
        raise ValueError(f"Unsupported import format: {input_format}")

    resolved_title = title or Path(raw_source).stem.replace("_", " ").strip()
    resolved_media_id = media_id or _guess_media_id(raw_source, resolved_title)
    payload = build_sidecar_payload_from_import(
        media_id=resolved_media_id,
        title=resolved_title,
        segments=imported_segments,
        category=category or _default_category(),
        source=source,
    )
    return await write_sidecar_file(Path(output_path), payload)


async def convert_sidecar_to_skp(
    *,
    input_path: str,
    output_path: str,
    alignment_json: str | None = None,
) -> Path:
    """Convert a Leapfrog sidecar JSON file into .skp text."""
    payload = await read_sidecar_file(Path(input_path))
    content = sidecar_to_skp_text(payload, append_alignment=alignment_json)
    return await _write_text(Path(output_path), content)


def _first_present(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


async def convert_manifest_to_sidecars(manifest_path: str) -> list[BatchConversionResult]:
    """Convert a CSV manifest of timestamp sources into Leapfrog sidecars."""
    raw_rows = await asyncio.to_thread(Path(manifest_path).read_text, "utf-8")
    reader = csv.DictReader(raw_rows.splitlines())
    results: list[BatchConversionResult] = []
    for row in reader:
        title = _first_present(row, "Title", "title")
        input_type = _first_present(row, "Input Type", "input_type", "type").lower()
        input_value = _first_present(row, "Input", "input", "Path", "path", "Url", "url")
        output = _first_present(row, "Output", "output")
        media_id = _first_present(row, "Media Id", "media_id")
        category = _first_present(row, "Category", "category") or _default_category()
        source = _first_present(row, "Source", "source") or "manifest_import"

        if not input_type or not input_value or not output:
            results.append(
                BatchConversionResult(
                    title=title or input_value or "unknown",
                    output_path=output,
                    status="error",
                    detail="Manifest row must include Input Type, Input, and Output",
                )
            )
            continue

        try:
            await convert_to_sidecar(
                input_path=None if input_type == "url" else input_value,
                input_url=input_value if input_type == "url" else None,
                output_path=output,
                input_format="html" if input_type == "url" else input_type,
                title=title or None,
                media_id=media_id or None,
                category=category,
                source=source,
            )
            results.append(
                BatchConversionResult(
                    title=title or Path(output).stem,
                    output_path=output,
                    status="ok",
                )
            )
        except Exception as exc:
            results.append(
                BatchConversionResult(
                    title=title or input_value,
                    output_path=output,
                    status="error",
                    detail=str(exc),
                )
            )
    return results


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert .skp or timestamp text into Leapfrog sidecars.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("to-sidecar", help="Import timestamp data into a Leapfrog sidecar JSON file.")
    import_parser.add_argument("--input", help="Local input file path.")
    import_parser.add_argument("--url", help="Optional URL to fetch instead of a local input file.")
    import_parser.add_argument("--input-format", choices=("skp", "text", "html"), required=True)
    import_parser.add_argument("--output", required=True, help="Output .leapfrog.json path.")
    import_parser.add_argument("--title", help="Override sidecar title.")
    import_parser.add_argument("--media-id", help="Override sidecar media id.")
    import_parser.add_argument("--category", default=_default_category(), help="Leapfrog category for imported segments.")
    import_parser.add_argument("--source", default="imported", help="Source label written into imported segments.")

    export_parser = subparsers.add_parser("to-skp", help="Export a Leapfrog sidecar JSON file to .skp text.")
    export_parser.add_argument("--input", required=True, help="Input .leapfrog.json path.")
    export_parser.add_argument("--output", required=True, help="Output .skp path.")
    export_parser.add_argument(
        "--alignment-json",
        help='Optional raw JSON line to append, e.g. \'{"local":0,"faselhdwatch":0}\'.',
    )

    batch_parser = subparsers.add_parser("batch-to-sidecar", help="Convert a CSV manifest of timestamp sources into Leapfrog sidecars.")
    batch_parser.add_argument("--manifest", required=True, help="Input CSV manifest path.")
    return parser


async def _amain(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "to-sidecar":
        await convert_to_sidecar(
            input_path=args.input,
            input_url=args.url,
            output_path=args.output,
            input_format=args.input_format,
            title=args.title,
            media_id=args.media_id,
            category=args.category,
            source=args.source,
        )
        return 0

    if args.command == "to-skp":
        await convert_sidecar_to_skp(
            input_path=args.input,
            output_path=args.output,
            alignment_json=args.alignment_json,
        )
        return 0

    if args.command == "batch-to-sidecar":
        results = await convert_manifest_to_sidecars(args.manifest)
        for result in results:
            if result.status == "ok":
                print(f"OK: {result.title} -> {result.output_path}")
            else:
                print(f"ERROR: {result.title} -> {result.output_path} ({result.detail})")
        return 0 if all(result.status == "ok" for result in results) else 1

    parser.error(f"Unknown command: {args.command}")
    return 2


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
