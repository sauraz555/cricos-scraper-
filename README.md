# CRICOS International Student Database Scraper & Enricher

High-speed hybrid enrichment pipeline (`httpx` + official Study Australia ACIR JSON + headless **Playwright Chromium**) for `u627422877_dashboard` (`provider_institution`, `courses`, `provider_contacts`, and `scholarships`), strictly collecting **International Student** details, entry/English requirements, intake dates, and extraction timestamps for incomplete records.

---

## Latest Extraction Summary & Dates

- **Extraction Date:** `2026-11-02`
- **Extraction Window (Local AEST):** `2026-11-02 12:08:30` → `2026-11-02 13:58:49`
- **Forensic Audit & Live Verification:** `2026-11-02T14:27:39+10:00` (`0` defects across all 9 assertions; `8 / 8` live Playwright checks `HTTP 200`)
- **Per-Record Timestamps:** Every row in the exported CSV and JSON sheets includes `extraction_date` (`YYYY-MM-DD`) and `extracted_at` (`YYYY-MM-DD HH:MM:SS`), alongside extracted **course intake dates** (`course_intake_dates` / `intakes`) and **provider intake dates** (`new_intake_date` / `intake_date`).

### Coverage Breakdown (`u627422877_dashboard`)

| Entity / Field | Newly Enriched Count | Notes |
| :--- | ---: | :--- |
| **Courses Enriched (`courses`)** | **`9,310` / `9,439`** | `98.6%` of incomplete CRICOS courses enriched |
| ├─ Course Intake Dates (`intakes`) | `6,971` | International semesters, trimesters & start dates |
| ├─ Assessment Style (`assessment_style`) | `7,524` | `exam`, `assignment`, or `mixed` |
| ├─ Total Course Duration (`total_course_duration`) | `6,106` | Verified CRICOS duration |
| ├─ English Requirements (`english_requirements`) | `4,615` | IELTS Academic, PTE Academic, TOEFL iBT, CAE/OET |
| ├─ Academic Entry Requirements (`entry_requirements`) | `2,619` | Overseas qualification & academic prerequisites |
| ├─ Direct Course URL (`apply_form`) | `1,812` | Deep course URLs replacing blank/generic links |
| ├─ Enrolment Fee (`enrolment_fee`) | `1,612` | Non-tuition international application/enrolment fee |
| ├─ Course Description (`course_description`) | `1,301` | Structured HTML course overview |
| ├─ Materials Fee (`materials_fee`) | `246` | Non-tuition materials/equipment fee |
| ├─ Offshore / Standard Intl Tuition (`offshore_tuition_fee`) | `50` | Strictly `overseas_full_fee` (zero Domestic CSP) |
| └─ Distinct Onshore Promo Fee (`onshore_tuition_fee`) | `4` | Populated **only** when a separate onshore fee exists |
| **Providers Enriched (`provider_institution`)** | **`731` / `802`** | Websites (`135`), Logos (`720`), **Intake Dates (`185`)**, Contacts (`486` emails / `457` phones) |
| **International Scholarships (`scholarships`)** | **`1,026`** | Matched across `123` institutions (`is_international = 1`) |

---

## Exported Deliverables (`exports/`)

- `exports/review_courses_international_clean.csv` & `exports/review_courses_international.json` — Side-by-side `current_*` vs `new_*` course data, IELTS/PTE/TOEFL scores, `course_intake_dates`, `source_url`, and `extracted_at` timestamps.
- `exports/review_providers_international.csv` & `exports/review_providers_international.json` — Enriched institution websites, `new_intake_date`, logos, international admissions contacts, and `extracted_at` timestamps.
- `exports/review_scholarships_international.csv` & `exports/review_scholarships_international.json` — Official international scholarships with eligibility, amounts, URLs, and `extracted_at` timestamps.
- `exports/u627422877_dashboard_intl_updates.sql` — Ready-to-import transactional MariaDB / MySQL `UPDATE` & `INSERT` patch (`724` provider updates, `482` contact inserts, `9,310` course updates, `1,026` scholarship inserts).
- `exports/enrichment_summary.json` & `exports/verification_report.json` — Run timestamps, field breakdown, and zero-defect Playwright audit results.

---

## Setup & Usage

### 1. Install Dependencies
```powershell
pip install -r requirements.txt
playwright install chromium
```

### 2. Review Spreadsheets & Generate SQL Patch
1. Open the CSVs in `exports/` (`review_courses_international_clean.csv`, `review_providers_international.csv`, `review_scholarships_international.csv`).
2. Set `approve_update` / `approve_insert` to `NO` on any row you wish to skip, or edit any `new_*` value directly and save.
3. Generate the MariaDB / MySQL `.sql` patch:
   ```powershell
   python run_pipeline.py --generate-sql
   ```
4. Import `exports/u627422877_dashboard_intl_updates.sql` via phpMyAdmin or MySQL CLI:
   ```powershell
   mysql -u YOUR_DB_USER -p u627422877_dashboard < "exports/u627422877_dashboard_intl_updates.sql"
   ```

### 3. CLI Reference (Running on New SQL Dumps)
```powershell
# Full pipeline on a new phpMyAdmin SQL dump:
python run_pipeline.py --sql-dump "C:\path\to\u627422877_dashboard.sql" --no-resume

# Resume an interrupted Stage 2/3 scrape:
python run_pipeline.py --skip-load --skip-stage1

# Re-run forensic sanitization + verification + clean exports:
python run_pipeline.py --verify-only

# Re-export CSV/JSON sheets from SQLite mirror:
python run_pipeline.py --export-only
```

---

## Pipeline Architecture (`src/`)

1. **`src/sql_dump_loader.py`** — Streams the 627 MB MariaDB dump into an indexed local SQLite mirror (`data/dashboard_mirror.sqlite`) in ~5s.
2. **`src/stage1_acir_harvester.py`** — Async HTTP/2 harvester against Study Australia's `X-Inertia` JSON endpoints for official CRICOS overseas fees, durations, intake dates, entry requirements, logos, and international scholarships.
3. **`src/stage2_playwright_enricher.py`** — Hybrid `httpx` + concurrent headless **Playwright Chromium** pool with `"International"` tab toggling and DuckDuckGo missing-website discovery.
4. **`src/stage3_entry_english_enricher.py`** — 3-layer Academic Entry & English Language Requirements extractor with `selectolax` HTML table parsing (IELTS, PTE, TOEFL, Cambridge, OET).
5. **`src/verify_and_sanitize.py`** — Forensic sanitizer, 9 zero-defect assertions, and live Playwright spot-checker.
6. **`src/export_review_sheets.py`** — Generates side-by-side CSV/JSON review sheets with extraction & intake dates.
7. **`src/generate_sql_updates.py`** — Generates transactional MariaDB/MySQL `UPDATE` & `INSERT` SQL patches.
