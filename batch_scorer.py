"""
Batch API scoring with:
- Async HTTP via aiohttp
- Exponential-backoff polling (don't hammer the status endpoint)
- Partial result handling (failed individual items don't abort the batch)
- Automatic fallback to individual scoring with 3-attempt retry per job
"""

import asyncio
import json
import logging
import random

import aiohttp

from config import CONFIG, SYSTEM_PROMPT_CACHED

log = logging.getLogger(__name__)

_BATCH_URL  = "https://api.anthropic.com/v1/messages/batches"
_SINGLE_URL = "https://api.anthropic.com/v1/messages"
_MODEL      = "claude-haiku-4-5-20251001"


# ──────────────────────────────────────────────────────
#  Internal helpers
# ──────────────────────────────────────────────────────
def _make_hdrs(api_key: str, *, batch: bool = True) -> dict:
    beta = (
        "message-batches-2024-09-24,prompt-caching-2024-07-31"
        if batch
        else "prompt-caching-2024-07-31"
    )
    return {
        "x-api-key":          api_key,
        "anthropic-version":  "2023-06-01",
        "anthropic-beta":     beta,
        "content-type":       "application/json",
    }


def _job_prompt(job: dict) -> str:
    return (
        f"Pozice: {job.get('title', '')}\n"
        f"Společnost: {job.get('company', '')}\n"
        f"Popis: {job.get('description', '') or '(bez popisu)'}"
    )


def _clamp_score(raw) -> int:
    try:
        return max(1, min(10, int(raw)))
    except (TypeError, ValueError):
        return 5


async def _post_json(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict,
    payload: dict,
    timeout_key: str = "batch_submit",
) -> dict:
    """POST and return parsed JSON; raises on HTTP error."""
    total = CONFIG["timeouts"].get(timeout_key, 30)
    async with session.post(
        url, headers=headers, json=payload,
        timeout=aiohttp.ClientTimeout(total=total),
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict,
    timeout_key: str = "batch_poll",
) -> dict:
    total = CONFIG["timeouts"].get(timeout_key, 15)
    async with session.get(
        url, headers=headers,
        timeout=aiohttp.ClientTimeout(total=total),
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


def _parse_result_line(line: str) -> tuple[str | None, dict | None]:
    """Parse one JSONL line from batch results. Returns (custom_id, result_dict)."""
    try:
        obj  = json.loads(line)
        cid  = obj.get("custom_id")
        robj = obj.get("result", {})
        if robj.get("type") == "succeeded":
            raw  = robj["message"]["content"][0]["text"].strip()
            raw  = raw.replace("```json", "").replace("```", "").strip()
            data = json.loads(raw)
            return cid, {"score": _clamp_score(data.get("score", 5)), "reason": data.get("reason", "")}
        # Non-success result
        log.warning("Batch item %s not succeeded: type=%s", cid, robj.get("type", "?"))
        return cid, {"score": 5, "reason": "Scoring nedostupný"}
    except json.JSONDecodeError as exc:
        log.warning("Could not parse batch result line: %s – %s", line[:80], exc)
        return None, None
    except Exception as exc:
        log.warning("Unexpected error parsing batch line: %s", exc)
        return None, None


# ──────────────────────────────────────────────────────
#  Public API
# ──────────────────────────────────────────────────────
async def score_jobs_batch_async(jobs: list[dict]) -> list[dict]:
    """
    Score all *jobs* via the Anthropic Batch API.
    Falls back to individual scoring on any failure.
    """
    if not jobs:
        return jobs

    api_key = CONFIG["anthropic_api_key"]
    if not api_key:
        log.warning("No API key – assigning default score 5 to all jobs")
        for job in jobs:
            job.setdefault("score", 5)
            job.setdefault("score_reason", "Scoring nedostupný (chybí API klíč)")
        return jobs

    hdrs = _make_hdrs(api_key, batch=True)
    batch_requests = [
        {
            "custom_id": str(i),
            "params": {
                "model":      _MODEL,
                "max_tokens": 120,
                "system":     SYSTEM_PROMPT_CACHED,
                "messages":   [{"role": "user", "content": _job_prompt(j)}],
            },
        }
        for i, j in enumerate(jobs)
    ]

    async with aiohttp.ClientSession() as session:
        # ── 1. Submit batch ──────────────────────────────────
        try:
            resp     = await _post_json(session, _BATCH_URL, hdrs, {"requests": batch_requests})
            batch_id = resp["id"]
            log.info("Batch submitted: %s (%d jobs)", batch_id, len(jobs))
        except Exception as exc:
            log.error("Batch submit failed: %s – falling back to individual scoring", exc)
            return await _fallback_scoring_async(jobs, api_key, session)

        # ── 2. Poll with exponential backoff ─────────────────
        status_url = f"{_BATCH_URL}/{batch_id}"
        wait       = CONFIG.get("batch_poll_initial", 10)
        elapsed    = 0
        max_wait   = CONFIG.get("batch_poll_max", 300)

        while elapsed < max_wait:
            await asyncio.sleep(wait)
            elapsed += wait
            try:
                status = await _get_json(session, status_url, hdrs)
                if status.get("processing_status") == "ended":
                    log.info("Batch %s complete after %ds", batch_id, elapsed)
                    break
                log.debug("Batch %s still processing (%ds elapsed)", batch_id, elapsed)
            except Exception as exc:
                log.warning("Batch poll error: %s", exc)
            # Grow wait geometrically, cap at 60 s
            wait = min(wait * 1.5, 60)
        else:
            log.error("Batch %s timed out after %ds – falling back", batch_id, max_wait)
            return await _fallback_scoring_async(jobs, api_key, session)

        # ── 3. Fetch results ─────────────────────────────────
        results_url = f"{_BATCH_URL}/{batch_id}/results"
        try:
            total = CONFIG["timeouts"]["batch_results"]
            async with session.get(
                results_url, headers=hdrs,
                timeout=aiohttp.ClientTimeout(total=total),
            ) as resp:
                resp.raise_for_status()
                results_text = await resp.text()
        except Exception as exc:
            log.error("Failed to fetch batch results: %s – falling back", exc)
            return await _fallback_scoring_async(jobs, api_key, session)

        # ── 4. Parse results (partial tolerance) ─────────────
        result_map: dict[str, dict] = {}
        for line in results_text.strip().splitlines():
            if not line.strip():
                continue
            cid, result = _parse_result_line(line)
            if cid is not None and result is not None:
                result_map[cid] = result

        # ── 5. Assign scores; collect missing ────────────────
        missing_indices: list[int] = []
        for i, job in enumerate(jobs):
            res = result_map.get(str(i))
            if res:
                job["score"]        = res["score"]
                job["score_reason"] = res["reason"]
                log.info("  ★ %d/10 – %s: %s", job["score"], job.get("title", "?"), job.get("score_reason", ""))
            else:
                log.warning("No batch result for job %d ('%s') – will retry individually", i, job.get("title", "?"))
                missing_indices.append(i)

        if missing_indices:
            missing = [jobs[i] for i in missing_indices]
            log.info("Retrying %d jobs individually", len(missing))
            await _fallback_scoring_async(missing, api_key, session)

        log.info("Scoring complete. Estimated cost: ~$%.4f", len(jobs) * 0.0002)
        return jobs


async def _fallback_scoring_async(
    jobs: list[dict],
    api_key: str,
    session: aiohttp.ClientSession,
) -> list[dict]:
    """Score jobs one-by-one with up to 3 retries each."""
    hdrs    = _make_hdrs(api_key, batch=False)
    retry   = CONFIG["retry"]
    total_t = CONFIG["timeouts"].get("batch_submit", 30)

    for job in jobs:
        scored = False
        for attempt in range(1, retry["max_attempts"] + 1):
            try:
                payload = {
                    "model":      _MODEL,
                    "max_tokens": 120,
                    "system":     SYSTEM_PROMPT_CACHED,
                    "messages":   [{"role": "user", "content": _job_prompt(job)}],
                }
                async with session.post(
                    _SINGLE_URL, headers=hdrs, json=payload,
                    timeout=aiohttp.ClientTimeout(total=total_t),
                ) as resp:
                    resp.raise_for_status()
                    data_raw = await resp.json()

                raw  = data_raw["content"][0]["text"].strip()
                raw  = raw.replace("```json", "").replace("```", "").strip()
                data = json.loads(raw)

                job["score"]        = _clamp_score(data.get("score", 5))
                job["score_reason"] = data.get("reason", "")
                scored = True
                break

            except Exception as exc:
                if attempt == retry["max_attempts"]:
                    log.warning("Fallback failed for '%s': %s", job.get("title", "?"), exc)
                else:
                    delay = min(retry["base_delay"] * (2 ** (attempt - 1)), retry["max_delay"])
                    delay += random.uniform(0, delay * retry["jitter"])
                    await asyncio.sleep(delay)

        if not scored:
            job.setdefault("score", 5)
            job.setdefault("score_reason", "Scoring nedostupný")

        log.info(
            "  ★ %d/10 – %s: %s",
            job.get("score", 5), job.get("title", "?"), job.get("score_reason", ""),
        )
        await asyncio.sleep(0.3)

    return jobs
