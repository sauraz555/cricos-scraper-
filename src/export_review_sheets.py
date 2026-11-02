"""
Exports clean, side-by-side CSV and JSON review sheets for:
  1. Incomplete Courses enriched with International Student details
     (exports/review_courses_international.csv & .json)
  2. Incomplete Providers enriched with Website, Logo, Intakes & Contacts
     (exports/review_providers_international.csv & .json)
  3. International Student Scholarships matched to DB institutions
     (exports/review_scholarships_international.csv & .json)
  4. Summary Audit Report (exports/enrichment_summary.json)

Strictly highlights ONLY newly populated fields for records that had missing details
in u627422877_dashboard (2).sql, while showing current DB values alongside new values
so human review is effortless before generating .sql updates.
"""
import csv
import datetime as dt
import json
import os
import re
import sqlite3
from urllib.parse import urlparse

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_DIR, "data", "dashboard_mirror.sqlite")
EXPORT_DIR = os.path.join(PROJECT_DIR, "exports")


def is_empty(val) -> bool:
    if val is None:
        return True
    s = str(val).strip()
    return s in ("", "NULL", "None", "0", "0.0")


GENERIC_LAST_SEGMENTS = {
    "about", "home", "index", "index.html", "index.php", "international",
    "international-students", "courses", "study", "contact", "contact-us", "search",
}


def is_shallow_url(u: str | None, repeated_urls: set[str] | None = None) -> bool:
    if is_empty(u):
        return True
    u_str = str(u).strip()
    if repeated_urls and u_str in repeated_urls:
        return True
    try:
        p = urlparse(u_str)
        path = p.path.rstrip("/")
        if not path:
            return True
        last_seg = path.rsplit("/", 1)[-1].lower()
        return last_seg in GENERIC_LAST_SEGMENTS
    except Exception:
        return True


def html_to_preview(s: str | None, max_len: int = 350) -> str:
    if not s:
        return ""
    clean = re.sub(r"<[^>]+>", " ", s)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:max_len] + ("..." if len(clean) > max_len else "")


def safe_write_csv(path: str, rows: list[dict]) -> str:
    if not rows:
        return path
    candidates = [
        path,
        path.replace(".csv", "_clean.csv"),
        path.replace(".csv", "_v2.csv"),
        path.replace(".csv", "_v3.csv"),
    ]
    for cand in candidates:
        try:
            with open(cand, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            return cand
        except PermissionError:
            continue
    raise PermissionError(f"All CSV output targets locked: {candidates}")


def export_all():
    os.makedirs(EXPORT_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------
    # 1. PROVIDERS REVIEW SHEET (Only providers that had missing details)
    # ------------------------------------------------------------------
    existing_contacts = {}
    for r in conn.execute("SELECT * FROM orig_provider_contacts"):
        existing_contacts.setdefault(r["cricos_provider_code"], []).append(dict(r))

    prov_rows_out = []
    prov_query = """
        SELECT p.*,
               ep.website AS new_website,
               ep.international_url AS new_international_url,
               ep.intake_date AS new_intake_date,
               ep.provider_logo AS new_provider_logo,
               ep.contact_email AS new_contact_email,
               ep.contact_phone AS new_contact_phone,
               ep.contact_website_url AS new_contact_website_url,
               ep.scholarships_json AS new_scholarships_json,
               ep.source_url AS source_url,
               ep.discovery_method AS discovery_method,
               ep.updated_at AS extracted_at
        FROM orig_provider_institution p
        LEFT JOIN enriched_providers ep ON ep.provider_id = p.id
        ORDER BY CAST(p.is_partner AS INTEGER) DESC, p.cricos_provider_code ASC
    """
    for r in conn.execute(prov_query):
        code = r["cricos_provider_code"]
        has_contact = code in existing_contacts
        fields_filled = []

        final_web = r["website"]
        if is_empty(r["website"]) and not is_empty(r["new_website"]):
            final_web = r["new_website"]
            fields_filled.append("website")

        final_intake = r["intake_date"]
        if is_empty(r["intake_date"]) and not is_empty(r["new_intake_date"]):
            final_intake = r["new_intake_date"]
            fields_filled.append("intake_date")

        final_logo = r["provider_logo"]
        if is_empty(r["provider_logo"]) and not is_empty(r["new_provider_logo"]):
            final_logo = r["new_provider_logo"]
            fields_filled.append("provider_logo")

        if not has_contact and (not is_empty(r["new_contact_email"]) or not is_empty(r["new_contact_phone"])):
            fields_filled.append("provider_contacts")

        sch_count = 0
        if r["new_scholarships_json"]:
            try:
                sch_count = len(json.loads(r["new_scholarships_json"]))
                if sch_count > 0:
                    fields_filled.append(f"scholarships({sch_count})")
            except Exception:
                pass

        if not fields_filled:
            continue

        prov_rows_out.append(
            {
                "approve_update": "YES",
                "provider_id": int(r["id"]),
                "cricos_provider_code": code,
                "institution_name": r["institution_name"],
                "trading_name": r["trading_name"] or "",
                "is_partner": int(r["is_partner"] or 0),
                "fields_newly_filled": ", ".join(fields_filled),
                "current_website": r["website"] or "",
                "new_website": final_web if "website" in fields_filled else "",
                "international_page_url": r["new_international_url"] or "",
                "current_intake_date": r["intake_date"] or "",
                "new_intake_date": final_intake if "intake_date" in fields_filled else "",
                "new_provider_logo": final_logo if "provider_logo" in fields_filled else "",
                "had_existing_contact": "YES" if has_contact else "NO",
                "new_contact_email": r["new_contact_email"] or "",
                "new_contact_phone": r["new_contact_phone"] or "",
                "new_contact_website_url": r["new_contact_website_url"] or "",
                "international_scholarships_found": sch_count,
                "discovery_method": r["discovery_method"] or "",
                "source_url": r["source_url"] or "",
                "extraction_date": (r["extracted_at"] or "")[:10],
                "extracted_at": r["extracted_at"] or "",
            }
        )

    prov_csv_path = os.path.join(EXPORT_DIR, "review_providers_international.csv")
    prov_json_path = os.path.join(EXPORT_DIR, "review_providers_international.json")
    prov_csv_path = safe_write_csv(prov_csv_path, prov_rows_out)
    with open(prov_json_path, "w", encoding="utf-8") as f:
        json.dump(prov_rows_out, f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------
    # 2. COURSES REVIEW SHEET (Only courses that had missing details)
    # ------------------------------------------------------------------
    course_rows_out = []
    course_json_out = []

    # Ensure Stage 3 English columns exist even if Stage 3 hasn't run yet
    ec_cols = {r[1] for r in conn.execute("PRAGMA table_info(enriched_courses)")}
    for col in ("english_requirements", "ielts_score", "pte_score", "toefl_score", "english_source_url", "english_method"):
        if col not in ec_cols:
            conn.execute(f"ALTER TABLE enriched_courses ADD COLUMN {col} TEXT;")

    course_query = """
        SELECT c.*,
               ec.apply_form AS new_apply_form,
               ec.course_description AS new_course_description,
               ec.entry_requirements AS new_entry_requirements,
               ec.english_requirements AS new_english_requirements,
               ec.ielts_score AS new_ielts_score,
               ec.pte_score AS new_pte_score,
               ec.toefl_score AS new_toefl_score,
               ec.english_source_url AS new_english_source_url,
               ec.english_method AS new_english_method,
               ec.onshore_tuition_fee AS new_onshore_tuition_fee,
               ec.offshore_tuition_fee AS new_offshore_tuition_fee,
               ec.enrolment_fee AS new_enrolment_fee,
               ec.materials_fee AS new_materials_fee,
               ec.total_course_duration AS new_total_course_duration,
               ec.assessment_style AS new_assessment_style,
               ec.intake_dates AS new_intake_dates,
               ec.scholarships_summary AS new_scholarships_summary,
               ec.international_verified AS international_verified,
               ec.match_confidence AS match_confidence,
               ec.match_method AS match_method,
               ec.source_url AS source_url,
               ec.updated_at AS extracted_at
        FROM orig_courses c
        INNER JOIN enriched_courses ec ON ec.course_id = c.id
        ORDER BY CAST(c.is_partner AS INTEGER) DESC, c.cricos_provider_code ASC, c.cricos_course_code ASC
    """

    field_fill_counts = {
        "apply_form": 0,
        "course_description": 0,
        "entry_requirements": 0,
        "english_requirements": 0,
        "offshore_tuition_fee": 0,
        "onshore_tuition_fee": 0,
        "enrolment_fee": 0,
        "materials_fee": 0,
        "total_course_duration": 0,
        "assessment_style": 0,
        "intakes": 0,
    }

    # Identify generic repeated URLs (same apply_form shared by >= 3 courses)
    repeated_urls = {
        r[0].strip()
        for r in conn.execute(
            "SELECT apply_form FROM orig_courses WHERE apply_form IS NOT NULL GROUP BY apply_form HAVING COUNT(*) >= 3"
        )
        if r[0]
    }

    for r in conn.execute(course_query):
        filled = []

        # 1. Direct Course URL (apply_form)
        new_url = ""
        if is_shallow_url(r["apply_form"], repeated_urls) and not is_shallow_url(r["new_apply_form"]):
            new_url = r["new_apply_form"]
            filled.append("apply_form")
            field_fill_counts["apply_form"] += 1

        # 2. Course Description
        new_desc_html = ""
        if is_empty(r["course_description"]) and not is_empty(r["new_course_description"]):
            new_desc_html = r["new_course_description"]
            filled.append("course_description")
            field_fill_counts["course_description"] += 1

        # 3. International Entry Requirements & English Language Requirements
        new_entry_html = ""
        if is_empty(r["entry_requirements"]) and not is_empty(r["new_entry_requirements"]):
            new_entry_html = r["new_entry_requirements"]
            filled.append("entry_requirements")
            field_fill_counts["entry_requirements"] += 1

        new_eng_html = ""
        if not is_empty(r["new_english_requirements"]):
            new_eng_html = r["new_english_requirements"]
            filled.append("english_requirements")
            field_fill_counts["english_requirements"] += 1

        # 4. Fees (International Onshore / Offshore / Enrolment / Materials)
        new_offshore = ""
        if is_empty(r["offshore_tuition_fee"]) and not is_empty(r["new_offshore_tuition_fee"]):
            new_offshore = r["new_offshore_tuition_fee"]
            filled.append("offshore_tuition_fee")
            field_fill_counts["offshore_tuition_fee"] += 1

        new_onshore = ""
        if is_empty(r["onshore_tuition_fee"]) and not is_empty(r["new_onshore_tuition_fee"]):
            new_onshore = r["new_onshore_tuition_fee"]
            filled.append("onshore_tuition_fee")
            field_fill_counts["onshore_tuition_fee"] += 1

        new_enrol = ""
        if is_empty(r["enrolment_fee"]) and not is_empty(r["new_enrolment_fee"]):
            new_enrol = r["new_enrolment_fee"]
            filled.append("enrolment_fee")
            field_fill_counts["enrolment_fee"] += 1

        new_mat = ""
        if is_empty(r["materials_fee"]) and not is_empty(r["new_materials_fee"]):
            new_mat = r["new_materials_fee"]
            filled.append("materials_fee")
            field_fill_counts["materials_fee"] += 1

        # 5. Total Course Duration
        new_dur = ""
        if is_empty(r["total_course_duration"]) and not is_empty(r["new_total_course_duration"]):
            new_dur = r["new_total_course_duration"]
            filled.append("total_course_duration")
            field_fill_counts["total_course_duration"] += 1

        # 6. Assessment Style
        new_assess = ""
        if is_empty(r["assessment_style"]) and not is_empty(r["new_assessment_style"]):
            new_assess = r["new_assessment_style"]
            filled.append("assessment_style")
            field_fill_counts["assessment_style"] += 1

        # 7. International Course Intake Dates
        new_intakes = ""
        if not is_empty(r["new_intake_dates"]):
            new_intakes = r["new_intake_dates"]
            filled.append("intakes")
            field_fill_counts["intakes"] += 1

        if not filled:
            continue

        csv_rec = {
            "approve_update": "YES",
            "course_id": int(r["id"]),
            "cricos_provider_code": r["cricos_provider_code"],
            "institution_name": r["institution_name"],
            "cricos_course_code": r["cricos_course_code"],
            "vet_national_code": r["vet_national_code"] or "",
            "course_name": r["course_name"],
            "course_level": r["course_level"] or "",
            "is_partner": int(r["is_partner"] or 0),
            "fields_newly_filled": ", ".join(filled),
            "international_verified": "YES" if r["international_verified"] else "CHECK",
            "match_confidence": round(float(r["match_confidence"] or 0.0), 2),
            "match_method": r["match_method"] or "",
            "current_apply_form": r["apply_form"] or "",
            "new_apply_form": new_url,
            "new_course_description_preview": html_to_preview(new_desc_html),
            "current_entry_requirements_preview": html_to_preview(r["entry_requirements"]),
            "new_entry_requirements_preview": html_to_preview(new_entry_html),
            "new_english_requirements_preview": html_to_preview(new_eng_html),
            "ielts_requirement": r["new_ielts_score"] or "",
            "pte_requirement": r["new_pte_score"] or "",
            "toefl_requirement": r["new_toefl_score"] or "",
            "english_source_url": r["new_english_source_url"] or "",
            "english_method": r["new_english_method"] or "",
            "current_offshore_tuition_fee": r["offshore_tuition_fee"] or "",
            "new_offshore_tuition_fee": new_offshore,
            "current_onshore_tuition_fee": r["onshore_tuition_fee"] or "",
            "new_onshore_tuition_fee": new_onshore,
            "new_enrolment_fee": new_enrol,
            "new_materials_fee": new_mat,
            "current_duration_weeks": r["course_duration_per_week"] or "",
            "new_total_course_duration": new_dur,
            "new_assessment_style": new_assess,
            "course_intake_dates": new_intakes,
            "provider_scholarships_note": r["new_scholarships_summary"] or "",
            "source_url": r["source_url"] or "",
            "extraction_date": (r["extracted_at"] or "")[:10],
            "extracted_at": r["extracted_at"] or "",
        }
        course_rows_out.append(csv_rec)

        json_rec = dict(csv_rec)
        json_rec["new_course_description_html"] = new_desc_html
        json_rec["new_entry_requirements_html"] = new_entry_html
        json_rec["new_english_requirements_html"] = new_eng_html
        course_json_out.append(json_rec)

    course_csv_path = os.path.join(EXPORT_DIR, "review_courses_international.csv")
    course_json_path = os.path.join(EXPORT_DIR, "review_courses_international.json")
    course_csv_path = safe_write_csv(course_csv_path, course_rows_out)
    with open(course_json_path, "w", encoding="utf-8") as f:
        json.dump(course_json_out, f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------
    # 3. INTERNATIONAL SCHOLARSHIPS REVIEW SHEET (Matched to DB Providers)
    # ------------------------------------------------------------------
    sch_rows_out = []
    for r in conn.execute(
        """
        SELECT * FROM enriched_scholarships
        WHERE is_international = 1 AND provider_id IS NOT NULL
        ORDER BY cricos_provider_code ASC, scholarship_name ASC
        """
    ):
        sch_rows_out.append(
            {
                "approve_insert": "YES",
                "provider_institution_id": int(r["provider_id"]),
                "cricos_provider_code": r["cricos_provider_code"],
                "institution_name": r["institution_name"],
                "scholarship_name": r["scholarship_name"],
                "scholarship_amount": r["scholarship_amount"] or "",
                "level_of_studies": r["level_of_studies"] or "",
                "field_of_studies": r["field_of_studies"] or "",
                "scholarship_description": r["scholarship_description"] or "",
                "eligibility": r["eligibility"] or "",
                "website": r["website"] or "",
                "source_url": r["source_url"] or "",
                "extraction_date": (r["updated_at"] or "")[:10],
                "extracted_at": r["updated_at"] or "",
            }
        )

    sch_csv_path = os.path.join(EXPORT_DIR, "review_scholarships_international.csv")
    sch_json_path = os.path.join(EXPORT_DIR, "review_scholarships_international.json")
    if sch_rows_out:
        with open(sch_csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(sch_rows_out[0].keys()))
            w.writeheader()
            w.writerows(sch_rows_out)
    with open(sch_json_path, "w", encoding="utf-8") as f:
        json.dump(sch_rows_out, f, indent=2, ensure_ascii=False)

    min_ts, max_ts = conn.execute(
        """
        SELECT MIN(ts), MAX(ts) FROM (
            SELECT updated_at AS ts FROM enriched_providers WHERE updated_at IS NOT NULL
            UNION ALL
            SELECT updated_at AS ts FROM enriched_courses WHERE updated_at IS NOT NULL
            UNION ALL
            SELECT updated_at AS ts FROM enriched_scholarships WHERE updated_at IS NOT NULL
        )
        """
    ).fetchone()
    now_iso = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    prov_intake_filled = sum(1 for row in prov_rows_out if row.get("new_intake_date"))

    summary = {
        "extraction_date": (max_ts or "")[:10] or dt.date.today().isoformat(),
        "extraction_window_local": {
            "started_at": min_ts or "",
            "completed_at": max_ts or "",
        },
        "exported_at": now_iso,
        "providers_enriched_count": len(prov_rows_out),
        "provider_intake_dates_filled": prov_intake_filled,
        "courses_enriched_count": len(course_rows_out),
        "international_scholarships_matched_to_db_providers": len(sch_rows_out),
        "course_field_fill_breakdown": field_fill_counts,
        "files_generated": [
            os.path.relpath(prov_csv_path, PROJECT_DIR).replace("\\", "/"),
            os.path.relpath(prov_json_path, PROJECT_DIR).replace("\\", "/"),
            os.path.relpath(course_csv_path, PROJECT_DIR).replace("\\", "/"),
            os.path.relpath(course_json_path, PROJECT_DIR).replace("\\", "/"),
            os.path.relpath(sch_csv_path, PROJECT_DIR).replace("\\", "/"),
            os.path.relpath(sch_json_path, PROJECT_DIR).replace("\\", "/"),
        ],
    }
    sum_path = os.path.join(EXPORT_DIR, "enrichment_summary.json")
    with open(sum_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("=== EXPORT SUMMARY ===")
    print(json.dumps(summary, indent=2))
    conn.close()


if __name__ == "__main__":
    export_all()
