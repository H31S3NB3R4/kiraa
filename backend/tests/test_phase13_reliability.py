"""Phase 13 tests: agent reliability hardening.

Mirrors the earlier fixture pattern: the dev dataset (seed 42, 100
transactions, 1 exception per type) is seeded into a temp SQLite DB, then
the reliability contract added in Phase 13 is verified offline (no
network, no API key required):

- **registry policy**: every tool entry pins its own wall-clock
  ``timeout_seconds`` and bounded ``max_retries`` — projected into
  ``TOOL_TIMEOUTS``/``TOOL_RETRY_POLICY`` — and the PROPOSE tool is never
  re-attempted (a retry landing after a slow success could draft a
  duplicate proposal),
- **timeout dispatch**: a stuck tool call is cut off at its budget, a
  timed-out attempt gets at most ``max_retries`` retries, exhaustion
  surfaces a structured ``TOOL_TIMEOUT`` envelope (never a hang), a
  timed-out attempt can succeed on retry, and non-timeout failures are
  *not* retried (deterministic tools would fail the same way twice),
- **settings ceiling**: ``TOOL_TIMEOUT_SECONDS`` tightens every dispatch
  below the registry budgets but never loosens one,
- **isolation**: each attempt runs on an isolated worker session bound
  to the same engine — the controller's session is never handed to tool
  code and stays usable after a stuck or failed call,
- **provider resilience**: the Gemini adapter retries transient HTTP
  failures (429/5xx) with a bounded backoff and succeeds, gives up after
  ``max_attempts`` with ``LLMProviderError``, and fails fast on
  non-transient errors (offline, injected client),
- **schema boundary**: a blank chat message is refused with 422 before
  the loop starts — no provider round, no ``agent_runs`` row charged,
- **loop integration**: a ``TOOL_TIMEOUT`` envelope flows through the
  controller as a traced tool error the model is told about, and the run
  still completes with an answer instead of hanging.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.controller import STATUS_COMPLETED, AgentController
from app.agent.providers.base import (
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    LLMToolCall,
    TextMessage,
    ToolResultsMessage,
)
from app.agent.providers.gemini import GeminiProvider
from app.agent.tool_registry import (
    TOOL_REGISTRY,
    TOOL_RETRY_POLICY,
    TOOL_TIMEOUTS,
    dispatch_tool,
)
from app.api.routes.agent import get_provider
from app.config import Settings
from app.db.session import get_db
from app.main import app
from app.models import AgentRun, ToolCall, Transaction
from app.services.dataset_generator import generate_dataset, write_dataset
from app.services.db_seed import build_engine, load_dataset_file, seed_database

KWS = {"transactions": 100, "window_days": 28, "exceptions_per_type": 1,
       "customers": 80, "seed": 42}
END = date(2026, 9, 3)  # same anchor day as the Phase 3-12 suites


class FakeProvider(LLMProvider):
    """Scripted provider: replays queued rounds (exceptions are raised)."""

    name = "fake"
    model = "fake-model"

    def __init__(self, rounds: list[LLMResponse | Exception]) -> None:
        self.rounds = list(rounds)
        self.calls = 0
        self.seen: list[dict] = []

    def generate(
        self,
        messages,
        tools,
        *,
        system_instruction: str | None = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        self.seen.append(
            {
                "messages": list(messages),
                "tools": list(tools),
                "system_instruction": system_instruction,
                "temperature": temperature,
            }
        )
        response = self.rounds[min(self.calls, len(self.rounds) - 1)]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        return response


class _HTTPError(Exception):
    """SDK-style transport error carrying an HTTP status code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class _FakeGeminiClient:
    """Offline stand-in for ``google_genai.Client`` with scripted outcomes.

    ``generate`` calls ``client.models.generate_content(...)``; the fake
    replays the queued outcomes (exceptions are raised), so the retry
    policy is exercised without any network.
    """

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, *, model, contents, config) -> Any:
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _patch_tool(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    *,
    timeout_seconds: float,
    max_retries: int,
    callable: Any,
) -> None:
    """Point one registry entry at ``callable`` with a test-sized policy.

    Copies the real entry (same permission/description/parameters) so the
    argument-validation path under test stays the production one.
    """
    spec = dict(TOOL_REGISTRY[name])
    spec["timeout_seconds"] = timeout_seconds
    spec["max_retries"] = max_retries
    spec["callable"] = callable
    monkeypatch.setitem(TOOL_REGISTRY, name, spec)


def _stuck_tool(
    release: threading.Event,
    log: list[int] | None = None,
    payload: dict[str, Any] | None = None,
):
    """A tool that stalls until ``release`` is set (5s hard cap)."""
    def tool(db: Session, **kwargs: Any) -> dict[str, Any]:
        if log is not None:
            log.append(1)
        release.wait(5.0)
        return dict(payload or {"tool": "stuck", "status": "ok"})
    return tool


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """Generate the dev dataset, write it to JSON, then seed a temp DB."""
    out_dir = tmp_path_factory.mktemp("phase13")
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
# Registry policy (architecture section 4)
# ---------------------------------------------------------------------------


def test_registry_pins_explicit_timeout_and_retry_policy() -> None:
    """Every entry carries a positive per-tool budget and a bounded retry
    count — projected into TOOL_TIMEOUTS/TOOL_RETRY_POLICY."""
    assert set(TOOL_TIMEOUTS) == set(TOOL_REGISTRY)
    assert set(TOOL_RETRY_POLICY) == set(TOOL_REGISTRY)
    for name, spec in TOOL_REGISTRY.items():
        assert spec["timeout_seconds"] > 0
        assert isinstance(spec["max_retries"], int)
        assert spec["max_retries"] >= 0
        assert TOOL_TIMEOUTS[name] == float(spec["timeout_seconds"])
        assert TOOL_RETRY_POLICY[name] == int(spec["max_retries"])


def test_propose_tool_is_never_retried() -> None:
    """propose_journal_entry pins max_retries=0: a retry landing after a
    slow-success draft could create a duplicate proposal, so the PROPOSE
    class gets exactly one attempt."""
    spec = TOOL_REGISTRY["propose_journal_entry"]
    assert spec["permission"] == "PROPOSE"
    assert spec["max_retries"] == 0


# ---------------------------------------------------------------------------
# Timeout dispatch (architecture sections 4 and 16)
# ---------------------------------------------------------------------------


def test_stuck_tool_times_out_with_structured_envelope(
    session, monkeypatch
) -> None:
    """A tool that never returns is cut off at its budget: dispatch
    returns a TOOL_TIMEOUT envelope (never a hang) and stamps latency."""
    never = threading.Event()  # never set
    _patch_tool(
        monkeypatch,
        "query_ledger",
        timeout_seconds=0.3,
        max_retries=0,
        callable=_stuck_tool(never),
    )

    started = time.perf_counter()
    result = dispatch_tool(session, "query_ledger", {"limit": 1})
    elapsed = time.perf_counter() - started

    assert elapsed < 5.0  # bounded, not hanging on the 5s release cap
    assert result["status"] == "error"
    assert result["error_type"] == "TOOL_TIMEOUT"
    assert result["tool"] == "query_ledger"
    assert result["details"]["timeout_seconds"] == pytest.approx(0.3)
    assert result["details"]["attempts"] == 1
    assert result["latency_ms"] >= 0.0


def test_timed_out_attempt_is_retried_within_max_retries(
    session, monkeypatch
) -> None:
    """A slow-then-fast tool succeeds on its second attempt and the
    retries actually run (attempts counted from the tool's own log)."""
    release = threading.Event()
    attempts: list[int] = []
    calls = {"n": 0}

    def flaky(db: Session, **kwargs: Any) -> dict[str, Any]:
        calls["n"] += 1
        attempts.append(calls["n"])
        if calls["n"] == 1:
            release.wait(5.0)  # first attempt stalls past the budget
        return {"tool": "query_ledger", "status": "ok", "round": calls["n"]}

    _patch_tool(
        monkeypatch,
        "query_ledger",
        timeout_seconds=0.3,
        max_retries=1,
        callable=flaky,
    )

    started = time.perf_counter()
    result = dispatch_tool(session, "query_ledger", {"limit": 1})
    elapsed = time.perf_counter() - started

    assert result["status"] == "ok"
    assert result["round"] == 2
    assert attempts == [1, 2]
    assert elapsed < 5.0
    release.set()  # unblock the abandoned first attempt's worker


def test_timeout_envelope_after_retry_exhaustion(
    session, monkeypatch
) -> None:
    """Every attempt stalling past the budget ends in TOOL_TIMEOUT with
    attempts = 1 + max_retries in the details envelope."""
    never = threading.Event()  # never set
    log: list[int] = []
    _patch_tool(
        monkeypatch,
        "query_ledger",
        timeout_seconds=0.3,
        max_retries=1,
        callable=_stuck_tool(never, log=log),
    )

    result = dispatch_tool(session, "query_ledger", {"limit": 1})

    assert result["status"] == "error"
    assert result["error_type"] == "TOOL_TIMEOUT"
    assert result["details"]["attempts"] == 2
    assert len(log) == 2  # both attempts actually started


def test_non_timeout_failures_are_not_retried(session, monkeypatch) -> None:
    """A fast deterministic failure (ValueError) returns its structured
    envelope on the first attempt — a retry would fail identically."""
    attempts: list[int] = []

    def broken(db: Session, **kwargs: Any) -> dict[str, Any]:
        attempts.append(1)
        raise ValueError("transaction window is inverted")

    _patch_tool(
        monkeypatch,
        "query_ledger",
        timeout_seconds=5.0,
        max_retries=1,
        callable=broken,
    )

    result = dispatch_tool(session, "query_ledger", {"limit": 1})

    assert result["status"] == "error"
    assert result["error_type"] == "VALIDATION_ERROR"
    assert "transaction window is inverted" in result["message"]
    assert len(attempts) == 1


# ---------------------------------------------------------------------------
# Settings ceiling + session isolation
# ---------------------------------------------------------------------------


def test_settings_ceiling_tightens_but_never_loosens(
    session, monkeypatch
) -> None:
    """TOOL_TIMEOUT_SECONDS acts as a global ceiling: 0.2s tightens a 5s
    registry budget; 999s cannot loosen it below 5s."""
    never = threading.Event()  # never set
    _patch_tool(
        monkeypatch,
        "query_ledger",
        timeout_seconds=5.0,
        max_retries=0,
        callable=_stuck_tool(never),
    )

    # Tighten: dispatch cuts off at 0.2s.
    monkeypatch.setattr(
        "app.agent.tool_registry.get_settings",
        lambda: Settings(tool_timeout_seconds=0.2),
    )
    tightened = dispatch_tool(session, "query_ledger", {"limit": 1})
    assert tightened["error_type"] == "TOOL_TIMEOUT"
    assert tightened["details"]["timeout_seconds"] == pytest.approx(0.2)

    # Loosen: the registry's 5s budget still wins.
    monkeypatch.setattr(
        "app.agent.tool_registry.get_settings",
        lambda: Settings(tool_timeout_seconds=999.0),
    )
    loosened = dispatch_tool(session, "query_ledger", {"limit": 1})
    assert loosened["error_type"] == "TOOL_TIMEOUT"
    assert loosened["details"]["timeout_seconds"] == pytest.approx(5.0)


def test_attempt_runs_on_isolated_worker_session(
    session, monkeypatch
) -> None:
    """The controller's session is never handed to tool code: the callable
    receives a *different* session bound to the same engine, and both stay
    usable after a stuck call."""
    release = threading.Event()
    seen: dict[str, Any] = {}

    def probe(db: Session, **kwargs: Any) -> dict[str, Any]:
        seen["is_controller_session"] = db is session
        seen["bind_is_same_engine"] = db.get_bind() is session.get_bind()
        release.wait(5.0)
        return {"tool": "query_ledger", "status": "ok"}

    _patch_tool(
        monkeypatch,
        "query_ledger",
        timeout_seconds=0.3,
        max_retries=0,
        callable=probe,
    )

    result = dispatch_tool(session, "query_ledger", {"limit": 1})
    release.set()

    assert result["error_type"] == "TOOL_TIMEOUT"
    assert seen["is_controller_session"] is False
    assert seen["bind_is_same_engine"] is True
    # The controller session survived the stuck call and still reads.
    count = session.execute(
        select(func.count()).select_from(Transaction)
    ).scalar_one()
    assert count > 0


# ---------------------------------------------------------------------------
# Provider resilience (Gemini adapter, offline injected client)
# ---------------------------------------------------------------------------


def _fake_response(text: str) -> Any:
    """A minimal ``GenerateContentResponse`` stand-in for the adapter."""
    return SimpleNamespace(
        text=text,
        function_calls=None,
    )


def test_gemini_provider_retries_transient_errors_and_succeeds() -> None:
    """429 then 500 then success: the adapter retries with bounded
    backoff (no network) and returns the parsed LLMResponse."""
    client = _FakeGeminiClient(
        [
            _HTTPError(429, "rate limited"),
            _HTTPError(503, "server busy"),
            _fake_response("Recovered answer."),
        ]
    )
    provider = GeminiProvider(
        "test-key", max_attempts=3, backoff_seconds=0.01, client=client
    )

    response = provider.generate([TextMessage("hello")], [])

    assert client.calls == 3
    assert response.text == "Recovered answer."
    assert response.tool_calls == []
    assert response.latency_ms >= 0.0


def test_gemini_provider_gives_up_after_max_attempts() -> None:
    """All attempts hit a transient error: the adapter raises
    LLMProviderError (the controller records model_error, never hangs)."""
    client = _FakeGeminiClient([_HTTPError(502, "bad gateway")])
    provider = GeminiProvider(
        "test-key", max_attempts=3, backoff_seconds=0.01, client=client
    )

    with pytest.raises(LLMProviderError, match="Gemini request failed"):
        provider.generate([TextMessage("hello")], [])
    assert client.calls == 3


def test_gemini_provider_fails_fast_on_non_transient_error() -> None:
    """A 400/401-style error is not retried: the adapter fails on the
    first attempt (bounded latency, no retry storm)."""
    client = _FakeGeminiClient([_HTTPError(401, "invalid api key")])
    provider = GeminiProvider(
        "test-key", max_attempts=3, backoff_seconds=0.01, client=client
    )

    with pytest.raises(LLMProviderError, match="invalid api key"):
        provider.generate([TextMessage("hello")], [])
    assert client.calls == 1


# ---------------------------------------------------------------------------
# Schema boundary: blank messages never reach the provider
# ---------------------------------------------------------------------------


def test_blank_chat_message_is_refused_before_the_loop(seeded) -> None:
    """A whitespace-only message gets a 422 at the schema boundary: no
    provider round, no agent_runs row charged."""
    def override_db() -> Iterator[Session]:
        db = Session(seeded.engine)
        try:
            yield db
        finally:
            db.close()

    with Session(seeded.engine) as db:
        before = db.execute(
            select(func.count()).select_from(AgentRun)
        ).scalar_one()

    provider = FakeProvider(
        [LLMResponse(tool_calls=[], text="should never run")]
    )
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_provider] = lambda: provider
    try:
        with TestClient(app) as client:
            response = client.post("/api/agent/chat", json={"message": "   \n\t"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert provider.calls == 0
    with Session(seeded.engine) as db:
        after = db.execute(
            select(func.count()).select_from(AgentRun)
        ).scalar_one()
    assert after == before


# ---------------------------------------------------------------------------
# Loop integration: TOOL_TIMEOUT flows through the controller
# ---------------------------------------------------------------------------


def test_timeout_envelope_flows_through_controller_as_tool_error(
    seeded, monkeypatch
) -> None:
    """A TOOL_TIMEOUT envelope is traced as a failed tool call, fed back
    to the provider, and the run still completes with an answer (the
    agent never hangs on a stuck tool)."""
    never = threading.Event()  # never set
    _patch_tool(
        monkeypatch,
        "query_ledger",
        timeout_seconds=0.3,
        max_retries=0,
        callable=_stuck_tool(never),
    )

    provider = FakeProvider(
        [
            LLMResponse(
                text=None,
                tool_calls=[
                    LLMToolCall(id="slow-1", name="query_ledger", args={"limit": 1})
                ],
            ),
            LLMResponse(
                tool_calls=[],
                text="The ledger query timed out; nothing to report.",
            ),
        ]
    )

    with Session(seeded.engine) as db:
        controller = AgentController(provider, db, max_tool_calls=3)
        result = controller.run("check the ledger")

    assert result["status"] == STATUS_COMPLETED
    assert result["answer"] == "The ledger query timed out; nothing to report."
    assert len(result["tool_calls"]) == 1
    call = result["tool_calls"][0]
    assert call["status"] == "error"
    assert call["error"] == "TOOL_TIMEOUT"
    assert call["result"]["error_type"] == "TOOL_TIMEOUT"

    # The envelope was fed back to the provider as a tool result.
    results_msg = provider.seen[1]["messages"][2]
    assert isinstance(results_msg, ToolResultsMessage)
    assert results_msg.results[0].result["error_type"] == "TOOL_TIMEOUT"
    assert results_msg.results[0].id == "slow-1"

    # The trace row records the timeout as the tool error.
    with Session(seeded.engine) as db:
        row = db.execute(
            select(ToolCall).where(ToolCall.tool_name == "query_ledger")
        ).scalars().first()
    assert row is not None
    assert row.status == "error"
    assert row.error == "TOOL_TIMEOUT"




