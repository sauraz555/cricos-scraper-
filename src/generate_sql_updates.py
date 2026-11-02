"""
Generates safe, transactional MariaDB/MySQL UPDATE & INSERT SQL files from the
reviewed CSV & JSON sheets in exports/.

Automatically honors any `approve_update` / `approve_insert` = NO flags (or edited
URLs / fees / intakes) saved in EITHER the .csv files (e.g. from Excel) OR the .json files!
Strictly updates ONLY columns that were missing/empty in u627422877_dashboard (2).sql.
"""
import csv
import datetime as dt
import json
import os

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPORT_DIR = os.path.join(PROJECT_DIR, "exports")
SQL_OUT_PATH = os.path.join(EXPORT_DIR, "u627422877_dashboard_intl_updates.sql")


def sql_escape(val) -> str:
    if val is None:
        return "NULL"
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


def load_csv_by_key(csv_path: str, key_col: str | None = None) -> dict | list:
    if not os.path.exists(csv_path):
        return {} if key_col else []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if key_col:
        return {str(r[key_col]).strip(): r for r in rows if r.get(key_col)}
    return rows


def generate_sql(output_path: str = SQL_OUT_PATH):
    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prov_json = os.path.join(EXPORT_DIR, "review_providers_international.json")
    prov_csv = os.path.join(EXPORT_DIR, "review_providers_international.csv")
    course_json = os.path.join(EXPORT_DIR, "review_courses_international.json")
    base_course_csv = os.path.join(EXPORT_DIR, "review_courses_international.csv")
    clean_course_csv = os.path.join(EXPORT_DIR, "review_courses_international_clean.csv")
    if os.path.exists(base_course_csv) and os.path.exists(clean_course_csv):
        course_csv = (
            base_course_csv
            if os.path.getmtime(base_course_csv) >= os.path.getmtime(clean_course_csv)
            else clean_course_csv
        )
    elif os.path.exists(clean_course_csv):
        course_csv = clean_course_csv
    else:
        course_csv = base_course_csv
    sch_json = os.path.join(EXPORT_DIR, "review_scholarships_international.json")
    sch_csv = os.path.join(EXPORT_DIR, "review_scholarships_international.csv")

    lines = [
        "-- =====================================================================",
        f"-- International Student Database Enrichment Patch ({now_str})",
        "-- Target Database: u627422877_dashboard (MariaDB / MySQL)",
        "-- Strictly updates ONLY missing/incomplete fields reviewed & approved.",
        "-- =====================================================================",
        "SET NAMES utf8mb4;",
        "ALTER TABLE `courses` ADD COLUMN IF NOT EXISTS `english_requirements` TEXT DEFAULT NULL AFTER `entry_requirements`;",
        "ALTER TABLE `courses` ADD COLUMN IF NOT EXISTS `intakes` TEXT DEFAULT NULL AFTER `total_course_duration`;",
        "START TRANSACTION;",
        "",
    ]

    prov_updates = 0
    contact_inserts = 0
    course_updates = 0
    sch_inserts = 0

    if os.path.exists(prov_json):
        with open(prov_json, "r", encoding="utf-8") as f:
            provs = json.load(f)
        csv_provs = load_csv_by_key(prov_csv, "provider_id")
        lines.append("-- 1. Provider Institution Updates & International Contacts")
        for p in provs:
            pid_str = str(p["provider_id"])
            cp = csv_provs.get(pid_str, p)
            if str(cp.get("approve_update", p.get("approve_update", "YES"))).strip().upper() != "YES":
                continue
            pid = int(p["provider_id"])
            sets = []
            new_web = cp.get("new_website") if "new_website" in cp else p.get("new_website")
            new_intake = cp.get("new_intake_date") if "new_intake_date" in cp else p.get("new_intake_date")
            new_logo = cp.get("new_provider_logo") if "new_provider_logo" in cp else p.get("new_provider_logo")

            if new_web:
                sets.append(f"`website` = {sql_escape(new_web)}")
            if new_intake:
                sets.append(f"`intake_date` = {sql_escape(new_intake[:250])}")
            if new_logo:
                sets.append(f"`provider_logo` = {sql_escape(new_logo[:250])}")
            if sets:
                sets.append(f"`updated_at` = {sql_escape(now_str)}")
                lines.append(
                    f"UPDATE `provider_institution` SET {', '.join(sets)} WHERE `id` = {pid};"
                )
                prov_updates += 1

            email = cp.get("new_contact_email") if "new_contact_email" in cp else p.get("new_contact_email")
            phone = cp.get("new_contact_phone") if "new_contact_phone" in cp else p.get("new_contact_phone")
            if p.get("had_existing_contact") == "NO" and email:
                c_url = (
                    cp.get("new_contact_website_url")
                    or p.get("new_contact_website_url")
                    or new_web
                    or p.get("current_website")
                    or ""
                )
                lines.append(
                    "INSERT INTO `provider_contacts` "
                    "(`user_id`, `provider_institution_id`, `cricos_provider_code`, `email`, `phone`, `contact_website_url`, `created_at`, `updated_at`) "
                    f"VALUES (1, {pid}, {sql_escape(p['cricos_provider_code'])}, "
                    f"{sql_escape(email[:250])}, "
                    f"{sql_escape(phone[:250] if phone else None)}, "
                    f"{sql_escape(c_url[:250])}, {sql_escape(now_str)}, {sql_escape(now_str)});"
                )
                contact_inserts += 1
        lines.append("")

    if os.path.exists(course_json):
        with open(course_json, "r", encoding="utf-8") as f:
            courses = json.load(f)
        csv_courses = load_csv_by_key(course_csv, "course_id")
        lines.append("-- 2. International Course Details Updates")
        for c in courses:
            cid_str = str(c["course_id"])
            cc = csv_courses.get(cid_str, c)
            if str(cc.get("approve_update", c.get("approve_update", "YES"))).strip().upper() != "YES":
                continue
            cid = int(c["course_id"])
            sets = []

            apply_u = cc.get("new_apply_form") if "new_apply_form" in cc else c.get("new_apply_form")
            if apply_u:
                sets.append(f"`apply_form` = {sql_escape(apply_u[:250])}")
            if c.get("new_course_description_html"):
                sets.append(f"`course_description` = {sql_escape(c['new_course_description_html'])}")
            if c.get("new_entry_requirements_html"):
                sets.append(f"`entry_requirements` = {sql_escape(c['new_entry_requirements_html'])}")
            if c.get("new_english_requirements_html"):
                sets.append(f"`english_requirements` = {sql_escape(c['new_english_requirements_html'])}")

            for col_db, col_rev in (
                ("offshore_tuition_fee", "new_offshore_tuition_fee"),
                ("onshore_tuition_fee", "new_onshore_tuition_fee"),
                ("enrolment_fee", "new_enrolment_fee"),
                ("materials_fee", "new_materials_fee"),
            ):
                val = cc.get(col_rev) if col_rev in cc else c.get(col_rev)
                if val not in ("", None):
                    try:
                        sets.append(f"`{col_db}` = {float(val)}")
                    except ValueError:
                        pass

            dur = cc.get("new_total_course_duration") if "new_total_course_duration" in cc else c.get("new_total_course_duration")
            if dur:
                sets.append(f"`total_course_duration` = {sql_escape(dur[:250])}")

            assess = cc.get("new_assessment_style") if "new_assessment_style" in cc else c.get("new_assessment_style")
            if assess in ("exam", "assignment", "mixed"):
                sets.append(f"`assessment_style` = {sql_escape(assess)}")

            intakes_val = cc.get("course_intake_dates") if "course_intake_dates" in cc else c.get("course_intake_dates")
            if intakes_val:
                sets.append(f"`intakes` = {sql_escape(str(intakes_val)[:500])}")

            if sets:
                sets.append(f"`updated_at` = {sql_escape(now_str)}")
                lines.append(f"UPDATE `courses` SET {', '.join(sets)} WHERE `id` = {cid};")
                course_updates += 1
        lines.append("")

    schs = load_csv_by_key(sch_csv, None)
    if not schs and os.path.exists(sch_json):
        with open(sch_json, "r", encoding="utf-8") as f:
            schs = json.load(f)
    if schs:
        lines.append("-- 3. International Student Scholarships (Course ID 0 = Institution-Wide)")
        for s in schs:
            if str(s.get("approve_insert", "YES")).strip().upper() != "YES":
                continue
            pid = int(s["provider_institution_id"])
            desc = (s.get("scholarship_description") or "")[:250]
            amt = (s.get("scholarship_amount") or "")[:250]
            web = (s.get("website") or "")[:250]
            lines.append(
                "INSERT INTO `scholarships` "
                "(`user_id`, `course_id`, `provider_institution_id`, `scholarship_name`, `scholarship_description`, `scholarship_amount`, `website`, `created_at`, `updated_at`) "
                f"VALUES (1, 0, {pid}, {sql_escape(s['scholarship_name'][:250])}, {sql_escape(desc)}, {sql_escape(amt)}, {sql_escape(web)}, {sql_escape(now_str)}, {sql_escape(now_str)});"
            )
            sch_inserts += 1
        lines.append("")

    lines.append("COMMIT;")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(
        f"Generated SQL file: {output_path}\n"
        f"  Provider UPDATEs: {prov_updates}\n"
        f"  Provider Contact INSERTs: {contact_inserts}\n"
        f"  Course UPDATEs: {course_updates}\n"
        f"  Scholarship INSERTs: {sch_inserts}"
    )


if __name__ == "__main__":
    generate_sql()
