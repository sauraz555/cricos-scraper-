# CRICOS Full-Register International Student Database Scraper & Enricher (v4)

High-speed hybrid sync & deep enrichment pipeline (`data.gov.au` Official CRICOS Register API + Study Australia ACIR `X-Inertia` JSON + National Training Register `training.gov.au` API + WordPress REST API + Recursive XML Sitemaps + Official `.pdf` Prospectus/Handbook Parser via `pymupdf` + async `httpx` + headless **Playwright Chromium**) for `u627422877_dashboard` (`provider_institution`, `courses`, `locations`, `course_locations`, `provider_contacts`, and `scholarships`).

Enforces **strict CRICOS Unique IDs** (`cricos_provider_code` for institutions and `cricos_course_code` for courses) with **zero fuzzy matching / zero mismatch**, strictly collecting **International Student** data (zero Domestic CSP/HECS/VSL/TAC/welfare/citizenship contamination and zero copied `onshore == offshore` fees).

---

## Latest Full-CRICOS Extraction Summary & Dates

- **Official CRICOS API Source:** `https://data.gov.au/data/api/3/action/package_show?id=e5ae7059-bfa8-4fa4-a5c0-c13cf3520193` (`metadata_modified: 2026-11-02T08:03:36Z`)
- **Extraction Date:** `2026-11-06`
- **Extraction Window (Local AEST):** `2026-11-02 12:17:15` → `2026-11-06 02:11:44` (Stage 5 Forensic Entry & English Requirements Audit/Re-Extraction & 34-Check Verification completed `2026-11-06T02:28:12+10:00`)
- **Strict Unique IDs Enforced:** `cricos_provider_code` (`UNIQUE INDEX uq_cricos_provider_code`) & `cricos_course_code` (`UNIQUE INDEX uq_cricos_course_code`) — `fuzzy_matching_allowed: false`
- **34-Assertion International Zero-Mismatch & Zero-Defect Audit:** **`0` defects across all 34 checks** (`0` duplicate CRICOS codes, `0` orphan provider codes, `0` provider/title mismatches vs CRICOS API, `0` fuzzy match methods, `0` domestic CSP/HECS/VSL/TAC/welfare/citizenship leaks, `0` pure-domestic ATAR/Selection Rank/VCE/HSC/QCE bullets, `0` domestic/apprenticeship URLs, `0` dummy `$1` fees, `0` copied onshore/offshore fees, `0` chopped decimal IELTS sub-bands, `0` IELTS/PTE/TOEFL concordance contradictions, `0` sub-threshold degree/nursing/teaching IELTS scores, `0` cross-level incompatible academic bullets, `0` marketing/nav/pathway/accordion-heading/truncated bullets, `0` domestic-only scholarships; **`12 / 12` live Playwright checks `HTTP 200` with `1.0` title overlap**)

### Full Australian CRICOS Coverage (`u627422877_dashboard`)

| Entity / Field | Verified Count | Coverage / Notes |
| :--- | ---: | :--- |
| **CRICOS Institutions (`provider_institution`)** | **`1,543`** | `772` existing DB updated + **`771` newly inserted** (`UNIQUE` on `cricos_provider_code`) |
| ├─ Institution Websites (`website`) | **`1,471`** | **`95.3%`** — Official CRICOS & `training.gov.au` RTO-verified domains (`+116` new RTO sites verified; `7` conflicting DDG domains cleared) |
| ├─ Institution Logos (`provider_logo`) | `1,305` | `84.6%` — Official ACIR & `og:image` logos |
| ├─ International Admissions Contacts (`provider_contacts`) | **`1,211`** | **`78.5%`** — Verified admissions emails (`1,135` net-new contact inserts; `0` press/IT/system emails) |
| └─ Provider Intake Dates (`intake_date`) | **`531`** | Institution-level international intake schedules |
| **Active CRICOS Courses (`courses`)** | **`25,199`** | `8,106` existing DB updated + **`17,093` newly inserted** (`UNIQUE` on `cricos_course_code`) |
| ├─ Total Course Duration (`total_course_duration` / weeks) | **`25,193`** | **`99.98%`** — Authoritative official PRISMS/CRICOS register |
| ├─ Official International Tuition (`offshore_tuition_fee`) | **`24,899`** | **`98.81%`** — Official CRICOS `Tuition Fee` (with PRISMS `$1` exchange placeholders cleared) |
| ├─ Assessment Style (`assessment_style`) | **`21,387`** | **`84.87%`** — `exam`, `assignment`, or `mixed` |
| ├─ Course Description (`course_description`) | **`18,560`** | **`73.65%`** — Clean HTML course & official TGA qualification overviews |
| ├─ Course Intake Dates (`intakes`) | **`18,424`** | **`73.11%`** — International semesters, trimesters & start months |
| ├─ Academic & English Entry Requirements (`entry_requirements`) | **`17,364`** | **`68.91%`** — Forensically verified, AQF level-compatible International prerequisites (`13,205` with explicit academic bullets + clean English section; `0` nav/marketing/truncated/cross-level noise) |
| ├─ CRICOS-Verified Course URL (`apply_form`) | **`14,507`** | **`57.57%`** — Verified deep HTML course URLs |
| ├─ Structured English Requirements (`english_requirements`) | **`14,298`** | **`56.74%`** — Decimal-accurate, concordance-verified IELTS Academic, PTE Academic & TOEFL iBT (`0` chopped sub-bands, `0` ELICOS bridging leaks, NMBA Nursing `>= 7.0` & Higher Ed `>= 6.0` enforced) |
| ├─ Enrolment Fee (`enrolment_fee`) | **`3,975`** | Verified international application/enrolment fee |
| ├─ Materials Fee (`materials_fee`) | **`1,152`** | Verified non-tuition materials/equipment fee |
| └─ Distinct Onshore Promo Fee (`onshore_tuition_fee`) | **`5`** | Live-verified Onshore Student Visa Holder promo fees (`0` copied offshore fees) |
| **Campus Locations (`locations`)** | **`3,850`** | Official CRICOS campus addresses across Australia |
| **Course-Campus Mappings (`course_locations`)** | **`44,855`** | Exact `cricos_course_code` × `location_name` mappings |
| **International Scholarships (`scholarships`)** | **`995`** | Strictly verified International Student eligibility (`68` citizen/PR/Indigenous-only excluded) |
| **Repaired 6-Digit CRICOS Codes** | **`214`** | Restored stripped leading zeros (`100033` → `0100033`) verified by provider |
| **Expired / Deregistered DB Courses Flagged** | **`1,333`** | Flagged `is_expired = 1` without breaking historical student FKs |

---

## Exported Deliverables (`exports/`)

Every file is automatically kept `< 45 MB` for seamless GitHub hosting and phpMyAdmin import:

- `exports/review_providers_international.csv` & `exports/review_providers_international.json` — All `1,543` CRICOS providers keyed by `cricos_provider_code` (`UPDATE_EXISTING` vs `INSERT_NEW_CRICOS`).
- `exports/review_courses_international_verified.csv` & `exports/review_courses_international_part1.jsonl` / `part2.jsonl` — All `25,199` active CRICOS courses keyed by `cricos_course_code`, including IELTS/PTE/TOEFL columns, official CRICOS fees, `course_intake_dates`, `match_method`, `source_url`, and `extraction_date` / `extracted_at`.
- `exports/review_scholarships_international.csv` & `exports/review_scholarships_international.json` — `995` strictly international-eligible scholarships keyed by `cricos_provider_code`.
- `exports/review_repaired_cricos_codes.csv` — Audit log of `214` 6-character DB course codes repaired with their leading `0`.
- `exports/review_expired_courses.csv` — Audit log of `1,333` expired/deregistered DB courses flagged `is_expired = 1`.
- `exports/u627422877_dashboard_intl_updates_part1.sql` & `exports/u627422877_dashboard_intl_updates_part2.sql` — Transactional, idempotent MariaDB / MySQL `INSERT ... ON DUPLICATE KEY UPDATE` patches enforcing `UNIQUE INDEX` on `cricos_provider_code` and `cricos_course_code`.
- `exports/enrichment_summary.json` & `exports/verification_report.json` — Complete coverage metrics, 34-assertion international zero-mismatch audit results, and live Playwright check logs.

---

## Setup & Usage

### 1. Install Dependencies
```powershell
pip install -r requirements.txt
playwright install chromium
```

### 2. Review Spreadsheets & Import SQL Patches
1. Open the CSVs in `exports/` (`review_courses_international_verified.csv`, `review_providers_international.csv`, `review_scholarships_international.csv`).
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
# Full end-to-end sync + scrape + Stage 4 PDF/TGA enrichment + verification:
python run_pipeline.py --sql-dump "C:\path\to\u627422877_dashboard.sql"

# Run Stage 4 Deep Web, PDF Prospectus & TGA VET Enricher + 23-check verification:
python run_pipeline.py --stage4-only

# Run Stage 5 Forensic Entry & English Requirements Re-Extraction + 34-check verification:
python run_pipeline.py --stage5-only

# Run 34-check zero-mismatch sanitization + live Playwright audit + clean exports:
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
5. **`src/entry_english_rules.py`** — Shared forensic rules engine for decimal-preserving IELTS/PTE/TOEFL extraction, cross-test concordance reconciliation, NMBA/AITSL/Higher-Ed minimum score guards, `<nav>`/`<header>`/`<footer>` DOM stripping, PDF line-wrap unwrapping, and 8-gate AQF `course_level`-compatible Academic Entry bullet validation.
6. **`src/stage3_entry_english_enricher.py`** — 3-layer Academic Entry & English Language Requirements extractor with `selectolax` HTML table parsing (IELTS, PTE, TOEFL, Cambridge, OET).
7. **`src/stage4_deep_pdf_web_enricher.py`** — 4-layer deep enricher: official `training.gov.au` RTO website/contact verification, official National Training Register `vet_national_code` qualification descriptions & statutory entry prerequisites, uncapped verified `apply_form` course scraping, and WordPress `/wp-json/wp/v2/` + recursive XML sitemaps + official `.pdf` prospectus/handbook parsing (`pymupdf`).
8. **`src/stage5_entry_requirements_audit_fixer.py`** — 3-phase forensic Entry & English Requirements recovery engine (`orig_courses` raw HTML re-parse + `training.gov.au` statutory VET `EntryRequirements` + live `<main>`-isolated re-fetch across `9,603` verified course & policy URLs).
9. **`src/verify_and_sanitize.py`** — Forensic International-Student sanitizer, **34 zero-mismatch & zero-defect assertions**, clinical pre-registration Nursing/Midwifery IELTS `>= 7.0` policy guard, and live Playwright spot-checker.
10. **`src/export_review_sheets.py`** — Exports side-by-side CSV and auto-chunked `<44 MB` JSONL review sheets with extraction timestamps.
11. **`src/generate_sql_updates.py`** — Generates idempotent `<42 MB` MariaDB/MySQL `UNIQUE INDEX` + `INSERT ... ON DUPLICATE KEY UPDATE` SQL patches.
