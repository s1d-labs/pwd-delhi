"""What this family scrapes: everything public on www.pwddelhi.gov.in (PWD, Government of NCT of Delhi).

FAMILY must equal the repo name (s1d-labs/<FAMILY>) and the family on scrape.s1d.me/runners.
Each project is one data set; each queue item is one API page, one day, or one site page. The task (crawl.py) builds the
request from the item's `kind` and payload, so the payload stays small.

Access: the site (AWS load balancer) refuses foreign IPs and also the 191.219.x proxy range; on 2026-11-04 it answered only
through the 95.134.119.x proxies (in-08 .. in-10). So every project runs with proxy="IN", proxy_only=["95.134.119."].

A new full pass: change CRAWL (e.g. "2026-12") and run the Setup workflow. The new keys go in the queue; old ones stay done.
In that pass, set FILES_SINCE to the date of the last pass: circulars uploaded before it are skipped, so their files (in the
bucket already) are not downloaded again. Street-light days are keyed by date: Setup adds the days since the last run.
"""
import json
import math
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

FAMILY = json.loads((Path(__file__).parent / "scrape.json").read_text())["family"]

HOST = "www.pwddelhi.gov.in"
SITE = f"https://{HOST}"
PROBE = f"{FAMILY}:{HOST}"
CRAWL = "2026-11"            # tag in every queue key; change it for a new full pass
FILES_SINCE = ""             # "" = all circulars and their files; a later pass: the date of the last pass ("2026-11-04")
PAGE_SIZE = 100              # rows per API page (the site's own maximum in its page-size menus)
DETAIL_PAGE_SIZE = 25        # projects per project-details item (about 100 requests each)
PROXY_ONLY = ["95.134.119."]  # the only proxy range the site let in on 2026-11-04
STREET_LIGHTS_FROM = date(2026, 3, 1)   # the street-light report has data from mid-2026; earlier days come back empty

# Row counts the APIs reported on 2026-11-04. Each queue gets 15 % more pages (+1) so the queue still covers the site as it grows;
# pages past the end come back empty and finish as done with 0 rows. The last page reports "queue_short" if the site has more.
TOTALS = {
    "projects": 13139,
    "circulars": 3582,
    "court-cases": 818,            # active cases
    "court-cases-archived": 1331,  # archived cases (a separate list on the site)
    "arbitration": 390,
    "contractors": 9014,
    "buildings": 1011,
    "flyovers": 78,
    "fobs": 15,
    "subways": 62,
    "roads": 1297,
    "unnamed-roads": 18,
}
PAGES = [u.strip() for u in (Path(__file__).parent / "pages.txt").read_text().splitlines() if u.strip()]   # the site's sitemap


def pages(total, size=PAGE_SIZE):
    return math.ceil(total * 1.15 / size) + 1


def queue(kind, total, size=PAGE_SIZE, stem=None, **extra):
    n = pages(total, size)
    stem = stem or CRAWL
    out = []
    for p in range(1, n + 1):
        payload = {"kind": kind, "page": p, "size": size, "expected": max(0, min(size, total - (p - 1) * size)), **extra}
        if kind == "circulars" and FILES_SINCE:
            payload["files_since"] = FILES_SINCE
        if p == n:
            payload["last"] = True       # the task reports "queue_short" when the site still has a next page here
        out.append({"key": f"{stem}-p{p:04d}", "payload": payload})
    return out


def page_key(url):
    sp = urlsplit(url)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", sp.path.strip("/")).strip("-") or "home"
    return f"{CRAWL}-{slug}{'-hi' if 'lang=hi' in sp.query else ''}"


def street_light_days(until=None):
    until = until or date.today()
    d, out = STREET_LIGHTS_FROM, []
    while d <= until:
        out.append({"key": f"day-{d.isoformat()}", "payload": {"kind": "street-lights", "date": d.isoformat(), "expected": 3}})
        d += timedelta(days=1)
    return out


def project(kind, title, record_label, items, unit_label="page", **pool):
    return {
        "name": f"{FAMILY}-{kind}",
        "title": title,
        "task": "crawl",
        "target": HOST,
        "env": {"SITE": SITE, "KIND": kind},
        "probe": PROBE,
        "unit_label": unit_label,
        "record_label": record_label,
        "items": items,
        "pool": pool,
    }


PROJECTS = [
    project("projects", "PWD Delhi works and projects (PMS)", "project", queue("projects", TOTALS["projects"])),
    project("circulars", "PWD Delhi circulars and office orders (with files)", "circular", queue("circulars", TOTALS["circulars"]),
            threads=4),
    project("court-cases", "PWD Delhi court cases (active and archived)", "court case",
            queue("court-cases", TOTALS["court-cases"], stem=f"{CRAWL}-active")
            + queue("court-cases", TOTALS["court-cases-archived"], stem=f"{CRAWL}-archived", archived=True)),
    project("arbitration", "PWD Delhi arbitration cases", "arbitration case", queue("arbitration", TOTALS["arbitration"])),
    project("contractors", "PWD Delhi contractors (PMS register)", "contractor", queue("contractors", TOTALS["contractors"])),
    project("project-details", "PWD Delhi project details (progress, status history, inspections, photos)", "project",
            queue("project-details", TOTALS["projects"], size=DETAIL_PAGE_SIZE)),
    project("buildings", "PWD Delhi maintenance inventory: buildings", "building", queue("buildings", TOTALS["buildings"])),
    project("flyovers", "PWD Delhi maintenance inventory: flyovers", "flyover", queue("flyovers", TOTALS["flyovers"])),
    project("fobs", "PWD Delhi maintenance inventory: foot-over-bridges", "foot-over-bridge", queue("fobs", TOTALS["fobs"])),
    project("subways", "PWD Delhi maintenance inventory: subways", "subway", queue("subways", TOTALS["subways"])),
    project("roads", "PWD Delhi maintenance inventory: roads (named and unnamed, with details)", "road",
            queue("roads", TOTALS["roads"], stem=f"{CRAWL}-named")
            + queue("unnamed-roads", TOTALS["unnamed-roads"], stem=f"{CRAWL}-unnamed")),
    project("street-lights", "PWD Delhi street-light inspections (per division and day)", "division-day", street_light_days(),
            unit_label="day"),
    project("offices", "PWD Delhi nodal officers and office directory", "office",
            [{"key": f"{CRAWL}-nodal-officers", "payload": {"kind": "nodal-officers", "page": 1, "expected": 363}},
             {"key": f"{CRAWL}-office-directory", "payload": {"kind": "office-directory", "page": 1, "expected": 141}}]),
    project("pages", "PWD Delhi site pages (text, tables, linked documents)", "page or document",
            [{"key": page_key(u), "payload": {"kind": "pages", "url": u, "expected": 1}} for u in PAGES],
            threads=4),
]

# Pool settings for every project (a project's own "pool" values win). Speed: each worker thread waits delay_ms after every
# request to the site; files on S3 load in the same threads. At most 3 workers run at once (3 proxies reach the site).
POOL = {"proxy": "IN", "proxy_only": PROXY_ONLY, "threads": 2, "delay_ms": 1000, "max_workers": 2}

# Health check (through the same proxies as the projects). Work holds while it fails.
PROBES = [{"name": PROBE, "url": f"{SITE}/circular", "expect": "circular", "proxy": "IN"}]
