"""
Stage 3: Dedicated International Entry Requirements & English Language Requirements
Extractor (Playwright + Async HTTP + Level-Aware Policy Table Parser).

Extracts BOTH:
  1. `entry_requirements` (Academic International Entry Requirements: overseas Year 12,
     Bachelor/AQF qualifications, work experience, portfolio, age >= 18 — zero CSP/ATAR-only noise)
  2. `english_requirements` (Structured IELTS Academic, PTE Academic, TOEFL iBT,
     Cambridge C1/CAE, OET scores + sub-band minimums)

Works across three verified layers (always recording exact source URL & method):
  - Layer A: Parse existing verified HTML in `orig_courses` / `enriched_courses`
  - Layer B: Scrape individual course pages (`apply_form`) with Playwright tab/accordion expansion
  - Layer C: Discover & scrape each institution's official International English & Entry
             Requirements policy page (with course-level awareness: VET/Cert/Diploma vs
             Bachelor vs Postgraduate vs Research vs Nursing/Education/Law).
"""
import asyncio
import datetime as dt
import html
import os
import re
import sqlite3
import time
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from playwright.async_api import async_playwright, Browser, BrowserContext
from selectolax.parser import HTMLParser

from entry_english_rules import (
    DECOMPOSE_NAV_SELECTOR,
    extract_academic_entry_html_v2,
    extract_english_tests_v2,
    html_to_clean_texts,
    parse_html_english_tables,
)

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_DIR, "data", "dashboard_mirror.sqlite")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def ensure_english_columns(conn: sqlite3.Connection):
    existing = {r[1] for r in conn.execute("PRAGMA table_info(enriched_courses)")}
    new_cols = {
        "english_requirements": "TEXT",
        "ielts_score": "TEXT",
        "pte_score": "TEXT",
        "toefl_score": "TEXT",
        "english_source_url": "TEXT",
        "english_method": "TEXT",
    }
    for col, ctype in new_cols.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE enriched_courses ADD COLUMN {col} {ctype};")
    conn.commit()


def clean_html_to_text(h: str | None) -> str:
    eng_txt, _ = html_to_clean_texts(h)
    return eng_txt


def extract_english_tests(text: str) -> dict:
    """
    Extract structured IELTS, PTE, TOEFL requirements from text using decimal-preserving v2 rules.
    """
    return extract_english_tests_v2(text)


def build_english_html(eng: dict) -> str | None:
    items = []
    if eng.get("ielts"):
        items.append(f"<li><strong>IELTS Academic:</strong> {html.escape(eng['ielts'])}</li>")
    if eng.get("pte"):
        items.append(f"<li><strong>PTE Academic:</strong> {html.escape(eng['pte'])}</li>")
    if eng.get("toefl"):
        items.append(f"<li><strong>TOEFL iBT:</strong> {html.escape(eng['toefl'])}</li>")
    if eng.get("other"):
        items.append(f"<li><strong>Other Accepted Tests:</strong> {html.escape(eng['other'])}</li>")
    if not items:
        return None
    return f"<h4>English Language Requirements (International Students)</h4><ul>{''.join(items)}</ul>"


def extract_academic_entry_html(
    text: str,
    course_level: str = "",
    course_name: str = "",
    duration_weeks: int | None = None,
) -> str | None:
    """Extract clean, level-compatible International Student Academic Entry Requirements."""
    return extract_academic_entry_html_v2(
        text,
        course_level=course_level,
        course_name=course_name,
        duration_weeks=duration_weeks,
    )


def combine_entry_and_english_html(acad_html: str | None, eng_html: str | None) -> str | None:
    if acad_html and eng_html:
        return f"{acad_html} {eng_html}"
    return acad_html or eng_html


def bucket_for_course(course_level: str, course_name: str) -> str:
    cn = (course_name or "").lower()
    cl = (course_level or "").lower()
    if "primary school" in cl or "junior secondary" in cl or "senior secondary" in cl:
        return "school"
    if any(k in cn for k in ("nursing", "midwifery", "clinical psychology", "initial teacher", "teaching", "education (primary", "education (secondary", "law", "juris doctor", "medicine", "physiotherapy", "pharmacy", "social work")):
        return "high_prof"
    if "doctoral" in cl or "doctor" in cn or "phd" in cn or ("master" in cn and "research" in cn):
        return "research"
    if "master" in cl or "graduate" in cl or "postgraduate" in cl:
        return "postgrad"
    if "bachelor" in cl or "associate degree" in cl or "undergraduate" in cl:
        return "undergrad"
    if "diploma" in cl or "certificate" in cl or "vocational" in cl:
        return "vet_diploma"
    if "elicos" in cl or "english" in cn or "non aqf" in cl or "foundation" in cl:
        return "elicos_foundation"
    return "general"


def parse_policy_page_by_level(text: str) -> dict[str, dict]:
    """
    Splits an institution's English & Entry Requirements policy page into chunks
    and maps IELTS/PTE/TOEFL + level-compatible Academic requirements to level buckets
    (NEVER copying `default_acad` across incompatible degree levels!).
    """
    out: dict[str, dict] = {}
    if not text:
        return out

    default_eng = extract_english_tests_v2(text)
    if any(default_eng.values()):
        out["general"] = {"eng": default_eng, "acad": None}

    level_specs = {
        "vet_diploma": (
            r"\b(diploma|advanced\s+diploma|certificate\s+(?:iii|iv)|vet\s+courses?|vocational)\b",
            "Diploma",
        ),
        "undergrad": (
            r"\b(undergraduate|bachelor(?:'s)?\s+degrees?|associate\s+degree)\b",
            "Bachelor Degree",
        ),
        "postgrad": (
            r"\b(postgraduate|master(?:'s)?(?:\s+by\s+coursework)?|graduate\s+diploma|graduate\s+certificate)\b",
            "Masters Degree (Coursework)",
        ),
        "research": (
            r"\b(higher\s+degree\s+by\s+research|hdr|doctor\s+of\s+philosophy|phd|master\s+of\s+philosophy)\b",
            "Doctoral Degree",
        ),
        "high_prof": (
            r"\b(nursing|teaching|initial\s+teacher\s+education|social\s+work|law|juris\s+doctor|medicine|psychology)\b",
            "",
        ),
    }

    for bkt, (pat, rep_lvl) in level_specs.items():
        for m in re.finditer(pat, text, re.I):
            window = text[max(0, m.start() - 60) : m.end() + 650]
            eng = extract_english_tests_v2(window)
            # Strip synthesized table lines from window before academic extraction
            acad_win = "\n".join(
                ln for ln in window.splitlines() if " — IELTS" not in ln and " — PTE" not in ln and " — TOEFL" not in ln
            )
            acad = extract_academic_entry_html_v2(acad_win, course_level=rep_lvl) if rep_lvl else None
            if any(eng.values()) or acad:
                merged_eng = dict(eng)
                if any(merged_eng.values()):
                    for k in ("pte", "toefl", "other"):
                        if not merged_eng.get(k) and default_eng.get(k) and merged_eng.get("ielts") == default_eng.get("ielts"):
                            merged_eng[k] = default_eng[k]
                out[bkt] = {"eng": merged_eng, "acad": acad}
                break

    return out


class FastPolicyAndCourseFetcher:
    def __init__(self, pw_concurrency: int = 8, http_concurrency: int = 36):
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
            limits=httpx.Limits(max_connections=45, max_keepalive_connections=25),
        )
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            headless=True,
            args=["--disable-http2", "--disable-blink-features=AutomationControlled"],
        )
        self.context = await self.browser.new_context(
            user_agent=USER_AGENT,
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
        if self.http_client:
            await self.http_client.aclose()
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()

    async def fetch_page(self, url: str, expand_accordions: bool = False, allow_pw: bool = True) -> tuple[str | None, str | None, str]:
        dead = False
        async with self.http_sem:
            try:
                r = await self.http_client.get(url)
                if r.status_code == 404:
                    return None, None, url
                if r.status_code == 200 and "text/html" in r.headers.get("content-type", "text/html"):
                    h = r.text
                    txt, _ = html_to_clean_texts(h)
                    if len(txt) >= 500 and "Just a moment..." not in txt:
                        # If HTTP already contains IELTS or PTE or Academic requirements, return immediately (0.15s!)
                        if re.search(r"\b(IELTS|PTE|TOEFL|Year\s*12|entry\s+requirements)\b", txt, re.I):
                            return h, txt, str(r.url)
                        if not expand_accordions:
                            return h, txt, str(r.url)
            except httpx.ConnectError as ce:
                if "getaddrinfo failed" in str(ce) or "Name or service not known" in str(ce):
                    dead = True
            except Exception:
                pass

        if not allow_pw or dead:
            return None, None, url

        async with self.pw_sem:
            page = await self.context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=12000)
                if expand_accordions:
                    # Click International switcher + Entry/English requirement tabs/accordions
                    for sel in (
                        "button:has-text('International')",
                        "[role='tab']:has-text('International')",
                        "button:has-text('Entry requirements')",
                        "button:has-text('Admission')",
                        "button:has-text('English')",
                        "summary:has-text('Entry')",
                        "summary:has-text('English')",
                    ):
                        loc = page.locator(sel).first
                        if await loc.count() > 0 and await loc.is_visible():
                            try:
                                await loc.click(timeout=1000)
                            except Exception:
                                pass
                h = await page.content()
                txt, _ = html_to_clean_texts(h)
                return h, txt, page.url
            except Exception:
                return None, None, url
            finally:
                await page.close()

    async def find_provider_english_urls(self, base_website: str, inst_name: str) -> list[str]:
        parsed = urlparse(base_website)
        domain = parsed.netloc.lower().replace("www.", "")
        origin = f"{parsed.scheme}://{parsed.netloc}"

        candidates = []
        # 1. Check homepage & /international links first
        h, _, final_u = await self.fetch_page(base_website, expand_accordions=False, allow_pw=False)
        if h:
            for a in HTMLParser(h).css("a[href]"):
                href = (a.attributes.get("href") or "").strip()
                if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                fu = urljoin(final_u, href)
                comb = (fu + " " + a.text(strip=True)).lower()
                if domain in urlparse(fu).netloc.lower() and not fu.lower().endswith(".pdf"):
                    if any(k in comb for k in ("english", "entry-requirement", "entry_requirement", "admission", "international", "apply", "enrol")):
                        candidates.append(fu)

        # 2. Only hit DuckDuckGo if site links didn't already yield an English or Entry page
        has_strong_cand = any(any(k in c.lower() for k in ("english", "entry", "admission")) for c in candidates)
        if not has_strong_cand:
            q = f'site:{domain} ("IELTS" OR "English language requirements" OR "entry requirements")'
            ddg_url = f"https://html.duckduckgo.com/html/?q={quote_plus(q)}"
            async with self.ddg_sem:
                dh, _, _ = await self.fetch_page(ddg_url, expand_accordions=False, allow_pw=False)
            if dh:
                from urllib.parse import parse_qs
                for a in HTMLParser(dh).css("a.result__a"):
                    href = a.attributes.get("href") or ""
                    if "uddg=" in href:
                        href = parse_qs(urlparse(href).query).get("uddg", [href])[0]
                    if href.startswith("http") and domain in urlparse(href).netloc.lower() and not href.lower().endswith(".pdf"):
                        candidates.insert(0, href)

        # Rank URLs containing 'english' or 'entry' highest
        def rank_u(u: str):
            ul = u.lower()
            s = 0
            if "english" in ul:
                s += 10
            if "entry" in ul or "admission" in ul:
                s += 8
            if "international" in ul:
                s += 5
            return -s

        uniq = list(dict.fromkeys(candidates))
        uniq.sort(key=rank_u)
        return uniq[:4]


async def run_stage3():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_english_columns(conn)
    now_iso = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ------------------------------------------------------------------
    # LAYER A: Parse existing verified HTML in master_courses & enriched_courses
    # ------------------------------------------------------------------
    print("Layer A: Extracting IELTS/PTE/TOEFL from verified course entry_requirements HTML...")
    all_master_courses = [dict(r) for r in conn.execute("SELECT * FROM master_courses")]
    ec_map = {r["cricos_course_code"]: dict(r) for r in conn.execute("SELECT * FROM enriched_courses")}

    layer_a_updates = []
    for mc in all_master_courses:
        ccode = mc["cricos_course_code"]
        ec = ec_map.get(ccode)
        if ec and ec.get("english_requirements"):
            continue
        combined_html = (mc.get("db_entry_requirements") or "") + "\n" + ((ec or {}).get("entry_requirements") or "")
        if not combined_html.strip():
            continue
        plain = clean_html_to_text(combined_html)
        eng = extract_english_tests(plain)
        eng_html = build_english_html(eng)
        if eng_html and ec:
            src = ec.get("source_url") or mc.get("db_apply_form") or ""
            layer_a_updates.append(
                (
                    eng_html,
                    eng["ielts"],
                    eng["pte"],
                    eng["toefl"],
                    src,
                    "parsed_from_verified_entry_requirements",
                    ccode,
                )
            )

    if layer_a_updates:
        conn.executemany(
            """
            UPDATE enriched_courses
            SET english_requirements = ?, ielts_score = ?, pte_score = ?, toefl_score = ?,
                english_source_url = ?, english_method = ?
            WHERE cricos_course_code = ?
            """,
            layer_a_updates,
        )
        conn.commit()

    ec_map = {r["cricos_course_code"]: dict(r) for r in conn.execute("SELECT * FROM enriched_courses")}
    already_eng = sum(1 for v in ec_map.values() if v.get("english_requirements"))
    print(f"  Layer A complete: {already_eng} courses already have structured English requirements ({time.time()-t0:.1f}s)")

    prov_rows = {
        r["cricos_provider_code"]: dict(r)
        for r in conn.execute("SELECT * FROM master_providers")
    }
    ep_rows = {
        r["cricos_provider_code"]: dict(r)
        for r in conn.execute("SELECT * FROM enriched_providers")
    }

    missing_by_prov: dict[str, list[dict]] = {}
    for mc in all_master_courses:
        ccode = mc["cricos_course_code"]
        ec = ec_map.get(ccode, {})
        has_entry = bool((mc.get("db_entry_requirements") or "").strip() or (ec.get("entry_requirements") or "").strip())
        has_eng = bool(ec.get("english_requirements"))
        if not has_entry or not has_eng:
            missing_by_prov.setdefault(mc["cricos_provider_code"], []).append(mc)

    target_prov_codes = sorted(missing_by_prov.keys(), key=lambda c: -len(missing_by_prov[c]))
    print(
        f"Layer B & C: Scraping Entry & English Requirements across {len(target_prov_codes)} CRICOS providers "
        f"({sum(len(v) for v in missing_by_prov.values())} courses needing Entry and/or English requirements)..."
    )

    prov_sem = asyncio.Semaphore(28)

    async with FastPolicyAndCourseFetcher(pw_concurrency=10, http_concurrency=48) as fetcher:

        async def process_provider_entry_english(pcode: str):
            async with prov_sem:
                try:
                    return await asyncio.wait_for(_do_provider(pcode), timeout=20.0)
                except asyncio.TimeoutError:
                    return []

        async def _do_provider(pcode: str) -> list[tuple]:
            mp = prov_rows.get(pcode, {})
            ep = ep_rows.get(pcode, {})
            web = ep.get("website") or mp.get("cricos_website") or mp.get("db_website")
            if web and not web.startswith("http"):
                web = "https://" + web.lstrip("/")
            if not web:
                return []

            courses = missing_by_prov[pcode]

            policy_urls = await fetcher.find_provider_english_urls(web, mp.get("institution_name", ""))
            # Never apply ELICOS language-centre URLs as institution-wide degree policies
            policy_urls = [
                u for u in policy_urls
                if not any(b in u.lower() for b in ("english-language-centre", "/elicos", "/blog/", "/news/", "/events/", "/staff/"))
            ]
            level_policies: dict[str, dict] = {}
            best_policy_url = web

            if policy_urls:
                fetched_policies = await asyncio.gather(
                    *[fetcher.fetch_page(u, expand_accordions=True, allow_pw=(idx == 0)) for idx, u in enumerate(policy_urls)]
                )
                for u, (_, ptxt, final_u) in zip(policy_urls, fetched_policies):
                    if ptxt and not any(b in final_u.lower() for b in ("english-language-centre", "/elicos")):
                        parsed_lvls = parse_policy_page_by_level(ptxt)
                        if parsed_lvls:
                            if best_policy_url == web or "english" in final_u.lower():
                                best_policy_url = final_u
                            for k, v in parsed_lvls.items():
                                if k not in level_policies:
                                    level_policies[k] = v

            updates = []

            async def enrich_one_course(mc: dict, idx: int):
                ccode = mc["cricos_course_code"]
                ec = ec_map.get(ccode, {})
                cur_entry = (mc.get("db_entry_requirements") or "").strip() or (ec.get("entry_requirements") or "").strip()
                cur_eng_html = ec.get("english_requirements")

                # Strictly use CRICOS-verified course URL only (never unverified db_apply_form)
                c_url = ec.get("apply_form")
                is_specific_url = bool(c_url and urlparse(c_url).path.rstrip("/") not in ("", "/about", "/home", "/international"))

                new_acad_html = None
                new_eng = {
                    "ielts": ec.get("ielts_score"),
                    "pte": ec.get("pte_score"),
                    "toefl": ec.get("toefl_score"),
                    "other": None,
                }
                eng_src = ec.get("english_source_url")
                eng_method = ec.get("english_method")

                if is_specific_url and idx < 10:
                    _, ctxt, cfinal = await fetcher.fetch_page(
                        c_url, expand_accordions=(idx < 3), allow_pw=(idx < 3)
                    )
                    if ctxt:
                        c_eng = extract_english_tests(ctxt)
                        if any(c_eng.values()):
                            new_eng = c_eng
                            eng_src = cfinal
                            eng_method = "course_page_cricos_verified"
                        if not cur_entry:
                            new_acad_html = extract_academic_entry_html(ctxt, mc.get("course_level", ""))

                bkt = bucket_for_course(mc.get("course_level", ""), mc.get("course_name", ""))
                pol = level_policies.get(bkt) or level_policies.get("general")
                if pol:
                    if not any(new_eng.values()) and any(pol["eng"].values()):
                        new_eng = pol["eng"]
                        eng_src = best_policy_url
                        eng_method = f"institution_official_policy_{bkt}"
                    if not cur_entry and not new_acad_html and pol.get("acad"):
                        new_acad_html = pol["acad"]

                final_eng_html = cur_eng_html or build_english_html(new_eng)
                final_entry_html = ec.get("entry_requirements") or mc.get("db_entry_requirements")
                if not final_entry_html:
                    final_entry_html = combine_entry_and_english_html(new_acad_html, final_eng_html)
                elif final_eng_html and "IELTS" not in final_entry_html:
                    final_entry_html = f"{final_entry_html} {final_eng_html}"

                if not final_eng_html and not final_entry_html:
                    return None

                return (
                    final_entry_html,
                    final_eng_html,
                    new_eng.get("ielts"),
                    new_eng.get("pte"),
                    new_eng.get("toefl"),
                    eng_src or c_url or best_policy_url,
                    eng_method or "stage3_policy",
                    now_iso,
                    ccode,
                )

            c_results = await asyncio.gather(*[enrich_one_course(mc, idx) for idx, mc in enumerate(courses)])
            for r in c_results:
                if r:
                    updates.append(r)
            return updates

        batch_size = 60
        total_updated = 0
        for i in range(0, len(target_prov_codes), batch_size):
            batch = target_prov_codes[i : i + batch_size]
            res_lists = await asyncio.gather(*[process_provider_entry_english(pc) for pc in batch])
            flat_rows = [item for sub in res_lists for item in sub]

            if flat_rows:
                conn.executemany(
                    """
                    UPDATE enriched_courses
                    SET entry_requirements = COALESCE(?, entry_requirements),
                        english_requirements = COALESCE(?, english_requirements),
                        ielts_score = COALESCE(?, ielts_score),
                        pte_score = COALESCE(?, pte_score),
                        toefl_score = COALESCE(?, toefl_score),
                        english_source_url = COALESCE(?, english_source_url),
                        english_method = COALESCE(?, english_method),
                        updated_at = ?
                    WHERE cricos_course_code = ?
                    """,
                    flat_rows,
                )
                conn.commit()
            total_updated += len(flat_rows)
            print(
                f"  Stage 3 progress: {min(i+batch_size, len(target_prov_codes))}/{len(target_prov_codes)} providers "
                f"| {total_updated} courses updated with Entry/English requirements ({time.time()-t0:.1f}s)"
            )

    final_entry_cnt = conn.execute(
        "SELECT COUNT(*) FROM enriched_courses WHERE entry_requirements IS NOT NULL AND TRIM(entry_requirements)!=''"
    ).fetchone()[0]
    final_eng_cnt = conn.execute(
        "SELECT COUNT(*) FROM enriched_courses WHERE english_requirements IS NOT NULL AND TRIM(english_requirements)!=''"
    ).fetchone()[0]
    total_mc = len(all_master_courses)
    print(
        f"Stage 3 Complete in {time.time()-t0:.1f}s! "
        f"Courses with Entry Requirements: {final_entry_cnt}/{total_mc} | "
        f"Courses with Structured English Requirements: {final_eng_cnt}/{total_mc}"
    )
    conn.close()


if __name__ == "__main__":
    asyncio.run(run_stage3())
