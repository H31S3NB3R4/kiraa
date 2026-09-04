"""Phase 7 tests: multi-turn agent state (``run_id`` continuation).

Same fixture pattern as the Phase 6 suite: the dev dataset (seed 42, 100
transactions, 1 exception per type) is seeded into a temp SQLite DB, and
every multi-turn behavior is exercised with scripted fake providers (no
network, no API key required):

- a follow-up that passes the previous ``run_id`` extends the *same* run:
  ``turn_count`` grows, ``user_query`` becomes the latest message, and the
  ``agent_messages`` transcript continues one dense per-run seq space,
- the 3-turn investigation flow (reconcile -> biggest exceptions ->
  investigate the top one) accumulates tool calls across turns,
- replay feeds the saved conversation back to the provider: user text,
  requested tool calls, the exact results the provider saw (without the
  ``latency_ms`` envelope key), and final answers -- in order, followed by
  the new user message,
- the tool-call budget spans the whole run: a follow-up cannot bypass it,
  and the refused round is persisted with ``tool_limit_hit`` (replayed as
  plain model text, never as an unanswered tool round),
- replay is bounded by ``max_history_messages`` and never starts with an
  orphan tool batch,
- a provider failure on a follow-up ends that turn with status
  ``model_error`` and a fallback answer appended to the transcript, and
  the run can still be continued afterwards,
- unknown ``run_id``s raise ``AgentRunNotFoundError`` (404 on the route,
  never a silent new conversation),
- ``POST /api/agent/chat`` continues a run end-to-end and reports
  ``turn_count`` (architecture section 13).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.controller import (
    STATUS_COMPLETED,
    STATUS_MODEL_ERROR,
    STATUS_TOOL_LIMIT,
    AgentController,
    AgentRunNotFoundError,
    run_agent,
)
from app.agent.providers.base import (
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    LLMToolCall,
    TextMessage,
    ToolCallsMessage,
    ToolResultsMessage,
)
from app.agent.tool_registry import TOOL_DECLARATIONS
from app.api.routes.agent import get_provider
from app.db.session import get_db
from app.main import app
from app.models import AgentMessage, AgentRun, ToolCall
from app.services.dataset_generator import generate_dataset, write_dataset
from app.services.db_seed import build_engine, load_dataset_file, seed_database

KWS = {"transactions": 100, "window_days": 28, "exceptions_per_type": 1,
       "customers": 80, "seed": 42}
END = date(2026, 9, 3)  # same anchor day as the Phase 3-6 suites

TOOL_LIMIT_PREFIX = "I stopped after reaching the configured tool-call limit"
MODEL_ERROR_PREFIX = "The AI model is temporarily unavailable"


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


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """Generate the dev dataset, write it to JSON, then seed a temp DB."""
    out_dir = tmp_path_factory.mktemp("phase7")
    dataset = generate_dataset(**KWS, end_date=END)
    json_path, _labels_path = write_dataset(dataset, out_dir, "dataset")

    bundle = type("SeededDb", (), {})()
    bundle.dataset = load_dataset_file(json_path)
    bundle.engine = build_engine(f"sqlite:///{out_dir / 'finance.db'}")
    bundle.counts = seed_database(bundle.engine, bundle.dataset)
    bundle.txns = {t["transaction_id"]: t for t in dataset["transactions"]}
    return bundle


@pytest.fixture
def session(seeded) -> Iterator[Session]:
    """One fresh session on the shared module-scoped database."""
    db = Session(seeded.engine)
    try:
        yield db
    finally:
        db.close()


def _call(
    call_id: str, name: str, args: dict[str, Any], latency_ms: float = 0.0
) -> LLMResponse:
    """One provider round requesting a single tool call."""
    return LLMResponse(
        text=None,
        tool_calls=[LLMToolCall(id=call_id, name=name, args=dict(args))],
        latency_ms=latency_ms,
    )


def _final(text: str, latency_ms: float = 0.0) -> LLMResponse:
    """One provider round with the final answer and no tool calls."""
    return LLMResponse(text=text, tool_calls=[], latency_ms=latency_ms)


def _transcript(db: Session, run_id: str) -> list[AgentMessage]:
    """The run's transcript events ordered by seq (dense per run)."""
    return list(
        db.execute(
            select(AgentMessage)
            .where(AgentMessage.run_id == run_id)
            .order_by(AgentMessage.seq)
        ).scalars()
    )


# --- multi-turn continuation: run state -------------------------------------


def test_follow_up_extends_the_same_run(seeded, session: Session) -> None:
    """Passing the previous run_id back extends the *same* run row: the
    transcript continues one dense per-run seq space, turn_count grows,
    and user_query tracks the latest message (architecture section 11)."""
    provider = FakeProvider(
        [
            _final("Week reconciled."),
            _final("The biggest exception is a missing settlement."),
        ]
    )
    first = run_agent(provider, session, "Reconcile this week.")

    rows_before = len(_transcript(session, first["run_id"]))
    second = run_agent(
        provider, session, "What are the biggest exceptions?", run_id=first["run_id"]
    )

    assert second["run_id"] == first["run_id"]
    assert second["turn_count"] == 2

    rows = _transcript(session, second["run_id"])
    # Turn 1 wrote [user, model-final]; turn 2 added [user, model-final].
    assert [m.role for m in rows] == ["user", "model", "user", "model"]
    # One dense seq space across the whole conversation (1..N).
    assert [m.seq for m in rows] == [1, 2, 3, 4]
    assert len(rows) == rows_before + 2

    run = session.get(AgentRun, second["run_id"])
    assert run.turn_count == 2
    assert run.user_query == "What are the biggest exceptions?"
    assert run.status == STATUS_COMPLETED

    # Earlier user messages are preserved verbatim in the transcript even
    # though the run row now points at the latest query.
    assert rows[0].content == {"text": "Reconcile this week."}
    assert rows[2].content == {"text": "What are the biggest exceptions?"}
    assert rows[1].content == {
        "text": "Week reconciled.",
        "tool_calls": [],
        "latency_ms": 0.0,
    }


def test_three_turn_investigation_accumulates_tool_calls(seeded, session: Session) -> None:
    """The PRD's 3-turn flow (reconcile -> biggest exceptions ->
    investigate the top one) accumulates tool calls and latency across
    turns on one run row, with a dense per-run tool seq space."""
    provider = FakeProvider(
        [
            _call("c1", "run_reconciliation", {}, latency_ms=10.0),
            _final("Reconciled this week; several exceptions flagged.", latency_ms=5.0),
            _call("c2", "detect_anomalies", {}, latency_ms=20.0),
            _final("The largest is a missing settlement.", latency_ms=6.0),
            _call("c3", "query_ledger", {"limit": 5}, latency_ms=30.0),
            _final("Investigated the top exception.", latency_ms=7.0),
        ]
    )
    controller = AgentController(provider, session)

    turn1 = controller.run("Reconcile this week.")
    assert turn1["status"] == STATUS_COMPLETED
    assert session.get(AgentRun, turn1["run_id"]).tool_call_count == 1

    turn2 = controller.run(
        "What are the biggest exceptions?", run_id=turn1["run_id"]
    )
    assert turn2["status"] == STATUS_COMPLETED
    # The run row is cumulative across turns; the returned trace is not.
    assert session.get(AgentRun, turn2["run_id"]).tool_call_count == 2
    assert len(turn2["tool_calls"]) == 1

    turn3 = controller.run("Investigate the top one.", run_id=turn1["run_id"])
    assert turn3["status"] == STATUS_COMPLETED
    assert session.get(AgentRun, turn3["run_id"]).tool_call_count == 3
    # Only the current turn's trace is returned (the run row stays cumulative).
    assert [c["tool_name"] for c in turn3["tool_calls"]] == ["query_ledger"]

    run = session.get(AgentRun, turn3["run_id"])
    assert run.turn_count == 3
    assert run.tool_call_count == 3
    assert run.total_llm_latency_ms == pytest.approx(10 + 5 + 20 + 6 + 30 + 7)

    # Tool seq space is dense per run across turns: 1..3.
    tool_rows = session.execute(
        select(ToolCall).where(ToolCall.run_id == run.run_id).order_by(ToolCall.seq)
    ).scalars().all()
    assert [row.seq for row in tool_rows] == [1, 2, 3]
    assert [row.tool_name for row in tool_rows] == [
        "run_reconciliation",
        "detect_anomalies",
        "query_ledger",
    ]


def test_transcript_roles_and_content_shapes(seeded, session: Session) -> None:
    """A turn with one tool round writes user/model/tool/model rows with
    the documented content shapes: model rows carry text+tool_calls+
    latency_ms, tool rows carry full results for audit."""
    provider = FakeProvider(
        [
            _call("c1", "run_reconciliation", {}, latency_ms=12.5),
            _final("Reconciled.", latency_ms=3.0),
        ]
    )
    first = run_agent(provider, session, "Reconcile this week.")

    rows = _transcript(session, first["run_id"])
    assert [m.role for m in rows] == ["user", "model", "tool", "model"]
    by_seq = {m.seq: m for m in rows}

    assert by_seq[1].content == {"text": "Reconcile this week."}
    # Model row requesting the tool call.
    assert by_seq[2].content["text"] is None
    assert by_seq[2].content["tool_calls"] == [
        {"id": "c1", "name": "run_reconciliation", "arguments": {}}
    ]
    assert by_seq[2].content["latency_ms"] == 12.5
    # Tool row mirrors what the provider received: status, and the FULL
    # result payload for audit (replay strips latency_ms separately).
    outcome = by_seq[3].content["results"][0]
    assert outcome["id"] == "c1"
    assert outcome["name"] == "run_reconciliation"
    assert outcome["status"] == "ok"
    assert outcome["error"] is None
    # Success envelopes carry no inner status key; the full payload is kept.
    assert outcome["result"]["tool"] == "run_reconciliation"
    assert "metrics" in outcome["result"]
    assert "latency_ms" not in outcome["result"]
    # Final model row.
    assert by_seq[4].content == {
        "text": "Reconciled.",
        "tool_calls": [],
        "latency_ms": 3.0,
    }


# --- history replay ----------------------------------------------------------


def test_follow_up_replays_saved_conversation_in_order(
    seeded, session: Session
) -> None:
    """Turn 2's first provider round receives the saved conversation in
    order: the earlier user text, the requested tool calls, the exact
    results the live loop delivered (without ``latency_ms``), the model's
    final text, and then the new user message (architecture section 11)."""
    provider = FakeProvider(
        [
            _call("c1", "query_ledger", {"limit": 2}, latency_ms=4.0),
            _final("Two ledger rows found.", latency_ms=2.0),
            _final("They belong to the same merchant.", latency_ms=1.0),
        ]
    )
    first = run_agent(provider, session, "Show me two ledger rows.")
    assert first["status"] == STATUS_COMPLETED

    # What the live loop delivered on turn 1's tool round.
    live = provider.seen[1]["messages"][-1]
    assert isinstance(live, ToolResultsMessage)
    assert "latency_ms" not in live.results[0].result

    second = run_agent(
        provider, session, "Summarize them.", run_id=first["run_id"]
    )
    assert second["status"] == STATUS_COMPLETED

    replayed = provider.seen[2]["messages"]  # turn 2's first round
    assert [type(m).__name__ for m in replayed] == [
        "TextMessage",
        "ToolCallsMessage",
        "ToolResultsMessage",
        "TextMessage",
        "TextMessage",
    ]
    assert replayed[0].text == "Show me two ledger rows."
    assert replayed[0].role == "user"
    assert [(c.id, c.name, c.args) for c in replayed[1].calls] == [
        ("c1", "query_ledger", {"limit": 2})
    ]
    assert [(r.id, r.name) for r in replayed[2].results] == [
        ("c1", "query_ledger")
    ]
    # The replayed payload is exactly what the live round delivered.
    assert replayed[2].results[0].result == live.results[0].result
    assert replayed[3].role == "model"
    assert replayed[3].text == "Two ledger rows found."
    assert replayed[4].role == "user"
    assert replayed[4].text == "Summarize them."
    # The tool contract is the registry's, stable across turns.
    assert provider.seen[2]["tools"] == TOOL_DECLARATIONS


def test_replay_preserves_every_round_of_multi_round_turns(
    seeded, session: Session
) -> None:
    """A turn with two tool rounds replays both request/result pairs in
    order, so a follow-up sees the whole investigation, not just the
    final answer."""
    provider = FakeProvider(
        [
            _call("a", "run_reconciliation", {}, latency_ms=1.0),
            _call("b", "detect_anomalies", {}, latency_ms=1.0),
            _final("Reconciled and scanned for anomalies.", latency_ms=1.0),
            _final("Noted.", latency_ms=1.0),
        ]
    )
    first = run_agent(provider, session, "Investigate this week.")
    assert first["status"] == STATUS_COMPLETED

    second = run_agent(provider, session, "Thanks.", run_id=first["run_id"])
    assert second["status"] == STATUS_COMPLETED

    replayed = provider.seen[3]["messages"]
    assert [type(m).__name__ for m in replayed] == [
        "TextMessage",
        "ToolCallsMessage",
        "ToolResultsMessage",
        "ToolCallsMessage",
        "ToolResultsMessage",
        "TextMessage",
        "TextMessage",
    ]
    assert [c.name for c in replayed[1].calls] == ["run_reconciliation"]
    assert [c.name for c in replayed[3].calls] == ["detect_anomalies"]
    assert replayed[2].results[0].name == "run_reconciliation"
    assert replayed[4].results[0].name == "detect_anomalies"
    assert replayed[5].role == "model"
    assert replayed[5].text == "Reconciled and scanned for anomalies."
    assert replayed[6].text == "Thanks."


# --- tool budget across turns ------------------------------------------------


def test_tool_budget_spans_the_whole_run(seeded, session: Session) -> None:
    """The tool-call budget is per run, not per turn: turn 3 of a run that
    already used 3 of 3 allowed calls is refused before dispatch, and the
    deterministic answer summarizes the whole conversation's tools."""
    provider = FakeProvider(
        [
            _call("c1", "query_ledger", {"limit": 1}, latency_ms=1.0),
            _call("c2", "query_ledger", {"limit": 2}, latency_ms=1.0),
            _final("Turn one done.", latency_ms=1.0),
            _call("c3", "query_ledger", {"limit": 5}, latency_ms=1.0),
            _final("Turn two done.", latency_ms=1.0),
            _call("c4", "detect_anomalies", {}, latency_ms=1.0),
        ]
    )
    first = run_agent(provider, session, "First question.", max_tool_calls=3)
    assert first["status"] == STATUS_COMPLETED
    second = run_agent(
        provider,
        session,
        "Second question.",
        run_id=first["run_id"],
        max_tool_calls=3,
    )
    assert second["status"] == STATUS_COMPLETED

    third = run_agent(
        provider,
        session,
        "Third question.",
        run_id=first["run_id"],
        max_tool_calls=3,
    )

    assert third["status"] == STATUS_TOOL_LIMIT
    assert third["answer"].startswith(TOOL_LIMIT_PREFIX)
    # Run-wide summary: the earlier turns' calls appear even though this
    # turn dispatched nothing.
    assert third["answer"].endswith(
        "query_ledger(limit=1); query_ledger(limit=2); query_ledger(limit=5)."
    )
    assert third["tool_calls"] == []
    assert third["tools_used"] == []

    run = session.get(AgentRun, third["run_id"])
    assert run.tool_call_count == 3  # unchanged: c4 was never dispatched
    rows = session.execute(
        select(ToolCall)
        .where(ToolCall.run_id == run.run_id)
        .order_by(ToolCall.seq)
    ).scalars().all()
    assert [row.tool_name for row in rows] == ["query_ledger"] * 3
    assert [row.seq for row in rows] == [1, 2, 3]

    # The refused round is auditable in the transcript, marked tool_limit_hit.
    last = _transcript(session, run.run_id)[-1]
    assert last.role == "model"
    assert last.content["tool_limit_hit"] is True
    assert last.content["tool_calls"] == [
        {"id": "c4", "name": "detect_anomalies", "arguments": {}}
    ]
    assert last.content["text"] == third["answer"]


def test_tool_limit_round_replays_as_plain_model_text(
    seeded, session: Session
) -> None:
    """A follow-up after a limit trip replays the refused round as the
    deterministic answer text — never as a pending tool round — and the
    run stays continuable."""
    provider = FakeProvider(
        [
            _call("c1", "query_ledger", {"limit": 1}),
            _call("c2", "query_ledger", {"limit": 2}),  # refused by the limit
            _final("Back on track after the limit."),
        ]
    )
    controller = AgentController(provider, session, max_tool_calls=1)
    first = controller.run("Keep going.")
    assert first["status"] == STATUS_TOOL_LIMIT

    second = controller.run("Continue anyway.", run_id=first["run_id"])
    assert second["status"] == STATUS_COMPLETED
    assert second["answer"] == "Back on track after the limit."
    run = session.get(AgentRun, second["run_id"])
    assert run.turn_count == 2
    assert run.tool_call_count == 1  # only c1 was ever executed

    replayed = provider.seen[2]["messages"]
    assert [type(m).__name__ for m in replayed] == [
        "TextMessage",
        "ToolCallsMessage",
        "ToolResultsMessage",
        "TextMessage",
        "TextMessage",
    ]
    limit_text = replayed[3]
    assert limit_text.role == "model"
    assert limit_text.text.startswith(TOOL_LIMIT_PREFIX)
    # The new user message closes the replay: no tool-shaped message was
    # fed back after the refused round (never an unanswered tool batch).
    assert replayed[4].text == "Continue anyway."


def test_history_replay_is_bounded_and_never_orphans_a_tool_batch(
    seeded, session: Session
) -> None:
    """Replay is bounded by ``max_history_messages`` (the most recent
    transcript events only — the persisted transcript stays complete)
    and never begins with a tool batch whose requesting model round fell
    outside the window."""

    def turn_two_replay(max_history_messages: int) -> list[Any]:
        provider = FakeProvider(
            [
                _call("a", "query_ledger", {"limit": 1}),
                _call("b", "detect_anomalies", {}),
                _final("Turn one final."),
                _final("Turn two final."),
            ]
        )
        controller = AgentController(
            provider,
            session,
            max_tool_calls=5,
            max_history_messages=max_history_messages,
        )
        first = controller.run("Turn one question.")
        assert first["status"] == STATUS_COMPLETED
        # Turn 1 wrote [user, model(a), tool, model(b), tool, model(final)].
        rows = _transcript(session, first["run_id"])
        assert [m.role for m in rows] == [
            "user", "model", "tool", "model", "tool", "model",
        ]

        second = controller.run("Turn two question.", run_id=first["run_id"])
        assert second["status"] == STATUS_COMPLETED
        return provider.seen[3]["messages"]

    # Window 3: [model(b), tool, model(final)] stays intact — the b round
    # and its results are replayed together.
    window_three = turn_two_replay(3)
    assert [type(m).__name__ for m in window_three] == [
        "ToolCallsMessage",
        "ToolResultsMessage",
        "TextMessage",
        "TextMessage",
    ]
    assert [c.name for c in window_three[0].calls] == ["detect_anomalies"]
    assert [r.name for r in window_three[1].results] == ["detect_anomalies"]
    assert window_three[2].text == "Turn one final."
    assert window_three[3].text == "Turn two question."

    # Window 2: the naive window [tool, model(final)] would orphan the
    # tool batch; replay drops the leading tool row instead.
    window_two = turn_two_replay(2)
    assert [type(m).__name__ for m in window_two] == ["TextMessage", "TextMessage"]
    assert [m.text for m in window_two] == ["Turn one final.", "Turn two question."]
    assert [m.role for m in window_two] == ["model", "user"]


# --- failure handling on follow-ups -------------------------------------------


def test_model_error_on_follow_up_keeps_run_continuable(
    seeded, session: Session
) -> None:
    """A provider failure on a follow-up ends that turn with status
    model_error and a safe fallback answer appended to the transcript as
    this turn's assistant message -- and the run still continues."""
    provider = FakeProvider(
        [
            _final("Turn one answer."),
            LLMProviderError("Gemini request failed: quota exceeded"),
            _final("Turn three answer, recovered."),
        ]
    )
    controller = AgentController(provider, session)
    first = controller.run("First question.")
    assert first["status"] == STATUS_COMPLETED

    second = controller.run("Second question.", run_id=first["run_id"])
    assert second["status"] == STATUS_MODEL_ERROR
    assert second["run_id"] == first["run_id"]
    assert second["turn_count"] == 2
    assert second["answer"].startswith(MODEL_ERROR_PREFIX)
    assert second["run_id"] in second["answer"]
    assert second["tool_calls"] == []
    assert second["tools_used"] == []

    run = session.get(AgentRun, first["run_id"])
    assert run.status == STATUS_MODEL_ERROR
    assert "quota exceeded" in run.error
    assert run.turn_count == 2
    assert run.final_response is None  # the fallback lives in the transcript

    rows = _transcript(session, first["run_id"])
    assert [m.role for m in rows] == ["user", "model", "user", "model"]
    assert rows[3].content == {
        "text": second["answer"],
        "tool_calls": [],
        "latency_ms": 0.0,
    }

    # The run continues coherently afterwards (turn 3 replays the fallback
    # as the model's text turn for turn 2).
    third = controller.run("Third question.", run_id=first["run_id"])
    assert third["status"] == STATUS_COMPLETED
    assert third["answer"] == "Turn three answer, recovered."
    assert third["turn_count"] == 3

    replayed = provider.seen[2]["messages"]  # turn 3's first round
    assert [type(m).__name__ for m in replayed] == ["TextMessage"] * 5
    assert [m.role for m in replayed] == ["user", "model", "user", "model", "user"]
    assert [m.text for m in replayed][:2] == ["First question.", "Turn one answer."]
    assert replayed[2].text == "Second question."
    assert replayed[3].text == second["answer"]
    assert replayed[4].text == "Third question."


def test_unknown_run_id_raises_not_found(seeded, session: Session) -> None:
    """A follow-up with an unknown run_id raises AgentRunNotFoundError
    instead of silently starting a new conversation -- and leaves no
    half-started run row behind."""
    before = session.execute(
        select(func.count()).select_from(AgentRun)
    ).scalar_one()

    provider = FakeProvider([])
    with pytest.raises(AgentRunNotFoundError, match="RUN-DOES-NOT-EXIST"):
        run_agent(provider, session, "Hello?", run_id="RUN-DOES-NOT-EXIST")

    assert provider.calls == 0  # the provider is never reached
    assert session.execute(
        select(func.count()).select_from(AgentRun)
    ).scalar_one() == before


# --- HTTP API -----------------------------------------------------------------


def test_agent_chat_endpoint_continues_run(seeded) -> None:
    """POST /api/agent/chat passing the previous run_id continues the same
    conversation end-to-end: turn_count grows and the saved history is
    replayed to the provider before the new message (section 13)."""
    provider = FakeProvider(
        [
            _final("Turn one answer."),
            _final("Turn two answer."),
        ]
    )

    def override_db() -> Iterator[Session]:
        db = Session(seeded.engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_provider] = lambda: provider
    try:
        with TestClient(app) as client:
            first = client.post(
                "/api/agent/chat",
                json={"message": "Reconcile this week."},
            )
            body1 = first.json()
            second = client.post(
                "/api/agent/chat",
                json={
                    "message": "What are the biggest exceptions?",
                    "run_id": body1["run_id"],
                },
            )
            body2 = second.json()
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200
    assert body1["status"] == "completed"
    assert body1["turn_count"] == 1

    assert second.status_code == 200
    assert body2["run_id"] == body1["run_id"]
    assert body2["turn_count"] == 2
    assert body2["answer"] == "Turn two answer."

    # The route replayed turn 1 (user + model text) before the new message.
    replayed = provider.seen[1]["messages"]
    assert [type(m).__name__ for m in replayed] == ["TextMessage"] * 3
    assert [m.role for m in replayed] == ["user", "model", "user"]
    assert [m.text for m in replayed] == [
        "Reconcile this week.",
        "Turn one answer.",
        "What are the biggest exceptions?",
    ]


def test_agent_chat_endpoint_unknown_run_id_returns_404(seeded) -> None:
    """POST /api/agent/chat with an unknown run_id returns 404 with the
    controller's message -- never a silent new conversation."""
    provider = FakeProvider([])

    def override_db() -> Iterator[Session]:
        db = Session(seeded.engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_provider] = lambda: provider
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/agent/chat",
                json={"message": "Hello?", "run_id": "RUN-DOES-NOT-EXIST"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert "does not exist" in response.json()["detail"]
    assert provider.calls == 0  # the provider is never reached

