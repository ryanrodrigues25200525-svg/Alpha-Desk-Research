"""The run index: one JSONL line per attempt, tolerant of damage and never fatal."""

from alpha_desk_research.memory.run_index import RunIndex, RunRecord, default_index_path


def _cfg(path):
    return {
        "memory_log_path": str(path / "trading_memory.md"),
        "run_index_path": str(path / "runs.jsonl"),
    }


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
