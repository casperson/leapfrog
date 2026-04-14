"""Unit tests for subtitle parsing helpers."""

from __future__ import annotations

from leapfrog.subtitles import clean_caption_text, parse_subtitle_text


def test_parse_subtitle_text_parses_srt_cues():
    raw = """1
00:00:01,000 --> 00:00:02,500
Hello there.

2
00:00:03,000 --> 00:00:04,000
General Kenobi!
"""
    cues = parse_subtitle_text(raw)
    assert len(cues) == 2
    assert cues[0].start_ms == 1000
    assert cues[0].end_ms == 2500
    assert cues[0].text == "Hello there."
    assert cues[1].text == "General Kenobi!"


def test_parse_subtitle_text_parses_webvtt_and_strips_markup():
    raw = """WEBVTT

00:00:01.000 --> 00:00:02.000
<i>Bad</i> {\\an8}language\\Nhere
"""
    cues = parse_subtitle_text(raw)
    assert len(cues) == 1
    assert cues[0].text == "Bad language here"


def test_clean_caption_text_normalizes_whitespace_and_tags():
    assert clean_caption_text("  <b>Hello</b>   world\\Nagain  ") == "Hello world again"
