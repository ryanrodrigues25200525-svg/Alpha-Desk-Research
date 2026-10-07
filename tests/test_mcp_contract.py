"""MCP server contract tests (Task 5, Steps 1–2 + 6b).

Uses an injected fake graph so no test touches the network or an LLM.
"""

import threading
import time
from contextlib import nullcontext
from pathlib import Path

import pytest

from mcp_server import jobs
from mcp_server.server import (
    DEPTH_PRESETS,
    get_report,
    job_status,
    list_reports,
    submit_research_job,
)
from mcp_server.worker import restart_worker, start_worker, stop_worker

REPORT_KEYS = [
    "market_report",
    "sentiment_report",
    "news_report",
    "fundamentals_report",
    "investment_plan",
    "trader_investment_plan",
    "final_trade_decision",
]


class _FakePropagator:
    def get_graph_args(self, callbacks=None):
        return {}


class FakeGraph:
    """Same public surface the worker uses on TradingAgentsGraph."""

    def __init__(self, ticker, results_dir, gate=None, chunks=None):
        self.ticker = ticker
        self.results_dir = Path(results_dir)
        # An unset gate blocks inside stream_run (job stays running); a preset
        # one lets the run finish. Never set it here: construction happens in
        # the worker thread, which would release the block before the test sees it.
        if gate is None:
            gate = threading.Event()
            gate.set()
        self.gate = gate
        self.chunks = chunks if chunks is not None else [
            {key: f"# {key} for {ticker}\n\ncontent" for key in REPORT_KEYS}
        ]
        self.propagator = _FakePropagator()

    def checkpoint_scope(self, *args, **kwargs):
        return nullcontext()

    def checkpoint_input(self, init_state):
        return init_state

    def create_run_state(self, company_name, trade_date, asset_type="stock", portfolio=None):
        return {"company_of_interest": company_name, "trade_date": trade_date}

    def stream_run(self, graph_input, **args):
        assert self.gate.wait(timeout=30), "fake graph gate never released"
        for chunk in self.chunks:
            yield [], dict(chunk)

    def record_decision(self, *args, **kwargs):
        return None

    def clear_checkpoint_on_success(self, *args, **kwargs):
        return None

    def save_reports(self, final_state, ticker, save_path=None, html=False):
        save_path = Path(save_path or (self.results_dir / "reports" / f"{ticker}_fake"))
        save_path.mkdir(parents=True, exist_ok=True)
        (save_path / "complete_report.md").write_text(
            f"# Trading Analysis Report: {ticker}\n\n" + "\n\n".join(
                final_state.get(k, "") for k in REPORT_KEYS if final_state.get(k)
            ),
            encoding="utf-8",
        )
        return save_path / "complete_report.md"


def _make_factory(results_dir, gate=None, chunks=None):
    def factory(job):
        return FakeGraph(job["ticker"], results_dir, gate=gate, chunks=chunks)

    return factory


@pytest.fixture()
def isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_RESULTS_DIR", str(tmp_path))
    jobs.configure(str(tmp_path))
    return tmp_path


@pytest.fixture()
def worker(isolated_registry):
    gate = threading.Event()
    gate.set()
    start_worker(graph_factory=_make_factory(isolated_registry, gate=gate))
    yield gate
    stop_worker()


def wait_until_done(job_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = job_status(job_id)["state"]
        if state in ("done", "failed"):
            return state
        time.sleep(0.05)
    raise TimeoutError(f"job {job_id} not done within {timeout}s")


def test_mcp_round_trip(worker):
    job_id = submit_research_job("AAPL", depth_preset="flash")
    assert wait_until_done(job_id) == "done"
    assert job_status(job_id)["state"] == "done"
    assert "# " in get_report(job_id)


def test_invalid_ticker_rejected(isolated_registry):
    with pytest.raises(ValueError):
        submit_research_job("!!!not-a-ticker!!!")


def test_partial_report_while_running(isolated_registry):
    gate = threading.Event()  # stays unset: the run blocks after claiming the job
    chunks = [
        {"market_report": "# market_report for AAPL\n\npartial content"},
        {k: f"# {k} for AAPL\n\ncontent" for k in REPORT_KEYS if k != "market_report"},
    ]
    start_worker(graph_factory=_make_factory(isolated_registry, gate=gate, chunks=chunks))
    try:
        job_id = submit_research_job("AAPL", depth_preset="deep")
        # Spin until the worker has claimed the job, then read partial pre-release.
        deadline = time.time() + 30
        while time.time() < deadline:
            if job_status(job_id)["state"] == "running":
                break
            time.sleep(0.05)
        assert job_status(job_id)["state"] == "running"
        waiting = get_report(job_id, section="market_report")
        assert "in progress" in waiting.lower() and job_id in waiting
        gate.set()
        # Spin until the first section lands, then read partial mid-run.
        deadline = time.time() + 30
        while time.time() < deadline:
            if job_status(job_id)["sections"]:
                break
            time.sleep(0.05)
        partial = get_report(job_id, section="market_report")
        assert "market_report" in partial.lower()
    finally:
        gate.set()
        stop_worker()


def test_crash_requeues_running_job(isolated_registry):
    gate = threading.Event()  # stays unset: the run blocks, job stays running
    start_worker(graph_factory=_make_factory(isolated_registry, gate=gate))
    try:
        job_id = submit_research_job("AAPL", depth_preset="flash")
        deadline = time.time() + 30
        while time.time() < deadline:
            if job_status(job_id)["state"] == "running":
                break
            time.sleep(0.05)
        assert job_status(job_id)["state"] == "running"
        stop_worker()  # simulate crash: worker dies, job row left running
        assert jobs.get_job(job_id)["state"] == "running"
        gate.set()
        restart_worker(graph_factory=_make_factory(isolated_registry, gate=gate))
        assert job_status(job_id)["state"] in ("queued", "running", "done")
    finally:
        gate.set()
        stop_worker()


def test_list_reports(worker):
    job_id = submit_research_job("AAPL", depth_preset="flash")
    assert wait_until_done(job_id) == "done"
    reports = list_reports("AAPL")
    assert any(r["job_id"] == job_id for r in reports)
    assert all("ticker" in r and "state" in r for r in reports)


def test_depth_preset_mapping():
    assert set(DEPTH_PRESETS) == {"flash", "standard", "deep"}
    flash, standard, deep = (
        DEPTH_PRESETS["flash"],
        DEPTH_PRESETS["standard"],
        DEPTH_PRESETS["deep"],
    )
    # flash is analysts-only with debates bypassed; deep is the full graph.
    assert len(flash["analysts"]) <= len(standard["analysts"]) <= len(deep["analysts"])
    assert flash["max_debate_rounds"] <= standard["max_debate_rounds"] <= deep["max_debate_rounds"]
    assert flash["max_risk_discuss_rounds"] == 0
    with pytest.raises(ValueError, match="depth_preset"):
        submit_research_job("AAPL", depth_preset="ultra")


def test_depth_preset_rounds_reach_worker(isolated_registry, monkeypatch):
    """Flash bypass (0/0) and deep debates (1/1) persist and reach graph config."""
    import sys
    import types
    from contextlib import nullcontext

    from mcp_server.worker import build_graph_factory

    flash_id = submit_research_job("AAPL", depth_preset="flash")
    deep_id = submit_research_job("AAPL", depth_preset="deep")
    flash_job = jobs.get_job(flash_id)
    deep_job = jobs.get_job(deep_id)
    assert (flash_job["max_debate_rounds"], flash_job["max_risk_discuss_rounds"]) == (0, 0)
    assert (deep_job["max_debate_rounds"], deep_job["max_risk_discuss_rounds"]) == (1, 1)

    seen = {}

    class _StubGraph:
        def __init__(self, selected_analysts, config):
            seen["analysts"] = selected_analysts
            seen["config"] = config

    monkeypatch.setitem(
        sys.modules,
        "tradingagents.graph.trading_graph",
        types.SimpleNamespace(TradingAgentsGraph=_StubGraph),
    )
    monkeypatch.setitem(
        sys.modules,
        "tradingagents.dataflows.config",
        types.SimpleNamespace(run_config_context=lambda config: nullcontext()),
    )
    factory = build_graph_factory()
    factory(flash_job)
    assert seen["config"]["max_debate_rounds"] == 0
    assert seen["config"]["max_risk_discuss_rounds"] == 0
    factory(deep_job)
    assert seen["config"]["max_debate_rounds"] == 1
    assert seen["config"]["max_risk_discuss_rounds"] == 1


def test_structured_error_strings(isolated_registry, worker):
    from mcp_server.server import (
        tool_get_report,
        tool_job_status,
        tool_list_reports,
        tool_submit_research_job,
    )

    assert tool_submit_research_job("!!!not-a-ticker!!!").startswith("ERROR:")
    assert tool_job_status("job_does_not_exist").startswith("ERROR:")
    assert tool_get_report("job_does_not_exist").startswith("ERROR:")
    assert tool_get_report("job_does_not_exist", section="nope").startswith("ERROR:")
    assert isinstance(tool_list_reports("AAPL"), str)
