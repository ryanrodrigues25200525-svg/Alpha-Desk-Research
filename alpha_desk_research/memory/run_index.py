"""The run index: one JSONL line per run attempt, beside the memory log.

The memory log records decisions. The index records *attempts*: every run that
reached the desk, including the ones that produced nothing tradeable, so a
wasted run is counted rather than lost. Each line carries what produced the run
(``settings``, the ``run_settings()`` allowlist) and who it was for, which is
what makes a run comparable with the next one.

The index is instrumentation: a failure to append never fails the run it
describes, and reading tolerates the world -- a malformed line is skipped and
counted, a missing file is an empty index.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from alpha_desk_research.dataflows.files import locked
from alpha_desk_research.default_config import build_default_config

logger = logging.getLogger(__name__)


def default_index_path(config: dict) -> Path:
    """Where a run's index goes: the configured path, else beside the memory log.

    The index is a sidecar of the log it annotates, so an unset path follows
    that log -- a backtest writing its own log writes its own index beside it.
    """
    path = config.get("run_index_path")
    if path:
        return Path(path).expanduser()
    return Path(config["memory_log_path"]).with_name("runs.jsonl")


@dataclass
class RunRecord:
    """One run attempt: who, when, what it decided, and what produced it."""

    ts: str
    ticker: str
    trade_date: str
    rating: str
    asset_type: str = "stock"
    signature: str = ""
    settings: dict = field(default_factory=dict)


_FIELDS = frozenset(f.name for f in fields(RunRecord))


class RunIndex:
    """Append-only JSONL record of run attempts, held by one writer at a time."""

    def __init__(self, config: dict | None = None):
        # No config is the live configuration, so a default construction still
        # knows where the log it annotates lives; an explicit dict is taken as
        # given, whether from a run or from a test.
        self.path = default_index_path(build_default_config() if config is None else config)

    def append(self, record: RunRecord) -> bool:
        """Add one line for ``record``. False, with a warning, when it could not be written.

        The write is the run's instrumentation, not part of it: a full disk, a
        read-only directory, a path another process holds or a record that does
        not serialize must degrade to a missing line, never to a failed run.
        """
        try:
            line = json.dumps(asdict(record), ensure_ascii=False)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with locked(self.path), open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Could not add a run to the index %s (%s)", self.path, exc)
            return False
        return True

    def load(self) -> tuple[list[RunRecord], int]:
        """Every record, in file order, and how many lines could not be read.

        A line that is not a JSON object of record fields is counted and
        skipped: one damaged line must not hide the runs around it.
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return [], 0
        except OSError as exc:
            logger.warning("Could not read the run index %s (%s)", self.path, exc)
            return [], 0
        records: list[RunRecord] = []
        malformed = 0
        for line in raw.splitlines():
            try:
                data = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if not isinstance(data, dict) or not data.get("ticker") or not data.get("trade_date"):
                malformed += 1
                continue
            try:
                records.append(RunRecord(**{k: v for k, v in data.items() if k in _FIELDS}))
            except TypeError:
                malformed += 1
        return records, malformed
