"""FastMCP stdio server: submit → poll → read research jobs.

Public functions raise ``ValueError`` on bad input; the ``tool_*`` wrappers
return structured ``"ERROR: ...""`` strings instead so no exception crosses
the MCP boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

from tradingagents.dataflows.symbols import safe_ticker_component

from . import jobs
from .worker import start_worker, stop_worker

# Presets map to graph shape: analysts-only + no debates (flash), fewer
# analysts + no risk debate (standard), full graph (deep).
DEPTH_PRESETS = {
    "flash": {
        "analysts": ["market", "news"],
        "max_debate_rounds": 0,
        "max_risk_discuss_rounds": 0,
    },
    "standard": {
        "analysts": ["market", "social", "news", "fundamentals"],
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 0,
    },
    "deep": {
        "analysts": ["market", "social", "news", "fundamentals"],
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
    },
}

ANALYST_KEYS = ("market", "social", "news", "fundamentals")

# get_report(section=...) aliases to the state keys holding each section.
SECTION_ALIASES = {
    "market_report": "market_report",
    "sentiment_report": "sentiment_report",
    "news_report": "news_report",
    "fundamentals_report": "fundamentals_report",
    "market": "market_report",
    "sentiment": "sentiment_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
    "investment_plan": "investment_plan",
    "research": "investment_plan",
    "trader_investment_plan": "trader_investment_plan",
    "trading": "trader_investment_plan",
    "final_trade_decision": "final_trade_decision",
    "decision": "final_trade_decision",
    "portfolio": "final_trade_decision",
}


def _default_trade_date() -> str:
    from tradingagents.dataflows.date_window import get_current_date

    return get_current_date()


def submit_research_job(
    ticker: str,
    trade_date: str | None = None,
    analysts: list[str] | None = None,
    depth_preset: str = "deep",
) -> str:
    """Queue a research job; return its id. Raises ``ValueError`` on bad input."""
    safe = safe_ticker_component(ticker)
    if depth_preset not in DEPTH_PRESETS:
        raise ValueError(
            f"unknown depth_preset: {depth_preset!r}; choose from {', '.join(sorted(DEPTH_PRESETS))}"
        )
    preset = DEPTH_PRESETS[depth_preset]
    if analysts is None:
        chosen = list(preset["analysts"])
    else:
        names = [{"sentiment": "social"}.get(a.strip().lower(), a.strip().lower()) for a in analysts if a.strip()]
        unknown = [a for a in names if a not in ANALYST_KEYS]
        if unknown:
            raise ValueError(f"unknown analyst(s) {', '.join(unknown)}; choose from market, sentiment, news, fundamentals")
        if not names:
            raise ValueError("name at least one analyst")
        chosen = names
    date = trade_date if trade_date is not None else _default_trade_date()
    from tradingagents.graph.trading_graph import _validate_trade_date

    date = _validate_trade_date(date)
    return jobs.create_job(
        ticker=safe,
        trade_date=date,
        analysts=chosen,
        depth_preset=depth_preset,
        max_debate_rounds=preset["max_debate_rounds"],
        max_risk_discuss_rounds=preset["max_risk_discuss_rounds"],
    )


def job_status(job_id: str) -> dict:
    """A job's state + progress; raises ``ValueError`` when unknown."""
    job = jobs.get_job(job_id)
    if job is None:
        raise ValueError(f"unknown job_id: {job_id!r}")
    return {
        "job_id": job["job_id"],
        "ticker": job["ticker"],
        "trade_date": job["trade_date"],
        "state": job["state"],
        "progress": {k: len(v) for k, v in job["progress"].items()},
        "sections": sorted(job["progress"]),
        "report_dir": job["report_dir"],
        "error": job["error"],
    }


def get_report(job_id: str, section: str | None = None) -> str:
    """A job's report markdown; partial-with-progress while still running.

    Raises ``ValueError`` for unknown jobs or sections.
    """
    job = jobs.get_job(job_id)
    if job is None:
        raise ValueError(f"unknown job_id: {job_id!r}")
    if section is not None:
        key = SECTION_ALIASES.get(section.strip().lower())
        if key is None:
            raise ValueError(
                f"unknown section: {section!r}; choose from {', '.join(sorted(SECTION_ALIASES))}"
            )
    if job["state"] == "failed":
        raise ValueError(job["error"] or f"job {job_id} failed")
    if job["state"] == "done" and job["report_dir"]:
        complete = Path(job["report_dir"]) / "complete_report.md"
        if complete.exists():
            text = complete.read_text(encoding="utf-8")
            if section is None:
                return text
            body = job["progress"].get(key, "")
            if body:
                return body
            raise ValueError(f"section {section!r} has no content in job {job_id}")
    # Queued / running / done-without-file-yet: partial with an in-progress marker.
    progress = job["progress"]
    if section is not None:
        body = progress.get(key, "")
        if body:
            if job["state"] in ("queued", "running"):
                return f"> Note: job {job_id} is {job['state']} — partial {section} below.\n\n{body}"
            return body
        if job["state"] in ("queued", "running"):
            return (
                f"Job {job_id} is {job['state']}: {section} is in progress "
                f"({len(progress)} of {len(ANALYST_KEYS)} analyst sections ready)."
            )
        raise ValueError(f"section {section!r} has no content in job {job_id}")
    if not progress:
        return f"Job {job_id} is {job['state']}: report is in progress (no sections ready yet)."
    parts = [f"# Trading Analysis Report: {job['ticker']} (in progress: {job['state']})", ""]
    for name, body in progress.items():
        parts += [f"## {name}", "", body, ""]
    return "\n".join(parts)


def list_reports(ticker: str | None = None) -> list[dict]:
    """Done jobs (newest first), optionally for one ticker."""
    safe = safe_ticker_component(ticker) if ticker is not None else None
    return [
        {
            "job_id": job["job_id"],
            "ticker": job["ticker"],
            "trade_date": job["trade_date"],
            "state": job["state"],
            "report_dir": job["report_dir"],
        }
        for job in jobs.list_jobs(safe)
        if job["state"] == "done"
    ]


def cancel_research_job(job_id: str) -> dict:
    """Cancel a ``queued``/``running`` job; raises ``ValueError`` when unknown or terminal."""
    return jobs.cancel_job(job_id)


def get_decision(job_id: str) -> str:
    """Just the final trade decision text; raises ``ValueError`` when missing."""
    job = jobs.get_job(job_id)
    if job is None:
        raise ValueError(f"unknown job_id: {job_id!r}")
    if job["state"] == "failed":
        raise ValueError(job["error"] or f"job {job_id} failed")
    decision = job["progress"].get("final_trade_decision", "")
    if decision:
        if job["state"] in ("queued", "running"):
            return f"> Note: job {job_id} is {job['state']} — partial decision below.\n\n{decision}"
        return decision
    if job["state"] in ("queued", "running"):
        return f"Job {job_id} is {job['state']}: no decision yet."
    raise ValueError(f"job {job_id} has no decision recorded")


def _err(exc: Exception) -> str:
    return f"ERROR: {exc}"


def tool_cancel_research_job(job_id: str) -> str:
    try:
        return json.dumps(cancel_research_job(job_id))
    except Exception as exc:
        return _err(exc)


def tool_get_decision(job_id: str) -> str:
    try:
        return get_decision(job_id)
    except Exception as exc:
        return _err(exc)


def tool_submit_research_job(
    ticker: str,
    trade_date: str | None = None,
    analysts: list[str] | None = None,
    depth_preset: str = "deep",
) -> str:
    try:
        return submit_research_job(ticker, trade_date, analysts, depth_preset)
    except Exception as exc:
        return _err(exc)


def tool_job_status(job_id: str) -> str:
    try:
        return json.dumps(job_status(job_id))
    except Exception as exc:
        return _err(exc)


def tool_get_report(job_id: str, section: str | None = None) -> str:
    try:
        return get_report(job_id, section)
    except Exception as exc:
        return _err(exc)


def tool_list_reports(ticker: str | None = None) -> str:
    try:
        return json.dumps(list_reports(ticker))
    except Exception as exc:
        return _err(exc)


def build_app():
    """The FastMCP stdio app with the six research tools."""
    from fastmcp import FastMCP

    app = FastMCP("alpha-desk-research")

    @app.tool(name="submit_research_job")
    def _submit(
        ticker: str,
        trade_date: str | None = None,
        analysts: list[str] | None = None,
        depth_preset: str = "deep",
    ) -> str:
        """Queue a full-graph research job; returns a job_id to poll."""
        return tool_submit_research_job(ticker, trade_date, analysts, depth_preset)

    @app.tool(name="job_status")
    def _status(job_id: str) -> str:
        """A job's state + per-section progress as JSON."""
        return tool_job_status(job_id)

    @app.tool(name="get_report")
    def _report(job_id: str, section: str | None = None) -> str:
        """A job's report markdown; partial-with-progress while running."""
        return tool_get_report(job_id, section)

    @app.tool(name="list_reports")
    def _list(ticker: str | None = None) -> str:
        """Done-job reports as JSON, optionally for one ticker."""
        return tool_list_reports(ticker)

    @app.tool(name="cancel_research_job")
    def _cancel(job_id: str) -> str:
        """Cancel a queued/running job; returns its row as JSON."""
        return tool_cancel_research_job(job_id)

    @app.tool(name="get_decision")
    def _decision(job_id: str) -> str:
        """Just the final trade decision text for a job."""
        return tool_get_decision(job_id)

    return app


def main() -> None:
    """Serve stdio: start the worker, run the app, stop on exit."""
    start_worker()
    try:
        build_app().run()
    finally:
        stop_worker()


if __name__ == "__main__":
    main()
