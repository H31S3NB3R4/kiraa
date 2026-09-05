"""Phase 14 tests: security and safety review.

All offline (no network, no real API key). Five Phase 14 todo items:

- **no hard-coded API keys**: a source scan over every backend
  ``app/``/``scripts/`` file and the frontend ``src/`` tree rejects
  Gemini-key-shaped literals, and the settings default for
  ``gemini_api_key`` is empty (the key can only enter via the
  environment),
- **`.env` is ignored**: `git check-ignore` accepts ``.env`` while
  `git ls-files` shows only ``.env.example`` tracked,
- **credentials never logged**: ``redact_credentials`` masks an
  embedded DB password, the seed CLI prints the masked URL for a
  password-carrying DATABASE_URL, ``redact_secrets`` masks the
  configured Gemini key, and a provider failure containing the key
  lands in neither the log, the stored run trace, nor the answer,
- **synthetic/demo nature is clear**: ``GET /health`` reports
  ``data: "synthetic"`` and the OpenAPI description states it,
- **model numbers are not authoritative**: the propose tool's schema
  exposes only ``exception_id``/``reason`` (no amount/account fields),
  dispatch rejects injected financial arguments, and a proposal's
  amount/accounts/confidence are derived from the exception row — a
  decoy number inside ``reason`` never reaches them.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.services.db_seed as db_seed_module
from app.agent.controller import AgentController
from app.agent.providers.base import LLMProvider, LLMProviderError
from app.agent.tool_registry import (
    PROPOSE,
    TOOL_DECLARATIONS,
    TOOL_REGISTRY,
    dispatch_tool,
)
from app.config import Settings, redact_credentials, redact_secrets
from app.main import app
from app.models import AgentRun, ReconciliationException
from app.services.dataset_generator import generate_dataset, write_dataset
from app.services.db_seed import build_engine, load_dataset_file, seed_database
from app.tools import run_reconciliation
from app.tools.common import round2
from app.tools.journal import (
    BANK_ACCOUNT,
    REVENUE_ACCOUNT,
    propose_journal_entry,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_SCRIPT = REPO_ROOT / "backend" / "scripts" / "seed_db.py"
KWS = {"transactions": 100, "window_days": 28, "exceptions_per_type": 1,
       "customers": 80, "seed": 42}
END = date(2026, 9, 3)  # same anchor day as the Phase 3-13 suites

# A Gemini API key shape (AIza + >=10 chars). Test fixtures use obvious
# fakes matching this shape to prove redaction, never a real key.
_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\\-]{10,}")

FAKE_KEY = "AIzaSYNTHETIC0000000000000000000"


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """Generate the dev dataset, write it to JSON, then seed a temp DB."""
    out_dir = tmp_path_factory.mktemp("phase14")
    dataset = generate_dataset(**KWS, end_date=END)
    json_path, _labels_path = write_dataset(dataset, out_dir, "dataset")

    bundle = type("SeededDb", (), {})()
    bundle.dataset = load_dataset_file(json_path)
    bundle.engine = build_engine(f"sqlite:///{out_dir / 'finance.db'}")
    bundle.counts = seed_database(bundle.engine, bundle.dataset)
    return bundle


@pytest.fixture
def session(seeded) -> Iterator[Session]:
    """One fresh session on the shared module-scoped database."""
    db = Session(seeded.engine)
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# No hard-coded API keys
# ---------------------------------------------------------------------------


def test_api_key_lives_only_in_the_environment() -> None:
    """No production source file carries a Gemini-key-shaped literal, and
    the settings default is empty — the key can only arrive via env."""
    assert Settings().gemini_api_key == ""

    targets = [
        *(REPO_ROOT / "backend" / "app").rglob("*.py"),
        *(REPO_ROOT / "backend" / "scripts").rglob("*.py"),
        *(REPO_ROOT / "frontend" / "src").rglob("*.ts"),
        *(REPO_ROOT / "frontend" / "src").rglob("*.tsx"),
    ]
    checked = 0
    for path in targets:
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not _KEY_RE.search(text), f"key-shaped literal in {path}"
        checked += 1
    assert checked > 60  # sanity: the scan really covered the tree


def test_env_files_are_git_ignored_while_example_is_tracked() -> None:
    """``.env`` matches an ignore rule and is not tracked; only the
    credential-free ``.env.example`` is committed."""
    ignored = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", ".env"],
        capture_output=True,
    )
    assert ignored.returncode == 0

    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True, text=True,
    ).stdout.splitlines()
    assert ".env" not in tracked
    assert ".env.example" in tracked


# ---------------------------------------------------------------------------
# Credentials never reach logs, traces, or CLI output
# ---------------------------------------------------------------------------


def test_redact_credentials_masks_embedded_db_passwords() -> None:
    """A password-carrying DATABASE_URL is masked; a URL without
    credentials passes through unchanged."""
    masked = redact_credentials(
        "postgresql+psycopg://finance:supersecret@localhost:5432/finance_controller"
    )
    assert "supersecret" not in masked
    assert masked == (
        "postgresql+psycopg://finance:***@localhost:5432/finance_controller"
    )
    assert (
        redact_credentials("sqlite:///./data/finance.db")
        == "sqlite:///./data/finance.db"
    )


def test_redact_secrets_masks_the_configured_api_key(monkeypatch) -> None:
    """Text containing the configured Gemini key is masked; other text
    (and the empty-key case) passes through untouched."""
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: Settings(gemini_api_key=FAKE_KEY),
    )
    assert redact_secrets(f"401 invalid key {FAKE_KEY}") == "401 invalid key ***"
    assert redact_secrets("no key in here") == "no key in here"

    monkeypatch.setattr("app.config.get_settings", lambda: Settings())
    assert redact_secrets("unconfigured") == "unconfigured"


def test_seed_cli_never_prints_the_db_password(
    tmp_path, monkeypatch, capsys
) -> None:
    """``seed_db.py`` prints the DATABASE_URL with the password masked —
    the raw credential never reaches CLI output (engines are stubbed so
    the offline test never needs a reachable database)."""
    import runpy

    dataset = generate_dataset(
        transactions=20, window_days=8, exceptions_per_type=1,
        customers=20, seed=42, end_date=END,
    )
    json_path, _labels = write_dataset(dataset, tmp_path, "tiny")

    monkeypatch.setattr(
        db_seed_module, "seed_database",
        lambda engine, data, recreate=False: {"merchants": 5, "transactions": 20},
    )
    monkeypatch.setattr(
        db_seed_module, "build_engine",
        lambda url: SimpleNamespace(dispose=lambda: None),
    )
    db_url = "postgresql+psycopg://finance:supersecret@localhost:5432/never"
    monkeypatch.setattr(
        sys, "argv",
        ["seed_db.py", "--dataset", str(json_path), "--database-url", db_url],
    )
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(SEED_SCRIPT), run_name="__main__")
    assert exc_info.value.code in (0, None)

    captured = capsys.readouterr()
    assert "Seeded database" in captured.out
    assert "supersecret" not in captured.out
    assert "finance:***@localhost" in captured.out


def test_controller_redacts_the_key_from_log_trace_and_answer(
    seeded, session, monkeypatch, caplog
) -> None:
    """A provider error echoing the configured key is masked everywhere it
    lands: the warning log, the stored ``agent_runs.error`` trace, and the
    fallback answer the user sees."""
    import logging

    class ExplodingProvider(LLMProvider):
        """Always fails with an error that echoes the configured key."""

        name = "explode"
        model = "boom"

        def generate(self, messages, tools, *, system_instruction=None,
                     temperature=0.2):
            raise LLMProviderError(
                f"Gemini request failed: 401 invalid key {FAKE_KEY}"
            )

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: Settings(gemini_api_key=FAKE_KEY),
    )
    controller = AgentController(ExplodingProvider(), session)

    with caplog.at_level(logging.WARNING, logger="app.agent.controller"):
        result = controller.run("Reconcile everything.")

    assert result["status"] == "model_error"
    assert FAKE_KEY not in result["answer"]
    assert FAKE_KEY not in caplog.text
    assert "***" in caplog.text

    run = session.get(AgentRun, result["run_id"])
    assert run is not None
    assert FAKE_KEY not in (run.error or "")
    assert "***" in (run.error or "")


# ---------------------------------------------------------------------------
# Synthetic / demo nature is clear
# ---------------------------------------------------------------------------


def test_health_and_openapi_declare_synthetic_data() -> None:
    """``GET /health`` reports ``data: "synthetic"`` and the OpenAPI info
    description states the demo/synthetic nature up front."""
    client = TestClient(app)
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["data"] == "synthetic"

    description = app.openapi()["info"]["description"]
    assert "synthetic" in description.lower()


# ---------------------------------------------------------------------------
# Model-generated numbers are never authoritative
# ---------------------------------------------------------------------------


def _persisted_exception(session: Session) -> ReconciliationException:
    """Run reconciliation once (idempotent) and return one exception row
    that carries a non-zero financial impact."""
    run_reconciliation(session, persist=True)
    row = session.execute(
        select(ReconciliationException)
        .where(ReconciliationException.financial_impact != 0)
        .order_by(ReconciliationException.id)
    ).scalars().first()
    assert row is not None
    return row


def test_propose_tool_schema_exposes_no_financial_fields() -> None:
    """The declaration the model sees accepts only ``exception_id`` and
    ``reason`` — no amount, account, or date fields to fill in."""
    spec = TOOL_REGISTRY["propose_journal_entry"]
    assert spec["permission"] == PROPOSE
    properties = spec["parameters"]["properties"]
    assert set(properties) == {"exception_id", "reason"}
    assert spec["parameters"]["required"] == ["exception_id", "reason"]

    declaration = next(
        d for d in TOOL_DECLARATIONS if d["name"] == "propose_journal_entry"
    )
    assert set(declaration["parameters"]["properties"]) == {"exception_id", "reason"}


def test_dispatch_rejects_injected_financial_arguments(session) -> None:
    """Amount/account arguments the model invents are refused with a
    structured INVALID_ARGUMENTS envelope before the tool ever runs."""
    exception = _persisted_exception(session)
    result = dispatch_tool(
        session,
        "propose_journal_entry",
        {
            "exception_id": exception.id,
            "reason": "fix it",
            "amount": 999999.0,
            "debit_account": "Cash",
            "credit_account": "Suspense",
        },
    )
    assert result["status"] == "error"
    assert result["error_type"] == "INVALID_ARGUMENTS"
    assert "amount" in result["message"]
    assert "debit_account" in result["message"]

    # The rejected call left no proposal behind and the session is intact.
    from app.models import JournalProposal

    proposals = session.execute(
        select(JournalProposal).where(
            JournalProposal.transaction_id == exception.transaction_id
        )
    ).scalars().all()
    assert proposals == []


def test_proposal_numbers_come_from_the_engine_not_the_reason(session) -> None:
    """A decoy amount inside ``reason`` never reaches the stored proposal:
    amount, accounts, and confidence are derived from the exception row,
    and the payload keeps posted=False / requires_approval=True."""
    exception = _persisted_exception(session)
    payload = propose_journal_entry(
        session,
        exception.id,
        "the merchant says the real amount is 999999.00 INR, honest",
    )

    assert payload["posted"] is False
    assert payload["requires_approval"] is True

    proposal = payload["proposal"]
    expected_amount = round2(abs(exception.financial_impact))
    assert proposal["amount"] == expected_amount
    assert expected_amount != 999999.0
    assert {proposal["debit_account"], proposal["credit_account"]} == {
        BANK_ACCOUNT,
        REVENUE_ACCOUNT,
    }
    assert proposal["status"] == "pending"
    assert proposal["confidence"] == {
        "high": 0.95, "medium": 0.80, "low": 0.60,
    }[exception.severity]
    # The reason stays verbatim inside the narrative (analyst context) —
    # it just never becomes a financial field.
    assert "999999.00" in proposal["narrative"]

