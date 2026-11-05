"""Parsing tests on saved API pages (tests/api/, 3 rows each, personal values replaced): fast, no network."""
import json
import re
from pathlib import Path

import crawl
import family

API = Path(__file__).parent / "api"
CONTACT = ("phone", "email", "mobile", "pan_no", "_pan", "bank", "username")


def load(name):
    return json.loads((API / f"{name}.json").read_text(encoding="utf-8"))


def flat_ok(rows):
    """Every record is flat (no dicts or lists), typed, and keeps no contact data."""
    assert rows
    for rid, rec in rows:
        assert rid == str(rec["id"]) and isinstance(rec["id"], int)
        for k, v in rec.items():
            assert v is None or isinstance(v, (str, int, float, bool)), (k, v)
            assert not any(c in k.lower() for c in CONTACT), k
        assert "someone@example.org" not in json.dumps(rec) and "0000000000" not in json.dumps(rec)


def test_projects():
    rows, more = crawl.parse_rows("projects", load("projects"))
    flat_ok(rows)
    assert len(rows) == 3 and more is True
    p = dict(rows)
    rec = next(r for r in p.values() if r["tender_no"])
    assert isinstance(rec["estimated_cost"], float) and rec["start_date"][4] == "-" and rec["n_tender_allotments"] >= 1
    assert rec["zone"] and rec["division"] and rec["category"]


def test_circulars():
    rows, more = crawl.parse_rows("circulars", load("circulars"))
    flat_ok(rows)
    _, c = rows[0]
    assert c["id"] == 3601 and c["category"] == "office_order" and c["uploaded_date"] == "2026-11-03"
    assert c["file_url"].startswith("https://") and c["file_ext"] == "pdf"


def test_court_cases_active_and_archived():
    for name, archived in (("court-cases", False), ("court-cases-archived", True)):
        rows, more = crawl.parse_rows("court-cases", load(name))
        flat_ok(rows)
        assert all(r["is_archived"] is archived for _, r in rows)
    rows, _ = crawl.parse_rows("court-cases", load("court-cases"))
    rec = next(r for _, r in rows if r["n_hearing_notes"])
    assert rec["last_hearing_date"] and rec["court"] and rec["n_concern_officers"] >= 1


def test_arbitration():
    rows, more = crawl.parse_rows("arbitration", load("arbitration"))
    flat_ok(rows)
    rec = next(r for _, r in rows if r["claimants"])
    assert isinstance(rec["total_claim_amount"], float) and rec["office"] and rec["n_parties"] >= 1


def test_contractors_drop_contact_data():
    raw = load("contractors")
    assert raw["data"][0]["phone"] and raw["data"][0]["pan_no"]        # in the API answer ...
    rows, more = crawl.parse_rows("contractors", raw)
    flat_ok(rows)                                                      # ... not in the record
    assert more is True and all(r["is_active"] is True for _, r in rows)
    scrubbed = json.dumps(crawl.scrub(raw))                            # nor in the raw copy kept in the bucket
    assert "0000000000" not in scrubbed and "AAAAA0000A" not in scrubbed and "bankDetails" not in scrubbed


def test_typed_values():
    assert crawl.day("2026-08-02T18:30:00.000Z") == "2026-08-03"       # midnight in Delhi, stored in UTC
    assert crawl.day("2026-12-08T00:00:00.000Z") == "2026-12-08"
    assert crawl.day("2026-11-03T00:00:00") == "2026-11-03" and crawl.day("") is None
    assert crawl.num("9028610.00") == 9028610.0 and crawl.num(None) is None and crawl.num("x") is None
    assert crawl.flag("yes") is True and crawl.flag(False) is False and crawl.flag("maybe") is None
    assert crawl.txt("  a \n b ") == "a b" and crawl.txt("-") is None


def test_empty_page_gives_nothing():
    assert crawl.parse_rows("projects", {"content": [], "last": True}) == ([], False)
    assert crawl.parse_rows("court-cases", {"data": {"cases": [], "pagination": {"hasNextPage": False}}}) == ([], False)
    assert crawl.parse_rows("contractors", {"data": [], "pagination": {"page": 5, "totalPages": 4}}) == ([], False)


SITE = "https://www.pwddelhi.gov.in"


def test_requests_use_each_apis_page_numbering():
    assert crawl.request_for(SITE, "projects", 1, 100)[2]["page"] == 0           # 0-based
    assert crawl.request_for(SITE, "circulars", 2, 100)[3]["page"] == (None, "1")
    assert crawl.request_for(SITE, "court-cases", 1, 100)[2]["page"] == 1        # 1-based
    assert crawl.request_for(SITE, "court-cases", 1, 100, archived=True)[2]["is_archived"] == "true"
    assert crawl.request_for(SITE, "contractors", 3, 100)[2] == {"page": 3, "limit": 100, "sort_by": "id", "sort_order": "ASC"}
    assert crawl.request_for(SITE, "unnamed-roads", 2, 100)[1:3] == (SITE + "/api/maintenance/roads/search", {"page": 2, "limit": 100, "list_type": "unnamed"})
    assert crawl.request_for(SITE, "street-lights", date="2026-11-03")[2] == {"date": "2026-11-03"}


def test_circular_files_skip_old_and_give_up_after_three_attempts(monkeypatch):
    hub = crawl.LocalHub()
    recs, _ = crawl.parse_rows("circulars", load("circulars"))           # uploaded 2026-11-03, 2026-11-02, ...
    monkeypatch.setattr(crawl, "download", lambda hub, url: ("ok", b"%PDF-1.4 test"))
    stored, failed = crawl.circular_files(hub, recs, files_since="2026-11-03")
    assert [rid for rid, _ in stored] == ["3601"] and failed == 0
    assert stored[0][1]["file_status"] == "stored" and stored[0][1]["file_bytes"] == 13
    monkeypatch.setattr(crawl, "download", lambda hub, url: ("error", None))
    assert crawl.circular_files(hub, recs, attempt=1) == ([], 3)         # not stored: the item fails and is retried
    stored, failed = crawl.circular_files(hub, recs, attempt=3)          # third attempt: stored with the error noted
    assert failed == 0 and {r["file_status"] for _, r in stored} == {"error"}


def test_queue_covers_the_site():
    for p in family.PROJECTS:
        keys = [i["key"] for i in p["items"]]
        assert len(keys) == len(set(keys)) and all(len(k) <= 200 for k in keys)
        assert p["name"].startswith(family.FAMILY + "-")
        assert all(i["payload"]["kind"] in set(crawl.FLAT) | {"project-details", "project-details-ids", "street-lights", "pages"} for i in p["items"])
    assert all(re.fullmatch(r"[A-Za-z0-9._-]+", i["key"]) for p in family.PROJECTS for i in p["items"])   # keys are upload paths
    pages_ = next(p for p in family.PROJECTS if p["name"].endswith("-pages"))
    assert len(pages_["items"]) == len(family.PAGES) == 170
    court = next(p for p in family.PROJECTS if p["name"].endswith("court-cases"))
    first = [i for i in court["items"] if not i["key"].startswith(family.CRAWL + "b")]     # the first pass ("b" is the second)
    assert sum(i["payload"].get("archived", False) for i in first) == family.pages(family.TOTALS["court-cases-archived"])
    assert sum(i["payload"]["expected"] for i in first) == family.TOTALS["court-cases"] + family.TOTALS["court-cases-archived"]
    details = next(p for p in family.PROJECTS if p["name"].endswith("-project-details"))
    by_id = [i for i in details["items"] if i["payload"]["kind"] == "project-details-ids"]
    assert by_id and all(1 <= len(i["payload"]["ids"]) <= family.DETAIL_PAGE_SIZE for i in by_id)


def test_maintenance_inventory():
    for kind in ("buildings", "flyovers", "fobs", "subways", "roads", "unnamed-roads"):
        rows, more = crawl.parse_rows(kind, load(kind))
        flat_ok(rows)
        assert len(rows) == 5 and more is True, kind
        rec = rows[0][1]
        assert rec["asset_code"] and "division" in rec, kind                     # location names merged in
        assert "s_no" not in rec and not isinstance(rec.get("location"), dict)
    rows, _ = crawl.parse_rows("flyovers", load("flyovers"))
    assert all(isinstance(r["length_m"], float) for _, r in rows if r["length_m"] is not None)
    rows, _ = crawl.parse_rows("roads", load("roads"))
    assert rows[0][1]["is_unnamed"] is False and isinstance(rows[0][1]["total_length_km"], float)


def test_road_detail_merges_into_the_road():
    rows, _ = crawl.parse_rows("roads", load("roads"))
    detail = load("road-detail")
    rid, rec = next(((rid, r) for rid, r in rows if rid == str(detail["data"]["id"])), rows[0])
    merged = crawl.merge_road(rec, detail)
    assert merged["id"] == rec["id"] and "road_id_code" in merged and "mobile_hp" not in merged
    flat_ok([(rid, merged)])


def test_street_lights_one_record_per_division_and_day():
    rows, more = crawl.parse_rows("street-lights", load("street-lights"), date="2026-11-03")
    assert rows and not more
    rid, r = rows[0]
    assert rid == f"2026-11-03-{r['divisionId']}" and isinstance(r["installed"], float) and r["targetDate"] == "2026-11-04"


def test_nodal_officers_keep_category_and_drop_contacts():
    rows, more = crawl.parse_rows("nodal-officers", load("nodal-officers"))
    flat_ok(rows)
    assert all(r["category"] for _, r in rows) and not any("fax" in r for _, r in rows)
    assert not any("someone@example.org" in (r.get("address") or "") for _, r in rows)


def test_office_directory():
    rows, _ = crawl.parse_rows("office-directory", load("office-directory"))
    assert rows and all(rid.startswith("office-") for rid, _ in rows)
    for _, r in rows:
        assert all(v is None or isinstance(v, (str, int, float, bool)) for v in r.values())
        assert "0000000000" not in json.dumps(r) and "someone@example.org" not in json.dumps(r)
    assert rows[0][1]["officers"].startswith("Test name") and rows[0][1]["designations"]


def test_project_detail():
    parts = {k: crawl.scrub(v) for k, v in load("project-detail-parts").items()}
    rec = crawl.flat_project_detail(141428, parts)
    flat_ok([("141428", rec)])
    assert rec["tender_portal"] and rec["agreement_value"] and rec["n_status_changes"] >= 1
    assert rec["circle"] and rec["funding_source"] and rec["n_trend_months"] >= 1 and rec["n_media"] == 0
    assert rec["first_status_date"][4] == "-"


def test_pages_text_tables_and_documents():
    url = SITE + "/rti"
    page, rows, docs = crawl.parse_page((Path(__file__).parent / "pages" / "rti.html").read_text(encoding="utf-8"), url)
    assert page["page_id"] == "page:/rti" and page["title"] and page["n_chars"] > 100
    assert not any("urban-roads-manual" in d or "/uploads/" in d for d, _ in docs)   # menu and pop-up links: only on the home page
    home, _, home_docs = crawl.parse_page((Path(__file__).parent / "pages" / "rti.html").read_text(encoding="utf-8"), SITE + "/")
    assert any("/uploads/" in d for d, _ in home_docs) and any("urban-roads-manual" in d for d, _ in home_docs)
    page, rows, docs = crawl.parse_page((Path(__file__).parent / "pages" / "budget.html").read_text(encoding="utf-8"), SITE + "/budget?lang=hi", with_docs=False)
    assert page["lang"] == "hi" and rows and docs == []
    rid, r = rows[0]
    assert rid.startswith("row:page:/budget?lang=hi#") and r["cells"] and r["c1"] is not None


def test_scrub_removes_emails_in_free_text():
    assert crawl.scrub({"address": "MSO Building, New Delhi-110002Email ID: someone@example.org"}) == {"address": "MSO Building, New Delhi-110002"}


def test_project_photos_are_read_in_pages_of_100(monkeypatch):
    """A project with 250 photos: the media list is read at offset 0, 100 and 200, and every photo is kept."""
    hub = crawl.LocalHub()
    parts = load("project-detail-parts")
    detail = json.loads(json.dumps(parts["detail"]))
    detail["data"]["media"] = {"items": [], "total": 250}

    def fake_call(hub, method, url, **kw):
        if "/media?" in url:
            off = int(url.rsplit("offset=", 1)[1])
            items = [{"id": n, "file_url": f"https://example-bucket.s3.amazonaws.com/p/{n}.jpg"} for n in range(off, min(off + 100, 250))]
            return {"success": True, "data": {"items": items, "total": 250, "offset": off, "limit": 100}}
        for k, v in (("progress-chart", "progress"), ("location-history", "locations"), ("inspections", "inspections")):
            if url.endswith(k):
                return parts[v]
        return detail

    monkeypatch.setattr(crawl, "call", fake_call)
    monkeypatch.setattr(crawl, "download", lambda hub, url: ("ok", b"jpg"))
    rid, rec = crawl.project_detail(hub, 141428, 0, 1, {})
    assert rec["n_media"] == 250 and rec["media_stored"] == 250 and rec["detail_complete"] is True
