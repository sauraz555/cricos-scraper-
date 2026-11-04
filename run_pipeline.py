"""
One-command CLI for the Full-Register CRICOS International Student Enricher (v2).
Enforces `cricos_provider_code` and `cricos_course_code` as strict Unique IDs with zero mismatch.
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from sql_dump_loader import DEFAULT_SQL_PATH, DB_PATH, load_sql_dump
from stage0_cricos_api_sync import run_stage0
from stage1_acir_harvester import run_stage1
from stage2_playwright_enricher import run_stage2
from stage3_entry_english_enricher import run_stage3
from verify_and_sanitize import run_full_verification
from export_review_sheets import export_all
from generate_sql_updates import generate_sql


def main():
    parser = argparse.ArgumentParser(description="CRICOS Full Register International Student Enricher CLI")
    parser.add_argument("--sql-dump", default=DEFAULT_SQL_PATH, help="Optional phpMyAdmin .sql dump to merge with CRICOS API")
    parser.add_argument("--skip-load", action="store_true", help="Skip reloading SQL dump if SQLite exists")
    parser.add_argument("--skip-stage0", action="store_true", help="Skip Stage 0 data.gov.au CRICOS API sync")
    parser.add_argument("--skip-stage1", action="store_true", help="Skip Stage 1 ACIR official harvester")
    parser.add_argument("--skip-stage2", action="store_true", help="Skip Stage 2 Playwright/HTTP course & provider scraper")
    parser.add_argument("--skip-stage3", action="store_true", help="Skip Stage 3 Entry & English requirements extractor")
    parser.add_argument("--no-resume", action="store_true", help="Re-scrape all providers from scratch instead of resuming")
    parser.add_argument("--max-providers", type=int, default=None, help="Optional cap on Stage 2 provider count")
    parser.add_argument("--export-only", action="store_true", help="Only export CSV/JSONL review sheets from SQLite")
    parser.add_argument("--verify-only", action="store_true", help="Run zero-mismatch sanitization, verification & export")
    parser.add_argument(
        "--generate-sql",
        action="store_true",
        help="Generate MariaDB UPSERT .sql patch(es) keyed by cricos_provider_code & cricos_course_code",
    )
    args = parser.parse_args()

    if args.generate_sql:
        generate_sql()
        return

    if args.export_only:
        export_all()
        return

    if args.verify_only:
        run_full_verification(run_live_playwright=True)
        return

    if (not args.skip_load or not os.path.exists(DB_PATH)) and os.path.exists(args.sql_dump):
        load_sql_dump(sql_path=args.sql_dump)

    if not args.skip_stage0:
        run_stage0()

    if not args.skip_stage1:
        asyncio.run(run_stage1())

    if not args.skip_stage2:
        asyncio.run(run_stage2(max_providers=args.max_providers, resume=not args.no_resume))

    if not args.skip_stage3:
        asyncio.run(run_stage3())

    run_full_verification(run_live_playwright=True)


if __name__ == "__main__":
    main()
