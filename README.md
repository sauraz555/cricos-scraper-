# CRICOS Full-Register International Student Database Scraper & Enricher (v2)

High-speed hybrid sync & enrichment pipeline (`data.gov.au` Official CRICOS Register API + Study Australia ACIR `X-Inertia` JSON + async `httpx` + headless **Playwright Chromium**) for `u627422877_dashboard` (`provider_institution`, `courses`, `locations`, `course_locations`, `provider_contacts`, and `scholarships`).

Enforces **strict CRICOS Unique IDs** (`cricos_provider_code` for institutions and `cricos_course_code` for courses) with **zero fuzzy matching / zero mismatch**, strictly collecting **International Student** data (zero Domestic CSP/HECS contamination and zero copied `onshore == offshore` fees).

---

## Latest Full-CRICOS Extraction Summary & Dates

- **Official CRICOS API Source:** `https://data.gov.au/data/api/3/action/package_show?id=e5ae7059-bfa8-4fa4-a5c0-c13cf3520193` (`metadata_modified: 2026-11-02T08:03:36Z`)
- **Extraction Date:** `2026-11-05`
- **Extraction Window (Local AEST):** `2026-11-02 12:17:15` → `2026-11-04 17:58:05` (Stage 3 & final verification completed `2026-11-05T09:34:36+10:00`)
- **Strict Unique IDs Enforced:** `cricos_provider_code` (`UNIQUE INDEX uq_cricos_provider_code`) & `cricos_course_code` (`UNIQUE INDEX uq_cricos_course_code`) — `fuzzy_matching_allowed: false`
- **15-Assertion Zero-Mismatch & Zero-Defect Audit:** **`0` defects across all 15 checks** (`0` duplicate CRICOS codes, `0` orphan provider codes, `0` provider/title mismatches vs CRICOS API, `0` fuzzy match methods, `0` domestic CSP leaks, `0` copied onshore/offshore fees; **`8 / 8` live Playwright checks `HTTP 200` with `1.0` title overlap**)

### Full Australian CRICOS Coverage (`u627422877_dashboard`)

| Entity / Field | Verified Count | Notes |
| :--- | ---: | :--- |
| **CRICOS Institutions (`provider_institution`)** | **`1,543`** | `772` existing DB updated + **`771` newly inserted** (`UNIQUE` on `cricos_provider_code`) |
| ├─ Institution Websites (`website`) | `1,362` | Official provider & international student URLs |
| ├─ Institution Logos (`provider_logo`) | `1,305` | Official ACIR & `og:image` logos |
| ├─ International Admissions Contacts (`provider_contacts`) | `988` | Verified admissions emails (`920` net-new contact inserts) |
| └─ Provider Intake Dates (`intake_date`) | `482` | Institution-level international intake schedules |
| **Active CRICOS Courses (`courses`)** | **`25,199`** | `8,106` existing DB updated + **`17,093` newly inserted** (`UNIQUE` on `cricos_course_code`) |
| ├─ Total Course Duration (`total_course_duration` / weeks) | `25,193` | `99.98%` coverage from official PRISMS/CRICOS register |
| ├─ Official International Tuition (`offshore_tuition_fee`) | `24,947` | `99.0%` official CRICOS `Tuition Fee` + non-tuition & total cost |
| ├─ Assessment Style (`assessment_style`) | `21,387` | `exam`, `assignment`, or `mixed` |
| ├─ Course Intake Dates (`intakes`) | `17,933` | International semesters, trimesters & start months |
| ├─ Academic Entry Requirements (`entry_requirements`) | `16,675` | International academic prerequisites (zero CSP/ATAR-only noise) |
| ├─ Course Description (`course_description`) | `15,086` | Structured HTML course overview |
| ├─ CRICOS-Verified Course URL (`apply_form`) | `13,598` | Strictly verified by exact `cricos_course_code` / unique 1-to-1 VET code |
| ├─ Structured English Requirements (`english_requirements`) | `10,458` | IELTS Academic, PTE Academic, TOEFL iBT, Cambridge/OET |
| ├─ Enrolment Fee (`enrolment_fee`) | `3,786` | Verified international application/enrolment fee |
| ├─ Materials Fee (`materials_fee`) | `961` | Verified non-tuition materials/equipment fee |
| └─ Distinct Onshore Promo Fee (`onshore_tuition_fee`) | `5` | Populated **only** when a distinct verified onshore promo fee exists |
| **Campus Locations (`locations`)** | **`3,850`** | Official CRICOS campus addresses across Australia |
| **Course-Campus Mappings (`course_locations`)** | **`44,855`** | Exact `cricos_course_code` × `location_name` mappings |
| **International Scholarships (`scholarships`)** | **`1,063`** | Strictly matched via `cricos_provider_code` (`is_international = 1`) |
| **Repaired 6-Digit CRICOS Codes** | **`214`** | Restored stripped leading zeros (`100033` → `0100033`) verified by provider |
| **Expired / Deregistered DB Courses Flagged** | **`1,333`** | Flagged `is_expired = 1` without breaking historical student FKs |

---

## Exported Deliverables (`exports/`)

Every file is automatically kept `< 45 MB` for seamless GitHub hosting and phpMyAdmin import:

- `exports/review_providers_international.csv` & `exports/review_providers_international.json` — All `1,543` CRICOS providers keyed by `cricos_provider_code` (`UPDATE_EXISTING` vs `INSERT_NEW_CRICOS`).
- `exports/review_courses_international_clean.csv` & `exports/review_courses_international_part1.jsonl` / `part2.jsonl` — All `25,199` active CRICOS courses keyed by `cricos_course_code`, including IELTS/PTE/TOEFL columns, official CRICOS fees, `course_intake_dates`, `match_method`, `source_url`, and `extraction_date` / `extracted_at`.
- `exports/review_scholarships_international.csv` & `exports/review_scholarships_international.json` — `1,063` international scholarships keyed by `cricos_provider_code`.
- `exports/review_repaired_cricos_codes.csv` — Audit log of `214` 6-character DB course codes repaired with their leading `0`.
- `exports/review_expired_courses.csv` — Audit log of `1,333` expired/deregistered DB courses flagged `is_expired = 1`.
- `exports/u627422877_dashboard_intl_updates_part1.sql` & `exports/u627422877_dashboard_intl_updates_part2.sql` — Transactional, idempotent MariaDB / MySQL `INSERT ... ON DUPLICATE KEY UPDATE` patches enforcing `UNIQUE INDEX` on `cricos_provider_code` and `cricos_course_code`.
- `exports/enrichment_summary.json` & `exports/verification_report.json` — Complete coverage metrics, 15-assertion zero-mismatch audit results, and live Playwright check logs.

---

## Setup & Usage

### 1. Install Dependencies
```powershell
pip install -r requirements.txt
playwright install chromium
```

### 2. Review Spreadsheets & Import SQL Patches
1. Open the CSVs in `exports/` (`review_courses_international_clean.csv`, `review_providers_international.csv`, `review_scholarships_international.csv`).
2. Set `approve_upsert` / `approve_insert` to `NO` on any row you want to skip, or edit any `new_*` value and save.
3. Re-generate the MariaDB / MySQL `.sql` patches (if you edited any CSV):
   ```powershell
   python run_pipeline.py --generate-sql
   ```
4. Import Part 1 followed by Part 2 into MariaDB / MySQL:
   ```powershell
   mysql -u YOUR_DB_USER -p u627422877_dashboard < "exports/u627422877_dashboard_intl_updates_part1.sql"
   mysql -u YOUR_DB_USER -p u627422877_dashboard < "exports/u627422877_dashboard_intl_updates_part2.sql"
   ```

### 3. CLI Reference
```powershell
# Full end-to-end sync + scrape + verification on a phpMyAdmin SQL dump:
python run_pipeline.py --sql-dump "C:\path\to\u627422877_dashboard.sql"

# Resume Stage 2 & Stage 3 after an interruption:
python run_pipeline.py --skip-load --skip-stage0 --skip-stage1

# Run 15-check zero-mismatch sanitization + live Playwright audit + clean exports:
python run_pipeline.py --verify-only

# Re-export CSV/JSONL sheets from SQLite:
python run_pipeline.py --export-only

# Generate transactional MariaDB UPSERT .sql patches:
python run_pipeline.py --generate-sql
```

---

## Pipeline Architecture (`src/`)

1. **`src/sql_dump_loader.py`** — Streams existing phpMyAdmin MariaDB dumps into local SQLite (`data/dashboard_mirror.sqlite`).
2. **`src/stage0_cricos_api_sync.py`** — Syncs the live `data.gov.au` CRICOS Register (`1,543` providers, `25,199` active courses, `3,850` campuses, `44,855` course-campus mappings), repairs 6-digit leading-zero codes, and enforces `cricos_provider_code` & `cricos_course_code` primary keys.
3. **`src/stage1_acir_harvester.py`** — Async harvester against Study Australia's `X-Inertia` JSON endpoints, matching strictly by exact `cricos_provider_code` and 7-char `cricos_course_code`.
4. **`src/stage2_playwright_enricher.py`** — Hybrid `httpx` + concurrent headless **Playwright Chromium** pool with `"International"` tab toggling and strict CRICOS / 1-to-1 unique VET code URL verification (zero fuzzy title matching).
5. **`src/stage3_entry_english_enricher.py`** — 3-layer Academic Entry & English Language Requirements extractor with `selectolax` HTML table parsing (IELTS, PTE, TOEFL, Cambridge, OET).
6. **`src/verify_and_sanitize.py`** — Forensic sanitizer, **15 zero-mismatch & zero-defect assertions**, and live Playwright spot-checker.
7. **`src/export_review_sheets.py`** — Exports side-by-side CSV and auto-chunked `<44 MB` JSONL review sheets with extraction timestamps.
8. **`src/generate_sql_updates.py`** — Generates idempotent `<42 MB` MariaDB/MySQL `UNIQUE INDEX` + `INSERT ... ON DUPLICATE KEY UPDATE` SQL patches.
