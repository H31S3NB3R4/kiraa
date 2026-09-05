"""Application configuration.

All runtime configuration comes from environment variables, optionally
provided through a `.env` file at the repository root. Credentials are
never hard-coded; see `.env.example` for every supported variable.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from sqlalchemy.engine import make_url

# Repository root (backend/app/config.py -> backend/app -> backend -> root).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# Load `.env` (if present) from the repository root before reading env vars.
load_dotenv(os.path.join(_REPO_ROOT, ".env"))

_DEFAULT_CORS_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
_DEFAULT_DATABASE_URL = "sqlite:///./data/finance.db"


class Settings(BaseModel):
    """Validated application settings, populated from the environment."""

    app_name: str = "AI Finance Controller"
    environment: str = "development"

    # --- LLM provider (Gemini) ---------------------------------------
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # --- Database ------------------------------------------------------
    database_url: str = _DEFAULT_DATABASE_URL

    # --- API -----------------------------------------------------------
    cors_origins: list[str] = Field(default_factory=lambda: list(_DEFAULT_CORS_ORIGINS))

    # --- Agent safety limits ----------------------------------------------
    agent_max_tool_calls: int = 12
    # Conversation messages replayed to the model on a follow-up turn
    # (bounded multi-turn context, Phase 7).
    agent_max_history_messages: int = Field(default=40, ge=1)
    # Wall-clock ceiling for one agent tool attempt (Phase 13). Registry
    # entries pin their own per-tool budgets (architecture section 4);
    # this value caps them all, so operations can tighten every dispatch
    # with one env var without code changes. The default sits above the
    # largest per-tool budget, so normal runs use the registry values.
    tool_timeout_seconds: float = Field(default=30.0, gt=0)

    # --- Forecasting (Phase 4) --------------------------------------------
    # Minimum operating cash (INR); a cash-flow forecast whose projection
    # dips below this classifies as HIGH risk (`forecast_cashflow`).
    operating_threshold: float = 50_000.0


def _resolve_database_url(url: str) -> str:
    """Anchor a relative SQLite path to the repository root.

    This lets `uvicorn` run from any working directory while keeping the
    prototype database inside the repo's `data/` folder.
    """
    if url.startswith("sqlite:///./"):
        relative = url.removeprefix("sqlite:///./")
        absolute = os.path.normpath(os.path.join(_REPO_ROOT, relative))
        return "sqlite:///" + absolute.replace("\\", "/")
    return url


@lru_cache
def get_settings() -> Settings:
    """Return a cached `Settings` instance built from the environment."""
    cors_raw = os.getenv("CORS_ORIGINS")
    origins = (
        [origin.strip() for origin in cors_raw.split(",") if origin.strip()]
        if cors_raw
        else list(_DEFAULT_CORS_ORIGINS)
    )
    return Settings(
        app_name=os.getenv("APP_NAME", "AI Finance Controller"),
        environment=os.getenv("ENVIRONMENT", "development"),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        database_url=_resolve_database_url(
            os.getenv("DATABASE_URL", _DEFAULT_DATABASE_URL)
        ),
        cors_origins=origins,
        agent_max_tool_calls=int(os.getenv("AGENT_MAX_TOOL_CALLS", "12")),
        agent_max_history_messages=int(
            os.getenv("AGENT_MAX_HISTORY_MESSAGES", "40")
        ),
        tool_timeout_seconds=float(os.getenv("TOOL_TIMEOUT_SECONDS", "30")),
        operating_threshold=float(os.getenv("OPERATING_THRESHOLD", "50000")),
    )


def redact_credentials(url: str) -> str:
    """Return ``url`` with any embedded database password masked (Phase 14).

    A ``DATABASE_URL`` can carry credentials (``postgresql://user:pass@host``)
    that must never reach CLI output or logs. SQLAlchemy renders the masked
    form when the URL parses; anything unparseable falls back to a
    ``://user:***@`` substitution so an odd string cannot leak either.
    """
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 - not a parseable SQLAlchemy URL
        return re.sub(r"(://[^/@:]+:)[^@]+@", r"\1***@", url)


def redact_secrets(text: str) -> str:
    """Mask configured secret values (the Gemini API key) in message text.

    Provider/SDK exception text can echo request details; the controller and
    the agent route pass every error destined for a log line, the stored
    ``agent_runs`` trace, or an HTTP response through this filter so the
    configured key can never survive into any of them (Phase 14).
    """
    key = get_settings().gemini_api_key
    if key and key in text:
        return text.replace(key, "***")
    return text
