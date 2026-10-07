"""Background worker: run queued jobs on the TradingAgents graph."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from tradingagents.default_config import DEFAULT_CONFIG

from . import jobs

logger = logging.getLogger(__name__)

# At most this many jobs run at once; the rest wait queued.
MAX_CONCURRENT_JOBS = 2

# Job config keys mirrored from the graph's live config for one run.
_CONFIG_KEYS = (
    "llm_provider",
    "deep_think_llm",
    "quick_think_llm",
    "max_debate_rounds",
    "max_risk_discuss_rounds",
    "max_tool_rounds",
    "output_language",
    "data_vendors",
    "tool_vendors",
    "checkpoint_enabled",
    "memory_log_path",
)

_worker: _Worker | None = None
_lock = threading.Lock()


def build_graph_factory():
    """Build real ``TradingAgentsGraph`` instances (imported late: heavy deps)."""

    def factory(job: dict):
        from tradingagents.dataflows.config import run_config_context
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        config = dict(DEFAULT_CONFIG)
        for key in _CONFIG_KEYS:
            if key in DEFAULT_CONFIG:
                config[key] = DEFAULT_CONFIG[key]
        config["results_dir"] = str(Path(config["results_dir"]))
        config["max_debate_rounds"] = job.get("max_debate_rounds", config["max_debate_rounds"])
        config["max_risk_discuss_rounds"] = job.get(
            "max_risk_discuss_rounds", config["max_risk_discuss_rounds"]
        )
        return TradingAgentsGraph(
            selected_analysts=tuple(job["analysts"]),
            config=config,
        ), run_config_context(config)

    return factory


class _Worker:
    """One background thread pulling ``queued`` jobs, up to the cap."""

    def __init__(self, graph_factory=None, poll_interval: float = 0.2):
        self._graph_factory = graph_factory or build_graph_factory()
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, name="mcp-worker", daemon=True)

    def start(self) -> None:
        requeued = jobs.requeue_running()
        if requeued:
            logger.info("Re-queued %d interrupted job(s): %s", len(requeued), requeued)
        self._thread.start()

    def stop(self, timeout: float = 30) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)

    def _claim(self) -> dict | None:
        with self._in_flight_lock:
            if self._in_flight >= MAX_CONCURRENT_JOBS:
                return None
            for job in jobs.list_jobs():
                if job["state"] == "queued":
                    jobs.set_state(job["job_id"], "running")
                    self._in_flight += 1
                    return jobs.get_job(job["job_id"])
            return None

    def _release(self) -> None:
        with self._in_flight_lock:
            self._in_flight = max(0, self._in_flight - 1)

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self._claim()
            if job is None:
                time.sleep(self._poll_interval)
                continue
            runner = threading.Thread(
                target=self._run_job, args=(job,), name=f"mcp-job-{job['job_id']}", daemon=True
            )
            runner.start()

    def _run_job(self, job: dict) -> None:
        job_id = job["job_id"]
        try:
            made = self._graph_factory(job)
            graph, ctx = made if isinstance(made, tuple) else (made, None)
            final_state: dict = {}
            if ctx is not None:
                with ctx:
                    final_state = self._stream(graph, job, final_state)
            else:
                final_state = self._stream(graph, job, final_state)
            results_dir = getattr(graph, "config", {}).get("results_dir") if isinstance(
                getattr(graph, "config", None), dict
            ) else None
            save_path = Path(results_dir) / "reports" / job_id if results_dir else None
            report_path = graph.save_reports(final_state, job["ticker"], save_path=save_path, html=False)
            jobs.set_state(job_id, "done", report_dir=str(report_path.parent))
        except Exception as exc:  # never raises across the worker: the row says failed
            logger.exception("Job %s failed", job_id)
            try:
                jobs.set_state(job_id, "failed", error=f"ERROR: job failed: {exc}")
            except Exception:
                logger.exception("Could not record failure for job %s", job_id)
        finally:
            self._release()

    def _stream(self, graph, job: dict, final_state: dict) -> dict:
        init_state = graph.create_run_state(job["ticker"], job["trade_date"])
        args = graph.propagator.get_graph_args()
        # checkpoint_scope recompiles with the per-ticker checkpointer when
        # enabled, so a restarted worker resumes instead of starting over.
        with graph.checkpoint_scope(job["ticker"], job["trade_date"]) as checkpoint_id:
            if checkpoint_id is not None:
                args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = checkpoint_id
            stream = graph.stream_run(graph.checkpoint_input(init_state), **args)
            for _messages, state in stream:
                if not isinstance(state, dict):
                    continue
                for key, value in state.items():
                    if key.endswith("_report") and value and final_state.get(key) != value:
                        final_state[key] = value
                        try:
                            jobs.set_progress(job["job_id"], key, value)
                        except Exception:
                            logger.warning("Could not record progress for %s", job["job_id"])
                for key in (
                    "investment_plan",
                    "trader_investment_plan",
                    "final_trade_decision",
                ):
                    if state.get(key) and final_state.get(key) != state[key]:
                        final_state[key] = state[key]
                        try:
                            jobs.set_progress(job["job_id"], key, state[key])
                        except Exception:
                            logger.warning("Could not record progress for %s", job["job_id"])
            final_state.setdefault("company_of_interest", job["ticker"])
            final_state.setdefault("trade_date", job["trade_date"])
            graph.record_decision(job["ticker"], job["trade_date"], final_state)
            graph.clear_checkpoint_on_success(job["ticker"], job["trade_date"])
            return final_state


def start_worker(graph_factory=None, poll_interval: float = 0.2) -> None:
    """Start the single background worker (idempotent within a process)."""
    global _worker
    with _lock:
        if _worker is not None:
            return
        _worker = _Worker(graph_factory=graph_factory, poll_interval=poll_interval)
        _worker.start()


def stop_worker() -> None:
    """Stop the background worker; in-flight jobs keep their ``running`` rows."""
    global _worker
    with _lock:
        worker, _worker = _worker, None
    if worker is not None:
        worker.stop()


def restart_worker(graph_factory=None, poll_interval: float = 0.2) -> list[str]:
    """Restart after a crash: re-queue ``running`` jobs, then start again."""
    stop_worker()
    requeued = jobs.requeue_running()
    start_worker(graph_factory=graph_factory, poll_interval=poll_interval)
    return requeued
