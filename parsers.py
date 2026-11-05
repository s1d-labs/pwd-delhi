"""Pure parsing for the pwd-delhi family: API requests, typed values, contact-data removal, and one flat record per row.
No network here; tests/test_parse.py runs it on saved API answers and pages."""
import hashlib
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------------------------------------------------------
# The APIs. `page` in a queue item is 1-based for every kind; each API's own numbering is set here.

MAINTENANCE = {"buildings": "buildings", "flyovers": "flyovers", "fobs": "fobs", "subways": "subways",
               "roads": "roads", "unnamed-roads": "roads"}


def request_for(site, kind, page=1, size=100, archived=False, date=None):
    """(method, url, params, files) for one API call."""
    if kind in ("projects", "project-details"):
        return "GET", f"{site}/api/projects/search", {"page": page - 1, "size": size}, None
    if kind == "circulars":                       # the site posts a multipart form
        return "POST", f"{site}/circular/api/search", None, {"page": (None, str(page - 1)), "size": (None, str(size))}
    if kind == "court-cases":
        params = {"page": page, "size": size, "office_type": "pwd_office", "report_type": "list_of_cases"}
        if archived:
            params["is_archived"] = "true"
        return "POST", f"{site}/api/litigation/court-cases/search", params, None
    if kind == "arbitration":
        return "POST", f"{site}/api/litigation/arbitration-cases/search", {"page": page, "size": size, "office_type": "pwd_office", "report_type": "general"}, None
    if kind == "contractors":                     # sorted by id, so new contractors land on the last pages
        return "GET", f"{site}/pms/api/contractors", {"page": page, "limit": size, "sort_by": "id", "sort_order": "ASC"}, None
    if kind in MAINTENANCE:
        params = {"page": page, "limit": size}
        if kind == "unnamed-roads":
            params["list_type"] = "unnamed"
        return "GET", f"{site}/api/maintenance/{MAINTENANCE[kind]}/search", params, None
    if kind == "nodal-officers":
        return "GET", f"{site}/api/nodal-officers", {"page": 1, "limit": 10000}, None
    if kind == "office-directory":
        return "GET", f"{site}/pms/api/users-tree", {}, None
    if kind == "street-lights":
        return "GET", f"{site}/street-light-inspection/api/data", {"date": date}, None
    raise ValueError(f"unknown kind {kind!r}")


def detail_requests(site, project_id):
    """The calls behind a project's detail window: {name: url}. 'media' is called only when the detail says there are photos."""
    b = f"{site}/api/projects/{project_id}"
    return {"detail": b, "progress": f"{b}/progress-chart", "locations": f"{b}/location-history", "inspections": f"{b}/inspections"}


def media_request(site, project_id, offset=0):
    return f"{site}/api/projects/{project_id}/media?limit=100&offset={offset}"


def road_request(site, road_id):
    return f"{site}/api/maintenance/roads/{road_id}"


def rows_and_more(kind, doc):
    """(rows, has_next_page) from one API answer."""
    if kind in ("projects", "project-details", "circulars"):
        return doc.get("content") or [], doc.get("last") is False
    if kind in ("court-cases", "arbitration"):
        data = doc.get("data") or {}
        pg = data.get("pagination") or {}
        return data.get("cases") or [], bool(pg.get("hasNextPage"))
    if kind == "contractors":
        pg = doc.get("pagination") or {}
        return doc.get("data") or [], (pg.get("page") or 0) < (pg.get("totalPages") or 0)
    if kind in MAINTENANCE:
        data = doc.get("data") or {}
        pg = data.get("pagination") or {}
        return data.get(MAINTENANCE[kind]) or [], (pg.get("current_page") or 0) < (pg.get("total_pages") or 0)
    if kind == "nodal-officers":                   # header rows name the category of the rows after them
        rows, cat = [], None
        for r in doc.get("data") or []:
            if r.get("isHeader"):
                cat = r.get("category")
            else:
                rows.append({**r, "category": r.get("category") or cat})
        return rows, bool(doc.get("hasMore"))
    if kind == "office-directory":
        rows = []

        def walk(n, parent=None):
            rows.append({**{k: v for k, v in n.items() if k != "children"}, "parent_office": parent})
            for c in n.get("children") or []:
                walk(c, n.get("office_name"))
        for n in doc.get("data") or []:
            walk(n)
        return rows, False
    if kind == "street-lights":
        return [r for rs in (doc.get("divisionDataByZone") or {}).values() for r in rs], False
    raise ValueError(f"unknown kind {kind!r}")


# ---------------------------------------------------------------------------------------------------------------------------
# Contact data that stays out of records and raw files.

SCRUB_EXACT = {"pan_no", "opposition_pan", "bankDetails", "username", "password", "opposition_address",
               "opposition_representative_contact", "bar_council_id", "fax", "office_contact_no", "office_email_id",
               "hp_info", "hp_11_info"}
SCRUB_PART = ("phone", "email", "mobile", "contact_no")
EMAIL = re.compile(r"\s*(?:e-?mail(?: id)?\s*[:\-]?\s*)?[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", re.I)


def scrub(x):
    if isinstance(x, dict):
        return {k: scrub(v) for k, v in x.items() if k not in SCRUB_EXACT and not any(p in k.lower() for p in SCRUB_PART)}
    if isinstance(x, list):
        return [scrub(v) for v in x]
    if isinstance(x, str) and "@" in x:          # e-mail addresses typed into free text (e.g. an office address)
        return EMAIL.sub("", x)
    return x


# ---------------------------------------------------------------------------------------------------------------------------
# Typed values. Empty strings and "-" become None; amounts are numbers as the API gives them (the site shows them / 100000 as lakh).

def txt(v):
    if v is None:
        return None
    v = re.sub(r"\s+", " ", str(v)).strip()
    return v if v and v != "-" else None


def num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def integer(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def flag(v):
    if isinstance(v, bool) or v is None:
        return v
    s = str(v).strip().lower()
    return True if s in ("true", "yes", "1", "y") else False if s in ("false", "no", "0", "n") else None


def day(v):
    """YYYY-MM-DD. Timestamps in UTC ('2026-08-02T18:30:00.000Z' = midnight in Delhi) are read as Indian time."""
    v = txt(v)
    if not v:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t.astimezone(IST) if t.tzinfo else t).date().isoformat()


def stamp(v):
    return txt(v)


def name(obj, key="name"):
    return txt(obj.get(key)) if isinstance(obj, dict) else None


def joined(values):
    vals = [v for v in (txt(x) for x in values) if v]
    return "; ".join(dict.fromkeys(vals)) or None


NUMERIC_KEY = re.compile(r"(cost|lakh|amount|_m$|_km$|_sqm$|length|width|area|clearance|lanes|^installed$|^working$|repaired|"
                         r"^no_of_(?!stories)|^year|_year$|percent|_pct$)")
DATE_KEY = re.compile(r"(date|_on$)", re.I)
ID_KEY = re.compile(r"(^id$|_id$|[a-z]Id$)")


def generic(r, drop=("s_no",)):
    """Flat, typed record from an API row: scalars kept (numbers by key name, dates as YYYY-MM-DD), nested objects merged
    in (their scalar fields), lists counted. The same key always gets the same type."""
    out = {}
    for k, v in r.items():
        if k in drop:
            continue
        if isinstance(v, dict):
            for k2, v2 in v.items():
                if not isinstance(v2, (dict, list)) and k2 not in out:
                    out[k2] = _typed(k2, v2)
        elif isinstance(v, list):
            out[f"n_{k}"] = len(v)
        else:
            out[k] = _typed(k, v)
    return out


def _typed(k, v):
    if isinstance(v, bool):
        return v
    if ID_KEY.search(k):
        return integer(v) if integer(v) is not None else txt(v)
    if NUMERIC_KEY.search(k):
        return num(v)
    if k.endswith("_at"):
        return stamp(v)
    if DATE_KEY.search(k):
        return day(v)
    if isinstance(v, (int, float)):
        return v
    return txt(v)


# ---------------------------------------------------------------------------------------------------------------------------
# One flat record per row.

def flat_project(r):
    ta = r.get("tenderAllotments") or []
    t0 = ta[0] if ta else {}
    phys = r.get("latest_physical_progress") or {}
    fin = r.get("latest_financial_progress") or {}
    con = r.get("contractor") or {}
    return {
        "id": integer(r.get("id")),
        "project_code": txt(r.get("project_id")),
        "project_name": txt(r.get("project_name")),
        "description": txt(r.get("description")),
        "status": txt(r.get("status")),
        "department": name(r.get("department")),
        "category_id": integer(r.get("category_id")),
        "category": name(r.get("category")),
        "scheme": name(r.get("scheme")),
        "zone_id": integer(r.get("zone_id")),
        "zone": name(r.get("zone")),
        "circle_id": integer(r.get("circle_id")),
        "division_id": integer(r.get("assigned_division_id")),
        "division": name(r.get("division")),
        "sub_division_id": integer(r.get("sub_division_id")),
        "sector_id": integer(r.get("sector_id")),
        "sub_sector_id": integer(r.get("sub_sector_id")),
        "work_type_id": integer(r.get("work_type_id")),
        "funding_source_id": integer(r.get("funding_source_id")),
        "head_type": txt(r.get("head_type")),
        "estimated_cost": num(r.get("estimated_cost")),
        "sanctioned_amount": num(r.get("sanctioned_amount")),
        "sanction_required": flag(r.get("sanction_required")),
        "sanction_type": txt(r.get("sanction_type")),
        "sanction_date": day(r.get("sanction_date")),
        "tender_required": flag(r.get("tender_required")),
        "tender_status": txt(r.get("tender_status")),
        "tender_amount": num(r.get("tender_amount")),
        "tender_amount_bfa": num(r.get("tender_amount_bfa")),
        "is_agreement": flag(r.get("is_agreement")),
        "tender_no": txt(t0.get("tender_no")),
        "agreement_no": txt(t0.get("agreement_no")),
        "award_amount": num(t0.get("award_amount")),
        "award_date": day(t0.get("award_date")),
        "n_tender_allotments": len(ta),
        "contractor_id": integer(r.get("contractor_id")),
        "contractor_name": txt(con.get("company_name")) or txt(con.get("name")),
        "contractor_registration_no": txt(con.get("registration_no")),
        "start_date": day(r.get("start_date")),
        "end_date": day(r.get("end_date")),
        "target_completion_date": day(r.get("target_completion_date")),
        "actual_start_date": day(r.get("actual_start_date")),
        "actual_completion_date": day(r.get("actual_completion_date")),
        "original_end_date": day(r.get("original_end_date")),
        "revised_end_date": day(r.get("revised_end_date")),
        "delay_reason": txt(r.get("delay_reason")),
        "handover_date": day(r.get("handover_date")),
        "handover_to": txt(r.get("handover_to")),
        "length_value": num(r.get("length_value")),
        "length_unit": txt(r.get("length_unit")) if r.get("length_value") not in (None, "") else None,
        "location_address": txt(r.get("location_address")),
        "ward_no": txt(r.get("ward_no")),
        "mp_constituency_id": integer(r.get("mp_constituency_id")),
        "mla_constituency_id": integer(r.get("mla_constituency_id")),
        "physical_progress_pct": num(phys.get("physical_progress_percent")),
        "physical_progress_date": day(phys.get("entry_date")),
        "physical_progress_activity": txt(phys.get("activity_description")),
        "expenditure_till_date": num(fin.get("expenditure_till_date")),
        "expenditure_pct": num(fin.get("cumulative_expenditure_percent")),
        "expenditure_date": day(fin.get("entry_date")),
        "last_bill_no": txt(fin.get("bill_no")),
        "has_bottlenecks": flag(r.get("has_bottlenecks")),
        "active_bottlenecks": integer(r.get("active_bottlenecks")),
        "media_count": integer(r.get("media_count")),
        "project_incharge": txt(r.get("project_incharge_name")),
        "remarks": txt(r.get("remarks")),
        "created_at": stamp(r.get("created_at")),
        "updated_at": stamp(r.get("updated_at")),
    }


def flat_circular(r):
    url = txt(r.get("fileUrl"))
    return {
        "id": integer(r.get("id")),
        "subject": txt(r.get("subject")),
        "reference_no": txt(r.get("referenceNo")),
        "category": txt(r.get("category")),
        "uploaded_date": day(r.get("createdAt")),
        "is_active": flag(r.get("isActive")),
        "file_url": url,
        "file_ext": file_ext(url) if url else None,
        "created_at": stamp(r.get("createdAt")),
        "updated_at": stamp(r.get("updatedAt")),
    }


def flat_court_case(r):
    notes = sorted(r.get("hearingNotes") or [], key=lambda h: h.get("hearing_date") or "")
    advs = r.get("caseAdvocates") or []
    officers = r.get("concernOfficers") or []
    ctype = r.get("caseType") or {}
    return {
        "id": integer(r.get("id")),
        "case_number": txt(r.get("case_number")),
        "financial_year": txt(r.get("financial_year")),
        "case_no_in_court": txt(r.get("case_no_in_court")),
        "case_title": txt(r.get("case_title")),
        "subject": txt(r.get("subject")),
        "diary_summary": txt(r.get("diary_summary")),
        "case_type": name(ctype),
        "case_type_code": txt(ctype.get("short_code")),
        "case_category_id": integer(r.get("case_category_id")),
        "case_category": name(r.get("caseCategory")),
        "court_id": integer(r.get("court_id")),
        "court": name(r.get("court")) or txt(r.get("court_name_other")),
        "court_room": txt(r.get("court_room_name")),
        "pwd_role": txt(r.get("pwd_role")),
        "opposite_party": txt(r.get("opposite_party")),
        "opposition_name": txt(r.get("opposition_name")),
        "opposition_type": txt(r.get("opposition_type")),
        "opposition_advocate": txt(r.get("opposition_representative_advocate")),
        "claim_by_opposition_amount": num(r.get("claim_by_opposition_amount")),
        "claim_by_opposition_description": txt(r.get("claim_by_opposition_description")),
        "amount_involved": num(r.get("amount_involved")),
        "current_status": txt(r.get("current_status")),
        "status_remarks": txt(r.get("status_remarks")),
        "risk_level": txt(r.get("risk_level")),
        "filing_date": day(r.get("filing_date")),
        "notice_receipt_date": day(r.get("date_of_receipt_of_notice")),
        "next_hearing_date": day(r.get("effective_next_hearing_date") or r.get("next_hearing_date")),
        "last_hearing_date": day(notes[-1].get("hearing_date")) if notes else None,
        "latest_proceedings": txt(r.get("latest_proceedings")),
        "n_hearing_notes": len(notes),
        "pwd_advocates": joined((a.get("advocate") or {}).get("name") for a in advs),
        "pwd_advocate_types": joined(a.get("appointment_type") for a in advs),
        "n_concern_officers": len(officers),
        "concern_officer_designations": joined(name(((o.get("user") or {}).get("designation"))) for o in officers),
        "zone_id": integer(r.get("pwd_zone_id")),
        "zone": name(r.get("zone")),
        "circle_id": integer(r.get("pwd_circle_id")),
        "circle": name(r.get("circle")),
        "division_id": integer(r.get("pwd_division_id")),
        "division": name(r.get("division")),
        "subdivision_id": integer(r.get("pwd_subdivision_id")),
        "subdivision": name(r.get("subdivision")),
        "is_archived": flag(r.get("is_archived")),
        "is_secretariat": flag(r.get("is_secretariat")),
        "is_work_related": flag(r.get("is_work_related")),
        "work_details": txt(r.get("work_details")),
        "pms_project_id": integer(r.get("pms_project_id")),
        "related_case_ids": joined(r.get("related_case_ids") or []),
        "created_at": stamp(r.get("created_at")),
        "updated_at": stamp(r.get("updated_at")),
    }


def flat_arbitration(r):
    hearings = sorted([h for h in r.get("hearings") or [] if h.get("hearing_date")], key=lambda h: h["hearing_date"])
    parties = r.get("parties") or []
    return {
        "id": integer(r.get("id")),
        "arbitration_number": txt(r.get("arbitration_number")),
        "financial_year": txt(r.get("financial_year")),
        "case_title": txt(r.get("case_title")),
        "external_case_number": txt(r.get("external_case_number")),
        "work_name": txt(r.get("work_name")),
        "work_description": txt(r.get("work_description")),
        "contractor_name": txt(r.get("contractor_name")),
        "claimants": joined(p.get("party_name") for p in parties if p.get("party_type") == "claimant"),
        "n_parties": len(parties),
        "arbitrators": txt(r.get("arbitrator_names")),
        "arbitration_type": txt(r.get("arbitration_type")),
        "arbitration_institution": txt(r.get("arbitration_institution")),
        "tender_amount": num(r.get("tender_amount")),
        "total_claim_amount": num(r.get("total_claim_amount")),
        "total_counter_claim_amount": num(r.get("total_counter_claim_amount")),
        "award_amount": num(r.get("award_amount")),
        "award_date": day(r.get("award_date")),
        "award_description": txt(r.get("award_description")),
        "interest_rate": num(r.get("interest_rate")),
        "interest_amount": num(r.get("interest_amount")),
        "interest_from_date": day(r.get("interest_from_date")),
        "interest_upto_date": day(r.get("interest_upto_date")),
        "acceptance_status": txt(r.get("acceptance_status")),
        "acceptance_date": day(r.get("acceptance_date")),
        "acceptance_remarks": txt(r.get("acceptance_remarks")),
        "current_status": txt(r.get("current_status")),
        "risk_level": txt(r.get("risk_level")),
        "planned_completion_date": day(r.get("planned_completion_date")),
        "actual_completion_date": day(r.get("actual_completion_date")),
        "case_registration_date": day(r.get("case_registration_date")),
        "notice_date": day(r.get("notice_date")),
        "notice_receipt_date": day(r.get("notice_receipt_date")),
        "fact_submitted_date": day(r.get("fact_submitted_date")),
        "counter_fact_submitted_date": day(r.get("counter_fact_submitted_date")),
        "first_hearing_date": day(r.get("first_hearing_date")),
        "next_hearing_date": day(r.get("next_hearing_date")),
        "last_hearing_date": day(hearings[-1]["hearing_date"]) if hearings else None,
        "n_hearings": len(hearings),
        "closing_date": day(r.get("closing_date")),
        "special_points": txt(r.get("special_points")),
        "remarks": txt(r.get("remarks")),
        "office": txt(r.get("office_display")),
        "zone_id": integer(r.get("pwd_zone_id")),
        "zone": name(r.get("zone")),
        "circle_id": integer(r.get("pwd_circle_id")),
        "circle": name(r.get("circle")),
        "division_id": integer(r.get("pwd_division_id")),
        "division": name(r.get("division")),
        "subdivision_id": integer(r.get("pwd_subdivision_id")),
        "subdivision": name(r.get("subdivision")),
        "is_secretariat": flag(r.get("is_secretariat")),
        "pms_project_id": integer(r.get("pms_project_id")),
        "created_at": stamp(r.get("created_at")),
        "updated_at": stamp(r.get("updated_at")),
    }


def flat_contractor(r):
    return {
        "id": integer(r.get("id")),
        "name": txt(r.get("name")),
        "company_name": txt(r.get("company_name")),
        "contact_person": txt(r.get("contact_person")),
        "address": txt(r.get("address")),
        "registration_no": txt(r.get("registration_no")),
        "gst_no": txt(r.get("gst_no")),
        "department_id": integer(r.get("department_id")),
        "is_active": flag(r.get("is_active")),
        "n_offices": len(r.get("offices") or []),
        "created_at": stamp(r.get("created_at")),
        "updated_at": stamp(r.get("updated_at")),
    }


def flat_asset(r):
    """Buildings, flyovers, foot-over-bridges, subways, roads: the generic flat record (location names merged in)."""
    return generic(r)


def flat_street_light(r, date):
    rec = generic(r)
    return {"id": f"{date}-{rec.get('divisionId')}", "date": date, **{k: v for k, v in rec.items() if k != "id"}}


def flat_nodal_officer(r):
    return generic(r)


def flat_office(r):
    officers = r.get("officers") or []
    return {
        "id": f"office-{r.get('office_id')}",
        "record_type": "office",
        "office_id": integer(r.get("office_id")),
        "office_name": txt(r.get("office_name")),
        "parent_id": integer(r.get("parent_id")),
        "parent_office": txt(r.get("parent_office")),
        "level": integer(r.get("level")),
        "address": txt(r.get("address")),
        "n_officers": len(officers),
        "designations": joined(o.get("designation_name") for o in officers),
        "officers": joined(o.get("name") for o in officers),
    }


FLAT = {"projects": flat_project, "circulars": flat_circular, "court-cases": flat_court_case,
        "arbitration": flat_arbitration, "contractors": flat_contractor, "nodal-officers": flat_nodal_officer,
        "office-directory": flat_office, **{k: flat_asset for k in MAINTENANCE}}


def parse_rows(kind, doc, **extra):
    """[(record_id, flat_record)] and has_next_page for one API answer. Pure."""
    rows, more = rows_and_more(kind, doc)
    out = []
    for r in rows:
        r = scrub(r)
        if kind == "street-lights":
            rec = flat_street_light(r, extra.get("date"))
        else:
            rec = FLAT[kind](r)
            if kind in ("roads", "unnamed-roads"):
                rec["is_unnamed"] = kind == "unnamed-roads"
        if rec.get("id") is not None:
            out.append((str(rec["id"]), rec))
    return out, more


def merge_road(rec, detail):
    """A road's list record plus its detail window (detail fields win where both have a value)."""
    d = generic(scrub((detail or {}).get("data") or {}))
    return {**rec, **{k: v for k, v in d.items() if v is not None or k not in rec}, "id": rec["id"]}


# ---------------------------------------------------------------------------------------------------------------------------
# Project details: the five calls behind the detail window, one flat record.

def flat_project_detail(pid, parts):
    """parts: {"detail", "progress", "locations", "inspections", "media"} API answers (scrubbed; None = not loaded)."""
    d = (parts.get("detail") or {}).get("data") or {}
    p = d.get("project") or {}
    ta = p.get("tenderAllotments") or []
    t0 = ta[0] if ta else {}
    logs = sorted(d.get("statusLogs") or [], key=lambda x: x.get("created_at") or "")
    bott = d.get("bottlenecks") or []
    prog = (parts.get("progress") or {}).get("data") or {}
    cur = prog.get("currentProgress") or {}
    cost = prog.get("costAnalysis") or {}
    trend = prog.get("monthlyTrend") or []
    locs = _items((parts.get("locations") or {}).get("data"))
    insp = _items((parts.get("inspections") or {}).get("data"))
    media = _items((parts.get("media") or {}).get("data")) or _items(d.get("media"))
    media_total = integer(((d.get("media") or {}) if isinstance(d.get("media"), dict) else {}).get("total"))
    last_loc = locs[-1] if locs else {}
    insp_dates = sorted(x for x in (day(i.get("inspection_date") or i.get("date") or i.get("created_at")) for i in insp) if x)
    return {
        "id": integer(pid),
        "project_code": txt(p.get("project_id")),
        "project_name": txt(p.get("project_name")),
        "status": txt(p.get("status")),
        "circle": name(p.get("circle")),
        "sub_division": name(p.get("subDivision")),
        "funding_source": name(p.get("fundingSource")),
        "work_type": name(p.get("work_type")),
        "sector": name(p.get("sector")),
        "sub_sector": name(p.get("sub_sector")),
        "mp_constituency": name(p.get("mp_constituency")),
        "mla_constituency": name(p.get("mla_constituency")),
        "major_head": name(p.get("majorHead")),
        "sub_major_head": name(p.get("subMajorHead")),
        "minor_head": name(p.get("minorHead")),
        "minor_head_code": txt(p.get("minor_head_code")),
        "allotment_type": txt(t0.get("allotment_type")),
        "tender_no": txt(t0.get("tender_no")),
        "tender_date": day(t0.get("tender_date")),
        "tender_portal": txt(t0.get("tender_portal")),
        "tender_portal_id": txt(t0.get("tender_portal_id")),
        "bid_type": txt(t0.get("bid_type")),
        "tender_type": txt(t0.get("tender_type")),
        "work_order_no": txt(t0.get("work_order_no")),
        "emd_amount": num(t0.get("emd_amount")),
        "bid_publishing_date": day(t0.get("bid_publishing_date")),
        "bid_submission_end_date": day(t0.get("bid_submission_end_date")),
        "bid_opening_date": day(t0.get("bid_opening_date")),
        "agreement_no": txt(t0.get("agreement_no")),
        "agreement_value": num(t0.get("agreement_value")),
        "agreement_date": day(t0.get("agreement_date")),
        "work_start_date": day(t0.get("work_start_date")),
        "contractor_id": integer(t0.get("contractor_id") or p.get("contractor_id")),
        "n_tender_allotments": len(ta),
        "n_status_changes": len(logs),
        "status_history": joined(f"{day(x.get('approved_at') or x.get('created_at'))} {x.get('to_status')}" for x in logs),
        "first_status_date": day(logs[0].get("approved_at") or logs[0].get("created_at")) if logs else None,
        "last_status_change_date": day(logs[-1].get("approved_at") or logs[-1].get("created_at")) if logs else None,
        "n_bottlenecks": len(bott),
        "bottlenecks": joined(b.get("description") or b.get("title") or b.get("bottleneck_type") for b in bott),
        "physical_progress_pct": num(cur.get("physical")),
        "financial_progress_pct": num(cur.get("financial")),
        "time_elapsed_pct": num(cur.get("time")),
        "project_cost": num(cost.get("projectCost")),
        "expenditure": num(cost.get("expenditure")),
        "cost_remaining": num(cost.get("remaining")),
        "n_trend_months": len(trend),
        "monthly_trend": joined(f"{m.get('month')}: physical {m.get('physical')}, financial {m.get('financial')}" for m in trend),
        "n_locations": len(locs),
        "last_latitude": num(last_loc.get("latitude") or last_loc.get("lat")),
        "last_longitude": num(last_loc.get("longitude") or last_loc.get("lng") or last_loc.get("lon")),
        "n_inspections": len(insp),
        "last_inspection_date": insp_dates[-1] if insp_dates else None,
        "n_media": max(len(media), media_total or 0),
    }


def _items(x):
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        for k in ("items", "data", "rows", "records"):
            if isinstance(x.get(k), list):
                return x[k]
    return []


def media_items(parts):
    return _items((parts.get("media") or {}).get("data"))


def media_url(m):
    for k in ("file_url", "fileUrl", "url", "media_url", "mediaUrl", "path", "file_path", "s3_url"):
        v = m.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return None


# ---------------------------------------------------------------------------------------------------------------------------
# Site pages (sitemap): text, table rows and linked documents.

DOC_EXT = {"pdf", "zip", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "rar", "odt", "ods", "txt", "rtf"}


def file_ext(url):
    last = urlsplit(url).path.rsplit("/", 1)[-1]
    ext = last.rsplit(".", 1)[-1].lower() if "." in last else ""
    return ext if re.fullmatch(r"[a-z0-9]{1,5}", ext) else "bin"


def doc_id(url):
    return "doc-" + hashlib.sha1(url.encode()).hexdigest()[:16]


def page_id(url):
    sp = urlsplit(url)
    return "page:" + (sp.path or "/") + (f"?{sp.query}" if sp.query else "")


def parse_page(html, url, with_docs=True):
    """(page record, [(id, table row record)], [(doc url, link text)]). The header, navigation and footer (the same on every
    page) are left out, except on the home page, whose body is mostly links."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    pid = page_id(url)
    home = urlsplit(url).path in ("", "/")
    title = txt(soup.title.get_text()) if soup.title else None
    for t in soup(["script", "style", "noscript", "svg"]):     # <template> stays: the home page keeps its pop-up links there
        t.decompose()
    if not home:
        for t in soup(["header", "nav", "footer"]):
            t.decompose()
    body = soup.find("main") or soup.body or soup
    rows = []
    for ti, table in enumerate(body.find_all("table"), 1):
        head = None
        for ri, tr in enumerate(table.find_all("tr"), 1):
            cells = [txt(c.get_text(" ")) or "" for c in tr.find_all(["th", "td"])]
            if not any(cells):
                continue
            if tr.find("th") and head is None:
                head = cells
                continue
            rec = {"record_type": "table_row", "page_id": pid, "page_url": url, "table_no": ti, "row_no": ri,
                   "headers": " | ".join(head) if head else None, "cells": " | ".join(cells)}
            for ci, c in enumerate(cells[:15], 1):
                rec[f"c{ci}"] = c or None
            rows.append((f"row:{pid}#{ti}.{ri}", rec))
    docs = {}
    if with_docs:
        for a in (soup if home else body).find_all("a", href=True):
            href = urljoin(url, a["href"].strip())
            if href.startswith("http") and file_ext(href) in DOC_EXT and href not in docs:
                docs[href] = txt(a.get_text(" ")) or txt(a.get("title"))
    text = txt(body.get_text(" "))
    page = {"record_type": "page", "page_id": pid, "url": url, "title": title,
            "lang": "hi" if "lang=hi" in url else "en", "text": text[:100000] if text else None,
            "n_chars": len(text or ""), "n_tables": len(body.find_all("table")), "n_table_rows": len(rows),
            "n_documents": len(docs)}
    return page, rows, list(docs.items())
