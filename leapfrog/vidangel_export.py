"""Export VidAngel filter events into raw and Leapfrog-friendly artifacts."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import os
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from xml.sax.saxutils import escape

import httpx

from .logger import get_logger, setup_logging
from .vidangel_taxonomy import get_vidangel_category_rows, normalize_vidangel_group_key

logger = get_logger(__name__)

VIDANGEL_API_BASE = "https://api.vidangel.com/api"
DEFAULT_APP_VERSION = "2026-05-22_07-35-15"
DEFAULT_LIMIT = 100
DEFAULT_CONCURRENCY = 6
DEFAULT_AUDIO_MERGE_GAP_MS = 1500
DEFAULT_VISUAL_MERGE_GAP_MS = 2000
DEFAULT_MIN_SEGMENT_MS = 500
RAW_EVENT_INDEX_FILENAME = "raw_filter_events.index.json"
RAW_EVENT_INDEX_FORMAT = "leapfrog.vidangel.raw-event-index/v1"

_raw_event_index_lock = asyncio.Lock()
_raw_event_index_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
_json_rows_cache_lock = asyncio.Lock()
_json_rows_cache: dict[
    tuple[str, str],
    tuple[tuple[int, int], list[dict[str, Any]]],
] = {}


def _parse_optional_int(value: Any) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        try:
            return int(float(raw))
        except ValueError:
            return None


def _parse_approx_ms(value: Any) -> int | None:
    """Convert a VidAngel approximate-seconds value to millisecond precision."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        seconds = Decimal(raw)
    except InvalidOperation:
        return None
    if not seconds.is_finite():
        return None
    # RawFilterEvent stores integer milliseconds, so only sub-millisecond
    # source precision requires rounding; fractional seconds remain intact.
    return int((seconds * 1000).to_integral_value(rounding=ROUND_HALF_UP))


def _parse_required_int(value: Any, *, field_name: str) -> int:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"missing required integer field: {field_name}")
    try:
        return int(raw)
    except ValueError:
        try:
            return int(float(raw))
        except ValueError as exc:
            raise ValueError(f"invalid integer for {field_name}: {raw}") from exc


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """One catalog query to replay against the works endpoint."""

    params: dict[str, str]
    label: str


@dataclass(frozen=True, slots=True)
class WorkStub:
    """Minimal catalog work data needed for deeper fetches."""

    id: int
    slug: str
    title: str
    work_type: str
    rating: str
    year: int | None
    poster_url: str
    tag_count: int
    service_slug: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "WorkStub":
        return cls(
            id=_parse_required_int(payload.get("id"), field_name="work.id"),
            slug=str(payload["slug"]),
            title=str(payload.get("title") or payload.get("name") or ""),
            work_type=str(payload.get("type") or payload.get("item_type") or ""),
            rating=str(payload.get("rating") or payload.get("mpaa_rating") or ""),
            year=_parse_optional_int(payload.get("year")),
            poster_url=str(payload.get("poster_url") or payload.get("poster") or ""),
            tag_count=_parse_optional_int(payload.get("tag_count")) or 0,
            service_slug=_pick_service_slug(payload),
        )


@dataclass(frozen=True, slots=True)
class TagDefinition:
    """Normalized VidAngel leaf category metadata."""

    category_id: int
    key: str
    display_title: str
    path_keys: tuple[str, ...]
    path_titles: tuple[str, ...]
    default_type: str
    mapped_category: str | None
    example_description: str | None

    def to_record(self) -> dict[str, Any]:
        return {
            "category_id": self.category_id,
            "key": self.key,
            "display_title": self.display_title,
            "path_keys": list(self.path_keys),
            "path_titles": list(self.path_titles),
            "default_type": self.default_type,
            "mapped_category": self.mapped_category,
            "example_description": self.example_description,
        }


@dataclass(frozen=True, slots=True)
class RawFilterEvent:
    """Exact per-tag event as reported by VidAngel."""

    media_id: str
    media_type: str
    title: str
    slug: str
    service_slug: str
    tag_set_id: int
    season_number: int | None
    episode_number: int | None
    path_keys: tuple[str, ...]
    path_titles: tuple[str, ...]
    display_title: str
    description: str | None
    tag_type: str
    start_ms: int
    end_ms: int
    mapped_category: str | None
    leaf_key: str

    @classmethod
    def from_record(cls, row: dict[str, Any]) -> "RawFilterEvent":
        """Parse a persisted raw-filter row into a typed event."""
        media_type = str(row.get("media_type") or "").strip()
        media_id = str(row.get("media_id") or "").strip()
        if not media_type or not media_id:
            raise ValueError("missing media_id or media_type")
        return cls(
            media_id=media_id,
            media_type=media_type,
            title=str(row.get("title") or ""),
            slug=str(row.get("slug") or ""),
            service_slug=str(row.get("service_slug") or ""),
            tag_set_id=_parse_required_int(row.get("tag_set_id"), field_name="tag_set_id"),
            season_number=_parse_optional_int(row.get("season_number")),
            episode_number=_parse_optional_int(row.get("episode_number")),
            path_keys=tuple(part for part in str(row.get("path_keys") or "").split("/") if part),
            path_titles=tuple(part for part in str(row.get("path_titles") or "").split(" > ") if part),
            display_title=str(row.get("display_title") or ""),
            description=str(row.get("description") or "").strip() or None,
            tag_type=str(row.get("tag_type") or ""),
            start_ms=_parse_required_int(row.get("start_ms"), field_name="start_ms"),
            end_ms=_parse_required_int(row.get("end_ms"), field_name="end_ms"),
            mapped_category=str(row.get("mapped_category") or "").strip() or None,
            leaf_key=str(row.get("leaf_key") or ""),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "media_type": self.media_type,
            "title": self.title,
            "slug": self.slug,
            "service_slug": self.service_slug,
            "tag_set_id": self.tag_set_id,
            "season_number": self.season_number,
            "episode_number": self.episode_number,
            "path_keys": list(self.path_keys),
            "path_titles": list(self.path_titles),
            "display_title": self.display_title,
            "description": self.description,
            "tag_type": self.tag_type,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "mapped_category": self.mapped_category,
            "leaf_key": self.leaf_key,
        }


class VidAngelExportDataError(RuntimeError):
    """Raised when a VidAngel export artifact is missing or malformed."""


@dataclass(frozen=True, slots=True)
class TitleArtifact:
    """All output material for one movie or episode."""

    media_id: str
    media_type: str
    title: str
    slug: str
    service_slug: str
    season_number: int | None
    episode_number: int | None
    summary: dict[str, Any]
    events: tuple[RawFilterEvent, ...]
    sidecar_payload: dict[str, Any]
    extra_metadata: dict[str, Any]

    def raw_record(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "media_type": self.media_type,
            "title": self.title,
            "slug": self.slug,
            "service_slug": self.service_slug,
            "season_number": self.season_number,
            "episode_number": self.episode_number,
            "summary": self.summary,
            "extra_metadata": self.extra_metadata,
            "events": [event.to_record() for event in self.events],
        }

    def summary_record(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "media_type": self.media_type,
            "title": self.title,
            "slug": self.slug,
            "service_slug": self.service_slug,
            "season_number": self.season_number,
            "episode_number": self.episode_number,
            "event_count": len(self.events),
            "category_count": len({event.mapped_category for event in self.events if event.mapped_category}),
            "tag_set_id": self.summary.get("tag_set_id"),
            "summary": self.summary,
            "extra_metadata": self.extra_metadata,
        }


@dataclass(frozen=True, slots=True)
class ShowSummary:
    """Aggregated export summary for one TV series across its episodes."""

    show_title: str
    show_slug: str
    service_slug: str
    year: int | None
    rating: str
    season_count: int
    episode_count: int
    event_count: int
    category_count: int
    tag_set_count: int

    def to_record(self) -> dict[str, Any]:
        return {
            "show_title": self.show_title,
            "show_slug": self.show_slug,
            "service_slug": self.service_slug,
            "year": self.year,
            "rating": self.rating,
            "season_count": self.season_count,
            "episode_count": self.episode_count,
            "event_count": self.event_count,
            "category_count": self.category_count,
            "tag_set_count": self.tag_set_count,
        }


class VidAngelClient:
    """Thin async client for the VidAngel JSON endpoints used by the exporter."""

    def __init__(
        self,
        *,
        authorization: str,
        profile_id: str,
        app_version: str,
        concurrency: int,
        extra_headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._authorization = authorization
        self._profile_id = profile_id
        self._app_version = app_version
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        headers = {
            "accept": "application/json, text/plain, */*",
            "authorization": authorization,
            "origin": "https://www.vidangel.com",
            "referer": "https://www.vidangel.com/",
            "user-agent": "Leapfrog VidAngel Exporter/0.1",
            "x-app-platform": "web",
            "x-app-version": app_version,
            "x-avod": "true",
            "x-os-version": "1.0",
            "x-profile": profile_id,
            "x-waf-switch": "KILL",
        }
        if extra_headers:
            for key, value in extra_headers.items():
                if value:
                    headers[key.lower()] = value
        self._client = httpx.AsyncClient(
            base_url=VIDANGEL_API_BASE,
            headers=headers,
            timeout=30.0,
            transport=transport,
        )

    async def close(self) -> None:
        """Close the shared AsyncClient."""
        await self._client.aclose()

    async def __aenter__(self) -> "VidAngelClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        attempts = 3
        delay = 0.5
        for attempt in range(1, attempts + 1):
            async with self._semaphore:
                response = await self._client.get(path, params=params)
            if response.status_code == 401:
                raise RuntimeError(
                    "VidAngel rejected the configured authorization header. "
                    "Refresh LEAPFROG_VIDANGEL_AUTHORIZATION and LEAPFROG_VIDANGEL_PROFILE_ID."
                )
            if response.status_code == 404:
                raise LookupError(f"{path}?{urlencode(params or {})}")
            if response.status_code >= 500 and attempt < attempts:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError(f"Unreachable retry loop for {path}")

    async def fetch_work_parameters(self) -> dict[str, Any]:
        """Return browse filter metadata."""
        return await self._get_json("/content/v2/work-parameters/")

    async def fetch_works_page(self, params: dict[str, str]) -> dict[str, Any]:
        """Return one works listing page."""
        return await self._get_json("/content/v2/works/", params=params)

    async def fetch_show_details(self, slug: str) -> dict[str, Any]:
        """Return the full show payload, including seasons and episode metadata."""
        payload = await self._get_json("/content/v2/shows/", params={"slug": slug})
        results = payload.get("results") or []
        if not results:
            raise LookupError(slug)
        return dict(results[0])

    async def fetch_filter_summary(self, catalog_item_id: int) -> dict[str, Any]:
        """Return summary counts and the tag-set id for one catalog item."""
        return await self._get_json(
            "/filtering/filter-summary/",
            params={"catalog_item_id": catalog_item_id},
        )

    async def fetch_tag_tree(self, tag_set_id: int) -> dict[str, Any]:
        """Return the full tag tree for one tag set."""
        return await self._get_json(f"/bff/tag-sets/{tag_set_id}/")

    async def fetch_season_guide(self, season_id: int) -> dict[str, Any]:
        """Return season-level guide text and metadata."""
        return await self._get_json(
            "/content/content-guides/by-season/",
            params={"season_id": season_id, "enhanced_vocabulary": "true"},
        )


def _sanitize_filename(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-")
    return token or "untitled"


def _load_headers_file(path: Path) -> dict[str, str]:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        headers: dict[str, str] = {}
        for line in raw.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
        return headers
    if isinstance(parsed, dict):
        return {str(key).lower(): str(value) for key, value in parsed.items() if value}
    raise ValueError(f"{path} must contain a JSON object or header lines")


def _resolve_auth_configuration() -> tuple[str, str, str, dict[str, str]]:
    authorization = os.environ.get("LEAPFROG_VIDANGEL_AUTHORIZATION", "").strip()
    profile_id = os.environ.get("LEAPFROG_VIDANGEL_PROFILE_ID", "").strip()
    app_version = os.environ.get("LEAPFROG_VIDANGEL_APP_VERSION", DEFAULT_APP_VERSION).strip()
    extra_headers: dict[str, str] = {}
    headers_file = os.environ.get("LEAPFROG_VIDANGEL_HEADERS_FILE", "").strip()
    if headers_file:
        extra_headers = _load_headers_file(Path(headers_file))
        authorization = authorization or extra_headers.get("authorization", "")
        profile_id = profile_id or extra_headers.get("x-profile", "")
        app_version = extra_headers.get("x-app-version", app_version)
    if not authorization or not profile_id:
        raise RuntimeError(
            "VidAngel export requires LEAPFROG_VIDANGEL_AUTHORIZATION and "
            "LEAPFROG_VIDANGEL_PROFILE_ID, either directly or via LEAPFROG_VIDANGEL_HEADERS_FILE."
        )
    return authorization, profile_id, app_version, extra_headers


def get_vidangel_export_dir() -> Path:
    """Return the default directory used for persisted VidAngel export artifacts."""
    override = os.environ.get("LEAPFROG_VIDANGEL_EXPORT_DIR", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "vidangel"


def _read_json_rows_sync(source: Path, rows_key: str) -> list[dict[str, Any]]:
    payload = json.loads(source.read_text(encoding="utf-8"))
    rows = payload.get(rows_key)
    if not isinstance(rows, list):
        raise ValueError(f"missing {rows_key} list")
    return [dict(row) for row in rows]


async def _load_cached_json_rows(
    source: Path,
    *,
    rows_key: str,
    artifact_label: str,
) -> list[dict[str, Any]]:
    if not await asyncio.to_thread(source.exists):
        return []
    try:
        stamp = await asyncio.to_thread(_raw_event_source_stamp, source)
        resolved_source = await asyncio.to_thread(source.resolve)
    except OSError as exc:
        raise VidAngelExportDataError(f"Invalid {artifact_label}: {source}") from exc
    cache_key = (str(resolved_source), rows_key)
    cached = _json_rows_cache.get(cache_key)
    if cached and cached[0] == stamp:
        return cached[1]

    async with _json_rows_cache_lock:
        cached = _json_rows_cache.get(cache_key)
        if cached and cached[0] == stamp:
            return cached[1]
        try:
            rows = await asyncio.to_thread(_read_json_rows_sync, source, rows_key)
        except Exception as exc:
            raise VidAngelExportDataError(f"Invalid {artifact_label}: {source}") from exc
        _json_rows_cache[cache_key] = (stamp, rows)
        return rows


async def load_vidangel_title_catalog_records(export_dir: Path | None = None) -> list[dict[str, Any]]:
    """Return normalized title catalog rows from the master VidAngel export, if present."""
    root = export_dir or get_vidangel_export_dir()
    return await _load_cached_json_rows(
        root / "title_catalog.json",
        rows_key="titles",
        artifact_label="VidAngel title catalog",
    )


async def load_vidangel_exportable_title_catalog_records(
    export_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Return only VidAngel titles physically present in the raw event export."""
    root = export_dir or get_vidangel_export_dir()
    rows = await load_vidangel_title_catalog_records(root)
    if not rows:
        return []
    index = await _load_raw_event_index(root)
    indexed_titles = index.get("titles")
    if not isinstance(indexed_titles, dict):
        raise VidAngelExportDataError("Invalid VidAngel raw filter event index.")
    return [
        row
        for row in rows
        if _raw_event_index_key(
            str(row.get("media_type") or ""),
            str(row.get("media_id") or ""),
        )
        in indexed_titles
    ]


async def load_vidangel_tag_definition_records(export_dir: Path | None = None) -> list[dict[str, Any]]:
    """Return normalized tag definition rows from the master VidAngel export, if present."""
    root = export_dir or get_vidangel_export_dir()
    return await _load_cached_json_rows(
        root / "tag_definitions.json",
        rows_key="definitions",
        artifact_label="VidAngel tag definitions",
    )


async def load_vidangel_raw_event_records(
    export_dir: Path | None = None,
) -> dict[tuple[str, str], list[RawFilterEvent]]:
    """Return raw VidAngel events grouped by media type and media id."""
    root = export_dir or get_vidangel_export_dir()
    source = root / "raw_filter_events.csv"
    if not source.exists():
        return {}
    if source.stat().st_size == 0:
        return {}

    grouped: dict[tuple[str, str], list[RawFilterEvent]] = {}
    raw_text = await asyncio.to_thread(source.read_text, encoding="utf-8")
    reader = csv.DictReader(io.StringIO(raw_text))
    for row in reader:
        try:
            event = RawFilterEvent.from_record(dict(row))
        except ValueError as exc:
            logger.warning(
                "Skipping malformed VidAngel raw event row from %s for media_id=%r title=%r: %s",
                source,
                row.get("media_id"),
                row.get("title"),
                exc,
            )
            continue
        grouped.setdefault((event.media_type, event.media_id), []).append(event)
    return grouped


def _raw_event_index_key(media_type: str, media_id: str) -> str:
    return f"{media_type}\x1f{media_id}"


def _raw_event_source_stamp(source: Path) -> tuple[int, int]:
    stat = source.stat()
    return stat.st_size, stat.st_mtime_ns


def _raw_event_index_matches_source(
    payload: dict[str, Any],
    source: Path,
) -> bool:
    source_meta = payload.get("source")
    if payload.get("format") != RAW_EVENT_INDEX_FORMAT or not isinstance(source_meta, dict):
        return False
    size, mtime_ns = _raw_event_source_stamp(source)
    return source_meta.get("size") == size and source_meta.get("mtime_ns") == mtime_ns


def _build_raw_event_index_sync(source: Path, index_path: Path) -> dict[str, Any]:
    """Build byte spans so one title can be read without parsing the full CSV."""
    titles: dict[str, dict[str, Any]] = {}
    with source.open("rb") as handle:
        header_line = handle.readline()
        try:
            fieldnames = next(csv.reader([header_line.decode("utf-8")]))
        except (UnicodeDecodeError, StopIteration, csv.Error) as exc:
            raise VidAngelExportDataError(f"Invalid VidAngel raw filter events export: {source}") from exc

        def decoded_lines():
            # readline(), rather than file iteration, keeps tell() available so
            # each logical CSV record can be mapped to its exact byte span.
            while raw_line := handle.readline():
                yield raw_line.decode("utf-8")

        reader = csv.DictReader(decoded_lines(), fieldnames=fieldnames)
        while True:
            start_offset = handle.tell()
            try:
                row = next(reader)
            except StopIteration:
                break
            except (UnicodeDecodeError, csv.Error) as exc:
                raise VidAngelExportDataError(f"Invalid VidAngel raw filter events export: {source}") from exc
            end_offset = handle.tell()
            media_type = str(row.get("media_type") or "").strip()
            media_id = str(row.get("media_id") or "").strip()
            if not media_type or not media_id:
                continue
            key = _raw_event_index_key(media_type, media_id)
            entry = titles.setdefault(key, {"event_count": 0, "spans": []})
            entry["event_count"] += 1
            spans = entry["spans"]
            if spans and spans[-1][1] == start_offset:
                spans[-1][1] = end_offset
            else:
                spans.append([start_offset, end_offset])

    size, mtime_ns = _raw_event_source_stamp(source)
    payload = {
        "format": RAW_EVENT_INDEX_FORMAT,
        "source": {
            "name": source.name,
            "size": size,
            "mtime_ns": mtime_ns,
        },
        "title_count": len(titles),
        "titles": titles,
    }
    try:
        temporary_path = index_path.with_name(f"{index_path.name}.tmp")
        temporary_path.write_text(f"{json.dumps(payload, separators=(',', ':'))}\n", encoding="utf-8")
        temporary_path.replace(index_path)
    except OSError as exc:
        # The in-memory index still makes this request fast even when the export
        # directory is read-only; only future process starts will rebuild it.
        logger.warning("Could not persist VidAngel raw event index at %s: %s", index_path, exc)
    return payload


async def _load_raw_event_index(export_dir: Path) -> dict[str, Any]:
    source = export_dir / "raw_filter_events.csv"
    if not source.exists() or source.stat().st_size == 0:
        raise VidAngelExportDataError("VidAngel raw filter events export is missing.")
    stamp = _raw_event_source_stamp(source)
    cache_key = str(source.resolve())
    cached = _raw_event_index_cache.get(cache_key)
    if cached and cached[0] == stamp:
        return cached[1]

    async with _raw_event_index_lock:
        cached = _raw_event_index_cache.get(cache_key)
        if cached and cached[0] == stamp:
            return cached[1]
        index_path = export_dir / RAW_EVENT_INDEX_FILENAME
        payload: dict[str, Any] | None = None
        if index_path.exists():
            try:
                raw = await asyncio.to_thread(index_path.read_text, encoding="utf-8")
                candidate = json.loads(raw)
                if isinstance(candidate, dict) and _raw_event_index_matches_source(candidate, source):
                    payload = candidate
            except (OSError, json.JSONDecodeError):
                payload = None
        if payload is None:
            payload = await asyncio.to_thread(_build_raw_event_index_sync, source, index_path)
        _raw_event_index_cache[cache_key] = (stamp, payload)
        return payload


async def prepare_vidangel_export(export_dir: Path | None = None) -> dict[str, Any]:
    """Index and cache an existing VidAngel export without network requests."""
    root = export_dir or get_vidangel_export_dir()
    titles = await load_vidangel_title_catalog_records(root)
    if not titles:
        raise VidAngelExportDataError("VidAngel title catalog is missing or empty.")
    definitions = await load_vidangel_tag_definition_records(root)
    if not definitions:
        raise VidAngelExportDataError("VidAngel tag definitions export is missing or empty.")
    index = await _load_raw_event_index(root)
    indexed_titles = index.get("titles")
    if not isinstance(indexed_titles, dict):
        raise VidAngelExportDataError("Invalid VidAngel raw filter event index.")
    event_count = sum(
        int(entry.get("event_count") or 0)
        for entry in indexed_titles.values()
        if isinstance(entry, dict)
    )
    return {
        "ok": True,
        "export_dir": str(root),
        "catalog_title_count": len(titles),
        "indexed_title_count": len(indexed_titles),
        "event_count": event_count,
        "definition_count": len(definitions),
        "index_path": str(root / RAW_EVENT_INDEX_FILENAME),
    }


async def prepare_vidangel_export_if_available(
    export_dir: Path | None = None,
) -> dict[str, Any]:
    """Warm an existing export when present, logging failures without stopping startup."""
    root = export_dir or get_vidangel_export_dir()
    if not await asyncio.to_thread((root / "title_catalog.json").exists):
        return {"ok": False, "available": False, "export_dir": str(root)}
    try:
        result = await prepare_vidangel_export(root)
    except Exception as exc:
        # Preparation is an optimization; damaged optional export artifacts
        # must never prevent the core Leapfrog service from starting.
        logger.warning("Could not prepare existing VidAngel export at %s: %s", root, exc)
        return {
            "ok": False,
            "available": True,
            "export_dir": str(root),
            "error": str(exc),
        }
    logger.info(
        "Prepared existing VidAngel export: titles=%s events=%s definitions=%s",
        result["indexed_title_count"],
        result["event_count"],
        result["definition_count"],
    )
    return {**result, "available": True}


def _read_indexed_raw_events_sync(
    source: Path,
    spans: list[list[int]],
) -> list[RawFilterEvent]:
    with source.open("rb") as handle:
        header_line = handle.readline().decode("utf-8")
        fieldnames = next(csv.reader([header_line]))
        events: list[RawFilterEvent] = []
        for start_offset, end_offset in spans:
            handle.seek(start_offset)
            raw = handle.read(end_offset - start_offset).decode("utf-8")
            reader = csv.DictReader(io.StringIO(raw), fieldnames=fieldnames)
            for row in reader:
                try:
                    events.append(RawFilterEvent.from_record(dict(row)))
                except ValueError as exc:
                    logger.warning(
                        "Skipping malformed indexed VidAngel event for media_id=%r: %s",
                        row.get("media_id"),
                        exc,
                    )
        return events


async def load_vidangel_raw_events_for_title(
    media_type: str,
    media_id: str,
    *,
    export_dir: Path | None = None,
) -> list[RawFilterEvent]:
    """Return one title's events using the persisted raw-event byte index."""
    root = export_dir or get_vidangel_export_dir()
    source = root / "raw_filter_events.csv"
    index = await _load_raw_event_index(root)
    titles = index.get("titles")
    if not isinstance(titles, dict):
        raise VidAngelExportDataError("Invalid VidAngel raw filter event index.")
    entry = titles.get(_raw_event_index_key(media_type, media_id))
    if not isinstance(entry, dict):
        return []
    spans = entry.get("spans")
    if not isinstance(spans, list):
        raise VidAngelExportDataError("Invalid VidAngel raw filter event index.")
    return await asyncio.to_thread(_read_indexed_raw_events_sync, source, spans)


def _event_base_id(event: RawFilterEvent) -> str:
    identity = json.dumps(
        {
            "media_id": event.media_id,
            "media_type": event.media_type,
            "tag_set_id": event.tag_set_id,
            "leaf_key": event.leaf_key,
            "start_ms": event.start_ms,
            "end_ms": event.end_ms,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"event-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}"


def _events_with_ids(events: list[RawFilterEvent]) -> list[tuple[str, RawFilterEvent]]:
    base_ids = [_event_base_id(event) for event in events]
    totals = Counter(base_ids)
    occurrences: Counter[str] = Counter()
    indexed_events: list[tuple[str, RawFilterEvent]] = []
    for base_id, event in zip(base_ids, events, strict=True):
        occurrences[base_id] += 1
        event_id = (
            base_id
            if totals[base_id] == 1
            else f"{base_id}-{occurrences[base_id]}"
        )
        indexed_events.append((event_id, event))
    return indexed_events


async def load_vidangel_export_title(
    media_id: str,
    *,
    export_dir: Path | None = None,
) -> dict[str, Any]:
    """Return the persisted VidAngel title metadata for one media id."""
    titles = await load_vidangel_title_catalog_records(export_dir)
    for row in titles:
        if str(row.get("media_id") or "") == str(media_id):
            return dict(row)
    raise LookupError(media_id)


async def build_vidangel_filter_catalog(
    media_id: str,
    *,
    export_dir: Path | None = None,
) -> dict[str, Any]:
    """Return grouped leaf-filter metadata for one VidAngel title."""
    title = await load_vidangel_export_title(media_id, export_dir=export_dir)
    root = export_dir or get_vidangel_export_dir()
    raw_source = root / "raw_filter_events.csv"
    if not raw_source.exists() or raw_source.stat().st_size == 0:
        raise VidAngelExportDataError("VidAngel raw filter events export is missing.")

    events = await load_vidangel_raw_events_for_title(
        str(title.get("media_type") or ""),
        str(title.get("media_id") or ""),
        export_dir=export_dir,
    )
    if not events:
        raise VidAngelExportDataError(
            f"No VidAngel raw filter events were exported for {media_id}."
        )

    definitions = {
        str(row.get("key") or ""): dict(row)
        for row in await load_vidangel_tag_definition_records(export_dir)
        if str(row.get("key") or "").strip()
    }
    if not definitions:
        raise VidAngelExportDataError("VidAngel tag definitions export is missing.")

    category_meta = {row["key"]: row for row in get_vidangel_category_rows()}
    indexed_events = _events_with_ids(events)
    counts = Counter(event.leaf_key for _, event in indexed_events if event.leaf_key)
    leaf_rows: list[dict[str, Any]] = []
    for leaf_key, count in sorted(counts.items()):
        definition = definitions.get(leaf_key, {})
        category_key = str(definition.get("mapped_category") or "").strip()
        category_row = category_meta.get(category_key, {})
        leaf_rows.append(
            {
                "leaf_key": leaf_key,
                "label": str(definition.get("display_title") or definition.get("key") or leaf_key),
                "description": str(definition.get("example_description") or "").strip() or None,
                "category": category_key or None,
                "category_label": str(category_row.get("label") or category_key or "Uncategorized"),
                "path_keys": list(definition.get("path_keys") or []),
                "path_titles": list(definition.get("path_titles") or []),
                "default_type": str(definition.get("default_type") or "").strip() or None,
                "event_count": count,
                "tag_type": next(
                    (event.tag_type for event in events if event.leaf_key == leaf_key),
                    "",
                ),
                "events": [
                    {
                        "event_id": event_id,
                        "start_ms": event.start_ms,
                        "end_ms": event.end_ms,
                        "description": event.description or event.display_title or leaf_key,
                        "display_title": event.display_title or None,
                        "tag_type": event.tag_type or None,
                    }
                    for event_id, event in indexed_events
                    if event.leaf_key == leaf_key
                ],
            }
        )

    grouped_by_category: dict[str, dict[str, Any]] = {}
    for leaf in leaf_rows:
        category = str(leaf.get("category") or "").strip() or "uncategorized"
        category_row = category_meta.get(category, {})
        grouped_by_category.setdefault(
            category,
            {
                "key": category,
                "label": str(category_row.get("label") or (category.replace("_", " ").title() if category != "uncategorized" else "Uncategorized")),
                "description": str(category_row.get("description") or ""),
                "filters": [],
            },
        )
        grouped_by_category[category]["filters"].append(leaf)

    return {
        "title": title,
        "leaf_count": len(leaf_rows),
        "event_count": len(events),
        "categories": sorted(
            grouped_by_category.values(),
            key=lambda item: str(item["label"]).lower(),
        ),
    }


def _format_clean_media_player_time(value_ms: int) -> str:
    """Format milliseconds using Clean Media Player's TimeSpan JSON form."""
    total_ms = max(0, int(value_ms))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    base = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{base}.{milliseconds:03d}0000" if milliseconds else base


def _clean_media_player_scene_type(event: RawFilterEvent) -> str:
    """Map VidAngel taxonomy to Clean Media Player's scene type names."""
    detail = " ".join(
        [event.leaf_key, event.display_title, event.description or ""]
    ).lower()
    category = str(event.mapped_category or "").lower()
    if "sex" in detail:
        return "Sex"
    if any(token in detail for token in ("nud", "immodest", "undress")):
        return "Nudity"
    if any(token in detail or token in category for token in ("violence", "gore")):
        return "Violence"
    if any(token in detail or token in category for token in ("profanity", "language")):
        return "Profanity"
    if any(token in detail or token in category for token in ("drug", "alcohol", "substance")):
        return "Substance"
    if any(token in detail or token in category for token in ("intense", "disturb", "horror")):
        return "Intense"
    if category == "sex_nudity_immodesty":
        return "Nudity"
    return "Other"


def build_clean_media_player_skp_payload(
    title: dict[str, Any],
    events: list[RawFilterEvent],
) -> dict[str, Any]:
    """Build the native JSON structure written by Clean Media Player."""
    extra = title.get("extra_metadata") if isinstance(title.get("extra_metadata"), dict) else {}
    year = _parse_optional_int(title.get("year") or extra.get("year"))
    season_number = _parse_optional_int(title.get("season_number"))
    episode_number = _parse_optional_int(title.get("episode_number"))
    skip_scenes = []
    for scene_id, event in enumerate(
        sorted(events, key=lambda item: (item.start_ms, item.end_ms, item.display_title)),
        start=1,
    ):
        # Clean Media Player rejects zero-length scenes, while many VidAngel
        # audio events are point timestamps. Expand those to the existing 500ms minimum.
        end_ms = (
            event.end_ms
            if event.end_ms > event.start_ms
            else event.start_ms + DEFAULT_MIN_SEGMENT_MS
        )
        skip_scenes.append(
            {
                "Id": scene_id,
                "SceneType": _clean_media_player_scene_type(event),
                "StartTime": _format_clean_media_player_time(event.start_ms),
                "EndTime": _format_clean_media_player_time(end_ms),
                "Blur": False,
            }
        )
    return {
        "SceneFileTypeId": 1,
        "Year": year,
        "SeasonNumber": season_number,
        "EpisodeNumber": episode_number,
        "SkipScenes": skip_scenes,
        "SkipScenesSyncs": [],
        "Unsynced": False,
    }


async def generate_filtered_vidangel_skp(
    media_id: str,
    selected_leaf_keys: list[str] | None = None,
    *,
    export_dir: Path | None = None,
    output_path: str | Path | None = None,
    selected_event_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Generate a native Clean Media Player .skp from selected VidAngel events."""
    title = await load_vidangel_export_title(media_id, export_dir=export_dir)
    root = export_dir or get_vidangel_export_dir()
    raw_source = root / "raw_filter_events.csv"
    if not raw_source.exists() or raw_source.stat().st_size == 0:
        raise VidAngelExportDataError("VidAngel raw filter events export is missing.")

    events = await load_vidangel_raw_events_for_title(
        str(title.get("media_type") or ""),
        str(title.get("media_id") or ""),
        export_dir=export_dir,
    )
    if not events:
        raise VidAngelExportDataError(
            f"No VidAngel raw filter events were exported for {media_id}."
        )

    requested = [str(value).strip() for value in (selected_leaf_keys or []) if str(value).strip()]
    requested_event_ids = [
        str(value).strip() for value in (selected_event_ids or []) if str(value).strip()
    ]
    indexed_events = _events_with_ids(events)
    if selected_event_ids is not None:
        if not requested_event_ids:
            raise ValueError("At least one VidAngel event must be selected.")
        events_by_id = dict(indexed_events)
        unknown_event_ids = sorted(set(requested_event_ids) - events_by_id.keys())
        if unknown_event_ids:
            raise ValueError(f"Unknown VidAngel event(s): {', '.join(unknown_event_ids)}")
        requested_event_id_set = set(requested_event_ids)
        filtered_events = [
            event
            for event_id, event in indexed_events
            if event_id in requested_event_id_set
        ]
    else:
        if not requested:
            raise ValueError("At least one leaf filter must be selected.")
        requested_set = set(requested)
        available = {event.leaf_key for event in events if event.leaf_key}
        unknown = sorted(requested_set - available)
        if unknown:
            raise ValueError(f"Unknown VidAngel leaf filter(s): {', '.join(unknown)}")
        filtered_events = [event for event in events if event.leaf_key in requested_set]
    if not filtered_events:
        raise ValueError("The selected VidAngel filters did not match any export events.")

    skp_payload = build_clean_media_player_skp_payload(title, filtered_events)
    skp_text = json.dumps(skp_payload, separators=(",", ":"), ensure_ascii=False)
    output_file = Path(output_path) if output_path else None
    if output_file:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(output_file.write_text, skp_text, "utf-8")

    return {
        "title": title,
        "selected_leaf_keys": requested,
        "selected_event_ids": requested_event_ids,
        "selected_event_count": len(filtered_events),
        "skp_text": skp_text,
        "filename": f"{_sanitize_filename(str(title.get('title') or media_id))}.skp",
        "output_path": str(output_file) if output_file else None,
    }


def build_catalog_queries(parameters: dict[str, Any]) -> list[QueryPlan]:
    """Build a broad but finite set of catalog queries for deduped discovery."""
    orders = [
        option["value"]
        for option in parameters.get("orders_by", {}).get("options", [])
        if option.get("value") in {"-popularity", "-published_at", "title"}
    ]
    type_values = [None, "movie", "show"]
    service_catalog_ids = sorted(
        {
            parsed
            for option in parameters.get("services", {}).get("options", [])
            for catalog in option.get("catalogs", [])
            for parsed in [_parse_optional_int(catalog.get("id"))]
            if parsed is not None
        }
    )
    category_slugs = sorted(
        {
            str(option["slug"])
            for option in parameters.get("categories", {}).get("options", [])
            if option.get("slug")
        }
    )
    queries: list[QueryPlan] = []
    seen: set[tuple[tuple[str, str], ...]] = set()

    def add_query(params: dict[str, str], label: str) -> None:
        normalized = tuple(sorted((key, value) for key, value in params.items() if value))
        if normalized in seen:
            return
        seen.add(normalized)
        queries.append(QueryPlan(params=dict(params), label=label))

    for order_by in orders:
        for type_value in type_values:
            params = {"limit": str(DEFAULT_LIMIT), "offset": "0", "order_by": order_by}
            label = f"works:{type_value or 'all'}:{order_by}"
            if type_value:
                params["type"] = type_value
            add_query(params, label)

    for catalog_id in service_catalog_ids:
        for type_value in ("movie", "show"):
            add_query(
                {
                    "limit": str(DEFAULT_LIMIT),
                    "offset": "0",
                    "order_by": "title",
                    "catalogs": str(catalog_id),
                    "type": type_value,
                },
                f"catalog:{catalog_id}:{type_value}",
            )

    for category_slug in category_slugs:
        for type_value in ("movie", "show"):
            add_query(
                {
                    "limit": str(DEFAULT_LIMIT),
                    "offset": "0",
                    "order_by": "title",
                    "category": category_slug,
                    "type": type_value,
                },
                f"genre:{category_slug}:{type_value}",
            )

    return queries


def map_vidangel_category(path_keys: tuple[str, ...]) -> str | None:
    """Map a VidAngel taxonomy path to the canonical VidAngel category-group key."""
    group_key = normalize_vidangel_group_key(path_keys)
    return group_key or None


def _collect_leaf_data(
    category_node: dict[str, Any],
    *,
    ancestors: tuple[dict[str, Any], ...],
    media: dict[str, Any],
    definitions: dict[str, TagDefinition],
    events: list[RawFilterEvent],
) -> None:
    path_nodes = ancestors + (category_node,)
    child_categories = category_node.get("child_categories") or []
    if child_categories:
        for child in child_categories:
            _collect_leaf_data(
                dict(child),
                ancestors=path_nodes,
                media=media,
                definitions=definitions,
                events=events,
            )
        return

    path_keys = tuple(str(node.get("key") or "") for node in path_nodes if node.get("key"))
    path_titles = tuple(
        str(node.get("display_title") or node.get("title") or node.get("key") or "")
        for node in path_nodes
        if node.get("display_title") or node.get("title") or node.get("key")
    )
    mapped_category = map_vidangel_category(path_keys)
    leaf_key = str(category_node.get("key") or "")
    example_description = None
    for tag in category_node.get("tags") or []:
        description = str(tag.get("description") or "").strip() or None
        if description and description != category_node.get("display_title"):
            example_description = description
            break
    definitions[leaf_key] = TagDefinition(
        category_id=_parse_optional_int(category_node.get("id")) or 0,
        key=leaf_key,
        display_title=str(category_node.get("display_title") or leaf_key),
        path_keys=path_keys,
        path_titles=path_titles,
        default_type=str(category_node.get("default_type") or ""),
        mapped_category=mapped_category,
        example_description=example_description,
    )
    for tag in category_node.get("tags") or []:
        start_ms = _parse_approx_ms(tag.get("start_approx")) or 0
        end_ms = _parse_approx_ms(tag.get("end_approx")) or 0
        events.append(
            RawFilterEvent(
                media_id=str(media["media_id"]),
                media_type=str(media["media_type"]),
                title=str(media["title"]),
                slug=str(media["slug"]),
                service_slug=str(media["service_slug"]),
                tag_set_id=_parse_required_int(media.get("tag_set_id"), field_name="media.tag_set_id"),
                season_number=media.get("season_number"),
                episode_number=media.get("episode_number"),
                path_keys=path_keys,
                path_titles=path_titles,
                display_title=str(category_node.get("display_title") or leaf_key),
                description=(str(tag.get("description")).strip() if tag.get("description") else None),
                tag_type=str(tag.get("type") or category_node.get("default_type") or ""),
                start_ms=start_ms,
                end_ms=end_ms,
                mapped_category=mapped_category,
                leaf_key=leaf_key,
            )
        )


def flatten_tag_tree(
    tag_tree: dict[str, Any],
    *,
    media: dict[str, Any],
) -> tuple[list[RawFilterEvent], dict[str, TagDefinition]]:
    """Flatten one VidAngel tag tree into exact events plus leaf definitions."""
    definitions: dict[str, TagDefinition] = {}
    events: list[RawFilterEvent] = []
    for category in tag_tree.get("tag_categories") or []:
        _collect_leaf_data(
            dict(category),
            ancestors=(),
            media=media,
            definitions=definitions,
            events=events,
        )
    events.sort(key=lambda item: (item.start_ms, item.end_ms, item.display_title))
    return events, definitions


def build_sidecar_payload(
    *,
    media_id: str,
    title: str,
    slug: str,
    events: list[RawFilterEvent],
) -> dict[str, Any]:
    """Convert raw exact events into merged Leapfrog sidecar segments."""
    merged_segments: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for event in sorted(
        (item for item in events if item.mapped_category),
        key=lambda item: (item.mapped_category or "", item.leaf_key, item.start_ms, item.end_ms),
    ):
        gap_ms = (
            DEFAULT_AUDIO_MERGE_GAP_MS
            if event.tag_type == "audio"
            else DEFAULT_VISUAL_MERGE_GAP_MS
        )
        start_ms = event.start_ms
        end_ms = max(event.end_ms, event.start_ms + DEFAULT_MIN_SEGMENT_MS)
        label_token = event.leaf_key
        if (
            current
            and current["category"] == event.mapped_category
            and current["leaf_key"] == event.leaf_key
            and start_ms <= current["end_ms"] + gap_ms
        ):
            current["end_ms"] = max(current["end_ms"], end_ms)
            current["labels"].add(label_token)
            if event.description:
                current["descriptions"].append(event.description)
            continue
        if current:
            merged_segments.append(_finalize_sidecar_segment(current, media_id=media_id))
        current = {
            "category": event.mapped_category,
            "leaf_key": event.leaf_key,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "labels": {label_token},
            "descriptions": [event.description] if event.description else [],
        }

    if current:
        merged_segments.append(_finalize_sidecar_segment(current, media_id=media_id))

    return {
        "format": "leapfrog.segment.sidecar/v1",
        "media_id": media_id,
        "title": title,
        "external_ids": {"vidangel_slug": slug, "vidangel_media_id": media_id},
        "segments": merged_segments,
    }


def _finalize_sidecar_segment(current: dict[str, Any], *, media_id: str) -> dict[str, Any]:
    descriptions = [value for value in current["descriptions"] if value]
    unique_descriptions: list[str] = []
    for description in descriptions:
        if description not in unique_descriptions:
            unique_descriptions.append(description)
        if len(unique_descriptions) == 3:
            break
    return {
        "media_id": media_id,
        "start_time": current["start_ms"] / 1000,
        "end_time": current["end_ms"] / 1000,
        "category": current["category"],
        "source": "vidangel",
        "confidence": None,
        "labels": ",".join(sorted(current["labels"])),
        "text_excerpt": " | ".join(unique_descriptions) if unique_descriptions else None,
        "created_at": None,
        "updated_at": None,
        "review_status": "pending",
    }


async def collect_catalog(client: VidAngelClient) -> tuple[list[WorkStub], dict[str, Any]]:
    """Discover a deduped set of works using a broad set of browse queries."""
    parameters = await client.fetch_work_parameters()
    works_by_id: dict[int, WorkStub] = {}
    query_results: list[dict[str, Any]] = []
    plans = build_catalog_queries(parameters)

    for query_index, plan in enumerate(plans, start=1):
        page = 0
        total_fetched = 0
        while True:
            params = dict(plan.params)
            params["offset"] = str(page * DEFAULT_LIMIT)
            payload = await client.fetch_works_page(params)
            results = payload.get("results") or []
            for result in results:
                try:
                    work = WorkStub.from_payload(dict(result))
                except ValueError as exc:
                    logger.warning("Skipping malformed VidAngel work payload %r: %s", result, exc)
                    continue
                works_by_id[work.id] = work
            total_fetched += len(results)
            page += 1
            next_link = payload.get("next")
            if next_link or len(results) == DEFAULT_LIMIT:
                if len(results) == DEFAULT_LIMIT and next_link is None and page < 50:
                    continue
            break
        logger.info(
            "Catalog query %s/%s %s fetched %s items; deduped works=%s",
            query_index,
            len(plans),
            plan.label,
            total_fetched,
            len(works_by_id),
        )
        query_results.append(
            {
                "label": plan.label,
                "params": plan.params,
                "items_fetched": total_fetched,
            }
        )

    works = sorted(works_by_id.values(), key=lambda item: (item.work_type, item.title.lower(), item.id))
    metadata = {
        "query_results": query_results,
        "query_count": len(query_results),
        "work_count": len(works),
    }
    return works, metadata


def _pick_service_slug(payload: dict[str, Any]) -> str:
    offerings = payload.get("offerings") or payload.get("offering_summaries") or []
    if offerings:
        first = offerings[0]
        catalog_id = first.get("catalog_id")
        if catalog_id:
            return str(catalog_id)
    groups = payload.get("offering_groups") or []
    for group in groups:
        catalogs = group.get("catalogs") or []
        if catalogs and catalogs[0].get("title"):
            return str(catalogs[0]["title"])
    return ""


def build_show_summaries(artifacts: list[TitleArtifact]) -> list[ShowSummary]:
    """Aggregate episode-level exports into one summary per TV series."""
    grouped: dict[tuple[str, str], list[TitleArtifact]] = {}
    for artifact in artifacts:
        if artifact.media_type != "episode":
            continue
        show_title = str(artifact.extra_metadata.get("show_title") or artifact.title or artifact.slug)
        show_slug = str(artifact.extra_metadata.get("show_slug") or artifact.slug)
        grouped.setdefault((show_title, show_slug), []).append(artifact)

    summaries: list[ShowSummary] = []
    for (show_title, show_slug), episodes in grouped.items():
        first = episodes[0]
        years = [episode.extra_metadata.get("year") for episode in episodes if episode.extra_metadata.get("year") is not None]
        ratings = [str(episode.extra_metadata.get("rating") or "") for episode in episodes if episode.extra_metadata.get("rating")]
        categories = {
            event.mapped_category
            for episode in episodes
            for event in episode.events
            if event.mapped_category
        }
        tag_set_ids = {
            parsed
            for episode in episodes
            for parsed in [_parse_optional_int(episode.summary.get("tag_set_id")) or 0]
            if parsed > 0
        }
        season_numbers = {
            parsed
            for episode in episodes
            for parsed in [_parse_optional_int(episode.season_number)]
            if parsed is not None
        }
        summaries.append(
            ShowSummary(
                show_title=show_title,
                show_slug=show_slug,
                service_slug=str(first.service_slug or ""),
                year=min(parsed_years) if (parsed_years := [parsed for year in years for parsed in [_parse_optional_int(year)] if parsed is not None]) else None,
                rating=ratings[0] if ratings else "",
                season_count=len(season_numbers),
                episode_count=len(episodes),
                event_count=sum(len(episode.events) for episode in episodes),
                category_count=len(categories),
                tag_set_count=len(tag_set_ids),
            )
        )
    return sorted(summaries, key=lambda item: (item.show_title.lower(), item.show_slug))


async def build_movie_artifact(
    client: VidAngelClient,
    work: WorkStub,
) -> tuple[TitleArtifact | None, dict[str, TagDefinition]]:
    """Fetch all export data for one movie work."""
    summary = await client.fetch_filter_summary(work.id)
    tag_set_id = _parse_optional_int(summary.get("tag_set_id")) or 0
    if tag_set_id <= 0:
        return None, {}
    tag_tree = await client.fetch_tag_tree(tag_set_id)
    media = {
        "media_id": str(work.id),
        "media_type": "movie",
        "title": work.title,
        "slug": work.slug,
        "service_slug": work.service_slug,
        "season_number": None,
        "episode_number": None,
        "tag_set_id": tag_set_id,
    }
    events, definitions = flatten_tag_tree(tag_tree, media=media)
    sidecar = build_sidecar_payload(media_id=str(work.id), title=work.title, slug=work.slug, events=events)
    artifact = TitleArtifact(
        media_id=str(work.id),
        media_type="movie",
        title=work.title,
        slug=work.slug,
        service_slug=work.service_slug,
        season_number=None,
        episode_number=None,
        summary=summary,
        events=tuple(events),
        sidecar_payload=sidecar,
        extra_metadata={
            "year": work.year,
            "rating": work.rating,
            "poster_url": work.poster_url,
        },
    )
    return artifact, definitions


async def build_show_artifacts(
    client: VidAngelClient,
    work: WorkStub,
) -> tuple[list[TitleArtifact], dict[str, TagDefinition]]:
    """Fetch all export data for one show work, producing one artifact per episode."""
    show = await client.fetch_show_details(work.slug)
    artifacts: list[TitleArtifact] = []
    definitions: dict[str, TagDefinition] = {}
    guide_by_season: dict[int, dict[str, Any]] = {}

    for season in show.get("seasons") or []:
        season_id = _parse_optional_int(season.get("id")) or 0
        if season_id and season_id not in guide_by_season:
            try:
                guide_by_season[season_id] = await client.fetch_season_guide(season_id)
            except Exception as exc:
                logger.warning("Could not fetch season guide for season_id=%s: %s", season_id, exc)
                guide_by_season[season_id] = {}
        for episode in season.get("episodes") or []:
            tag_set_id = 0
            service_slug = ""
            for offering in episode.get("offerings") or []:
                parsed_tag_set_id = _parse_optional_int(offering.get("tag_set_id")) or 0
                if parsed_tag_set_id:
                    tag_set_id = parsed_tag_set_id
                    service_slug = str(offering.get("catalog_id") or "")
                    break
            episode_id = _parse_optional_int(episode.get("id")) or 0
            if episode_id <= 0:
                logger.warning("Skipping episode with invalid id in show %s: %r", work.slug, episode)
                continue
            if tag_set_id <= 0:
                summary = await client.fetch_filter_summary(episode_id)
                tag_set_id = _parse_optional_int(summary.get("tag_set_id")) or 0
            else:
                summary = await client.fetch_filter_summary(episode_id)
            if tag_set_id <= 0:
                continue
            tag_tree = await client.fetch_tag_tree(tag_set_id)
            season_number = _parse_optional_int(episode.get("season_number"))
            if season_number is None:
                season_number = _parse_optional_int(season.get("number"))
            episode_number = _parse_optional_int(episode.get("episode_number"))
            media = {
                "media_id": str(episode_id),
                "media_type": "episode",
                "title": str(episode.get("title") or ""),
                "slug": f"{work.slug}-s{season.get('number')}-e{episode.get('episode_number')}",
                "service_slug": service_slug,
                "season_number": season_number,
                "episode_number": episode_number,
                "tag_set_id": tag_set_id,
            }
            episode_events, episode_definitions = flatten_tag_tree(tag_tree, media=media)
            definitions.update(episode_definitions)
            sidecar = build_sidecar_payload(
                media_id=str(episode["id"]),
                title=str(episode.get("player_overlay_title") or episode.get("title") or ""),
                slug=media["slug"],
                events=episode_events,
            )
            guide = guide_by_season.get(season_id, {})
            artifacts.append(
                TitleArtifact(
                    media_id=str(episode["id"]),
                    media_type="episode",
                    title=str(episode.get("player_overlay_title") or episode.get("title") or ""),
                    slug=media["slug"],
                    service_slug=service_slug,
                    season_number=media["season_number"],
                    episode_number=media["episode_number"],
                    summary=summary,
                    events=tuple(episode_events),
                    sidecar_payload=sidecar,
                    extra_metadata={
                        "show_title": str(show.get("title") or ""),
                        "show_slug": str(show.get("slug") or work.slug),
                        "year": episode.get("year"),
                        "rating": episode.get("rating"),
                        "runtime": episode.get("runtime"),
                        "age_guidance": guide.get("age_guidance"),
                        "season_synopsis": guide.get("synopsis"),
                    },
                )
            )
    return artifacts, definitions


async def export_vidangel_artifacts(
    *,
    output_dir: Path,
    concurrency: int = DEFAULT_CONCURRENCY,
    write_per_title_artifacts: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Fetch the VidAngel catalog and write master catalog/event exports."""
    authorization, profile_id, app_version, extra_headers = _resolve_auth_configuration()
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw_titles"
    sidecar_dir = output_dir / "sidecars"
    if write_per_title_artifacts:
        raw_dir.mkdir(parents=True, exist_ok=True)
        sidecar_dir.mkdir(parents=True, exist_ok=True)

    async with VidAngelClient(
        authorization=authorization,
        profile_id=profile_id,
        app_version=app_version,
        concurrency=concurrency,
        extra_headers=extra_headers,
        transport=transport,
    ) as client:
        works, catalog_metadata = await collect_catalog(client)
        logger.info("Discovered %s deduped catalog works", len(works))
        definitions: dict[str, TagDefinition] = {}
        artifacts: list[TitleArtifact] = []

        async def process_work(work: WorkStub) -> list[TitleArtifact]:
            if work.work_type == "show":
                show_artifacts, show_definitions = await build_show_artifacts(client, work)
                definitions.update(show_definitions)
                return show_artifacts
            artifact, movie_definitions = await build_movie_artifact(client, work)
            definitions.update(movie_definitions)
            return [artifact] if artifact else []

        total_works = len(works)
        for index, work in enumerate(works, start=1):
            try:
                artifacts.extend(await process_work(work))
            except Exception as exc:
                logger.warning("Skipping work id=%s slug=%s after fetch failure: %s", work.id, work.slug, exc)
            if index == 1 or index % 25 == 0 or index == total_works:
                logger.info(
                    "Processed %s/%s works; exported %s titles and %s definitions so far",
                    index,
                    total_works,
                    len(artifacts),
                    len(definitions),
                )

    raw_events = [event for artifact in artifacts for event in artifact.events]
    movie_artifacts = [artifact for artifact in artifacts if artifact.media_type == "movie"]
    episode_artifacts = [artifact for artifact in artifacts if artifact.media_type == "episode"]
    movie_events = [event for event in raw_events if event.media_type == "movie"]
    episode_events = [event for event in raw_events if event.media_type == "episode"]
    show_summaries = build_show_summaries(episode_artifacts)
    await _write_json(
        output_dir / "catalog_manifest.json",
        {
            "format": "leapfrog.vidangel.catalog/v1",
            "catalog_metadata": catalog_metadata,
            "title_count": len(artifacts),
            "event_count": len(raw_events),
            "definition_count": len(definitions),
            "titles": [artifact.summary_record() for artifact in artifacts],
        },
    )
    await _write_json(
        output_dir / "title_catalog.json",
        {
            "format": "leapfrog.vidangel.title-catalog/v1",
            "title_count": len(artifacts),
            "titles": [artifact.summary_record() for artifact in artifacts],
        },
    )
    await _write_json(
        output_dir / "movies_catalog.json",
        {
            "format": "leapfrog.vidangel.title-catalog/v1",
            "media_type": "movie",
            "title_count": len(movie_artifacts),
            "titles": [artifact.summary_record() for artifact in movie_artifacts],
        },
    )
    await _write_json(
        output_dir / "tv_catalog.json",
        {
            "format": "leapfrog.vidangel.title-catalog/v1",
            "media_type": "episode",
            "title_count": len(episode_artifacts),
            "titles": [artifact.summary_record() for artifact in episode_artifacts],
        },
    )
    await _write_json(
        output_dir / "shows_catalog.json",
        {
            "format": "leapfrog.vidangel.show-catalog/v1",
            "show_count": len(show_summaries),
            "shows": [summary.to_record() for summary in show_summaries],
        },
    )
    await _write_json(
        output_dir / "tag_definitions.json",
        {
            "format": "leapfrog.vidangel.tag-definitions/v1",
            "definition_count": len(definitions),
            "definitions": [definition.to_record() for definition in sorted(definitions.values(), key=lambda item: item.key)],
        },
    )
    await _write_title_catalog_csv(output_dir / "title_catalog.csv", artifacts)
    await _write_title_catalog_csv(output_dir / "movies_catalog.csv", movie_artifacts)
    await _write_title_catalog_csv(output_dir / "tv_catalog.csv", episode_artifacts)
    await _write_show_catalog_csv(output_dir / "shows_catalog.csv", show_summaries)
    await _write_csv(output_dir / "raw_filter_events.csv", raw_events)
    # Persist a compact title-to-byte-span index so the browser never has to
    # parse the multi-million-row raw event export for each title selection.
    await _load_raw_event_index(output_dir)
    await _write_csv(output_dir / "movie_filter_events.csv", movie_events)
    await _write_csv(output_dir / "tv_filter_events.csv", episode_events)
    await _write_tag_definitions_csv(
        output_dir / "tag_definitions.csv",
        sorted(definitions.values(), key=lambda item: item.key),
    )
    await _write_xlsx_workbook(
        output_dir / "vidangel_catalog.xlsx",
        {
            "movies_catalog": _title_catalog_rows(movie_artifacts),
            "tv_catalog": _title_catalog_rows(episode_artifacts),
            "shows_catalog": _show_catalog_rows(show_summaries),
            "movie_filter_events": _event_rows(movie_events),
            "tv_filter_events": _event_rows(episode_events),
            "tag_definitions": _tag_definition_rows(sorted(definitions.values(), key=lambda item: item.key)),
        },
    )

    if write_per_title_artifacts:
        for artifact in artifacts:
            stem = _sanitize_filename(
                f"{artifact.media_type}-{artifact.slug or artifact.media_id}-{artifact.media_id}"
            )
            await _write_json(raw_dir / f"{stem}.json", artifact.raw_record())
            await _write_json(sidecar_dir / f"{stem}.leapfrog.json", artifact.sidecar_payload)

    return {
        "work_count": catalog_metadata["work_count"],
        "title_count": len(artifacts),
        "event_count": len(raw_events),
        "definition_count": len(definitions),
        "output_dir": str(output_dir),
        "write_per_title_artifacts": write_per_title_artifacts,
    }


async def _write_json(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True)
    await asyncio.to_thread(path.write_text, f"{text}\n", "utf-8")


async def _write_csv(path: Path, events: list[RawFilterEvent]) -> None:
    fieldnames = [
        "media_id",
        "media_type",
        "title",
        "slug",
        "service_slug",
        "season_number",
        "episode_number",
        "tag_set_id",
        "path_keys",
        "path_titles",
        "display_title",
        "description",
        "tag_type",
        "start_ms",
        "end_ms",
        "mapped_category",
        "leaf_key",
    ]

    def _write() -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for event in events:
                writer.writerow(
                    {
                        "media_id": event.media_id,
                        "media_type": event.media_type,
                        "title": event.title,
                        "slug": event.slug,
                        "service_slug": event.service_slug,
                        "season_number": event.season_number or "",
                        "episode_number": event.episode_number or "",
                        "tag_set_id": event.tag_set_id,
                        "path_keys": "/".join(event.path_keys),
                        "path_titles": " > ".join(event.path_titles),
                        "display_title": event.display_title,
                        "description": event.description or "",
                        "tag_type": event.tag_type,
                        "start_ms": event.start_ms,
                        "end_ms": event.end_ms,
                        "mapped_category": event.mapped_category or "",
                        "leaf_key": event.leaf_key,
                    }
                )

    await asyncio.to_thread(_write)


async def _write_title_catalog_csv(path: Path, artifacts: list[TitleArtifact]) -> None:
    rows = _title_catalog_rows(artifacts)
    fieldnames = list(rows[0].keys()) if rows else [
        "media_id",
        "media_type",
        "title",
        "slug",
        "service_slug",
        "season_number",
        "episode_number",
        "event_count",
        "category_count",
        "tag_set_id",
        "show_title",
        "show_slug",
        "year",
        "rating",
        "runtime",
        "poster_url",
    ]

    def _write() -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    await asyncio.to_thread(_write)


async def _write_tag_definitions_csv(path: Path, definitions: list[TagDefinition]) -> None:
    rows = _tag_definition_rows(definitions)
    fieldnames = list(rows[0].keys()) if rows else [
        "category_id",
        "key",
        "display_title",
        "path_keys",
        "path_titles",
        "default_type",
        "mapped_category",
        "example_description",
    ]

    def _write() -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    await asyncio.to_thread(_write)


async def _write_show_catalog_csv(path: Path, summaries: list[ShowSummary]) -> None:
    rows = _show_catalog_rows(summaries)
    fieldnames = list(rows[0].keys()) if rows else [
        "show_title",
        "show_slug",
        "service_slug",
        "year",
        "rating",
        "season_count",
        "episode_count",
        "event_count",
        "category_count",
        "tag_set_count",
    ]

    def _write() -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    await asyncio.to_thread(_write)


def _title_catalog_rows(artifacts: list[TitleArtifact]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for artifact in artifacts:
        summary = artifact.summary_record()
        extra = artifact.extra_metadata
        rows.append(
            {
                "media_id": artifact.media_id,
                "media_type": artifact.media_type,
                "title": artifact.title,
                "slug": artifact.slug,
                "service_slug": artifact.service_slug,
                "season_number": artifact.season_number or "",
                "episode_number": artifact.episode_number or "",
                "event_count": summary["event_count"],
                "category_count": summary["category_count"],
                "tag_set_id": summary["tag_set_id"] or "",
                "show_title": extra.get("show_title", ""),
                "show_slug": extra.get("show_slug", ""),
                "year": extra.get("year", ""),
                "rating": extra.get("rating", ""),
                "runtime": extra.get("runtime", ""),
                "poster_url": extra.get("poster_url", ""),
            }
        )
    return rows


def _show_catalog_rows(summaries: list[ShowSummary]) -> list[dict[str, Any]]:
    return [summary.to_record() for summary in summaries]


def _tag_definition_rows(definitions: list[TagDefinition]) -> list[dict[str, Any]]:
    return [
        {
            "category_id": definition.category_id,
            "key": definition.key,
            "display_title": definition.display_title,
            "path_keys": "/".join(definition.path_keys),
            "path_titles": " > ".join(definition.path_titles),
            "default_type": definition.default_type,
            "mapped_category": definition.mapped_category or "",
            "example_description": definition.example_description or "",
        }
        for definition in definitions
    ]


def _event_rows(events: list[RawFilterEvent]) -> list[dict[str, Any]]:
    return [
        {
            "media_id": event.media_id,
            "media_type": event.media_type,
            "title": event.title,
            "slug": event.slug,
            "service_slug": event.service_slug,
            "season_number": event.season_number or "",
            "episode_number": event.episode_number or "",
            "tag_set_id": event.tag_set_id,
            "path_keys": "/".join(event.path_keys),
            "path_titles": " > ".join(event.path_titles),
            "display_title": event.display_title,
            "description": event.description or "",
            "tag_type": event.tag_type,
            "start_ms": event.start_ms,
            "end_ms": event.end_ms,
            "mapped_category": event.mapped_category or "",
            "leaf_key": event.leaf_key,
        }
        for event in events
    ]


async def _write_xlsx_workbook(path: Path, sheets: dict[str, list[dict[str, Any]]]) -> None:
    await asyncio.to_thread(_create_simple_xlsx, path, sheets)


def _create_simple_xlsx(path: Path, sheets: dict[str, list[dict[str, Any]]]) -> None:
    normalized_sheets: list[tuple[str, list[dict[str, Any]]]] = []
    for index, (name, rows) in enumerate(sheets.items(), start=1):
        normalized_sheets.append((_safe_sheet_name(name, index=index), rows))

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _xlsx_content_types_xml(len(normalized_sheets)))
        archive.writestr("_rels/.rels", _xlsx_root_rels_xml())
        archive.writestr("xl/workbook.xml", _xlsx_workbook_xml([name for name, _ in normalized_sheets]))
        archive.writestr("xl/_rels/workbook.xml.rels", _xlsx_workbook_rels_xml(len(normalized_sheets)))
        archive.writestr("xl/styles.xml", _xlsx_styles_xml())
        for index, (_, rows) in enumerate(normalized_sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _xlsx_sheet_xml(rows))


def _safe_sheet_name(value: str, *, index: int) -> str:
    cleaned = re.sub(r"[:\\\\/?*\\[\\]]", "_", value).strip() or f"Sheet{index}"
    return cleaned[:31]


def _xlsx_content_types_xml(sheet_count: int) -> str:
    overrides = [
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
    ]
    overrides.extend(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'{"".join(overrides)}'
        "</Types>"
    )


def _xlsx_root_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )


def _xlsx_workbook_xml(sheet_names: list[str]) -> str:
    sheets = "".join(
        f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(sheet_names, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheets}</sheets>"
        "</workbook>"
    )


def _xlsx_workbook_rels_xml(sheet_count: int) -> str:
    rels = [
        f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, sheet_count + 1)
    ]
    rels.append(
        f'<Relationship Id="rId{sheet_count + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'{"".join(rels)}'
        "</Relationships>"
    )


def _xlsx_styles_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )


def _xlsx_sheet_xml(rows: list[dict[str, Any]]) -> str:
    headers = list(rows[0].keys()) if rows else []
    all_rows = [headers] + [[row.get(header, "") for header in headers] for row in rows]
    sheet_rows: list[str] = []
    for row_index, row_values in enumerate(all_rows, start=1):
        cells = [
            _xlsx_cell_xml(f"{_xlsx_column_name(col_index)}{row_index}", value)
            for col_index, value in enumerate(row_values, start=1)
        ]
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    dimension = "A1"
    if headers:
        dimension = f"A1:{_xlsx_column_name(len(headers))}{len(all_rows)}"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="{dimension}"/>'
        '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/>'
        f'<sheetData>{"".join(sheet_rows)}</sheetData>'
        '</worksheet>'
    )


def _xlsx_column_name(index: int) -> str:
    name = ""
    current = index
    while current > 0:
        current, remainder = divmod(current - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _xlsx_cell_xml(cell_ref: str, value: Any) -> str:
    if value is None or value == "":
        return f'<c r="{cell_ref}"/>'
    if isinstance(value, bool):
        return f'<c r="{cell_ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, int | float):
        return f'<c r="{cell_ref}"><v>{value}</v></c>'
    return f'<c r="{cell_ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        default=str(get_vidangel_export_dir()),
        help="Directory for raw JSON, CSV, definitions, and sidecars.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("LEAPFROG_VIDANGEL_CONCURRENCY", str(DEFAULT_CONCURRENCY))),
        help="Maximum concurrent HTTP requests.",
    )
    parser.add_argument(
        "--write-per-title-artifacts",
        action="store_true",
        help="Also write one raw JSON and one Leapfrog sidecar per exported title.",
    )
    export_skp_parser = parser.add_subparsers(dest="command")
    skp_parser = export_skp_parser.add_parser(
        "export-skp",
        help="Export a filtered VidAngel title to native Clean Media Player .skp JSON.",
    )
    skp_parser.add_argument("--media-id", required=True, help="VidAngel media_id to export.")
    skp_parser.add_argument(
        "--leaf-key",
        action="append",
        required=True,
        help="VidAngel leaf filter key to include. Repeat for multiple filters.",
    )
    skp_parser.add_argument(
        "--export-dir",
        help="Directory containing the VidAngel export artifacts. Defaults to the configured export directory.",
    )
    skp_parser.add_argument("--output", help="Optional output .skp path. Writes to stdout when omitted.")
    prepare_parser = export_skp_parser.add_parser(
        "prepare",
        help="Index and cache existing export files without contacting VidAngel.",
    )
    prepare_parser.add_argument(
        "--export-dir",
        help="Directory containing existing VidAngel export artifacts.",
    )
    return parser


async def _amain() -> int:
    setup_logging(level=os.environ.get("LEAPFROG_LOG_LEVEL", "INFO"))
    args = _build_arg_parser().parse_args()
    if getattr(args, "command", None) == "prepare":
        result = await prepare_vidangel_export(
            Path(args.export_dir) if args.export_dir else get_vidangel_export_dir()
        )
        logger.info(
            "VidAngel export preparation completed: titles=%s events=%s definitions=%s index=%s",
            result["indexed_title_count"],
            result["event_count"],
            result["definition_count"],
            result["index_path"],
        )
        return 0
    if getattr(args, "command", None) == "export-skp":
        result = await generate_filtered_vidangel_skp(
            args.media_id,
            args.leaf_key,
            export_dir=Path(args.export_dir) if getattr(args, "export_dir", None) else get_vidangel_export_dir(),
            output_path=args.output,
        )
        if not args.output:
            print(result["skp_text"], end="")
        logger.info(
            "VidAngel filtered SKP export completed: media_id=%s selected=%s output=%s",
            args.media_id,
            result["selected_event_count"],
            result["output_path"] or "stdout",
        )
        return 0
    result = await export_vidangel_artifacts(
        output_dir=Path(args.output_dir),
        concurrency=max(1, args.concurrency),
        write_per_title_artifacts=bool(args.write_per_title_artifacts),
    )
    logger.info(
        "VidAngel export completed: works=%s titles=%s events=%s definitions=%s output=%s",
        result["work_count"],
        result["title_count"],
        result["event_count"],
        result["definition_count"],
        result["output_dir"],
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
