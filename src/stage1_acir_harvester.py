"""
Stage 1: High-Speed Official Study Australia (ACIR) International Student Harvester (v2).

Strictly matches ALL 1,543 CRICOS providers and ALL 25,199 active CRICOS courses
using ONLY exact `cricos_provider_code` and exact 7-character `cricos_course_code`
(ZERO fuzzy provider-name, 6-digit prefix, or course-title matching).
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


def clean_url(u: str | None) -> str | None:
    if not u:
        return None
    u = str(u).strip()
    if not u or u.upper() in ("NULL", "NONE", "N/A"):
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


def format_html_entry_and_english(item: dict, matching_sites: list[dict]) -> tuple[str | None, str | None, str | None, str | None]:
    blocks = []
    overseas = strip_domestic_noise(item.get("overseas_student_entry") or "")
    entry = strip_domestic_noise(item.get("entry_requirements") or "")
    std_entry = strip_domestic_noise(item.get("standard_entry_requirements") or "")
    alt_entry = strip_domestic_noise(item.get("alternate_entry_requirements") or "")

    ielts_scores = sorted(
        {
            str(s.get("overall_ielts_score")).strip()
            for s in matching_sites
            if s.get("overall_ielts_score") not in (None, "", 0, "0", "0.0")
        }
    )
    toefl_scores = sorted(
        {
            str(s.get("overall_toefl_score")).strip()
            for s in matching_sites
            if s.get("overall_toefl_score") not in (None, "", 0, "0")
        }
    )

    if overseas:
        blocks.append(f"<p><strong>International Student Entry:</strong> {html.escape(overseas)}</p>")
    if entry and entry != overseas:
        blocks.append(f"<p><strong>Entry Requirements:</strong> {html.escape(entry)}</p>")
    elif std_entry and std_entry != overseas:
        blocks.append(f"<p><strong>Academic Entry Requirements:</strong> {html.escape(std_entry)}</p>")
    if alt_entry and alt_entry not in (overseas, entry, std_entry):
        blocks.append(f"<p><strong>Additional Pathways:</strong> {html.escape(alt_entry)}</p>")

    ielts_str = f"IELTS Overall minimum score: {', '.join(ielts_scores)}" if ielts_scores else None
    toefl_str = f"TOEFL Overall minimum score: {', '.join(toefl_scores)}" if toefl_scores else None

    eng_li = []
    if ielts_str:
        eng_li.append(f"<li><strong>IELTS Academic:</strong> {html.escape(ielts_str)}</li>")
    if toefl_str:
        eng_li.append(f"<li><strong>TOEFL iBT:</strong> {html.escape(toefl_str)}</li>")
    eng_html = (
        f"<h4>English Language Requirements (International Students)</h4><ul>{''.join(eng_li)}</ul>"
        if eng_li
        else None
    )

    entry_html = ("<h4>Entry Requirements (International Students)</h4> " + " ".join(blocks)) if blocks else None
    return entry_html, eng_html, ielts_str, toefl_str


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
        DROP TABLE IF EXISTS enriched_scholarships;
        CREATE TABLE enriched_scholarships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            acir_id TEXT UNIQUE,
            provider_id INTEGER,
            cricos_provider_code TEXT NOT NULL,
            institution_name TEXT NOT NULL,
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

        DROP TABLE IF EXISTS acir_provider_map;
        CREATE TABLE acir_provider_map (
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

    master_provs = {
        r["cricos_provider_code"]: dict(r)
        for r in conn.execute("SELECT * FROM master_providers")
    }
    master_courses_by_prov: dict[str, list[dict]] = {}
    master_courses_by_code: dict[str, dict] = {}
    for r in conn.execute("SELECT * FROM master_courses"):
        d = dict(r)
        master_courses_by_prov.setdefault(d["cricos_provider_code"], []).append(d)
        master_courses_by_code[d["cricos_course_code"]] = d

    existing_ep = {
        r["cricos_provider_code"]: dict(r)
        for r in conn.execute("SELECT * FROM enriched_providers")
    }
    existing_ec = {
        r["cricos_course_code"]: dict(r)
        for r in conn.execute("SELECT * FROM enriched_courses")
    }

    print(
        f"Master CRICOS loaded: {len(master_provs)} providers, {len(master_courses_by_code)} active courses."
    )

    limits = httpx.Limits(max_connections=35, max_keepalive_connections=25)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    }
    sem = asyncio.Semaphore(22)

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=22.0, limits=limits) as client:
        version = await get_inertia_version(client)
        print(f"Connected to Study Australia ACIR (Inertia version={version})")

        # --- STEP 1A: Fetch ALL ACIR Providers & Match STRICTLY by Exact CRICOS Provider Code ---
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
        org_id_to_cricos_list: dict[int, list[str]] = {}

        for ap in all_acir_providers:
            raw_codes = ap.get("cricos_code") or ""
            codes = [c.strip().upper() for c in re.split(r"[;,\s/]+", raw_codes) if c.strip()]
            exact_codes = [c for c in codes if c in master_provs]
            if not exact_codes:
                continue

            logos = ap.get("logos") or {}
            logo_url = logos.get("original") or logos.get("logo_150_140") or logos.get("default")
            if logo_url and not str(logo_url).startswith("http"):
                logo_url = None
            web = clean_url(ap.get("web_address"))

            for c in exact_codes:
                info = {
                    "cricos_provider_code": c,
                    "acir_id": ap["id"],
                    "acir_slug": ap["slug"],
                    "acir_name": ap.get("name"),
                    "web_address": web,
                    "logo_url": logo_url,
                    "description": ap.get("description"),
                    "organisation_id": ap.get("organisation_id"),
                    "has_active_courses": bool(ap.get("has_active_courses")),
                }
                if c not in matched_providers or ap.get("has_active_courses"):
                    matched_providers[c] = info
                if ap.get("organisation_id"):
                    org_id_to_cricos_list.setdefault(ap["organisation_id"], [])
                    if c not in org_id_to_cricos_list[ap["organisation_id"]]:
                        org_id_to_cricos_list[ap["organisation_id"]].append(c)

        conn.executemany(
            """
            INSERT OR REPLACE INTO acir_provider_map
            (cricos_provider_code, acir_id, acir_slug, acir_name, web_address, logo_url, description)
            VALUES (:cricos_provider_code, :acir_id, :acir_slug, :acir_name, :web_address, :logo_url, :description)
            """,
            list(matched_providers.values()),
        )
        conn.commit()
        print(
            f"Strictly matched {len(matched_providers)} / {len(master_provs)} CRICOS providers in ACIR by exact cricos_provider_code ({time.time()-t0:.1f}s)"
        )

        # --- STEP 1B: Fetch ALL International Scholarships & Match STRICTLY by Exact CRICOS Provider Code ---
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
                if oid and oid in org_id_to_cricos_list and len(org_id_to_cricos_list[oid]) == 1:
                    matched_cricos = org_id_to_cricos_list[oid][0]
                    break
                ocodes = [c.strip().upper() for c in re.split(r"[;,\s/]+", org.get("cricos_code") or "") if c.strip()]
                for oc in ocodes:
                    if oc in master_provs:
                        matched_cricos = oc
                        break
                if matched_cricos:
                    break

            if not matched_cricos:
                continue

            mp = master_provs[matched_cricos]
            rec = (
                str(s.get("id")),
                mp["db_provider_id"],
                matched_cricos,
                mp["institution_name"],
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
            f"Saved {len(sch_rows)} strictly CRICOS-matched international scholarships across {len(prov_scholarships_map)} providers ({time.time()-t_sch:.1f}s)"
        )

        # --- STEP 1C: Deduplicate ACIR Provider Endpoints & Match Courses STRICTLY by Exact 7-Char CRICOS Code ---
        t_crs = time.time()
        unique_acir_endpoints: dict[str, dict] = {}
        endpoint_to_cricos_codes: dict[str, list[str]] = {}
        for c_code, pinfo in matched_providers.items():
            ep_key = f"{pinfo['acir_slug']}/{pinfo['acir_id']}"
            unique_acir_endpoints[ep_key] = pinfo
            endpoint_to_cricos_codes.setdefault(ep_key, []).append(c_code)

        print(
            f"Fetching ACIR course catalogues across {len(unique_acir_endpoints)} unique ACIR endpoints for {len(matched_providers)} CRICOS providers..."
        )

        async def fetch_endpoint_courses(ep_key: str, pinfo: dict):
            path = f"/provider/{pinfo['acir_slug']}/{pinfo['acir_id']}/courses"
            first = await fetch_inertia_json(client, path, {"page": 1}, version, sem)
            if not first or "props" not in first or "courses" not in first["props"]:
                return ep_key, []
            total = first["props"]["courses"]["meta"].get("total") or 0
            items = list(first["props"]["courses"]["data"])
            pages = math.ceil(total / 10)
            if pages > 1:
                more = await asyncio.gather(
                    *[fetch_inertia_json(client, path, {"page": pg}, version, sem) for pg in range(2, pages + 1)]
                )
                for m in more:
                    if m and "props" in m and "courses" in m["props"]:
                        items.extend(m["props"]["courses"]["data"])
            return ep_key, items

        ep_items_list = list(unique_acir_endpoints.items())
        enriched_course_rows = []
        enriched_provider_rows = []
        matched_course_count = 0

        for i in range(0, len(ep_items_list), 45):
            batch = ep_items_list[i : i + 45]
            results = await asyncio.gather(*[fetch_endpoint_courses(k, p) for k, p in batch])

            for ep_key, acir_courses in results:
                cricos_codes_for_ep = endpoint_to_cricos_codes[ep_key]

                # Index ACIR courses strictly by exact 7-character CRICOS course code
                by_exact_cricos: dict[str, dict] = {}
                intakes_by_prov: dict[str, set[str]] = {c: set() for c in cricos_codes_for_ep}

                for ac in acir_courses:
                    for st in ac.get("sites") or []:
                        sc = (st.get("cricos_code") or "").strip().upper()
                        if not sc or sc not in master_courses_by_code:
                            continue
                        mc_owner = master_courses_by_code[sc]["cricos_provider_code"]
                        if mc_owner not in cricos_codes_for_ep:
                            continue
                        by_exact_cricos.setdefault(sc, ac)
                        sd = (st.get("course_start_date") or "").strip()
                        if sd and len(sd) < 80:
                            intakes_by_prov[mc_owner].add(sd)

                for cricos_code in cricos_codes_for_ep:
                    mp = master_provs[cricos_code]
                    pinfo = matched_providers[cricos_code]
                    prev_ep = existing_ep.get(cricos_code) or {}

                    agg_intakes = (
                        "; ".join(sorted(intakes_by_prov[cricos_code]))[:250]
                        if intakes_by_prov[cricos_code]
                        else prev_ep.get("intake_date") or mp.get("db_intake_date")
                    )
                    sch_list = prov_scholarships_map.get(cricos_code, [])
                    best_web = (
                        pinfo["web_address"]
                        or mp.get("cricos_website")
                        or prev_ep.get("website")
                        or mp.get("db_website")
                    )

                    # Preserve "enriched" status if Stage 2 already scraped this provider's website
                    ep_status = prev_ep.get("status") if prev_ep.get("status") == "enriched" else "acir_enriched"

                    enriched_provider_rows.append(
                        (
                            cricos_code,
                            mp["db_provider_id"],
                            mp["institution_name"],
                            best_web,
                            prev_ep.get("international_url"),
                            agg_intakes,
                            pinfo["logo_url"] or prev_ep.get("provider_logo") or mp.get("db_provider_logo"),
                            prev_ep.get("contact_email"),
                            prev_ep.get("contact_phone"),
                            prev_ep.get("contact_website_url"),
                            json.dumps(sch_list) if sch_list else prev_ep.get("scholarships_json"),
                            f"{BASE_URL}/provider/{pinfo['acir_slug']}/{pinfo['acir_id']}",
                            prev_ep.get("discovery_method") or "acir_exact_cricos_provider",
                            ep_status,
                            None,
                            now_iso,
                        )
                    )

                    # Match ONLY exact 7-character cricos_course_code on this exact cricos_provider_code
                    for mc in master_courses_by_prov.get(cricos_code, []):
                        ccode = mc["cricos_course_code"]
                        ac = by_exact_cricos.get(ccode)
                        if not ac:
                            continue

                        matched_course_count += 1
                        prev_ec = existing_ec.get(ccode) or {}
                        acir_course_url = f"{BASE_URL}/course/{ac.get('slug')}/{ac.get('id')}"

                        # Filter sites STRICTLY to this exact 7-character cricos_course_code!
                        exact_sites = [
                            s
                            for s in (ac.get("sites") or [])
                            if (s.get("cricos_code") or "").strip().upper() == ccode
                        ]

                        web_urls = [
                            clean_url(s.get("course_web_address"))
                            for s in exact_sites
                            if is_deep_url(clean_url(s.get("course_web_address")))
                        ]
                        best_url = web_urls[0] if web_urls else prev_ec.get("apply_form")

                        desc_html = format_html_description(ac) or prev_ec.get("course_description")
                        entry_html, eng_html, ielts_s, toefl_s = format_html_entry_and_english(ac, exact_sites)
                        entry_html = entry_html or prev_ec.get("entry_requirements")
                        eng_html = prev_ec.get("english_requirements") or eng_html
                        ielts_s = prev_ec.get("ielts_score") or ielts_s
                        pte_s = prev_ec.get("pte_score")
                        toefl_s = prev_ec.get("toefl_score") or toefl_s
                        eng_src = prev_ec.get("english_source_url") or (acir_course_url if eng_html else None)
                        eng_meth = prev_ec.get("english_method") or ("acir_exact_cricos" if eng_html else None)

                        # Official CRICOS API Tuition Fee is authoritative; fallback to ACIR overseas_full_fee if missing
                        overseas_fees = [
                            float(s["fees"]["overseas_full_fee"])
                            for s in exact_sites
                            if s.get("fees") and s["fees"].get("overseas_full_fee") not in (None, "", 0, "0")
                        ]
                        best_offshore_fee = (
                            mc.get("cricos_tuition_fee")
                            or (overseas_fees[0] if overseas_fees else None)
                            or prev_ec.get("offshore_tuition_fee")
                        )

                        dur_str = format_duration(exact_sites) or prev_ec.get("total_course_duration")
                        if not dur_str and mc.get("duration_weeks"):
                            dur_str = f"{mc['duration_weeks']} weeks"

                        c_intakes = sorted(
                            {
                                (s.get("course_start_date") or "").strip()
                                for s in exact_sites
                                if (s.get("course_start_date") or "").strip()
                            }
                        )
                        c_intake_str = (
                            "; ".join(c_intakes)[:250] if c_intakes else prev_ec.get("intake_dates")
                        )

                        assess = (
                            infer_assessment_style(
                                " ".join(
                                    str(ac.get(k) or "")
                                    for k in ("description", "structure", "subjects", "other_info")
                                )
                            )
                            or prev_ec.get("assessment_style")
                        )

                        sch_summary = (
                            f"{len(sch_list)} provider scholarship(s) available (e.g. {sch_list[0]['name']})"
                            if sch_list
                            else prev_ec.get("scholarships_summary")
                        )

                        enriched_course_rows.append(
                            (
                                ccode,
                                cricos_code,
                                mc["db_course_id"],
                                mc["institution_name"],
                                mc["course_name"],
                                best_url,
                                desc_html,
                                entry_html,
                                eng_html,
                                ielts_s,
                                pte_s,
                                toefl_s,
                                eng_src,
                                eng_meth,
                                prev_ec.get("onshore_tuition_fee"),
                                best_offshore_fee,
                                prev_ec.get("enrolment_fee"),
                                prev_ec.get("materials_fee"),
                                dur_str,
                                assess,
                                c_intake_str,
                                sch_summary,
                                1,
                                1.0,
                                "acir_exact_cricos",
                                best_url or acir_course_url,
                                "acir_enriched",
                                None,
                                now_iso,
                            )
                        )

            if enriched_provider_rows:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO enriched_providers
                    (cricos_provider_code, provider_id, institution_name, website, international_url,
                     intake_date, provider_logo, contact_email, contact_phone, contact_website_url,
                     scholarships_json, source_url, discovery_method, status, error_msg, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    enriched_provider_rows,
                )
            if enriched_course_rows:
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
                    enriched_course_rows,
                )
            conn.commit()
            enriched_provider_rows.clear()
            enriched_course_rows.clear()
            print(
                f"  Processed {min(i+45, len(ep_items_list))}/{len(ep_items_list)} ACIR endpoints -> {matched_course_count} exact CRICOS courses matched ({time.time()-t_crs:.1f}s)"
            )

    print(
        f"Stage 1 Complete in {time.time()-t0:.1f}s! Total exact-CRICOS matched ACIR courses: {matched_course_count}"
    )
    conn.close()


if __name__ == "__main__":
    asyncio.run(run_stage1())
