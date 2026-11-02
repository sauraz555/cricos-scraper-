"""
Stage 2: High-Speed Hybrid Async HTTP + Playwright Browser Pool Enricher.

Targets ONLY providers and courses that still have missing international-student
details after Stage 1 (ACIR). Combines:
  1. Fast HTTP/1.1 crawling (sitemaps, WordPress ?s= search, /international, /fees, /courses)
  2. Concurrent Playwright Chromium context pool (resource-blocked for 5x speed,
     auto-toggling 'International' student views on university SPAs)
  3. Strict International-Student extraction (IELTS/PTE/TOEFL, Overseas/Onshore/Offshore
     Tuition, Enrolment & Materials Fees, Intakes, Duration, Assessment Style,
     Scholarships, and International Admissions Contacts).
"""
import argparse
import asyncio
import datetime as dt
import html
import json
import os
import re
import sqlite3
import time
from urllib.parse import urljoin, urlparse, quote_plus

import httpx
from playwright.async_api import async_playwright, Browser, BrowserContext
from selectolax.parser import HTMLParser

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_DIR, "data", "dashboard_mirror.sqlite")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
)
SHORT_TO_FULL_MONTH = {
    "jan": "January", "feb": "February", "mar": "March", "apr": "April",
    "may": "May", "jun": "June", "jul": "July", "aug": "August",
    "sep": "September", "sept": "September", "oct": "October",
    "nov": "November", "dec": "December",
}


GENERIC_LAST_SEGMENTS = {
    "about", "home", "index", "index.html", "index.php", "international",
    "international-students", "courses", "study", "contact", "contact-us", "search",
}


def is_deep_url(u: str | None, generic_urls: set[str] | None = None) -> bool:
    if not u:
        return False
    u_str = str(u).strip()
    if generic_urls and u_str in generic_urls:
        return False
    try:
        p = urlparse(u_str)
        path = p.path.rstrip("/")
        if not path:
            return False
        last_seg = path.rsplit("/", 1)[-1].lower()
        if last_seg in GENERIC_LAST_SEGMENTS:
            return False
        return True
    except Exception:
        return False


def slugify_tokens(text: str) -> list[str]:
    stop = {"of", "in", "and", "the", "for", "to", "with", "a", "an"}
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    return [t for t in toks if t not in stop and len(t) > 1]


def parse_money(val_str: str) -> float | None:
    try:
        v = float(val_str.replace(",", "").strip())
        return v if v > 0 else None
    except Exception:
        return None


def extract_international_fees(text: str) -> dict:
    """
    Extract international student fees while strictly ignoring CSP / Domestic fees.
    Returns dict with keys: offshore_tuition_fee, onshore_tuition_fee, enrolment_fee, materials_fee.
    """
    out = {
        "offshore_tuition_fee": None,
        "onshore_tuition_fee": None,
        "enrolment_fee": None,
        "materials_fee": None,
    }
    if not text:
        return out

    # Enrolment / Application fee (typically $50 - $1,000)
    enrol_m = re.search(
        r"(?:enrolment|enrollment|application|admin(?:istration)?)\s+fee[^\$\d]{0,35}\$\s*([\d,]{2,6}(?:\.\d{1,2})?)",
        text,
        re.I,
    )
    if enrol_m:
        v = parse_money(enrol_m.group(1))
        if v and 20 <= v <= 2500:
            out["enrolment_fee"] = v

    # Materials / Resource / Equipment fee (typically $50 - $6,000)
    mat_m = re.search(
        r"(?:material(?:s)?|resource(?:s)?|equipment|consumables?)\s+fee[^\$\d]{0,35}\$\s*([\d,]{2,6}(?:\.\d{1,2})?)",
        text,
        re.I,
    )
    if mat_m:
        v = parse_money(mat_m.group(1))
        if v and 20 <= v <= 10000:
            out["materials_fee"] = v

    # Explicit Onshore vs Offshore international tuition fee
    onshore_m = re.search(
        r"onshore(?:\s+international)?(?:\s+tuition)?\s*(?:fee|price|cost)?[^\$\d]{0,40}\$\s*([\d,]{4,7}(?:\.\d{1,2})?)",
        text,
        re.I,
    )
    if onshore_m:
        v = parse_money(onshore_m.group(1))
        if v and 1500 <= v <= 450000:
            out["onshore_tuition_fee"] = v

    offshore_m = re.search(
        r"offshore(?:\s+international)?(?:\s+tuition)?\s*(?:fee|price|cost)?[^\$\d]{0,40}\$\s*([\d,]{4,7}(?:\.\d{1,2})?)",
        text,
        re.I,
    )
    if offshore_m:
        v = parse_money(offshore_m.group(1))
        if v and 1500 <= v <= 450000:
            out["offshore_tuition_fee"] = v

    # General International / Overseas / CRICOS tuition fee
    if not out["offshore_tuition_fee"]:
        intl_m = re.search(
            r"(?:international|overseas|cricos)[^\n\$\d]{0,50}(?:tuition|course|total|annual)?\s*(?:fee|cost)[^\$\d]{0,35}\$\s*([\d,]{4,7}(?:\.\d{1,2})?)",
            text,
            re.I,
        )
        if not intl_m:
            intl_m = re.search(
                r"(?:tuition\s+fee|total\s+course\s+fee)[^\n\$]{0,40}?\$\s*([\d,]{4,7}(?:\.\d{1,2})?)",
                text,
                re.I,
            )
            # Guard against CSP / Domestic proximity
            if intl_m:
                ctx = text[max(0, intl_m.start() - 80) : intl_m.end() + 80].lower()
                if "commonwealth supported" in ctx or "csp" in ctx or "hecs" in ctx:
                    if "international" not in ctx and "cricos" not in ctx:
                        intl_m = None
        if intl_m:
            v = parse_money(intl_m.group(1))
            if v and 1500 <= v <= 450000:
                out["offshore_tuition_fee"] = v

    return out


def extract_intakes(text: str) -> str | None:
    if not text:
        return None
    # Look near "intake", "start date", "commencement"
    found_months = []
    for m in re.finditer(
        r"(?:intake(?:s)?|start\s+date(?:s)?|commencement|orientation|term\s+start)[^\n]{0,160}",
        text,
        re.I,
    ):
        seg = m.group(0)
        if re.search(r"\bmonthly\b|\bevery\s+month\b", seg, re.I):
            return "Monthly"
        for tok in re.findall(r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b", seg, re.I):
            full = SHORT_TO_FULL_MONTH.get(tok[:3].lower(), tok.capitalize())
            if full not in found_months:
                found_months.append(full)
        if not found_months:
            sem_m = re.findall(r"\b(Semester\s*[123]|Trimester\s*[123]|Term\s*[1234])\b", seg, re.I)
            for sm in sem_m:
                norm_sm = " ".join(sm.split()).title()
                if norm_sm not in found_months:
                    found_months.append(norm_sm)
    if found_months:
        # Sort calendar months in order
        m_order = {m: i for i, m in enumerate(MONTH_NAMES)}
        found_months.sort(key=lambda x: m_order.get(x, 99))
        return ", ".join(found_months[:12])
    return None


def extract_entry_requirements_html(text: str) -> str | None:
    if not text:
        return None
    bullets = []

    # 1. IELTS
    ielts_m = re.search(
        r"IELTS(?:\s+Academic)?[^\n.]{0,90}?\b([45678](?:\.[05])?)\b[^\n.]{0,90}",
        text,
        re.I,
    )
    if ielts_m:
        s = re.sub(r"\s+", " ", ielts_m.group(0)).strip(" ,;:-")
        bullets.append(f"<strong>English (IELTS):</strong> {html.escape(s[:220])}")

    # 2. PTE / TOEFL
    pte_m = re.search(r"PTE(?:\s+Academic)?[^\n.]{0,70}?\b(\d{2})\b[^\n.]{0,60}", text, re.I)
    if pte_m:
        s = re.sub(r"\s+", " ", pte_m.group(0)).strip(" ,;:-")
        bullets.append(f"<strong>English (PTE):</strong> {html.escape(s[:180])}")

    # 3. Academic Year 12 / AQF / Bachelor
    acad_m = re.search(
        r"(?:completion\s+of\s+(?:Australian\s+)?Year\s+1[012]|Australian\s+Year\s+12|bachelor(?:'s)?\s+degree|AQF\s+Level\s+\d+|Certificate\s+(?:III|IV)|Diploma\s+in)[^\n.]{0,160}",
        text,
        re.I,
    )
    if acad_m:
        s = re.sub(r"\s+", " ", acad_m.group(0)).strip(" ,;:-")
        if "commonwealth supported" not in s.lower() and "hecs" not in s.lower():
            bullets.append(f"<strong>Academic Requirement:</strong> {html.escape(s[:240])}")

    # 4. Minimum age for international students
    age_m = re.search(r"(?:minimum\s+age|must\s+be(?:\s+at\s+least)?|aged?)\s*18\s*(?:years|\+)", text, re.I)
    if age_m:
        bullets.append("<strong>Age Requirement:</strong> Minimum 18 years of age at course commencement.")

    if not bullets:
        return None
    li_html = "".join(f"<li>{b}</li>" for b in bullets)
    return f"<h4>Entry Requirements (International Students)</h4><ul>{li_html}</ul>"


def extract_description_html(tree: HTMLParser, course_name: str) -> str | None:
    # Check meta description first as clean fallback, and main content paragraphs
    paras = []
    for p in tree.css("main p, article p, .entry-content p, .course-overview p, .course-description p, section p"):
        t = re.sub(r"\s+", " ", p.text(strip=True))
        if len(t) < 65 or len(t) > 1200:
            continue
        tl = t.lower()
        if any(
            bad in tl
            for bad in (
                "cookie", "privacy policy", "copyright", "all rights reserved",
                "acknowledge the traditional", "traditional custodians",
                "commonwealth supported place", "hecs-help",
            )
        ):
            continue
        if t not in paras:
            paras.append(t)
        if len(paras) >= 3:
            break

    if not paras:
        meta = tree.css_first('meta[name="description"], meta[property="og:description"]')
        if meta:
            md = re.sub(r"\s+", " ", (meta.attributes.get("content") or "").strip())
            if len(md) >= 60:
                paras.append(md)

    if not paras:
        return None
    return "<h4>Course Information</h4> " + " ".join(f"<p>{html.escape(p)}</p>" for p in paras)


def extract_duration_text(text: str) -> str | None:
    m = re.search(
        r"(?:duration|course\s+length|full[- ]time)\s*[:\-]?\s*(\d+(?:\.\d+)?\s*(?:weeks?|years?|months?)(?:\s*\(\d+\s*weeks?\))?(?:\s*full[- ]time)?)",
        text,
        re.I,
    )
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    return None


def classify_assessment(text: str, is_vet: bool = False) -> str | None:
    if not text:
        return "mixed" if is_vet else None
    tl = text.lower()
    has_exam = bool(re.search(r"\b(written exam|final exam|examination|examinations|invigilated exam)\b", tl))
    has_assign = bool(
        re.search(
            r"\b(assignment|assignments|project|portfolio|case study|practical|observation|competency|role play|presentation|work placement)\b",
            tl,
        )
    )
    if has_exam and has_assign:
        return "mixed"
    if has_exam:
        return "exam"
    if has_assign:
        return "assignment"
    return "assignment" if is_vet else None


def extract_contacts(text: str, html_str: str, base_url: str) -> tuple[str | None, str | None, str]:
    domain = urlparse(base_url).netloc.lower().replace("www.", "")
    emails = set(re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", html_str or ""))
    valid_emails = [
        e.lower()
        for e in emails
        if not any( e.lower().endswith(ext) for ext in (".png", ".jpg", ".webp", ".svg", ".js", ".css") )
        and "sentry" not in e.lower()
        and "example.com" not in e.lower()
    ]
    # Rank emails: international > admissions > enrol > info > same-domain
    def email_rank(e: str):
        score = 0
        if domain and domain in e:
            score += 10
        if "international" in e:
            score += 8
        elif any(k in e for k in ("admission", "enrol", "study", "future", "enquir")):
            score += 5
        elif "info@" in e or "contact@" in e:
            score += 3
        return -score

    valid_emails.sort(key=email_rank)
    best_email = valid_emails[0] if valid_emails else None

    phones = re.findall(
        r"(?:\+61\s*[23478][\s\d]{7,10}|\b0[23478][\s\d]{8,10}\b|\b1[38]00[\s\d]{6,8}\b)",
        text or "",
    )
    best_phone = re.sub(r"\s+", " ", phones[0]).strip() if phones else None
    return best_email, best_phone, base_url


class HybridFetcher:
    """Fast HTTP/1.1 first; Playwright Chromium pool fallback with resource blocking & International tab toggling."""

    def __init__(self, pw_concurrency: int = 6, http_concurrency: int = 24):
        self.pw_sem = asyncio.Semaphore(pw_concurrency)
        self.http_sem = asyncio.Semaphore(http_concurrency)
        self.http_client: httpx.AsyncClient | None = None
        self.pw = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None

    async def __aenter__(self):
        self.http_client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-AU,en;q=0.9"},
            follow_redirects=True,
            timeout=10.0,
            http2=False,  # Avoids ERR_HTTP2_PROTOCOL_ERROR on college servers
            verify=False,
            limits=httpx.Limits(max_connections=35, max_keepalive_connections=20),
        )
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            headless=True,
            args=["--disable-http2", "--disable-blink-features=AutomationControlled"],
        )
        self.context = await self.browser.new_context(
            user_agent=USER_AGENT,
            locale="en-AU",
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True,
        )
        await self.context.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,eot,mp4,webm,mp3,avi}",
            lambda route: route.abort(),
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.http_client:
            await self.http_client.aclose()
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()

    async def fetch(self, url: str, allow_playwright: bool = True, toggle_international: bool = False) -> tuple[str | None, str | None, str]:
        """Returns (html_content, plain_text, final_url)."""
        # 1. Try fast HTTP first
        dns_or_connect_dead = False
        async with self.http_sem:
            try:
                r = await self.http_client.get(url)
                if r.status_code == 404:
                    return None, None, url
                if r.status_code == 200 and "text/html" in r.headers.get("content-type", "text/html"):
                    html_str = r.text
                    tree = HTMLParser(html_str)
                    for tag in tree.css("script, style, noscript"):
                        tag.decompose()
                    txt = tree.body.text(separator="\n", strip=True) if tree.body else ""
                    # Check if page is real content vs JS shell / Cloudflare challenge
                    if len(txt) >= 450 and "Just a moment..." not in txt and "Enable JavaScript" not in txt[:300]:
                        if not toggle_international:
                            return html_str, txt, str(r.url)
                        # Only need Playwright toggle if domestic CSP text is present without international info
                        if "international" in txt.lower() or "cricos" in txt.lower():
                            return html_str, txt, str(r.url)
            except httpx.ConnectError as ce:
                if "getaddrinfo failed" in str(ce) or "Name or service not known" in str(ce):
                    dns_or_connect_dead = True
            except Exception:
                pass

        if not allow_playwright or dns_or_connect_dead:
            return None, None, url

        # 2. Escalate to Playwright Chromium
        async with self.pw_sem:
            page = await self.context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=14000)
                if toggle_international:
                    # Try clicking International student switcher if visible
                    for sel in (
                        "button:has-text('International')",
                        "[role='tab']:has-text('International')",
                        "a:has-text('International students')",
                    ):
                        loc = page.locator(sel).first
                        if await loc.count() > 0 and await loc.is_visible():
                            try:
                                await loc.click(timeout=1500)
                                await page.wait_for_load_state("domcontentloaded", timeout=2000)
                                break
                            except Exception:
                                pass
                html_str = await page.content()
                txt = await page.inner_text("body")
                final_u = page.url
                if "Just a moment..." in (await page.title()):
                    return None, None, final_u
                return html_str, txt, final_u
            except Exception:
                return None, None, url
            finally:
                await page.close()

    async def discover_missing_website(self, inst_name: str, cricos_code: str) -> str | None:
        """Fast DuckDuckGo HTML lookup for providers with NULL website."""
        q = f'"{cricos_code}" "{inst_name}" Australia'
        search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(q)}"
        html_str, _, _ = await self.fetch(search_url, allow_playwright=False)
        if not html_str:
            return None
        tree = HTMLParser(html_str)
        skip_domains = (
            "cricos.education.gov.au", "studyaustralia.gov.au", "training.gov.au",
            "gooduniversitiesguide.com.au", "facebook.com", "linkedin.com",
            "wikipedia.org", "youtube.com", "instagram.com", "duckduckgo.com",
            "hotcoursesabroad.com", "idp.com", "myskills.gov.au", "yourcareer.gov.au",
        )
        for a in tree.css("a.result__a"):
            href = a.attributes.get("href") or ""
            if "uddg=" in href:
                from urllib.parse import parse_qs
                qs = parse_qs(urlparse(href).query)
                href = qs.get("uddg", [href])[0]
            if href.startswith("http"):
                p = urlparse(href)
                dom = p.netloc.lower()
                if not any(sd in dom for sd in skip_domains):
                    return f"{p.scheme}://{p.netloc}/"
        return None


async def enrich_single_provider(
    fetcher: HybridFetcher,
    prov: dict,
    existing_ep: dict | None,
    incomplete_courses: list[dict],
    existing_ec_map: dict[int, dict],
) -> tuple[dict, list[dict]]:
    now_iso = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cricos_prov = prov["cricos_provider_code"]
    inst_name = prov["institution_name"]

    website = (existing_ep or {}).get("website") or prov.get("website")
    if website and not website.startswith("http"):
        website = "https://" + website.lstrip("/")

    if not website:
        website = await fetcher.discover_missing_website(inst_name, cricos_prov)

    ep_result = {
        "provider_id": int(prov["id"]),
        "cricos_provider_code": cricos_prov,
        "institution_name": inst_name,
        "website": website,
        "international_url": (existing_ep or {}).get("international_url"),
        "intake_date": (existing_ep or {}).get("intake_date") or prov.get("intake_date"),
        "provider_logo": (existing_ep or {}).get("provider_logo") or prov.get("provider_logo"),
        "contact_email": (existing_ep or {}).get("contact_email"),
        "contact_phone": (existing_ep or {}).get("contact_phone"),
        "contact_website_url": (existing_ep or {}).get("contact_website_url"),
        "scholarships_json": (existing_ep or {}).get("scholarships_json"),
        "source_url": (existing_ep or {}).get("source_url") or website,
        "discovery_method": (existing_ep or {}).get("discovery_method") or "stage2_playwright_http",
        "status": "enriched" if website else "no_website",
        "error_msg": None,
        "updated_at": now_iso,
    }

    if not website:
        return ep_result, []

    parsed_home = urlparse(website)
    origin = f"{parsed_home.scheme}://{parsed_home.netloc}"

    # 1. Fetch provider homepage + discover key international/course/contact/fee links
    home_html, home_txt, final_home = await fetcher.fetch(website, allow_playwright=True)
    site_links: dict[str, str] = {}  # url -> anchor text
    prov_corpus_parts = []
    if home_txt:
        prov_corpus_parts.append(home_txt)

    if home_html:
        tree = HTMLParser(home_html)
        # Extract logo if still missing
        if not ep_result["provider_logo"]:
            og_img = tree.css_first('meta[property="og:image"], img[alt*="logo" i], img[src*="logo" i]')
            if og_img:
                src = og_img.attributes.get("content") or og_img.attributes.get("src")
                if src:
                    ep_result["provider_logo"] = urljoin(final_home, src)

        for a in tree.css("a[href]"):
            href = (a.attributes.get("href") or "").strip()
            if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            full_u = urljoin(final_home, href)
            if urlparse(full_u).netloc.replace("www.", "") == urlparse(final_home).netloc.replace("www.", ""):
                site_links.setdefault(full_u, a.text(strip=True)[:100])

        em, ph, _ = extract_contacts(home_txt or "", home_html, final_home)
        if em and not ep_result["contact_email"]:
            ep_result["contact_email"] = em
        if ph and not ep_result["contact_phone"]:
            ep_result["contact_phone"] = ph

    # Pick up to 4 high-value subpages (international, fees/intakes, courses, contact)
    priority_subpages = []
    for u, anchor in site_links.items():
        comb = (u + " " + anchor).lower()
        if any(k in comb for k in ("international", "enrol", "admission", "intake", "fee", "contact", "scholarship", "courses")):
            if not u.lower().endswith(".pdf"):
                priority_subpages.append(u)
    priority_subpages = list(dict.fromkeys(priority_subpages))[:4]

    if priority_subpages:
        sub_results = await asyncio.gather(*[fetcher.fetch(u, allow_playwright=False) for u in priority_subpages])
        for sub_u, (sh, st, sfinal) in zip(priority_subpages, sub_results):
            if st:
                prov_corpus_parts.append(st)
                if "international" in sub_u.lower() and not ep_result["international_url"]:
                    ep_result["international_url"] = sfinal
                if "contact" in sub_u.lower() or not ep_result["contact_email"]:
                    em, ph, cu = extract_contacts(st, sh or "", sfinal)
                    if em and (not ep_result["contact_email"] or "international" in em):
                        ep_result["contact_email"] = em
                        ep_result["contact_website_url"] = cu
                    if ph and not ep_result["contact_phone"]:
                        ep_result["contact_phone"] = ph
            if sh:
                for a in HTMLParser(sh).css("a[href]"):
                    href = (a.attributes.get("href") or "").strip()
                    if href and not href.startswith(("mailto:", "tel:", "javascript:", "#")):
                        fu = urljoin(sfinal, href)
                        if urlparse(fu).netloc.replace("www.", "") == urlparse(final_home).netloc.replace("www.", ""):
                            site_links.setdefault(fu, a.text(strip=True)[:100])

    if not ep_result["contact_website_url"] and (ep_result["contact_email"] or ep_result["contact_phone"]):
        ep_result["contact_website_url"] = final_home

    prov_corpus = "\n".join(prov_corpus_parts)
    prov_fees = extract_international_fees(prov_corpus)
    prov_intakes = extract_intakes(prov_corpus)
    if prov_intakes and not ep_result["intake_date"]:
        ep_result["intake_date"] = prov_intakes
    prov_entry_fallback = extract_entry_requirements_html(prov_corpus)

    # Try sitemap.xml quickly to index deep course URLs on this provider's domain
    for sm_path in ("/sitemap.xml", "/course-sitemap.xml", "/courses-sitemap.xml"):
        sm_html, _, _ = await fetcher.fetch(origin + sm_path, allow_playwright=False)
        if sm_html and "<loc>" in sm_html:
            for loc in re.findall(r"<loc>(https?://[^<]+)</loc>", sm_html)[:1500]:
                if not loc.endswith(".xml"):
                    site_links.setdefault(loc.strip(), "")
            break

    # 2. Enrich incomplete courses on this provider (cap per-provider deep fetches so giant unis stay fast)
    course_updates = []
    # Identify generic placeholder URLs shared across >= 3 courses on this provider
    url_counts: dict[str, int] = {}
    for dc in incomplete_courses:
        u = (dc.get("apply_form") or "").strip()
        if u:
            url_counts[u] = url_counts.get(u, 0) + 1
    generic_urls = {u for u, cnt in url_counts.items() if cnt >= 3}

    # Prioritize courses still missing description, entry_requirements, or deep URL
    targets = []
    for dc in incomplete_courses:
        cid = int(dc["id"])
        ec = existing_ec_map.get(cid, {})
        cur_url = ec.get("apply_form") or (
            dc.get("apply_form") if is_deep_url(dc.get("apply_form"), generic_urls) else None
        )
        cur_desc = ec.get("course_description") or dc.get("course_description")
        cur_entry = ec.get("entry_requirements") or dc.get("entry_requirements")
        if not cur_url or not cur_desc or not cur_entry or not dc.get("enrolment_fee") or not dc.get("assessment_style"):
            targets.append((dc, ec, cur_url))

    # Match candidate URLs from site_links for courses lacking a deep URL
    async def process_course(dc: dict, ec: dict, cur_url: str | None, allow_live_fetch: bool):
        cid = int(dc["id"])
        c_code = (dc.get("cricos_course_code") or "").strip()
        v_code = (dc.get("vet_national_code") or "").strip()
        c_name = (dc.get("course_name") or "").strip()
        toks = slugify_tokens(c_name)

        best_url = cur_url
        match_method = ec.get("match_method") or "provider_fallback"
        conf = float(ec.get("match_confidence") or 0.7)

        if not best_url and site_links:
            best_score = 0
            for cand_u, anchor in site_links.items():
                if cand_u.lower().endswith((".pdf", ".jpg", ".png", ".zip")):
                    continue
                hay = (cand_u + " " + anchor).lower()
                sc = 0
                if c_code and c_code.lower() in hay:
                    sc += 100
                if v_code and v_code.lower() in hay:
                    sc += 90
                if toks:
                    matched_toks = sum(1 for t in toks if t in hay)
                    ratio = matched_toks / len(toks)
                    if ratio >= 0.75:
                        sc += int(ratio * 60)
                if sc > best_score and is_deep_url(cand_u):
                    best_score = sc
                    best_url = cand_u
            if best_url and best_score >= 45:
                match_method = "provider_sitemap_or_link_match"
                conf = min(0.95, best_score / 100.0)
            else:
                best_url = None

        page_desc = ec.get("course_description")
        page_entry = ec.get("entry_requirements")
        page_dur = ec.get("total_course_duration")
        page_assess = ec.get("assessment_style")
        page_intake = ec.get("intake_dates") or ep_result.get("intake_date")
        fees = {
            "onshore_tuition_fee": ec.get("onshore_tuition_fee"),
            "offshore_tuition_fee": ec.get("offshore_tuition_fee"),
            "enrolment_fee": ec.get("enrolment_fee") or prov_fees["enrolment_fee"],
            "materials_fee": ec.get("materials_fee") or prov_fees["materials_fee"],
        }

        if allow_live_fetch and best_url and (not page_desc or not page_entry or not fees["enrolment_fee"]):
            chtml, ctxt, cfinal = await fetcher.fetch(best_url, allow_playwright=True, toggle_international=True)
            if chtml and ctxt:
                best_url = cfinal
                if c_code and c_code.lower() in ctxt.lower():
                    conf = 1.0
                    match_method = "verified_cricos_on_course_page"
                ctree = HTMLParser(chtml)
                if not page_desc and not dc.get("course_description"):
                    page_desc = extract_description_html(ctree, c_name)
                if not page_entry and not dc.get("entry_requirements"):
                    page_entry = extract_entry_requirements_html(ctxt)
                cfees = extract_international_fees(ctxt)
                for fk in ("onshore_tuition_fee", "offshore_tuition_fee", "enrolment_fee", "materials_fee"):
                    if cfees[fk] and not fees[fk]:
                        fees[fk] = cfees[fk]
                if not page_dur and not dc.get("total_course_duration"):
                    page_dur = extract_duration_text(ctxt)
                if not page_assess and not dc.get("assessment_style"):
                    page_assess = classify_assessment(ctxt, is_vet=bool(v_code))
                ci = extract_intakes(ctxt)
                if ci:
                    page_intake = ci

        # Apply clean provider-level international fallbacks where course page didn't repeat college-wide policies
        if not page_entry and not dc.get("entry_requirements") and prov_entry_fallback:
            page_entry = prov_entry_fallback
        if not page_dur and not dc.get("total_course_duration") and dc.get("course_duration_per_week"):
            try:
                wks = int(dc["course_duration_per_week"])
                if wks > 0:
                    page_dur = f"{wks} weeks"
            except Exception:
                pass
        if not page_assess and not dc.get("assessment_style"):
            page_assess = classify_assessment(prov_corpus, is_vet=bool(v_code))

        return (
            cid,
            cricos_prov,
            dc["institution_name"],
            c_code,
            c_name,
            best_url,
            page_desc,
            page_entry,
            fees["onshore_tuition_fee"],
            fees["offshore_tuition_fee"],
            fees["enrolment_fee"],
            fees["materials_fee"],
            page_dur,
            page_assess,
            page_intake,
            ec.get("scholarships_summary"),
            1,
            conf,
            match_method,
            best_url or ep_result["source_url"],
            "stage2_enriched",
            None,
            now_iso,
        )

    # Live-fetch up to 8 missing-content course pages per provider concurrently; apply fast link/provider rules to rest
    live_tasks = []
    for idx, (dc, ec, cur_url) in enumerate(targets):
        need_page = (not (ec.get("course_description") or dc.get("course_description"))) or (
            not (ec.get("entry_requirements") or dc.get("entry_requirements"))
        )
        allow_live = idx < 8 and need_page
        live_tasks.append(process_course(dc, ec, cur_url, allow_live))

    if live_tasks:
        course_updates = await asyncio.gather(*live_tasks)

    return ep_result, list(course_updates)


async def run_stage2(max_providers: int | None = None, resume: bool = True):
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    providers = [dict(r) for r in conn.execute("SELECT * FROM orig_provider_institution")]
    ep_map = {r["provider_id"]: dict(r) for r in conn.execute("SELECT * FROM enriched_providers")}
    ec_map = {r["course_id"]: dict(r) for r in conn.execute("SELECT * FROM enriched_courses")}

    if resume:
        before = len(providers)
        providers = [
            p
            for p in providers
            if ep_map.get(int(p["id"]), {}).get("status") not in ("enriched", "no_website")
        ]
        print(f"Resume enabled: skipping {before - len(providers)} already-scraped providers ({len(providers)} remaining).")

    courses_by_prov: dict[str, list[dict]] = {}
    for r in conn.execute("SELECT * FROM orig_courses"):
        d = dict(r)
        courses_by_prov.setdefault(d["cricos_provider_code"], []).append(d)

    # Prioritize providers that have the most courses still missing descriptions/entry_requirements/URLs/contacts
    def prov_priority(p: dict):
        code = p["cricos_provider_code"]
        ep = ep_map.get(int(p["id"]), {})
        cs = courses_by_prov.get(code, [])
        missing_core = 0
        for c in cs:
            ec = ec_map.get(int(c["id"]), {})
            if not (ec.get("course_description") or c.get("course_description")):
                missing_core += 2
            if not (ec.get("entry_requirements") or c.get("entry_requirements")):
                missing_core += 2
            if not (ec.get("apply_form") or is_deep_url(c.get("apply_form"))):
                missing_core += 1
        no_web = 10 if not (ep.get("website") or p.get("website")) else 0
        no_intake = 3 if not (ep.get("intake_date") or p.get("intake_date")) else 0
        return -(missing_core + no_web + no_intake)

    providers.sort(key=prov_priority)
    if max_providers:
        providers = providers[:max_providers]

    print(f"Stage 2 starting across {len(providers)} providers using Hybrid HTTP + Playwright Chromium pool...")

    prov_sem = asyncio.Semaphore(20)

    async with HybridFetcher(pw_concurrency=8, http_concurrency=36) as fetcher:
        async def bounded_prov(p: dict):
            async with prov_sem:
                pid = int(p["id"])
                code = p["cricos_provider_code"]
                try:
                    return await asyncio.wait_for(
                        enrich_single_provider(
                            fetcher, p, ep_map.get(pid), courses_by_prov.get(code, []), ec_map
                        ),
                        timeout=22.0,
                    )
                except asyncio.TimeoutError:
                    now_iso = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    existing = ep_map.get(pid) or {}
                    return (
                        {
                            "provider_id": pid,
                            "cricos_provider_code": code,
                            "institution_name": p["institution_name"],
                            "website": existing.get("website") or p.get("website"),
                            "international_url": existing.get("international_url"),
                            "intake_date": existing.get("intake_date") or p.get("intake_date"),
                            "provider_logo": existing.get("provider_logo") or p.get("provider_logo"),
                            "contact_email": existing.get("contact_email"),
                            "contact_phone": existing.get("contact_phone"),
                            "contact_website_url": existing.get("contact_website_url"),
                            "scholarships_json": existing.get("scholarships_json"),
                            "source_url": existing.get("source_url") or p.get("website"),
                            "discovery_method": existing.get("discovery_method") or "timeout_partial",
                            "status": "enriched",
                            "error_msg": "provider_timeout_22s",
                            "updated_at": now_iso,
                        },
                        [],
                    )

        batch_size = 40
        total_c_updated = 0
        for i in range(0, len(providers), batch_size):
            batch = providers[i : i + batch_size]
            results = await asyncio.gather(*[bounded_prov(p) for p in batch])

            ep_rows = []
            ec_rows = []
            for ep_res, c_list in results:
                ep_rows.append(
                    (
                        ep_res["provider_id"],
                        ep_res["cricos_provider_code"],
                        ep_res["institution_name"],
                        ep_res["website"],
                        ep_res["international_url"],
                        ep_res["intake_date"],
                        ep_res["provider_logo"],
                        ep_res["contact_email"],
                        ep_res["contact_phone"],
                        ep_res["contact_website_url"],
                        ep_res["scholarships_json"],
                        ep_res["source_url"],
                        ep_res["discovery_method"],
                        ep_res["status"],
                        ep_res["error_msg"],
                        ep_res["updated_at"],
                    )
                )
                ec_rows.extend(c_list)
                total_c_updated += len(c_list)

            conn.executemany(
                """
                INSERT OR REPLACE INTO enriched_providers
                (provider_id, cricos_provider_code, institution_name, website, international_url,
                 intake_date, provider_logo, contact_email, contact_phone, contact_website_url,
                 scholarships_json, source_url, discovery_method, status, error_msg, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ep_rows,
            )
            if ec_rows:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO enriched_courses
                    (course_id, cricos_provider_code, institution_name, cricos_course_code, course_name,
                     apply_form, course_description, entry_requirements, onshore_tuition_fee,
                     offshore_tuition_fee, enrolment_fee, materials_fee, total_course_duration,
                     assessment_style, intake_dates, scholarships_summary, international_verified,
                     match_confidence, match_method, source_url, status, error_msg, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ec_rows,
                )
            conn.commit()
            print(
                f"  Stage 2 progress: {min(i+batch_size, len(providers))}/{len(providers)} providers | {total_c_updated} course records enriched ({time.time()-t0:.1f}s)"
            )

    conn.close()
    print(f"Stage 2 finished in {time.time()-t0:.1f}s!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-providers", type=int, default=None, help="Optional cap on providers per run")
    args = parser.parse_args()
    asyncio.run(run_stage2(max_providers=args.max_providers))
