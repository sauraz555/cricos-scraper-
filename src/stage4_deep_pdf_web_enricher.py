"""
Stage 4: Deep Web, WordPress REST API, Recursive Sitemap, Official PDF Prospectus &
National Training Register (`training.gov.au`) Enricher.

Fills remaining NULL gaps in `entry_requirements`, `english_requirements`,
`course_description`, `apply_form`, `intake_dates`, and provider `website` / `contact_email`
across all 1,543 CRICOS providers and 25,199 active CRICOS courses while strictly preserving:
  1. Official CRICOS Unique IDs (`cricos_provider_code` & `cricos_course_code`)
  2. Zero Mismatch (0 fuzzy title matching — exact 7-char CRICOS code, 1-to-1 unique VET code,
     exact `training.gov.au` national code, or level-aware official provider policy)
  3. 100% International Student Purity (sanitized through `verify_and_sanitize` filters)
"""
import asyncio
import collections
import datetime as dt
import html
import os
import re
import sqlite3
import sys
import time
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import httpx
import pymupdf
from playwright.async_api import Browser, BrowserContext, async_playwright
from selectolax.parser import HTMLParser

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from stage2_playwright_enricher import (
    CRICOS_COURSE_CODE_RE,
    extract_contacts,
    extract_description_html,
    extract_intakes,
    extract_international_fees,
    is_deep_url,
    page_has_conflicting_cricos,
)
from stage3_entry_english_enricher import (
    bucket_for_course,
    build_english_html,
    combine_entry_and_english_html,
    ensure_english_columns,
    extract_academic_entry_html,
    extract_english_tests,
    parse_html_english_tables,
    parse_policy_page_by_level,
)
from verify_and_sanitize import (
    BAD_EMAIL_PATTERNS,
    clean_single_test_str,
    rebuild_english_html,
    sanitize_course_url,
    sanitize_desc_html,
    sanitize_entry_html,
    sanitize_url,
)

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_DIR, "data", "dashboard_mirror.sqlite")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}

CRICOS_PROV_CODE_RE = re.compile(r"\b(0\d{4}[A-Z0-9])\b")

FREEMAIL_DOMAINS = {
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "yahoo.com.au",
    "icloud.com", "live.com", "live.com.au", "bigpond.com", "bigpond.net.au",
    "optusnet.com.au", "msn.com", "aol.com", "protonmail.com", "mail.com",
    "qq.com", "163.com", "126.com", "xtra.co.nz",
}

MAX_PDF_BYTES = 14 * 1024 * 1024  # Cap individual PDF downloads at 14 MB


def norm_org_name(s: str | None) -> str:
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"\b(pty\.?\s*ltd\.?|limited|inc\.?|incorporated|australia|the|trustee\s+for.*)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_pdf_or_text_description(text: str, course_name: str = "") -> str | None:
    if not text:
        return None
    paras = []
    for blk in re.split(r"\n{2,}", text):
        t = re.sub(r"\s+", " ", blk).strip()
        if len(t) < 75 or len(t) > 1000:
            continue
        tl = t.lower()
        if any(
            bad in tl
            for bad in (
                "cookie", "privacy policy", "copyright", "all rights reserved",
                "cricos provider", "rto code", "disclaimer", "table of contents",
                "refund policy", "overseas student health cover", "oshc",
                "commonwealth supported", "hecs-help", "vet student loan",
            )
        ):
            continue
        # Must read like prose sentences rather than a table of unit codes
        if len(re.findall(r"\b[A-Z]{3,4}\d{3,5}[A-Z]?\b", t)) >= 4:
            continue
        if "." not in t:
            continue
        if t not in paras:
            paras.append(t)
        if len(paras) >= 2:
            break
    if not paras:
        return None
    raw_html = "<h4>Course Information</h4> " + " ".join(f"<p>{html.escape(p)}</p>" for p in paras)
    return sanitize_desc_html(raw_html)


def format_tga_description(raw_html: str | None) -> str | None:
    if not raw_html:
        return None
    # Strip custom <ntr-tcref ...>CODE</ntr-tcref> tags while preserving inner text
    cleaned = re.sub(r"</?ntr-tcref[^>]*>", "", raw_html, flags=re.I)
    tree = HTMLParser(cleaned)
    paras = []
    for p in tree.css("p"):
        txt = re.sub(r"\s+", " ", p.text(separator=" ", strip=True)).strip()
        if len(txt) < 35:
            continue
        low = txt.lower()
        if low.startswith(("licensing/regulatory", "no licensing, legislative", "no occupational licensing")):
            continue
        if txt not in paras:
            paras.append(txt)
        if len(paras) >= 4:
            break
    if not paras:
        return None
    out = "<h4>Course Information</h4> " + " ".join(f"<p>{html.escape(p)}</p>" for p in paras)
    return sanitize_desc_html(out)


def format_tga_entry_requirements(raw_html: str | None) -> str | None:
    if not raw_html:
        return None
    cleaned = re.sub(r"</?ntr-tcref[^>]*>", "", raw_html, flags=re.I)
    tree = HTMLParser(cleaned)
    plain = re.sub(r"\s+", " ", tree.text(separator=" ", strip=True)).strip()
    low = plain.lower().strip(" .:")
    if not low or low in ("nil", "none", "n/a", "not applicable") or low.startswith(
        ("there are no entry requirements", "no entry requirements", "nil ")
    ):
        return None
    bullets = []
    for node in tree.css("li, p"):
        if node.tag == "p" and node.css("li"):
            continue
        t = re.sub(r"\s+", " ", node.text(separator=" ", strip=True)).strip()
        if len(t) < 18 or t.lower() in ("or", "and", "nil", "entry to this qualification is limited to those who:", "entry to this qualification is open to individuals who:"):
            continue
        bullets.append(f"<li>{html.escape(t)}</li>")
    if not bullets and len(plain) >= 25:
        bullets.append(f"<li>{html.escape(plain)}</li>")
    if not bullets:
        return None
    cand = f"<h4>Academic Entry Requirements (International Students)</h4><ul>{''.join(bullets[:5])}</ul>"
    return sanitize_entry_html(cand, None)


# ==============================================================================
# PHASE 4A: Provider Website Verification & TGA Official Domain Discovery
# ==============================================================================
async def phase_4a_providers(conn: sqlite3.Connection):
    t0 = time.time()
    print("\n[Stage 4A] Verifying provider websites & discovering missing RTO websites via training.gov.au...")
    mp_rows = {r["cricos_provider_code"]: dict(r) for r in conn.execute("SELECT * FROM master_providers")}
    ep_rows = {r["cricos_provider_code"]: dict(r) for r in conn.execute("SELECT * FROM enriched_providers")}
    all_prov_codes = set(mp_rows.keys())
    now_iso = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. Prefer official cricos_website root domain over legacy subdomain mismatches (e.g. SCU hotelschool.scu.edu.au -> www.scu.edu.au)
    fixed_cricos_web = 0
    for pcode, mp in mp_rows.items():
        cr_w = (mp.get("cricos_website") or "").strip()
        ep = ep_rows.get(pcode, {})
        ep_w = (ep.get("website") or "").strip()
        if cr_w:
            if not cr_w.startswith("http"):
                cr_w = "https://" + cr_w.lstrip("/")
            cr_clean = sanitize_url(cr_w, allow_combined_dom_intl_path=True)
            if cr_clean:
                cr_dom = urlparse(cr_clean).netloc.lower().replace("www.", "")
                ep_dom = urlparse(ep_w).netloc.lower().replace("www.", "") if ep_w else ""
                if not ep_w or (cr_dom and ep_dom and ep_dom != cr_dom and ep_dom.endswith("." + cr_dom)):
                    conn.execute(
                        "UPDATE enriched_providers SET website = ?, status = 'enriched', updated_at = ? WHERE cricos_provider_code = ?",
                        (cr_clean, now_iso, pcode),
                    )
                    fixed_cricos_web += 1
    conn.commit()
    if fixed_cricos_web:
        print(f"  Aligned {fixed_cricos_web} provider websites to official root CRICOS website.")

    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=9.0,
        verify=False,
        limits=httpx.Limits(max_connections=45, max_keepalive_connections=20),
    ) as client:
        sem = asyncio.Semaphore(25)

        # 2. Audit DDG-discovered websites (where both cricos_website & db_website were empty) for conflicting CRICOS provider codes
        ddg_provs = [
            pcode
            for pcode, mp in mp_rows.items()
            if not (mp.get("cricos_website") or "").strip()
            and not (mp.get("db_website") or "").strip()
            and (ep_rows.get(pcode, {}).get("website") or "").strip()
        ]

        async def audit_ddg_web(pcode: str) -> str | None:
            w = ep_rows[pcode]["website"]
            async with sem:
                try:
                    r = await client.get(w)
                    if r.status_code >= 400:
                        return pcode
                    found_pcodes = set(CRICOS_PROV_CODE_RE.findall(r.text.upper()))
                    other_known = (found_pcodes & all_prov_codes) - {pcode}
                    if other_known and pcode not in found_pcodes:
                        return pcode
                except Exception:
                    pass
            return None

        if ddg_provs:
            bad_ddg = [p for p in await asyncio.gather(*[audit_ddg_web(pc) for pc in ddg_provs]) if p]
            if bad_ddg:
                for pcode in bad_ddg:
                    conn.execute(
                        "UPDATE enriched_providers SET website = NULL, international_url = NULL, contact_website_url = NULL, status = 'no_website' WHERE cricos_provider_code = ?",
                        (pcode,),
                    )
                    # Clear any unverified course URLs from that mismatched domain
                    conn.execute(
                        "UPDATE enriched_courses SET apply_form = NULL, match_method = 'cricos_official_register' WHERE cricos_provider_code = ? AND match_method != 'acir_exact_cricos'",
                        (pcode,),
                    )
                conn.commit()
                print(f"  Cleared {len(bad_ddg)} DDG provider websites that displayed a conflicting CRICOS provider code: {bad_ddg[:10]}")

        # Refresh ep_rows
        ep_rows = {r["cricos_provider_code"]: dict(r) for r in conn.execute("SELECT * FROM enriched_providers")}
        missing_web_provs = [
            pcode for pcode, ep in ep_rows.items() if not (ep.get("website") or "").strip()
        ]
        print(f"  Querying official training.gov.au RTO directory for {len(missing_web_provs)} providers missing websites...")

        async def discover_via_tga(pcode: str):
            mp = mp_rows[pcode]
            ep = ep_rows[pcode]
            search_names = []
            for raw_n in (mp.get("trading_name"), mp.get("institution_name")):
                if raw_n:
                    for part in str(raw_n).split(","):
                        p_clean = part.strip()
                        if p_clean and p_clean not in search_names:
                            search_names.append(p_clean)

            target_norms = {norm_org_name(n) for n in search_names if norm_org_name(n)}
            cand_urls = []
            best_email = ep.get("contact_email")
            best_phone = ep.get("contact_phone")

            async with sem:
                for sname in search_names[:3]:
                    try:
                        r = await client.get(
                            f"https://training.gov.au/api/search/organisation?api-version=1.0&searchText={quote_plus(sname)}&offset=0&pageSize=5"
                        )
                        if r.status_code != 200:
                            continue
                        items = r.json().get("data") or []
                        for it in items[:3]:
                            rcode = it.get("code")
                            if not rcode:
                                continue
                            det_r = await client.get(
                                f"https://training.gov.au/api/organisation/{rcode}?api-version=1.0&include=all"
                            )
                            if det_r.status_code != 200:
                                continue
                            det = det_r.json()
                            det_names = set()
                            for ln in det.get("legalNames") or []:
                                if ln.get("name"):
                                    det_names.add(norm_org_name(ln["name"]))
                            for tn in det.get("tradingNames") or []:
                                if tn.get("name"):
                                    det_names.add(norm_org_name(tn["name"]))
                            if not (det_names & target_norms):
                                continue

                            # Exact legal/trading name match confirmed on official TGA register!
                            for wobj in det.get("webAddresses") or []:
                                if not wobj.get("endDate") and wobj.get("webAddress"):
                                    wu = wobj["webAddress"].strip()
                                    if not wu.startswith("http"):
                                        wu = "https://" + wu.lstrip("/")
                                    cand_urls.append(wu)

                            for cobj in det.get("contacts") or []:
                                em = (cobj.get("email") or "").strip().lower()
                                ph = (cobj.get("phone") or cobj.get("mobile") or "").strip()
                                if ph and not best_phone:
                                    best_phone = ph
                                if em and "@" in em:
                                    dom = em.split("@", 1)[1]
                                    if dom not in FREEMAIL_DOMAINS:
                                        cand_urls.append(f"https://{dom}/")
                                        cand_urls.append(f"https://www.{dom}/")
                                        if not BAD_EMAIL_PATTERNS.search(em):
                                            if not best_email or any(k in em for k in ("info@", "admin@", "enquir", "admission", "international")):
                                                best_email = em
                            break
                        if cand_urls:
                            break
                    except Exception:
                        continue

                # Verify candidate URL resolves & has zero conflicting CRICOS provider codes
                verified_web = None
                seen_u = set()
                for cu in cand_urls:
                    cu_clean = sanitize_url(cu, allow_combined_dom_intl_path=True)
                    if not cu_clean or cu_clean in seen_u:
                        continue
                    seen_u.add(cu_clean)
                    try:
                        resp = await client.get(cu_clean)
                        if resp.status_code == 200 and len(resp.text) > 250:
                            found_pcodes = set(CRICOS_PROV_CODE_RE.findall(resp.text.upper()))
                            other_known = (found_pcodes & all_prov_codes) - {pcode}
                            if other_known and pcode not in found_pcodes:
                                continue
                            final_clean = sanitize_url(str(resp.url), allow_combined_dom_intl_path=True)
                            if final_clean:
                                verified_web = final_clean
                                h_em, h_ph, _ = extract_contacts(resp.text, resp.text, final_clean)
                                if h_em and not BAD_EMAIL_PATTERNS.search(h_em):
                                    best_email = h_em
                                if h_ph and not best_phone:
                                    best_phone = h_ph
                                break
                    except Exception:
                        continue

                if verified_web or (best_email and best_email != ep.get("contact_email")):
                    return (
                        verified_web,
                        best_email if (best_email and not BAD_EMAIL_PATTERNS.search(best_email)) else None,
                        best_phone,
                        verified_web or ep.get("contact_website_url"),
                        verified_web or ep.get("source_url"),
                        "tga_official_rto_verified" if verified_web else ep.get("discovery_method"),
                        "enriched" if verified_web else ep.get("status"),
                        now_iso,
                        pcode,
                    )
            return None

        tga_results = [r for r in await asyncio.gather(*[discover_via_tga(pc) for pc in missing_web_provs]) if r]
        if tga_results:
            conn.executemany(
                """
                UPDATE enriched_providers
                SET website = COALESCE(?, website),
                    contact_email = COALESCE(?, contact_email),
                    contact_phone = COALESCE(?, contact_phone),
                    contact_website_url = COALESCE(?, contact_website_url),
                    source_url = COALESCE(?, source_url),
                    discovery_method = COALESCE(?, discovery_method),
                    status = COALESCE(?, status),
                    updated_at = ?
                WHERE cricos_provider_code = ?
                """,
                tga_results,
            )
            conn.commit()
        new_webs = sum(1 for r in tga_results if r[0])
        print(f"  [Stage 4A Complete] Discovered & live-verified {new_webs} new RTO websites ({time.time()-t0:.1f}s).")


# ==============================================================================
# PHASE 4B: Official National Training Register (`training.gov.au`) VET Harvester
# ==============================================================================
async def phase_4b_tga_vet(conn: sqlite3.Connection):
    t0 = time.time()
    rows = conn.execute(
        """
        SELECT mc.cricos_course_code, mc.vet_national_code,
               ec.course_description, ec.entry_requirements, ec.english_requirements, ec.source_url
        FROM master_courses mc
        JOIN enriched_courses ec ON mc.cricos_course_code = ec.cricos_course_code
        WHERE NULLIF(mc.vet_national_code, '') IS NOT NULL
          AND (NULLIF(ec.course_description, '') IS NULL OR NULLIF(ec.entry_requirements, '') IS NULL)
        """
    ).fetchall()

    distinct_vcodes = sorted({r["vet_national_code"].strip().upper() for r in rows if r["vet_national_code"]})
    print(f"\n[Stage 4B] Fetching {len(distinct_vcodes)} distinct National VET codes from official training.gov.au API...")
    if not distinct_vcodes:
        return

    vcode_data: dict[str, tuple[str | None, str | None]] = {}
    sem = asyncio.Semaphore(25)

    async with httpx.AsyncClient(
        headers={"User-Agent": HEADERS["User-Agent"], "Accept": "application/json"},
        follow_redirects=True,
        timeout=12.0,
        verify=False,
        limits=httpx.Limits(max_connections=35, max_keepalive_connections=20),
    ) as client:

        async def fetch_one_vcode(vcode: str):
            desc_html = None
            entry_html = None
            async with sem:
                try:
                    r1 = await client.get(f"https://training.gov.au/api/training/{vcode}?api-version=1.0&include=all")
                    if r1.status_code == 200:
                        d1 = r1.json()
                        if (d1.get("code") or "").upper() == vcode and d1.get("releases"):
                            rel_num = d1["releases"][0].get("releaseNumber", 1)
                            r2 = await client.get(
                                f"https://training.gov.au/api/training/{vcode}/releases/{rel_num}?include=All&api-version=1.0"
                            )
                            if r2.status_code == 200:
                                d2 = r2.json()
                                cbs = d2.get("contentBundles") or []
                                if cbs and cbs[0].get("id"):
                                    cb_r = await client.get(f"https://training.gov.au/api/content/bundle/{cbs[0]['id']}")
                                    if cb_r.status_code == 200:
                                        for item in cb_r.json().get("items") or []:
                                            ctype = item.get("contentType")
                                            if ctype == "Description" and not desc_html:
                                                desc_html = format_tga_description(item.get("content"))
                                            elif ctype == "EntryRequirements" and not entry_html:
                                                entry_html = format_tga_entry_requirements(item.get("content"))
                    if not desc_html:
                        yc_r = await client.get(f"https://api.yourcareer.gov.au/api/Courses/details/?CourseCode={vcode}")
                        if yc_r.status_code == 200:
                            yc = yc_r.json()
                            if (yc.get("courseCode") or "").upper() == vcode:
                                desc_html = format_tga_description(yc.get("courseDescription"))
                except Exception:
                    pass
            if desc_html or entry_html:
                vcode_data[vcode] = (desc_html, entry_html)

        await asyncio.gather(*[fetch_one_vcode(vc) for vc in distinct_vcodes])

    now_iso = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updates = []
    desc_filled = 0
    entry_filled = 0
    for r in rows:
        vc = r["vet_national_code"].strip().upper()
        if vc not in vcode_data:
            continue
        tga_desc, tga_entry = vcode_data[vc]
        cur_desc = (r["course_description"] or "").strip() or None
        cur_entry = (r["entry_requirements"] or "").strip() or None
        cur_eng = (r["english_requirements"] or "").strip() or None

        new_desc = cur_desc or tga_desc
        new_entry = cur_entry
        if not cur_entry and tga_entry:
            new_entry = sanitize_entry_html(tga_entry, cur_eng)
        elif cur_entry and tga_entry and "Academic Entry Requirements" not in cur_entry:
            new_entry = sanitize_entry_html(tga_entry, cur_eng) or cur_entry

        if new_desc != cur_desc:
            desc_filled += 1
        if new_entry != cur_entry:
            entry_filled += 1

        if new_desc != cur_desc or new_entry != cur_entry:
            src_u = r["source_url"]
            if not src_u or src_u == "https://cricos.education.gov.au/":
                src_u = f"https://training.gov.au/Training/Details/{vc}"
            updates.append((new_desc, new_entry, src_u, now_iso, r["cricos_course_code"]))

    if updates:
        conn.executemany(
            """
            UPDATE enriched_courses
            SET course_description = ?, entry_requirements = ?, source_url = ?, updated_at = ?
            WHERE cricos_course_code = ?
            """,
            updates,
        )
        conn.commit()
    print(
        f"  [Stage 4B Complete] Matched {len(vcode_data)}/{len(distinct_vcodes)} VET codes -> "
        f"+{desc_filled} descriptions, +{entry_filled} statutory entry requirements ({time.time()-t0:.1f}s)."
    )


# ==============================================================================
# SHARED DEEP FETCHER (HTTP + Playwright + PDF Stream Parser + Recursive Sitemap)
# ==============================================================================
class DeepEnricherEngine:
    def __init__(self, pw_concurrency: int = 10, http_concurrency: int = 55):
        self.pw_sem = asyncio.Semaphore(pw_concurrency)
        self.http_sem = asyncio.Semaphore(http_concurrency)
        self.search_sem = asyncio.Semaphore(4)
        self.client: httpx.AsyncClient | None = None
        self.pw = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None

    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            timeout=10.0,
            http2=False,
            verify=False,
            limits=httpx.Limits(max_connections=65, max_keepalive_connections=30),
        )
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            headless=True,
            args=["--disable-http2", "--disable-blink-features=AutomationControlled"],
        )
        self.context = await self.browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-AU",
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,
        )
        await self.context.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,eot,mp4,webm,mp3,avi}",
            lambda route: route.abort(),
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.client:
            await self.client.aclose()
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()

    async def fetch_html(
        self, url: str, allow_pw: bool = False, expand_tabs: bool = False, require_ielts_or_pw: bool = False
    ) -> tuple[str | None, str | None, str]:
        dead = False
        http_html = None
        http_txt = None
        http_final = url

        async with self.http_sem:
            try:
                r = await self.client.get(url)
                if r.status_code == 404:
                    return None, None, url
                ctype = r.headers.get("content-type", "text/html").lower()
                if r.status_code == 200 and ("html" in ctype or "xml" in ctype or "text" in ctype):
                    http_html = r.text
                    http_final = str(r.url)
                    tbl_txt = parse_html_english_tables(http_html)
                    tree = HTMLParser(http_html)
                    for tag in tree.css("script, style, noscript"):
                        tag.decompose()
                    http_txt = tree.body.text(separator="\n", strip=True) if tree.body else tree.text(separator="\n", strip=True)
                    if tbl_txt:
                        http_txt = tbl_txt + "\n" + http_txt
                    if len(http_txt) >= 450 and "Just a moment..." not in http_txt[:400]:
                        if not require_ielts_or_pw or re.search(r"\b(IELTS|PTE|TOEFL)\b", http_txt, re.I):
                            return http_html, http_txt, http_final
            except httpx.ConnectError as ce:
                if "getaddrinfo failed" in str(ce) or "Name or service not known" in str(ce):
                    dead = True
            except Exception:
                pass

        if not allow_pw or dead:
            return http_html, http_txt, http_final

        async with self.pw_sem:
            page = await self.context.new_page()
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=13000)
                if resp and resp.status == 404:
                    return None, None, url
                if expand_tabs:
                    for sel in (
                        "button:has-text('International')",
                        "[role='tab']:has-text('International')",
                        "a:has-text('International students')",
                        "button:has-text('Entry requirements')",
                        "button:has-text('Admission')",
                        "button:has-text('English')",
                        "summary:has-text('Entry')",
                        "summary:has-text('English')",
                    ):
                        loc = page.locator(sel).first
                        if await loc.count() > 0 and await loc.is_visible():
                            try:
                                await loc.click(timeout=900)
                            except Exception:
                                pass
                    await page.wait_for_timeout(350)
                h = await page.content()
                tbl_txt = parse_html_english_tables(h)
                txt = await page.inner_text("body")
                if tbl_txt:
                    txt = tbl_txt + "\n" + txt
                return h, txt, page.url
            except Exception:
                return http_html, http_txt, http_final
            finally:
                await page.close()

    async def fetch_pdf_pages(self, pdf_url: str) -> list[str]:
        """Streams a same-domain PDF up to MAX_PDF_BYTES and returns extracted text per page via MuPDF."""
        async with self.http_sem:
            try:
                async with self.client.stream("GET", pdf_url, timeout=14.0) as r:
                    if r.status_code != 200:
                        return []
                    cl = int(r.headers.get("content-length") or 0)
                    if cl > MAX_PDF_BYTES:
                        return []
                    chunks = []
                    total = 0
                    async for chunk in r.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_PDF_BYTES:
                            return []
                        chunks.append(chunk)
                    data = b"".join(chunks)
            except Exception:
                return []

        if not data or not data.startswith(b"%PDF"):
            return []
        try:
            doc = pymupdf.open(stream=data, filetype="pdf")
            pages = []
            max_p = min(len(doc), 120)
            for i in range(max_p):
                pages.append(doc[i].get_text("text") or "")
            doc.close()
            return pages
        except Exception:
            return []

    async def fetch_raw_text(self, url: str) -> str | None:
        async with self.http_sem:
            try:
                r = await self.client.get(url, timeout=8.0)
                if r.status_code == 200:
                    return r.text
            except Exception:
                pass
        return None

    async def discover_sitemaps_and_links(self, origin: str, domain: str) -> tuple[dict[str, str], list[str]]:
        """Returns (html_links_dict, pdf_urls_list) by expanding robots.txt + recursive XML sitemaps + homepage."""
        links: dict[str, str] = {}
        pdfs: list[str] = []

        # 1. Homepage + /international
        home_h, _, home_final = await self.fetch_html(origin + "/", allow_pw=False)
        if home_h:
            for a in HTMLParser(home_h).css("a[href]"):
                href = (a.attributes.get("href") or "").strip()
                if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                fu = urljoin(home_final, href)
                if domain in urlparse(fu).netloc.lower():
                    if fu.lower().split("?")[0].endswith(".pdf"):
                        pdfs.append(fu)
                    else:
                        links.setdefault(fu, a.text(strip=True)[:100])

        # 2. Check robots.txt + standard sitemap indexes
        sm_queue = []
        robots = await self.fetch_raw_text(origin + "/robots.txt")
        if robots:
            for sm in re.findall(r"Sitemap:\s*(https?://\S+)", robots, re.I):
                sm_queue.append(sm.strip())
        for std_sm in ("/sitemap.xml", "/wp-sitemap.xml", "/sitemap_index.xml", "/course-sitemap.xml", "/courses-sitemap.xml"):
            if origin + std_sm not in sm_queue:
                sm_queue.append(origin + std_sm)

        child_sitemaps = []
        for sm_u in sm_queue[:4]:
            xml = await self.fetch_raw_text(sm_u)
            if not xml or "<loc>" not in xml:
                continue
            locs = [x.strip() for x in re.findall(r"<loc>\s*(https?://[^<\s]+)\s*</loc>", xml)]
            xml_children = [x for x in locs if x.lower().split("?")[0].endswith(".xml")]
            non_xml = [x for x in locs if not x.lower().split("?")[0].endswith(".xml")]
            for u in non_xml[:3000]:
                if u.lower().split("?")[0].endswith(".pdf"):
                    pdfs.append(u)
                else:
                    links.setdefault(u, "")
            for cx in xml_children:
                child_sitemaps.append(cx)
            if non_xml or xml_children:
                break

        # Prioritize relevant child sitemaps (courses, programs, degrees, pages, study, international)
        if child_sitemaps:
            def rank_sm(u: str):
                ul = u.lower()
                if any(k in ul for k in ("course", "program", "degree", "qualification", "study")):
                    return 0
                if any(k in ul for k in ("page", "international", "admission", "future")):
                    return 1
                if any(k in ul for k in ("news", "post", "event", "blog", "staff", "author", "tag")):
                    return 9
                return 3

            child_sitemaps = sorted(list(dict.fromkeys(child_sitemaps)), key=rank_sm)
            good_children = [c for c in child_sitemaps if rank_sm(c) < 9][:6]
            if good_children:
                c_xmls = await asyncio.gather(*[self.fetch_raw_text(cu) for cu in good_children])
                for c_xml in c_xmls:
                    if not c_xml or "<loc>" not in c_xml:
                        continue
                    for loc in re.findall(r"<loc>\s*(https?://[^<\s]+)\s*</loc>", c_xml)[:2500]:
                        u = loc.strip()
                        if u.lower().split("?")[0].endswith(".pdf"):
                            pdfs.append(u)
                        elif not u.lower().split("?")[0].endswith(".xml"):
                            links.setdefault(u, "")

        return links, list(dict.fromkeys(pdfs))

    async def search_domain_policy_or_pdf(self, domain: str) -> tuple[list[str], list[str]]:
        """Bing + DuckDuckGo fallback to locate official English/Entry policy pages & PDF prospectuses on `domain`."""
        html_cands = []
        pdf_cands = []
        q = f'site:{domain} ("IELTS" OR "English language requirements" OR "International Student Handbook")'
        async with self.search_sem:
            # Try Bing first (less aggressive rate limiting than DDG)
            b_txt = await self.fetch_raw_text(f"https://www.bing.com/search?q={quote_plus(q)}")
            if b_txt:
                for a in HTMLParser(b_txt).css("li.b_algo h2 a[href]"):
                    href = (a.attributes.get("href") or "").strip()
                    if href.startswith("http") and domain in urlparse(href).netloc.lower():
                        if href.lower().split("?")[0].endswith(".pdf"):
                            pdf_cands.append(href)
                        else:
                            html_cands.append(href)
            if not html_cands and not pdf_cands:
                d_txt = await self.fetch_raw_text(f"https://html.duckduckgo.com/html/?q={quote_plus(q)}")
                if d_txt:
                    for a in HTMLParser(d_txt).css("a.result__a"):
                        href = a.attributes.get("href") or ""
                        if "uddg=" in href:
                            href = parse_qs(urlparse(href).query).get("uddg", [href])[0]
                        if href.startswith("http") and domain in urlparse(href).netloc.lower():
                            if href.lower().split("?")[0].endswith(".pdf"):
                                pdf_cands.append(href)
                            else:
                                html_cands.append(href)
        return html_cands, pdf_cands


# ==============================================================================
# PHASE 4C: Uncapped Verified `apply_form` Course Page Scraper
# ==============================================================================
async def phase_4c_verified_course_urls(conn: sqlite3.Connection, engine: DeepEnricherEngine):
    t0 = time.time()
    rows = [
        dict(r)
        for r in conn.execute(
            """
            SELECT ec.cricos_course_code, ec.cricos_provider_code, ec.course_name,
                   ec.apply_form, ec.course_description, ec.entry_requirements,
                   ec.english_requirements, ec.ielts_score, ec.pte_score, ec.toefl_score,
                   ec.english_source_url, ec.english_method, ec.intake_dates,
                   ec.enrolment_fee, ec.materials_fee,
                   mc.course_level, mc.vet_national_code
            FROM enriched_courses ec
            JOIN master_courses mc ON ec.cricos_course_code = mc.cricos_course_code
            WHERE NULLIF(ec.apply_form, '') IS NOT NULL
              AND (
                    NULLIF(ec.english_requirements, '') IS NULL
                 OR NULLIF(ec.entry_requirements, '') IS NULL
                 OR NULLIF(ec.course_description, '') IS NULL
              )
            """
        )
    ]

    by_url: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        u = sanitize_course_url(r["apply_form"])
        if u and is_deep_url(u):
            by_url[u].append(r)

    unique_urls = list(by_url.keys())
    print(
        f"\n[Stage 4C] Scraping {len(unique_urls)} unique already-verified `apply_form` course URLs "
        f"({len(rows)} gap courses previously skipped by Stage 2/3 per-provider caps)..."
    )
    if not unique_urls:
        return

    # Track per-domain Playwright usage so one giant JS university doesn't monopolize 2,000 PW tabs
    pw_domain_counts: dict[str, int] = collections.Counter()
    now_iso = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async def process_one_url(url: str) -> list[tuple]:
        dom = urlparse(url).netloc.lower()
        use_pw = pw_domain_counts[dom] < 18
        h, txt, final_u = await engine.fetch_html(
            url, allow_pw=use_pw, expand_tabs=use_pw, require_ielts_or_pw=use_pw
        )
        if use_pw:
            pw_domain_counts[dom] += 1
        if not txt or len(txt) < 200:
            return []

        clean_final = sanitize_course_url(final_u) or url
        eng_raw = extract_english_tests(txt)
        ie = clean_single_test_str(eng_raw.get("ielts"), "ielts")
        pt = clean_single_test_str(eng_raw.get("pte"), "pte")
        tf = clean_single_test_str(eng_raw.get("toefl"), "toefl")
        eng_html = rebuild_english_html(ie, pt, tf)

        tree = HTMLParser(h) if h else None
        desc_cand = sanitize_desc_html(extract_description_html(tree)) if tree else None
        intakes_cand = extract_intakes(txt)
        fees_cand = extract_international_fees(txt)

        out_rows = []
        for r in by_url[url]:
            ccode = r["cricos_course_code"]
            cur_desc = r["course_description"]
            cur_entry = r["entry_requirements"]
            cur_eng = r["english_requirements"]
            cur_ie = r["ielts_score"]
            cur_pt = r["pte_score"]
            cur_tf = r["toefl_score"]
            cur_esrc = r["english_source_url"]
            cur_emeth = r["english_method"]
            cur_intakes = r["intake_dates"]
            cur_enrol = r["enrolment_fee"]
            cur_mat = r["materials_fee"]

            changed = False
            if not cur_desc and desc_cand:
                cur_desc = desc_cand
                changed = True

            if not cur_eng and eng_html:
                cur_eng = eng_html
                cur_ie, cur_pt, cur_tf = ie, pt, tf
                cur_esrc = clean_final
                cur_emeth = "course_page_cricos_verified"
                changed = True

            acad_cand = extract_academic_entry_html(txt, r["course_level"] or "")
            if not cur_entry and (acad_cand or cur_eng):
                new_ent = sanitize_entry_html(acad_cand, cur_eng)
                if new_ent:
                    cur_entry = new_ent
                    changed = True
            elif cur_entry and cur_eng and "English Language Requirements" not in cur_entry:
                new_ent = sanitize_entry_html(cur_entry, cur_eng)
                if new_ent and new_ent != cur_entry:
                    cur_entry = new_ent
                    changed = True

            if not cur_intakes and intakes_cand:
                cur_intakes = intakes_cand
                changed = True
            if not cur_enrol and fees_cand["enrolment_fee"]:
                cur_enrol = fees_cand["enrolment_fee"]
                changed = True
            if not cur_mat and fees_cand["materials_fee"]:
                cur_mat = fees_cand["materials_fee"]
                changed = True

            if changed:
                out_rows.append(
                    (
                        cur_desc,
                        cur_entry,
                        cur_eng,
                        cur_ie,
                        cur_pt,
                        cur_tf,
                        cur_esrc,
                        cur_emeth,
                        cur_intakes,
                        cur_enrol,
                        cur_mat,
                        now_iso,
                        ccode,
                    )
                )
        return out_rows

    chunk_size = 300
    total_updated = 0
    for i in range(0, len(unique_urls), chunk_size):
        batch = unique_urls[i : i + chunk_size]
        res = await asyncio.gather(*[process_one_url(u) for u in batch])
        flat = [item for sub in res for item in sub]
        if flat:
            conn.executemany(
                """
                UPDATE enriched_courses
                SET course_description = ?,
                    entry_requirements = ?,
                    english_requirements = ?,
                    ielts_score = ?,
                    pte_score = ?,
                    toefl_score = ?,
                    english_source_url = ?,
                    english_method = ?,
                    intake_dates = ?,
                    enrolment_fee = ?,
                    materials_fee = ?,
                    updated_at = ?
                WHERE cricos_course_code = ?
                """,
                flat,
            )
            conn.commit()
            total_updated += len(flat)
        print(
            f"  [4C Progress] {min(i + chunk_size, len(unique_urls))}/{len(unique_urls)} URLs | "
            f"+{total_updated} courses enriched | {time.time()-t0:.1f}s"
        )


# ==============================================================================
# PHASE 4D: Deep Provider WP-JSON + Recursive Sitemaps + PDF Prospectuses/Policies
# ==============================================================================
async def phase_4d_deep_provider_pdf_web(conn: sqlite3.Connection, engine: DeepEnricherEngine):
    t0 = time.time()
    mp_map = {r["cricos_provider_code"]: dict(r) for r in conn.execute("SELECT * FROM master_providers")}
    ep_map = {r["cricos_provider_code"]: dict(r) for r in conn.execute("SELECT * FROM enriched_providers")}
    ec_map = {r["cricos_course_code"]: dict(r) for r in conn.execute("SELECT * FROM enriched_courses")}

    courses_by_prov: dict[str, list[dict]] = collections.defaultdict(list)
    gap_score_by_prov: dict[str, int] = collections.Counter()

    for r in conn.execute("SELECT * FROM master_courses"):
        mc = dict(r)
        pcode = mc["cricos_provider_code"]
        courses_by_prov[pcode].append(mc)
        ec = ec_map.get(mc["cricos_course_code"], {})
        gaps = (
            int(not ec.get("english_requirements"))
            + int(not ec.get("entry_requirements"))
            + int(not ec.get("course_description"))
        )
        if gaps > 0:
            gap_score_by_prov[pcode] += gaps

    target_provs = [
        pc
        for pc, sc in sorted(gap_score_by_prov.items(), key=lambda kv: -kv[1])
        if sc > 0 and (ep_map.get(pc, {}).get("website") or "").strip()
    ]
    print(
        f"\n[Stage 4D] Running Deep WP-JSON + Recursive Sitemap + Official PDF Prospectus & Policy Enricher "
        f"across {len(target_provs)} providers with remaining gaps..."
    )

    prov_sem = asyncio.Semaphore(22)

    async def run_one_provider(pcode: str):
        async with prov_sem:
            try:
                return await asyncio.wait_for(_enrich_provider_deep(pcode), timeout=45.0)
            except asyncio.TimeoutError:
                return None, []
            except Exception:
                return None, []

    async def _enrich_provider_deep(pcode: str) -> tuple[tuple | None, list[tuple]]:
        ep = ep_map[pcode]
        web = ep["website"].strip()
        if not web.startswith("http"):
            web = "https://" + web.lstrip("/")
        parsed = urlparse(web)
        if not parsed.netloc:
            return None, []
        origin = f"{parsed.scheme}://{parsed.netloc}"
        domain = parsed.netloc.lower().replace("www.", "")
        now_iso = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        prov_courses = courses_by_prov[pcode]
        known_cricos = {mc["cricos_course_code"].upper() for mc in prov_courses}
        vet_counts = collections.Counter(
            (mc.get("vet_national_code") or "").strip().upper()
            for mc in prov_courses
            if (mc.get("vet_national_code") or "").strip()
        )
        cricos_to_mc = {mc["cricos_course_code"].upper(): mc for mc in prov_courses}
        unique_vet_to_ccode = {
            (mc.get("vet_national_code") or "").strip().upper(): mc["cricos_course_code"].upper()
            for mc in prov_courses
            if (mc.get("vet_national_code") or "").strip()
            and vet_counts[(mc.get("vet_national_code") or "").strip().upper()] == 1
        }

        # Per-course working accumulator seeded from existing enriched_courses
        c_state: dict[str, dict] = {}
        need_any_url_or_desc = False
        need_any_eng_or_entry = False
        for mc in prov_courses:
            cc = mc["cricos_course_code"]
            ec = ec_map.get(cc, {})
            c_state[cc] = {
                "apply_form": ec.get("apply_form"),
                "match_method": ec.get("match_method") or "cricos_official_register",
                "course_description": ec.get("course_description"),
                "entry_requirements": ec.get("entry_requirements"),
                "english_requirements": ec.get("english_requirements"),
                "ielts_score": ec.get("ielts_score"),
                "pte_score": ec.get("pte_score"),
                "toefl_score": ec.get("toefl_score"),
                "english_source_url": ec.get("english_source_url"),
                "english_method": ec.get("english_method"),
                "intake_dates": ec.get("intake_dates"),
                "enrolment_fee": ec.get("enrolment_fee"),
                "materials_fee": ec.get("materials_fee"),
                "source_url": ec.get("source_url"),
                "changed": False,
            }
            if not ec.get("apply_form") or not ec.get("course_description"):
                need_any_url_or_desc = True
            if not ec.get("english_requirements") or not ec.get("entry_requirements"):
                need_any_eng_or_entry = True

        level_policies: dict[str, dict] = {}
        best_policy_url: str | None = None
        best_policy_is_pdf = False
        prov_new_email = ep.get("contact_email")
        prov_new_phone = ep.get("contact_phone")
        prov_new_intake = ep.get("intake_date")

        # ------------------------------------------------------------------
        # 1. WordPress REST API (`/wp-json/wp/v2/`) Fast Course & Policy Discovery
        # ------------------------------------------------------------------
        wp_pdfs = []
        if len(prov_courses) <= 180 and (need_any_url_or_desc or need_any_eng_or_entry):
            for ep_slug in ("pages", "courses", "course"):
                wp_txt = await engine.fetch_raw_text(f"{origin}/wp-json/wp/v2/{ep_slug}?per_page=100&_fields=link,title,content")
                if not wp_txt or not wp_txt.lstrip().startswith("["):
                    continue
                try:
                    import json as _json
                    wp_items = _json.loads(wp_txt)
                except Exception:
                    continue
                if not isinstance(wp_items, list):
                    continue
                for item in wp_items:
                    if not isinstance(item, dict):
                        continue
                    link = sanitize_course_url(item.get("link"))
                    rendered = ((item.get("content") or {}).get("rendered")) or ""
                    title_r = ((item.get("title") or {}).get("rendered")) or ""
                    if not rendered:
                        continue
                    # Collect any same-domain PDF links inside WP content
                    for pdf_m in re.findall(r'https?://[^"\'\s<>]+\.pdf', rendered, re.I):
                        if domain in urlparse(pdf_m).netloc.lower():
                            wp_pdfs.append(pdf_m)

                    tbl_txt = parse_html_english_tables(rendered)
                    tree = HTMLParser(rendered)
                    plain = (tbl_txt + "\n" if tbl_txt else "") + tree.text(separator="\n", strip=True)
                    up_plain = plain.upper()

                    # Check if this WP page is an English/Entry/International policy page
                    comb_t = ((link or "") + " " + title_r).lower()
                    if any(k in comb_t for k in ("english", "entry-requirement", "admission", "international")) and not any(
                        b in comb_t for b in ("elicos", "english-language-centre", "/blog/", "/news/")
                    ):
                        plvls = parse_policy_page_by_level(plain)
                        if plvls and link:
                            best_policy_url = best_policy_url or link
                            for bk, bv in plvls.items():
                                level_policies.setdefault(bk, bv)

                    # Match exact 7-char CRICOS course code or 1-to-1 unique VET code
                    matched_ccodes = []
                    for cc in known_cricos:
                        if cc in up_plain and not page_has_conflicting_cricos(up_plain, cc, known_cricos):
                            matched_ccodes.append((cc, "verified_cricos_on_course_page"))
                    if not matched_ccodes:
                        for vc, cc in unique_vet_to_ccode.items():
                            if vc in up_plain and not page_has_conflicting_cricos(up_plain, cc, known_cricos):
                                matched_ccodes.append((cc, "verified_unique_vet_code_on_course_page"))

                    for cc, mm in matched_ccodes:
                        st = c_state[cc]
                        if link and is_deep_url(link) and not st["apply_form"]:
                            st["apply_form"] = link
                            st["match_method"] = mm
                            st["source_url"] = link
                            st["changed"] = True
                        if not st["course_description"]:
                            d_h = sanitize_desc_html(extract_description_html(tree)) or clean_pdf_or_text_description(plain)
                            if d_h:
                                st["course_description"] = d_h
                                st["changed"] = True
                        eng_d = extract_english_tests(plain)
                        ie = clean_single_test_str(eng_d.get("ielts"), "ielts")
                        pt = clean_single_test_str(eng_d.get("pte"), "pte")
                        tf = clean_single_test_str(eng_d.get("toefl"), "toefl")
                        e_h = rebuild_english_html(ie, pt, tf)
                        if not st["english_requirements"] and e_h and link:
                            st["english_requirements"] = e_h
                            st["ielts_score"], st["pte_score"], st["toefl_score"] = ie, pt, tf
                            st["english_source_url"] = link
                            st["english_method"] = "course_page_cricos_verified"
                            st["changed"] = True
                        if not st["entry_requirements"]:
                            ac_h = extract_academic_entry_html(plain, cricos_to_mc[cc].get("course_level", ""))
                            comb_e = sanitize_entry_html(ac_h, st["english_requirements"])
                            if comb_e:
                                st["entry_requirements"] = comb_e
                                st["changed"] = True
                        if not st["intake_dates"]:
                            ci = extract_intakes(plain)
                            if ci:
                                st["intake_dates"] = ci
                                st["changed"] = True

        # ------------------------------------------------------------------
        # 2. Recursive Sitemap + Link Discovery
        # ------------------------------------------------------------------
        site_links, site_pdfs = await engine.discover_sitemaps_and_links(origin, domain)
        for wp_p in wp_pdfs:
            if wp_p not in site_pdfs:
                site_pdfs.append(wp_p)

        # Discover any new exact CRICOS / unique-VET course URLs in expanded sitemaps
        newly_matched_courses = []
        for cc, mc in cricos_to_mc.items():
            st = c_state[cc]
            if st["apply_form"]:
                continue
            vc = (mc.get("vet_national_code") or "").strip().upper()
            for cand_u, anchor in site_links.items():
                clean_cu = sanitize_course_url(cand_u)
                if not clean_cu or not is_deep_url(clean_cu):
                    continue
                hay = (clean_cu + " " + anchor).upper()
                if cc in hay:
                    st["apply_form"] = clean_cu
                    st["match_method"] = "verified_cricos_in_provider_url"
                    st["source_url"] = clean_cu
                    st["changed"] = True
                    newly_matched_courses.append(cc)
                    break
                if vc and vet_counts[vc] == 1 and vc in hay:
                    st["apply_form"] = clean_cu
                    st["match_method"] = "verified_unique_vet_code_in_provider_url"
                    st["source_url"] = clean_cu
                    st["changed"] = True
                    newly_matched_courses.append(cc)
                    break

        # Fetch up to 20 newly-discovered course URLs to populate their descriptions/entry/English
        for cc in newly_matched_courses[:20]:
            st = c_state[cc]
            ch, ctxt, cfin = await engine.fetch_html(st["apply_form"], allow_pw=False)
            if ctxt and not page_has_conflicting_cricos(ctxt.upper(), cc, known_cricos):
                if not st["course_description"] and ch:
                    dh = sanitize_desc_html(extract_description_html(HTMLParser(ch)))
                    if dh:
                        st["course_description"] = dh
                eng_d = extract_english_tests(ctxt)
                ie = clean_single_test_str(eng_d.get("ielts"), "ielts")
                pt = clean_single_test_str(eng_d.get("pte"), "pte")
                tf = clean_single_test_str(eng_d.get("toefl"), "toefl")
                eh = rebuild_english_html(ie, pt, tf)
                if not st["english_requirements"] and eh:
                    st["english_requirements"] = eh
                    st["ielts_score"], st["pte_score"], st["toefl_score"] = ie, pt, tf
                    st["english_source_url"] = sanitize_course_url(cfin) or st["apply_form"]
                    st["english_method"] = "course_page_cricos_verified"
                if not st["entry_requirements"]:
                    ach = extract_academic_entry_html(ctxt, cricos_to_mc[cc].get("course_level", ""))
                    ce = sanitize_entry_html(ach, st["english_requirements"])
                    if ce:
                        st["entry_requirements"] = ce

        # ------------------------------------------------------------------
        # 3. Official HTML Policy Pages (Sitemap + Search Fallback + Playwright Accordion Expansion)
        # ------------------------------------------------------------------
        if need_any_eng_or_entry:
            policy_cands = []
            for u, anch in site_links.items():
                ul = (u + " " + anch).lower()
                if any(b in ul for b in ("english-language-centre", "/elicos", "/blog/", "/news/", "/events/", "/staff/", "/media/")):
                    continue
                if any(k in ul for k in ("english-language", "english-requirement", "english_requirement", "language-proficiency", "entry-requirement", "admission-requirement", "minimum-english")):
                    policy_cands.append(u)
                elif "international" in ul and any(k in ul for k in ("apply", "admission", "entry", "english", "enrol")):
                    policy_cands.append(u)

            if not policy_cands:
                s_htmls, s_pdfs = await engine.search_domain_policy_or_pdf(domain)
                policy_cands.extend(s_htmls)
                for sp in s_pdfs:
                    if sp not in site_pdfs:
                        site_pdfs.insert(0, sp)

            def rank_pol(u: str):
                ul = u.lower()
                sc = 0
                if "english" in ul:
                    sc += 15
                if "proficiency" in ul or "requirement" in ul:
                    sc += 10
                if "international" in ul:
                    sc += 6
                if "entry" in ul or "admission" in ul:
                    sc += 5
                return -sc

            policy_cands = [sanitize_course_url(u) for u in policy_cands]
            policy_cands = [u for u in dict.fromkeys(policy_cands) if u]
            policy_cands.sort(key=rank_pol)

            for idx, pu in enumerate(policy_cands[:4]):
                ph, ptxt, pfin = await engine.fetch_html(
                    pu, allow_pw=(idx < 2), expand_tabs=(idx < 2), require_ielts_or_pw=(idx < 2)
                )
                if ph:
                    # Also harvest any PDF handbooks linked directly from the Admissions/English page!
                    for a in HTMLParser(ph).css("a[href]"):
                        href = (a.attributes.get("href") or "").strip()
                        if href and ".pdf" in href.lower():
                            fpdf = urljoin(pfin, href)
                            if domain in urlparse(fpdf).netloc.lower() and fpdf not in site_pdfs:
                                site_pdfs.insert(0, fpdf)
                    if not prov_new_email:
                        em, phn, _ = extract_contacts(ptxt or "", ph, pfin)
                        if em and not BAD_EMAIL_PATTERNS.search(em):
                            prov_new_email = em
                        if phn and not prov_new_phone:
                            prov_new_phone = phn
                if ptxt and not any(b in pfin.lower() for b in ("english-language-centre", "/elicos")):
                    if not prov_new_intake:
                        pi = extract_intakes(ptxt)
                        if pi:
                            prov_new_intake = pi
                    plvls = parse_policy_page_by_level(ptxt)
                    if plvls:
                        clean_pfin = sanitize_course_url(pfin) or pu
                        if not best_policy_url or "english" in clean_pfin.lower():
                            best_policy_url = clean_pfin
                            best_policy_is_pdf = False
                        for bk, bv in plvls.items():
                            if bk not in level_policies or (
                                not any(level_policies[bk]["eng"].values()) and any(bv["eng"].values())
                            ):
                                level_policies[bk] = bv

        # ------------------------------------------------------------------
        # 4. Official Same-Domain PDF Prospectus / Handbook / Entry & Fee Schedule Parser
        # ------------------------------------------------------------------
        still_need_pdf = any(
            not st["english_requirements"] or not st["entry_requirements"] or not st["course_description"]
            for st in c_state.values()
        )
        if still_need_pdf and site_pdfs:
            def rank_pdf(u: str):
                ul = u.lower()
                if any(b in ul for b in ("annual-report", "financial", "map", "campus-map", "calendar-201", "privacy", "whs", "covid")):
                    return 50
                sc = 0
                if any(cc.lower() in ul for cc in list(known_cricos)[:60]):
                    sc += 20
                if any(vc.lower() in ul for vc in list(unique_vet_to_ccode.keys())[:60]):
                    sc += 18
                if "english" in ul or "ielts" in ul:
                    sc += 16
                if "entry" in ul or "admission" in ul:
                    sc += 14
                if "prospectus" in ul or "course-guide" in ul or "course_guide" in ul or "brochure" in ul:
                    sc += 13
                if "handbook" in ul or "international" in ul:
                    sc += 11
                if "fee" in ul or "intake" in ul:
                    sc += 7
                return -sc

            ranked_pdfs = sorted(site_pdfs, key=rank_pdf)
            good_pdfs = [u for u in ranked_pdfs if rank_pdf(u) <= 0][:5]

            for pdf_u in good_pdfs:
                clean_pdf_u = sanitize_course_url(pdf_u, allow_pdf=True)
                if not clean_pdf_u:
                    continue
                pages = await engine.fetch_pdf_pages(clean_pdf_u)
                if not pages:
                    continue
                full_pdf_txt = "\n".join(pages)
                if not prov_new_intake:
                    pi = extract_intakes(full_pdf_txt[:30000])
                    if pi:
                        prov_new_intake = pi
                if not prov_new_email:
                    em, phn, _ = extract_contacts(full_pdf_txt[:20000], full_pdf_txt[:20000], clean_pdf_u)
                    if em and not BAD_EMAIL_PATTERNS.search(em):
                        prov_new_email = em
                    if phn and not prov_new_phone:
                        prov_new_phone = phn

                # 4a. Check page-level exact CRICOS / 1-to-1 unique VET course blocks inside PDF
                for pg_idx, pg_txt in enumerate(pages):
                    if len(pg_txt) < 80:
                        continue
                    up_pg = pg_txt.upper()
                    found_on_pg = set(CRICOS_COURSE_CODE_RE.findall(up_pg)) & known_cricos
                    if len(found_on_pg) == 1:
                        # Single CRICOS course flyer / dedicated handbook page!
                        cc = next(iter(found_on_pg))
                        spread = pg_txt
                        if pg_idx + 1 < len(pages):
                            nxt_codes = set(CRICOS_COURSE_CODE_RE.findall(pages[pg_idx + 1].upper())) & known_cricos
                            if not (nxt_codes - {cc}):
                                spread = pg_txt + "\n" + pages[pg_idx + 1]
                        st = c_state[cc]
                        if not st["course_description"]:
                            dh = clean_pdf_or_text_description(spread)
                            if dh:
                                st["course_description"] = dh
                                st["source_url"] = st["source_url"] or clean_pdf_u
                                st["changed"] = True
                        eng_d = extract_english_tests(spread)
                        ie = clean_single_test_str(eng_d.get("ielts"), "ielts")
                        pt = clean_single_test_str(eng_d.get("pte"), "pte")
                        tf = clean_single_test_str(eng_d.get("toefl"), "toefl")
                        eh = rebuild_english_html(ie, pt, tf)
                        if not st["english_requirements"] and eh:
                            st["english_requirements"] = eh
                            st["ielts_score"], st["pte_score"], st["toefl_score"] = ie, pt, tf
                            st["english_source_url"] = clean_pdf_u
                            st["english_method"] = "pdf_course_cricos_verified"
                            st["changed"] = True
                        if not st["entry_requirements"]:
                            ach = extract_academic_entry_html(spread, cricos_to_mc[cc].get("course_level", ""))
                            ce = sanitize_entry_html(ach, st["english_requirements"])
                            if ce:
                                st["entry_requirements"] = ce
                                st["source_url"] = st["source_url"] or clean_pdf_u
                                st["changed"] = True
                        if not st["intake_dates"]:
                            ci = extract_intakes(spread)
                            if ci:
                                st["intake_dates"] = ci
                                st["changed"] = True
                    elif len(found_on_pg) > 1:
                        # Multi-course summary schedule table: inspect tight 320-char row window per CRICOS code
                        for cc in found_on_pg:
                            st = c_state[cc]
                            if st["english_requirements"]:
                                continue
                            m_cc = re.search(re.escape(cc), pg_txt, re.I)
                            if m_cc:
                                row_win = pg_txt[max(0, m_cc.start() - 120) : m_cc.end() + 240]
                                # Only accept if no OTHER CRICOS code sits inside the tight forward window
                                fwd_codes = set(CRICOS_COURSE_CODE_RE.findall(pg_txt[m_cc.end() : m_cc.end() + 200].upper())) & known_cricos
                                if not (fwd_codes - {cc}):
                                    eng_d = extract_english_tests(row_win)
                                    ie = clean_single_test_str(eng_d.get("ielts"), "ielts")
                                    pt = clean_single_test_str(eng_d.get("pte"), "pte")
                                    tf = clean_single_test_str(eng_d.get("toefl"), "toefl")
                                    eh = rebuild_english_html(ie, pt, tf)
                                    if eh:
                                        st["english_requirements"] = eh
                                        st["ielts_score"], st["pte_score"], st["toefl_score"] = ie, pt, tf
                                        st["english_source_url"] = clean_pdf_u
                                        st["english_method"] = "pdf_schedule_cricos_verified"
                                        st["changed"] = True

                # 4b. Also parse institution-level International Entry & English policy from PDF Handbook/Prospectus
                if not any( b in clean_pdf_u.lower() for b in ("elicos",) ):
                    pdf_lvls = parse_policy_page_by_level(full_pdf_txt[:80000])
                    if pdf_lvls:
                        if not best_policy_url:
                            best_policy_url = clean_pdf_u
                            best_policy_is_pdf = True
                        for bk, bv in pdf_lvls.items():
                            if bk not in level_policies or (
                                not any(level_policies[bk]["eng"].values()) and any(bv["eng"].values())
                            ):
                                level_policies[bk] = bv

        # ------------------------------------------------------------------
        # 5. Apply Level-Aware Official Provider Policy to Remaining Course Gaps
        # ------------------------------------------------------------------
        if level_policies and best_policy_url:
            for cc, mc in cricos_to_mc.items():
                st = c_state[cc]
                cname = mc.get("course_name") or ""
                clvl = mc.get("course_level") or ""
                bkt = bucket_for_course(clvl, cname)
                pol = level_policies.get(bkt) or level_policies.get("general")
                if not pol:
                    continue

                if not st["english_requirements"] and any(pol["eng"].values()):
                    ie = clean_single_test_str(pol["eng"].get("ielts"), "ielts")
                    pt = clean_single_test_str(pol["eng"].get("pte"), "pte")
                    tf = clean_single_test_str(pol["eng"].get("toefl"), "toefl")

                    # Enforce level sanity & NMBA pre-registration Nursing/Midwifery >= 7.0 guard
                    if ie:
                        msc = re.search(r"\b([45678](?:\.[05])?)\b", ie)
                        sc = float(msc.group(1)) if msc else 0.0
                        cl_low = clvl.lower()
                        if ("masters" in cl_low or "doctoral" in cl_low) and sc < 6.0:
                            ie = pt = tf = None
                        elif "bachelor" in cl_low and sc < 5.5:
                            ie = pt = tf = None
                        elif sc < 7.0 and re.match(r"^(?:bachelor|diploma)\s+of\s+(?:nursing|midwifery)\b", cname.strip(), re.I):
                            ie = pt = tf = None

                    eh = rebuild_english_html(ie, pt, tf)
                    if eh:
                        st["english_requirements"] = eh
                        st["ielts_score"], st["pte_score"], st["toefl_score"] = ie, pt, tf
                        st["english_source_url"] = best_policy_url
                        prefix = "pdf_institution_policy" if best_policy_is_pdf else "institution_official_policy"
                        st["english_method"] = f"{prefix}_{bkt}"
                        st["changed"] = True

                if not st["entry_requirements"]:
                    comb_e = sanitize_entry_html(pol.get("acad"), st["english_requirements"])
                    if comb_e:
                        st["entry_requirements"] = comb_e
                        if not st["source_url"] or st["source_url"] == "https://cricos.education.gov.au/":
                            st["source_url"] = best_policy_url
                        st["changed"] = True
                elif st["english_requirements"] and "English Language Requirements" not in st["entry_requirements"]:
                    comb_e = sanitize_entry_html(st["entry_requirements"], st["english_requirements"])
                    if comb_e and comb_e != st["entry_requirements"]:
                        st["entry_requirements"] = comb_e
                        st["changed"] = True

                if not st["intake_dates"] and prov_new_intake:
                    st["intake_dates"] = prov_new_intake
                    st["changed"] = True

        prov_upd = None
        if (
            prov_new_email != ep.get("contact_email")
            or prov_new_phone != ep.get("contact_phone")
            or prov_new_intake != ep.get("intake_date")
        ):
            prov_upd = (prov_new_email, prov_new_phone, prov_new_intake, now_iso, pcode)

        c_upds = []
        for cc, st in c_state.items():
            if st["changed"]:
                c_upds.append(
                    (
                        st["apply_form"],
                        st["match_method"],
                        st["course_description"],
                        st["entry_requirements"],
                        st["english_requirements"],
                        st["ielts_score"],
                        st["pte_score"],
                        st["toefl_score"],
                        st["english_source_url"],
                        st["english_method"],
                        st["intake_dates"],
                        st["source_url"] or "https://cricos.education.gov.au/",
                        now_iso,
                        cc,
                    )
                )
        return prov_upd, c_upds

    batch_size = 44
    tot_courses_updated = 0
    tot_provs_updated = 0
    for i in range(0, len(target_provs), batch_size):
        batch = target_provs[i : i + batch_size]
        results = await asyncio.gather(*[run_one_provider(pc) for pc in batch])
        p_rows = [p for p, _ in results if p]
        c_rows = [c for _, clist in results for c in clist]

        if p_rows:
            conn.executemany(
                """
                UPDATE enriched_providers
                SET contact_email = COALESCE(?, contact_email),
                    contact_phone = COALESCE(?, contact_phone),
                    intake_date = COALESCE(?, intake_date),
                    updated_at = ?
                WHERE cricos_provider_code = ?
                """,
                p_rows,
            )
            tot_provs_updated += len(p_rows)

        if c_rows:
            conn.executemany(
                """
                UPDATE enriched_courses
                SET apply_form = ?,
                    match_method = ?,
                    course_description = ?,
                    entry_requirements = ?,
                    english_requirements = ?,
                    ielts_score = ?,
                    pte_score = ?,
                    toefl_score = ?,
                    english_source_url = ?,
                    english_method = ?,
                    intake_dates = ?,
                    source_url = ?,
                    updated_at = ?
                WHERE cricos_course_code = ?
                """,
                c_rows,
            )
            tot_courses_updated += len(c_rows)

        conn.commit()
        print(
            f"  [4D Progress] {min(i + batch_size, len(target_provs))}/{len(target_provs)} providers | "
            f"+{tot_courses_updated} courses & +{tot_provs_updated} providers enriched | {time.time()-t0:.1f}s"
        )


async def run_stage4():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_english_columns(conn)

    await phase_4a_providers(conn)
    await phase_4b_tga_vet(conn)

    async with DeepEnricherEngine(pw_concurrency=10, http_concurrency=55) as engine:
        await phase_4c_verified_course_urls(conn, engine)
        await phase_4d_deep_provider_pdf_web(conn, engine)

    # Print before/after coverage summary directly from SQLite
    summary = dict(
        conn.execute(
            """
            SELECT
                COUNT(*) as total_courses,
                SUM(CASE WHEN NULLIF(entry_requirements,'') IS NOT NULL THEN 1 ELSE 0 END) as has_entry,
                SUM(CASE WHEN NULLIF(english_requirements,'') IS NOT NULL THEN 1 ELSE 0 END) as has_english,
                SUM(CASE WHEN NULLIF(course_description,'') IS NOT NULL THEN 1 ELSE 0 END) as has_desc,
                SUM(CASE WHEN NULLIF(apply_form,'') IS NOT NULL THEN 1 ELSE 0 END) as has_apply_form,
                SUM(CASE WHEN NULLIF(intake_dates,'') IS NOT NULL THEN 1 ELSE 0 END) as has_intakes
            FROM enriched_courses
            """
        ).fetchone()
    )
    prov_summary = dict(
        conn.execute(
            """
            SELECT
                COUNT(*) as total_providers,
                SUM(CASE WHEN NULLIF(website,'') IS NOT NULL THEN 1 ELSE 0 END) as has_website,
                SUM(CASE WHEN NULLIF(contact_email,'') IS NOT NULL THEN 1 ELSE 0 END) as has_email,
                SUM(CASE WHEN NULLIF(intake_date,'') IS NOT NULL THEN 1 ELSE 0 END) as has_intake
            FROM enriched_providers
            """
        ).fetchone()
    )
    conn.close()
    print(f"\n=== STAGE 4 COMPLETE ({time.time()-t0:.1f}s) ===")
    print("Providers:", prov_summary)
    print("Courses:  ", summary)


if __name__ == "__main__":
    asyncio.run(run_stage4())
