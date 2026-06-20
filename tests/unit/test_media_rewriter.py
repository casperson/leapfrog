"""Unit tests for VidAngel-taxonomy media rewrite planning."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leapfrog import database as db
import leapfrog.media_rewriter as media_rewriter
from leapfrog.media_rewriter import (
    RewriteEligibilityError,
    _rewrite_subtitle_cues,
    build_rewrite_plan,
    discover_rewrite_candidates,
    execute_rewrite_plan,
    get_leaf_keys_for_user_profile,
)
from leapfrog.subtitles import SubtitleCue


pytestmark = pytest.mark.usefixtures("setup_db")


async def _seed_movie(tmp_path: Path, *, labels: str = "fuck", text_excerpt: str | None = None) -> Path:
    media_path = tmp_path / "Sample Movie (2019).mp4"
    media_path.write_bytes(b"source-bytes")
    await db.upsert_scan_job(
        plex_guid="movie-1",
        title="Sample Movie",
        file_path=str(media_path),
        rating_key="rk-1",
        library_id="1",
        library_title="Movies",
        media_type="movie",
        content_rating="R",
        year=2019,
        show_guid="",
    )
    sidecar = media_path.with_suffix(".leapfrog.json")
    sidecar.write_text(
        json.dumps(
            {
                "format": "leapfrog.segment.sidecar/v1",
                "media_id": "movie-1",
                "title": "Sample Movie",
                "segments": [
                    {
                        "media_id": "movie-1",
                        "start_time": 1,
                        "end_time": 3,
                        "category": "language_profanity",
                        "source": "sidecar",
                        "labels": labels,
                        "text_excerpt": text_excerpt,
                    },
                    {
                        "media_id": "movie-1",
                        "start_time": 10,
                        "end_time": 14,
                        "category": "sex_nudity_immodesty",
                        "source": "sidecar",
                        "labels": "immodesty_female",
                        "text_excerpt": "scene",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return media_path


@pytest.mark.asyncio
async def test_discover_rewrite_candidates_marks_exact_leaf_sidecar_ready(tmp_path: Path, monkeypatch):
    await _seed_movie(tmp_path)
    monkeypatch.setattr("leapfrog.media_rewriter._probe_media", lambda file_path: {"duration_s": 100.0, "bitrate": 1_500_000, "has_audio": True})

    candidates = await discover_rewrite_candidates()

    assert len(candidates) == 1
    assert candidates[0]["rewrite_ready"] is True
    assert candidates[0]["source_type"] == "sidecar"


@pytest.mark.asyncio
async def test_discover_rewrite_candidates_rejects_unmappable_labels(tmp_path: Path):
    await _seed_movie(tmp_path, labels="legacy_weapon")

    candidates = await discover_rewrite_candidates()

    assert len(candidates) == 1
    assert candidates[0]["rewrite_ready"] is False
    assert "exact VidAngel leaf keys" in str(candidates[0]["unsupported_reason"])


@pytest.mark.asyncio
async def test_discover_rewrite_candidates_includes_sidecar_only_media_in_known_directory(tmp_path: Path, monkeypatch):
    await _seed_movie(tmp_path)
    extra_media_path = tmp_path / "Unscanned Movie (2020).mkv"
    extra_media_path.write_bytes(b"source-bytes")
    extra_media_path.with_suffix(".leapfrog.json").write_text(
        json.dumps(
            {
                "format": "leapfrog.segment.sidecar/v1",
                "media_id": "external-media",
                "title": "Unscanned Movie",
                "segments": [
                    {
                        "media_id": "external-media",
                        "start_time": 5,
                        "end_time": 7,
                        "category": "language_profanity",
                        "source": "sidecar",
                        "labels": "fuck",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("leapfrog.media_rewriter._probe_media", lambda file_path: {"duration_s": 100.0, "bitrate": 1_500_000, "has_audio": True})

    candidates = await discover_rewrite_candidates()

    assert len(candidates) == 2
    extra_candidate = next(candidate for candidate in candidates if candidate["file_path"] == str(extra_media_path))
    assert extra_candidate["rewrite_ready"] is True
    assert extra_candidate["media_id"] == f"file:{extra_media_path}"
    assert extra_candidate["source_type"] == "sidecar"


@pytest.mark.asyncio
async def test_discover_rewrite_candidates_uses_persisted_roots_without_scan_jobs(tmp_path: Path):
    extra_media_path = tmp_path / "Library Only Movie (2021).mkv"
    extra_media_path.write_bytes(b"source-bytes")
    extra_media_path.with_suffix(".leapfrog.json").write_text(
        json.dumps(
            {
                "format": "leapfrog.segment.sidecar/v1",
                "media_id": "external-media",
                "title": "Library Only Movie",
                "segments": [
                    {
                        "media_id": "external-media",
                        "start_time": 5,
                        "end_time": 7,
                        "category": "language_profanity",
                        "source": "sidecar",
                        "labels": "fuck",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    await db.set_setting("rewrite_discovery_roots", json.dumps([str(tmp_path)]))
    media_rewriter._invalidate_rewrite_discovery_cache()

    candidates = await discover_rewrite_candidates()

    assert len(candidates) == 1
    assert candidates[0]["file_path"] == str(extra_media_path)
    assert candidates[0]["rewrite_ready"] is True
    assert candidates[0]["media_id"] == f"file:{extra_media_path}"


@pytest.mark.asyncio
async def test_discover_rewrite_candidates_caches_directory_walks(tmp_path: Path, monkeypatch):
    extra_media_path = tmp_path / "Cached Movie (2022).mkv"
    sidecar_path = extra_media_path.with_suffix(".leapfrog.json")
    extra_media_path.write_bytes(b"source-bytes")
    sidecar_path.write_text(
        json.dumps(
            {
                "format": "leapfrog.segment.sidecar/v1",
                "media_id": "cached-media",
                "title": "Cached Movie",
                "segments": [
                    {
                        "media_id": "cached-media",
                        "start_time": 2,
                        "end_time": 4,
                        "category": "language_profanity",
                        "source": "sidecar",
                        "labels": "fuck",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    await db.set_setting("rewrite_discovery_roots", json.dumps([str(tmp_path)]))
    media_rewriter._invalidate_rewrite_discovery_cache()
    calls = 0

    def fake_discover(roots: list[Path]) -> list[tuple[Path, Path]]:
        nonlocal calls
        calls += 1
        assert roots == [tmp_path]
        return [(extra_media_path, sidecar_path)]

    monkeypatch.setattr(media_rewriter, "_discover_rewrite_sources_in_roots", fake_discover)

    first = await discover_rewrite_candidates()
    second = await discover_rewrite_candidates()

    assert len(first) == 1
    assert len(second) == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_build_rewrite_plan_routes_language_to_mute_and_other_to_cut(tmp_path: Path, monkeypatch):
    await _seed_movie(tmp_path, text_excerpt="fuck")

    async def fake_probe(file_path: str):
        return {
            "duration_s": 100.0,
            "bitrate": 1_500_000,
            "video_stream_index": 0,
            "audio_streams": [
                {"index": 1, "language": "eng", "title": "English", "codec_name": "aac", "is_english": True},
                {"index": 2, "language": "spa", "title": "Spanish", "codec_name": "aac", "is_english": False},
                {"index": 3, "language": "eng", "title": "English Alt", "codec_name": "aac", "is_english": True},
            ],
            "subtitle_streams": [
                {"index": 4, "language": "eng", "title": "English SDH", "codec_name": "subrip", "is_english": True, "is_text": True},
                {"index": 5, "language": "spa", "title": "Spanish", "codec_name": "subrip", "is_english": False, "is_text": True},
            ],
        }

    monkeypatch.setattr("leapfrog.media_rewriter._probe_media", fake_probe)

    plan = await build_rewrite_plan(
        "movie-1",
        selected_leaf_keys=["fuck", "immodesty_female"],
        language_mode="mute",
    )

    assert plan["matched_leaf_keys"] == ["fuck", "immodesty_female"]
    assert plan["cut_ranges"] == [{"start": 10.0, "end": 14.0}]
    assert plan["mute_ranges"] == [{"start": 1.0, "end": 3.0}]
    assert plan["muted_audio_stream_indexes"] == [1, 3]
    assert plan["muted_subtitle_stream_indexes"] == [4]
    assert plan["subtitle_redactions"] == [{"start": 1, "end": 3, "terms": ["fuck"]}]


@pytest.mark.asyncio
async def test_build_rewrite_plan_supports_file_backed_candidate_ids(tmp_path: Path, monkeypatch):
    await _seed_movie(tmp_path)
    extra_media_path = tmp_path / "Unscanned Movie (2020).mkv"
    extra_media_path.write_bytes(b"source-bytes")
    extra_media_path.with_suffix(".leapfrog.json").write_text(
        json.dumps(
            {
                "format": "leapfrog.segment.sidecar/v1",
                "media_id": "external-media",
                "title": "Unscanned Movie",
                "segments": [
                    {
                        "media_id": "external-media",
                        "start_time": 5,
                        "end_time": 7,
                        "category": "language_profanity",
                        "source": "sidecar",
                        "labels": "fuck",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    async def fake_probe(file_path: str):
        return {"duration_s": 100.0, "bitrate": 1_500_000, "has_audio": True}

    monkeypatch.setattr("leapfrog.media_rewriter._probe_media", fake_probe)

    plan = await build_rewrite_plan(
        f"file:{extra_media_path}",
        selected_leaf_keys=["fuck"],
        language_mode="mute",
    )

    assert plan["media_id"] == f"file:{extra_media_path}"
    assert plan["title"] == "Unscanned Movie (2020)"
    assert plan["mute_ranges"] == [{"start": 5.0, "end": 7.0}]


@pytest.mark.asyncio
async def test_build_rewrite_plan_routes_language_to_cut_when_requested(tmp_path: Path, monkeypatch):
    await _seed_movie(tmp_path)

    async def fake_probe(file_path: str):
        return {"duration_s": 100.0, "bitrate": 1_500_000, "has_audio": True}

    monkeypatch.setattr("leapfrog.media_rewriter._probe_media", fake_probe)

    plan = await build_rewrite_plan(
        "movie-1",
        selected_leaf_keys=["fuck"],
        language_mode="cut",
    )

    assert plan["cut_ranges"] == [{"start": 1.0, "end": 3.0}]
    assert plan["mute_ranges"] == []


@pytest.mark.asyncio
async def test_build_rewrite_plan_rejects_non_text_subtitles_when_rewrite_is_needed(tmp_path: Path, monkeypatch):
    await _seed_movie(tmp_path, text_excerpt="fuck")

    async def fake_probe(file_path: str):
        return {
            "duration_s": 100.0,
            "bitrate": 1_500_000,
            "video_stream_index": 0,
            "audio_streams": [
                {"index": 1, "language": "eng", "title": "English", "codec_name": "aac", "is_english": True},
            ],
            "subtitle_streams": [
                {"index": 4, "language": "eng", "title": "English PGS", "codec_name": "hdmv_pgs_subtitle", "is_english": True, "is_text": False},
            ],
        }

    monkeypatch.setattr("leapfrog.media_rewriter._probe_media", fake_probe)

    with pytest.raises(RewriteEligibilityError, match="text subtitle codecs"):
        await build_rewrite_plan(
            "movie-1",
            selected_leaf_keys=["fuck"],
            language_mode="mute",
        )


@pytest.mark.asyncio
async def test_build_rewrite_plan_rejects_ass_subtitles_when_rewrite_is_needed(tmp_path: Path, monkeypatch):
    await _seed_movie(tmp_path, text_excerpt="fuck")

    async def fake_probe(file_path: str):
        return {
            "duration_s": 100.0,
            "bitrate": 1_500_000,
            "video_stream_index": 0,
            "audio_streams": [
                {"index": 1, "language": "eng", "title": "English", "codec_name": "aac", "is_english": True},
            ],
            "subtitle_streams": [
                {"index": 4, "language": "eng", "title": "English Styled", "codec_name": "ass", "is_english": True, "is_text": False},
            ],
        }

    monkeypatch.setattr("leapfrog.media_rewriter._probe_media", fake_probe)

    with pytest.raises(RewriteEligibilityError, match="text subtitle codecs"):
        await build_rewrite_plan(
            "movie-1",
            selected_leaf_keys=["fuck"],
            language_mode="mute",
        )


def test_rewrite_subtitle_cues_redacts_offending_words_without_dropping_cue():
    cues = [SubtitleCue(start_ms=1000, end_ms=3000, text="This fuck line stays on screen.")]

    rewritten = _rewrite_subtitle_cues(
        cues,
        keep_ranges=[(0.0, 10.0)],
        cut_ranges=[],
        subtitle_redactions=[{"start": 1.0, "end": 3.0, "text": "fuck"}],
    )

    assert len(rewritten) == 1
    assert rewritten[0].start_ms == 1000
    assert rewritten[0].end_ms == 3000
    assert rewritten[0].text == "This **** line stays on screen."


@pytest.mark.asyncio
async def test_build_rewrite_plan_uses_leaf_terms_when_text_excerpt_is_euphemized(tmp_path: Path, monkeypatch):
    await _seed_movie(tmp_path, text_excerpt="f-word")

    async def fake_probe(file_path: str):
        return {
            "duration_s": 100.0,
            "bitrate": 1_500_000,
            "video_stream_index": 0,
            "audio_streams": [{"index": 1, "language": "eng", "title": "English", "codec_name": "aac", "is_english": True}],
            "subtitle_streams": [{"index": 4, "language": "eng", "title": "English", "codec_name": "subrip", "is_english": True, "is_text": True}],
        }

    monkeypatch.setattr("leapfrog.media_rewriter._probe_media", fake_probe)

    plan = await build_rewrite_plan(
        "movie-1",
        selected_leaf_keys=["fuck"],
        language_mode="mute",
    )

    assert plan["subtitle_redactions"] == [{"start": 1, "end": 3, "terms": ["fuck", "f-word"]}]


def test_rewrite_subtitle_cues_redacts_phrase_across_caption_punctuation():
    cues = [SubtitleCue(start_ms=1000, end_ms=3000, text="That f-word lands even with punctuation.")]

    rewritten = _rewrite_subtitle_cues(
        cues,
        keep_ranges=[(0.0, 10.0)],
        cut_ranges=[],
        subtitle_redactions=[{"start": 1.0, "end": 3.0, "text": "f word"}],
    )

    assert rewritten[0].text == "That *-**** lands even with punctuation."


def test_rewrite_subtitle_cues_redacts_multi_word_phrase_with_flexible_spacing():
    cues = [SubtitleCue(start_ms=1000, end_ms=3000, text="You shut...up right now.")]

    rewritten = _rewrite_subtitle_cues(
        cues,
        keep_ranges=[(0.0, 10.0)],
        cut_ranges=[],
        subtitle_redactions=[{"start": 1.0, "end": 3.0, "text": "shut up"}],
    )

    assert rewritten[0].text == "You ****...** right now."


def test_rewrite_subtitle_cues_redacts_common_word_form_variants():
    cues = [SubtitleCue(start_ms=1000, end_ms=3000, text="They were screaming and fucked up.")]

    rewritten = _rewrite_subtitle_cues(
        cues,
        keep_ranges=[(0.0, 10.0)],
        cut_ranges=[],
        subtitle_redactions=[
            {"start": 1.0, "end": 3.0, "text": "scream"},
            {"start": 1.0, "end": 3.0, "text": "fuck"},
        ],
    )

    assert rewritten[0].text == "They were ********* and ****** up."


def test_rewrite_subtitle_cues_redacts_plural_y_variants():
    cues = [SubtitleCue(start_ms=1000, end_ms=3000, text="Those obscenities keep landing.")]

    rewritten = _rewrite_subtitle_cues(
        cues,
        keep_ranges=[(0.0, 10.0)],
        cut_ranges=[],
        subtitle_redactions=[{"start": 1.0, "end": 3.0, "text": "obscenity"}],
    )

    assert rewritten[0].text == "Those *********** keep landing."


@pytest.mark.asyncio
async def test_get_leaf_keys_for_user_profile_uses_vidangel_labels_only(tmp_path: Path):
    await db.upsert_user_filter("alice", True)
    await db.upsert_user_category_preference("alice", "language_profanity", enabled=True, threshold=None)
    await db.set_user_label_preference("alice", "language_profanity", "fuck", enabled=True, threshold=None)
    await db.set_user_label_preference("alice", "language_profanity", "shit", enabled=False, threshold=None)

    selected = await get_leaf_keys_for_user_profile("alice")

    assert "fuck" in selected
    assert "shit" not in selected


@pytest.mark.asyncio
async def test_execute_rewrite_plan_retries_then_fails_when_size_limit_exceeded(tmp_path: Path, monkeypatch):
    await _seed_movie(tmp_path)
    output_path = tmp_path / "Sample Movie (2019).cleaned.mkv"
    attempts: list[int] = []

    async def fake_run_ffmpeg(**kwargs):
        attempts.append(kwargs["video_bitrate"])
        Path(kwargs["output_path"]).write_bytes(b"x" * 300)
        return "libx264"

    async def fake_prepare_subtitle_streams(**kwargs):
        return [], []

    monkeypatch.setattr("leapfrog.media_rewriter._run_ffmpeg", fake_run_ffmpeg)
    monkeypatch.setattr("leapfrog.media_rewriter._prepare_subtitle_streams", fake_prepare_subtitle_streams)
    monkeypatch.setattr("leapfrog.media_rewriter.os.path.getsize", lambda path: 200 if str(path).endswith(".mp4") else 300)

    with pytest.raises(Exception, match="120% size ceiling"):
        await execute_rewrite_plan(
            {
                "media_id": "movie-1",
                "title": "Sample Movie",
                "file_path": str(tmp_path / "Sample Movie (2019).mp4"),
                "selected_leaf_keys": ["fuck"],
                "language_mode": "mute",
                "keep_ranges": [{"start": 0.0, "end": 20.0}],
                "mute_ranges": [{"start": 1.0, "end": 3.0}],
                "cut_ranges": [],
                "summary": {"output_path": str(output_path)},
                "media_info": {
                    "duration_s": 20.0,
                    "video_stream_index": 0,
                    "audio_streams": [{"index": 1, "language": "eng", "title": "English", "codec_name": "aac", "is_english": True}],
                    "subtitle_streams": [],
                },
            },
            size_limit_percent=120,
        )

    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_execute_rewrite_plan_passes_all_audio_streams_to_ffmpeg_and_uses_rewritten_subtitles(tmp_path: Path, monkeypatch):
    output_path = tmp_path / "Sample Movie (2019).cleaned.mkv"
    captured: dict[str, object] = {}

    async def fake_prepare_subtitle_streams(**kwargs):
        return (
            [{"path": str(tmp_path / "eng.srt"), "language": "eng", "title": "English"}],
            [{"index": 5, "language": "spa", "title": "Spanish"}],
        )

    async def fake_run_ffmpeg(**kwargs):
        captured.update(kwargs)
        Path(kwargs["output_path"]).write_bytes(b"x" * 200)
        return "h264_nvenc"

    monkeypatch.setattr("leapfrog.media_rewriter._prepare_subtitle_streams", fake_prepare_subtitle_streams)
    monkeypatch.setattr("leapfrog.media_rewriter._run_ffmpeg", fake_run_ffmpeg)
    monkeypatch.setattr("leapfrog.media_rewriter.os.path.getsize", lambda path: 200 if str(path).endswith(".mp4") else 200)

    result = await execute_rewrite_plan(
        {
            "media_id": "movie-1",
            "title": "Sample Movie",
            "file_path": str(tmp_path / "Sample Movie (2019).mp4"),
            "selected_leaf_keys": ["fuck"],
            "language_mode": "mute",
            "keep_ranges": [{"start": 0.0, "end": 20.0}],
            "mute_ranges": [{"start": 1.0, "end": 3.0}],
            "cut_ranges": [],
            "summary": {"output_path": str(output_path)},
            "media_info": {
                "duration_s": 20.0,
                "video_stream_index": 0,
                "audio_streams": [
                    {"index": 1, "language": "eng", "title": "English", "codec_name": "aac", "is_english": True},
                    {"index": 2, "language": "spa", "title": "Spanish", "codec_name": "aac", "is_english": False},
                    {"index": 3, "language": "eng", "title": "English Commentary", "codec_name": "aac", "is_english": True},
                ],
                "subtitle_streams": [
                    {"index": 4, "language": "eng", "title": "English", "codec_name": "subrip", "is_english": True, "is_text": True},
                    {"index": 5, "language": "spa", "title": "Spanish", "codec_name": "subrip", "is_english": False, "is_text": True},
                ],
            },
        },
        size_limit_percent=120,
    )

    assert len(captured["media_info"]["audio_streams"]) == 3
    assert captured["rewritten_subtitles"] == [{"path": str(tmp_path / "eng.srt"), "language": "eng", "title": "English"}]
    assert captured["passthrough_subtitles"] == [{"index": 5, "language": "spa", "title": "Spanish"}]
    assert result["output_path"] == str(output_path)
