"""
Fast streaming loader that extracts college/university/course tables from the
phpMyAdmin MariaDB SQL dump into a local SQLite mirror + staging database.
"""
import json
import os
import sqlite3
import time

DEFAULT_SQL_PATH = os.path.expanduser(os.path.join("~", "Downloads", "u627422877_dashboard (2).sql"))
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_DIR, "data", "dashboard_mirror.sqlite")

TARGET_TABLES = {
    "provider_institution",
    "courses",
    "provider_contacts",
    "scholarships",
    "interlace_scholarships",
    "course_career_outcomes",
}


def parse_sql_tuple(s: str) -> list:
    """Parse a single MySQL VALUES tuple interior (without outer parens)."""
    vals = []
    i = 0
    n = len(s)
    while i < n:
        while i < n and s[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        if s[i] == "'":
            i += 1
            buf = []
            while i < n:
                ch = s[i]
                if ch == "\\" and i + 1 < n:
                    nxt = s[i + 1]
                    if nxt == "n":
                        buf.append("\n")
                    elif nxt == "r":
                        buf.append("\r")
                    elif nxt == "t":
                        buf.append("\t")
                    elif nxt == "0":
                        buf.append("\0")
                    else:
                        buf.append(nxt)
                    i += 2
                elif ch == "'":
                    if i + 1 < n and s[i + 1] == "'":
                        buf.append("'")
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    buf.append(ch)
                    i += 1
            vals.append("".join(buf))
            while i < n and s[i] != ",":
                i += 1
            i += 1
        else:
            j = s.find(",", i)
            if j == -1:
                tok = s[i:].strip()
                i = n
            else:
                tok = s[i:j].strip()
                i = j + 1
            if tok == "NULL":
                vals.append(None)
            else:
                vals.append(tok)
    return vals


def split_values_block(block: str):
    """
    Yield individual tuple interiors from a multi-line or multi-tuple MySQL
    VALUES block, safely respecting single-quoted strings and escapes.
    """
    in_str = False
    escape = False
    depth = 0
    start = None
    for idx, ch in enumerate(block):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "'":
                in_str = False
        else:
            if ch == "'":
                in_str = True
            elif ch == "(":
                if depth == 0:
                    start = idx + 1
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and start is not None:
                    yield block[start:idx]
                    start = None


def init_enrichment_tables(conn: sqlite3.Connection):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS enriched_providers (
            provider_id INTEGER PRIMARY KEY,
            cricos_provider_code TEXT NOT NULL,
            institution_name TEXT NOT NULL,
            website TEXT,
            international_url TEXT,
            intake_date TEXT,
            provider_logo TEXT,
            contact_email TEXT,
            contact_phone TEXT,
            contact_website_url TEXT,
            scholarships_json TEXT,
            source_url TEXT,
            discovery_method TEXT,
            status TEXT DEFAULT 'pending',
            error_msg TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS enriched_courses (
            course_id INTEGER PRIMARY KEY,
            cricos_provider_code TEXT NOT NULL,
            institution_name TEXT NOT NULL,
            cricos_course_code TEXT NOT NULL,
            course_name TEXT NOT NULL,
            apply_form TEXT,
            course_description TEXT,
            entry_requirements TEXT,
            onshore_tuition_fee REAL,
            offshore_tuition_fee REAL,
            enrolment_fee REAL,
            materials_fee REAL,
            total_course_duration TEXT,
            assessment_style TEXT,
            intake_dates TEXT,
            scholarships_summary TEXT,
            international_verified INTEGER DEFAULT 0,
            match_confidence REAL DEFAULT 0.0,
            match_method TEXT,
            source_url TEXT,
            status TEXT DEFAULT 'pending',
            error_msg TEXT,
            updated_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_enriched_courses_provider
            ON enriched_courses(cricos_provider_code);
        CREATE INDEX IF NOT EXISTS idx_enriched_courses_status
            ON enriched_courses(status);
        CREATE INDEX IF NOT EXISTS idx_enriched_providers_status
            ON enriched_providers(status);
        """
    )


def load_sql_dump(sql_path: str = DEFAULT_SQL_PATH, db_path: str = DB_PATH):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    t0 = time.time()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")

    created_tables = set()
    table_cols = {}

    cur_table = None
    buf = []

    def flush_statement(tname: str, full_stmt: str):
        v_pos = full_stmt.find(") VALUES")
        if v_pos == -1:
            return 0
        if tname not in created_tables:
            p1 = full_stmt.find("(")
            cols = [c.strip().strip("`") for c in full_stmt[p1 + 1 : v_pos].split(",")]
            table_cols[tname] = cols
            col_defs = ", ".join(
                f'"{c}" INTEGER PRIMARY KEY' if c == "id" else f'"{c}" TEXT'
                for c in cols
            )
            conn.execute(f'DROP TABLE IF EXISTS "orig_{tname}";')
            conn.execute(f'CREATE TABLE "orig_{tname}" ({col_defs});')
            created_tables.add(tname)

        cols = table_cols[tname]
        placeholders = ", ".join(["?"] * len(cols))
        rows_to_insert = []
        values_part = full_stmt[v_pos + len(") VALUES") :]
        for tup_str in split_values_block(values_part):
            vals = parse_sql_tuple(tup_str)
            if len(vals) == len(cols):
                rows_to_insert.append(vals)
        if rows_to_insert:
            conn.executemany(
                f'INSERT OR REPLACE INTO "orig_{tname}" VALUES ({placeholders})',
                rows_to_insert,
            )
        return len(rows_to_insert)

    counts = {t: 0 for t in TARGET_TABLES}
    with open(sql_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if cur_table is None:
                if line.startswith("INSERT INTO `"):
                    tname = line.split("`")[1]
                    if tname in TARGET_TABLES:
                        cur_table = tname
                        buf = [line]
                        if line.rstrip().endswith(";"):
                            counts[cur_table] += flush_statement(cur_table, "".join(buf))
                            cur_table = None
                            buf = []
            else:
                buf.append(line)
                if line.rstrip().endswith(";"):
                    counts[cur_table] += flush_statement(cur_table, "".join(buf))
                    cur_table = None
                    buf = []

    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_orig_courses_provider ON "orig_courses"("cricos_provider_code");'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_orig_courses_cricos ON "orig_courses"("cricos_course_code");'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_orig_prov_cricos ON "orig_provider_institution"("cricos_provider_code");'
    )

    init_enrichment_tables(conn)
    conn.commit()
    elapsed = time.time() - t0
    print(f"Loaded SQL dump into {db_path} in {elapsed:.2f}s:")
    for t, c in sorted(counts.items()):
        print(f"  orig_{t}: {c} rows")
    conn.close()


if __name__ == "__main__":
    load_sql_dump()
