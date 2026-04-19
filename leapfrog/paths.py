"""Filesystem paths for Leapfrog runtime data."""

from __future__ import annotations

import os
from pathlib import Path


def get_data_dir() -> Path:
    """Return the configured Leapfrog data directory."""
    return Path(os.environ.get("LEAPFROG_DATA", str(Path.home() / ".leapfrog")))


def get_legacy_data_dir() -> Path:
    """Return the historical fixed Leapfrog data directory."""
    return Path.home() / ".leapfrog"


def get_models_dir() -> Path:
    """Return the local directory used for bundled and downloaded models."""
    return get_data_dir() / "models"


def get_thumbnails_dir() -> Path:
    """Return the directory used for persisted review thumbnails."""
    return get_data_dir() / "thumbnails"


def get_legacy_thumbnails_dir() -> Path:
    """Return the historical thumbnail directory used before configurable data dirs."""
    return get_legacy_data_dir() / "thumbnails"
