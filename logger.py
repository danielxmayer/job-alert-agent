"""
Structured JSON logging and per-run / per-portal metrics.
"""

import json
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone


# ──────────────────────────────────────────────────────
#  JSON log formatter
# ──────────────────────────────────────────────────────
class JSONFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        obj: dict = {
            "ts":     datetime.now(timezone.utc).isoformat(),
            "level":  record.levelname,
            "logger": record.name,
            "msg":    record.getMessage(),
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, ensure_ascii=False)


# ──────────────────────────────────────────────────────
#  Metrics helpers
# ──────────────────────────────────────────────────────
class PortalMetrics:
    """Accumulate scrape stats per portal."""

    def __init__(self) -> None:
        self._data: dict[str, dict] = defaultdict(lambda: {
            "jobs_found": 0,
            "requests":   0,
            "errors":     0,
            "skipped":    0,
            "duration_ms": 0,
        })

    def record(
        self,
        portal: str,
        *,
        jobs_found: int = 0,
        error: bool = False,
        skipped: bool = False,
        duration_ms: int = 0,
    ) -> None:
        m = self._data[portal]
        m["requests"]   += 1
        m["jobs_found"] += jobs_found
        m["duration_ms"] += duration_ms
        if error:
            m["errors"] += 1
        if skipped:
            m["skipped"] += 1

    def summary(self) -> dict:
        return dict(self._data)


class RunMetrics:
    """Aggregate metrics for one complete agent run."""

    def __init__(self) -> None:
        self._start          = time.time()
        self.portals         = PortalMetrics()
        self.total_scraped   = 0
        self.new_jobs        = 0
        self.qualified_jobs  = 0
        self.scoring_cost    = 0.0
        self.email_sent      = False
        self.whatsapp_sent   = False

    def elapsed_ms(self) -> int:
        return int((time.time() - self._start) * 1000)

    def log_summary(self, logger: logging.Logger) -> None:
        summary = {
            "total_scraped":   self.total_scraped,
            "new_jobs":        self.new_jobs,
            "qualified_jobs":  self.qualified_jobs,
            "scoring_cost_usd": round(self.scoring_cost, 4),
            "email_sent":      self.email_sent,
            "whatsapp_sent":   self.whatsapp_sent,
            "elapsed_ms":      self.elapsed_ms(),
            "portals":         self.portals.summary(),
        }
        logger.info("Run summary: %s", json.dumps(summary, ensure_ascii=False))


# ──────────────────────────────────────────────────────
#  Setup helper
# ──────────────────────────────────────────────────────
def setup_logging(json_mode: bool = False, level: int = logging.INFO) -> None:
    """Configure root logger. Call once at startup."""
    root = logging.getLogger()
    root.setLevel(level)
    for h in root.handlers[:]:
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    if json_mode:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    root.addHandler(handler)
