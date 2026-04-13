"""
Job portal parsers with multiple CSS selector chains per portal.
Falls back to the next selector chain when the primary one finds nothing,
and logs which chain succeeded for observability.
"""

import logging

from bs4 import BeautifulSoup, Tag

from config import CONFIG

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────
#  Internal helpers
# ──────────────────────────────────────────────────────
def _first(card: Tag, selectors: list[str]) -> Tag | None:
    """Return the first element matched by any selector in the list."""
    for sel in selectors:
        el = card.select_one(sel)
        if el:
            return el
    return None


def _build_job(
    card: Tag,
    title_sels: list[str],
    company_sels: list[str],
    loc_sels: list[str],
    desc_sels: list[str],
    portal: str,
    page_url: str,
    url_prefix: str = "",
) -> dict | None:
    """
    Build a job dict from a BS4 card element.
    Returns None if no title element could be found.
    """
    t = _first(card, title_sels)
    if not t:
        return None

    link = t.get("href", page_url)
    if link and str(link).startswith("/") and url_prefix:
        link = url_prefix + link

    c = _first(card, company_sels) if company_sels else None
    l = _first(card, loc_sels)     if loc_sels     else None
    d = _first(card, desc_sels)    if desc_sels     else None

    return {
        "title":       t.get_text(strip=True),
        "company":     c.get_text(strip=True) if c else "",
        "location":    l.get_text(strip=True) if l else "",
        "description": d.get_text(strip=True) if d else "",
        "link":        link,
        "portal":      portal,
    }


def _parse_with_card_chains(
    soup: BeautifulSoup,
    card_chains: list[str],
    title_sels: list[str],
    company_sels: list[str],
    loc_sels: list[str],
    desc_sels: list[str],
    portal: str,
    url: str,
    url_prefix: str = "",
) -> list[dict]:
    """
    Iterate over *card_chains* until one returns cards, then parse them.
    Logs which selector chain was used.
    """
    for card_sel in card_chains:
        cards = soup.select(card_sel)
        if cards:
            log.debug("[%s] Card selector '%s' matched %d cards", portal, card_sel, len(cards))
            jobs = []
            for card in cards:
                j = _build_job(card, title_sels, company_sels, loc_sels, desc_sels,
                               portal, url, url_prefix)
                if j:
                    jobs.append(j)
            return jobs
    log.debug("[%s] No card selector matched for %s", portal, url)
    return []


# ──────────────────────────────────────────────────────
#  Portal parsers
# ──────────────────────────────────────────────────────
def parse_jobs_cz(soup: BeautifulSoup, portal: str, url: str) -> list[dict]:
    return _parse_with_card_chains(
        soup,
        card_chains=[
            "article.SearchResultCard",
            "article[class*='SearchResult']",
            ".search-result-item",
            "article[data-jobad-id]",
        ],
        title_sels=[
            "h2.SearchResultCard__title a",
            "h2[class*='title'] a",
            ".job-title a",
            "h2 a",
        ],
        company_sels=[
            ".SearchResultCard__company",
            "[class*='company']",
            ".employer",
        ],
        loc_sels=[
            ".SearchResultCard__info",
            "[class*='location']",
            "[class*='locality']",
        ],
        desc_sels=[
            ".SearchResultCard__description",
            "[class*='description']",
        ],
        portal=portal, url=url,
    )


def parse_prace_cz(soup: BeautifulSoup, portal: str, url: str) -> list[dict]:
    return _parse_with_card_chains(
        soup,
        card_chains=[
            ".offer-item",
            ".job-offer",
            "[class*='offer-item']",
            "article[class*='offer']",
            "article",
        ],
        title_sels=[
            "h2 a", "h3 a",
            ".offer-title a",
            ".job-title a",
            "a[class*='title']",
        ],
        company_sels=[
            ".company-name",
            ".employer",
            "[class*='company']",
        ],
        loc_sels=[
            ".location",
            ".locality",
            "[class*='location']",
        ],
        desc_sels=[
            ".offer-description",
            "[class*='description']",
        ],
        portal=portal, url=url,
    )


def parse_kariera_cz(soup: BeautifulSoup, portal: str, url: str) -> list[dict]:
    return _parse_with_card_chains(
        soup,
        card_chains=[
            ".job-listing__item",
            ".offer-card",
            "[class*='job-listing']",
            "[class*='offer-card']",
            "li[class*='job']",
            "article",
        ],
        title_sels=[
            "a.job-listing__title",
            "a.offer-title",
            "h2 a", "h3 a",
            "a[class*='title']",
        ],
        company_sels=[
            ".job-listing__company",
            ".offer-company",
            "[class*='company']",
        ],
        loc_sels=[
            ".job-listing__location",
            ".offer-location",
            "[class*='location']",
        ],
        desc_sels=[],
        portal=portal, url=url,
    )


def parse_profesia_cz(soup: BeautifulSoup, portal: str, url: str) -> list[dict]:
    return _parse_with_card_chains(
        soup,
        card_chains=[
            "li.list-row",
            ".offer-item",
            "li[class*='list']",
            "[class*='offer']",
        ],
        title_sels=[
            "h2 a",
            ".title a",
            "a[class*='title']",
            "a",
        ],
        company_sels=[
            ".company",
            ".employer-title",
            "[class*='company']",
            "[class*='employer']",
        ],
        loc_sels=[
            ".job-location",
            ".location",
            "[class*='location']",
        ],
        desc_sels=[],
        portal=portal, url=url, url_prefix="https://www.profesia.cz",
    )


def parse_indeed(soup: BeautifulSoup, portal: str, url: str) -> list[dict]:
    jobs = []
    for card in soup.select(".job_seen_beacon"):
        t = (
            card.select_one("h2.jobTitle a")
            or card.select_one("[data-testid='job-title'] a")
            or card.select_one("h2 a")
        )
        if not t:
            continue
        c = (
            card.select_one("[data-testid='company-name']")
            or card.select_one(".companyName")
            or card.select_one("[class*='company']")
        )
        l = (
            card.select_one("[data-testid='text-location']")
            or card.select_one(".companyLocation")
            or card.select_one("[class*='location']")
        )
        d = card.select_one(".job-snippet") or card.select_one("[class*='description']")
        href = t.get("href", "")
        jobs.append({
            "title":       t.get_text(strip=True),
            "company":     c.get_text(strip=True) if c else "",
            "location":    l.get_text(strip=True) if l else "",
            "description": d.get_text(strip=True) if d else "",
            "link":        "https://cz.indeed.com" + href if str(href).startswith("/") else href,
            "portal":      portal,
        })
    return jobs


def parse_generic(soup: BeautifulSoup, portal: str, url: str) -> list[dict]:
    """
    Generic fallback parser – extracts any link whose text looks like a job title
    and contains at least one configured keyword.
    """
    keywords = CONFIG["keywords"]
    jobs = []
    for a in soup.select("a[href]"):
        title = a.get_text(strip=True)
        if 10 <= len(title) <= 120 and any(kw.lower() in title.lower() for kw in keywords):
            jobs.append({
                "title":       title,
                "company":     "",
                "location":    "",
                "description": "",
                "link":        a["href"],
                "portal":      portal,
            })
    return jobs[:20]


# ──────────────────────────────────────────────────────
#  Parser registry
# ──────────────────────────────────────────────────────
PARSERS: dict[str, callable] = {
    "parse_jobs_cz":    parse_jobs_cz,
    "parse_prace_cz":   parse_prace_cz,
    "parse_kariera_cz": parse_kariera_cz,
    "parse_profesia_cz":parse_profesia_cz,
    "parse_indeed":     parse_indeed,
    "parse_generic":    parse_generic,
}
