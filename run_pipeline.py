"""
One-command CLI for the International Student CRICOS Database Enricher.

Examples:
  # 1. Generate the MariaDB/MySQL .sql update file from your reviewed CSV/JSON sheets:
  python run_pipeline.py --generate-sql

  # 2. Run the full enrichment + verification pipeline on a new SQL dump:
  python run_pipeline.py --sql-dump "C:\path\to\new_dump.sql"

  # 3. Re-export clean CSV & JSON review sheets from local SQLite staging DB:
  python run_pipeline.py --export-only

  # 4. Run forensic sanitization + zero-defect verification only:
  python run_pipeline.py --verify-only
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from sql_dump_loader import load_sql_dump, DEFAULT_SQL_PATH, DB_PATH
from stage1_acir_harvester import run_stage1
from stage2_playwright_enricher import run_stage2
from stage3_entry_english_enricher import run_stage3
from verify_and_sanitize import run_sanitization_and_audit
from export_review_sheets import export_all
from generate_sql_updates import generate_sql


def main():
    parser = argparse.ArgumentParser(description="CRICOS International Student Enricher CLI")
    parser.add_argument("--sql-dump", default=DEFAULT_SQL_PATH, help="Path to phpMyAdmin .sql dump")
    parser.add_argument("--skip-load", action="store_true", help="Skip reloading SQL dump if SQLite exists")
    parser.add_argument("--skip-stage1", action="store_true", help="Skip Stage 1 ACIR official harvester")
    parser.add_argument("--skip-stage2", action="store_true", help="Skip Stage 2 Playwright/HTTP course & provider scraper")
    parser.add_argument("--skip-stage3", action="store_true", help="Skip Stage 3 Entry & English requirements extractor")
    parser.add_argument("--no-resume", action="store_true", help="Re-scrape all providers from scratch instead of resuming")
    parser.add_argument("--max-providers", type=int, default=None, help="Optional cap on Stage 2 provider count")
    parser.add_argument("--export-only", action="store_true", help="Only export CSV/JSON review sheets from SQLite")
    parser.add_argument("--verify-only", action="store_true", help="Run sanitization & re-export clean CSV/JSON sheets")
    parser.add_argument(
        "--generate-sql",
        action="store_true",
        help="Generate MariaDB UPDATE/INSERT .sql patch from reviewed CSV/JSON sheets",
    )
    args = parser.parse_args()

    if args.generate_sql:
        generate_sql()
        return

    if args.export_only:
        export_all()
        return

    if args.verify_only:
        run_sanitization_and_audit()
        export_all()
        return

    if not args.skip_load or not os.path.exists(DB_PATH):
        load_sql_dump(sql_path=args.sql_dump)

    if not args.skip_stage1:
        asyncio.run(run_stage1())

    if not args.skip_stage2:
        asyncio.run(run_stage2(max_providers=args.max_providers, resume=not args.no_resume))

    if not args.skip_stage3:
        asyncio.run(run_stage3())

    run_sanitization_and_audit()
    export_all()


if __name__ == "__main__":
    main()
