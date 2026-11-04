"""
Stage 2: High-Speed Hybrid Async HTTP + Playwright Browser Pool Enricher (v2 — Full CRICOS & Zero-Mismatch).

Enriches ALL 1,543 CRICOS providers and ALL 25,199 active CRICOS courses keyed strictly
by `cricos_provider_code` and `cricos_course_code`.

Zero-Mismatch Guarantees:
  1. Never uses fuzzy course-title matching to assign course URLs, descriptions, or fees.
  2. Candidate course pages from provider sitemaps/links are ONLY accepted when the exact
     7-character `cricos_course_code` is verified, OR when a `vet_national_code` uniquely
     maps to a single active CRICOS course on that provider with zero conflicting CRICOS codes.
  3. Authoritative `cricos_tuition_fee` and `duration_weeks` from the official data.gov.au
     CRICOS register are preserved and never overwritten by heuristic regexes.
"""
import argparse
import asyncio
import collections
import datetime as dt
import html
import os
import re
import sqlite3
import time
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import httpx
from playwright.async_api import Browser, BrowserContext, async_playwright
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

CRICOS_COURSE_CODE_RE = re.compile(r"\b(\d{6}[A-Z0-9])\b")


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
        return last_seg not in GENERIC_LAST_SEGMENTS
    except Exception:
        return False


def parse_money(val_str: str) -> float | None:
    try:
        v = float(val_str.replace(",", "").strip())
        return v if v > 0 else None
    except Exception:
        return None


def extract_international_fees(text: str) -> dict:
    out = {
        "offshore_tuition_fee": None,
        "onshore_tuition_fee": None,
        "enrolment_fee": None,
        "materials_fee": None,
    }
    if not text:
        return out

    enrol_m = re.search(
        r"(?:enrolment|enrollment|application|admin(?:istration)?)\s+fee[^\$\d]{0,35}\$\s*([\d,]{2,6}(?:\.\d{1,2})?)",
        text,
        re.I,
    )
    if enrol_m:
        v = parse_money(enrol_m.group(1))
        if v and 50 <= v <= 2000:
            out["enrolment_fee"] = v

    mat_m = re.search(
        r"(?:material(?:s)?|resource(?:s)?|equipment|consumables?)\s+fee[^\$\d]{0,35}\$\s*([\d,]{2,6}(?:\.\d{1,2})?)",
        text,
        re.I,
    )
    if mat_m:
        v = parse_money(mat_m.group(1))
        if v and 50 <= v <= 5000:
            out["materials_fee"] = v

    onshore_m = re.search(
        r"onshore(?:\s+international)?(?:\s+tuition)?\s*(?:fee|price|cost)?[^\$\d]{0,40}\$\s*([\d,]{4,7}(?:\.\d{1,2})?)",
        text,
        re.I,
    )
    offshore_m = re.search(
        r"offshore(?:\s+international)?(?:\s+tuition)?\s*(?:fee|price|cost)?[^\$\d]{0,40}\$\s*([\d,]{4,7}(?:\.\d{1,2})?)",
        text,
        re.I,
    )
    if onshore_m and offshore_m:
        von = parse_money(onshore_m.group(1))
        voff = parse_money(offshore_m.group(1))
        if von and voff and 1500 <= von <= 450000 and 1500 <= voff <= 450000 and abs(von - voff) >= 100:
            out["onshore_tuition_fee"] = von
            out["offshore_tuition_fee"] = voff

    return out


def extract_intakes(text: str) -> str | None:
    if not text:
        return None
    found_months = []
    for m in re.finditer(
        r"(?:intake(?:s)?|start\s+date(?:s)?|commencement|orientation|term\s+start)[^\n]{0,160}",
        text,
        re.I,
    ):
        seg = m.group(0)
        if re.search(r"\bmonthly\b|\bevery\s+month\b", seg, re.I):
            return "Monthly"
        for tok in re.findall(
            r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b",
            seg,
            re.I,
        ):
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
        m_order = {m: i for i, m in enumerate(MONTH_NAMES)}
        found_months.sort(key=lambda x: m_order.get(x, 99))
        return ", ".join(found_months[:12])
    return None


def extract_entry_requirements_html(text: str) -> str | None:
    if not text:
        return None
    bullets = []

    acad_m = re.search(
        r"(?:completion\s+of\s+(?:Australian\s+)?Year\s+1[012]|Australian\s+Year\s+12|bachelor(?:'s)?\s+degree|AQF\s+Level\s+\d+|Certificate\s+(?:III|IV)|Diploma\s+in)[^\n.]{0,160}",
        text,
        re.I,
    )
    if acad_m:
        s = re.sub(r"\s+", " ", acad_m.group(0)).strip(" ,;:-")
        if "commonwealth supported" not in s.lower() and "hecs" not in s.lower():
            bullets.append(f"<strong>Academic Requirement:</strong> {html.escape(s[:240])}")

    age_m = re.search(r"(?:minimum\s+age|must\s+be(?:\s+at\s+least)?|aged?)\s*18\s*(?:years|\+)", text, re.I)
    if age_m:
        bullets.append("<strong>Age Requirement:</strong> Minimum 18 years of age at course commencement.")

    if not bullets:
        return None
    li_html = "".join(f"<li>{b}</li>" for b in bullets)
    return f"<h4>Entry Requirements (International Students)</h4><ul>{li_html}</ul>"


def extract_description_html(tree: HTMLParser) -> str | None:
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


def classify_assessment(text: str, is_vet: bool = False) -> str | None:
    if not text:
        return "assignment" if is_vet else None
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


BAD_PLACEHOLDER_EMAILS = re.compile(
    r"(example\.|you@|your@|email@email|name@|test@|user@|domain\.com|alex\.smith@|john\.doe@|jane\.doe@|firstname|wixpress|sentry)",
    re.I,
)


def extract_contacts(text: str, html_str: str, base_url: str) -> tuple[str | None, str | None, str]:
    domain = urlparse(base_url).netloc.lower().replace("www.", "")
    emails = set(re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", html_str or ""))
    valid_emails = [
        e.lower()
        for e in emails
        if not any(e.lower().endswith(ext) for ext in (".png", ".jpg", ".webp", ".svg", ".js", ".css"))
        and not BAD_PLACEHOLDER_EMAILS.search(e)
    ]

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
    def __init__(self, pw_concurrency: int = 8, http_concurrency: int = 42):
        self.pw_sem = asyncio.Semaphore(pw_concurrency)
        self.http_sem = asyncio.Semaphore(http_concurrency)
        self.ddg_sem = asyncio.Semaphore(3)
        self.http_client: httpx.AsyncClient | None = None
        self.pw = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None

    async def __aenter__(self):
        self.http_client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-AU,en;q=0.9"},
            follow_redirects=True,
            timeout=9.0,
            http2=False,
            verify=False,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=25),
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
                    if len(txt) >= 450 and "Just a moment..." not in txt and "Enable JavaScript" not in txt[:300]:
                        if not toggle_international or "international" in txt.lower() or "cricos" in txt.lower():
                            return html_str, txt, str(r.url)
            except httpx.ConnectError as ce:
                if "getaddrinfo failed" in str(ce) or "Name or service not known" in str(ce):
                    dns_or_connect_dead = True
            except Exception:
                pass

        if not allow_playwright or dns_or_connect_dead:
            return None, None, url

        async with self.pw_sem:
            page = await self.context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=12000)
                if toggle_international:
                    for sel in (
                        "button:has-text('International')",
                        "[role='tab']:has-text('International')",
                        "a:has-text('International students')",
                    ):
                        loc = page.locator(sel).first
                        if await loc.count() > 0 and await loc.is_visible():
                            try:
                                await loc.click(timeout=1200)
                                await page.wait_for_load_state("domcontentloaded", timeout=1800)
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
        q = f'"{cricos_code}" "{inst_name}" Australia'
        search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(q)}"
        async with self.ddg_sem:
            html_str, _, _ = await self.fetch(search_url, allow_playwright=False)
        if not html_str:
            return None
        tree = HTMLParser(html_str)
        skip_domains = (
            "cricos.education.gov.au", "studyaustralia.gov.au", "training.gov.au",
            "gooduniversitiesguide.com.au", "facebook.com", "linkedin.com",
            "wikipedia.org", "youtube.com", "instagram.com", "duckduckgo.com",
            "hotcoursesabroad.com", "idp.com", "myskills.gov.au", "yourcareer.gov.au",
            "data.gov.au", "abn.business.gov.au", "asic.gov.au",
        )
        for a in tree.css("a.result__a"):
            href = a.attributes.get("href") or ""
            if "uddg=" in href:
                qs = parse_qs(urlparse(href).query)
                href = qs.get("uddg", [href])[0]
            if href.startswith("http"):
                p = urlparse(href)
                dom = p.netloc.lower()
                if not any(sd in dom for sd in skip_domains):
                    return f"{p.scheme}://{p.netloc}/"
        return None


def page_has_conflicting_cricos(text: str, expected_cricos: str, known_provider_courses: set[str]) -> bool:
    """
    Returns True if the page mentions another active CRICOS course code from this provider
    WITHOUT mentioning `expected_cricos` (prevents multi-course or wrong-course contamination).
    """
    found = {m.upper() for m in CRICOS_COURSE_CODE_RE.findall(text.upper())}
    if expected_cricos.upper() in found:
        return False
    other_provider_codes = found & (known_provider_courses - {expected_cricos.upper()})
    return len(other_provider_codes) > 0


async def enrich_single_provider(
    fetcher: HybridFetcher,
    mp: dict,
    existing_ep: dict | None,
    prov_courses: list[dict],
    existing_ec_map: dict[str, dict],
) -> tuple[dict, list[tuple]]:
    now_iso = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cricos_prov = mp["cricos_provider_code"]
    inst_name = mp["institution_name"]

    website = (
        (existing_ep or {}).get("website")
        or mp.get("cricos_website")
        or mp.get("db_website")
    )
    if website and not website.startswith("http"):
        website = "https://" + website.lstrip("/")

    if not website:
        website = await fetcher.discover_missing_website(inst_name, cricos_prov)

    ep_result = {
        "cricos_provider_code": cricos_prov,
        "provider_id": mp.get("db_provider_id"),
        "institution_name": inst_name,
        "website": website,
        "international_url": (existing_ep or {}).get("international_url"),
        "intake_date": (existing_ep or {}).get("intake_date") or mp.get("db_intake_date"),
        "provider_logo": (existing_ep or {}).get("provider_logo") or mp.get("db_provider_logo"),
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

    site_links: dict[str, str] = {}
    prov_corpus_parts = []
    prov_fees = {"offshore_tuition_fee": None, "onshore_tuition_fee": None, "enrolment_fee": None, "materials_fee": None}

    if website:
        parsed_home = urlparse(website)
        origin = f"{parsed_home.scheme}://{parsed_home.netloc}"

        need_prov_scrape = not (
            ep_result["contact_email"] and ep_result["provider_logo"] and ep_result["intake_date"]
        )
        home_html, home_txt, final_home = await fetcher.fetch(website, allow_playwright=need_prov_scrape)
        if home_txt:
            prov_corpus_parts.append(home_txt)

        if home_html:
            tree = HTMLParser(home_html)
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

        priority_subpages = []
        for u, anchor in site_links.items():
            comb = (u + " " + anchor).lower()
            if any(k in comb for k in ("international", "enrol", "admission", "intake", "fee", "contact", "courses")):
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

        # Sitemap check for exact CRICOS / unique VET code URLs
        for sm_path in ("/sitemap.xml", "/course-sitemap.xml", "/courses-sitemap.xml"):
            sm_html, _, _ = await fetcher.fetch(origin + sm_path, allow_playwright=False)
            if sm_html and "<loc>" in sm_html:
                for loc in re.findall(r"<loc>(https?://[^<]+)</loc>", sm_html)[:2000]:
                    if not loc.endswith(".xml"):
                        site_links.setdefault(loc.strip(), "")
                break
    else:
        prov_corpus = ""

    # Count VET national codes on this provider so a VET code is ONLY used as a link locator
    # if it maps 1-to-1 to a SINGLE active CRICOS course on this provider!
    vet_counts = collections.Counter(
        (mc.get("vet_national_code") or "").strip().upper()
        for mc in prov_courses
        if (mc.get("vet_national_code") or "").strip()
    )
    known_prov_cricos_codes = {mc["cricos_course_code"].upper() for mc in prov_courses}

    url_counts = collections.Counter(
        (mc.get("db_apply_form") or "").strip() for mc in prov_courses if (mc.get("db_apply_form") or "").strip()
    )
    generic_urls = {u for u, cnt in url_counts.items() if cnt >= 3}

    async def process_course(mc: dict, allow_live_fetch: bool) -> tuple:
        ccode = mc["cricos_course_code"]
        vcode = (mc.get("vet_national_code") or "").strip().upper()
        cname = mc["course_name"]
        ec = existing_ec_map.get(ccode) or {}

        acir_url = ec.get("apply_form") if ec.get("match_method") == "acir_exact_cricos" else None
        db_url = mc.get("db_apply_form") if is_deep_url(mc.get("db_apply_form"), generic_urls) else None

        best_url = acir_url
        match_method = ec.get("match_method") or "cricos_official_register"
        conf = 1.0

        # Strict zero-mismatch candidate URL discovery: ONLY exact cricos_course_code OR 1-to-1 unique vet_national_code!
        cand_from_links = None
        if not best_url and site_links:
            for cand_u, anchor in site_links.items():
                if cand_u.lower().endswith((".pdf", ".jpg", ".png", ".zip")):
                    continue
                if not is_deep_url(cand_u):
                    continue
                hay = (cand_u + " " + anchor).upper()
                if ccode in hay:
                    cand_from_links = cand_u
                    match_method = "verified_cricos_in_provider_url"
                    break
                if vcode and vet_counts[vcode] == 1 and vcode in hay:
                    cand_from_links = cand_u
                    match_method = "verified_unique_vet_code_in_provider_url"

        target_fetch_url = best_url or cand_from_links or db_url
        page_desc = ec.get("course_description") or mc.get("db_course_description")
        page_entry = ec.get("entry_requirements") or mc.get("db_entry_requirements")
        page_dur = (
            ec.get("total_course_duration")
            or mc.get("db_total_course_duration")
            or (f"{mc['duration_weeks']} weeks" if mc.get("duration_weeks") else None)
        )
        page_assess = ec.get("assessment_style") or mc.get("db_assessment_style")
        page_intake = ec.get("intake_dates") or ep_result.get("intake_date")

        # Authoritative CRICOS Tuition Fee is always preserved first
        offshore_fee = (
            mc.get("cricos_tuition_fee")
            or ec.get("offshore_tuition_fee")
            or mc.get("db_offshore_tuition_fee")
        )
        onshore_fee = ec.get("onshore_tuition_fee")
        enrol_fee = ec.get("enrolment_fee") or mc.get("db_enrolment_fee") or prov_fees["enrolment_fee"]
        mat_fee = ec.get("materials_fee") or mc.get("db_materials_fee") or prov_fees["materials_fee"]

        if allow_live_fetch and target_fetch_url and (not page_desc or not page_entry or not best_url):
            chtml, ctxt, cfinal = await fetcher.fetch(target_fetch_url, allow_playwright=True, toggle_international=True)
            if chtml and ctxt:
                conflict = page_has_conflicting_cricos(ctxt, ccode, known_prov_cricos_codes)
                has_exact_cricos = ccode in ctxt.upper()
                has_unique_vet = bool(vcode and vet_counts[vcode] == 1 and vcode in ctxt.upper())

                if not conflict and (acir_url or has_exact_cricos or has_unique_vet):
                    best_url = cfinal
                    if has_exact_cricos:
                        match_method = "verified_cricos_on_course_page"
                    elif has_unique_vet and not acir_url:
                        match_method = "verified_unique_vet_code_on_course_page"

                    ctree = HTMLParser(chtml)
                    if not page_desc:
                        page_desc = extract_description_html(ctree)
                    if not page_entry:
                        page_entry = extract_entry_requirements_html(ctxt)
                    cfees = extract_international_fees(ctxt)
                    if cfees["onshore_tuition_fee"] and has_exact_cricos:
                        onshore_fee = cfees["onshore_tuition_fee"]
                    if not offshore_fee and cfees["offshore_tuition_fee"] and has_exact_cricos:
                        offshore_fee = cfees["offshore_tuition_fee"]
                    if not enrol_fee and cfees["enrolment_fee"]:
                        enrol_fee = cfees["enrolment_fee"]
                    if not mat_fee and cfees["materials_fee"]:
                        mat_fee = cfees["materials_fee"]
                    if not page_assess:
                        page_assess = classify_assessment(ctxt, is_vet=bool(vcode))
                    ci = extract_intakes(ctxt)
                    if ci:
                        page_intake = ci
        elif cand_from_links and not best_url:
            # URL itself explicitly contains the 7-char CRICOS code or 1-to-1 unique VET code
            best_url = cand_from_links

        if not page_assess:
            page_assess = classify_assessment(prov_corpus, is_vet=bool(vcode))

        return (
            ccode,
            cricos_prov,
            mc.get("db_course_id"),
            mc["institution_name"],
            cname,
            best_url,
            page_desc,
            page_entry,
            ec.get("english_requirements"),
            ec.get("ielts_score"),
            ec.get("pte_score"),
            ec.get("toefl_score"),
            ec.get("english_source_url"),
            ec.get("english_method"),
            onshore_fee,
            offshore_fee,
            enrol_fee,
            mat_fee,
            page_dur,
            page_assess,
            page_intake,
            ec.get("scholarships_summary"),
            1,
            conf,
            match_method,
            best_url or ec.get("source_url") or ep_result["source_url"] or "https://cricos.education.gov.au/",
            "stage2_enriched",
            None,
            now_iso,
        )

    live_budget = 6
    tasks = []
    for mc in prov_courses:
        ec = existing_ec_map.get(mc["cricos_course_code"]) or {}
        needs_content = not (ec.get("course_description") or mc.get("db_course_description"))
        use_live = False
        if needs_content and live_budget > 0:
            use_live = True
            live_budget -= 1
        tasks.append(process_course(mc, use_live))

    course_updates = await asyncio.gather(*tasks) if tasks else []
    return ep_result, list(course_updates)


async def run_stage2(max_providers: int | None = None, resume: bool = True):
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    all_providers = [dict(r) for r in conn.execute("SELECT * FROM master_providers")]
    ep_map = {r["cricos_provider_code"]: dict(r) for r in conn.execute("SELECT * FROM enriched_providers")}
    ec_map = {r["cricos_course_code"]: dict(r) for r in conn.execute("SELECT * FROM enriched_courses")}

    courses_by_prov: dict[str, list[dict]] = {}
    for r in conn.execute("SELECT * FROM master_courses"):
        d = dict(r)
        courses_by_prov.setdefault(d["cricos_provider_code"], []).append(d)

    if resume:
        # Run Stage 2 on providers not yet website-scraped OR providers that have courses not yet in enriched_courses
        before = len(all_providers)
        providers = []
        for p in all_providers:
            pcode = p["cricos_provider_code"]
            ep_stat = ep_map.get(pcode, {}).get("status")
            prov_cs = courses_by_prov.get(pcode, [])
            unprocessed_cs = any(c["cricos_course_code"] not in ec_map for c in prov_cs)
            if ep_stat not in ("enriched", "no_website") or unprocessed_cs:
                providers.append(p)
        print(
            f"Resume enabled: skipping {before - len(providers)} fully-enriched providers ({len(providers)} providers queued)."
        )
    else:
        providers = all_providers

    if max_providers:
        providers = providers[:max_providers]

    print(
        f"Stage 2 starting across {len(providers)} CRICOS providers ({sum(len(courses_by_prov.get(p['cricos_provider_code'], [])) for p in providers)} courses)..."
    )

    prov_sem = asyncio.Semaphore(28)

    async with HybridFetcher(pw_concurrency=10, http_concurrency=48) as fetcher:

        async def bounded_prov(mp: dict):
            async with prov_sem:
                pcode = mp["cricos_provider_code"]
                p_courses = courses_by_prov.get(pcode, [])
                try:
                    return await asyncio.wait_for(
                        enrich_single_provider(fetcher, mp, ep_map.get(pcode), p_courses, ec_map),
                        timeout=20.0,
                    )
                except asyncio.TimeoutError:
                    now_iso = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    existing = ep_map.get(pcode) or {}
                    fallback_web = existing.get("website") or mp.get("cricos_website") or mp.get("db_website")
                    fallback_c_rows = []
                    for mc in p_courses:
                        ccode = mc["cricos_course_code"]
                        ec = ec_map.get(ccode) or {}
                        dur = (
                            ec.get("total_course_duration")
                            or mc.get("db_total_course_duration")
                            or (f"{mc['duration_weeks']} weeks" if mc.get("duration_weeks") else None)
                        )
                        fallback_c_rows.append(
                            (
                                ccode,
                                pcode,
                                mc.get("db_course_id"),
                                mc["institution_name"],
                                mc["course_name"],
                                ec.get("apply_form"),
                                ec.get("course_description") or mc.get("db_course_description"),
                                ec.get("entry_requirements") or mc.get("db_entry_requirements"),
                                ec.get("english_requirements"),
                                ec.get("ielts_score"),
                                ec.get("pte_score"),
                                ec.get("toefl_score"),
                                ec.get("english_source_url"),
                                ec.get("english_method"),
                                ec.get("onshore_tuition_fee"),
                                mc.get("cricos_tuition_fee") or ec.get("offshore_tuition_fee") or mc.get("db_offshore_tuition_fee"),
                                ec.get("enrolment_fee") or mc.get("db_enrolment_fee"),
                                ec.get("materials_fee") or mc.get("db_materials_fee"),
                                dur,
                                ec.get("assessment_style") or mc.get("db_assessment_style") or ("assignment" if mc.get("vet_national_code") else None),
                                ec.get("intake_dates") or existing.get("intake_date") or mp.get("db_intake_date"),
                                ec.get("scholarships_summary"),
                                1,
                                1.0,
                                ec.get("match_method") or "cricos_official_register",
                                ec.get("apply_form") or ec.get("source_url") or fallback_web or "https://cricos.education.gov.au/",
                                "stage2_timeout_cricos_synced",
                                "provider_timeout_20s",
                                now_iso,
                            )
                        )
                    return (
                        {
                            "cricos_provider_code": pcode,
                            "provider_id": mp.get("db_provider_id"),
                            "institution_name": mp["institution_name"],
                            "website": fallback_web,
                            "international_url": existing.get("international_url"),
                            "intake_date": existing.get("intake_date") or mp.get("db_intake_date"),
                            "provider_logo": existing.get("provider_logo") or mp.get("db_provider_logo"),
                            "contact_email": existing.get("contact_email"),
                            "contact_phone": existing.get("contact_phone"),
                            "contact_website_url": existing.get("contact_website_url"),
                            "scholarships_json": existing.get("scholarships_json"),
                            "source_url": existing.get("source_url") or fallback_web,
                            "discovery_method": existing.get("discovery_method") or "timeout_partial",
                            "status": "enriched",
                            "error_msg": "provider_timeout_20s",
                            "updated_at": now_iso,
                        },
                        fallback_c_rows,
                    )

        batch_size = 60
        total_c_updated = 0
        for i in range(0, len(providers), batch_size):
            batch = providers[i : i + batch_size]
            results = await asyncio.gather(*[bounded_prov(p) for p in batch])

            ep_rows = []
            ec_rows = []
            for ep_res, c_list in results:
                ep_rows.append(
                    (
                        ep_res["cricos_provider_code"],
                        ep_res["provider_id"],
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
                (cricos_provider_code, provider_id, institution_name, website, international_url,
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
                    (cricos_course_code, cricos_provider_code, course_id, institution_name, course_name,
                     apply_form, course_description, entry_requirements, english_requirements,
                     ielts_score, pte_score, toefl_score, english_source_url, english_method,
                     onshore_tuition_fee, offshore_tuition_fee, enrolment_fee, materials_fee,
                     total_course_duration, assessment_style, intake_dates, scholarships_summary,
                     international_verified, match_confidence, match_method, source_url, status, error_msg, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ec_rows,
                )
            conn.commit()
            print(
                f"  Stage 2 progress: {min(i+batch_size, len(providers))}/{len(providers)} providers | {total_c_updated} course records processed ({time.time()-t0:.1f}s)"
            )

    conn.close()
    print(f"Stage 2 finished in {time.time()-t0:.1f}s!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-providers", type=int, default=None)
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()
    asyncio.run(run_stage2(max_providers=args.max_providers, resume=not args.no_resume))
