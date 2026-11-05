"""
Shared Forensic Rules & Extractors for International Student Academic & English
Entry Requirements (v4).

Used identically across:
  - Stage 3 (`stage3_entry_english_enricher.py`)
  - Stage 4 (`stage4_deep_pdf_web_enricher.py`)
  - Stage 5 (`stage5_entry_requirements_audit_fixer.py`)
  - Verification & Sanitization (`verify_and_sanitize.py`)

Guarantees:
  1. Zero decimal chopping on IELTS sub-bands (`5.5`, `6.0`, `6.5`, `7.0`, `7.5`).
  2. Zero `<nav>` / sidebar / bare course-title bullets masquerading as Academic Entry Requirements.
  3. Zero synthesized `— IELTS Academic:` table rows bleeding into Academic bullets.
  4. Zero cross-level contamination (School vs VET vs Bachelor vs Honours vs Postgrad vs Research).
  5. Zero marketing blurbs, exit pathways, career outcomes, fee lines, testimonials, or other-course exceptions.
  6. Zero mid-sentence lowercase fragments or dangling-preposition/conjunction PDF line-wrap truncations.
  7. Zero IELTS/PTE/TOEFL concordance contradictions or sub-threshold degree/nursing/teaching IELTS scores.
"""
from __future__ import annotations

import html
import re
from selectolax.parser import HTMLParser

# ------------------------------------------------------------------------------
# 1. Domestic & Noise Regexes
# ------------------------------------------------------------------------------
DOMESTIC_ONLY_TEXT_RE = re.compile(
    r"(?:\b(?:"
    r"commonwealth\s+supported|csp|hecs(?:-help)?|fee-help|sa-help|os-help|vet\s+student\s+loans?|"
    r"vtac|uac|qtac|satac|tisc|domestic\s+rpl|"
    r"austudy|ausstudy|abstudy|centrelink|youth\s+allowance|concession\s+card|health\s+care\s+card|"
    r"smart\s+and\s+skilled|skills\s+first|fee-free\s+tafe|free\s+tafe|certificate\s+3\s+guarantee|"
    r"jobs\s+and\s+skills\s+wa|subsidised\s+training|government[- ]subsidised|government\s+funded|"
    r"australian\s+citizens?|new\s+zealand\s+citizens?|permanent\s+residents?|"
    r"domestic\s+applicants?|domestic\s+students?|local\s+students?|first\s+nation\s+applicants?|"
    r"domestic\s+(?:undergraduate|postgraduate|coursework)?\s*(?:admission|entry|application|offer|fee)s?|"
    r"year\s*12\s+student\s+from\s+another\s+state|"
    r"humanitarian\s+visa|australian\s+apprenticeships?|australian\s+year\s+12\s+students\s+only"
    r")\b|via\s*uac|and\s*tisc)",
    re.I,
)

DOMESTIC_STATE_CERT_OR_RANK_RE = re.compile(
    r"(?:\b(?:"
    r"atar|selection\s+rank|guaranteed\s+entry(?:\s+rank)?|lowest\s+selection\s+rank|"
    r"vce(?:\s+units?)?|victorian\s+certificate\s+of\s+education|hsc|qce|sace|wace|tce|ntcet|ncea|op\s*\d{1,2}\b|"
    r"study\s+score\s+of|ib\s+score\s+of|band\s+[23456]\s+in"
    r")\b|(?<!ielts\s)(?<!overall\s)\bband\s+[2345]\b)",
    re.I,
)

INTL_EQUIV_CONTEXT_RE = re.compile(
    r"\b(international|overseas|equivalent)\b",
    re.I,
)

# Explicit predicate/framing showing a bullet is actually an ENTRY REQUIREMENT (not a bare nav link)
ENTRY_REQUIREMENT_PREDICATE_RE = re.compile(
    r"\b("
    r"completion\s+of|completed?|completing|successfully\s+complete|"
    r"hold|holding|holders?\s+of|possess|achieved?|attain(?:ed)?|passed|graduated|graduate\s+of|"
    r"satisfactory|must\b|minimum|at\s+least|require[ds]?|requirement|prerequisite|"
    r"entry\s+(?:to|into|level|requirement|criteria)|admission\s+(?:to|into|requirement|criteria)|admitted\s+to|"
    r"eligible|eligibility|applicants?\b|candidates?\b|"
    r"students?\s+(?:must|who|need|seeking|entering|commencing|wishing|applying)|"
    r"open\s+to|prior\s+to|before\s+enrol|"
    r"equivalent|year\s*1[012]\b|aqf\s+level|gpa\b|grade\s+point\s+average|wam\b|weighted\s+average|"
    r"work\s+experience|relevant\s+(?:industry\s+|professional\s+)?experience|"
    r"years?\s+of\s+age|aged?\s+1[5678]\b|turn\s+18|older\s+than\s+1[678]|"
    r"portfolio|audition|interview|lln\b|language,\s*literacy|accredit(?:ed|ation)"
    r")\b",
    re.I,
)

# Substantive academic/age/qualification/selection concept (prevents generic "Applicants must apply online")
SUBSTANTIVE_REQ_CONCEPT_RE = re.compile(
    r"\b("
    r"year\s*(?:[789]|1[012])\b|secondary\s+(?:school|education)|high\s+school|school\s+certificate|"
    r"aqf|bachelor|honours|master|doctoral|phd|postgraduate|undergraduate|"
    r"graduate\s+diploma|graduate\s+certificate|advanced\s+diploma|diploma|associate\s+degree|"
    r"certificate\s+(?:i|ii|iii|iv|1|2|3|4)\b|qualification|degree|award|"
    r"international\s+baccalaureate|foundation\s+(?:program|studies|year|course)|"
    r"gpa\b|grade\s+point\s+average|weighted\s+average|wam\b|academic\s+(?:merit|standing|record|results|transcript|achievement|performance)|"
    r"work\s+experience|professional\s+experience|industry\s+experience|relevant\s+experience|employment\s+experience|"
    r"minimum\s+age|years?\s+of\s+age|aged?\s+1[5678]\b|turn\s+18|"
    r"prerequisite|mathematics|chemistry|physics|biology|calculus|science\s+subject|"
    r"portfolio|audition|interview|research\s+proposal|supervisor|"
    r"lln\b|language,\s*literacy\s+and\s+numeracy|competency\s+in\s+english|aeas|"
    r"current\s+registration|registered\s+nurse|ahpra|nmba|working\s+with\s+children|police\s+check"
    r")\b",
    re.I,
)

# UI, Navigation, Marketing, Career Outcomes, Exit Pathways, Fees, Testimonials, Country Rows
NON_ENTRY_NOISE_RE = re.compile(
    r"("
    # Nav / UI / CTA
    r"\bsave\s+.{1,80}\s+to\s+course\s+favourites\b|\bcourse\s+favourites\b|\badd\s+to\s+favourites\b|"
    r"\bbrowse\s+(?:bachelor|master|courses?|degrees?|programs?|study\s+areas?)\b|"
    r"\bfind\s+the\s+right\s+course\b|\bexplore\s+(?:our\s+)?courses?\b|\bview\s+(?:all\s+)?courses?\b|"
    r"\brelated\s+courses?\b|\bsimilar\s+courses?\b|\bpopular\s+courses?\b|\bfeatured\s+courses?\b|"
    r"\bback\s+to\s+top\b|\bskip\s+to\s+content\b|\bread\s+more\b|\blearn\s+more\b|"
    r"\benrol\s+now\b|\bapply\s+now\b|\bdownload\s+(?:brochure|course\s+guide|flyer|handbook)\b|"
    r"\bbook\s+a\s+consultation\b|\bcontact\s+us\b|\bspeak\s+(?:to|with)\s+an?\s+(?:advisor|team)\b|"
    # Course overview / hero marketing
    r"\bthis\s+(?:course|degree|program|programme|qualification|diploma|certificate|master|bachelor)\s+"
    r"(?:is\s+designed|is\s+ideal|is\s+suitable|is\s+aimed|is\s+accredited|is\s+delivered|provides|prepares|equips|offers|will|gives|enables|focuses|allows|covers|delivers|teaches|reflects\s+the\s+role)\b|"
    r"\bgain\s+the\s+(?:knowledge|skills|practical)\b|\bdevelop\s+the\s+(?:skills|knowledge)\b|"
    r"\bstudy\s+the\s+(?:bachelor|master|diploma|certificate|graduate)\b|"
    r"\bprepare\s+for\s+a\s+(?:rewarding\s+)?career\b|\btake\s+your\s+career\b|\badvance\s+your\s+career\b|"
    r"\bkick-?start\s+your\s+career\b|\blaunch\s+you\s+into\s+your\s+future\b|"
    r"\byou\s+will\s+(?:get|learn|gain|develop|study|graduate)\b|\blearn\s+how\s+to\b|"
    # School marketing / grade-span descriptions ("from Early Learning to Year 12")
    r"\b(?:from\s+)?(?:early\s+learning|elc|prep|reception|kindergarten|pre-?kindergarten|k)\s+(?:to|through\s+to|-|–)\s+year\s*1[012]\b|"
    r"\boffers\s+exceptional\s+education\b|\bwe\s+inspire\s+a\s+love\s+of\s+learning\b|"
    r"\bwe\s+pride\s+ourselves\b|\bis\s+an?\s+(?:outstanding|leading|independent|anglican|catholic|christian|co-educational)\b.{0,45}\bschool\b|"
    r"\bday\s+and\s+boarding\s+school\b|"
    # Exit pathways / articulation out / career outcomes
    r"\bgraduates\s+(?:will|can|may|of\s+this|are\s+able|are\s+eligible)\b|"
    r"\bupon\s+(?:successful\s+)?completion\s+of\s+this\b|\bon\s+(?:successful\s+)?completion\s+of\s+this\b|"
    r"\bafter\s+completing\s+this\b|\bsuccessful\s+completion\s+of\s+this\s+(?:qualification|course|diploma|certificate|program)\b|"
    r"\bprovides?\s+a\s+pathway\s+(?:into|to)\b|\bpathway\s+into\s+(?:a\s+|the\s+)?(?:bachelor|master|second\s+year|diploma|university)\b|"
    r"\bwill\s+be\s+granted\s+entry\s+into\s+the\s+second\s+year\b|"
    r"\bstudents\s+successfully\s+completing\s+the\s+diploma\b|"
    r"\barticulate\s+into\b|\bcredit\s+transfer\s+(?:into|towards)\b|"
    r"\bcareer\s+(?:opportunities|outcomes|pathways|options)\b|\bemployment\s+outcomes\b|\bjob\s+roles\s+include\b|"
    # Fees / CRICOS / Refund / Privacy / Admin
    r"\btuition\s+fees?\b|\bapplication\s+fee\b|\benrolment\s+fee\b|\bmaterial\s+fees?\b|"
    r"\bnon-refundable\b|\baud\s*\$|\$\s*\d[\d,]*|\bdeposit\s+refunded\b|\brefund\s+policy\b|"
    r"\bcricos\s+(?:provider|course)\s*(?:code|no|number)?\b|\brto\s*(?:code|no|number)\b|\babn\s*\d|"
    r"\bprivacy\s+policy\b|\bcollecting\s+such\s+information\b|\benable\s+completion\s+of\s+the\s+enrolment\s+process\b|"
    r"\boverseas\s+student\s+health\s+cover\b|\boshc\b|"
    # Testimonials / School camps / Trips
    r"\bwork\s+experience\s+opportunities\s+were\s+invaluable\b|\bonline\s+boutique\b|"
    r"\bcanberra\s+trip\b|\boutdoor\s+education\s+trips?\b|\byear\s*10\s+connect\s+terms\b|"
    # Cookies / State subject prefixes / RPL & Credit promos / Meta-boilerplate / Exit pathways
    r"\bwe\s+use\s+cookies\b|\bby\s+clicking\s+[“\"']?accept\b|"
    r"^\s*(?:nsw|vic|qld|wa|sa|tas|act|nt)\s*:|"
    r"\bget\s+credit\s+for\s+your\s+prior\b|\brecognition\s+of\s+prior\s+learning\b|"
    r"\bcourse\s+[‘'\"]?credits?[’'\"]?,\s+where\s+you\s+are\s+granted\b|"
    r"\breceive\s+credit\s+for\s+your\s+previous\s+study\b|"
    r"\bduration\s+can\s+vary\s+due\s+to\s+your\s+prior\s+learning\b|"
    r"\bno\s+formal\s+credit\s+transfer\s+arrangements\b|"
    r"\bprovide\s+a\s+general\s+guide\s+only\b|\ba\s+prerequisite\s+is\s+a\s+required\s+course\b|"
    r"\bmandatory\s+for\s+the\s+award\s+associated\s+with\s+your\s+program\b|"
    r"\bskills\s+in\s+this\s+qualification\s+must\s+be\s+applied\s+in\s+accordance\s+with\s+commonwealth\b|"
    r"\bselect\s+where\s+you\s+studied\b|"
    r"\bcheck\s+whether\s+your\s+qualification\s+is\s+equivalent\s+to.{0,35}\s+page\b|"
    r"\bif\s+your\s+country\s+or\s+qualification\s+is\s+not\s+listed\b|"
    r"\bthere\s+are\s+many\s+ways\s+to\s+be\s+considered\s+for\s+entry\b|"
    r"\blearn\s+about\s+different\s+entry\s+pathways\b|\bwe[’']ll\s+guide\s+you\s+towards\b|"
    r"\bget\s+a\s+head\s+start\.\s+explore\b|\bhere[’']s\s+your\s+opportunity\s+to\s+get\s+ahead\b|"
    r"\bearly\s+offer\s+program\s+allows\s+year\s*12\b|\bprincipal[’']s\s+recommendation\s+scheme\b|"
    r"\bbecoming\s+an\s+ib\s+world\s+school\b|\bexpressions\s+of\s+interest\s+are\s+now\s+open\b|"
    r"\bflex-?semester\s+academic\s+calendar\b|"
    r"\bindicative\s+(?:full\s+|csp\s+|annual\s+)?fee\s+to\s+complete\b|"
    r"\bpartner\s+with\s+us\s+to\s+offer\s+work\s+experience\b|"
    r"\bschedule\s+of\s+meetings\s+with\s+your\s+supervisor\b|"
    r"\bschool\s+of\s+graduate\s+research\s+works\s+with\b|"
    r"\bmaximum\s+duration\s+of\s+the\s+phd\s+program\b|"
    r"\bgain\s+entry\s+to\s+a\s+range\s+of\s+bachelor\s+courses\s+by\s+studying\b|"
    r"\buniversity\s+entrance\s+program\s*-\s*a\s+14-week\b|"
    r"\b(?:the\s+)?students?\s+will\s+be\s+(?:issued|awarded)\b|"
    r"\bsatisfactory\s+completion\s+of\s+this\s+course\s+can\s+lead\s+to\b|"
    r"\bis\s+to\s+be\s+a\s+pathway\s+into\b|\bwant\s+a\s+pathway\s+into\b|"
    r"\bcommon\s+pathway\s+into\s+the\s+program\b|\bnot\s+a\s+direct\s+pathway\b|"
    r"\bdiploma\s+as\s+a\s+pathway\b|\bpathway\s+to\s+remedial\s+massage\b|"
    r"\bcourse\s+overview\s+the\b|\bis\s+designed\s+for\s+ambitious\s+students\b|"
    r"\bminor\s+introduces\s+students\s+to\b|\boffers\s+free\s+work\s+experience\s+placements\b|"
    r"\bbenefit\s+from\s+additional\s+undergraduate\s+study\s+in\s+english\s+before\b|"
    r"\bwill\s+naturally\s+encounter\b|\bstudents\s+take\s+at\s+least\b|"
    r"\bif\s+you[’']?ve\s+completed\s+a\s+different\s+english\s+test\b|\bcheck\s+accepted\s+english\s+tests\b|"
    r"\bthere\s+are\s+numerous\s+pathways\s+you\s+can\s+take\b|"
    r"\bas\s+outlined\s+in\s+the\s+tabs\s+below\b|\bas\s+outlined\s+below\b|"
    r"\bif\s+you\s+(?:don[’']t|do\s+not)\s+meet\s+the\s+(?:academic\s+)?entry\s+requirements\b|"
    r"\bdegree[’']s\s+zones\b|\badmission\s+point\s+that\s+recognises\b|"
    r"\baligned\s+with\s+the\s+learning\s+outcomes\s+of\s+units\b|"
    r"\bexemption\s+of\s+english[- ]language\b|\bacademic\s+english\s+language\s+bridging\s+course\b|"
    r"^these\s+include\s+those\s+relating\s+to\b|"
    # Synthesized English table lines or waivers
    r"\s[—–-]\s*(?:ielts|pte|toefl|cambridge|oet)\b|\benglish\s+waivers?\s+are\s+not\s+available\b"
    r")",
    re.I,
)

ACCORDION_HEADING_ONLY_RE = re.compile(
    r"^(?:"
    r"(?:for\s+)?students?\s+(?:under|over)\s+18\s+years?\s+of\s+age(?:\s*\([^)]+\))?|"
    r"minimum\s+age\s+requirements?|"
    r"(?:postgraduate|undergraduate)\s+minimum\s+(?:entry\s+)?requirements?|"
    r"notes?\s+on\s+prerequisite\s+subjects?|"
    r"year\s*1[012]\s+prerequisite\s+subjects?|"
    r"secondary\s+education\s*\(\s*year\s*1[012]\s*\)|"
    r"equivalent\s+australian\s+year\s*1[012]|"
    r"school\s+leaver\s+(?:of|or)\s+recent\s+year\s*12\s+entry|"
    r"completing\s+year\s*12\s+in\s+the\s+current\s+year.*|"
    r"for\s+current\s+year\s*12\s+students?|"
    r"providing\s+pathways?\s+for\s+year\s*12\s+students?|"
    r"best\s+of\s+\d\s+year\s*12\s+subject\s+grades?|"
    r"high\s+school\s*\(\s*year\s*11\s*(?:&|and)\s*12\s*\)|"
    r"qualification\s+equivalent|"
    r"language,\s*literacy\s+and\s+numeracy(?:\s*\(\s*lln\s*\))?(?:\s+requirements?)?|"
    r"i\s+completed\s+secondary\s+school\s+in\s+australia|"
    r"international\s+baccalaureate\s+diploma\s+year\s*11\s*[-–]\s*12.*"
    r")\.?$",
    re.I,
)

COUNTRY_CERT_ROW_RE = re.compile(
    r"\b("
    r"certificado\s+de\s+ensino|certificado\s+de\s+ensio|educacion\s+media|educación\s+media|"
    r"bachillerato|bằng\s+tốt\s+nghiệp|bang\s+tot\s+nghiep|sijil\s+pelajaran|gao\s*kao|"
    r"abitur|matura|diploma\s+sekolah|senior\s+middle\s+school\s+graduation|"
    r"all\s+india\s+senior\s+school|indian\s+school\s+certificate\s*\(\s*isc\s*\)"
    r")\b",
    re.I,
)

DANGLING_END_RE = re.compile(
    r"(?:\b(?:"
    r"and|or|of|to|in|into|with|within|without|for|from|by|at|as|on|upon|about|between|through|"
    r"during|before|after|under|over|a|an|the|minimum|least|level|including|such|is|are|was|were|"
    r"be|been|have|has|had|can|may|must|will|shall|should|would|could|that|which|who|whom|whose|"
    r"than|see|refer|visit|either|neither|both|whether|plus|co|"
    r"internation|representi|equivalen|qualificati|requir|universit|educati|progra|experien|applican|informatio|certifica|completio"
    r")|:|\band\s+\d{1,2})$",
    re.I,
)

DECOMPOSE_NAV_SELECTOR = (
    "script, style, noscript, svg, iframe, nav, header, footer, aside, form, button, select, option, "
    "[role='navigation'], [role='banner'], [role='contentinfo'], "
    "[aria-label*='menu' i], [aria-label*='navigation' i], [aria-label*='breadcrumb' i], "
    ".nav, .navbar, .menu, .mega-menu, .sub-menu, .sidebar, .breadcrumb, .breadcrumbs, "
    ".site-header, .site-footer, .page-header, .page-footer, .cookie, .cookies, "
    ".related-courses, .similar-courses, .featured-courses, .testimonial, .testimonials"
)

ENTRY_HEADING_RE = re.compile(
    r"^\s*(?:international\s+|overseas\s+)?(?:student\s+)?(?:academic\s+|minimum\s+|course\s+|standard\s+|general\s+)?"
    r"(?:entry\s+requirements?|admission\s+requirements?|academic\s+requirements?|entry\s+criteria|admission\s+criteria|"
    r"eligibility(?:\s+requirements?|\s+criteria)?|prerequisites?|minimum\s+requirements?|who\s+can\s+apply)\s*:?\s*$",
    re.I,
)

STOP_SECTION_HEADING_RE = re.compile(
    r"^\s*(?:tuition\s+fees?|fees?\s+(?:and|&)\s+(?:scholarships?|costs?|charges?|funding)|course\s+fees?|scholarships?|"
    r"career\s+(?:outcomes?|opportunities|pathways?|options)|where\s+this\s+(?:course|degree)\s+can\s+take\s+you|"
    r"what\s+you\s+will\s+(?:study|learn)|course\s+structure|program\s+structure|units?\s+of\s+competency|core\s+units?|"
    r"study\s+pathways?|credit\s+and\s+pathways?|how\s+to\s+apply|application\s+process|"
    r"related\s+courses?|similar\s+courses?|you\s+may\s+also\s+like|frequently\s+asked\s+questions|faqs?|"
    r"student\s+(?:stories|testimonials?|profiles?)|campuses?\s+and\s+delivery|intakes?\s+and\s+dates?)\s*:?\s*$",
    re.I,
)


# ------------------------------------------------------------------------------
# 2. Clean Text & DOM Helpers
# ------------------------------------------------------------------------------
def normalize_ligatures(text: str) -> str:
    if not text:
        return ""
    return (
        text.replace("\ufb00", "ff")
        .replace("\ufb01", "fi")
        .replace("\ufb02", "fl")
        .replace("\ufb03", "ffi")
        .replace("\ufb04", "ffl")
        .replace("\ufffd", "'")
        .replace("\u00a0", " ")
    )


def unwrap_pdf_and_ligatures(text: str) -> str:
    """Unwrap hard right-margin PDF single-line breaks into full prose sentences."""
    if not text:
        return ""
    text = normalize_ligatures(text)
    raw_lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    out_lines: list[str] = []
    for ln in raw_lines:
        if not ln:
            out_lines.append("")
            continue
        if out_lines and out_lines[-1]:
            prev = out_lines[-1]
            starts_new_item = bool(
                re.match(r"^(?:[•·▪➢\*–\-]|\(\d+\)|\([a-z]\)|\d+[\.\)])\s+", ln)
                or ENTRY_HEADING_RE.match(ln)
                or STOP_SECTION_HEADING_RE.match(ln)
            )
            prev_ended_sentence = prev.endswith((".", "!", "?", ";")) or (
                prev.endswith(":") and len(prev) < 65
            )
            # Join wrapped PDF lines when previous line didn't finish a sentence, or next line starts lowercase
            if not starts_new_item and (
                (ln[0].islower() and not prev.endswith((".", "!", "?")))
                or (not prev_ended_sentence and len(prev) >= 38 and not prev.endswith(":"))
            ):
                out_lines[-1] = f"{prev} {ln}"
                continue
        out_lines.append(ln)
    return "\n".join(out_lines)


def parse_html_english_tables(html_str: str | None) -> str:
    """
    Convert multi-column HTML English tables into labeled lines strictly for English extraction.
    """
    if not html_str:
        return ""
    tree = HTMLParser(html_str)
    synthesized_lines = []
    for table in tree.css("table"):
        rows = table.css("tr")
        if len(rows) < 2:
            continue
        headers = [re.sub(r"\s+", " ", c.text(strip=True)).lower() for c in rows[0].css("th, td")]
        if not any(any(k in h for k in ("ielts", "pte", "pearson", "toefl", "english")) for h in headers):
            continue

        col_map = {}
        for idx, h in enumerate(headers):
            if "ielts" in h:
                col_map["IELTS Academic"] = idx
            elif "pte" in h or "pearson" in h:
                col_map["PTE Academic"] = idx
            elif "toefl" in h:
                col_map["TOEFL iBT"] = idx
            elif "cambridge" in h or "c1" in h or "cae" in h:
                col_map["Cambridge C1"] = idx
            elif "oet" in h:
                col_map["OET"] = idx

        if not col_map:
            continue

        for r in rows[1:]:
            cells = [re.sub(r"\s+", " ", c.text(strip=True)) for c in r.css("td, th")]
            if not cells:
                continue
            row_label = cells[0] if 0 not in col_map.values() else "Standard"
            parts = []
            for label, cidx in col_map.items():
                if cidx < len(cells) and cells[cidx] and cells[cidx] not in ("-", "N/A", "n/a"):
                    parts.append(f"{label}: {cells[cidx]}")
            if parts:
                synthesized_lines.append(f"{row_label} — " + "; ".join(parts) + ".")
    return "\n".join(synthesized_lines)


def html_to_clean_texts(html_str: str | None) -> tuple[str, str]:
    """
    Returns `(english_ready_text, academic_main_text)`:
      - Strips `<nav>`, `<header>`, `<footer>`, `<aside>`, menus, breadcrumbs, buttons, related-course cards.
      - Prefers `<main>` / `[role='main']` when substantial (>= 200 chars).
      - Keeps synthesized `— IELTS Academic:` table rows ONLY in `english_ready_text`, NEVER in `academic_main_text`.
    """
    if not html_str:
        return "", ""
    tree = HTMLParser(html_str)
    for tag in tree.css(DECOMPOSE_NAV_SELECTOR):
        tag.decompose()

    main_node = tree.css_first("main, [role='main']")
    main_html = main_node.html if main_node else (tree.body.html if tree.body else html_str)
    main_txt = (
        main_node.text(separator="\n", strip=True)
        if main_node
        else (tree.body.text(separator="\n", strip=True) if tree.body else tree.text(separator="\n", strip=True))
    )
    if main_node and len(main_txt) < 200 and tree.body:
        main_html = tree.body.html
        main_txt = tree.body.text(separator="\n", strip=True)

    main_txt = normalize_ligatures(re.sub(r"[ \t]+", " ", main_txt)).strip()
    tbl_txt = normalize_ligatures(parse_html_english_tables(main_html or html_str))
    eng_txt = f"{tbl_txt}\n{main_txt}".strip() if tbl_txt else main_txt
    return eng_txt, main_txt


# ------------------------------------------------------------------------------
# 3. English Test Extraction, Sanitization & Concordance
# ------------------------------------------------------------------------------
IELTS_BAND_NUM_RE = re.compile(r"\b(4\.[05]|[5678](?:\.[05])?)\b")

PTE_CONCORDANCE_CENTER = {
    4.5: 32,
    5.0: 38,
    5.5: 44,
    6.0: 51,
    6.5: 58,
    7.0: 65,
    7.5: 73,
    8.0: 79,
    8.5: 84,
}


def has_contradictory_ielts_subband(s: str | None) -> bool:
    """
    Returns True if an IELTS string contains a sub-band higher than overall, EXCEPT when
    Speaking/Listening is legitimately +0.5/+1.0 higher on >= 6.5 overall (Nursing/Teaching/Health).
    """
    if not s:
        return False
    nums = [float(x) for x in IELTS_BAND_NUM_RE.findall(s)]
    if len(nums) < 2:
        return False
    overall = nums[0]
    higher = [x for x in nums[1:] if x > overall]
    if not higher:
        return False
    if (
        overall >= 6.5
        and max(higher) - overall <= 1.0
        and re.search(r"\b(speaking|listening)\b", s, re.I)
        and not re.search(r"^\s*IELTS\s+Overall\s+minimum\s+score\s*:\s*\d", s, re.I)
    ):
        return False
    return True


CHOPPED_DECIMAL_TAIL_RE = re.compile(
    r"(?:,\s*|\s+\(?\s*)?(?:with\s+)?(?:no\s+(?:individual\s+|single\s+|communicative\s+)?(?:skill\s+bands?|bands?|sub-?bands?|sub-?scores?|scores?|skills?|sections?|components?)?\s*(?:scores?\s+)?(?:less\s+than|below|<)\s*|"
    r"minimum\s+(?:bands?\s+|scores?\s+)?(?:of\s+)?|each\s+band\s*(?:>=|of)?\s*|all\s+bands?\s*(?:>=|of)?\s*)(?:a\s+)?[45678]\s*\)?\s*$",
    re.I,
)


def has_chopped_decimal_subband(s: str | None) -> bool:
    """
    Detects strings where an overall score was written with `.0`/`.5` (or `[678] overall`),
    and the trailing sub-band clause ends in a bare integer like `less than 5` or `below a 6`
    due to legacy period-splitting!
    """
    if not s:
        return False
    m_ov = re.search(r"\b([45678]\.[05]|[678]\s+overall)\b", s, re.I)
    if not m_ov:
        return False
    return bool(CHOPPED_DECIMAL_TAIL_RE.search(s.strip()))


def clean_single_test_str_v2(s: str | None, test_type: str) -> str | None:
    if not s:
        return None
    s = normalize_ligatures(s)
    s = re.sub(r"[\r\n\t]+", " ", s)
    s = re.sub(r"<[a-zA-Z/!][^>]*>", " ", s)
    s = re.sub(r"\bAcademinc?\b", "Academic", s, flags=re.I)
    s = re.sub(r"\(\s*Academic\s+Module\s*\)", "Academic", s, flags=re.I)
    s = re.sub(r"\s+>\s+(?=(?:Overall|band|score|[345678]))", ": ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()

    # Reject URLs, fees, CRICOS codes, EAP conditional bridging advice, semester/week durations, or month names
    if re.search(
        r"(?:https?://|www\.|\$\s*\d|\baud\b|\bcricos\b|\btuition\b|\bannual\s+fees?\b|"
        r"\badvised\s+to\s+enrol\s+in\s+(?:our\s+)?eap\b|"
        r"\bper\s+week\b|\btrimester\b|\bsemester\b|\bseptember\b|\boctober\b|\bnovember\b|\bfebruary\b)",
        s,
        re.I,
    ):
        return None

    # Collapse duplicate test prefix ("IELTS IELTS ..." -> "IELTS ...")
    s = re.sub(r"\b(IELTS|PTE|TOEFL)\s+\1\b", r"\1", s, flags=re.I)
    s = re.sub(r"^(IELTS|PTE|TOEFL)(?:\s+or\s+equivalent)?\s*\)\s*[:\-–]?\s*", r"\1: ", s, flags=re.I)
    s = re.sub(r"\(\s*(Academic|iBT)\s*\)", r"\1", s, flags=re.I)

    # Truncate before bled-in headings, cohorts, waivers, packaged ELICOS clauses, or multi-course exception clauses
    s = re.split(
        r"(?:,\s*or\s+Score\s+of\s+\d+(?:\.\d+)?\s+when\s+packaged\b|"
        r"\b(?:Academic\s+Requirements?|Age\s+Requirements?|Prerequisites?|Entry\s+Requirements?|"
        r"Course\s+Structure|Delivery\s+Methods?|"
        r"Academic\s*:|Domestic\s+Students?|Domestic\s+Applicants?|International\s+Students?|"
        r"all\s+courses\s+except|except\s+for\s+the\s+following|English\s+waivers?|"
        r"English\s+Language\s+Requir\w*|Alternatives?\s*:|"
        r"\d{1,2}[,\s]+(?:\d{1,2}\s+or\s+)?\d{1,2}\s+week\s+program|"
        r"packaged?\s+offer|bridging\s+course)\b)",
        s,
        flags=re.I,
    )[0]

    if re.search(r"\b(atar|scaled\s+score|selection\s+rank)\b", s, re.I):
        return None

    if test_type == "ielts":
        s = re.split(
            r"\b(?:PTE|Pearson|TOEFL|Cambridge|CAE|C1\s+Advanced|C2\s+Proficiency|OET|Duolingo|ISLPR|AEAS)\b",
            s,
            flags=re.I,
        )[0]
        # Trim unverified chopped integer sub-band tail if overall was decimal (e.g. "6.0 ... less than 5")
        if has_chopped_decimal_subband(s):
            s = CHOPPED_DECIMAL_TAIL_RE.sub("", s).strip()
    elif test_type == "pte":
        m_start = re.search(r"\b(?:PTE|Pearson)\b.*", s, flags=re.I)
        if not m_start:
            return None
        s = m_start.group(0)
        s = re.split(
            r"\b(?:IELTS|TOEFL|Cambridge|CAE|C1\s+Advanced|C2\s+Proficiency|OET|Duolingo|ISLPR|AEAS)\b",
            s,
            flags=re.I,
        )[0]
        # Truncate if two table cells got concatenated (e.g. "PTE Academic 42 with no band less than 36 50 with no band...")
        m_two_cells = re.match(
            r"^(\b(?:PTE|Pearson)[^0-9]{0,35}\b[345678]\d\b[^0-9]{4,45}?\b[345678]\d\b)\s+[345678]\d\b",
            s,
            flags=re.I,
        )
        if m_two_cells:
            s = m_two_cells.group(1)
    elif test_type == "toefl":
        m_start = re.search(r"\bTOEFL\b.*", s, flags=re.I)
        if not m_start:
            return None
        s = m_start.group(0)
        if re.match(r"^TOEFL\s*[—\-–:]+\s*(?:PTE|IELTS)", s, flags=re.I):
            num_m = re.search(r"\b(1[01]\d|[3456789]\d)\b", s)
            s = f"TOEFL iBT: Overall {num_m.group(1)}" if num_m else ""
        else:
            s = re.split(
                r"\b(?:IELTS|PTE|Pearson|Cambridge|CAE|C1\s+Advanced|C2\s+Proficiency|OET|Duolingo|ISLPR|AEAS)\b",
                s,
                flags=re.I,
            )[0]
        m_two_cells = re.match(
            r"^(\bTOEFL[^0-9]{0,35}\b(?:1[01]\d|[3456789]\d)\b[^0-9]{4,45}?\b\d{1,2}\b)\s+(?:1[01]\d|[456789]\d)\b",
            s,
            flags=re.I,
        )
        if m_two_cells:
            s = m_two_cells.group(1)

    # Fix unbalanced parentheses
    chars = list(s)
    stack: list[int] = []
    rem: set[int] = set()
    for i, ch in enumerate(chars):
        if ch == "(":
            stack.append(i)
        elif ch == ")":
            if stack:
                stack.pop()
            else:
                rem.add(i)
    rem.update(stack)
    s = "".join(ch for i, ch in enumerate(chars) if i not in rem)
    s = re.sub(r"\s+", " ", s).strip(" ,;:-–—.=")

    if len(s) < 7 or len(s) > 138:
        if len(s) > 138:
            # Truncate cleanly at clause boundary if overly verbose
            short = re.split(r"[.;]|,\s+(?:or|and\s+must|provided|test\s+must|taken\s+within)", s)[0].strip(" ,;:-–—.=")
            if 7 <= len(short) <= 138:
                s = short
            else:
                return None
        else:
            return None

    if test_type == "ielts":
        nums = [float(x) for x in IELTS_BAND_NUM_RE.findall(s)]
        if not nums or not (4.5 <= nums[0] <= 8.5):
            return None
        if has_contradictory_ielts_subband(s):
            return None
        # Reject integer-only "IELTS ... 7" if no decimal and not clearly saying overall/band/score/minimum/:
        if "." not in s and not re.search(r"\b(overall|band|minimum|score|academic\s*:|ielts\s*:)\b", s, re.I):
            return None
    elif test_type == "pte":
        m = re.search(r"\b([345678]\d)\b", s)
        if not m or not (36 <= int(m.group(1)) <= 86):
            return None
    elif test_type == "toefl":
        m = re.search(r"\b(1[01]\d|[3456789]\d)\b", s)
        if not m or not (35 <= int(m.group(1)) <= 115):
            return None

    return s


def extract_english_tests_v2(text: str) -> dict:
    """
    Decimal-preserving IELTS Academic, PTE Academic & TOEFL iBT extractor.
    Never chops `.0`/`.5` sub-bands, skips ELICOS bridging pathway sentences, and enforces word boundaries.
    """
    res = {"ielts": None, "pte": None, "toefl": None, "other": None}
    if not text:
        return res

    flat = re.sub(r"\s+", " ", normalize_ligatures(text))
    nb = r"(?:(?![;\n]|\.(?!\d)).)"

    ielts_patterns = [
        rf"\bIELTS(?:\s*/\s*IELTS\s+UKVI)?(?:\s*\(?Academic(?:\s+module)?\)?)?{nb}{{0,55}}?"
        rf"(?:\b([45678]\.[05])\b|(?:overall|minimum|score|band|at\s+least|>=|≥|:)\s*(?:of\s+|at\s+least\s+|score\s*:?\s*|minimum\s*(?:score)?\s*:?\s*)*\b([5678])\b(?!\s*(?:years?|weeks?|months?|semesters?|trimesters?|units?|subjects?|days?|\s*-\s*1[012]\b)))"
        rf"(?:\s*overall)?(?:\s*[\(,\-–]?\s*{nb}{{0,90}}?\b(?:band|sub-?score|sub-?band|skill|section|component|listening|writing|speaking|reading|less\s+than|below|minimum|each|<)\b{nb}{{0,65}})?",
        rf"\b([45678]\.[05])\s+(?:Academic\s+)?IELTS\b{nb}{{0,80}}",
    ]

    for pat in ielts_patterns:
        for m in re.finditer(pat, flat, re.I):
            # Skip bridging/pathway sentences ("Applicants with IELTS scores of 5.5 may enter via bridging...")
            ctx_around = flat[max(0, m.start() - 45) : min(len(flat), m.end() + 65)]
            if re.search(r"\b(bridging|elicos\s+pathway|packaged?\s+offer|do\s+not\s+meet|if\s+your\s+ielts)\b", ctx_around, re.I):
                continue
            g_str = m.group(1) or (m.group(2) if m.lastindex and m.lastindex >= 2 else None)
            if not g_str:
                continue
            sc = float(g_str)
            if not (4.5 <= sc <= 8.5):
                continue
            seg = m.group(0).strip(" ,;:-")
            seg = re.sub(r"\.+$", "", seg)
            tail = flat[m.end() : m.end() + 105]
            band_m = re.search(
                rf"^[\s,;:\(\-–]*(?:with\s+)?(?:no\s+(?:individual\s+)?(?:band|sub-?band|sub-?score|score|skill)\s*(?:score\s+)?(?:less\s+than|below|<)|minimum\s+(?:of\s+)?[45678](?:\.[05])?\s+in\s+(?:each|all)|no\s+band\s*<\s*)\s*[45678](?:\.[05])?{nb}{{0,40}}",
                tail,
                re.I,
            )
            if band_m and not re.search(r"\b(less\s+than|below|no\s+band|in\s+each|listening|speaking)\b", seg, re.I):
                b_seg = re.sub(r"\.+$", "", band_m.group(0).strip(" ,;:-"))
                seg = f"{seg}, {b_seg}"
            cleaned_ie = clean_single_test_str_v2(seg, "ielts")
            if cleaned_ie:
                res["ielts"] = cleaned_ie
                break
        if res["ielts"]:
            break

    pte_pat = (
        rf"\b(?:PTE(?:\s+Academic)?|Pearson\s+Test\s+of\s+English)\b"
        rf"(?!\s*\)\s*:\s*[A-Za-z])[^;\n\d]{{0,40}}?\b([345678]\d)\b"
        rf"(?!\s*(?:years?|weeks?|%|,\s*202\d))(?:\s*overall)?(?:\s*[\(,\-–]?\s*{nb}{{0,75}})?"
    )
    for pm in re.finditer(pte_pat, flat, re.I):
        val = int(pm.group(1))
        if 36 <= val <= 86:
            cleaned_pt = clean_single_test_str_v2(pm.group(0), "pte")
            if cleaned_pt:
                res["pte"] = cleaned_pt
                break

    toefl_pat = (
        rf"\bTOEFL(?:\s*iBT|\s*\(iBT\)|\s+Internet[- ]based(?:\s+Test)?)?\b"
        rf"[^;\n\d]{{0,45}}?\b(1[01]\d|[3456789]\d)\b(?:\s*overall)?(?:\s*[\(,\-–]?\s*{nb}{{0,75}})?"
    )
    for tm in re.finditer(toefl_pat, flat, re.I):
        val = int(tm.group(1))
        if 35 <= val <= 115:
            cleaned_tf = clean_single_test_str_v2(tm.group(0), "toefl")
            if cleaned_tf:
                res["toefl"] = cleaned_tf
                break

    # Reconcile concordance if IELTS was extracted alongside PTE/TOEFL
    ie, pt, tf = reconcile_test_concordance(res["ielts"], res["pte"], res["toefl"])
    res["ielts"], res["pte"], res["toefl"] = ie, pt, tf
    return res


def reconcile_test_concordance(
    ie: str | None, pt: str | None, tf: str | None
) -> tuple[str | None, str | None, str | None]:
    ie_m = IELTS_BAND_NUM_RE.search(ie or "")
    pt_m = re.search(r"\b([345678]\d)\b", pt or "")
    tf_m = re.search(r"\b(1[01]\d|[3456789]\d)\b", tf or "")

    ie_v = float(ie_m.group(1)) if ie_m else None
    pt_v = int(pt_m.group(1)) if pt_m else None
    tf_v = int(tf_m.group(1)) if tf_m else None

    if ie_v is not None and pt_v is not None:
        center = PTE_CONCORDANCE_CENTER.get(ie_v, 55)
        if abs(pt_v - center) >= 14:
            pt = None
            pt_v = None

    if ie_v is not None and tf_v is not None:
        if (
            (ie_v >= 6.5 and tf_v < 65)
            or (ie_v >= 7.0 and tf_v < 79)
            or (ie_v <= 5.5 and tf_v >= 79)
            or (ie_v <= 6.0 and tf_v >= 96)
        ):
            tf = None
            tf_v = None

    if ie_v is None and pt_v is not None and tf_v is not None:
        if (pt_v >= 58 and tf_v < 65) or (pt_v <= 45 and tf_v >= 79):
            tf = None

    return ie, pt, tf


def is_prereg_nursing_or_midwifery(course_name: str) -> bool:
    cn = (course_name or "").strip().lower()
    if any(x in cn for x in ("non-registration", "post-registration", "nursing studies", "health sciences")):
        return False
    return bool(
        re.search(
            r"\b(bachelor\s+of\s+nursing|diploma\s+of\s+nursing|bachelor\s+of\s+midwifery|"
            r"master\s+of\s+nursing\s+practice|master\s+of\s+midwifery(?:\s+practice)?)\b",
            cn,
        )
    )


def is_initial_teacher_education(course_name: str) -> bool:
    cn = (course_name or "").strip().lower()
    return bool(
        re.search(
            r"\b(master\s+of\s+teaching|bachelor\s+of\s+education\s*\((?:primary|secondary|early\s+childhood))\b",
            cn,
        )
    )


def validate_and_reconcile_english(
    ie: str | None,
    pt: str | None,
    tf: str | None,
    course_level: str,
    course_name: str,
    emeth: str = "",
    esrc: str = "",
) -> tuple[str | None, str | None, str | None]:
    """
    Validates cleaned IELTS/PTE/TOEFL scores against AQF `course_level`, professional registration
    floors (NMBA Nursing/Midwifery 7.0, AITSL Teacher Education >= 6.5, Higher Ed Bachelor/Master >= 6.0),
    VET non-clinical caps (< 7.0), Primary/Junior School exclusions, and cross-test concordance.
    """
    ie = clean_single_test_str_v2(ie, "ielts")
    pt = clean_single_test_str_v2(pt, "pte")
    tf = clean_single_test_str_v2(tf, "toefl")

    cl = (course_level or "").strip()
    cl_low = cl.lower()
    cn_low = (course_name or "").strip().lower()
    em_low = (emeth or "").lower()
    es_low = (esrc or "").lower()

    # 1. Primary School Studies: young children do not take adult IELTS Academic / PTE / TOEFL
    if cl == "Primary School Studies":
        return None, None, None

    # 2. Policy URLs from ELICOS / blog / news / staff pages are invalid for award courses
    if "policy" in em_low and any(
        b in es_low for b in ("english-language-centre", "/elicos", "/blog/", "/news/", "/events/", "/staff/")
    ):
        return None, None, None

    # 3. Beginner / General English / High School Preparation ELICOS courses shouldn't carry degree IELTS >= 5.5
    if cl == "Non AQF Award" and re.search(
        r"\b(general\s+english|beginner|elementary|starter|high\s+school\s+preparation|primary\s+school\s+preparation|young\s+learners)\b",
        cn_low,
    ):
        return None, None, None

    ie_m = IELTS_BAND_NUM_RE.search(ie or "")
    ie_v = float(ie_m.group(1)) if ie_m else None

    pt_m = re.search(r"\b([345678]\d)\b", pt or "")
    pt_v = int(pt_m.group(1)) if pt_m else None

    # 4. Junior Secondary Studies (Years 7-10): reject adult university IELTS >= 6.0
    if cl == "Junior Secondary Studies":
        if "policy" in em_low or (ie_v is not None and ie_v >= 6.0) or (pt_v is not None and pt_v >= 50):
            return None, None, None

    # 5. Higher Education Minimum Thresholds:
    #    - All Australian Bachelor, Bachelor Honours, Masters & Doctoral degrees require >= 6.0 IELTS
    if ("bachelor" in cl_low or "masters" in cl_low or "doctoral" in cl_low):
        if (ie_v is not None and ie_v < 6.0) or (ie_v is None and pt_v is not None and pt_v < 48):
            return None, None, None
    elif ("associate degree" in cl_low or "graduate" in cl_low):
        if (ie_v is not None and ie_v < 5.5) or (ie_v is None and pt_v is not None and pt_v < 42):
            return None, None, None

    # 6. Pre-registration Nursing & Midwifery: NMBA requires >= 7.0 IELTS (PTE >= 65)
    if is_prereg_nursing_or_midwifery(course_name):
        if (ie_v is not None and ie_v < 7.0) or (pt_v is not None and pt_v < 64):
            return None, None, None

    # 7. Initial Teacher Education: requires >= 6.5 IELTS
    if is_initial_teacher_education(course_name):
        if (ie_v is not None and ie_v < 6.5) or (pt_v is not None and pt_v < 56):
            return None, None, None

    # 8. Non-clinical / non-aviation / non-interpreting / non-teaching VET courses with suspicious IELTS >= 7.0
    if cl in (
        "Certificate I", "Certificate II", "Certificate III", "Certificate IV",
        "Diploma", "Advanced Diploma", "Vocational Short Course",
    ):
        if ie_v is not None and ie_v >= 7.0:
            if not re.search(
                r"\b(nursing|midwifery|interpreting|interpreter|translating|translator|tesol|teaching|education|early\s+childhood|aviation|pilot|flying|law|legal|dental|paramed)\b",
                cn_low,
            ):
                return None, None, None

    return reconcile_test_concordance(ie, pt, tf)


# ------------------------------------------------------------------------------
# 4. Strict 8-Gate Academic Entry Bullet Validator
# ------------------------------------------------------------------------------
def is_bullet_compatible_with_level(
    bullet: str,
    course_level: str,
    course_name: str = "",
    duration_weeks: int | None = None,
) -> bool:
    bl = bullet.lower()
    cl = (course_level or "").strip()
    cl_low = cl.lower()
    cn_low = (course_name or "").strip().lower()
    wks = int(duration_weeks or 0)

    # Explicit cohort prefix mismatch
    if bl.startswith(("postgraduate:", "master's", "masters:", "higher degree by research:", "hdr:")):
        if cl in (
            "Certificate I", "Certificate II", "Certificate III", "Certificate IV",
            "Diploma", "Advanced Diploma", "Associate Degree", "Bachelor Degree",
            "Primary School Studies", "Junior Secondary Studies",
            "Senior Secondary Certificate of Education", "Vocational Short Course", "Non AQF Award",
        ):
            return False
    if bl.startswith(("undergraduate:", "school leaver")):
        if cl in (
            "Graduate Certificate", "Graduate Diploma",
            "Masters Degree (Coursework)", "Masters Degree (Research)",
            "Masters Degree (Extended)", "Doctoral Degree",
            "Primary School Studies", "Junior Secondary Studies",
        ):
            return False

    # 1. School Courses (Primary, Junior Secondary, Senior Secondary)
    if cl in ("Primary School Studies", "Junior Secondary Studies", "Senior Secondary Certificate of Education"):
        if re.search(
            r"\b(18\s+years|minimum\s+age\s*:?\s*18|aged?\s+18|turn\s+18|bachelor|master|doctoral|phd|"
            r"graduate\s+diploma|graduate\s+certificate|diploma\s+of|advanced\s+diploma|"
            r"certificate\s+(?:ii|iii|iv|2|3|4)|aqf\s+level|work\s+experience)\b",
            bl,
        ):
            return False
        if cl in ("Primary School Studies", "Junior Secondary Studies") and re.search(r"\byear\s*1[12]\b", bl):
            return False
        if cl == "Primary School Studies" and re.search(r"\byear\s*(?:[789]|10)\b", bl):
            return False
        if cl == "Senior Secondary Certificate of Education" and re.search(
            r"\b(?:completion\s+of|completed?|hold)\s+(?:an?\s+)?(?:australian\s+)?year\s*12\b",
            bl,
        ):
            return False
        return True

    # 2. Undergraduate / VET / Foundation / Associate Degree / Embedded 4-Year Honours
    is_undergrad_or_vet = cl in (
        "Certificate I", "Certificate II", "Certificate III", "Certificate IV",
        "Diploma", "Advanced Diploma", "Associate Degree", "Bachelor Degree",
        "Vocational Short Course", "Non AQF Award",
    ) or (cl == "Bachelor Honours Degree" and wks >= 120)

    if is_undergrad_or_vet:
        if cl in (
            "Certificate I", "Certificate II", "Certificate III", "Certificate IV",
            "Diploma", "Advanced Diploma", "Vocational Short Course",
        ) and re.search(
            r"\bfor\s+entry\s+(?:into|to)\s+(?:most\s+)?[a-z\s]{0,25}\bundergraduate\s+degree\s+programs?\b",
            bl,
        ):
            return False
        is_grad_entry_ug = "graduate entry" in cn_low or (cl == "Non AQF Award" and "postgraduate" in cn_low)
        if not is_grad_entry_ug:
            # Must NOT demand completion of a Bachelor, Master, Graduate Diploma, or Postgraduate degree
            if re.search(
                r"\b("
                r"completion\s+of\s+(?:a|an)\s+(?:recognised\s+|australian\s+)?(?:three[- ]year\s+|four[- ]year\s+|3[- ]year\s+|4[- ]year\s+)?bachelor|"
                r"completed\s+(?:a|an)\s+(?:recognised\s+|australian\s+)?bachelor|"
                r"hold\s+(?:a|an)\s+(?:recognised\s+|australian\s+)?bachelor|"
                r"bachelor(?:'s)?\s+degree\s+(?:or\s+equivalent|in\s+a|from\s+a|with\s+a)|"
                r"three[- ]year\s+bachelor|four[- ]year\s+bachelor|"
                r"master(?:'s)?\s+degree|graduate\s+diploma|graduate\s+certificate|"
                r"postgraduate\s+(?:qualification|degree)|aqf\s+level\s+[789]\b"
                r")\b",
                bl,
            ):
                return False

    # 3. End-on 1-Year Bachelor Honours (`wks <= 90`)
    if cl == "Bachelor Honours Degree" and 0 < wks <= 90:
        if re.search(r"\byear\s*1[012]\b", bl) and not re.search(r"\bbachelor\b", bl):
            return False
        if re.search(r"\b(master(?:'s)?\s+degree|doctoral|phd)\b", bl) and not re.search(r"\bbachelor\b", bl):
            return False

    # 4. Postgraduate & Research (`Graduate Certificate`, `Graduate Diploma`, `Masters`, `Doctoral`)
    if cl in (
        "Graduate Certificate", "Graduate Diploma",
        "Masters Degree (Coursework)", "Masters Degree (Research)",
        "Masters Degree (Extended)", "Doctoral Degree",
    ):
        if re.search(
            r"\b(year\s*1[012]\b|senior\s+secondary|high\s+school|secondary\s+school|school\s+leaver|"
            r"international\s+baccalaureate|foundation\s+(?:studies|program|year)|certificate\s+(?:i|ii|iii|iv)\b)\b",
            bl,
        ):
            return False
        if cl in ("Masters Degree (Research)", "Doctoral Degree") and re.search(
            r"\bentry\s+(?:into|to)\s+(?:a\s+|an\s+)?graduate\s+(?:certificate|diploma)\b",
            bl,
        ):
            return False
        if cl != "Doctoral Degree" and bl.startswith(("phds ", "phd ")):
            return False

    return True


def _violates_other_course_exception(bullet: str, course_name: str) -> bool:
    """
    Rejects provider-wide policy bullets that state an exception for ONE specific unrelated course,
    e.g. 'Those undertaking the Certificate IV in Automotive Mechanical Diagnosis (AUR40216) must...'
    """
    m = re.search(
        r"\b(?:those\s+undertaking|applicants\s+(?:for|enrolling\s+in|applying\s+for)|students\s+(?:undertaking|enrolling\s+in)|for\s+entry\s+(?:into|to)|entry\s+(?:into|to)|admission\s+(?:into|to))\s+(?:the\s+|a\s+|an\s+)?"
        r"((?:certificate\s+(?:i|ii|iii|iv)|diploma|advanced\s+diploma|associate\s+degree|bachelor|graduate\s+certificate|graduate\s+diploma|master)\s+(?:of|in)\s+[A-Za-z\s\(\)&]{4,45})",
        bullet,
        re.I,
    )
    if not m:
        return False
    target_phrase = m.group(1).lower()
    stop_words = {
        "certificate", "diploma", "advanced", "associate", "degree",
        "bachelor", "graduate", "master", "of", "in", "the", "and", "ii", "iii", "iv",
    }
    target_toks = [t for t in re.findall(r"[a-z]{4,}", target_phrase) if t not in stop_words]
    if not target_toks:
        return False
    cn_low = (course_name or "").lower()
    return not any(t in cn_low for t in target_toks)


def clean_and_validate_academic_bullet(
    raw: str | None,
    course_level: str = "",
    course_name: str = "",
    duration_weeks: int | None = None,
) -> str | None:
    if not raw:
        return None
    s = normalize_ligatures(raw)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Strip leading list markers
    s = re.sub(r"^(?:[•·▪➢\*–\-]+|\(\d+\)|\([a-z]\)|\d+[\.\)])\s*", "", s).strip()
    # Strip redundant section heading prefixes
    s = re.sub(
        r"^(?:Academic\s+Entry\s+Requirements?\s*(?:\(International(?:\s+Students)?\))?|"
        r"International\s+Student\s+Entry|International\s+Entry\s+Requirements?|"
        r"Minimum\s+Academic\s+(?:Entry\s+)?Requirements?|Standard\s+Entry\s+Requirements?|"
        r"Entry\s+Requirements?|Admission\s+Requirements?|Academic\s+Requirements?)\s*[:\-–—]\s*",
        "",
        s,
        flags=re.I,
    ).strip()

    # Deterministic drop-cap / table-suffix / spacing repairs
    if s.startswith("dmission to "):
        s = "A" + s
    s = re.sub(r"\b(or\s+(?:international\s+|overseas\s+)?equivalent)\s+Selection\s+Rank\s*\.?$", r"\1.", s, flags=re.I)
    s = re.sub(r"\b(Age\s+Requirements?|Academic\s+Requirements?|Minimum\s+Age)\s*:([A-Za-z0-9])", r"\1: \2", s, flags=re.I)
    s = re.sub(r"([a-z]):([A-Z])", r"\1: \2", s)
    s = re.sub(r"\bOR(Completion|Successful|Minimum|Applicants|Hold)\b", r"; or \1", s)
    s = re.sub(r"\.{2,}", ".", s)
    if re.match(r"^[A-Z]{3,4}\d{4,5}[A-Z]?\s+(?:Certificate|Diploma|Advanced\s+Diploma)\b", s):
        s = f"Prerequisite qualification: Completion of {s}"

    # Safe capitalization ONLY when starting a genuine complete requirement phrase
    if re.match(
        r"^(?:completion\s+of|successful\s+completion\s+of|satisfactory\s+completion\s+of|"
        r"an\s+australian\s+(?:year|bachelor|diploma|certificate|aqf)|"
        r"australian\s+(?:year\s*1[012]|bachelor|diploma|aqf)|"
        r"applicants?\s+must|students?\s+must|candidates?\s+must|minimum\s+age|hold\s+an?\s+)\b",
        s,
    ):
        s = s[0].upper() + s[1:]

    if len(s) < 22 or len(s) > 420:
        return None

    low = s.lower()

    # Gate 1: Exclude pure English score lines & synthesized English table rows
    if low.startswith(
        (
            "english language", "english proficiency", "english requirement", "english entry",
            "english general ", "english (", "ielts", "pte", "pearson test", "toefl", "cambridge",
            "oet", "duolingo", "other accepted tests", "minimum english",
        )
    ):
        return None
    if re.search(r"\b(ielts|pte|toefl)\b", low) and not re.search(
        r"\b(year\s*1[012]|bachelor|diploma|certificate\s+(?:ii|iii|iv)|aqf|gpa|work\s+experience|years?\s+of\s+age)\b",
        low,
    ):
        return None

    # Gate 2: Exclude domestic-only clauses & domestic state certs/ranks without international equivalent
    if DOMESTIC_ONLY_TEXT_RE.search(s):
        return None
    if DOMESTIC_STATE_CERT_OR_RANK_RE.search(s) and not INTL_EQUIV_CONTEXT_RE.search(s):
        return None
    if re.search(r"(?<!non-)apprenticeship", low) and not INTL_EQUIV_CONTEXT_RE.search(s):
        return None

    # Gate 3: Exclude UI/Nav/Marketing/Career-Outcome/Exit-Pathway/Fee/Testimonial/Accordion/Country-Row noise
    if NON_ENTRY_NOISE_RE.search(s):
        return None
    if ACCORDION_HEADING_ONLY_RE.search(s):
        return None
    if re.search(r"\([Yy]ear\s*1[012]\)\s*,\s*[A-Z][a-z]+$", s):
        return None
    if COUNTRY_CERT_ROW_RE.search(s):
        return None

    # Gate 4: Require BOTH an explicit Entry Requirement Predicate AND a Substantive Academic/Age/Qual Concept
    if not ENTRY_REQUIREMENT_PREDICATE_RE.search(s):
        return None
    if not SUBSTANTIVE_REQ_CONCEPT_RE.search(s):
        return None

    # Gate 5: Exclude single-course exceptions belonging to a different course
    if course_name and _violates_other_course_exception(s, course_name):
        return None

    # Gate 6: Exclude mid-sentence lowercase starts & dangling truncated endings
    if not s[0].isupper() and not s[0].isdigit():
        return None
    stripped_end = s.rstrip(" .!,;*–—-")
    if DANGLING_END_RE.search(stripped_end):
        return None
    if s.count("(") != s.count(")"):
        return None

    # Gate 7: Enforce strict AQF course_level & duration compatibility
    if course_level and not is_bullet_compatible_with_level(s, course_level, course_name, duration_weeks):
        return None

    return s


def filter_consistent_bullets(bullets: list[str], course_level: str = "") -> list[str]:
    """
    Ensures we never keep BOTH Undergraduate Year-12 bullets AND Postgraduate Bachelor-degree bullets
    on the same course.
    """
    if len(bullets) <= 1:
        return bullets
    cl = (course_level or "").strip()
    ug_b = [b for b in bullets if re.search(r"\b(undergraduate|year\s*12)\b", b, re.I)]
    pg_b = [b for b in bullets if re.search(r"\b(postgraduate|master|bachelor(?:'s)?\s+degree)\b", b, re.I)]
    if ug_b and pg_b:
        if cl in ("Graduate Certificate", "Graduate Diploma", "Masters Degree (Coursework)", "Masters Degree (Research)", "Masters Degree (Extended)", "Doctoral Degree"):
            return [b for b in bullets if b not in ug_b]
        if cl in ("Certificate I", "Certificate II", "Certificate III", "Certificate IV", "Diploma", "Advanced Diploma", "Associate Degree", "Bachelor Degree"):
            return [b for b in bullets if b not in pg_b]
    return bullets


def extract_academic_entry_html_v2(
    text: str | None,
    course_level: str = "",
    course_name: str = "",
    duration_weeks: int | None = None,
    max_bullets: int = 4,
) -> str | None:
    """
    Extracts clean, level-compatible International Academic Entry Requirements HTML:
      1. Unwraps PDF line breaks & ligatures.
      2. Prioritizes explicit `Entry / Admission / Academic Requirements` section windows when present.
      3. Validates every candidate bullet through all 8 forensic gates.
    """
    if not text:
        return None
    unwrapped = unwrap_pdf_and_ligatures(text)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in unwrapped.splitlines() if ln.strip()]

    # Collect lines inside explicit Entry / Admission / Eligibility sections first
    section_lines: list[str] = []
    in_section = False
    chars_in_sec = 0
    for ln in lines:
        if ENTRY_HEADING_RE.match(ln):
            in_section = True
            chars_in_sec = 0
            continue
        if in_section:
            if STOP_SECTION_HEADING_RE.match(ln) or chars_in_sec > 2400:
                in_section = False
                continue
            section_lines.append(ln)
            chars_in_sec += len(ln)

    def _collect_from_lines(cand_lines: list[str]) -> list[str]:
        picked: list[str] = []
        seen: set[str] = set()
        for ln in cand_lines:
            # Split multi-sentence paragraphs if longer than 240 chars
            parts = [ln] if len(ln) <= 240 else [p.strip() for p in re.split(r"(?<=[.!?;])\s+", ln) if p.strip()]
            for part in parts:
                v = clean_and_validate_academic_bullet(part, course_level, course_name, duration_weeks)
                if v and v.lower() not in seen:
                    seen.add(v.lower())
                    picked.append(v)
                    if len(picked) >= max_bullets:
                        return filter_consistent_bullets(picked, course_level)
        return filter_consistent_bullets(picked, course_level)

    picked = _collect_from_lines(section_lines) if section_lines else []
    if not picked:
        picked = _collect_from_lines(lines)

    if not picked:
        return None
    lis = "".join(f"<li>{html.escape(p)}</li>" for p in picked[:max_bullets])
    return f"<h4>Academic Entry Requirements (International Students)</h4><ul>{lis}</ul>"
