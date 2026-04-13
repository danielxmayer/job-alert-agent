"""
Job Alert Agent – async refactor
- Portály: jobs.cz, práce.cz, kariera.cz, dobraprace.cz, profesia.cz, startupjobs.cz
- Filtry: marketing | Praha + Středočeský kraj | plný úvazek
- Spuštění: každý den v 10:00 CET (GitHub Actions schedule)
- Notifikace: Email + WhatsApp (with retry)
- Scoring: Claude Haiku API (Batch + prompt caching) – fit 1–10 dle CV
- Async: concurrent portal scraping via asyncio + aiohttp
- Reliability: exponential backoff, circuit breaker, SQLite deduplication
"""

import asyncio
import hashlib
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import aiohttp
from bs4 import BeautifulSoup
from urllib.parse import quote as urlquote

# Local modules
from config import CONFIG, PORTALS, HEADERS, validate_config
from database import init_db, load_seen, add_seen_job, log_scrape, log_audit
from http_client import fetch_page_async, circuit_breaker
from parsers import PARSERS
from batch_scorer import score_jobs_batch_async
from notifications import send_email_async, send_whatsapp_async
from logger import setup_logging, RunMetrics

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────
def job_id(title: str, company: str, portal: str) -> str:
    """
    Stable collision-resistant identifier for job deduplication.
    MD5 is intentionally used here for its speed; this is NOT a cryptographic operation.
    """
    key = f"{title.lower().strip()}{company.lower().strip()}{portal}"
    return hashlib.md5(key.encode()).hexdigest()


def is_relevant(job: dict) -> bool:
    """Return True if the job matches configured keywords and locations."""
    try:
        tl = (job.get("title") or "").lower()
        ll = ((job.get("location") or "") + " " + (job.get("link") or "")).lower()
        kw_match  = any(kw.lower() in tl for kw in CONFIG["keywords"])
        loc_match = (not job.get("location")) or any(loc.lower() in ll for loc in CONFIG["locations"])
        return kw_match and loc_match
    except Exception as exc:
        log.warning("is_relevant check failed for job '%s': %s", job.get("title", "?"), exc)
        return False


# ──────────────────────────────────────────────────────
#  Scraping
# ──────────────────────────────────────────────────────
async def _scrape_one(
    session: aiohttp.ClientSession,
    portal_name: str,
    pcfg: dict,
    keyword: str,
    location: str,
    metrics: RunMetrics,
    run_id: str,
    db_file: str,
) -> list[dict]:
    """Fetch and parse one portal/keyword/location combination."""
    url = pcfg["url"].format(
        keyword=urlquote(keyword),
        location=urlquote(location),
    )
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    log.info("[%s] Scraping – %s / %s", portal_name, keyword, location)

    html = await fetch_page_async(session, url, portal_name, HEADERS)
    duration_ms = int((time.monotonic() - t0) * 1000)

    if html is None:
        metrics.portals.record(portal_name, error=True, duration_ms=duration_ms)
        log_scrape(db_file, run_id, portal_name, keyword, location, 0, 1,
                   "fetch_failed", started_at, duration_ms)
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")
        parser_fn = PARSERS[pcfg["parser"]]
        jobs = parser_fn(soup, portal_name, url)
        metrics.portals.record(portal_name, jobs_found=len(jobs), duration_ms=duration_ms)
        log_scrape(db_file, run_id, portal_name, keyword, location, len(jobs), 0,
                   None, started_at, duration_ms)
        log.debug("[%s] Found %d jobs", portal_name, len(jobs))
        return jobs
    except Exception as exc:
        log.error("[%s] Parser error: %s", portal_name, exc, exc_info=True)
        metrics.portals.record(portal_name, error=True, duration_ms=duration_ms)
        log_scrape(db_file, run_id, portal_name, keyword, location, 0, 1,
                   str(exc), started_at, duration_ms)
        return []


async def scrape_all_async(metrics: RunMetrics, run_id: str, db_file: str) -> list[dict]:
    """
    Scrape all portals concurrently (bounded by max_concurrent semaphore).
    Applies a per-request rate-limit delay and per-portal circuit breaking.
    """
    sem = asyncio.Semaphore(CONFIG["max_concurrent"])
    rate_delay = CONFIG["rate_limit_delay"]
    all_jobs: list[dict] = []

    async with aiohttp.ClientSession() as session:

        async def bounded(portal_name, pcfg, kw, loc):
            async with sem:
                if circuit_breaker.is_open(portal_name):
                    metrics.portals.record(portal_name, skipped=True)
                    return []
                result = await _scrape_one(
                    session, portal_name, pcfg, kw, loc, metrics, run_id, db_file,
                )
                await asyncio.sleep(rate_delay)
                return result

        tasks = [
            bounded(portal_name, pcfg, kw, loc)
            for portal_name, pcfg in PORTALS.items()
            for kw in CONFIG["keywords"][:2]
            for loc in CONFIG["locations"]
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, list):
            all_jobs.extend(r)
        elif isinstance(r, Exception):
            log.error("Scrape task raised unexpected exception: %s", r)

    relevant = [j for j in all_jobs if is_relevant(j)]
    metrics.total_scraped = len(all_jobs)
    log.info("Relevant: %d / %d scraped", len(relevant), len(all_jobs))
    return relevant


# ──────────────────────────────────────────────────────
#  Main async entry point
# ──────────────────────────────────────────────────────
async def run_check_async() -> None:
    run_id  = str(uuid.uuid4())[:8]
    metrics = RunMetrics()
    db_file = CONFIG["db_file"]

    log.info("=" * 55)
    log.info("Job Alert Agent starting [run_id=%s]", run_id)

    # Initialise SQLite
    init_db(db_file)
    seen = load_seen(db_file)
    log.info("Seen jobs in DB: %d", len(seen))

    # Scrape portals concurrently
    all_jobs = await scrape_all_async(metrics, run_id, db_file)

    # Deduplicate
    new_jobs: list[dict] = []
    new_ids:  list[str]  = []
    for j in all_jobs:
        jid = job_id(j["title"], j["company"], j["portal"])
        if jid not in seen:
            new_jobs.append(j)
            new_ids.append(jid)
            seen.add(jid)

    metrics.new_jobs = len(new_jobs)
    log.info("New jobs: %d", len(new_jobs))

    if new_jobs:
        # Score via Batch API
        new_jobs = await score_jobs_batch_async(new_jobs)
        metrics.scoring_cost = len(new_jobs) * 0.0002

        # Persist to DB + audit trail
        for jid, j in zip(new_ids, new_jobs):
            add_seen_job(db_file, jid, j)
            log_audit(db_file, run_id, jid, j, "scored")

        # Filter by minimum score
        qualified = [j for j in new_jobs if j.get("score", 0) >= CONFIG["min_score"]]
        metrics.qualified_jobs = len(qualified)
        log.info("Qualified (score ≥%d): %d", CONFIG["min_score"], len(qualified))

        if qualified:
            metrics.email_sent     = await send_email_async(qualified)
            metrics.whatsapp_sent  = await send_whatsapp_async(qualified)
            for j in qualified:
                jid = job_id(j["title"], j["company"], j["portal"])
                log_audit(db_file, run_id, jid, j, "notified")
        else:
            log.info("No jobs met minimum score threshold")
    else:
        log.info("No new jobs found")

    # Log circuit breaker status
    cb_status = circuit_breaker.status()
    if any(v["open"] for v in cb_status.values()):
        log.warning("Circuit breaker status: %s", cb_status)

    metrics.log_summary(log)
    log.info("Done.")


def run_check() -> None:
    """Synchronous wrapper – called by the scheduler and __main__."""
    asyncio.run(run_check_async())


# ──────────────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────────────
if __name__ == "__main__":
    json_logs = os.getenv("LOG_JSON", "").lower() in ("1", "true", "yes")
    setup_logging(json_mode=json_logs)
    log = logging.getLogger(__name__)

    validate_config()

    log.info("=" * 55)
    log.info("Job Alert Agent started")
    log.info("Min score: %d/10 | Model: claude-haiku-4-5", CONFIG["min_score"])
    log.info("=" * 55)

    run_check()



