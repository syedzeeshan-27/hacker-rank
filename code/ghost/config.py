"""Paths, config.json and .env handling.

Secrets come from environment variables only (loaded from an uncommitted .env at the repo root).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

CODE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CODE_DIR.parent
DATASET_DIR = REPO_ROOT / "dataset"
PROMPTS_DIR = CODE_DIR / "prompts"
CONFIG_PATH = CODE_DIR / "config.json"
ENV_PATH = REPO_ROOT / ".env"

DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


CONFIG = load_config()
CACHE_DIR = REPO_ROOT / CONFIG.get("cache_dir", ".cache")
OUTPUT_PATH = REPO_ROOT / CONFIG.get("output_file", "output.csv")

# .env values win over whatever the parent shell exported, so a run is reproducible.
load_dotenv(ENV_PATH, override=True)
_DOTENV = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}


def anthropic_api_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def anthropic_base_url() -> str:
    """Only honor a base URL that the project's own .env sets.

    The developer shell may export ANTHROPIC_BASE_URL for other tools; that must not redirect our calls.
    """
    value = (_DOTENV.get("ANTHROPIC_BASE_URL") or "").strip()
    return value or DEFAULT_ANTHROPIC_BASE_URL


def dataset_path(relative: str) -> Path:
    return DATASET_DIR / relative


def prompt_text(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")
