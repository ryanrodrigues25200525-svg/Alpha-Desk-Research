"""The run index: one JSONL line per attempt, tolerant of damage and never fatal."""

import copy
import json

from alpha_desk_research.default_config import DEFAULT_CONFIG
from alpha_desk_research.graph.trading_graph import AlphaDeskGraph
from alpha_desk_research.memory.run_index import RunIndex, RunRecord, default_index_path


def _cfg(path):
    return {
        "memory_log_path": str(path / "trading_memory.md"),
        "run_index_path": str(path / "runs.jsonl"),
    }


# A complete configuration, so a graph can be built for real (no endpoint is
# called). The index path is empty, so a run's index follows its own memory log.
MINIMAL_CFG = {**copy.deepcopy(DEFAULT_CONFIG), "run_index_path": ""}


def _graph(tmp_path):
    """A graph on a temp memory log, wired for the index alone.

    Real ``__init__`` (the index is built there), with the state log stubbed the
    way test_cli_memory_log.py stubs it: these tests are about the index, and
    they drive ``record_decision`` with the little state the index reads.
    """
    graph = AlphaDeskGraph(config={**MINIMAL_CFG, "memory_log_path": str(tmp_path / "log.md")})
    graph._log_state = lambda *a: None
    return graph


def test_appending_records_round_trips(tmp_path):
    idx = RunIndex(_cfg(tmp_path))
    rec = RunRecord(
        ts="2026-10-10T21:11:51+00:00",
        ticker="CAT",
        trade_date="2026-10-09",
        rating="Buy",
        settings={"deep_think_llm": "m"},
    )
    assert idx.append(rec) is True
    assert idx.load() == ([rec], 0)


def test_the_index_sits_beside_the_memory_log_when_unset(tmp_path):
    log = {"memory_log_path": str(tmp_path / "trading_memory.md")}
    assert default_index_path(log) == tmp_path / "runs.jsonl"


def test_a_malformed_line_is_skipped_and_counted(tmp_path):
    idx = RunIndex(_cfg(tmp_path))
    idx.append(RunRecord(ts="t", ticker="CAT", trade_date="2026-10-09", rating="Buy"))
    idx.path.write_text(idx.path.read_text() + "{not json\n", encoding="utf-8")
    records, malformed = idx.load()
    assert [r.ticker for r in records] == ["CAT"] and malformed == 1


def test_a_missing_index_is_empty_not_an_error(tmp_path):
    assert RunIndex(_cfg(tmp_path)).load() == ([], 0)


def test_a_write_failure_is_reported_not_raised(tmp_path):
    idx = RunIndex(_cfg(tmp_path))
    idx.path.mkdir()  # a directory where the file belongs: the write must not raise
    assert (
        idx.append(RunRecord(ts="t", ticker="CAT", trade_date="2026-10-09", rating="Buy")) is False
    )


def test_an_unserializable_record_is_reported_not_raised(tmp_path):
    idx = RunIndex(_cfg(tmp_path))
    bad = RunRecord(
        ts="t",
        ticker="CAT",
        trade_date="2026-10-09",
        rating="Buy",
        settings={"x": object()},
    )
    assert idx.append(bad) is False


def test_a_default_construction_uses_the_live_configuration():
    assert RunIndex().path.name == "runs.jsonl"


def test_the_index_path_comes_from_the_environment_like_its_siblings(tmp_path, monkeypatch):
    from alpha_desk_research.default_config import DEFAULT_CONFIG, build_default_config

    monkeypatch.setenv("ALPHA_DESK_RUN_INDEX_PATH", str(tmp_path / "custom.jsonl"))
    assert DEFAULT_CONFIG["run_index_path"].endswith("runs.jsonl")  # import-time default
    assert build_default_config()["run_index_path"] == str(tmp_path / "custom.jsonl")
    assert default_index_path(build_default_config()) == tmp_path / "custom.jsonl"


def test_a_run_with_no_decision_is_still_counted(tmp_path):
    graph = _graph(tmp_path)
    graph.record_decision("NVDA", "2026-01-10", {})
    records, _ = graph.run_index.load()
    assert [r.rating for r in records] == ["REVIEW"]  # nothing tradeable, still a wasted run


def test_a_scored_run_records_its_settings_and_rating(tmp_path):
    graph = _graph(tmp_path)  # same helper style as test_cli_memory_log.py
    graph.record_decision("NVDA", "2026-01-10", {"final_trade_decision": "Rating: Buy\n\nbuy"})
    record = graph.run_index.load()[0][0]
    assert (
        record.rating == "Buy"
        and record.settings["deep_think_llm"] == graph.config["deep_think_llm"]
    )


def test_the_index_file_records_settings_without_paths_or_secrets(tmp_path):
    graph = _graph(tmp_path)
    graph.record_decision("NVDA", "2026-01-10", {"final_trade_decision": "Rating: Buy\n\nbuy"})
    line = json.loads(graph.run_index.path.read_text(encoding="utf-8").splitlines()[0])
    assert not [k for k in line["settings"] if k.endswith(("_path", "_key", "_url"))]
    assert str(tmp_path) not in json.dumps(line["settings"])
