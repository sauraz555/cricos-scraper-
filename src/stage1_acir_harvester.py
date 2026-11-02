"""
Stage 1: High-Speed Official Study Australia (ACIR) International Student Harvester.
Uses async HTTP/2 + X-Inertia JSON protocol against search.studyaustralia.gov.au
to match CRICOS providers, courses, and international scholarships at ~100+ items/sec.
"""
import asyncio
import datetime as dt
import html
import json
import math
import os
import re
import sqlite3
import time
from urllib.parse import urlparse

import httpx
from selectolax.parser import HTMLParser

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_DIR, "data", "dashboard_mirror.sqlite")
BASE_URL = "https://search.studyaustralia.gov.au"


def norm_name(s: str) -> str:
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    for tok in ("pty ltd", "pty. ltd.", "pty", "limited", "ltd", "inc", "australia", "the "):
        s = s.replace(tok, " ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def norm_course_title(s: str) -> str:
    if not s:
        return ""
    s = s.lower()
    # Strip trailing VET code like "- BSB60420" or "(SIT50422)"
    s = re.sub(r"[-–]\s*[a-z]{3,4}\d{4,6}[a-z]?\b", " ", s)
    s = re.sub(r"\([a-z]{3,4}\d{4,6}[a-z]?\)", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def clean_url(u: str) -> str | None:
    if not u:
        return None
    u = u.strip()
    if not u:
        return None
    if not u.startswith(("http://", "https://")):
        u = "https://" + u.lstrip("/")
    return u


def is_deep_url(u: str | None) -> bool:
    if not u:
        return False
    try:
        p = urlparse(u)
        return p.path not in ("", "/") or bool(p.query)
    except Exception:
        return False


def strip_domestic_noise(text: str) -> str:
    """Remove purely domestic CSP/HECS-HELP/VTAC/UAC lines while preserving academic requirements."""
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    kept = []
    domestic_only_pat = re.compile(
        r"\b(commonwealth supported place|hecs-help|fee-help|austudy|abstudy|centrelink|vtac|qtac|satac|tisc)\b",
        re.I,
    )
    for sent in sentences:
        s = sent.strip()
        if not s:
            continue
        if domestic_only_pat.search(s) and "international" not in s.lower() and "overseas" not in s.lower():
            continue
        kept.append(s)
    return " ".join(kept)


def format_html_description(item: dict) -> str | None:
    parts = []
    desc = (item.get("description") or "").strip()
    if desc:
        paras = [f"<p>{html.escape(p.strip())}</p>" for p in re.split(r"\n{2,}", desc) if p.strip()]
        parts.append("<h4>Course Information</h4> " + " ".join(paras))
    designed = (item.get("designed_for") or "").strip()
    if designed and designed not in desc:
        parts.append(f"<h5>Designed For</h5> <p>{html.escape(designed)}</p>")
    structure = (item.get("structure") or "").strip()
    if structure and len(structure) < 1500:
        parts.append(f"<h5>Course Structure</h5> <p>{html.escape(structure)}</p>")
    pathways = (item.get("study_pathways") or "").strip()
    if pathways and len(pathways) < 1000:
        parts.append(f"<h5>Study & Career Pathways</h5> <p>{html.escape(pathways)}</p>")
    return " ".join(parts) if parts else None


def format_html_entry_requirements(item: dict, sites: list[dict]) -> str | None:
    blocks = []
    overseas = strip_domestic_noise(item.get("overseas_student_entry") or "")
    entry = strip_domestic_noise(item.get("entry_requirements") or "")
    std_entry = strip_domestic_noise(item.get("standard_entry_requirements") or "")
    alt_entry = strip_domestic_noise(item.get("alternate_entry_requirements") or "")

    ielts_scores = sorted(
        {str(s.get("overall_ielts_score")) for s in sites if s.get("overall_ielts_score") not in (None, "", 0, "0")}
    )
    toefl_scores = sorted(
        {str(s.get("overall_toefl_score")) for s in sites if s.get("overall_toefl_score") not in (None, "", 0, "0")}
    )

    if overseas:
        blocks.append(f"<p><strong>International Student Entry:</strong> {html.escape(overseas)}</p>")
    if entry and entry != overseas:
        blocks.append(f"<p><strong>Entry Requirements:</strong> {html.escape(entry)}</p>")
    elif std_entry and std_entry != overseas:
        blocks.append(f"<p><strong>Academic Entry Requirements:</strong> {html.escape(std_entry)}</p>")
    if alt_entry and alt_entry not in (overseas, entry, std_entry):
        blocks.append(f"<p><strong>Additional Pathways:</strong> {html.escape(alt_entry)}</p>")

    eng_bits = []
    if ielts_scores:
        eng_bits.append(f"IELTS Overall minimum score: {', '.join(ielts_scores)}")
    if toefl_scores:
        eng_bits.append(f"TOEFL Overall minimum score: {', '.join(toefl_scores)}")
    if eng_bits:
        blocks.append(f"<p><strong>English Language Requirements (International):</strong> {'; '.join(eng_bits)}.</p>")

    if not blocks:
        return None
    return "<h4>Entry Requirements (International Students)</h4> " + " ".join(blocks)


def infer_assessment_style(text: str) -> str | None:
    if not text:
        return None
    tl = text.lower()
    has_exam = bool(re.search(r"\b(examination|examinations|written exam|final exam|exams)\b", tl))
    has_assign = bool(
        re.search(
            r"\b(assignment|assignments|project|projects|portfolio|coursework|practical assessment|competency|case study|thesis|dissertation)\b",
            tl,
        )
    )
    if has_exam and has_assign:
        return "mixed"
    if has_exam:
        return "exam"
    if has_assign:
        return "assignment"
    return None


def format_duration(sites: list[dict]) -> str | None:
    seen = []
    for s in sites:
        for att in s.get("attendance") or []:
            mode = (att.get("name") or "").strip()
            length = att.get("length")
            unit = (att.get("du_name_plural") or att.get("du_name") or "").strip()
            weeks = att.get("du_week")
            if length and unit:
                u = att.get("du_name") if length == 1 else unit
                label = f"{length} {u} {mode.lower()}".strip()
                if weeks and unit != "weeks":
                    label += f" ({weeks} weeks)"
                if label not in seen:
                    seen.append(label)
    return ", ".join(seen) if seen else None


def setup_extra_tables(conn: sqlite3.Connection):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS enriched_scholarships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            acir_id TEXT UNIQUE,
            provider_id INTEGER,
            cricos_provider_code TEXT,
            institution_name TEXT,
            scholarship_name TEXT NOT NULL,
            scholarship_description TEXT,
            eligibility TEXT,
            scholarship_amount TEXT,
            website TEXT,
            level_of_studies TEXT,
            field_of_studies TEXT,
            is_international INTEGER DEFAULT 1,
            source_url TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS acir_provider_map (
            cricos_provider_code TEXT PRIMARY KEY,
            acir_id TEXT,
            acir_slug TEXT,
            acir_name TEXT,
            web_address TEXT,
            logo_url TEXT,
            description TEXT
        );
        """
    )
    conn.commit()


async def get_inertia_version(client: httpx.AsyncClient) -> str:
    r = await client.get(f"{BASE_URL}/courses")
    r.raise_for_status()
    app = HTMLParser(r.text).css_first("#app")
    data = json.loads(html.unescape(app.attributes["data-page"]))
    return data["version"]


async def fetch_inertia_json(
    client: httpx.AsyncClient, path: str, params: dict, version: str, sem: asyncio.Semaphore
) -> dict | None:
    headers = {
        "X-Inertia": "true",
        "X-Inertia-Version": version,
        "Accept": "application/json",
    }
    for attempt in range(4):
        async with sem:
            try:
                r = await client.get(f"{BASE_URL}{path}", params=params, headers=headers)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 409:
                    # Inertia version updated
                    new_ver = await get_inertia_version(client)
                    headers["X-Inertia-Version"] = new_ver
                    continue
                if r.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                return None
            except Exception:
                await asyncio.sleep(0.8 * (attempt + 1))
    return None


async def run_stage1():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    setup_extra_tables(conn)

    # Load all 802 DB providers
    db_providers = {
        r["cricos_provider_code"]: dict(r)
        for r in conn.execute("SELECT * FROM orig_provider_institution")
    }
    name_to_cricos = {}
    for code, p in db_providers.items():
        for nm in (p.get("institution_name"), p.get("trading_name")):
            nn = norm_name(nm)
            if nn:
                name_to_cricos.setdefault(nn, code)

    # Load all DB courses grouped by provider
    db_courses_by_prov: dict[str, list[dict]] = {}
    for r in conn.execute("SELECT * FROM orig_courses"):
        d = dict(r)
        db_courses_by_prov.setdefault(d["cricos_provider_code"], []).append(d)

    print(f"DB loaded: {len(db_providers)} providers, {sum(len(v) for v in db_courses_by_prov.values())} courses.")

    limits = httpx.Limits(max_connections=25, max_keepalive_connections=20)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    }
    sem = asyncio.Semaphore(16)

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20.0, limits=limits) as client:
        version = await get_inertia_version(client)
        print(f"Connected to Study Australia ACIR (Inertia version={version})")

        # --- STEP 1A: Fetch all ACIR Providers ---
        p1 = await fetch_inertia_json(client, "/providers", {"page": 1}, version, sem)
        total_prov = p1["props"]["providers"]["meta"]["total"]
        prov_pages = math.ceil(total_prov / 10)
        print(f"Fetching {total_prov} ACIR providers across {prov_pages} pages...")

        all_acir_providers = list(p1["props"]["providers"]["data"])
        prov_tasks = [
            fetch_inertia_json(client, "/providers", {"page": pg}, version, sem)
            for pg in range(2, prov_pages + 1)
        ]
        for res in await asyncio.gather(*prov_tasks):
            if res and "props" in res and "providers" in res["props"]:
                all_acir_providers.extend(res["props"]["providers"]["data"])

        matched_providers: dict[str, dict] = {}
        org_id_to_cricos: dict[int, str] = {}

        for ap in all_acir_providers:
            raw_codes = ap.get("cricos_code") or ""
            codes = [c.strip() for c in re.split(r"[;,\s]+", raw_codes) if c.strip()]
            matched_code = None
            for c in codes:
                if c in db_providers:
                    matched_code = c
                    break
            if not matched_code:
                nn = norm_name(ap.get("name") or "")
                if nn in name_to_cricos:
                    matched_code = name_to_cricos[nn]

            if matched_code:
                logos = ap.get("logos") or {}
                logo_url = logos.get("original") or logos.get("logo_150_140") or logos.get("default")
                if logo_url and not str(logo_url).startswith("http"):
                    logo_url = None
                web = clean_url(ap.get("web_address"))
                info = {
                    "cricos_provider_code": matched_code,
                    "acir_id": ap["id"],
                    "acir_slug": ap["slug"],
                    "acir_name": ap.get("name"),
                    "web_address": web,
                    "logo_url": logo_url,
                    "description": ap.get("description"),
                    "organisation_id": ap.get("organisation_id"),
                }
                # Prefer entry with active courses if duplicate
                if matched_code not in matched_providers or ap.get("has_active_courses"):
                    matched_providers[matched_code] = info
                if ap.get("organisation_id"):
                    org_id_to_cricos[ap["organisation_id"]] = matched_code

        conn.executemany(
            """
            INSERT OR REPLACE INTO acir_provider_map
            (cricos_provider_code, acir_id, acir_slug, acir_name, web_address, logo_url, description)
            VALUES (:cricos_provider_code, :acir_id, :acir_slug, :acir_name, :web_address, :logo_url, :description)
            """,
            list(matched_providers.values()),
        )
        conn.commit()
        print(f"Matched {len(matched_providers)} / {len(db_providers)} DB providers in ACIR ({time.time()-t0:.1f}s)")

        # --- STEP 1B: Fetch all ACIR International Scholarships ---
        t_sch = time.time()
        s1 = await fetch_inertia_json(client, "/scholarships", {"page": 1}, version, sem)
        total_sch = s1["props"]["scholarships"]["meta"]["total"]
        sch_pages = math.ceil(total_sch / 10)
        print(f"Fetching {total_sch} ACIR scholarships across {sch_pages} pages...")

        all_sch = list(s1["props"]["scholarships"]["data"])
        sch_tasks = [
            fetch_inertia_json(client, "/scholarships", {"page": pg}, version, sem)
            for pg in range(2, sch_pages + 1)
        ]
        for res in await asyncio.gather(*sch_tasks):
            if res and "props" in res and "scholarships" in res["props"]:
                all_sch.extend(res["props"]["scholarships"]["data"])

        now_iso = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sch_rows = []
        prov_scholarships_map: dict[str, list[dict]] = {}

        for s in all_sch:
            if not s.get("is_for_international_students", False):
                continue
            amt_parts = []
            if s.get("amount_max"):
                amt_parts.append(f"Up to AUD ${s['amount_max']}")
            elif s.get("amount_annual"):
                amt_parts.append(f"AUD ${s['amount_annual']}/year")
            if s.get("amount_comment"):
                amt_parts.append(str(s["amount_comment"]).strip())
            amt_str = " — ".join(amt_parts)[:250] if amt_parts else "See scholarship details"

            levels = ", ".join(l.get("name", "") for l in (s.get("level_of_studies") or []) if l.get("name"))
            fos = ", ".join(f.get("name", "") for f in (s.get("field_of_studies") or []) if f.get("name"))
            web = clean_url(s.get("web_address")) or f"{BASE_URL}/scholarship/{s.get('slug')}/{s.get('id')}"
            src = f"{BASE_URL}/scholarship/{s.get('slug')}/{s.get('id')}"

            orgs = s.get("organisations") or []
            matched_cricos = None
            for org in orgs:
                oid = org.get("organisation_id")
                if oid and oid in org_id_to_cricos:
                    matched_cricos = org_id_to_cricos[oid]
                    break
                ocodes = [c.strip() for c in re.split(r"[;,\s]+", org.get("cricos_code") or "") if c.strip()]
                for oc in ocodes:
                    if oc in db_providers:
                        matched_cricos = oc
                        break
                if not matched_cricos:
                    onn = norm_name(org.get("name") or "")
                    if onn in name_to_cricos:
                        matched_cricos = name_to_cricos[onn]
                if matched_cricos:
                    break

            prov_obj = db_providers.get(matched_cricos) if matched_cricos else None
            prov_id = int(prov_obj["id"]) if prov_obj else None
            inst_name = prov_obj["institution_name"] if prov_obj else (orgs[0].get("name") if orgs else "National / Multi-Institution")

            rec = (
                s.get("id"),
                prov_id,
                matched_cricos,
                inst_name,
                (s.get("name") or "").strip(),
                (s.get("description") or "").strip(),
                (s.get("eligibility") or "").strip(),
                amt_str,
                web,
                levels,
                fos,
                1,
                src,
                now_iso,
            )
            sch_rows.append(rec)
            if matched_cricos:
                prov_scholarships_map.setdefault(matched_cricos, []).append(
                    {"name": (s.get("name") or "").strip(), "amount": amt_str, "url": web}
                )

        conn.executemany(
            """
            INSERT OR REPLACE INTO enriched_scholarships
            (acir_id, provider_id, cricos_provider_code, institution_name, scholarship_name,
             scholarship_description, eligibility, scholarship_amount, website, level_of_studies,
             field_of_studies, is_international, source_url, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            sch_rows,
        )
        conn.commit()
        print(
            f"Saved {len(sch_rows)} international scholarships ({len(prov_scholarships_map)} DB providers with scholarships) in {time.time()-t_sch:.1f}s"
        )

        # --- STEP 1C: Fetch Courses for Matched Providers & Match by CRICOS Code / Title ---
        t_crs = time.time()
        print(f"Fetching ACIR course catalogues for {len(matched_providers)} matched providers...")

        async def fetch_provider_courses(cricos_code: str, pinfo: dict):
            path = f"/provider/{pinfo['acir_slug']}/{pinfo['acir_id']}/courses"
            first = await fetch_inertia_json(client, path, {"page": 1}, version, sem)
            if not first or "props" not in first or "courses" not in first["props"]:
                return cricos_code, []
            total = first["props"]["courses"]["meta"].get("total") or 0
            items = list(first["props"]["courses"]["data"])
            pages = min(math.ceil(total / 10), 100)
            if pages > 1:
                more = await asyncio.gather(
                    *[fetch_inertia_json(client, path, {"page": pg}, version, sem) for pg in range(2, pages + 1)]
                )
                for m in more:
                    if m and "props" in m and "courses" in m["props"]:
                        items.extend(m["props"]["courses"]["data"])
            return cricos_code, items

        prov_items_list = list(matched_providers.items())
        enriched_course_rows = []
        enriched_provider_rows = []
        matched_course_count = 0

        # Process in chunks of 40 providers so memory stays tiny and SQLite checkpoints frequently
        for i in range(0, len(prov_items_list), 40):
            batch = prov_items_list[i : i + 40]
            results = await asyncio.gather(*[fetch_provider_courses(c, p) for c, p in batch])

            for cricos_code, acir_courses in results:
                db_prov = db_providers[cricos_code]
                pinfo = matched_providers[cricos_code]
                db_courses = db_courses_by_prov.get(cricos_code, [])

                # Build ACIR lookup indices for this provider
                by_exact_cricos: dict[str, dict] = {}
                by_prefix6: dict[str, dict] = {}
                by_title: dict[str, dict] = {}
                provider_intakes_set = set()

                for ac in acir_courses:
                    for st in ac.get("sites") or []:
                        sd = (st.get("course_start_date") or "").strip()
                        if sd and len(sd) < 80:
                            provider_intakes_set.add(sd)
                        sc = (st.get("cricos_code") or "").strip()
                        if sc:
                            by_exact_cricos.setdefault(sc.upper(), ac)
                            if len(sc) >= 6:
                                by_prefix6.setdefault(sc[:6].upper(), ac)
                    nt = norm_course_title(ac.get("name") or "")
                    if nt:
                        by_title.setdefault(nt, ac)

                # Save provider enrichment row
                agg_intakes = "; ".join(sorted(provider_intakes_set))[:250] if provider_intakes_set else None
                sch_list = prov_scholarships_map.get(cricos_code, [])
                enriched_provider_rows.append(
                    (
                        int(db_prov["id"]),
                        cricos_code,
                        db_prov["institution_name"],
                        pinfo["web_address"] or db_prov.get("website"),
                        None,
                        agg_intakes,
                        pinfo["logo_url"],
                        None,
                        None,
                        None,
                        json.dumps(sch_list) if sch_list else None,
                        f"{BASE_URL}/provider/{pinfo['acir_slug']}/{pinfo['acir_id']}",
                        "acir_official_api",
                        "acir_enriched",
                        None,
                        now_iso,
                    )
                )

                # Match each DB course on this provider
                for dc in db_courses:
                    dc_code = (dc.get("cricos_course_code") or "").strip().upper()
                    ac = None
                    method = None
                    conf = 0.0

                    if dc_code in by_exact_cricos:
                        ac = by_exact_cricos[dc_code]
                        method = "acir_exact_cricos"
                        conf = 1.0
                    elif len(dc_code) >= 6 and dc_code[:6] in by_prefix6:
                        ac = by_prefix6[dc_code[:6]]
                        method = "acir_cricos_6digit"
                        conf = 0.97
                    else:
                        nt = norm_course_title(dc.get("course_name") or "")
                        if nt and nt in by_title:
                            ac = by_title[nt]
                            method = "acir_provider_title_match"
                            conf = 0.88

                    if not ac:
                        continue

                    matched_course_count += 1
                    sites = ac.get("sites") or []
                    # Prefer site matching exact CRICOS code
                    matching_sites = [
                        s for s in sites if (s.get("cricos_code") or "").strip().upper()[:6] == dc_code[:6]
                    ] or sites

                    web_urls = [
                        clean_url(s.get("course_web_address"))
                        for s in matching_sites
                        if is_deep_url(clean_url(s.get("course_web_address")))
                    ]
                    best_url = web_urls[0] if web_urls else None

                    desc_html = format_html_description(ac)
                    entry_html = format_html_entry_requirements(ac, matching_sites)

                    overseas_fees = [
                        float(s["fees"]["overseas_full_fee"])
                        for s in matching_sites
                        if s.get("fees") and s["fees"].get("overseas_full_fee") not in (None, "", 0, "0")
                    ]
                    best_overseas_fee = overseas_fees[0] if overseas_fees else None

                    dur_str = format_duration(matching_sites)
                    if not dur_str and dc.get("course_duration_per_week"):
                        try:
                            wks = int(dc["course_duration_per_week"])
                            if wks > 0:
                                dur_str = f"{wks} weeks"
                        except Exception:
                            pass

                    c_intakes = sorted(
                        {(s.get("course_start_date") or "").strip() for s in matching_sites if (s.get("course_start_date") or "").strip()}
                    )
                    c_intake_str = "; ".join(c_intakes)[:250] if c_intakes else None

                    assess = infer_assessment_style(
                        " ".join(
                            str(ac.get(k) or "")
                            for k in ("description", "structure", "subjects", "other_info")
                        )
                    )

                    sch_summary = (
                        f"{len(sch_list)} provider scholarship(s) available (e.g. {sch_list[0]['name']})"
                        if sch_list
                        else None
                    )
                    acir_course_url = f"{BASE_URL}/course/{ac.get('slug')}/{ac.get('id')}"

                    enriched_course_rows.append(
                        (
                            int(dc["id"]),
                            cricos_code,
                            dc["institution_name"],
                            dc["cricos_course_code"],
                            dc["course_name"],
                            best_url,
                            desc_html,
                            entry_html,
                            None,               # onshore_tuition_fee (only set when explicitly published as separate onshore promo fee)
                            best_overseas_fee,  # offshore_tuition_fee (standard CRICOS / ACIR international fee)
                            None,               # enrolment_fee (Stage 2 website scraper)
                            None,               # materials_fee (Stage 2 website scraper)
                            dur_str,
                            assess,
                            c_intake_str,
                            sch_summary,
                            1,                  # international_verified = 1 (ACIR overseas schema)
                            conf,
                            method,
                            best_url or acir_course_url,
                            "acir_enriched",
                            None,
                            now_iso,
                        )
                    )

            conn.executemany(
                """
                INSERT OR REPLACE INTO enriched_providers
                (provider_id, cricos_provider_code, institution_name, website, international_url,
                 intake_date, provider_logo, contact_email, contact_phone, contact_website_url,
                 scholarships_json, source_url, discovery_method, status, error_msg, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                enriched_provider_rows,
            )
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
                enriched_course_rows,
            )
            conn.commit()
            enriched_provider_rows.clear()
            enriched_course_rows.clear()
            print(
                f"  Processed {min(i+40, len(prov_items_list))}/{len(prov_items_list)} providers -> {matched_course_count} courses matched so far ({time.time()-t_crs:.1f}s)"
            )

    print(f"Stage 1 Complete in {time.time()-t0:.1f}s! Total ACIR-matched courses: {matched_course_count}")
    conn.close()


if __name__ == "__main__":
    asyncio.run(run_stage1())
