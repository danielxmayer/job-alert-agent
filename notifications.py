"""
Email and WhatsApp notifications with retry logic (exponential backoff).
"""

import asyncio
import logging
import random
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiohttp

from config import CONFIG

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────
#  Scoring helpers (shared with email template)
# ──────────────────────────────────────────────────────
def score_emoji(s: int) -> str:
    return "🟢" if s >= 9 else "🟡" if s >= 7 else "🟠" if s >= 5 else "🔴"


def score_color(s: int) -> str:
    return "#1D9E75" if s >= 8 else "#BA7517" if s >= 5 else "#A32D2D"


def score_label(s: int) -> str:
    return "Výborný fit" if s >= 9 else "Dobrý fit" if s >= 7 else "Částečný fit" if s >= 5 else "Slabý fit"


# ──────────────────────────────────────────────────────
#  Email
# ──────────────────────────────────────────────────────
def _build_email_html(jobs: list[dict]) -> tuple[str, str]:
    """Return (html_body, subject) for the job-alert email."""
    count = len(jobs)
    avg   = round(sum(j.get("score", 5) for j in jobs) / count, 1)
    good  = len([j for j in jobs if j.get("score", 0) >= 7])

    rows = ""
    for j in sorted(jobs, key=lambda x: x.get("score", 0), reverse=True):
        s   = j.get("score", 5)
        col = score_color(s)
        lbl = score_label(s)
        loc = f' · <span style="color:#888">{j["location"]}</span>' if j.get("location") else ""
        rsn = (
            f'<div style="font-size:12px;color:#999;margin-top:4px;font-style:italic">'
            f'{j["score_reason"]}</div>'
            if j.get("score_reason") else ""
        )
        rows += (
            f'<tr><td style="padding:14px 12px;border-bottom:1px solid #f0f0f0;vertical-align:top">'
            f'<div style="margin-bottom:6px">'
            f'<span style="background:{col};color:white;font-weight:700;font-size:14px;border-radius:6px;padding:3px 10px">{s}/10</span>'
            f'<span style="font-size:11px;color:{col};margin-left:8px;font-weight:600">{lbl}</span>'
            f'</div>'
            f'<a href="{j["link"]}" style="font-size:15px;font-weight:700;color:#1a1a1a;text-decoration:none;display:block;margin-bottom:3px">{j["title"]}</a>'
            f'<span style="font-size:13px;color:#555">{j.get("company","")}</span>{loc}'
            f'<span style="font-size:11px;background:#f5f5f5;color:#777;border-radius:4px;padding:2px 7px;margin-left:6px">{j["portal"]}</span>'
            f'{rsn}</td></tr>'
        )

    html = (
        f'<div style="font-family:Arial,sans-serif;max-width:640px;margin:auto">'
        f'<div style="background:#1D9E75;padding:22px 28px;border-radius:10px 10px 0 0">'
        f'<h1 style="color:white;margin:0;font-size:22px">🔔 Job Alert</h1>'
        f'<p style="color:rgba(255,255,255,.85);margin:6px 0 0;font-size:13px">'
        f'{datetime.now().strftime("%d.%m.%Y %H:%M")} · marketing · Praha + SČK · plný úvazek</p>'
        f'</div>'
        f'<div style="background:#f8f8f6;border:1px solid #e5e5e5;border-top:none;padding:16px 28px">'
        f'<table style="width:100%;text-align:center"><tr>'
        f'<td><div style="font-size:28px;font-weight:700">{count}</div>'
        f'<div style="font-size:12px;color:#888">nových inzerátů</div></td>'
        f'<td><div style="font-size:28px;font-weight:700;color:#1D9E75">{avg}</div>'
        f'<div style="font-size:12px;color:#888">průměrný fit /10</div></td>'
        f'<td><div style="font-size:28px;font-weight:700">{good}</div>'
        f'<div style="font-size:12px;color:#888">dobrý fit (7+)</div></td>'
        f'</tr></table></div>'
        f'<div style="background:white;border:1px solid #e5e5e5;border-top:none;border-radius:0 0 10px 10px">'
        f'<table style="width:100%;border-collapse:collapse">{rows}</table>'
        f'</div>'
        f'<p style="font-size:11px;color:#bbb;text-align:center;margin-top:14px">'
        f'Scoring: Claude Haiku AI · Batch API + prompt caching</p>'
        f'</div>'
    )
    subject = (
        f"[Job Alert] {count} nových · fit {avg}/10 · {datetime.now().strftime('%d.%m.%Y')}"
    )
    return html, subject


def _smtp_send(cfg: dict, msg_str: str) -> None:
    """Blocking SMTP call (run via asyncio.to_thread)."""
    with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as srv:
        srv.starttls()
        srv.login(cfg["sender"], cfg["password"])
        srv.sendmail(cfg["sender"], cfg["recipient"], msg_str)


async def send_email_async(jobs: list[dict]) -> bool:
    """Send the HTML email digest. Returns True on success."""
    cfg = CONFIG["email"]
    if not all([cfg["sender"], cfg["password"], cfg["recipient"]]):
        log.warning("Email not configured – skipping")
        return False

    html, subject = _build_email_html(jobs)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["sender"]
    msg["To"]      = cfg["recipient"]
    msg.attach(MIMEText(html, "html", "utf-8"))
    msg_str = msg.as_string()

    retry = CONFIG["retry"]
    for attempt in range(1, retry["max_attempts"] + 1):
        try:
            await asyncio.to_thread(_smtp_send, cfg, msg_str)
            log.info("Email sent → %s", cfg["recipient"])
            return True
        except Exception as exc:
            if attempt == retry["max_attempts"]:
                log.error("Email failed after %d attempts: %s", attempt, exc)
                return False
            delay = min(retry["base_delay"] * (2 ** (attempt - 1)), retry["max_delay"])
            delay += random.uniform(0, delay * retry["jitter"])
            log.warning("Email attempt %d failed: %s – retrying in %.1fs", attempt, exc, delay)
            await asyncio.sleep(delay)

    return False


# ──────────────────────────────────────────────────────
#  WhatsApp
# ──────────────────────────────────────────────────────
async def send_whatsapp_async(jobs: list[dict]) -> bool:
    """Send a WhatsApp summary via the Meta Cloud API. Returns True on success."""
    cfg = CONFIG["whatsapp"]
    if not all([cfg["token"], cfg["phone_id"], cfg["recipient"]]):
        log.warning("WhatsApp not configured – skipping")
        return False

    count = len(jobs)
    avg   = round(sum(j.get("score", 5) for j in jobs) / count, 1)
    top3  = sorted(jobs, key=lambda j: j.get("score", 0), reverse=True)[:3]
    lines = "".join(
        f"\n{score_emoji(j.get('score', 5))} *{j['title']}* ({j.get('score', 5)}/10)\n"
        f"   {j.get('company', '')} · {j['portal']}\n"
        for j in top3
    )
    msg = (
        f"🔔 *Job Alert – {datetime.now().strftime('%d.%m. %H:%M')}*\n\n"
        f"Nalezeno *{count} nových* inzerátů\n📍 Praha + SČK · plný úvazek\n"
        f"⭐ Průměrný fit: *{avg}/10*\n\n*Top 3 pozice:*{lines}\n"
        f"👉 {top3[0]['link'] if top3 else 'https://www.jobs.cz'}"
    )
    url     = f"https://graph.facebook.com/v19.0/{cfg['phone_id']}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to":                cfg["recipient"],
        "type":              "text",
        "text":              {"body": msg},
    }
    headers = {
        "Authorization": f"Bearer {cfg['token']}",
        "Content-Type":  "application/json",
    }

    retry = CONFIG["retry"]
    for attempt in range(1, retry["max_attempts"] + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, headers=headers, json=payload,
                    timeout=aiohttp.ClientTimeout(total=CONFIG["timeouts"]["notification"]),
                ) as resp:
                    resp.raise_for_status()
            log.info("WhatsApp sent → %s", cfg["recipient"])
            return True
        except Exception as exc:
            if attempt == retry["max_attempts"]:
                log.error("WhatsApp failed after %d attempts: %s", attempt, exc)
                return False
            delay = min(retry["base_delay"] * (2 ** (attempt - 1)), retry["max_delay"])
            delay += random.uniform(0, delay * retry["jitter"])
            log.warning("WhatsApp attempt %d failed: %s – retrying in %.1fs", attempt, exc, delay)
            await asyncio.sleep(delay)

    return False
