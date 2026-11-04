"""
Stage 0: Official data.gov.au CRICOS Register Sync & Canonical Key Builder.

Fetches the live CKAN package:
  https://data.gov.au/data/api/3/action/package_show?id=e5ae7059-bfa8-4fa4-a5c0-c13cf3520193
Downloads the 4 official Department of Education CRICOS CSV exports:
  - CRICOS Institutions.csv (1,543 registered providers)
  - CRICOS Courses.csv (25,199 active + 432 expired courses)
  - CRICOS Locations.csv (3,855 campuses)
  - CRICOS Course Locations.csv (45,759 course-to-campus mappings)

Enforces STRICT CRICOS UNIQUE IDs (`cricos_provider_code` and `cricos_course_code`)
with ZERO fuzzy matching, repairs 6-digit leading-zero-stripped DB codes when both
`0 + code` and `cricos_provider_code` match the official register, and purges any
non-exact legacy course matches.
"""
import csv
import datetime as dt
import io
import os
import re
import sqlite3
import time

import httpx

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_DIR, "data", "dashboard_mirror.sqlite")
CKAN_PACKAGE_URL = "https://data.gov.au/data/api/3/action/package_show?id=e5ae7059-bfa8-4fa4-a5c0-c13cf3520193"


def parse_money(val: str | None) -> float | None:
    if not val:
        return None
    cleaned = re.sub(r"[^0-9.]", "", str(val))
    if not cleaned:
        return None
    try:
        num = float(cleaned)
        return num if num > 0 else None
    except ValueError:
        return None


def parse_int(val: str | None) -> int | None:
    if not val:
        return None
    cleaned = re.sub(r"[^0-9]", "", str(val))
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def clean_url(u: str | None) -> str | None:
    if not u:
        return None
    s = str(u).strip()
    if not s or s.upper() in ("NULL", "NONE", "N/A"):
        return None
    if not s.startswith(("http://", "https://")):
        s = "https://" + s.lstrip("/")
    return s


def download_csv_rows(client: httpx.Client, url: str) -> list[dict]:
    r = client.get(url)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def init_v2_tables(conn: sqlite3.Connection):
    conn.executescript(
        """
        DROP TABLE IF EXISTS cricos_api_meta;
        CREATE TABLE cricos_api_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        DROP TABLE IF EXISTS repaired_db_course_codes;
        CREATE TABLE repaired_db_course_codes (
            db_course_id INTEGER PRIMARY KEY,
            old_cricos_course_code TEXT NOT NULL,
            new_cricos_course_code TEXT NOT NULL,
            cricos_provider_code TEXT NOT NULL,
            course_name TEXT
        );

        DROP TABLE IF EXISTS db_expired_or_removed_courses;
        CREATE TABLE db_expired_or_removed_courses (
            db_course_id INTEGER PRIMARY KEY,
            cricos_course_code TEXT NOT NULL,
            cricos_provider_code TEXT NOT NULL,
            institution_name TEXT,
            course_name TEXT,
            reason TEXT NOT NULL
        );

        DROP TABLE IF EXISTS master_providers;
        CREATE TABLE master_providers (
            cricos_provider_code TEXT PRIMARY KEY,
            db_provider_id INTEGER,
            in_existing_db INTEGER DEFAULT 0,
            institution_name TEXT NOT NULL,
            trading_name TEXT,
            institution_type TEXT,
            institution_capacity INTEGER,
            cricos_website TEXT,
            db_website TEXT,
            db_provider_logo TEXT,
            db_intake_date TEXT,
            postal_address_line_1 TEXT,
            postal_address_line_2 TEXT,
            postal_address_line_3 TEXT,
            postal_address_line_4 TEXT,
            postal_address_city TEXT,
            postal_address_state TEXT,
            postal_address_postcode TEXT,
            is_partner INTEGER DEFAULT 0
        );

        DROP TABLE IF EXISTS master_courses;
        CREATE TABLE master_courses (
            cricos_course_code TEXT PRIMARY KEY,
            cricos_provider_code TEXT NOT NULL,
            db_course_id INTEGER,
            in_existing_db INTEGER DEFAULT 0,
            institution_name TEXT NOT NULL,
            course_name TEXT NOT NULL,
            vet_national_code TEXT,
            dual_qualification TEXT,
            board_field_1_education TEXT,
            narrow_field_1_education TEXT,
            detailed_field_1_education TEXT,
            board_field_2_education TEXT,
            narrow_field_2_education TEXT,
            detailed_field_2_education TEXT,
            course_level TEXT,
            foundation_studies TEXT,
            work_component TEXT,
            work_component_hours_per_week TEXT,
            work_component_weeks TEXT,
            work_component_total_hours TEXT,
            course_language TEXT,
            duration_weeks INTEGER,
            cricos_tuition_fee REAL,
            cricos_non_tuition_fee REAL,
            cricos_estimated_total_cost REAL,
            cricos_expired TEXT,
            db_course_description TEXT,
            db_entry_requirements TEXT,
            db_apply_form TEXT,
            db_total_course_duration TEXT,
            db_offshore_tuition_fee REAL,
            db_onshore_tuition_fee REAL,
            db_enrolment_fee REAL,
            db_materials_fee REAL,
            db_assessment_style TEXT,
            is_partner INTEGER DEFAULT 0
        );
        CREATE INDEX idx_master_courses_provider ON master_courses(cricos_provider_code);

        DROP TABLE IF EXISTS master_locations;
        CREATE TABLE master_locations (
            cricos_provider_code TEXT NOT NULL,
            institution_name TEXT NOT NULL,
            location_name TEXT NOT NULL,
            location_type TEXT,
            address_line_1 TEXT,
            address_line_2 TEXT,
            address_line_3 TEXT,
            address_line_4 TEXT,
            city TEXT,
            state TEXT,
            postcode TEXT,
            PRIMARY KEY (cricos_provider_code, location_name)
        );

        DROP TABLE IF EXISTS master_course_locations;
        CREATE TABLE master_course_locations (
            cricos_course_code TEXT NOT NULL,
            cricos_provider_code TEXT NOT NULL,
            institution_name TEXT NOT NULL,
            location_name TEXT NOT NULL,
            location_city TEXT,
            location_state TEXT,
            PRIMARY KEY (cricos_course_code, location_name)
        );
        """
    )


def migrate_enrichment_tables_to_cricos_pk(conn: sqlite3.Connection):
    """
    Ensure `enriched_providers` is strictly keyed by `cricos_provider_code PRIMARY KEY`
    and `enriched_courses` is strictly keyed by `cricos_course_code PRIMARY KEY`,
    preserving ONLY 100% CRICOS-verified records and purging any legacy fuzzy/title matches.
    """
    conn.row_factory = sqlite3.Row
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    old_ep = []
    if "enriched_providers" in tables:
        ep_info = list(conn.execute("PRAGMA table_info(enriched_providers)"))
        pk_col = next((r[1] for r in ep_info if r[5] == 1), None)
        if pk_col != "cricos_provider_code":
            old_ep = [dict(r) for r in conn.execute("SELECT * FROM enriched_providers")]
            conn.execute("DROP TABLE enriched_providers;")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS enriched_providers (
            cricos_provider_code TEXT PRIMARY KEY,
            provider_id INTEGER,
            institution_name TEXT NOT NULL,
            website TEXT,
            international_url TEXT,
            intake_date TEXT,
            provider_logo TEXT,
            contact_email TEXT,
            contact_phone TEXT,
            contact_website_url TEXT,
            scholarships_json TEXT,
            source_url TEXT,
            discovery_method TEXT,
            status TEXT DEFAULT 'pending',
            error_msg TEXT,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ep_v2_status ON enriched_providers(status);
        """
    )

    if old_ep:
        valid_mp = {
            r[0]: (r[1], r[2])
            for r in conn.execute("SELECT cricos_provider_code, db_provider_id, institution_name FROM master_providers")
        }
        to_ins = []
        for r in old_ep:
            code = (r.get("cricos_provider_code") or "").strip().upper()
            if code in valid_mp:
                db_pid, canonical_name = valid_mp[code]
                to_ins.append(
                    (
                        code,
                        db_pid,
                        canonical_name,
                        r.get("website"),
                        r.get("international_url"),
                        r.get("intake_date"),
                        r.get("provider_logo"),
                        r.get("contact_email"),
                        r.get("contact_phone"),
                        r.get("contact_website_url"),
                        r.get("scholarships_json"),
                        r.get("source_url"),
                        r.get("discovery_method"),
                        r.get("status"),
                        r.get("error_msg"),
                        r.get("updated_at"),
                    )
                )
        conn.executemany(
            """
            INSERT OR REPLACE INTO enriched_providers
            (cricos_provider_code, provider_id, institution_name, website, international_url,
             intake_date, provider_logo, contact_email, contact_phone, contact_website_url,
             scholarships_json, source_url, discovery_method, status, error_msg, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            to_ins,
        )
        print(f"  Migrated {len(to_ins)} existing verified providers to PK(cricos_provider_code).")

    old_ec = []
    if "enriched_courses" in tables:
        ec_info = list(conn.execute("PRAGMA table_info(enriched_courses)"))
        pk_col = next((r[1] for r in ec_info if r[5] == 1), None)
        if pk_col != "cricos_course_code":
            old_ec = [dict(r) for r in conn.execute("SELECT * FROM enriched_courses")]
            conn.execute("DROP TABLE enriched_courses;")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS enriched_courses (
            cricos_course_code TEXT PRIMARY KEY,
            cricos_provider_code TEXT NOT NULL,
            course_id INTEGER,
            institution_name TEXT NOT NULL,
            course_name TEXT NOT NULL,
            apply_form TEXT,
            course_description TEXT,
            entry_requirements TEXT,
            english_requirements TEXT,
            ielts_score TEXT,
            pte_score TEXT,
            toefl_score TEXT,
            english_source_url TEXT,
            english_method TEXT,
            onshore_tuition_fee REAL,
            offshore_tuition_fee REAL,
            enrolment_fee REAL,
            materials_fee REAL,
            total_course_duration TEXT,
            assessment_style TEXT,
            intake_dates TEXT,
            scholarships_summary TEXT,
            international_verified INTEGER DEFAULT 1,
            match_confidence REAL DEFAULT 1.0,
            match_method TEXT,
            source_url TEXT,
            status TEXT DEFAULT 'pending',
            error_msg TEXT,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ec_v2_provider ON enriched_courses(cricos_provider_code);
        CREATE INDEX IF NOT EXISTS idx_ec_v2_status ON enriched_courses(status);
        """
    )

    # Map repaired codes so exact matches on repaired codes also migrate cleanly
    repaired_map = {
        r[0]: r[1]
        for r in conn.execute("SELECT old_cricos_course_code, new_cricos_course_code FROM repaired_db_course_codes")
    }
    valid_mc = {
        r[0]: (r[1], r[2], r[3], r[4])
        for r in conn.execute(
            "SELECT cricos_course_code, cricos_provider_code, db_course_id, institution_name, course_name FROM master_courses"
        )
    }

    if old_ec:
        STRICT_EXACT_METHODS = {"acir_exact_cricos", "verified_cricos_on_course_page", "parsed_existing_db"}
        kept_ec = []
        purged_fuzzy = 0
        for r in old_ec:
            raw_code = (r.get("cricos_course_code") or "").strip().upper()
            code = repaired_map.get(raw_code, raw_code)
            pcode = (r.get("cricos_provider_code") or "").strip().upper()
            method = (r.get("match_method") or "").strip()

            if code not in valid_mc:
                continue
            mc_pcode, db_cid, mc_inst, mc_cname = valid_mc[code]
            if pcode != mc_pcode:
                purged_fuzzy += 1
                continue
            if method not in STRICT_EXACT_METHODS:
                purged_fuzzy += 1
                continue

            kept_ec.append(
                (
                    code,
                    mc_pcode,
                    db_cid,
                    mc_inst,
                    mc_cname,
                    r.get("apply_form"),
                    r.get("course_description"),
                    r.get("entry_requirements"),
                    r.get("english_requirements"),
                    r.get("ielts_score"),
                    r.get("pte_score"),
                    r.get("toefl_score"),
                    r.get("english_source_url"),
                    r.get("english_method"),
                    r.get("onshore_tuition_fee"),
                    r.get("offshore_tuition_fee"),
                    r.get("enrolment_fee"),
                    r.get("materials_fee"),
                    r.get("total_course_duration"),
                    r.get("assessment_style"),
                    r.get("intake_dates"),
                    r.get("scholarships_summary"),
                    1,
                    1.0,
                    method,
                    r.get("source_url"),
                    r.get("status"),
                    r.get("error_msg"),
                    r.get("updated_at"),
                )
            )
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
            kept_ec,
        )
        print(
            f"  Migrated {len(kept_ec)} strictly CRICOS-verified course records to PK(cricos_course_code) "
            f"(purged {purged_fuzzy} legacy fuzzy/prefix matches for zero-mismatch guarantee)."
        )


def run_stage0():
    t0 = time.time()
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")

    print(f"Fetching official CRICOS CKAN package metadata from data.gov.au...")
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        pkg = client.get(CKAN_PACKAGE_URL).json()["result"]
        resources = pkg.get("resources") or []

        res_urls = {}
        res_modified = {}
        for res in resources:
            nm = (res.get("name") or "").strip().lower()
            if nm == "cricos institutions.csv":
                res_urls["institutions"] = res["url"]
                res_modified["institutions"] = res.get("last_modified") or ""
            elif nm == "cricos courses.csv":
                res_urls["courses"] = res["url"]
                res_modified["courses"] = res.get("last_modified") or ""
            elif nm == "cricos locations.csv":
                res_urls["locations"] = res["url"]
                res_modified["locations"] = res.get("last_modified") or ""
            elif nm == "cricos course locations.csv":
                res_urls["course_locations"] = res["url"]
                res_modified["course_locations"] = res.get("last_modified") or ""

        missing_res = {"institutions", "courses", "locations", "course_locations"} - set(res_urls.keys())
        if missing_res:
            raise RuntimeError(f"Missing expected CRICOS CSV resources in CKAN response: {missing_res}")

        inst_rows = download_csv_rows(client, res_urls["institutions"])
        course_rows = download_csv_rows(client, res_urls["courses"])
        loc_rows = download_csv_rows(client, res_urls["locations"])
        cloc_rows = download_csv_rows(client, res_urls["course_locations"])

    init_v2_tables(conn)

    now_iso = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    conn.executemany(
        "INSERT INTO cricos_api_meta (key, value) VALUES (?, ?)",
        [
            ("package_url", CKAN_PACKAGE_URL),
            ("metadata_modified", pkg.get("metadata_modified") or ""),
            ("institutions_last_modified", res_modified["institutions"]),
            ("courses_last_modified", res_modified["courses"]),
            ("synced_at", now_iso),
        ],
    )

    # Load existing SQL dump tables if present
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    orig_provs = {}
    if "orig_provider_institution" in tables:
        for r in conn.execute("SELECT * FROM orig_provider_institution"):
            if r["cricos_provider_code"]:
                orig_provs[r["cricos_provider_code"].strip().upper()] = dict(r)

    orig_courses_by_code = {}
    orig_courses_all = []
    if "orig_courses" in tables:
        for r in conn.execute("SELECT * FROM orig_courses"):
            d = dict(r)
            orig_courses_all.append(d)
            if d.get("cricos_course_code"):
                orig_courses_by_code[d["cricos_course_code"].strip().upper()] = d

    # Index official CRICOS API Providers & Courses strictly by CRICOS Code
    api_provs = {}
    for r in inst_rows:
        pcode = (r.get("CRICOS Provider Code") or "").strip().upper()
        if pcode:
            api_provs[pcode] = r

    api_courses_all = {}
    api_courses_active = {}
    for r in course_rows:
        ccode = (r.get("CRICOS Course Code") or "").strip().upper()
        if not ccode:
            continue
        api_courses_all[ccode] = r
        if (r.get("Expired") or "").strip().lower() == "no":
            api_courses_active[ccode] = r

    # Step A: Repair 6-character stripped-leading-zero codes in orig_courses ONLY when BOTH
    # ('0' + code) exists in official CRICOS AND cricos_provider_code matches 100%!
    repaired_rows = []
    expired_or_removed_rows = []
    for d in orig_courses_all:
        cid = int(d["id"])
        raw_c = (d.get("cricos_course_code") or "").strip().upper()
        raw_p = (d.get("cricos_provider_code") or "").strip().upper()
        if not raw_c:
            continue

        if raw_c in api_courses_all:
            api_r = api_courses_all[raw_c]
            if (api_r.get("Expired") or "").strip().lower() == "yes":
                expired_or_removed_rows.append(
                    (cid, raw_c, raw_p, d.get("institution_name"), d.get("course_name"), "cricos_expired_yes")
                )
            continue

        if len(raw_c) == 6 and ("0" + raw_c) in api_courses_all and ("0" + raw_c) not in orig_courses_by_code:
            cand_c = "0" + raw_c
            api_r = api_courses_all[cand_c]
            api_p = (api_r.get("CRICOS Provider Code") or "").strip().upper()
            if api_p == raw_p:
                repaired_rows.append((cid, raw_c, cand_c, raw_p, d.get("course_name")))
                orig_courses_by_code[cand_c] = d
                if (api_r.get("Expired") or "").strip().lower() == "yes":
                    expired_or_removed_rows.append(
                        (cid, cand_c, raw_p, d.get("institution_name"), d.get("course_name"), "cricos_expired_yes")
                    )
                continue

        expired_or_removed_rows.append(
            (cid, raw_c, raw_p, d.get("institution_name"), d.get("course_name"), "removed_from_cricos_register")
        )

    conn.executemany(
        "INSERT INTO repaired_db_course_codes VALUES (?, ?, ?, ?, ?)",
        repaired_rows,
    )
    conn.executemany(
        "INSERT INTO db_expired_or_removed_courses VALUES (?, ?, ?, ?, ?, ?)",
        expired_or_removed_rows,
    )

    # Step B: Build master_providers (All 1,543 active CRICOS institutions)
    mp_rows = []
    for pcode, ar in sorted(api_provs.items()):
        op = orig_provs.get(pcode)
        mp_rows.append(
            (
                pcode,
                int(op["id"]) if op else None,
                1 if op else 0,
                (ar.get("Institution Name") or "").strip(),
                (ar.get("Trading Name") or "").strip() or (op.get("trading_name") if op else None),
                (ar.get("Institution Type") or "").strip(),
                parse_int(ar.get("Institution Capacity")),
                clean_url(ar.get("Website")),
                clean_url(op.get("website")) if op else None,
                op.get("provider_logo") if op else None,
                op.get("intake_date") if op else None,
                (ar.get("Postal Address Line 1") or "").strip() or None,
                (ar.get("Postal Address Line 2") or "").strip() or None,
                (ar.get("Postal Address Line 3") or "").strip() or None,
                (ar.get("Postal Address Line 4") or "").strip() or None,
                (ar.get("Postal Address City") or "").strip() or None,
                (ar.get("Postal Address State") or "").strip() or None,
                (ar.get("Postal Address Postcode") or "").strip() or None,
                int(op["is_partner"]) if (op and op.get("is_partner") is not None) else 0,
            )
        )
    conn.executemany(
        """
        INSERT INTO master_providers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        mp_rows,
    )

    # Step C: Build master_courses (All 25,199 active CRICOS courses)
    mc_rows = []
    for ccode, ar in sorted(api_courses_active.items()):
        pcode = (ar.get("CRICOS Provider Code") or "").strip().upper()
        oc = orig_courses_by_code.get(ccode)
        # Strict check: only inherit existing DB fields if the existing DB row belongs to the EXACT SAME cricos_provider_code!
        if oc and (oc.get("cricos_provider_code") or "").strip().upper() != pcode:
            oc = None

        wks = parse_int(ar.get("Duration (Weeks)"))
        mc_rows.append(
            (
                ccode,
                pcode,
                int(oc["id"]) if oc else None,
                1 if oc else 0,
                (ar.get("Institution Name") or "").strip(),
                (ar.get("Course Name") or "").strip(),
                (ar.get("VET National Code") or "").strip() or None,
                (ar.get("Dual Qualification") or "").strip() or None,
                (ar.get("Field of Education 1 Broad Field") or "").strip() or None,
                (ar.get("Field of Education 1 Narrow Field") or "").strip() or None,
                (ar.get("Field of Education 1 Detailed Field") or "").strip() or None,
                (ar.get("Field of Education 2 Broad Field") or "").strip() or None,
                (ar.get("Field of Education 2 Narrow Field") or "").strip() or None,
                (ar.get("Field of Education 2 Detailed Field") or "").strip() or None,
                (ar.get("Course Level") or "").strip() or None,
                (ar.get("Foundation Studies") or "").strip() or None,
                (ar.get("Work Component") or "").strip() or None,
                (ar.get("Work Component Hours/Week") or "").strip() or None,
                (ar.get("Work Component Weeks") or "").strip() or None,
                (ar.get("Work Component Total Hours") or "").strip() or None,
                (ar.get("Course Language") or "").strip() or None,
                wks,
                parse_money(ar.get("Tuition Fee")),
                parse_money(ar.get("Non Tuition Fee")),
                parse_money(ar.get("Estimated Total Course Cost")),
                "No",
                oc.get("course_description") if oc else None,
                oc.get("entry_requirements") if oc else None,
                clean_url(oc.get("apply_form")) if oc else None,
                oc.get("total_course_duration") if oc else None,
                parse_money(oc.get("offshore_tuition_fee")) if oc else None,
                parse_money(oc.get("onshore_tuition_fee")) if oc else None,
                parse_money(oc.get("enrolment_fee")) if oc else None,
                parse_money(oc.get("materials_fee")) if oc else None,
                oc.get("assessment_style") if oc else None,
                int(oc["is_partner"]) if (oc and oc.get("is_partner") is not None) else 0,
            )
        )
    conn.executemany(
        """
        INSERT INTO master_courses VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        mc_rows,
    )

    # Step D: Build master_locations & master_course_locations (active courses & providers only)
    ml_rows = []
    for r in loc_rows:
        pcode = (r.get("CRICOS Provider Code") or "").strip().upper()
        lname = (r.get("Location Name") or "").strip()
        if pcode in api_provs and lname:
            ml_rows.append(
                (
                    pcode,
                    (r.get("Institution Name") or "").strip(),
                    lname,
                    (r.get("Location Type") or "").strip() or None,
                    (r.get("Address Line 1") or "").strip() or "",
                    (r.get("Address Line 2") or "").strip() or None,
                    (r.get("Address Line 3") or "").strip() or None,
                    (r.get("Address Line 4") or "").strip() or None,
                    (r.get("City") or "").strip() or None,
                    (r.get("State") or "").strip() or None,
                    (r.get("Postcode") or "").strip() or None,
                )
            )
    conn.executemany(
        "INSERT OR REPLACE INTO master_locations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ml_rows,
    )

    mcl_rows = []
    for r in cloc_rows:
        ccode = (r.get("CRICOS Course Code") or "").strip().upper()
        pcode = (r.get("CRICOS Provider Code") or "").strip().upper()
        lname = (r.get("Location Name") or "").strip()
        if ccode in api_courses_active and lname:
            mcl_rows.append(
                (
                    ccode,
                    pcode,
                    (r.get("Institution Name") or "").strip(),
                    lname,
                    (r.get("Location City") or "").strip() or None,
                    (r.get("Location State") or "").strip() or None,
                )
            )
    conn.executemany(
        "INSERT OR REPLACE INTO master_course_locations VALUES (?, ?, ?, ?, ?, ?)",
        mcl_rows,
    )

    migrate_enrichment_tables_to_cricos_pk(conn)
    conn.commit()

    print(
        f"Stage 0 CRICOS API Sync complete in {time.time()-t0:.1f}s!\n"
        f"  master_providers (PK=cricos_provider_code): {len(mp_rows)} ({sum(1 for x in mp_rows if x[2]==1)} existing DB + {sum(1 for x in mp_rows if x[2]==0)} newly added)\n"
        f"  master_courses (PK=cricos_course_code):     {len(mc_rows)} ({sum(1 for x in mc_rows if x[3]==1)} existing DB + {sum(1 for x in mc_rows if x[3]==0)} newly added)\n"
        f"  repaired_db_course_codes (leading '0'):     {len(repaired_rows)}\n"
        f"  db_expired_or_removed_courses flagged:      {len(expired_or_removed_rows)}\n"
        f"  master_locations:                           {len(ml_rows)}\n"
        f"  master_course_locations:                    {len(mcl_rows)}"
    )
    conn.close()


if __name__ == "__main__":
    run_stage0()
