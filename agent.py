"""
Job Alert Agent
- Portály: jobs.cz, práce.cz, kariera.cz, dobraprace.cz, profesia.cz, indeed.com, monster.cz
- Filtry: marketing | Praha + Středočeský kraj | plný úvazek
- Spuštění: každý den v 10:00 CET
- Notifikace: Email + WhatsApp
- Scoring: Claude Haiku API (Batch + prompt caching) – fit 1–10 dle CV
"""

import os
import json
import time
import hashlib
import smtplib
import logging
import schedule
import requests
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────────────
CONFIG = {
    "keywords":  ["marketing", "online marketing", "marketingový specialista"],
    "locations": ["Praha", "Středočeský kraj"],
    "min_score": 6,
    "schedule_time_cet": "10:00",
    "seen_file": "seen_jobs.json",
    "email": {
        "sender":    os.getenv("EMAIL_SENDER", ""),
        "password":  os.getenv("EMAIL_PASSWORD", ""),
        "recipient": os.getenv("EMAIL_RECIPIENT", ""),
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
    },
    "whatsapp": {
        "token":     os.getenv("WHATSAPP_TOKEN", ""),
        "phone_id":  os.getenv("WHATSAPP_PHONE_ID", ""),
        "recipient": os.getenv("WHATSAPP_RECIPIENT", ""),
    },
    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
}

# ──────────────────────────────────────────────────────
#  CV kandidáta – základ pro AI scoring
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

# System prompt s CV – cache_control zajistí cachování mezi voláními (90% úspora)
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
#  URL šablony portálů
# ──────────────────────────────────────────────────────
PORTALS = {
    "jobs.cz":       {"url": "https://www.jobs.cz/prace/?q%5B%5D={keyword}&locality%5Blabel%5D={location}&employment_type%5B%5D=plny_uvazek", "parser": "parse_jobs_cz"},
    "prace.cz":      {"url": "https://www.prace.cz/nabidky/hlavni-mesto-praha/praha/?keywords%5B%5D={keyword}", "parser": "parse_prace_cz"},
    "kariera.cz":    {"url": "https://www.kariera.cz/nabidky-prace/?query={keyword}&city={location}", "parser": "parse_kariera_cz"},
    "dobraprace.cz": {"url": "https://www.dobraprace.cz/nabidka-prace/?search=1&text_what={keyword}&region_arr%5B0%5D=PHA", "parser": "parse_generic"},
    "profesia.cz":   {"url": "https://www.profesia.cz/prace/{location}/?search_anywhere={keyword}&count_days=1", "parser": "parse_profesia_cz"},
    "startupjobs.cz":{"url": "https://www.startupjobs.cz/nabidky?q={keyword}&l=Praha", "parser": "parse_generic"},
}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────
#  Parsery
# ──────────────────────────────────────────────────────
def _card(card, title_sel, company_sel, location_sel, desc_sel, link_attr, portal, url, prefix=""):
    t = card.select_one(title_sel)
    if not t: return None
    c = card.select_one(company_sel) if company_sel else None
    l = card.select_one(location_sel) if location_sel else None
    d = card.select_one(desc_sel) if desc_sel else None
    href = t.get(link_attr, url) if link_attr else t.get("href", url)
    if href and href.startswith("/") and prefix: href = prefix + href
    return {"title": t.get_text(strip=True), "company": c.get_text(strip=True) if c else "",
            "location": l.get_text(strip=True) if l else "", "description": d.get_text(strip=True) if d else "",
            "link": href, "portal": portal}

def parse_jobs_cz(soup, portal, url):
    jobs = []
    for card in soup.select("article.SearchResultCard"):
        j = _card(card, "h2.SearchResultCard__title a", ".SearchResultCard__company",
                  ".SearchResultCard__info", ".SearchResultCard__description", "href", portal, url)
        if j: jobs.append(j)
    return jobs

def parse_prace_cz(soup, portal, url):
    jobs = []
    for card in soup.select(".offer-item, .job-offer"):
        j = _card(card, "h2 a, h3 a, .offer-title a", ".company-name, .employer",
                  ".location, .locality", ".offer-description", "href", portal, url)
        if j: jobs.append(j)
    return jobs

def parse_kariera_cz(soup, portal, url):
    jobs = []
    for card in soup.select(".job-listing__item, .offer-card"):
        j = _card(card, "a.job-listing__title, a.offer-title", ".job-listing__company, .offer-company",
                  ".job-listing__location, .offer-location", None, "href", portal, url)
        if j: jobs.append(j)
    return jobs

def parse_profesia_cz(soup, portal, url):
    jobs = []
    for card in soup.select("li.list-row, .offer-item"):
        j = _card(card, "h2 a, .title a", ".company, .employer-title",
                  ".job-location, .location", None, "href", portal, url, "https://www.profesia.cz")
        if j: jobs.append(j)
    return jobs

def parse_indeed(soup, portal, url):
    jobs = []
    for card in soup.select(".job_seen_beacon"):
        t = card.select_one("h2.jobTitle a, [data-testid='job-title'] a")
        if not t: continue
        c = card.select_one("[data-testid='company-name'], .companyName")
        l = card.select_one("[data-testid='text-location'], .companyLocation")
        d = card.select_one(".job-snippet")
        href = t.get("href", "")
        jobs.append({"title": t.get_text(strip=True), "company": c.get_text(strip=True) if c else "",
                     "location": l.get_text(strip=True) if l else "", "description": d.get_text(strip=True) if d else "",
                     "link": "https://cz.indeed.com" + href if href.startswith("/") else href, "portal": portal})
    return jobs

def parse_generic(soup, portal, url):
    jobs = []
    for a in soup.select("a[href]"):
        title = a.get_text(strip=True)
        if 10 <= len(title) <= 120 and any(kw.lower() in title.lower() for kw in CONFIG["keywords"]):
            jobs.append({"title": title, "company": "", "location": "", "description": "", "link": a["href"], "portal": portal})
    return jobs[:20]

PARSERS = {"parse_jobs_cz": parse_jobs_cz, "parse_prace_cz": parse_prace_cz,
           "parse_kariera_cz": parse_kariera_cz, "parse_profesia_cz": parse_profesia_cz,
           "parse_indeed": parse_indeed, "parse_generic": parse_generic}

# ──────────────────────────────────────────────────────
#  Scraping
# ──────────────────────────────────────────────────────
def fetch_page(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log.warning(f"Chyba: {url} → {e}")
        return None

def is_relevant(job):
    tl = job["title"].lower()
    ll = (job["location"] + " " + job.get("link","")).lower()
    return (any(kw.lower() in tl for kw in CONFIG["keywords"]) and
            (any(loc.lower() in ll for loc in CONFIG["locations"]) or not job["location"]))

def scrape_all():
    all_jobs = []
    for portal_name, pcfg in PORTALS.items():
        for kw in CONFIG["keywords"][:2]:
            for loc in CONFIG["locations"]:
                url = pcfg["url"].format(keyword=requests.utils.quote(kw), location=requests.utils.quote(loc))
                log.info(f"Scrapuji {portal_name} – {kw} / {loc}")
                soup = fetch_page(url)
                if soup:
                    all_jobs.extend(PARSERS[pcfg["parser"]](soup, portal_name, url))
                time.sleep(2)
    relevant = [j for j in all_jobs if is_relevant(j)]
    log.info(f"Relevantních inzerátů: {len(relevant)}")
    return relevant

# ──────────────────────────────────────────────────────
#  Pomocné funkce
# ──────────────────────────────────────────────────────
def job_id(title, company, portal):
    return hashlib.md5(f"{title.lower().strip()}{company.lower().strip()}{portal}".encode()).hexdigest()

def load_seen():
    if os.path.exists(CONFIG["seen_file"]):
        with open(CONFIG["seen_file"], "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    with open(CONFIG["seen_file"], "w", encoding="utf-8") as f:
        json.dump(list(seen), f)

def score_emoji(s): return "🟢" if s>=9 else "🟡" if s>=7 else "🟠" if s>=5 else "🔴"
def score_color(s): return "#1D9E75" if s>=8 else "#BA7517" if s>=5 else "#A32D2D"
def score_label(s): return "Výborný fit" if s>=9 else "Dobrý fit" if s>=7 else "Částečný fit" if s>=5 else "Slabý fit"

# ──────────────────────────────────────────────────────
#  CV Scoring – Batch API + Prompt Caching (max úspora)
#  Model: claude-haiku-4-5 (nejlevnější)
#  Batch API: 50% sleva na všech tokenech
#  Prompt cache: 90% sleva na CV kontextu
# ──────────────────────────────────────────────────────
def score_jobs_batch(jobs):
    if not jobs: return jobs
    api_key = CONFIG["anthropic_api_key"]
    hdrs = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "message-batches-2024-09-24,prompt-caching-2024-07-31",
        "content-type": "application/json",
    }
    # Sestav dávku
    batch_requests = [
        {"custom_id": str(i), "params": {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 120,
            "system": SYSTEM_PROMPT_CACHED,
            "messages": [{"role": "user", "content":
                f"Pozice: {j.get('title','')}\nSpolečnost: {j.get('company','')}\nPopis: {j.get('description','') or '(bez popisu)'}"}],
        }} for i, j in enumerate(jobs)
    ]
    # Odešli dávku
    try:
        r = requests.post("https://api.anthropic.com/v1/messages/batches", headers=hdrs,
                          json={"requests": batch_requests}, timeout=30)
        r.raise_for_status()
        batch_id = r.json()["id"]
        log.info(f"Batch odeslan: {batch_id} ({len(jobs)} inzerátů)")
    except Exception as e:
        log.error(f"Batch API selhal: {e} – používám fallback")
        return _fallback_scoring(jobs, api_key, hdrs)
    # Polling – čekej na dokončení
    for _ in range(60):
        time.sleep(5)
        try:
            s = requests.get(f"https://api.anthropic.com/v1/messages/batches/{batch_id}", headers=hdrs, timeout=15).json()
            if s.get("processing_status") == "ended": break
        except: pass
    else:
        return _fallback_scoring(jobs, api_key, hdrs)
    # Načti výsledky
    try:
        results_text = requests.get(f"https://api.anthropic.com/v1/messages/batches/{batch_id}/results",
                                    headers=hdrs, timeout=30).text
        result_map = {}
        for line in results_text.strip().splitlines():
            if not line.strip(): continue
            obj = json.loads(line)
            cid = obj.get("custom_id")
            try:
                if obj.get("result", {}).get("type") == "succeeded":
                    raw = obj["result"]["message"]["content"][0]["text"].strip().replace("```json","").replace("```","").strip()
                    data = json.loads(raw)
                    result_map[cid] = {"score": max(1, min(10, int(data.get("score",5)))), "reason": data.get("reason","")}
                else:
                    result_map[cid] = {"score": 5, "reason": "Scoring nedostupný"}
            except:
                result_map[cid] = {"score": 5, "reason": "Parsing selhal"}
    except Exception as e:
        log.error(f"Načtení výsledků selhalo: {e}")
        return _fallback_scoring(jobs, api_key, hdrs)
    # Přiřaď výsledky
    for i, job in enumerate(jobs):
        res = result_map.get(str(i), {"score": 5, "reason": ""})
        job["score"] = res["score"]
        job["score_reason"] = res["reason"]
        log.info(f"  ★ {job['score']}/10 – {job['title']}: {job['score_reason']}")
    log.info(f"Scoring hotov. Odhadované náklady: ~${len(jobs)*0.0002:.4f}")
    return jobs

def _fallback_scoring(jobs, api_key, hdrs):
    """Záložní individuální volání při selhání Batch API."""
    fallback_hdrs = {**hdrs, "anthropic-beta": "prompt-caching-2024-07-31"}
    for job in jobs:
        try:
            r = requests.post("https://api.anthropic.com/v1/messages", headers=fallback_hdrs,
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 120,
                      "system": SYSTEM_PROMPT_CACHED,
                      "messages": [{"role": "user", "content":
                          f"Pozice: {job.get('title','')}\nSpolečnost: {job.get('company','')}\nPopis: {job.get('description','') or '(bez popisu)'}"}]},
                timeout=20)
            r.raise_for_status()
            raw = r.json()["content"][0]["text"].strip().replace("```json","").replace("```","").strip()
            data = json.loads(raw)
            job["score"] = max(1, min(10, int(data.get("score",5))))
            job["score_reason"] = data.get("reason","")
        except Exception as e:
            log.warning(f"Fallback selhal pro '{job.get('title','?')}': {e}")
            job["score"] = 5
            job["score_reason"] = "Scoring nedostupný"
        log.info(f"  ★ {job['score']}/10 – {job['title']}: {job['score_reason']}")
        time.sleep(0.3)
    return jobs

# ──────────────────────────────────────────────────────
#  Email notifikace
# ──────────────────────────────────────────────────────
def send_email(jobs):
    cfg = CONFIG["email"]
    if not all([cfg["sender"], cfg["password"], cfg["recipient"]]):
        log.warning("Email není nakonfigurován – přeskakuji")
        return
    count = len(jobs)
    avg = round(sum(j.get("score",5) for j in jobs) / count, 1)
    good = len([j for j in jobs if j.get("score",0) >= 7])
    sorted_jobs = sorted(jobs, key=lambda j: j.get("score",0), reverse=True)
    rows = ""
    for j in sorted_jobs:
        s = j.get("score",5)
        col = score_color(s)
        lbl = score_label(s)
        loc = f' · <span style="color:#888">{j["location"]}</span>' if j.get("location") else ""
        rsn = f'<div style="font-size:12px;color:#999;margin-top:4px;font-style:italic">{j["score_reason"]}</div>' if j.get("score_reason") else ""
        rows += f"""<tr><td style="padding:14px 12px;border-bottom:1px solid #f0f0f0;vertical-align:top">
          <div style="margin-bottom:6px">
            <span style="background:{col};color:white;font-weight:700;font-size:14px;border-radius:6px;padding:3px 10px">{s}/10</span>
            <span style="font-size:11px;color:{col};margin-left:8px;font-weight:600">{lbl}</span>
          </div>
          <a href="{j['link']}" style="font-size:15px;font-weight:700;color:#1a1a1a;text-decoration:none;display:block;margin-bottom:3px">{j['title']}</a>
          <span style="font-size:13px;color:#555">{j.get("company","")}</span>{loc}
          <span style="font-size:11px;background:#f5f5f5;color:#777;border-radius:4px;padding:2px 7px;margin-left:6px">{j['portal']}</span>
          {rsn}</td></tr>"""
    html = f"""<div style="font-family:Arial,sans-serif;max-width:640px;margin:auto">
      <div style="background:#1D9E75;padding:22px 28px;border-radius:10px 10px 0 0">
        <h1 style="color:white;margin:0;font-size:22px">🔔 Job Alert</h1>
        <p style="color:rgba(255,255,255,.85);margin:6px 0 0;font-size:13px">{datetime.now().strftime('%d.%m.%Y %H:%M')} · marketing · Praha + SČK · plný úvazek</p>
      </div>
      <div style="background:#f8f8f6;border:1px solid #e5e5e5;border-top:none;padding:16px 28px">
        <table style="width:100%;text-align:center"><tr>
          <td><div style="font-size:28px;font-weight:700">{count}</div><div style="font-size:12px;color:#888">nových inzerátů</div></td>
          <td><div style="font-size:28px;font-weight:700;color:#1D9E75">{avg}</div><div style="font-size:12px;color:#888">průměrný fit /10</div></td>
          <td><div style="font-size:28px;font-weight:700">{good}</div><div style="font-size:12px;color:#888">dobrý fit (7+)</div></td>
        </tr></table>
      </div>
      <div style="background:white;border:1px solid #e5e5e5;border-top:none;border-radius:0 0 10px 10px">
        <table style="width:100%;border-collapse:collapse">{rows}</table>
      </div>
      <p style="font-size:11px;color:#bbb;text-align:center;margin-top:14px">Scoring: Claude Haiku AI · Batch API + prompt caching</p>
    </div>"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Job Alert] {count} nových · fit {avg}/10 · {datetime.now().strftime('%d.%m.%Y')}"
    msg["From"] = cfg["sender"]
    msg["To"] = cfg["recipient"]
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as srv:
            srv.starttls()
            srv.login(cfg["sender"], cfg["password"])
            srv.sendmail(cfg["sender"], cfg["recipient"], msg.as_string())
        log.info(f"Email odeslan → {cfg['recipient']}")
    except Exception as e:
        log.error(f"Email selhal: {e}")

# ──────────────────────────────────────────────────────
#  WhatsApp notifikace
# ──────────────────────────────────────────────────────
def send_whatsapp(jobs):
    cfg = CONFIG["whatsapp"]
    if not all([cfg["token"], cfg["phone_id"], cfg["recipient"]]):
        log.warning("WhatsApp není nakonfigurován – přeskakuji")
        return
    count = len(jobs)
    avg = round(sum(j.get("score",5) for j in jobs) / count, 1)
    top3 = sorted(jobs, key=lambda j: j.get("score",0), reverse=True)[:3]
    lines = "".join(f"\n{score_emoji(j.get('score',5))} *{j['title']}* ({j.get('score',5)}/10)\n   {j.get('company','')} · {j['portal']}\n" for j in top3)
    msg = (f"🔔 *Job Alert – {datetime.now().strftime('%d.%m. %H:%M')}*\n\n"
           f"Nalezeno *{count} nových* inzerátů\n📍 Praha + SČK · plný úvazek\n"
           f"⭐ Průměrný fit: *{avg}/10*\n\n*Top 3 pozice:*{lines}\n👉 {top3[0]['link'] if top3 else 'https://www.jobs.cz'}")
    try:
        r = requests.post(f"https://graph.facebook.com/v19.0/{cfg['phone_id']}/messages",
            headers={"Authorization": f"Bearer {cfg['token']}", "Content-Type": "application/json"},
            json={"messaging_product": "whatsapp", "to": cfg["recipient"], "type": "text", "text": {"body": msg}},
            timeout=10)
        r.raise_for_status()
        log.info(f"WhatsApp odeslan → {cfg['recipient']}")
    except Exception as e:
        log.error(f"WhatsApp selhal: {e}")

# ──────────────────────────────────────────────────────
#  Hlavní smyčka
# ──────────────────────────────────────────────────────
def run_check():
    log.info("=" * 55)
    log.info("Spouštím kontrolu inzerátů...")
    seen = load_seen()
    all_jobs = scrape_all()
    new_jobs = []
    for j in all_jobs:
        jid = job_id(j["title"], j["company"], j["portal"])
        if jid not in seen:
            new_jobs.append(j)
            seen.add(jid)
    log.info(f"Nových inzerátů: {len(new_jobs)}")
    if new_jobs:
        new_jobs = score_jobs_batch(new_jobs)
        qualified = [j for j in new_jobs if j.get("score",0) >= CONFIG["min_score"]]
        log.info(f"Po filtru skóre ≥{CONFIG['min_score']}: {len(qualified)}")
        if qualified:
            send_email(qualified)
            send_whatsapp(qualified)
        else:
            log.info("Žádné inzeráty s dostatečným skóre")
    else:
        log.info("Žádné nové inzeráty")
    save_seen(seen)
    log.info("Hotovo.")

def cet_to_utc(t):
    h, m = map(int, t.split(":"))
    now = datetime.now(timezone.utc)
    mo = now.month
    cest = 3 < mo < 10 or (mo==3 and now.day>=25) or (mo==10 and now.day<25)
    return f"{(h - (2 if cest else 1)) % 24:02d}:{m:02d}"

if __name__ == "__main__":
    cet = CONFIG["schedule_time_cet"]
    utc = cet_to_utc(cet)
    log.info("=" * 55)
    log.info(f"Job Alert Agent spuštěn")
    log.info(f"Denní kontrola: {cet} CET = {utc} UTC")
    log.info(f"Min. skóre: {CONFIG['min_score']}/10 | Model: claude-haiku-4-5")
    log.info("=" * 55)
    schedule.every().day.at(utc).do(run_check)
    log.info(f"Čekám na {cet} CET... (Ctrl+C pro ukončení)")
    run_check()  # TEST – spustí kontrolu ihned
    while True:
        schedule.run_pending()
        time.sleep(30)
