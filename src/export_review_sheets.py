"""
Exports complete, strictly CRICOS-keyed CSV and JSON/JSONL review sheets across:
  1. All 1,543 Official CRICOS Providers (`cricos_provider_code` Unique ID)
  2. All 25,199 Active Official CRICOS Courses (`cricos_course_code` Unique ID)
  3. 1,063 Strictly CRICOS-Matched International Scholarships
  4. 214 Repaired Leading-Zero CRICOS Course Codes (`review_repaired_cricos_codes.csv`)
  5. 1,333 Expired / Deregistered DB Courses (`review_expired_courses.csv`)
  6. Master Enrichment Summary (`enrichment_summary.json`)
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

MAX_SINGLE_FILE_BYTES = 44 * 1024 * 1024  # Keep every file < 44 MB so GitHub never warns/rejects


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


def html_to_preview(s: str | None, max_len: int = 320) -> str:
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


def write_jsonl_chunked(base_stem: str, records: list[dict]) -> list[str]:
    """
    Writes JSONL file(s) (`<stem>_part1.jsonl`, `<stem>_part2.jsonl`, ... or `<stem>.jsonl` if < 44 MB)
    and cleans up any stale chunks so total file sizes always stay well below GitHub's 50 MB limit.
    """
    for old_f in os.listdir(EXPORT_DIR):
        if old_f.startswith(base_stem) and (old_f.endswith(".jsonl") or old_f.endswith(".json")):
            try:
                os.remove(os.path.join(EXPORT_DIR, old_f))
            except OSError:
                pass

    serialized = [json.dumps(r, ensure_ascii=False) + "\n" for r in records]
    total_bytes = sum(len(s.encode("utf-8")) for s in serialized)

    written_paths = []
    if total_bytes <= MAX_SINGLE_FILE_BYTES:
        out_p = os.path.join(EXPORT_DIR, f"{base_stem}.jsonl")
        with open(out_p, "w", encoding="utf-8") as f:
            f.writelines(serialized)
        written_paths.append(out_p)
        return written_paths

    part_idx = 1
    cur_lines = []
    cur_bytes = 0
    for line in serialized:
        b_len = len(line.encode("utf-8"))
        if cur_lines and (cur_bytes + b_len > MAX_SINGLE_FILE_BYTES):
            out_p = os.path.join(EXPORT_DIR, f"{base_stem}_part{part_idx}.jsonl")
            with open(out_p, "w", encoding="utf-8") as f:
                f.writelines(cur_lines)
            written_paths.append(out_p)
            part_idx += 1
            cur_lines = []
            cur_bytes = 0
        cur_lines.append(line)
        cur_bytes += b_len

    if cur_lines:
        out_p = os.path.join(EXPORT_DIR, f"{base_stem}_part{part_idx}.jsonl")
        with open(out_p, "w", encoding="utf-8") as f:
            f.writelines(cur_lines)
        written_paths.append(out_p)

    return written_paths


def load_courses_jsonl() -> list[dict]:
    single = os.path.join(EXPORT_DIR, "review_courses_international.jsonl")
    if os.path.exists(single):
        with open(single, "r", encoding="utf-8") as f:
            return [json.loads(ln) for ln in f if ln.strip()]
    parts = sorted(
        f for f in os.listdir(EXPORT_DIR)
        if f.startswith("review_courses_international_part") and f.endswith(".jsonl")
    )
    out = []
    for p in parts:
        with open(os.path.join(EXPORT_DIR, p), "r", encoding="utf-8") as f:
            out.extend(json.loads(ln) for ln in f if ln.strip())
    return out


def export_all():
    os.makedirs(EXPORT_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    existing_contacts = set()
    if "orig_provider_contacts" in tables:
        for r in conn.execute("SELECT cricos_provider_code FROM orig_provider_contacts"):
            if r[0]:
                existing_contacts.add(r[0].strip().upper())

    # ------------------------------------------------------------------
    # 1. ALL 1,543 OFFICIAL CRICOS PROVIDERS (Unique ID = cricos_provider_code)
    # ------------------------------------------------------------------
    prov_rows_out = []
    prov_query = """
        SELECT mp.*,
               ep.website AS ep_website,
               ep.international_url AS ep_international_url,
               ep.intake_date AS ep_intake_date,
               ep.provider_logo AS ep_provider_logo,
               ep.contact_email AS ep_contact_email,
               ep.contact_phone AS ep_contact_phone,
               ep.contact_website_url AS ep_contact_website_url,
               ep.scholarships_json AS ep_scholarships_json,
               ep.source_url AS ep_source_url,
               ep.discovery_method AS ep_discovery_method,
               ep.updated_at AS extracted_at
        FROM master_providers mp
        LEFT JOIN enriched_providers ep ON ep.cricos_provider_code = mp.cricos_provider_code
        ORDER BY mp.is_partner DESC, mp.cricos_provider_code ASC
    """
    for r in conn.execute(prov_query):
        code = r["cricos_provider_code"]
        has_contact = code in existing_contacts
        in_db = int(r["in_existing_db"] or 0) == 1

        final_web = r["ep_website"] or r["cricos_website"] or r["db_website"] or ""
        final_intake = r["ep_intake_date"] or r["db_intake_date"] or ""
        final_logo = r["ep_provider_logo"] or r["db_provider_logo"] or ""

        sch_count = 0
        if r["ep_scholarships_json"]:
            try:
                sch_count = len(json.loads(r["ep_scholarships_json"]))
            except Exception:
                pass

        prov_rows_out.append(
            {
                "approve_upsert": "YES",
                "cricos_provider_code": code,
                "record_action": "UPDATE_EXISTING" if in_db else "INSERT_NEW_CRICOS",
                "db_provider_id": r["db_provider_id"] or "",
                "institution_name": r["institution_name"],
                "trading_name": r["trading_name"] or "",
                "institution_type": r["institution_type"] or "",
                "institution_capacity": r["institution_capacity"] or "",
                "postal_address_city": r["postal_address_city"] or "",
                "postal_address_state": r["postal_address_state"] or "",
                "postal_address_postcode": r["postal_address_postcode"] or "",
                "is_partner": int(r["is_partner"] or 0),
                "current_website": r["db_website"] or "",
                "new_website": final_web,
                "international_page_url": r["ep_international_url"] or "",
                "current_intake_date": r["db_intake_date"] or "",
                "new_intake_date": final_intake,
                "new_provider_logo": final_logo,
                "had_existing_contact": "YES" if has_contact else "NO",
                "new_contact_email": r["ep_contact_email"] or "",
                "new_contact_phone": r["ep_contact_phone"] or "",
                "new_contact_website_url": r["ep_contact_website_url"] or "",
                "international_scholarships_found": sch_count,
                "discovery_method": r["ep_discovery_method"] or "cricos_official_register",
                "source_url": r["ep_source_url"] or final_web or "https://cricos.education.gov.au/",
                "extraction_date": (r["extracted_at"] or "")[:10] or dt.date.today().isoformat(),
                "extracted_at": r["extracted_at"] or "",
            }
        )

    prov_csv_path = safe_write_csv(os.path.join(EXPORT_DIR, "review_providers_international.csv"), prov_rows_out)
    prov_json_path = os.path.join(EXPORT_DIR, "review_providers_international.json")
    with open(prov_json_path, "w", encoding="utf-8") as f:
        json.dump(prov_rows_out, f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------
    # 2. ALL 25,199 ACTIVE OFFICIAL CRICOS COURSES (Unique ID = cricos_course_code)
    # ------------------------------------------------------------------
    course_rows_out = []
    course_json_out = []

    field_fill_counts = {
        "offshore_tuition_fee_official_cricos": 0,
        "total_course_duration": 0,
        "apply_form": 0,
        "course_description": 0,
        "entry_requirements": 0,
        "english_requirements": 0,
        "onshore_tuition_fee_distinct_promo": 0,
        "enrolment_fee": 0,
        "materials_fee": 0,
        "assessment_style": 0,
        "intakes": 0,
    }

    course_query = """
        SELECT mc.*,
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
               ec.match_confidence AS match_confidence,
               ec.match_method AS match_method,
               ec.source_url AS source_url,
               ec.updated_at AS extracted_at
        FROM master_courses mc
        LEFT JOIN enriched_courses ec ON ec.cricos_course_code = mc.cricos_course_code
        ORDER BY mc.is_partner DESC, mc.cricos_provider_code ASC, mc.cricos_course_code ASC
    """

    for r in conn.execute(course_query):
        in_db = int(r["in_existing_db"] or 0) == 1

        final_url = r["new_apply_form"] or ""
        if final_url:
            field_fill_counts["apply_form"] += 1

        desc_html = r["new_course_description"] or r["db_course_description"] or ""
        if desc_html:
            field_fill_counts["course_description"] += 1

        entry_html = r["new_entry_requirements"] or r["db_entry_requirements"] or ""
        if entry_html:
            field_fill_counts["entry_requirements"] += 1

        eng_html = r["new_english_requirements"] or ""
        if eng_html:
            field_fill_counts["english_requirements"] += 1

        # Authoritative official CRICOS Tuition Fee first, then verified ACIR/page overseas fee
        offshore_fee = r["cricos_tuition_fee"] or r["new_offshore_tuition_fee"] or r["db_offshore_tuition_fee"]
        offshore_str = offshore_fee if offshore_fee is not None else ""
        if offshore_str != "":
            field_fill_counts["offshore_tuition_fee_official_cricos"] += 1

        # Strict rule: onshore_tuition_fee ONLY when distinct verified onshore promo exists (never copy offshore!)
        onshore_fee = r["new_onshore_tuition_fee"]
        if onshore_fee is not None and offshore_fee is not None and abs(float(onshore_fee) - float(offshore_fee)) < 1.0:
            onshore_fee = None
        onshore_str = onshore_fee if onshore_fee is not None else ""
        if onshore_str != "":
            field_fill_counts["onshore_tuition_fee_distinct_promo"] += 1

        enrol_fee = r["new_enrolment_fee"] or r["db_enrolment_fee"]
        enrol_str = enrol_fee if enrol_fee is not None else ""
        if enrol_str != "":
            field_fill_counts["enrolment_fee"] += 1

        mat_fee = r["new_materials_fee"] or r["db_materials_fee"]
        mat_str = mat_fee if mat_fee is not None else ""
        if mat_str != "":
            field_fill_counts["materials_fee"] += 1

        dur_str = (
            r["new_total_course_duration"]
            or r["db_total_course_duration"]
            or (f"{r['duration_weeks']} weeks" if r["duration_weeks"] else "")
        )
        if dur_str:
            field_fill_counts["total_course_duration"] += 1

        assess_str = r["new_assessment_style"] or r["db_assessment_style"] or ""
        if assess_str:
            field_fill_counts["assessment_style"] += 1

        intake_str = r["new_intake_dates"] or ""
        if intake_str:
            field_fill_counts["intakes"] += 1

        csv_rec = {
            "approve_upsert": "YES",
            "cricos_course_code": r["cricos_course_code"],
            "cricos_provider_code": r["cricos_provider_code"],
            "record_action": "UPDATE_EXISTING" if in_db else "INSERT_NEW_CRICOS",
            "db_course_id": r["db_course_id"] or "",
            "institution_name": r["institution_name"],
            "course_name": r["course_name"],
            "vet_national_code": r["vet_national_code"] or "",
            "course_level": r["course_level"] or "",
            "is_partner": int(r["is_partner"] or 0),
            "match_confidence": 1.0,
            "match_method": r["match_method"] or "cricos_official_register",
            "current_apply_form": r["db_apply_form"] or "",
            "new_apply_form": final_url,
            "new_course_description_preview": html_to_preview(desc_html),
            "new_entry_requirements_preview": html_to_preview(entry_html),
            "new_english_requirements_preview": html_to_preview(eng_html),
            "ielts_requirement": r["new_ielts_score"] or "",
            "pte_requirement": r["new_pte_score"] or "",
            "toefl_requirement": r["new_toefl_score"] or "",
            "english_source_url": r["new_english_source_url"] or "",
            "english_method": r["new_english_method"] or "",
            "current_db_offshore_tuition_fee": r["db_offshore_tuition_fee"] or "",
            "new_offshore_tuition_fee": offshore_str,
            "cricos_non_tuition_fee": r["cricos_non_tuition_fee"] if r["cricos_non_tuition_fee"] is not None else "",
            "cricos_estimated_total_cost": r["cricos_estimated_total_cost"] if r["cricos_estimated_total_cost"] is not None else "",
            "new_onshore_tuition_fee": onshore_str,
            "new_enrolment_fee": enrol_str,
            "new_materials_fee": mat_str,
            "duration_weeks": r["duration_weeks"] or "",
            "new_total_course_duration": dur_str,
            "new_assessment_style": assess_str,
            "course_intake_dates": intake_str,
            "provider_scholarships_note": r["new_scholarships_summary"] or "",
            "source_url": r["source_url"] or final_url or "https://cricos.education.gov.au/",
            "extraction_date": (r["extracted_at"] or "")[:10] or dt.date.today().isoformat(),
            "extracted_at": r["extracted_at"] or "",
        }
        course_rows_out.append(csv_rec)

        json_rec = dict(csv_rec)
        json_rec["dual_qualification"] = r["dual_qualification"] or ""
        json_rec["board_field_1_education"] = r["board_field_1_education"] or ""
        json_rec["narrow_field_1_education"] = r["narrow_field_1_education"] or ""
        json_rec["detailed_field_1_education"] = r["detailed_field_1_education"] or ""
        json_rec["board_field_2_education"] = r["board_field_2_education"] or ""
        json_rec["narrow_field_2_education"] = r["narrow_field_2_education"] or ""
        json_rec["detailed_field_2_education"] = r["detailed_field_2_education"] or ""
        json_rec["foundation_studies"] = r["foundation_studies"] or ""
        json_rec["work_component"] = r["work_component"] or ""
        json_rec["work_component_hours_per_week"] = r["work_component_hours_per_week"] or ""
        json_rec["work_component_weeks"] = r["work_component_weeks"] or ""
        json_rec["work_component_total_hours"] = r["work_component_total_hours"] or ""
        json_rec["course_language"] = r["course_language"] or "English"
        json_rec["new_course_description_html"] = desc_html
        json_rec["new_entry_requirements_html"] = entry_html
        json_rec["new_english_requirements_html"] = eng_html
        course_json_out.append(json_rec)

    course_csv_path = safe_write_csv(
        os.path.join(EXPORT_DIR, "review_courses_international_clean.csv"), course_rows_out
    )
    course_jsonl_paths = write_jsonl_chunked("review_courses_international", course_json_out)

    # ------------------------------------------------------------------
    # 3. INTERNATIONAL SCHOLARSHIPS (Strictly matched by cricos_provider_code)
    # ------------------------------------------------------------------
    sch_rows_out = []
    for r in conn.execute(
        """
        SELECT * FROM enriched_scholarships
        WHERE is_international = 1 AND cricos_provider_code IS NOT NULL
        ORDER BY cricos_provider_code ASC, scholarship_name ASC
        """
    ):
        sch_rows_out.append(
            {
                "approve_insert": "YES",
                "cricos_provider_code": r["cricos_provider_code"],
                "db_provider_institution_id": r["provider_id"] or "",
                "institution_name": r["institution_name"],
                "scholarship_name": r["scholarship_name"],
                "scholarship_amount": r["scholarship_amount"] or "",
                "level_of_studies": r["level_of_studies"] or "",
                "field_of_studies": r["field_of_studies"] or "",
                "scholarship_description": r["scholarship_description"] or "",
                "eligibility": r["eligibility"] or "",
                "website": r["website"] or "",
                "source_url": r["source_url"] or "",
                "extraction_date": (r["updated_at"] or "")[:10] or dt.date.today().isoformat(),
                "extracted_at": r["updated_at"] or "",
            }
        )

    sch_csv_path = safe_write_csv(os.path.join(EXPORT_DIR, "review_scholarships_international.csv"), sch_rows_out)
    sch_json_path = os.path.join(EXPORT_DIR, "review_scholarships_international.json")
    with open(sch_json_path, "w", encoding="utf-8") as f:
        json.dump(sch_rows_out, f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------
    # 4. REPAIRED LEADING-ZERO CODES & EXPIRED DB COURSES AUDIT SHEETS
    # ------------------------------------------------------------------
    repaired_rows = [dict(r) for r in conn.execute("SELECT * FROM repaired_db_course_codes ORDER BY new_cricos_course_code")]
    rep_csv_path = safe_write_csv(os.path.join(EXPORT_DIR, "review_repaired_cricos_codes.csv"), repaired_rows)

    expired_rows = [dict(r) for r in conn.execute("SELECT * FROM db_expired_or_removed_courses ORDER BY cricos_provider_code, cricos_course_code")]
    exp_csv_path = safe_write_csv(os.path.join(EXPORT_DIR, "review_expired_courses.csv"), expired_rows)

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
    cricos_meta = dict(conn.execute("SELECT key, value FROM cricos_api_meta").fetchall())

    files_gen = [
        os.path.relpath(prov_csv_path, PROJECT_DIR).replace("\\", "/"),
        os.path.relpath(prov_json_path, PROJECT_DIR).replace("\\", "/"),
        os.path.relpath(course_csv_path, PROJECT_DIR).replace("\\", "/"),
        *[os.path.relpath(p, PROJECT_DIR).replace("\\", "/") for p in course_jsonl_paths],
        os.path.relpath(sch_csv_path, PROJECT_DIR).replace("\\", "/"),
        os.path.relpath(sch_json_path, PROJECT_DIR).replace("\\", "/"),
        os.path.relpath(rep_csv_path, PROJECT_DIR).replace("\\", "/"),
        os.path.relpath(exp_csv_path, PROJECT_DIR).replace("\\", "/"),
    ]

    summary = {
        "unique_id_keys": {
            "providers": "cricos_provider_code",
            "courses": "cricos_course_code",
            "fuzzy_matching_allowed": False,
        },
        "cricos_official_api": cricos_meta,
        "extraction_date": dt.date.today().isoformat(),
        "extraction_window_local": {
            "started_at": min_ts or "",
            "completed_at": max_ts or "",
        },
        "exported_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "providers_total_cricos": len(prov_rows_out),
        "providers_existing_db_updated": sum(1 for p in prov_rows_out if p["record_action"] == "UPDATE_EXISTING"),
        "providers_new_cricos_inserted": sum(1 for p in prov_rows_out if p["record_action"] == "INSERT_NEW_CRICOS"),
        "provider_websites_populated": sum(1 for p in prov_rows_out if p["new_website"]),
        "provider_intake_dates_populated": sum(1 for p in prov_rows_out if p["new_intake_date"]),
        "provider_logos_populated": sum(1 for p in prov_rows_out if p["new_provider_logo"]),
        "provider_contact_emails_populated": sum(1 for p in prov_rows_out if p["new_contact_email"]),
        "courses_total_active_cricos": len(course_rows_out),
        "courses_existing_db_updated": sum(1 for c in course_rows_out if c["record_action"] == "UPDATE_EXISTING"),
        "courses_new_cricos_inserted": sum(1 for c in course_rows_out if c["record_action"] == "INSERT_NEW_CRICOS"),
        "repaired_6digit_leading_zero_codes": len(repaired_rows),
        "expired_or_deregistered_db_courses_flagged": len(expired_rows),
        "international_scholarships_matched": len(sch_rows_out),
        "locations_total_cricos": conn.execute("SELECT COUNT(*) FROM master_locations").fetchone()[0],
        "course_locations_total_cricos": conn.execute("SELECT COUNT(*) FROM master_course_locations").fetchone()[0],
        "course_field_coverage": field_fill_counts,
        "files_generated": files_gen,
    }
    sum_path = os.path.join(EXPORT_DIR, "enrichment_summary.json")
    with open(sum_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("=== EXPORT SUMMARY ===")
    print(json.dumps(summary, indent=2))
    conn.close()


if __name__ == "__main__":
    export_all()
