"""Match VidAngel export artifacts to Plex titles and write adjacent sidecars."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import database as db
from .logger import get_logger, setup_logging
from .paths import get_data_dir
from .segment_export import write_sidecar_file
from .vidangel_export import (
    RawFilterEvent,
    build_sidecar_payload,
    get_vidangel_export_dir,
    load_vidangel_exportable_title_catalog_records,
    load_vidangel_raw_event_records,
)
import leapfrog.plex_client as plex_mod

logger = get_logger(__name__)

_REPORT_FIELDNAMES = [
    "plex_guid",
    "show_guid",
    "file_path",
    "media_type",
    "title",
    "show_title",
    "season_number",
    "episode_number",
    "status",
    "detail",
    "matched_media_id",
    "matched_slug",
    "sidecar_path",
    "event_count",
]


@dataclass(frozen=True, slots=True)
class SidecarGenerationResult:
    """Outcome for one matched or skipped Plex job."""

    plex_guid: str
    file_path: str
    media_type: str
    title: str
    status: str
    detail: str
    show_guid: str | None = None
    show_title: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    matched_media_id: str | None = None
    matched_slug: str | None = None
    sidecar_path: str | None = None
    event_count: int = 0

    def to_record(self) -> dict[str, Any]:
        return {
            "plex_guid": self.plex_guid,
            "show_guid": self.show_guid,
            "file_path": self.file_path,
            "media_type": self.media_type,
            "title": self.title,
            "show_title": self.show_title,
            "season_number": self.season_number,
            "episode_number": self.episode_number,
            "status": self.status,
            "detail": self.detail,
            "matched_media_id": self.matched_media_id,
            "matched_slug": self.matched_slug,
            "sidecar_path": self.sidecar_path,
            "event_count": self.event_count,
        }


def _normalize_title(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", (value or "").lower())
    return " ".join(cleaned.split())


def _parse_json_file(path: Path, list_key: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get(list_key)
    return [dict(row) for row in rows] if isinstance(rows, list) else []


async def _write_json(path: Path, payload: dict[str, Any]) -> None:
    await asyncio.to_thread(path.write_text, f"{json.dumps(payload, indent=2, sort_keys=True)}\n", "utf-8")


async def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = list(rows[0].keys()) if rows else list(_REPORT_FIELDNAMES)

    def _write() -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    await asyncio.to_thread(_write)


async def _load_raw_events_async(export_dir: Path) -> dict[tuple[str, str], list[RawFilterEvent]]:
    """Load raw VidAngel events using the shared export parser."""
    return await load_vidangel_raw_event_records(export_dir)


def _load_movie_catalog(export_dir: Path) -> list[dict[str, Any]]:
    return _parse_json_file(export_dir / "movies_catalog.json", "titles")


def _load_tv_catalog(export_dir: Path) -> list[dict[str, Any]]:
    return _parse_json_file(export_dir / "tv_catalog.json", "titles")


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


def _load_raw_events(export_dir: Path) -> dict[tuple[str, str], list[RawFilterEvent]]:
    source = export_dir / "raw_filter_events.csv"
    if not source.exists():
        raise FileNotFoundError(source)
    if source.stat().st_size == 0:
        return {}

    grouped: dict[tuple[str, str], list[RawFilterEvent]] = {}
    with source.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            media_type = str(row.get("media_type") or "").strip()
            media_id = str(row.get("media_id") or "").strip()
            try:
                event = RawFilterEvent(
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
            except ValueError as exc:
                logger.warning(
                    "Skipping malformed VidAngel event row from %s for media_id=%r title=%r: %s",
                    source,
                    media_id,
                    row.get("title"),
                    exc,
                )
                continue
            grouped.setdefault((media_type, media_id), []).append(event)
    return grouped


def _build_movie_index(rows: list[dict[str, Any]]) -> dict[tuple[str, int | None], list[dict[str, Any]]]:
    indexed: dict[tuple[str, int | None], list[dict[str, Any]]] = {}
    for row in rows:
        extra_metadata = row.get("extra_metadata") if isinstance(row.get("extra_metadata"), dict) else {}
        year = _parse_optional_int(extra_metadata.get("year"))
        key = (_normalize_title(str(row.get("title") or "")), year)
        indexed.setdefault(key, []).append(row)
    return indexed


def _build_episode_index(rows: list[dict[str, Any]]) -> dict[tuple[str, int, int], list[dict[str, Any]]]:
    indexed: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
    for row in rows:
        extra = row.get("extra_metadata") if isinstance(row.get("extra_metadata"), dict) else {}
        show_title = _normalize_title(str(extra.get("show_title") or row.get("show_title") or ""))
        season_number = _parse_optional_int(row.get("season_number"))
        episode_number = _parse_optional_int(row.get("episode_number"))
        if not show_title or season_number is None or episode_number is None:
            continue
        key = (show_title, season_number, episode_number)
        indexed.setdefault(key, []).append(row)
    return indexed


def match_movie_catalog_row(job: dict[str, Any], movie_index: dict[tuple[str, int | None], list[dict[str, Any]]]) -> tuple[dict[str, Any] | None, str]:
    title_key = _normalize_title(str(job.get("title") or ""))
    year = _parse_optional_int(job.get("year"))
    exact = movie_index.get((title_key, year), []) if title_key else []
    if len(exact) == 1:
        return exact[0], "exact title/year match"
    if len(exact) > 1:
        return None, "ambiguous movie title/year match"
    title_only_matches = [
        row
        for (candidate_title, _candidate_year), values in movie_index.items()
        if candidate_title == title_key
        for row in values
    ]
    if len(title_only_matches) == 1:
        return title_only_matches[0], "title-only movie match"
    if len(title_only_matches) > 1:
        return None, "ambiguous movie title match"
    return None, "no movie match"


def match_episode_catalog_row(
    *,
    show_title: str,
    season_number: int | None,
    episode_number: int | None,
    episode_index: dict[tuple[str, int, int], list[dict[str, Any]]],
) -> tuple[dict[str, Any] | None, str]:
    if not show_title or season_number is None or episode_number is None:
        return None, "missing episode match metadata"
    key = (_normalize_title(show_title), season_number, episode_number)
    matches = episode_index.get(key, [])
    if len(matches) == 1:
        return matches[0], "exact show/season/episode match"
    if len(matches) > 1:
        return None, "ambiguous episode match"
    return None, "no episode match"


def _library_episode_metadata(job: dict[str, Any]) -> tuple[str, str, int | None, int | None]:
    title_parts = re.split(r"\s+[\u2013\u2014]\s+", str(job.get("title") or ""), maxsplit=2)
    show_title = title_parts[0] if len(title_parts) == 3 else ""
    episode_title = title_parts[2] if len(title_parts) == 3 else ""

    season_number = None
    if len(title_parts) == 3:
        season_match = re.search(r"\b(\d+)\b", title_parts[1])
        season_number = int(season_match.group(1)) if season_match else None

    episode_number = None
    filename_match = re.search(
        r"(?i)\bS(\d{1,3})E(\d{1,4})\b",
        Path(str(job.get("file_path") or "")).name,
    )
    if filename_match:
        season_number = int(filename_match.group(1))
        episode_number = int(filename_match.group(2))
    return show_title, episode_title, season_number, episode_number


def filter_vidangel_catalog_to_library(
    catalog_rows: list[dict[str, Any]],
    library_jobs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return catalog rows that match a movie or episode in the local library."""
    movie_index = _build_movie_index(
        [row for row in catalog_rows if str(row.get("media_type") or "") == "movie"]
    )
    episode_rows = [
        row for row in catalog_rows if str(row.get("media_type") or "") != "movie"
    ]
    episode_index = _build_episode_index(episode_rows)
    episode_title_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    episode_season_title_index: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
    for row in episode_rows:
        extra = row.get("extra_metadata") if isinstance(row.get("extra_metadata"), dict) else {}
        show_title = _normalize_title(str(row.get("show_title") or extra.get("show_title") or ""))
        episode_title = _normalize_title(str(row.get("title") or ""))
        if show_title and episode_title:
            episode_title_index.setdefault((show_title, episode_title), []).append(row)
            season_number = _parse_optional_int(row.get("season_number"))
            if season_number is not None:
                episode_season_title_index.setdefault(
                    (show_title, season_number, episode_title),
                    [],
                ).append(row)

    matched_keys: set[tuple[str, str]] = set()
    for job in library_jobs:
        media_type = str(job.get("media_type") or "movie")
        match: dict[str, Any] | None = None
        if media_type == "episode":
            show_title, episode_title, season_number, episode_number = _library_episode_metadata(job)
            match, _ = match_episode_catalog_row(
                show_title=show_title,
                season_number=season_number,
                episode_number=episode_number,
                episode_index=episode_index,
            )
            if match is None and show_title and episode_title:
                normalized_show_title = _normalize_title(show_title)
                normalized_episode_title = _normalize_title(episode_title)
                if season_number is not None:
                    title_matches = episode_season_title_index.get(
                        (normalized_show_title, season_number, normalized_episode_title),
                        [],
                    )
                else:
                    title_matches = episode_title_index.get(
                        (normalized_show_title, normalized_episode_title),
                        [],
                    )
                if len(title_matches) == 1:
                    match = title_matches[0]
        else:
            match, _ = match_movie_catalog_row(job, movie_index)
        if match is not None:
            matched_keys.add(
                (
                    str(match.get("media_type") or media_type),
                    str(match.get("media_id") or ""),
                )
            )

    return [
        row
        for row in catalog_rows
        if (
            str(row.get("media_type") or ""),
            str(row.get("media_id") or ""),
        )
        in matched_keys
    ]


async def load_vidangel_library_title_catalog_records(
    export_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Return exportable VidAngel titles matched to the persisted local library."""
    catalog_rows = await load_vidangel_exportable_title_catalog_records(export_dir)
    if not catalog_rows:
        return []
    matched_rows = filter_vidangel_catalog_to_library(catalog_rows, await db.get_scan_jobs())
    result = []
    for row in matched_rows:
        extra = row.get("extra_metadata") if isinstance(row.get("extra_metadata"), dict) else {}
        result.append(
            {
                **row,
                "show_title": row.get("show_title") or extra.get("show_title"),
                "year": row.get("year") or extra.get("year"),
                "rating": row.get("rating") or extra.get("rating"),
            }
        )
    return result


def _build_report_stem(base_name: str, media_group: str) -> str:
    return base_name.replace("_plex_titles", f"_{media_group}_plex_titles")


def _split_rows_by_media_type(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "movie": [row for row in rows if str(row.get("media_type") or "") == "movie"],
        "tv": [row for row in rows if str(row.get("media_type") or "") != "movie"],
    }


def _common_file_path(rows: list[dict[str, Any]]) -> str:
    file_paths = [str(row.get("file_path") or "") for row in rows if str(row.get("file_path") or "").strip()]
    if not file_paths:
        return ""
    try:
        return os.path.commonpath(file_paths)
    except ValueError:
        return file_paths[0]


def _collapse_show_level_no_filter_rows(
    rows: list[dict[str, Any]],
    episode_index: dict[tuple[str, int, int], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    # Collapse only fully uncovered shows; partially covered shows stay at the
    # episode level so the missing episodes remain actionable.
    shows_with_catalog_filters = {show_title for show_title, _, _ in episode_index}
    passthrough: list[dict[str, Any]] = []
    grouped_tv_rows: dict[str, list[dict[str, Any]]] = {}

    for row in rows:
        media_type = str(row.get("media_type") or "")
        if media_type == "movie":
            passthrough.append(row)
            continue
        show_key = _normalize_title(str(row.get("show_title") or ""))
        if not show_key or str(row.get("detail") or "") != "no episode match":
            passthrough.append(row)
            continue
        grouped_tv_rows.setdefault(show_key, []).append(row)

    collapsed_rows = list(passthrough)
    for show_key, show_rows in grouped_tv_rows.items():
        if show_key in shows_with_catalog_filters:
            collapsed_rows.extend(show_rows)
            continue
        first = show_rows[0]
        collapsed_rows.append(
            {
                **first,
                "plex_guid": str(first.get("show_guid") or first.get("plex_guid") or ""),
                "file_path": _common_file_path(show_rows),
                "media_type": "show",
                "title": str(first.get("show_title") or first.get("title") or ""),
                "show_title": str(first.get("show_title") or first.get("title") or ""),
                "season_number": None,
                "episode_number": None,
                "detail": "no VidAngel filters for show",
            }
        )

    return sorted(
        collapsed_rows,
        key=lambda row: (
            str(row.get("media_type") or ""),
            str(row.get("show_title") or ""),
            str(row.get("title") or ""),
            str(row.get("file_path") or ""),
        ),
    )


def _dedupe_report_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for row in rows:
        key = (
            str(row.get("plex_guid") or ""),
            str(row.get("media_type") or ""),
            str(row.get("title") or ""),
            str(row.get("status") or ""),
            str(row.get("detail") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


async def _write_report_set(source_dir: Path, base_name: str, rows: list[dict[str, Any]]) -> dict[str, str]:
    files = {
        f"{base_name}_json": str(source_dir / f"{base_name}.json"),
        f"{base_name}_csv": str(source_dir / f"{base_name}.csv"),
    }
    await _write_json(source_dir / f"{base_name}.json", {"count": len(rows), "titles": rows})
    await _write_csv(source_dir / f"{base_name}.csv", rows)

    for media_group, media_rows in _split_rows_by_media_type(rows).items():
        report_stem = _build_report_stem(base_name, media_group)
        files[f"{report_stem}_json"] = str(source_dir / f"{report_stem}.json")
        files[f"{report_stem}_csv"] = str(source_dir / f"{report_stem}.csv")
        await _write_json(source_dir / f"{report_stem}.json", {"count": len(media_rows), "titles": media_rows})
        await _write_csv(source_dir / f"{report_stem}.csv", media_rows)

    return files


async def generate_vidangel_sidecars(
    *,
    library_id: str | None = None,
    export_dir: Path | None = None,
    only_missing: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Match VidAngel export artifacts to Plex titles and write adjacent sidecars."""
    source_dir = export_dir or get_vidangel_export_dir()
    movie_index = _build_movie_index(_load_movie_catalog(source_dir))
    episode_index = _build_episode_index(_load_tv_catalog(source_dir))
    event_index = await _load_raw_events_async(source_dir)

    jobs = await db.get_scan_jobs_by_library(library_id) if library_id else await db.get_scan_jobs()
    if not jobs:
        return {"library_id": library_id, "results": [], "summary": {"total": 0, "written": 0, "matched": 0}}

    try:
        client = plex_mod.get_client()
    except RuntimeError:
        client = None

    results: list[SidecarGenerationResult] = []
    for job in jobs:
        file_path = str(job.get("file_path") or "")
        media_type = str(job.get("media_type") or "movie")
        title = str(job.get("title") or "")
        show_guid = str(job.get("show_guid") or "")
        show_title = ""
        season_number = None
        episode_number = None
        if not file_path:
            results.append(
                SidecarGenerationResult(
                    plex_guid=str(job.get("plex_guid") or ""),
                    file_path="",
                    media_type=media_type,
                    title=title,
                    status="skipped",
                    detail="missing file path",
                    show_guid=show_guid or None,
                )
            )
            continue
        media_path = Path(file_path)
        media_dir = media_path.parent
        if not media_dir.exists():
            results.append(
                SidecarGenerationResult(
                    plex_guid=str(job.get("plex_guid") or ""),
                    file_path=file_path,
                    media_type=media_type,
                    title=title,
                    status="skipped",
                    detail="media directory does not exist",
                    show_guid=show_guid or None,
                )
            )
            continue
        if not media_path.exists():
            results.append(
                SidecarGenerationResult(
                    plex_guid=str(job.get("plex_guid") or ""),
                    file_path=file_path,
                    media_type=media_type,
                    title=title,
                    status="skipped",
                    detail="media file does not exist",
                    show_guid=show_guid or None,
                )
            )
            continue
        sidecar_path = str(media_path.with_suffix(".leapfrog.json"))
        if only_missing and Path(sidecar_path).exists():
            results.append(
                SidecarGenerationResult(
                    plex_guid=str(job.get("plex_guid") or ""),
                    file_path=file_path,
                    media_type=media_type,
                    title=title,
                    status="skipped",
                    detail="sidecar already exists",
                    show_guid=show_guid or None,
                    sidecar_path=sidecar_path,
                )
            )
            continue

        match_row: dict[str, Any] | None = None
        match_detail = ""
        if media_type == "episode":
            if client and job.get("rating_key"):
                show_title, season_number, episode_number = await client.get_episode_match_info(str(job["rating_key"]))
            match_row, match_detail = match_episode_catalog_row(
                show_title=show_title,
                season_number=season_number,
                episode_number=episode_number,
                episode_index=episode_index,
            )
        else:
            match_row, match_detail = match_movie_catalog_row(job, movie_index)

        if not match_row:
            results.append(
                SidecarGenerationResult(
                    plex_guid=str(job.get("plex_guid") or ""),
                    file_path=file_path,
                    media_type=media_type,
                    title=title,
                    status="unmatched",
                    detail=match_detail,
                    show_guid=show_guid or None,
                    show_title=show_title or None,
                    season_number=season_number,
                    episode_number=episode_number,
                )
            )
            continue

        matched_media_id = str(match_row.get("media_id") or "")
        event_key = (str(match_row.get("media_type") or media_type), matched_media_id)
        events = event_index.get(event_key, [])
        payload = build_sidecar_payload(
            media_id=matched_media_id,
            title=str(match_row.get("title") or title),
            slug=str(match_row.get("slug") or ""),
            events=events,
        )
        if not dry_run:
            try:
                await write_sidecar_file(sidecar_path, payload)
            except OSError as exc:
                results.append(
                    SidecarGenerationResult(
                        plex_guid=str(job.get("plex_guid") or ""),
                        file_path=file_path,
                        media_type=media_type,
                        title=title,
                        status="skipped",
                        detail=f"failed to write sidecar: {exc.strerror or exc}",
                        show_guid=show_guid or None,
                        show_title=show_title or None,
                        season_number=season_number,
                        episode_number=episode_number,
                        matched_media_id=matched_media_id,
                        matched_slug=str(match_row.get("slug") or ""),
                        sidecar_path=sidecar_path,
                        event_count=len(events),
                    )
                )
                continue
        results.append(
            SidecarGenerationResult(
                plex_guid=str(job.get("plex_guid") or ""),
                file_path=file_path,
                media_type=media_type,
                title=title,
                status="written" if not dry_run else "matched",
                detail=match_detail,
                show_guid=show_guid or None,
                show_title=show_title or None,
                season_number=season_number,
                episode_number=episode_number,
                matched_media_id=matched_media_id,
                matched_slug=str(match_row.get("slug") or ""),
                sidecar_path=sidecar_path,
                event_count=len(events),
            )
        )

    summary = {
        "total": len(results),
        "written": sum(1 for result in results if result.status == "written"),
        "matched": sum(1 for result in results if result.status in {"written", "matched"}),
        "unmatched": sum(1 for result in results if result.status == "unmatched"),
        "skipped": sum(1 for result in results if result.status == "skipped"),
    }
    result_rows = [result.to_record() for result in results]
    matched_rows = [row for row in result_rows if row["status"] in {"written", "matched"}]
    unmatched_rows = _collapse_show_level_no_filter_rows(
        [row for row in result_rows if row["status"] == "unmatched"],
        episode_index,
    )
    ambiguous_rows = [row for row in unmatched_rows if str(row.get("detail") or "").startswith("ambiguous")]
    skipped_rows = [row for row in result_rows if row["status"] == "skipped"]
    out_rows = _collapse_show_level_no_filter_rows(
        _dedupe_report_rows(unmatched_rows + ambiguous_rows + skipped_rows),
        episode_index,
    )
    report_files: dict[str, str] = {}
    for base_name, rows in (
        ("matched_plex_titles", matched_rows),
        ("unmatched_plex_titles", unmatched_rows),
        ("ambiguous_plex_titles", ambiguous_rows),
        ("skipped_plex_titles", skipped_rows),
        ("out_plex_titles", out_rows),
    ):
        report_files.update(await _write_report_set(source_dir, base_name, rows))
    return {
        "library_id": library_id,
        "export_dir": str(source_dir),
        "only_missing": only_missing,
        "dry_run": dry_run,
        "summary": summary,
        "report_files": report_files,
        "results": result_rows,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library-id", help="Optional Plex library id to limit sidecar generation.")
    parser.add_argument(
        "--export-dir",
        default=str(get_vidangel_export_dir()),
        help="Directory containing the VidAngel export database files.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Match titles without writing sidecars.")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Rewrite sidecars even when a .leapfrog.json file already exists.",
    )
    return parser


async def _amain() -> int:
    setup_logging(level="INFO")
    args = _build_arg_parser().parse_args()
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    db.set_db_path(data_dir / "leapfrog.db")
    await db.init_db()
    result = await generate_vidangel_sidecars(
        library_id=args.library_id,
        export_dir=Path(args.export_dir),
        only_missing=not args.overwrite,
        dry_run=bool(args.dry_run),
    )
    logger.info("VidAngel sidecar generation completed: %s", result["summary"])
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
