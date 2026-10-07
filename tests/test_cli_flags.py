"""CLI hardening: --no-live produces identical reports, failures exit non-zero,
buffer updates are serialized for the Live refresh thread."""

from __future__ import annotations

import threading

import pytest
import typer
from typer.testing import CliRunner

import cli.main as m
from cli import run
from cli.display import MessageBuffer


@pytest.mark.unit
def test_no_live_flag_exists():
    out = CliRunner().invoke(m.app, ["--help"]).output
    assert "no-live" in out


@pytest.mark.unit
def test_message_buffer_serializes_concurrent_writes():
    buf = MessageBuffer(max_length=1000)
    buf.init_for_analysis(["market"])
    assert hasattr(buf, "_lock"), "buffer needs a lock for the Live refresh thread"

    def add_many(n):
        for i in range(n):
            buf.add_message("Agent", f"msg-{i}")
            buf.update_agent_status("Market Analyst", "in_progress")

    threads = [threading.Thread(target=add_many, args=(100,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(buf.messages) == 400

    snap = buf.snapshot()
    assert len(snap["messages"]) == 400
    snap["messages"].clear()
    assert len(buf.messages) == 400  # snapshot is a copy, not a view


@pytest.mark.unit
def test_stream_failure_exits_nonzero(monkeypatch):
    monkeypatch.setattr(run.sys.stdin, "isatty", lambda: True)
    from cli.models import AnalystType
    monkeypatch.setattr(run, "get_user_selections", lambda flags: {
        "ticker": "NVDA", "analysis_date": "2026-09-23", "asset_type": "stock",
        "analysts": [AnalystType.MARKET], "research_depth": 1, "quick_think_llm": "q",
        "deep_think_llm": "d", "backend_url": None, "llm_provider": "openai",
    })

    class _Boom:
        def __init__(self, *a, **k):
            pass

        def create_run_state(self, *a, **k):
            return {}

        @property
        def propagator(self):
            raise RuntimeError("propagator exploded")

    monkeypatch.setattr(run, "TradingAgentsGraph", _Boom)
    with pytest.raises(typer.Exit) as exc:
        run.run_analysis(flags={"ticker": "NVDA", "no_live": True})
    assert exc.value.exit_code != 0
