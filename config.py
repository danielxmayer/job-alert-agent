"""
Centralized configuration with validation.
Includes portal definitions, CV profile, and system prompt.
"""

import os
import logging
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────
#  Main configuration
# ──────────────────────────────────────────────────────
CONFIG = {
    "keywords":  ["marketing", "online marketing", "marketingový specialista"],
    "locations": ["Praha", "Středočeský kraj"],
    "min_score": 6,
    "schedule_time_cet": "10:00",
    "db_file": "job_alert.db",
    # Retry policy (applies to HTTP requests and notifications)
    "retry": {
        "max_attempts": 3,
        "base_delay":   1.0,   # seconds
        "max_delay":    30.0,  # seconds
        "jitter":       0.5,   # fraction of delay added as random noise
    },
    # Per-operation HTTP timeouts (seconds)
    "timeouts": {
        "connect":        10,
        "read":           20,
        "batch_submit":   30,
        "batch_poll":     15,
        "batch_results":  30,
        "notification":   10,
    },
    # Concurrency / rate limiting
    "rate_limit_delay":  1.5,   # seconds between requests from the same portal
    "max_concurrent":    3,     # max simultaneous HTTP requests across all portals
    # Batch API polling
    "batch_poll_initial": 10,   # seconds before first status check
    "batch_poll_max":     300,  # max total wait before falling back (seconds)
    # Circuit breaker – open after this many consecutive failures per portal
    "circuit_breaker_threshold": 3,
    # Email
    "email": {
        "sender":    os.getenv("EMAIL_SENDER", ""),
        "password":  os.getenv("EMAIL_PASSWORD", ""),
        "recipient": os.getenv("EMAIL_RECIPIENT", ""),
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
    },
    # WhatsApp (Cloud API)
    "whatsapp": {
        "token":     os.getenv("WHATSAPP_TOKEN", ""),
        "phone_id":  os.getenv("WHATSAPP_PHONE_ID", ""),
        "recipient": os.getenv("WHATSAPP_RECIPIENT", ""),
    },
    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
}

# ──────────────────────────────────────────────────────
#  URL templates for job portals
# ──────────────────────────────────────────────────────
PORTALS = {
    "jobs.cz": {
        "url":    "https://www.jobs.cz/prace/?q%5B%5D={keyword}&locality%5Blabel%5D={location}&employment_type%5B%5D=plny_uvazek",
        "parser": "parse_jobs_cz",
    },
    "prace.cz": {
        "url":    "https://www.prace.cz/nabidky/hlavni-mesto-praha/praha/?keywords%5B%5D={keyword}",
        "parser": "parse_prace_cz",
    },
    "kariera.cz": {
        "url":    "https://www.kariera.cz/nabidky-prace/?query={keyword}&city={location}",
        "parser": "parse_kariera_cz",
    },
    "dobraprace.cz": {
        "url":    "https://www.dobraprace.cz/nabidka-prace/?search=1&text_what={keyword}&region_arr%5B0%5D=PHA",
        "parser": "parse_generic",
    },
    "profesia.cz": {
        "url":    "https://www.profesia.cz/prace/{location}/?search_anywhere={keyword}&count_days=1",
        "parser": "parse_profesia_cz",
    },
    "startupjobs.cz": {
        "url":    "https://www.startupjobs.cz/nabidky?q={keyword}&l=Praha",
        "parser": "parse_generic",
    },
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
    )
}

# ──────────────────────────────────────────────────────
#  Candidate CV – basis for AI scoring
# ──────────────────────────────────────────────────────
CANDIDATE_CV = """
Jméno: Daniel Mayer
Profil: Marketingový a mediální manažer, 10+ let praxe

ZKUŠENOSTI:
- Head of Marketing Investments, TV Nova (2021–2026, Praha)
  Řízení ročního marketingového rozpočtu, mediální strategie (TV, digital, OOH),
  finanční reporting, procurement, vedení týmu pro mediální partnerství.
- Media Manager, Zonky (2017–2021, Praha)
  Mediaplány, analýza cílových skupin, TV/tisk/rozhlas/online,
  optimalizace kampaní, správa ATL rozpočtu, obchodní smlouvy.
- Media Manager, Mafra (2014–2017, Praha)
  Mediaplány pro značky skupiny MAFRA, vyhodnocování kampaní.
- Marketing Specialist, Ekospol (2013, Praha)
- Media Planner, Médea (2011–2013, Praha)

VÝSLEDKY: Voyo 100k→950k předplatitelů, spuštění Oneplay, rebranding Nova
DOVEDNOSTI: Mediální strategie, plánování médií, řízení rozpočtů, procurement, online marketing
JAZYKY: Čeština (rodilý), Angličtina (pokročilý), Němčina (pokročilý)
VZDĚLÁNÍ: Ing. Marketingové řízení, ČZU Praha
"""

# System prompt with CV – cache_control enables 90 % token savings across calls
SYSTEM_PROMPT_CACHED = [
    {
        "type": "text",
        "text": (
            "Jsi HR expert. Hodnotíš shodu pracovních inzerátů s profilem kandidáta.\n\n"
            f"PROFIL KANDIDÁTA:\n{CANDIDATE_CV}\n\n"
            "Pro každý inzerát vrať POUZE JSON (bez markdown, bez komentářů):\n"
            '{"score": <1-10>, "reason": "<max 1 věta česky>"}\n\n'
            "Škála:\n"
            "1–3 = špatný fit | 4–6 = částečný fit | 7–8 = dobrý fit | 9–10 = výborný fit"
        ),
        "cache_control": {"type": "ephemeral"},
    }
]


# ──────────────────────────────────────────────────────
#  Startup validation
# ──────────────────────────────────────────────────────
def validate_config() -> list[str]:
    """Check required environment variables and log warnings. Returns list of warnings."""
    warnings: list[str] = []

    if not CONFIG["anthropic_api_key"]:
        warnings.append("ANTHROPIC_API_KEY not set – scoring will use default score of 5")

    email = CONFIG["email"]
    if not all([email["sender"], email["password"], email["recipient"]]):
        warnings.append("Email credentials incomplete – email notifications disabled")

    wa = CONFIG["whatsapp"]
    if not all([wa["token"], wa["phone_id"], wa["recipient"]]):
        warnings.append("WhatsApp credentials incomplete – WhatsApp notifications disabled")

    for w in warnings:
        log.warning("Config: %s", w)

    return warnings
