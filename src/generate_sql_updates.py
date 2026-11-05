"""
Generates MariaDB 10.5+ / 11.x & phpMyAdmin-safe UPSERT (.sql) patch files
strictly keyed by `cricos_provider_code` and `cricos_course_code` (`UNIQUE INDEX` +
`INSERT ... ON DUPLICATE KEY UPDATE`).

Guarantees:
  - 100% of DDL (`ALTER TABLE`, `ADD COLUMN IF NOT EXISTS`, `MODIFY COLUMN`, `ADD UNIQUE INDEX IF NOT EXISTS`)
    executes in the preamble BEFORE `START TRANSACTION;` (zero implicit commits mid-transaction).
  - Repairs 214 stripped-leading-zero 6-digit `cricos_course_code`s on BOTH `courses` and `course_locations`
    and deduplicates any pre-existing duplicate `(cricos_provider_code, location_name)` /
    `(cricos_course_code, location_name)` rows BEFORE creating unique indexes (prevents ERROR 1062).
  - Alters `scholarships.course_id` to `NULLABLE` while preserving `scholarships_course_id_foreign`
    (`ON DELETE CASCADE`), widens `scholarship_description` to `TEXT`, adds `eligibility`, `level_of_studies`,
    `field_of_studies`, `source_url`, and inserts `course_id = NULL` (prevents ERROR 1452).
  - Widens `courses.intakes` to `TEXT`, `course_description`/`entry_requirements`/`english_requirements` to
    `MEDIUMTEXT`, and `apply_form` to `VARCHAR(500)` (prevents ERROR 1406 regardless of migration order).
  - Backfills relational foreign keys (`provider_institution_id`, `course_id`) at the end of the import.
  - Automatically splits into `<42 MB` parts so GitHub and phpMyAdmin import effortlessly.
"""
import csv
import datetime as dt
import json
import os
import sqlite3

from export_review_sheets import clean_intake_string, load_courses_jsonl

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


def write_sql_chunks(base_stem: str, preamble_lines: list[str], dml_statements: list[str]) -> list[str]:
    for old_f in os.listdir(EXPORT_DIR):
        if old_f.startswith(base_stem) and old_f.endswith(".sql"):
            try:
                os.remove(os.path.join(EXPORT_DIR, old_f))
            except OSError:
                pass

    part1_header = "\n".join(preamble_lines) + "\n\nSTART TRANSACTION;\n\n"
    partN_header = (
        "-- =====================================================================\n"
        "-- Continuation Part (Import AFTER Part 1 completes)\n"
        "-- =====================================================================\n"
        "SET NAMES utf8mb4;\n"
        "START TRANSACTION;\n\n"
    )
    footer = "\nCOMMIT;\n"

    total_est = (
        len(part1_header.encode("utf-8"))
        + sum(len(s.encode("utf-8")) + 1 for s in dml_statements)
        + len(footer.encode("utf-8"))
    )
    if total_est <= MAX_SQL_PART_BYTES:
        out_p = os.path.join(EXPORT_DIR, f"{base_stem}.sql")
        with open(out_p, "w", encoding="utf-8") as f:
            f.write(part1_header)
            f.write("\n".join(dml_statements))
            f.write(footer)
        return [out_p]

    written = []
    part = 1
    cur_stmts = []
    cur_bytes = len(part1_header.encode("utf-8")) + len(footer.encode("utf-8"))

    for st in dml_statements:
        b = len(st.encode("utf-8")) + 1
        if cur_stmts and (cur_bytes + b > MAX_SQL_PART_BYTES):
            out_p = os.path.join(EXPORT_DIR, f"{base_stem}_part{part}.sql")
            hdr = part1_header if part == 1 else partN_header
            with open(out_p, "w", encoding="utf-8") as f:
                f.write(hdr)
                f.write("\n".join(cur_stmts))
                f.write(footer)
            written.append(out_p)
            part += 1
            cur_stmts = []
            cur_bytes = len(partN_header.encode("utf-8")) + len(footer.encode("utf-8"))
        cur_stmts.append(st)
        cur_bytes += b

    if cur_stmts:
        out_p = os.path.join(EXPORT_DIR, f"{base_stem}_part{part}.sql")
        hdr = part1_header if part == 1 else partN_header
        with open(out_p, "w", encoding="utf-8") as f:
            f.write(hdr)
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

    repaired = [dict(r) for r in conn.execute("SELECT * FROM repaired_db_course_codes ORDER BY new_cricos_course_code")]

    # =========================================================================
    # PREAMBLE (Part 1 Header BEFORE `START TRANSACTION;` — All DDL + Pre-Index Dedup)
    # =========================================================================
    preamble_lines = [
        "-- =====================================================================",
        f"-- Full CRICOS International Student Register & Enrichment Patch ({now_str})",
        "-- Target Database: u627422877_dashboard (MariaDB 10.5+ / 11.x)",
        "-- Unique Keys Enforced: cricos_provider_code & cricos_course_code",
        "-- Import Order: Import Part 1 first, then Part 2.",
        "-- =====================================================================",
        "SET NAMES utf8mb4;",
        "",
        "-- A. Idempotent Schema Upgrades (Compatible whether Laravel migrations ran before or after)",
        "ALTER TABLE `provider_institution` ADD COLUMN IF NOT EXISTS `country_code` VARCHAR(2) NOT NULL DEFAULT 'AU';",
        "ALTER TABLE `provider_institution` ADD COLUMN IF NOT EXISTS `admissions_email` VARCHAR(255) DEFAULT NULL;",
        "",
        "ALTER TABLE `courses` ADD COLUMN IF NOT EXISTS `country_code` VARCHAR(2) NOT NULL DEFAULT 'AU';",
        "ALTER TABLE `courses` ADD COLUMN IF NOT EXISTS `provider_institution_id` BIGINT(20) UNSIGNED DEFAULT NULL;",
        "ALTER TABLE `courses` ADD COLUMN IF NOT EXISTS `english_requirements` MEDIUMTEXT DEFAULT NULL AFTER `entry_requirements`;",
        "ALTER TABLE `courses` ADD COLUMN IF NOT EXISTS `intakes` TEXT DEFAULT NULL AFTER `total_course_duration`;",
        "ALTER TABLE `courses` ADD COLUMN IF NOT EXISTS `cricos_non_tuition_fee` DOUBLE DEFAULT NULL AFTER `offshore_tuition_fee`;",
        "ALTER TABLE `courses` ADD COLUMN IF NOT EXISTS `cricos_estimated_total_cost` DOUBLE DEFAULT NULL AFTER `cricos_non_tuition_fee`;",
        "ALTER TABLE `courses` ADD COLUMN IF NOT EXISTS `is_expired` TINYINT(1) NOT NULL DEFAULT 0 AFTER `is_partner`;",
        "ALTER TABLE `courses` MODIFY COLUMN `intakes` TEXT DEFAULT NULL;",
        "ALTER TABLE `courses` MODIFY COLUMN `course_description` MEDIUMTEXT DEFAULT NULL;",
        "ALTER TABLE `courses` MODIFY COLUMN `entry_requirements` MEDIUMTEXT DEFAULT NULL;",
        "ALTER TABLE `courses` MODIFY COLUMN `english_requirements` MEDIUMTEXT DEFAULT NULL;",
        "ALTER TABLE `courses` MODIFY COLUMN `apply_form` VARCHAR(500) DEFAULT NULL;",
        "ALTER TABLE `courses` ADD INDEX IF NOT EXISTS `idx_courses_country_expired` (`country_code`, `is_expired`);",
        "",
        "ALTER TABLE `locations` ADD COLUMN IF NOT EXISTS `country_code` VARCHAR(2) NOT NULL DEFAULT 'AU';",
        "ALTER TABLE `locations` ADD COLUMN IF NOT EXISTS `provider_institution_id` BIGINT(20) UNSIGNED DEFAULT NULL;",
        "ALTER TABLE `locations` MODIFY COLUMN `address_line_1` VARCHAR(255) DEFAULT NULL;",
        "ALTER TABLE `locations` MODIFY COLUMN `postcode` VARCHAR(255) DEFAULT NULL;",
        "",
        "ALTER TABLE `course_locations` ADD COLUMN IF NOT EXISTS `country_code` VARCHAR(2) NOT NULL DEFAULT 'AU';",
        "ALTER TABLE `course_locations` ADD COLUMN IF NOT EXISTS `course_id` BIGINT(20) UNSIGNED DEFAULT NULL;",
        "ALTER TABLE `course_locations` ADD COLUMN IF NOT EXISTS `provider_institution_id` BIGINT(20) UNSIGNED DEFAULT NULL;",
        "",
        "-- B. Upgrade `scholarships` for institution-wide international scholarships (course_id = NULL)",
        "ALTER TABLE `scholarships` DROP FOREIGN KEY IF EXISTS `scholarships_course_id_foreign`;",
        "ALTER TABLE `scholarships` MODIFY COLUMN `course_id` BIGINT(20) UNSIGNED NULL DEFAULT NULL;",
        "ALTER TABLE `scholarships` MODIFY COLUMN `scholarship_description` TEXT DEFAULT NULL;",
        "ALTER TABLE `scholarships` MODIFY COLUMN `scholarship_amount` VARCHAR(500) DEFAULT NULL;",
        "ALTER TABLE `scholarships` MODIFY COLUMN `website` VARCHAR(500) DEFAULT NULL;",
        "ALTER TABLE `scholarships` ADD COLUMN IF NOT EXISTS `eligibility` TEXT DEFAULT NULL AFTER `scholarship_description`;",
        "ALTER TABLE `scholarships` ADD COLUMN IF NOT EXISTS `level_of_studies` VARCHAR(255) DEFAULT NULL AFTER `scholarship_amount`;",
        "ALTER TABLE `scholarships` ADD COLUMN IF NOT EXISTS `field_of_studies` TEXT DEFAULT NULL AFTER `level_of_studies`;",
        "ALTER TABLE `scholarships` ADD COLUMN IF NOT EXISTS `source_url` VARCHAR(500) DEFAULT NULL AFTER `website`;",
        "UPDATE `scholarships` SET `course_id` = NULL WHERE `course_id` = 0;",
        "ALTER TABLE `scholarships` ADD CONSTRAINT `scholarships_course_id_foreign` FOREIGN KEY (`course_id`) REFERENCES `courses` (`id`) ON DELETE CASCADE;",
        "",
        "-- C. Repair 6-character stripped-leading-zero CRICOS course codes BEFORE adding UNIQUE indexes",
    ]

    for r in repaired:
        old_c = sql_escape(r["old_cricos_course_code"])
        new_c = sql_escape(r["new_cricos_course_code"])
        prov_c = sql_escape(r["cricos_provider_code"])
        preamble_lines.append(
            f"UPDATE `courses` SET `cricos_course_code` = {new_c}, `updated_at` = {sql_escape(now_str)} "
            f"WHERE `cricos_course_code` = {old_c} AND `cricos_provider_code` = {prov_c} "
            f"AND NOT EXISTS (SELECT 1 FROM (SELECT `id` FROM `courses` WHERE `cricos_course_code` = {new_c}) AS existing_c);"
        )
        preamble_lines.append(
            f"UPDATE `course_locations` SET `cricos_course_code` = {new_c}, `updated_at` = {sql_escape(now_str)} "
            f"WHERE `cricos_course_code` = {old_c} AND `cricos_provider_code` = {prov_c};"
        )

    preamble_lines.extend(
        [
            "",
            "-- D. Normalize empty CRICOS codes & deduplicate pre-existing location rows BEFORE UNIQUE index creation",
            "UPDATE `provider_institution` SET `cricos_provider_code` = NULL WHERE `cricos_provider_code` = '';",
            "UPDATE `courses` SET `cricos_course_code` = NULL WHERE `cricos_course_code` = '';",
            "UPDATE `locations` SET `cricos_provider_code` = NULL WHERE `cricos_provider_code` = '';",
            "UPDATE `course_locations` SET `cricos_course_code` = NULL WHERE `cricos_course_code` = '';",
            "DELETE l1 FROM `locations` l1 INNER JOIN `locations` l2 "
            "ON l1.`cricos_provider_code` = l2.`cricos_provider_code` "
            "AND LEFT(l1.`location_name`, 191) = LEFT(l2.`location_name`, 191) "
            "AND l1.`id` > l2.`id` "
            "WHERE l1.`cricos_provider_code` IS NOT NULL;",
            "DELETE cl1 FROM `course_locations` cl1 INNER JOIN `course_locations` cl2 "
            "ON cl1.`cricos_course_code` = cl2.`cricos_course_code` "
            "AND LEFT(cl1.`location_name`, 191) = LEFT(cl2.`location_name`, 191) "
            "AND cl1.`id` > cl2.`id` "
            "WHERE cl1.`cricos_course_code` IS NOT NULL;",
            "",
            "-- E. Enforce Strict Unique CRICOS Identifiers",
            "ALTER TABLE `provider_institution` ADD UNIQUE INDEX IF NOT EXISTS `uq_cricos_provider_code` (`cricos_provider_code`);",
            "ALTER TABLE `courses` ADD UNIQUE INDEX IF NOT EXISTS `uq_cricos_course_code` (`cricos_course_code`);",
            "ALTER TABLE `locations` ADD UNIQUE INDEX IF NOT EXISTS `uq_loc_cricos_name` (`cricos_provider_code`(32), `location_name`(191));",
            "ALTER TABLE `course_locations` ADD UNIQUE INDEX IF NOT EXISTS `uq_cloc_cricos_name` (`cricos_course_code`(32), `location_name`(191));",
        ]
    )

    # =========================================================================
    # PURE DML STATEMENTS (Executed inside START TRANSACTION ... COMMIT)
    # =========================================================================
    stmts = []

    # 1. Flag expired / deregistered courses
    expired_codes = [r[0] for r in conn.execute("SELECT cricos_course_code FROM db_expired_or_removed_courses")]
    if expired_codes:
        stmts.append("-- 1. Flag expired / deregistered CRICOS courses (is_expired = 1)")
        for i in range(0, len(expired_codes), 250):
            chunk = ", ".join(sql_escape(c) for c in expired_codes[i : i + 250])
            stmts.append(f"UPDATE `courses` SET `is_expired` = 1 WHERE `cricos_course_code` IN ({chunk});")

    # 2. Upsert all 1,543 CRICOS Providers + International Contacts
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
        raw_intake = (cp.get("new_intake_date") if "new_intake_date" in cp else p.get("new_intake_date")) or None
        intake = clean_intake_string(raw_intake) or None
        logo = (cp.get("new_provider_logo") if "new_provider_logo" in cp else p.get("new_provider_logo")) or None
        email = (cp.get("new_contact_email") if "new_contact_email" in cp else p.get("new_contact_email")) or None
        phone = (cp.get("new_contact_phone") if "new_contact_phone" in cp else p.get("new_contact_phone")) or None

        prov_values.append(
            f"(1, 'AU', {sql_escape(logo[:250] if logo else None)}, {sql_escape(pcode)}, "
            f"{sql_escape((mp.get('trading_name') or '')[:250] or None)}, "
            f"{sql_escape(mp.get('institution_name', p['institution_name'])[:250])}, "
            f"{sql_escape(mp.get('institution_type'))}, {sql_int(mp.get('institution_capacity'))}, "
            f"{sql_escape(web[:250] if web else None)}, "
            f"{sql_escape(email[:250] if email else None)}, "
            f"{sql_escape(mp.get('postal_address_line_1'))}, {sql_escape(mp.get('postal_address_line_2'))}, "
            f"{sql_escape(mp.get('postal_address_line_3'))}, {sql_escape(mp.get('postal_address_line_4'))}, "
            f"{sql_escape(mp.get('postal_address_city'))}, {sql_escape(mp.get('postal_address_state'))}, "
            f"{sql_escape(mp.get('postal_address_postcode'))}, "
            f"{sql_escape(intake[:250] if intake else None)}, "
            f"{int(mp.get('is_partner') or 0)}, 0, {sql_escape(now_str)}, {sql_escape(now_str)})"
        )
        prov_upserts += 1

        if email:
            c_url = (cp.get("new_contact_website_url") or p.get("new_contact_website_url") or web or "")[:250]
            contact_stmts.append(
                "INSERT INTO `provider_contacts` (`user_id`, `provider_institution_id`, `cricos_provider_code`, `email`, `phone`, `contact_website_url`, `created_at`, `updated_at`) "
                f"SELECT 1, `id`, {sql_escape(pcode)}, {sql_escape(email[:250])}, {sql_escape(phone[:250] if phone else None)}, {sql_escape(c_url)}, {sql_escape(now_str)}, {sql_escape(now_str)} "
                f"FROM `provider_institution` WHERE `cricos_provider_code` = {sql_escape(pcode)} "
                f"AND NOT EXISTS (SELECT 1 FROM `provider_contacts` pc WHERE pc.`cricos_provider_code` = {sql_escape(pcode)}) LIMIT 1;"
            )
            contact_inserts += 1

    stmts.append("-- 2. Upsert All 1,543 Official CRICOS Providers (Key = cricos_provider_code)")
    for i in range(0, len(prov_values), 150):
        batch_vals = ",\n".join(prov_values[i : i + 150])
        stmts.append(
            "INSERT INTO `provider_institution` "
            "(`user_id`, `country_code`, `provider_logo`, `cricos_provider_code`, `trading_name`, `institution_name`, `institution_type`, `institution_capacity`, `website`, `admissions_email`, "
            "`postal_address_line_1`, `postal_address_line_2`, `postal_address_line_3`, `postal_address_line_4`, `postal_address_city`, `postal_address_state`, `postal_address_postcode`, "
            "`intake_date`, `is_partner`, `is_transferable`, `created_at`, `updated_at`) VALUES\n"
            f"{batch_vals}\n"
            "ON DUPLICATE KEY UPDATE "
            "`country_code` = 'AU', "
            "`institution_name` = VALUES(`institution_name`), "
            "`trading_name` = COALESCE(VALUES(`trading_name`), `trading_name`), "
            "`institution_type` = VALUES(`institution_type`), "
            "`institution_capacity` = VALUES(`institution_capacity`), "
            "`website` = COALESCE(NULLIF(`website`, ''), VALUES(`website`)), "
            "`admissions_email` = COALESCE(NULLIF(`admissions_email`, ''), VALUES(`admissions_email`)), "
            "`provider_logo` = COALESCE(NULLIF(`provider_logo`, ''), VALUES(`provider_logo`)), "
            "`intake_date` = COALESCE(NULLIF(`intake_date`, ''), VALUES(`intake_date`)), "
            "`postal_address_line_1` = VALUES(`postal_address_line_1`), "
            "`postal_address_line_2` = VALUES(`postal_address_line_2`), "
            "`postal_address_line_3` = VALUES(`postal_address_line_3`), "
            "`postal_address_line_4` = VALUES(`postal_address_line_4`), "
            "`postal_address_city` = VALUES(`postal_address_city`), "
            "`postal_address_state` = VALUES(`postal_address_state`), "
            "`postal_address_postcode` = VALUES(`postal_address_postcode`), "
            "`updated_at` = VALUES(`updated_at`);"
        )

    if contact_stmts:
        stmts.append("-- 3. Insert International Admissions Contacts")
        stmts.extend(contact_stmts)

    # 4. Upsert All 25,199 Active CRICOS Courses (Key = cricos_course_code)
    courses = load_courses_jsonl()
    csv_courses = load_csv_by_key(clean_course_csv, "cricos_course_code")
    course_upserts = 0
    course_values = []

    for c in courses:
        ccode = c["cricos_course_code"].strip().upper()
        cc = csv_courses.get(ccode, c)
        if str(cc.get("approve_upsert", c.get("approve_upsert", "YES"))).strip().upper() != "YES":
            continue

        apply_u = (
            cc.get("new_apply_form") if "new_apply_form" in cc else c.get("new_apply_form")
        ) or c.get("current_apply_form") or None
        dur = (cc.get("new_total_course_duration") if "new_total_course_duration" in cc else c.get("new_total_course_duration")) or None
        assess = (cc.get("new_assessment_style") if "new_assessment_style" in cc else c.get("new_assessment_style")) or None
        if assess not in ("exam", "assignment", "mixed"):
            assess = None
        raw_intakes_v = (cc.get("course_intake_dates") if "course_intake_dates" in cc else c.get("course_intake_dates")) or None
        intakes_v = clean_intake_string(str(raw_intakes_v)) if raw_intakes_v else None

        offshore_v = cc.get("new_offshore_tuition_fee") if "new_offshore_tuition_fee" in cc else c.get("new_offshore_tuition_fee")
        onshore_v = cc.get("new_onshore_tuition_fee") if "new_onshore_tuition_fee" in cc else c.get("new_onshore_tuition_fee")
        enrol_v = cc.get("new_enrolment_fee") if "new_enrolment_fee" in cc else c.get("new_enrolment_fee")
        mat_v = cc.get("new_materials_fee") if "new_materials_fee" in cc else c.get("new_materials_fee")

        course_values.append(
            f"(1, 'AU', {sql_escape(c['cricos_provider_code'])}, {sql_escape(c['institution_name'][:250])}, "
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
            f"{sql_escape(intakes_v)}, "
            f"{sql_int(c.get('duration_weeks'))}, "
            f"{sql_num(onshore_v)}, {sql_num(offshore_v)}, "
            f"{sql_num(c.get('cricos_non_tuition_fee'))}, {sql_num(c.get('cricos_estimated_total_cost'))}, "
            f"{sql_num(enrol_v)}, {sql_num(mat_v)}, "
            f"{sql_escape(assess)}, "
            f"{sql_escape(c.get('new_entry_requirements_html') or None)}, "
            f"{sql_escape(c.get('new_english_requirements_html') or None)}, "
            f"{sql_escape(apply_u[:500] if apply_u else None)}, "
            f"{int(c.get('is_partner') or 0)}, 0, {sql_escape(now_str)}, {sql_escape(now_str)})"
        )
        course_upserts += 1

    stmts.append("-- 4. Upsert All 25,199 Active Official CRICOS Courses (Key = cricos_course_code)")
    for i in range(0, len(course_values), 120):
        batch_vals = ",\n".join(course_values[i : i + 120])
        stmts.append(
            "INSERT INTO `courses` "
            "(`user_id`, `country_code`, `cricos_provider_code`, `institution_name`, `cricos_course_code`, `course_name`, "
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
            "`country_code` = 'AU', "
            "`cricos_provider_code` = VALUES(`cricos_provider_code`), "
            "`institution_name` = VALUES(`institution_name`), "
            "`course_name` = VALUES(`course_name`), "
            "`vet_national_code` = VALUES(`vet_national_code`), "
            "`dual_qualification` = VALUES(`dual_qualification`), "
            "`board_field_1_education` = COALESCE(VALUES(`board_field_1_education`), `board_field_1_education`), "
            "`narrow_field_1_education` = COALESCE(VALUES(`narrow_field_1_education`), `narrow_field_1_education`), "
            "`detailed_field_1_education` = COALESCE(VALUES(`detailed_field_1_education`), `detailed_field_1_education`), "
            "`board_field_2_education` = COALESCE(VALUES(`board_field_2_education`), `board_field_2_education`), "
            "`narrow_field_2_education` = COALESCE(VALUES(`narrow_field_2_education`), `narrow_field_2_education`), "
            "`detailed_field_2_education` = COALESCE(VALUES(`detailed_field_2_education`), `detailed_field_2_education`), "
            "`course_level` = VALUES(`course_level`), "
            "`foundation_studies` = COALESCE(VALUES(`foundation_studies`), `foundation_studies`), "
            "`work_component` = COALESCE(VALUES(`work_component`), `work_component`), "
            "`work_component_hours_per_week` = COALESCE(VALUES(`work_component_hours_per_week`), `work_component_hours_per_week`), "
            "`work_component_weeks` = COALESCE(VALUES(`work_component_weeks`), `work_component_weeks`), "
            "`work_component_total_hours` = COALESCE(VALUES(`work_component_total_hours`), `work_component_total_hours`), "
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

    # 5. Upsert Campus Locations & Course Locations
    loc_rows = [dict(r) for r in conn.execute("SELECT * FROM master_locations")]
    stmts.append("-- 5. Upsert Official CRICOS Campus Locations")
    for i in range(0, len(loc_rows), 250):
        b = [
            f"(1, 'AU', {sql_escape(r['cricos_provider_code'])}, {sql_escape(r['institution_name'][:250])}, "
            f"{sql_escape(r['location_name'][:250])}, {sql_escape(r['location_type'] or 'Campus')}, "
            f"{sql_escape((r['address_line_1'] or '')[:250] or None)}, {sql_escape(r['address_line_2'])}, "
            f"{sql_escape(r['address_line_3'])}, {sql_escape(r['address_line_4'])}, "
            f"{sql_escape(r['city'])}, {sql_escape(r['state'])}, {sql_escape(r['postcode'])}, "
            f"{sql_escape(now_str)}, {sql_escape(now_str)})"
            for r in loc_rows[i : i + 250]
        ]
        stmts.append(
            "INSERT INTO `locations` (`user_id`, `country_code`, `cricos_provider_code`, `institution_name`, `location_name`, `location_type`, "
            "`address_line_1`, `address_line_2`, `address_line_3`, `address_line_4`, `city`, `state`, `postcode`, `created_at`, `updated_at`) VALUES\n"
            + ",\n".join(b)
            + "\nON DUPLICATE KEY UPDATE "
            "`country_code` = 'AU', "
            "`institution_name` = VALUES(`institution_name`), "
            "`location_type` = VALUES(`location_type`), "
            "`address_line_1` = VALUES(`address_line_1`), "
            "`address_line_2` = VALUES(`address_line_2`), "
            "`address_line_3` = VALUES(`address_line_3`), "
            "`address_line_4` = VALUES(`address_line_4`), "
            "`city` = VALUES(`city`), "
            "`state` = VALUES(`state`), "
            "`postcode` = VALUES(`postcode`), "
            "`updated_at` = VALUES(`updated_at`);"
        )

    cloc_rows = [dict(r) for r in conn.execute("SELECT * FROM master_course_locations")]
    stmts.append("-- 6. Upsert Official CRICOS Course-Location Mappings")
    for i in range(0, len(cloc_rows), 350):
        b = [
            f"(1, 'AU', {sql_escape(r['cricos_provider_code'])}, {sql_escape(r['institution_name'][:250])}, "
            f"{sql_escape(r['cricos_course_code'])}, {sql_escape(r['location_name'][:250])}, "
            f"{sql_escape(r['location_city'])}, {sql_escape(r['location_state'])}, "
            f"{sql_escape(now_str)}, {sql_escape(now_str)})"
            for r in cloc_rows[i : i + 350]
        ]
        stmts.append(
            "INSERT INTO `course_locations` (`user_id`, `country_code`, `cricos_provider_code`, `institution_name`, `cricos_course_code`, `location_name`, `location_city`, `location_state`, `created_at`, `updated_at`) VALUES\n"
            + ",\n".join(b)
            + "\nON DUPLICATE KEY UPDATE "
            "`country_code` = 'AU', "
            "`cricos_provider_code` = VALUES(`cricos_provider_code`), "
            "`institution_name` = VALUES(`institution_name`), "
            "`location_city` = VALUES(`location_city`), "
            "`location_state` = VALUES(`location_state`), "
            "`updated_at` = VALUES(`updated_at`);"
        )

    # 7. Insert / Refresh International Scholarships dynamically resolved via cricos_provider_code (course_id = NULL)
    schs = load_csv_by_key(sch_csv, None)
    sch_inserts = 0
    if schs:
        stmts.append("-- 7. Upsert International Student Scholarships (course_id = NULL, resolved via cricos_provider_code)")
        for s in schs:
            if str(s.get("approve_insert", "YES")).strip().upper() != "YES":
                continue
            pcode = (s.get("cricos_provider_code") or "").strip().upper()
            if not pcode:
                continue
            sname = (s.get("scholarship_name") or "").strip()[:250]
            if not sname:
                continue
            desc = (s.get("scholarship_description") or "").strip() or None
            elig = (s.get("eligibility") or "").strip() or None
            amt = ((s.get("scholarship_amount") or "").strip()[:500]) or None
            lvl = ((s.get("level_of_studies") or "").strip()[:250]) or None
            fld = (s.get("field_of_studies") or "").strip() or None
            web = ((s.get("website") or "").strip()[:500]) or None
            src = ((s.get("source_url") or "").strip()[:500]) or None

            stmts.append(
                "UPDATE `scholarships` sc "
                "INNER JOIN `provider_institution` pi ON pi.`id` = sc.`provider_institution_id` "
                f"SET sc.`scholarship_description` = {sql_escape(desc)}, "
                f"sc.`eligibility` = {sql_escape(elig)}, "
                f"sc.`scholarship_amount` = {sql_escape(amt)}, "
                f"sc.`level_of_studies` = {sql_escape(lvl)}, "
                f"sc.`field_of_studies` = {sql_escape(fld)}, "
                f"sc.`website` = {sql_escape(web)}, "
                f"sc.`source_url` = {sql_escape(src)}, "
                f"sc.`updated_at` = {sql_escape(now_str)} "
                f"WHERE pi.`cricos_provider_code` = {sql_escape(pcode)} AND sc.`scholarship_name` = {sql_escape(sname)} AND sc.`course_id` IS NULL;"
            )
            stmts.append(
                "INSERT INTO `scholarships` "
                "(`user_id`, `course_id`, `provider_institution_id`, `scholarship_name`, `scholarship_description`, `eligibility`, `scholarship_amount`, `level_of_studies`, `field_of_studies`, `website`, `source_url`, `created_at`, `updated_at`) "
                f"SELECT 1, NULL, pi.`id`, {sql_escape(sname)}, {sql_escape(desc)}, {sql_escape(elig)}, {sql_escape(amt)}, {sql_escape(lvl)}, {sql_escape(fld)}, {sql_escape(web)}, {sql_escape(src)}, {sql_escape(now_str)}, {sql_escape(now_str)} "
                f"FROM `provider_institution` pi WHERE pi.`cricos_provider_code` = {sql_escape(pcode)} "
                f"AND NOT EXISTS (SELECT 1 FROM `scholarships` sc WHERE sc.`provider_institution_id` = pi.`id` AND sc.`scholarship_name` = {sql_escape(sname)}) LIMIT 1;"
            )
            sch_inserts += 1

    # 8. Relational Foreign Key Backfill (links courses, locations, and course_locations to numeric IDs)
    stmts.extend(
        [
            "-- 8. Backfill relational foreign keys across courses, locations, and course_locations",
            "UPDATE `courses` c INNER JOIN `provider_institution` p ON p.`cricos_provider_code` = c.`cricos_provider_code` "
            "SET c.`provider_institution_id` = p.`id` WHERE c.`provider_institution_id` IS NULL AND c.`cricos_provider_code` IS NOT NULL;",
            "UPDATE `locations` l INNER JOIN `provider_institution` p ON p.`cricos_provider_code` = l.`cricos_provider_code` "
            "SET l.`provider_institution_id` = p.`id` WHERE l.`provider_institution_id` IS NULL AND l.`cricos_provider_code` IS NOT NULL;",
            "UPDATE `course_locations` cl INNER JOIN `provider_institution` p ON p.`cricos_provider_code` = cl.`cricos_provider_code` "
            "SET cl.`provider_institution_id` = p.`id` WHERE cl.`provider_institution_id` IS NULL AND cl.`cricos_provider_code` IS NOT NULL;",
            "UPDATE `course_locations` cl INNER JOIN `courses` c ON c.`cricos_course_code` = cl.`cricos_course_code` "
            "SET cl.`course_id` = c.`id` WHERE cl.`course_id` IS NULL AND cl.`cricos_course_code` IS NOT NULL;",
        ]
    )

    written_files = write_sql_chunks("u627422877_dashboard_intl_updates", preamble_lines, stmts)
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
        f"  Scholarship UPSERTs:           {sch_inserts}"
    )
    return written_files


if __name__ == "__main__":
    generate_sql()
