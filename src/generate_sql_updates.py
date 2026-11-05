"""
Generates transactional, idempotent MariaDB/MySQL UPSERT (.sql) patch files
strictly keyed by `cricos_provider_code` and `cricos_course_code` (`UNIQUE INDEX` +
`INSERT ... ON DUPLICATE KEY UPDATE`).

Covers:
  1. Repairing 214 6-digit stripped-leading-zero `cricos_course_code`s (`100033` -> `0100033`)
  2. Flagging 1,333 expired/deregistered DB courses (`is_expired = 1`)
  3. Adding strict `UNIQUE INDEX` constraints on `cricos_provider_code` and `cricos_course_code`
  4. Upserting all 1,543 CRICOS providers + International Contacts
  5. Upserting all 25,199 active CRICOS courses (official CRICOS fields + scraped International fields)
  6. Upserting all 3,855 CRICOS campus locations & 44,878 active course-location mappings
  7. Inserting 1,063 strictly CRICOS-matched International Scholarships

Automatically splits into `<42 MB` parts if needed so GitHub and phpMyAdmin handle them effortlessly.
"""
import csv
import datetime as dt
import json
import os
import sqlite3

from export_review_sheets import load_courses_jsonl

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_DIR, "data", "dashboard_mirror.sqlite")
EXPORT_DIR = os.path.join(PROJECT_DIR, "exports")
MAX_SQL_PART_BYTES = 42 * 1024 * 1024


def sql_escape(val) -> str:
    if val is None or val == "":
        return "NULL"
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, (int, float)):
        return str(val)
    s = str(val)
    s = (
        s.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\0", "")
    )
    return f"'{s}'"


def sql_num(val) -> str:
    if val in (None, ""):
        return "NULL"
    try:
        return str(float(val))
    except (ValueError, TypeError):
        return "NULL"


def sql_int(val, default: str = "NULL") -> str:
    if val in (None, ""):
        return default
    try:
        return str(int(float(val)))
    except (ValueError, TypeError):
        return default


def load_csv_by_key(csv_path: str, key_col: str | None = None) -> dict | list:
    if not os.path.exists(csv_path):
        return {} if key_col else []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if key_col:
        return {str(r[key_col]).strip().upper(): r for r in rows if r.get(key_col)}
    return rows


def write_sql_chunks(base_stem: str, header_lines: list[str], statements: list[str]) -> list[str]:
    for old_f in os.listdir(EXPORT_DIR):
        if old_f.startswith(base_stem) and old_f.endswith(".sql"):
            try:
                os.remove(os.path.join(EXPORT_DIR, old_f))
            except OSError:
                pass

    full_header = "\n".join(header_lines) + "\nSTART TRANSACTION;\n\n"
    footer = "\nCOMMIT;\n"

    total_est = len(full_header.encode("utf-8")) + sum(len(s.encode("utf-8")) + 1 for s in statements) + len(footer.encode("utf-8"))
    if total_est <= MAX_SQL_PART_BYTES:
        out_p = os.path.join(EXPORT_DIR, f"{base_stem}.sql")
        with open(out_p, "w", encoding="utf-8") as f:
            f.write(full_header)
            f.write("\n".join(statements))
            f.write(footer)
        return [out_p]

    written = []
    part = 1
    cur_stmts = []
    cur_bytes = len(full_header.encode("utf-8")) + len(footer.encode("utf-8"))

    for st in statements:
        b = len(st.encode("utf-8")) + 1
        if cur_stmts and (cur_bytes + b > MAX_SQL_PART_BYTES):
            out_p = os.path.join(EXPORT_DIR, f"{base_stem}_part{part}.sql")
            part_hdr = full_header if part == 1 else "SET NAMES utf8mb4;\nSTART TRANSACTION;\n\n"
            with open(out_p, "w", encoding="utf-8") as f:
                f.write(part_hdr)
                f.write("\n".join(cur_stmts))
                f.write(footer)
            written.append(out_p)
            part += 1
            cur_stmts = []
            cur_bytes = 64
        cur_stmts.append(st)
        cur_bytes += b

    if cur_stmts:
        out_p = os.path.join(EXPORT_DIR, f"{base_stem}_part{part}.sql")
        part_hdr = full_header if part == 1 else "SET NAMES utf8mb4;\nSTART TRANSACTION;\n\n"
        with open(out_p, "w", encoding="utf-8") as f:
            f.write(part_hdr)
            f.write("\n".join(cur_stmts))
            f.write(footer)
        written.append(out_p)

    return written


def generate_sql():
    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    prov_json = os.path.join(EXPORT_DIR, "review_providers_international.json")
    prov_csv = os.path.join(EXPORT_DIR, "review_providers_international.csv")
    verified_course_csv = os.path.join(EXPORT_DIR, "review_courses_international_verified.csv")
    clean_course_csv = os.path.join(EXPORT_DIR, "review_courses_international_clean.csv")
    if os.path.exists(verified_course_csv) and (
        not os.path.exists(clean_course_csv)
        or os.path.getmtime(verified_course_csv) >= os.path.getmtime(clean_course_csv)
    ):
        clean_course_csv = verified_course_csv
    sch_csv = os.path.join(EXPORT_DIR, "review_scholarships_international.csv")

    header_lines = [
        "-- =====================================================================",
        f"-- Full CRICOS International Student Register & Enrichment Patch ({now_str})",
        "-- Target Database: u627422877_dashboard (MariaDB / MySQL)",
        "-- Unique Keys Enforced: cricos_provider_code & cricos_course_code",
        "-- =====================================================================",
        "SET NAMES utf8mb4;",
        "ALTER TABLE `courses` ADD COLUMN IF NOT EXISTS `english_requirements` TEXT DEFAULT NULL AFTER `entry_requirements`;",
        "ALTER TABLE `courses` ADD COLUMN IF NOT EXISTS `intakes` TEXT DEFAULT NULL AFTER `total_course_duration`;",
        "ALTER TABLE `courses` ADD COLUMN IF NOT EXISTS `cricos_non_tuition_fee` DOUBLE DEFAULT NULL AFTER `offshore_tuition_fee`;",
        "ALTER TABLE `courses` ADD COLUMN IF NOT EXISTS `cricos_estimated_total_cost` DOUBLE DEFAULT NULL AFTER `cricos_non_tuition_fee`;",
        "ALTER TABLE `courses` ADD COLUMN IF NOT EXISTS `is_expired` TINYINT(1) NOT NULL DEFAULT 0 AFTER `is_partner`;",
    ]

    stmts = []

    # 1. Repair 6-digit leading-zero-stripped CRICOS course codes BEFORE adding UNIQUE index
    repaired = [dict(r) for r in conn.execute("SELECT * FROM repaired_db_course_codes")]
    if repaired:
        stmts.append("-- 1. Repair 6-character stripped-leading-zero CRICOS course codes")
        for r in repaired:
            stmts.append(
                f"UPDATE `courses` SET `cricos_course_code` = {sql_escape(r['new_cricos_course_code'])}, `updated_at` = {sql_escape(now_str)} "
                f"WHERE `cricos_course_code` = {sql_escape(r['old_cricos_course_code'])} AND `cricos_provider_code` = {sql_escape(r['cricos_provider_code'])};"
            )

    # 2. Flag expired / deregistered courses
    expired_codes = [r[0] for r in conn.execute("SELECT cricos_course_code FROM db_expired_or_removed_courses")]
    if expired_codes:
        stmts.append("-- 2. Flag expired / deregistered CRICOS courses (is_expired = 1)")
        for i in range(0, len(expired_codes), 250):
            chunk = ", ".join(sql_escape(c) for c in expired_codes[i : i + 250])
            stmts.append(f"UPDATE `courses` SET `is_expired` = 1 WHERE `cricos_course_code` IN ({chunk});")

    # 3. Add strict UNIQUE indexes on cricos_provider_code, cricos_course_code, locations, course_locations
    stmts.extend(
        [
            "-- 3. Enforce Unique CRICOS Identifiers",
            "ALTER TABLE `provider_institution` ADD UNIQUE INDEX IF NOT EXISTS `uq_cricos_provider_code` (`cricos_provider_code`);",
            "ALTER TABLE `courses` ADD UNIQUE INDEX IF NOT EXISTS `uq_cricos_course_code` (`cricos_course_code`);",
            "ALTER TABLE `locations` ADD UNIQUE INDEX IF NOT EXISTS `uq_loc_cricos_name` (`cricos_provider_code`(32), `location_name`(191));",
            "ALTER TABLE `course_locations` ADD UNIQUE INDEX IF NOT EXISTS `uq_cloc_cricos_name` (`cricos_course_code`(32), `location_name`(191));",
        ]
    )

    # 4. Upsert all 1,543 CRICOS Providers + International Contacts
    mp_lookup = {r["cricos_provider_code"]: dict(r) for r in conn.execute("SELECT * FROM master_providers")}
    provs = json.load(open(prov_json, encoding="utf-8"))
    csv_provs = load_csv_by_key(prov_csv, "cricos_provider_code")

    prov_upserts = 0
    contact_inserts = 0
    prov_values = []
    contact_stmts = []

    for p in provs:
        pcode = p["cricos_provider_code"].strip().upper()
        cp = csv_provs.get(pcode, p)
        if str(cp.get("approve_upsert", p.get("approve_upsert", "YES"))).strip().upper() != "YES":
            continue
        mp = mp_lookup.get(pcode, {})
        web = (cp.get("new_website") if "new_website" in cp else p.get("new_website")) or None
        intake = (cp.get("new_intake_date") if "new_intake_date" in cp else p.get("new_intake_date")) or None
        logo = (cp.get("new_provider_logo") if "new_provider_logo" in cp else p.get("new_provider_logo")) or None

        prov_values.append(
            f"(1, {sql_escape(logo[:250] if logo else None)}, {sql_escape(pcode)}, "
            f"{sql_escape((mp.get('trading_name') or '')[:250] or None)}, "
            f"{sql_escape(mp.get('institution_name', p['institution_name'])[:250])}, "
            f"{sql_escape(mp.get('institution_type'))}, {sql_int(mp.get('institution_capacity'))}, "
            f"{sql_escape(web[:250] if web else None)}, "
            f"{sql_escape(mp.get('postal_address_line_1'))}, {sql_escape(mp.get('postal_address_line_2'))}, "
            f"{sql_escape(mp.get('postal_address_line_3'))}, {sql_escape(mp.get('postal_address_line_4'))}, "
            f"{sql_escape(mp.get('postal_address_city'))}, {sql_escape(mp.get('postal_address_state'))}, "
            f"{sql_escape(mp.get('postal_address_postcode'))}, "
            f"{sql_escape(intake[:250] if intake else None)}, "
            f"{int(mp.get('is_partner') or 0)}, 0, {sql_escape(now_str)}, {sql_escape(now_str)})"
        )
        prov_upserts += 1

        email = cp.get("new_contact_email") if "new_contact_email" in cp else p.get("new_contact_email")
        phone = cp.get("new_contact_phone") if "new_contact_phone" in cp else p.get("new_contact_phone")
        if p.get("had_existing_contact") == "NO" and email:
            c_url = (cp.get("new_contact_website_url") or p.get("new_contact_website_url") or web or "")[:250]
            contact_stmts.append(
                "INSERT INTO `provider_contacts` (`user_id`, `provider_institution_id`, `cricos_provider_code`, `email`, `phone`, `contact_website_url`, `created_at`, `updated_at`) "
                f"SELECT 1, `id`, {sql_escape(pcode)}, {sql_escape(email[:250])}, {sql_escape(phone[:250] if phone else None)}, {sql_escape(c_url)}, {sql_escape(now_str)}, {sql_escape(now_str)} "
                f"FROM `provider_institution` WHERE `cricos_provider_code` = {sql_escape(pcode)} "
                f"AND NOT EXISTS (SELECT 1 FROM `provider_contacts` pc WHERE pc.`cricos_provider_code` = {sql_escape(pcode)}) LIMIT 1;"
            )
            contact_inserts += 1

    stmts.append("-- 4. Upsert All 1,543 Official CRICOS Providers (Key = cricos_provider_code)")
    for i in range(0, len(prov_values), 150):
        batch_vals = ",\n".join(prov_values[i : i + 150])
        stmts.append(
            "INSERT INTO `provider_institution` "
            "(`user_id`, `provider_logo`, `cricos_provider_code`, `trading_name`, `institution_name`, `institution_type`, `institution_capacity`, `website`, "
            "`postal_address_line_1`, `postal_address_line_2`, `postal_address_line_3`, `postal_address_line_4`, `postal_address_city`, `postal_address_state`, `postal_address_postcode`, "
            "`intake_date`, `is_partner`, `is_transferable`, `created_at`, `updated_at`) VALUES\n"
            f"{batch_vals}\n"
            "ON DUPLICATE KEY UPDATE "
            "`institution_name` = VALUES(`institution_name`), "
            "`trading_name` = COALESCE(VALUES(`trading_name`), `trading_name`), "
            "`institution_type` = VALUES(`institution_type`), "
            "`institution_capacity` = VALUES(`institution_capacity`), "
            "`website` = COALESCE(NULLIF(`website`, ''), VALUES(`website`)), "
            "`provider_logo` = COALESCE(NULLIF(`provider_logo`, ''), VALUES(`provider_logo`)), "
            "`intake_date` = COALESCE(NULLIF(`intake_date`, ''), VALUES(`intake_date`)), "
            "`postal_address_line_1` = VALUES(`postal_address_line_1`), "
            "`postal_address_city` = VALUES(`postal_address_city`), "
            "`postal_address_state` = VALUES(`postal_address_state`), "
            "`postal_address_postcode` = VALUES(`postal_address_postcode`), "
            "`updated_at` = VALUES(`updated_at`);"
        )

    if contact_stmts:
        stmts.append("-- 5. Insert International Admissions Contacts")
        stmts.extend(contact_stmts)

    # 6. Upsert All 25,199 Active CRICOS Courses (Key = cricos_course_code)
    courses = load_courses_jsonl()
    csv_courses = load_csv_by_key(clean_course_csv, "cricos_course_code")
    course_upserts = 0
    course_values = []

    for c in courses:
        ccode = c["cricos_course_code"].strip().upper()
        cc = csv_courses.get(ccode, c)
        if str(cc.get("approve_upsert", c.get("approve_upsert", "YES"))).strip().upper() != "YES":
            continue

        apply_u = (cc.get("new_apply_form") if "new_apply_form" in cc else c.get("new_apply_form")) or None
        dur = (cc.get("new_total_course_duration") if "new_total_course_duration" in cc else c.get("new_total_course_duration")) or None
        assess = (cc.get("new_assessment_style") if "new_assessment_style" in cc else c.get("new_assessment_style")) or None
        if assess not in ("exam", "assignment", "mixed"):
            assess = None
        intakes_v = (cc.get("course_intake_dates") if "course_intake_dates" in cc else c.get("course_intake_dates")) or None

        offshore_v = cc.get("new_offshore_tuition_fee") if "new_offshore_tuition_fee" in cc else c.get("new_offshore_tuition_fee")
        onshore_v = cc.get("new_onshore_tuition_fee") if "new_onshore_tuition_fee" in cc else c.get("new_onshore_tuition_fee")
        enrol_v = cc.get("new_enrolment_fee") if "new_enrolment_fee" in cc else c.get("new_enrolment_fee")
        mat_v = cc.get("new_materials_fee") if "new_materials_fee" in cc else c.get("new_materials_fee")

        course_values.append(
            f"(1, {sql_escape(c['cricos_provider_code'])}, {sql_escape(c['institution_name'][:250])}, "
            f"{sql_escape(ccode)}, {sql_escape(c['course_name'][:250])}, "
            f"{sql_escape(c.get('new_course_description_html') or None)}, "
            f"{sql_escape(c.get('vet_national_code') or None)}, "
            f"{sql_escape(c.get('dual_qualification') or None)}, "
            f"{sql_escape(c.get('board_field_1_education') or None)}, "
            f"{sql_escape(c.get('narrow_field_1_education') or None)}, "
            f"{sql_escape(c.get('detailed_field_1_education') or None)}, "
            f"{sql_escape(c.get('board_field_2_education') or None)}, "
            f"{sql_escape(c.get('narrow_field_2_education') or None)}, "
            f"{sql_escape(c.get('detailed_field_2_education') or None)}, "
            f"{sql_escape(c.get('course_level') or None)}, "
            f"{sql_escape(c.get('foundation_studies') or None)}, "
            f"{sql_escape(c.get('work_component') or None)}, "
            f"{sql_escape(c.get('work_component_hours_per_week') or None)}, "
            f"{sql_escape(c.get('work_component_weeks') or None)}, "
            f"{sql_escape(c.get('work_component_total_hours') or None)}, "
            f"{sql_escape(c.get('course_language') or 'English')}, "
            f"{sql_escape(dur[:250] if dur else None)}, "
            f"{sql_escape(str(intakes_v)[:500] if intakes_v else None)}, "
            f"{sql_int(c.get('duration_weeks'))}, "
            f"{sql_num(onshore_v)}, {sql_num(offshore_v)}, "
            f"{sql_num(c.get('cricos_non_tuition_fee'))}, {sql_num(c.get('cricos_estimated_total_cost'))}, "
            f"{sql_num(enrol_v)}, {sql_num(mat_v)}, "
            f"{sql_escape(assess)}, "
            f"{sql_escape(c.get('new_entry_requirements_html') or None)}, "
            f"{sql_escape(c.get('new_english_requirements_html') or None)}, "
            f"{sql_escape(apply_u[:250] if apply_u else None)}, "
            f"{int(c.get('is_partner') or 0)}, 0, {sql_escape(now_str)}, {sql_escape(now_str)})"
        )
        course_upserts += 1

    stmts.append("-- 6. Upsert All 25,199 Active Official CRICOS Courses (Key = cricos_course_code)")
    for i in range(0, len(course_values), 120):
        batch_vals = ",\n".join(course_values[i : i + 120])
        stmts.append(
            "INSERT INTO `courses` "
            "(`user_id`, `cricos_provider_code`, `institution_name`, `cricos_course_code`, `course_name`, "
            "`course_description`, `vet_national_code`, `dual_qualification`, "
            "`board_field_1_education`, `narrow_field_1_education`, `detailed_field_1_education`, "
            "`board_field_2_education`, `narrow_field_2_education`, `detailed_field_2_education`, "
            "`course_level`, `foundation_studies`, `work_component`, `work_component_hours_per_week`, "
            "`work_component_weeks`, `work_component_total_hours`, `course_language`, "
            "`total_course_duration`, `intakes`, `course_duration_per_week`, "
            "`onshore_tuition_fee`, `offshore_tuition_fee`, `cricos_non_tuition_fee`, `cricos_estimated_total_cost`, "
            "`enrolment_fee`, `materials_fee`, `assessment_style`, `entry_requirements`, `english_requirements`, "
            "`apply_form`, `is_partner`, `is_expired`, `created_at`, `updated_at`) VALUES\n"
            f"{batch_vals}\n"
            "ON DUPLICATE KEY UPDATE "
            "`cricos_provider_code` = VALUES(`cricos_provider_code`), "
            "`institution_name` = VALUES(`institution_name`), "
            "`course_name` = VALUES(`course_name`), "
            "`vet_national_code` = VALUES(`vet_national_code`), "
            "`course_level` = VALUES(`course_level`), "
            "`course_duration_per_week` = VALUES(`course_duration_per_week`), "
            "`offshore_tuition_fee` = COALESCE(VALUES(`offshore_tuition_fee`), `offshore_tuition_fee`), "
            "`cricos_non_tuition_fee` = VALUES(`cricos_non_tuition_fee`), "
            "`cricos_estimated_total_cost` = VALUES(`cricos_estimated_total_cost`), "
            "`onshore_tuition_fee` = COALESCE(VALUES(`onshore_tuition_fee`), `onshore_tuition_fee`), "
            "`enrolment_fee` = COALESCE(`enrolment_fee`, VALUES(`enrolment_fee`)), "
            "`materials_fee` = COALESCE(`materials_fee`, VALUES(`materials_fee`)), "
            "`course_description` = COALESCE(NULLIF(`course_description`, ''), VALUES(`course_description`)), "
            "`entry_requirements` = COALESCE(NULLIF(`entry_requirements`, ''), VALUES(`entry_requirements`)), "
            "`english_requirements` = COALESCE(NULLIF(`english_requirements`, ''), VALUES(`english_requirements`)), "
            "`intakes` = COALESCE(NULLIF(`intakes`, ''), VALUES(`intakes`)), "
            "`total_course_duration` = COALESCE(NULLIF(`total_course_duration`, ''), VALUES(`total_course_duration`)), "
            "`assessment_style` = COALESCE(`assessment_style`, VALUES(`assessment_style`)), "
            "`apply_form` = COALESCE(VALUES(`apply_form`), `apply_form`), "
            "`is_expired` = 0, "
            "`updated_at` = VALUES(`updated_at`);"
        )

    # 7. Upsert Campus Locations & Course Locations
    loc_rows = [dict(r) for r in conn.execute("SELECT * FROM master_locations")]
    stmts.append("-- 7. Upsert Official CRICOS Campus Locations")
    for i in range(0, len(loc_rows), 250):
        b = [
            f"(1, {sql_escape(r['cricos_provider_code'])}, {sql_escape(r['institution_name'][:250])}, "
            f"{sql_escape(r['location_name'][:250])}, {sql_escape(r['location_type'] or 'Campus')}, "
            f"{sql_escape((r['address_line_1'] or '')[:250])}, {sql_escape(r['address_line_2'])}, "
            f"{sql_escape(r['address_line_3'])}, {sql_escape(r['address_line_4'])}, "
            f"{sql_escape(r['city'])}, {sql_escape(r['state'])}, {sql_escape(r['postcode'])}, "
            f"{sql_escape(now_str)}, {sql_escape(now_str)})"
            for r in loc_rows[i : i + 250]
        ]
        stmts.append(
            "INSERT INTO `locations` (`user_id`, `cricos_provider_code`, `institution_name`, `location_name`, `location_type`, "
            "`address_line_1`, `address_line_2`, `address_line_3`, `address_line_4`, `city`, `state`, `postcode`, `created_at`, `updated_at`) VALUES\n"
            + ",\n".join(b)
            + "\nON DUPLICATE KEY UPDATE `address_line_1` = VALUES(`address_line_1`), `city` = VALUES(`city`), `state` = VALUES(`state`), `postcode` = VALUES(`postcode`), `updated_at` = VALUES(`updated_at`);"
        )

    cloc_rows = [dict(r) for r in conn.execute("SELECT * FROM master_course_locations")]
    stmts.append("-- 8. Upsert Official CRICOS Course-Location Mappings")
    for i in range(0, len(cloc_rows), 350):
        b = [
            f"(1, {sql_escape(r['cricos_provider_code'])}, {sql_escape(r['institution_name'][:250])}, "
            f"{sql_escape(r['cricos_course_code'])}, {sql_escape(r['location_name'][:250])}, "
            f"{sql_escape(r['location_city'])}, {sql_escape(r['location_state'])}, "
            f"{sql_escape(now_str)}, {sql_escape(now_str)})"
            for r in cloc_rows[i : i + 350]
        ]
        stmts.append(
            "INSERT INTO `course_locations` (`user_id`, `cricos_provider_code`, `institution_name`, `cricos_course_code`, `location_name`, `location_city`, `location_state`, `created_at`, `updated_at`) VALUES\n"
            + ",\n".join(b)
            + "\nON DUPLICATE KEY UPDATE `location_city` = VALUES(`location_city`), `location_state` = VALUES(`location_state`), `updated_at` = VALUES(`updated_at`);"
        )

    # 8. Insert International Scholarships dynamically resolved via cricos_provider_code
    schs = load_csv_by_key(sch_csv, None)
    sch_inserts = 0
    if schs:
        stmts.append("-- 9. Insert International Student Scholarships (Resolved strictly via cricos_provider_code)")
        for s in schs:
            if str(s.get("approve_insert", "YES")).strip().upper() != "YES":
                continue
            pcode = (s.get("cricos_provider_code") or "").strip().upper()
            if not pcode:
                continue
            sname = (s.get("scholarship_name") or "")[:250]
            desc = (s.get("scholarship_description") or "")[:250]
            amt = (s.get("scholarship_amount") or "")[:250]
            web = (s.get("website") or "")[:250]
            stmts.append(
                "INSERT INTO `scholarships` (`user_id`, `course_id`, `provider_institution_id`, `scholarship_name`, `scholarship_description`, `scholarship_amount`, `website`, `created_at`, `updated_at`) "
                f"SELECT 1, 0, pi.`id`, {sql_escape(sname)}, {sql_escape(desc)}, {sql_escape(amt)}, {sql_escape(web)}, {sql_escape(now_str)}, {sql_escape(now_str)} "
                f"FROM `provider_institution` pi WHERE pi.`cricos_provider_code` = {sql_escape(pcode)} "
                f"AND NOT EXISTS (SELECT 1 FROM `scholarships` sc WHERE sc.`provider_institution_id` = pi.`id` AND sc.`scholarship_name` = {sql_escape(sname)}) LIMIT 1;"
            )
            sch_inserts += 1

    written_files = write_sql_chunks("u627422877_dashboard_intl_updates", header_lines, stmts)
    conn.close()

    print(
        f"Generated SQL file(s): {written_files}\n"
        f"  Repaired 6-digit CRICOS codes: {len(repaired)}\n"
        f"  Expired DB courses flagged:    {len(expired_codes)}\n"
        f"  Provider UPSERTs:              {prov_upserts}\n"
        f"  Provider Contact INSERTs:      {contact_inserts}\n"
        f"  Active Course UPSERTs:         {course_upserts}\n"
        f"  Campus Location UPSERTs:       {len(loc_rows)}\n"
        f"  Course-Location UPSERTs:       {len(cloc_rows)}\n"
        f"  Scholarship INSERTs:           {sch_inserts}"
    )
    return written_files


if __name__ == "__main__":
    generate_sql()
