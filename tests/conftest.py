"""Pytest fixtures.

The context builders live in ``tests/helpers.py`` and are importable directly
(``from helpers import make_context``) because ``tests`` is on the path via the
``pythonpath`` setting in ``pyproject.toml``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from campaign_preflight.config import PreflightConfig


@pytest.fixture
def config() -> PreflightConfig:
    return PreflightConfig()


@pytest.fixture
def demo_dir() -> Path:
    from campaign_preflight.providers.fixture_provider import DEMO_DIR

    return DEMO_DIR


@pytest.fixture
def examples_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures"
