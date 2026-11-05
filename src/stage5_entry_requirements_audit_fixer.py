"""
Stage 5: Forensic Entry & English Requirements Audit & Re-Extraction Engine.

Repairs all historical extraction defects across all 25,199 active CRICOS courses BEFORE
final v4 sanitization:
  - Step 5A: Re-extracts decimal-accurate IELTS/PTE/TOEFL & clean Academic prerequisites
             from untouched raw `orig_courses.entry_requirements` HTML (~5,078 rows).
  - Step 5B: Re-applies explicit statutory prerequisite bullets from official `training.gov.au`
             Training Package `EntryRequirements` across all distinct `vet_national_code`s.
  - Step 5C: Concurrently re-fetches verified `apply_form` & `english_source_url` HTML/PDF pages
             (with `<nav>`, `<header>`, `<footer>`, `<aside>` stripped FIRST and PDF line-wraps
             unwrapped) for every course whose sub-band decimal was chopped, whose English score
             failed level/concordance rules, or whose `<nav>` links previously crowded out real
             `<main>` Academic Entry Requirements.
"""
from __future__ import annotations

import asyncio
import collections
import datetime as dt
import os
import re
import sqlite3
import sys
import time
from urllib.parse import urlparse

import httpx
import pymupdf

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from entry_english_rules import (
    extract_academic_entry_html_v2,
    extract_english_tests_v2,
    has_chopped_decimal_subband,
    html_to_clean_texts,
    unwrap_pdf_and_ligatures,
    validate_and_reconcile_english,
)
from stage2_playwright_enricher import is_deep_url
from stage3_entry_english_enricher import bucket_for_course, parse_policy_page_by_level
from stage4_deep_pdf_web_enricher import (
    HEADERS,
    DeepEnricherEngine,
    format_tga_entry_requirements,
)
from verify_and_sanitize import (
    _extract_academic_bullets_from_entry_html,
    rebuild_english_html,
    sanitize_course_url,
    sanitize_entry_html,
)

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_DIR, "data", "dashboard_mirror.sqlite")


def _needs_english_repair(row: dict) -> bool:
    ie = row.get("ielts_score")
    pt = row.get("pte_score")
    tf = row.get("toefl_score")
    if not (ie or pt or tf):
        return True
    if ie and (
        has_chopped_decimal_subband(ie)
        or bool(
            re.search(
                r"(?:less\s+than|below|<|minimum(?:\s+of)?|each(?:\s+band)?)\s*:?\s*(?:a\s+)?[45678]\s*$",
                ie.strip(),
                re.I,
            )
        )
    ):
        return True
    v_ie, v_pt, v_tf = validate_and_reconcile_english(
        ie,
        pt,
        tf,
        course_level=row.get("course_level") or "",
        course_name=row.get("course_name") or "",
        emeth=row.get("english_method") or "",
        esrc=row.get("english_source_url") or "",
    )
    return (v_ie != ie) or (v_pt != pt) or (v_tf != tf)


def _has_clean_academic_bullets(row: dict) -> bool:
    san = sanitize_entry_html(
        row.get("entry_requirements"),
        None,
        course_level=row.get("course_level") or "",
        course_name=row.get("course_name") or "",
        duration_weeks=row.get("duration_weeks"),
    )
    return bool(_extract_academic_bullets_from_entry_html(san))


# ==============================================================================
# STEP 5A: Re-extract from untouched raw `orig_courses` HTML
# ==============================================================================
def step_5a_reextract_orig_courses(conn: sqlite3.Connection):
    t0 = time.time()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "orig_courses" not in tables:
        print("\n[Stage 5A] `orig_courses` table not present; skipping local raw re-extraction.")
        return

    rows = conn.execute(
        """
        SELECT ec.cricos_course_code, ec.course_name, ec.entry_requirements,
               ec.english_requirements, ec.ielts_score, ec.pte_score, ec.toefl_score,
               ec.english_source_url, ec.english_method, ec.apply_form, ec.source_url,
               mc.course_level, mc.duration_weeks,
               oc.entry_requirements AS raw_orig_entry
        FROM enriched_courses ec
        JOIN master_courses mc ON ec.cricos_course_code = mc.cricos_course_code
        JOIN orig_courses oc ON UPPER(TRIM(oc.cricos_course_code)) = ec.cricos_course_code
        WHERE oc.entry_requirements IS NOT NULL AND TRIM(oc.entry_requirements) != ''
        """
    ).fetchall()

    now_iso = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updates = []
    restored_eng = 0
    restored_acad = 0

    for r in rows:
        d = dict(r)
        raw_h = d["raw_orig_entry"]
        eng_txt, main_txt = html_to_clean_texts(raw_h)
        clvl = d["course_level"] or ""
        cname = d["course_name"] or ""
        wks = d["duration_weeks"]

        cur_ie, cur_pt, cur_tf = d["ielts_score"], d["pte_score"], d["toefl_score"]
        cur_esrc = d["english_source_url"]
        cur_emeth = d["english_method"]

        if _needs_english_repair(d):
            ex_eng = extract_english_tests_v2(eng_txt)
            v_ie, v_pt, v_tf = validate_and_reconcile_english(
                ex_eng.get("ielts"),
                ex_eng.get("pte"),
                ex_eng.get("toefl"),
                course_level=clvl,
                course_name=cname,
                emeth="parsed_from_verified_entry_requirements",
                esrc=d["source_url"] or "",
            )
            if v_ie or v_pt or v_tf:
                cur_ie, cur_pt, cur_tf = v_ie, v_pt, v_tf
                cur_esrc = cur_esrc or d["apply_form"] or d["source_url"]
                cur_emeth = cur_emeth or "parsed_from_verified_entry_requirements"
                restored_eng += 1

        eng_html = rebuild_english_html(cur_ie, cur_pt, cur_tf)

        cur_entry = d["entry_requirements"]
        if not _has_clean_academic_bullets(d):
            acad_cand = extract_academic_entry_html_v2(
                main_txt, course_level=clvl, course_name=cname, duration_weeks=wks
            )
            if not acad_cand:
                acad_cand = sanitize_entry_html(
                    raw_h, None, course_level=clvl, course_name=cname, duration_weeks=wks
                )
            if acad_cand:
                cur_entry = sanitize_entry_html(
                    acad_cand, eng_html, course_level=clvl, course_name=cname, duration_weeks=wks
                )
                restored_acad += 1
        elif eng_html:
            cur_entry = sanitize_entry_html(
                cur_entry, eng_html, course_level=clvl, course_name=cname, duration_weeks=wks
            )

        if (
            cur_ie != d["ielts_score"]
            or cur_pt != d["pte_score"]
            or cur_tf != d["toefl_score"]
            or cur_entry != d["entry_requirements"]
            or eng_html != d["english_requirements"]
        ):
            updates.append(
                (
                    cur_entry,
                    eng_html,
                    cur_ie,
                    cur_pt,
                    cur_tf,
                    cur_esrc,
                    cur_emeth,
                    now_iso,
                    d["cricos_course_code"],
                )
            )

    if updates:
        conn.executemany(
            """
            UPDATE enriched_courses
            SET entry_requirements = ?,
                english_requirements = ?,
                ielts_score = ?,
                pte_score = ?,
                toefl_score = ?,
                english_source_url = ?,
                english_method = ?,
                updated_at = ?
            WHERE cricos_course_code = ?
            """,
            updates,
        )
        conn.commit()
    print(
        f"\n[Stage 5A Complete] Re-parsed {len(rows)} raw `orig_courses` HTML blobs -> "
        f"restored {restored_eng} decimal-accurate English rows & {restored_acad} clean Academic rows ({time.time()-t0:.1f}s)."
    )


# ==============================================================================
# STEP 5B: Official `training.gov.au` Statutory VET Prerequisite Re-Pass
# ==============================================================================
async def step_5b_tga_statutory_entry(conn: sqlite3.Connection):
    t0 = time.time()
    rows = [
        dict(r)
        for r in conn.execute(
            """
            SELECT mc.cricos_course_code, mc.vet_national_code, mc.course_level, mc.duration_weeks,
                   ec.course_name, ec.entry_requirements, ec.english_requirements, ec.source_url
            FROM master_courses mc
            JOIN enriched_courses ec ON mc.cricos_course_code = ec.cricos_course_code
            WHERE NULLIF(mc.vet_national_code, '') IS NOT NULL
            """
        )
    ]
    need_rows = [r for r in rows if not _has_clean_academic_bullets(r)]
    distinct_vcodes = sorted({r["vet_national_code"].strip().upper() for r in need_rows if r["vet_national_code"]})
    print(
        f"\n[Stage 5B] Checking {len(distinct_vcodes)} distinct VET codes on official training.gov.au "
        f"for {len(need_rows)} VET courses needing clean statutory Academic Entry Requirements..."
    )
    if not distinct_vcodes:
        return

    vcode_entry: dict[str, str] = {}
    sem = asyncio.Semaphore(28)

    async with httpx.AsyncClient(
        headers={"User-Agent": HEADERS["User-Agent"], "Accept": "application/json"},
        follow_redirects=True,
        timeout=12.0,
        verify=False,
        limits=httpx.Limits(max_connections=35, max_keepalive_connections=20),
    ) as client:

        async def fetch_one(vcode: str):
            async with sem:
                try:
                    r1 = await client.get(f"https://training.gov.au/api/training/{vcode}?api-version=1.0&include=all")
                    if r1.status_code != 200:
                        return
                    d1 = r1.json()
                    if (d1.get("code") or "").upper() != vcode or not d1.get("releases"):
                        return
                    rel_num = d1["releases"][0].get("releaseNumber", 1)
                    r2 = await client.get(
                        f"https://training.gov.au/api/training/{vcode}/releases/{rel_num}?include=All&api-version=1.0"
                    )
                    if r2.status_code != 200:
                        return
                    cbs = r2.json().get("contentBundles") or []
                    if not cbs or not cbs[0].get("id"):
                        return
                    cb_r = await client.get(f"https://training.gov.au/api/content/bundle/{cbs[0]['id']}")
                    if cb_r.status_code != 200:
                        return
                    for item in cb_r.json().get("items") or []:
                        if item.get("contentType") == "EntryRequirements":
                            ent_h = format_tga_entry_requirements(item.get("content"))
                            if ent_h:
                                vcode_entry[vcode] = ent_h
                            break
                except Exception:
                    pass

        await asyncio.gather(*[fetch_one(vc) for vc in distinct_vcodes])

    now_iso = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updates = []
    for r in need_rows:
        vc = r["vet_national_code"].strip().upper()
        tga_ent = vcode_entry.get(vc)
        if not tga_ent:
            continue
        merged = sanitize_entry_html(
            tga_ent,
            r["english_requirements"],
            course_level=r["course_level"] or "",
            course_name=r["course_name"] or "",
            duration_weeks=r["duration_weeks"],
        )
        if merged and _extract_academic_bullets_from_entry_html(merged):
            src_u = r["source_url"]
            if not src_u or src_u == "https://cricos.education.gov.au/":
                src_u = f"https://training.gov.au/Training/Details/{vc}"
            updates.append((merged, src_u, now_iso, r["cricos_course_code"]))

    if updates:
        conn.executemany(
            """
            UPDATE enriched_courses
            SET entry_requirements = ?, source_url = ?, updated_at = ?
            WHERE cricos_course_code = ?
            """,
            updates,
        )
        conn.commit()
    print(
        f"  [Stage 5B Complete] Found statutory entry requirements on {len(vcode_entry)} VET codes -> "
        f"updated {len(updates)} VET courses ({time.time()-t0:.1f}s)."
    )


# ==============================================================================
# STEP 5C: Live DOM-Cleaned Re-Extraction Across Verified Course & Policy URLs
# ==============================================================================
async def step_5c_live_reextract(conn: sqlite3.Connection):
    t0 = time.time()
    all_rows = [
        dict(r)
        for r in conn.execute(
            """
            SELECT ec.cricos_course_code, ec.cricos_provider_code, ec.course_name,
                   ec.apply_form, ec.entry_requirements, ec.english_requirements,
                   ec.ielts_score, ec.pte_score, ec.toefl_score,
                   ec.english_source_url, ec.english_method, ec.source_url,
                   mc.course_level, mc.duration_weeks
            FROM enriched_courses ec
            JOIN master_courses mc ON ec.cricos_course_code = mc.cricos_course_code
            """
        )
    ]

    # Map every URL -> courses needing English repair and/or Academic `<main>` re-extraction
    url_to_courses: dict[str, list[dict]] = collections.defaultdict(list)
    for r in all_rows:
        need_eng = _needs_english_repair(r)
        need_acad = not _has_clean_academic_bullets(r)
        if not need_eng and not need_acad:
            continue

        c_url = sanitize_course_url(r.get("apply_form"))
        e_url = sanitize_course_url(r.get("english_source_url"), allow_pdf=True)

        # Prefer specific course page `apply_form` when available; also include `english_source_url` if English needs repair
        added_any = False
        if c_url and is_deep_url(c_url):
            url_to_courses[c_url].append(r)
            added_any = True
        if e_url and e_url != c_url and (need_eng or not added_any) and is_deep_url(e_url):
            url_to_courses[e_url].append(r)

    unique_urls = list(url_to_courses.keys())
    print(
        f"\n[Stage 5C] Re-fetching {len(unique_urls)} unique verified course & policy URLs "
        f"(DOM `<nav>`/`<header>`/`<footer>` stripped + decimal-preserving IELTS)..."
    )
    if not unique_urls:
        return

    # Accumulate best repaired values keyed by cricos_course_code (course URL beats shared policy URL)
    repaired_by_cc: dict[str, dict] = {}
    pw_domain_counts: dict[str, int] = collections.Counter()

    async with DeepEnricherEngine(pw_concurrency=10, http_concurrency=60) as engine:

        async def process_url(url: str):
            linked_courses = url_to_courses[url]
            is_pdf = url.lower().split("?")[0].endswith(".pdf")
            is_shared_policy = len(linked_courses) >= 3 and not all(
                sanitize_course_url(c.get("apply_form")) == url for c in linked_courses
            )

            eng_txt = ""
            main_txt = ""
            final_u = url

            if is_pdf:
                pages = await engine.fetch_pdf_pages(url)
                if not pages:
                    return
                joined_pdf = "\n\n".join(pages[:45])
                eng_txt = joined_pdf
                main_txt = joined_pdf
            else:
                dom = urlparse(url).netloc.lower()
                use_pw = pw_domain_counts[dom] < 12
                h, _, f_u = await engine.fetch_html(
                    url, allow_pw=use_pw, expand_tabs=use_pw, require_ielts_or_pw=False
                )
                if use_pw:
                    pw_domain_counts[dom] += 1
                if not h:
                    return
                final_u = sanitize_course_url(f_u, allow_pdf=True) or url
                eng_txt, main_txt = html_to_clean_texts(h)

            if len(eng_txt) < 120:
                return

            pol_by_lvl = parse_policy_page_by_level(eng_txt) if is_shared_policy else {}
            direct_eng = extract_english_tests_v2(eng_txt) if not is_shared_policy else {}

            for r in linked_courses:
                cc = r["cricos_course_code"]
                clvl = r["course_level"] or ""
                cname = r["course_name"] or ""
                wks = r["duration_weeks"]
                bkt = bucket_for_course(clvl, cname)

                cand_ie = cand_pt = cand_tf = None
                cand_emeth = None
                cand_acad = None

                if is_shared_policy:
                    pol = pol_by_lvl.get(bkt) or pol_by_lvl.get("general")
                    if pol and pol.get("eng"):
                        em = f"pdf_institution_policy_{bkt}" if is_pdf else f"institution_official_policy_{bkt}"
                        cand_ie, cand_pt, cand_tf = validate_and_reconcile_english(
                            pol["eng"].get("ielts"),
                            pol["eng"].get("pte"),
                            pol["eng"].get("toefl"),
                            course_level=clvl,
                            course_name=cname,
                            emeth=em,
                            esrc=final_u,
                        )
                        if cand_ie or cand_pt or cand_tf:
                            cand_emeth = em
                    if pol and pol.get("acad"):
                        cand_acad = sanitize_entry_html(
                            pol["acad"], None, course_level=clvl, course_name=cname, duration_weeks=wks
                        )
                else:
                    # Course-specific page or course-specific PDF!
                    src_for_cc = eng_txt
                    if is_pdf and cc.upper() in eng_txt.upper():
                        idx_pos = eng_txt.upper().find(cc.upper())
                        src_for_cc = eng_txt[max(0, idx_pos - 1200) : idx_pos + 2600]
                        c_ex = extract_english_tests_v2(src_for_cc)
                    else:
                        c_ex = direct_eng

                    em = "pdf_course_cricos_verified" if is_pdf else "course_page_cricos_verified"
                    cand_ie, cand_pt, cand_tf = validate_and_reconcile_english(
                        c_ex.get("ielts"),
                        c_ex.get("pte"),
                        c_ex.get("toefl"),
                        course_level=clvl,
                        course_name=cname,
                        emeth=em,
                        esrc=final_u,
                    )
                    if cand_ie or cand_pt or cand_tf:
                        cand_emeth = em

                    acad_text_src = src_for_cc if is_pdf else main_txt
                    cand_acad = extract_academic_entry_html_v2(
                        acad_text_src,
                        course_level=clvl,
                        course_name=cname,
                        duration_weeks=wks,
                    )

                if not (cand_ie or cand_pt or cand_tf or cand_acad):
                    continue

                prev = repaired_by_cc.get(cc, {})
                is_course_url = not is_shared_policy
                # Course-specific URL takes precedence over shared policy URL
                if cand_ie or cand_pt or cand_tf:
                    if not prev.get("ie") or is_course_url:
                        prev["ie"], prev["pt"], prev["tf"] = cand_ie, cand_pt, cand_tf
                        prev["esrc"] = final_u
                        prev["emeth"] = cand_emeth
                if cand_acad:
                    if not prev.get("acad") or is_course_url:
                        prev["acad"] = cand_acad
                repaired_by_cc[cc] = prev

        chunk_size = 300
        for i in range(0, len(unique_urls), chunk_size):
            batch = unique_urls[i : i + chunk_size]
            await asyncio.gather(*[process_url(u) for u in batch])
            print(
                f"  [5C Progress] {min(i + chunk_size, len(unique_urls))}/{len(unique_urls)} URLs fetched | "
                f"{len(repaired_by_cc)} courses recovered | {time.time()-t0:.1f}s"
            )

    now_iso = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row_by_cc = {r["cricos_course_code"]: r for r in all_rows}
    db_updates = []
    eng_fixed = 0
    acad_fixed = 0

    for cc, rep in repaired_by_cc.items():
        r = row_by_cc[cc]
        clvl = r["course_level"] or ""
        cname = r["course_name"] or ""
        wks = r["duration_weeks"]

        cur_ie, cur_pt, cur_tf = r["ielts_score"], r["pte_score"], r["toefl_score"]
        cur_esrc = r["english_source_url"]
        cur_emeth = r["english_method"]

        if _needs_english_repair(r) and (rep.get("ie") or rep.get("pt") or rep.get("tf")):
            cur_ie = rep["ie"]
            cur_pt = rep["pt"]
            cur_tf = rep["tf"]
            cur_esrc = rep["esrc"]
            cur_emeth = rep["emeth"]
            eng_fixed += 1

        eng_html = rebuild_english_html(cur_ie, cur_pt, cur_tf)
        cur_entry = r["entry_requirements"]

        if not _has_clean_academic_bullets(r) and rep.get("acad"):
            cur_entry = sanitize_entry_html(
                rep["acad"], eng_html, course_level=clvl, course_name=cname, duration_weeks=wks
            )
            acad_fixed += 1
        elif eng_html:
            cur_entry = sanitize_entry_html(
                cur_entry, eng_html, course_level=clvl, course_name=cname, duration_weeks=wks
            )

        db_updates.append(
            (
                cur_entry,
                eng_html,
                cur_ie,
                cur_pt,
                cur_tf,
                cur_esrc,
                cur_emeth,
                now_iso,
                cc,
            )
        )

    if db_updates:
        conn.executemany(
            """
            UPDATE enriched_courses
            SET entry_requirements = ?,
                english_requirements = ?,
                ielts_score = ?,
                pte_score = ?,
                toefl_score = ?,
                english_source_url = ?,
                english_method = ?,
                updated_at = ?
            WHERE cricos_course_code = ?
            """,
            db_updates,
        )
        conn.commit()
    print(
        f"  [Stage 5C Complete] Recovered {eng_fixed} live English requirements & "
        f"{acad_fixed} clean `<main>` Academic Entry Requirements ({time.time()-t0:.1f}s)."
    )


async def run_stage5():
    t0 = time.time()
    print("==========================================================================")
    print("STAGE 5: FORENSIC ENTRY & ENGLISH REQUIREMENTS AUDIT & RE-EXTRACTION")
    print("==========================================================================")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    step_5a_reextract_orig_courses(conn)
    await step_5b_tga_statutory_entry(conn)
    await step_5c_live_reextract(conn)

    conn.close()
    print(f"\n[Stage 5 Finished in {time.time()-t0:.1f}s] Ready for v4 Zero-Defect Sanitization & Verification!")


if __name__ == "__main__":
    asyncio.run(run_stage5())
