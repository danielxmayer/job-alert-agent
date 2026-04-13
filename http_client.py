"""
Async HTTP client with:
- Exponential backoff + jitter on transient failures
- Per-portal circuit breaker (opens after N consecutive failures)
- Configurable timeouts
"""

import asyncio
import logging
import random
from collections import defaultdict

import aiohttp

from config import CONFIG

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────
#  Circuit breaker
# ──────────────────────────────────────────────────────
class CircuitBreaker:
    """
    Tracks consecutive failures per portal.
    Once the threshold is reached the circuit opens and all further
    requests for that portal are skipped until the agent restarts.
    """

    def __init__(self, threshold: int = 3) -> None:
        self.threshold = threshold
        self._failures: dict[str, int] = defaultdict(int)
        self._open: dict[str, bool] = defaultdict(bool)

    def record_success(self, portal: str) -> None:
        self._failures[portal] = 0
        self._open[portal] = False

    def record_failure(self, portal: str) -> None:
        self._failures[portal] += 1
        if self._failures[portal] >= self.threshold and not self._open[portal]:
            log.warning(
                "Circuit breaker OPEN for %s after %d consecutive failures",
                portal, self._failures[portal],
            )
            self._open[portal] = True

    def is_open(self, portal: str) -> bool:
        return self._open.get(portal, False)

    def status(self) -> dict:
        return {
            p: {"failures": self._failures[p], "open": self._open[p]}
            for p in set(list(self._failures) + list(self._open))
        }


# Shared circuit breaker instance used by all scraping code
circuit_breaker = CircuitBreaker(threshold=CONFIG["circuit_breaker_threshold"])


# ──────────────────────────────────────────────────────
#  Page fetcher
# ──────────────────────────────────────────────────────
async def fetch_page_async(
    session: aiohttp.ClientSession,
    url: str,
    portal: str,
    headers: dict,
    *,
    max_attempts: int | None = None,
    base_delay: float | None = None,
    max_delay: float | None = None,
    jitter: float | None = None,
) -> str | None:
    """
    Fetch *url* and return the response body as a string.
    Returns None on permanent failure (circuit open or all retries exhausted).
    """
    if circuit_breaker.is_open(portal):
        log.warning("[%s] Circuit open – skipping %s", portal, url)
        return None

    retry_cfg = CONFIG["retry"]
    max_attempts = max_attempts if max_attempts is not None else retry_cfg["max_attempts"]
    base_delay   = base_delay   if base_delay   is not None else retry_cfg["base_delay"]
    max_delay    = max_delay    if max_delay    is not None else retry_cfg["max_delay"]
    jitter       = jitter       if jitter       is not None else retry_cfg["jitter"]

    timeout = aiohttp.ClientTimeout(
        connect=CONFIG["timeouts"]["connect"],
        sock_read=CONFIG["timeouts"]["read"],
    )

    for attempt in range(1, max_attempts + 1):
        try:
            async with session.get(url, headers=headers, timeout=timeout) as resp:
                resp.raise_for_status()
                text = await resp.text()
                circuit_breaker.record_success(portal)
                log.debug("[%s] Fetched %s (attempt %d)", portal, url, attempt)
                return text

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt == max_attempts:
                log.warning(
                    "[%s] Failed after %d attempts: %s → %s",
                    portal, max_attempts, url, exc,
                )
                circuit_breaker.record_failure(portal)
                return None

            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            delay += random.uniform(0, delay * jitter)
            log.debug(
                "[%s] Attempt %d failed (%s), retrying in %.1fs",
                portal, attempt, exc, delay,
            )
            await asyncio.sleep(delay)
