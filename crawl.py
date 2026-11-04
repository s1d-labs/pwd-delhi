"""The family's `crawl` task (see scrape.json): everything public on www.pwddelhi.gov.in, one queue item at a time.

    python crawl.py --local KIND [PAGE | DATE | URL]     # one item without the hub: records to stdout, counters to stderr
      KIND: projects | project-details | circulars | court-cases | court-cases-archived | arbitration | contractors |
            buildings | flyovers | fobs | subways | roads | unnamed-roads | street-lights (DATE) | nodal-officers |
            office-directory | pages (URL)
      On --local, rows that need more calls (project details, roads, circular files, page documents) stop after LOCAL_LIMIT (3).

The pool worker runs it with SCRAPE_POOL_PROJECT, KIND and SITE (the project's env), SCRAPE_JOB_INDEX / SCRAPE_TOTAL_JOBS,
SCRAPE_MAX_HOURS, and (proxy="IN") HTTPS_PROXY / SCRAPE_PROXY: an Indian proxy, because the site refuses foreign IPs.

Kinds of queue items:
  * API list pages (most kinds): one API page; one flat record per row (the site's own id). Roads also load each road's detail.
  * project-details: one page of the project list (25 projects), then each project's detail window: detail, progress chart,
    location history, inspections, and photos when it has some (the photos go to the bucket: files/<project>/<n>.<ext>).
  * circulars: the circular's file goes to the bucket too (files/<id>.<ext>).
  * pages: one page of the sitemap: its text, its table rows, and the documents it links (docs/<id>.<ext> in the bucket).
Every item keeps its raw answer in the bucket (pages/<key>.json.gz or .html.gz), contact data removed (parsers.scrub).
"""
import gzip
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import requests

from parsers import *  # noqa: F401,F403  (tests and --local use the parsers through this module too)
from parsers import (day, detail_requests, doc_id, file_ext, flat_project_detail, media_items, media_request, media_url, merge_road,
                     parse_page, parse_rows, request_for, road_request, rows_and_more, scrub)

CONTENT_TYPES_DEFAULT = "application/octet-stream"

PROJECT = os.environ.get("SCRAPE_POOL_PROJECT", "")
KIND = os.environ.get("KIND", "")
SITE = os.environ.get("SITE", "https://www.pwddelhi.gov.in").rstrip("/")
SITE_HOST = urlsplit(SITE).hostname
DEADLINE = time.time() + float(os.environ.get("SCRAPE_MAX_HOURS", "1")) * 3600 - 120
UA = "s1d-labs scraper (https://github.com/s1d-labs)"
API_HEADERS = {"User-Agent": UA, "X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
LOCAL_LIMIT = None           # set by --local: rows that need more calls stop after this many

# Files on S3 and our uploads (B2) go direct, not through the Indian proxy: S3 answers from anywhere, and the proxies are few.
# If S3 refuses a direct request, the file is tried once more through the proxy (SCRAPE_PROXY).
for _k in ("NO_PROXY", "no_proxy"):
    os.environ[_k] = ",".join(x for x in (os.environ.get(_k, ""), ".amazonaws.com", ".backblazeb2.com") if x)

_local = threading.local()


def site_session():
    """One requests.Session per thread for the site (it uses HTTPS_PROXY from the environment: the Indian proxy)."""
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
    return _local.s


def pause(hub):
    time.sleep(max(0.2, (hub.settings.get("delay_ms") or 0) / 1000))


def nthreads(hub, cap=8):
    return max(1, min(cap, int(hub.settings.get("threads") or 1)))


def limit(rows):
    return rows[:LOCAL_LIMIT] if LOCAL_LIMIT else rows


# ---------------------------------------------------------------------------------------------------------------------------
# Requests to the site: counted for the hub's speed control, with the hub's delay after each.

def call(hub, method, url, params=None, files=None, want="json"):
    """The answer (dict for JSON, str for HTML), or None."""
    t = time.time()
    try:
        r = site_session().request(method, url, params=params, files=files, timeout=60,
                                   headers=API_HEADERS if want == "json" else {"User-Agent": UA})
    except requests.RequestException:
        hub.metric("http_error")
        hub.error()
        pause(hub)
        return None
    hub.metric("n_http")
    hub.metric("t_http_s", time.time() - t)
    pause(hub)
    if r.status_code != 200:
        hub.metric(f"http_{r.status_code}")
        if r.status_code in (403, 429, 503):
            hub.block()
        return None
    if want == "json":
        try:
            return r.json()
        except ValueError:
            hub.metric("bad_json")
            hub.error()
            return None
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def fetch_api(hub, kind, page=1, size=100, archived=False, date=None):
    method, url, params, files = request_for(SITE, kind, page, size, archived, date)
    return call(hub, method, url, params=params, files=files)


def download(hub, url):
    """(status, bytes): 'ok', 'missing' (403/404/410: it will not come back) or 'error' (try later).
    Files on the site itself go through the proxy (counted as site requests); files elsewhere (S3) go direct first, then
    through the proxy if refused. Those are counted as file_* (not the site's n_http)."""
    if urlsplit(url).hostname == SITE_HOST:
        t = time.time()
        try:
            r = site_session().get(url, timeout=180, headers={"User-Agent": UA})
        except requests.RequestException:
            hub.metric("http_error")
            hub.error()
            return "error", None
        hub.metric("n_http")
        hub.metric("t_http_s", time.time() - t)
        pause(hub)
        if r.status_code == 200 and r.content:
            return "ok", r.content
        hub.metric(f"http_{r.status_code}")
        return ("missing" if r.status_code in (403, 404, 410) else "error"), None
    proxy = os.environ.get("SCRAPE_PROXY") or os.environ.get("HTTPS_PROXY")
    status = "error"
    for route in [None] + ([proxy] if proxy else []):
        s = requests.Session()
        s.trust_env = False
        if route:
            s.proxies = {"http": route, "https": route}
        t = time.time()
        try:
            r = s.get(url, timeout=180, headers={"User-Agent": UA})
        except requests.RequestException:
            hub.metric("file_error")
            status = "error"
            continue
        hub.metric("file_http")
        hub.metric("file_t_s", time.time() - t)
        if r.status_code == 200 and r.content:
            return "ok", r.content
        hub.metric(f"file_http_{r.status_code}")
        status = "missing" if r.status_code in (403, 404, 410) else "error"
    return status, None


def keep_file(hub, path, data, ext):
    up = hub.upload_many([(path, data, CONTENT_TYPES.get(ext, CONTENT_TYPES_DEFAULT))])[0]
    return {"file_status": "stored", "file_path": up["key"], "file_bytes": up["bytes"], "file_sha256": up["sha256"]}


CONTENT_TYPES = {"pdf": "application/pdf", "zip": "application/zip", "doc": "application/msword", "jpg": "image/jpeg",
                 "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif", "xls": "application/vnd.ms-excel",
                 "csv": "text/csv", "txt": "text/plain", "rar": "application/vnd.rar", "mp4": "video/mp4",
                 "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                 "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
NO_FILE = {"file_status": None, "file_path": None, "file_bytes": None, "file_sha256": None}


def pmap(hub, fn, items):
    with ThreadPoolExecutor(nthreads(hub)) as ex:
        return list(ex.map(fn, items))


# ---------------------------------------------------------------------------------------------------------------------------
# Handlers. Each returns (records, failed, raw, more): records to store, how many rows failed for now (the item then fails and
# is retried; from the third attempt the rows are stored with what loaded), the raw answer to keep, has-next-page.

def circular_files(hub, recs, files_since=None, attempt=1):
    """Download and keep each circular's file. files_since (YYYY-MM-DD): a later pass skips circulars uploaded before it
    (record and file). Failures use the key 'circular-<id>' (a record with the bare id would clear them at once)."""
    todo = [(rid, rec) for rid, rec in recs if not (files_since and rec.get("uploaded_date") and rec["uploaded_date"] < files_since)]
    hub.metric("circulars_skipped_old", len(recs) - len(todo))

    def one(pair):
        rid, rec = pair
        if not rec.get("file_url"):
            return rid, {**rec, **NO_FILE, "file_status": "none"}
        if time.time() > DEADLINE:
            return None
        status, data = download(hub, rec["file_url"])
        if status == "missing":
            hub.failure("file", f"circular-{rid}", "file server refused (403/404)", url=rec["file_url"])
            return rid, {**rec, **NO_FILE, "file_status": "missing"}
        if status != "ok":
            hub.failure("file", f"circular-{rid}", "download failed", url=rec["file_url"], attempt=attempt)
            return (rid, {**rec, **NO_FILE, "file_status": "error"}) if attempt >= 3 else None
        if attempt > 1:
            hub.clear_failure("file", f"circular-{rid}")
        return rid, {**rec, **keep_file(hub, f"files/{rid}.{rec['file_ext'] or 'bin'}", data, rec["file_ext"])}

    stored = [d for d in pmap(hub, one, todo) if d]
    return stored, len(todo) - len(stored)


def handle_list(hub, kind, pl, attempt):
    doc = fetch_api(hub, kind, int(pl.get("page", 1)), int(pl.get("size", 100)), bool(pl.get("archived")), pl.get("date"))
    if doc is None:
        return None
    recs, more = parse_rows(kind, doc, date=pl.get("date"))
    raw, failed = scrub(doc), 0
    if kind == "circulars":
        recs, failed = circular_files(hub, limit(recs), pl.get("files_since"), attempt)
    elif kind in ("roads", "unnamed-roads"):
        details = {}

        def one(pair):
            rid, rec = pair
            d = call(hub, "GET", road_request(SITE, rid)) if time.time() < DEADLINE else None
            if d is None:
                return (rid, {**rec, "detail_loaded": False}) if attempt >= 3 else None
            details[rid] = scrub(d)
            return rid, {**merge_road(rec, d), "detail_loaded": True}
        todo = limit(recs)
        recs = [d for d in pmap(hub, one, todo) if d]
        failed = len(todo) - len(recs)
        raw = {"list": raw, "details": details}
    return recs, failed, raw, more


def handle_project_details(hub, pl, attempt):
    doc = fetch_api(hub, "project-details", int(pl["page"]), int(pl.get("size", 25)))
    if doc is None:
        return None
    rows, more = rows_and_more("project-details", doc)
    raws = {}

    def one(row):
        pid = row.get("id")
        if pid is None or time.time() > DEADLINE:
            return None
        parts = {k: call(hub, "GET", u) for k, u in detail_requests(SITE, pid).items()}
        media_total = ((parts.get("detail") or {}).get("data") or {}).get("media") or {}
        if (row.get("media_count") or 0) > 0 or (isinstance(media_total, dict) and (media_total.get("total") or 0) > 0):
            parts["media"] = call(hub, "GET", media_request(SITE, pid))
        complete = all(v is not None for v in parts.values())
        if not complete and attempt < 3:
            return None
        parts = {k: scrub(v) if v is not None else None for k, v in parts.items()}
        raws[str(pid)] = parts
        rec = flat_project_detail(pid, parts)
        stored = 0
        for i, m in enumerate(media_items(parts), 1):
            url = media_url(m)
            if not url:
                continue
            status, data = download(hub, url)
            if status == "ok":
                ext = file_ext(url)
                keep_file(hub, f"files/{pid}/{m.get('id') or i}.{ext}", data, ext)
                stored += 1
            else:
                hub.failure("photo", f"project-{pid}-{m.get('id') or i}", f"photo did not load ({status})", url=url)
        rec.update({"media_stored": stored, "detail_complete": complete})
        return str(pid), rec

    todo = limit(rows)
    recs = [d for d in pmap(hub, one, todo) if d]
    return recs, len(todo) - len(recs), {"list": scrub(doc), "details": raws}, more


def handle_page(hub, pl, attempt):
    url = pl["url"]
    html = call(hub, "GET", url, want="html")
    if html is None:
        return None
    hub.upload_many([(f"pages/{pl['key']}.html.gz", gzip.compress(html.encode()), "application/gzip")])
    # Hindi pages link the same documents as the English ones; /circular links the files the circulars project keeps already.
    page, rows, docs = parse_page(html, url, with_docs="lang=hi" not in url and urlsplit(url).path.rstrip("/") != "/circular")
    page["html_path"] = f"{PROJECT}/pages/{pl['key']}.html.gz"

    def one(pair):
        durl, text = pair
        did, ext = doc_id(durl), file_ext(durl)
        rec = {"record_type": "document", "url": durl, "link_text": text, "page_id": page["page_id"], "page_url": url,
               "file_ext": ext, "file_name": urlsplit(durl).path.rsplit("/", 1)[-1] or None}
        if time.time() > DEADLINE:
            return None
        status, data = download(hub, durl)
        if status == "ok":
            return did, {**rec, **keep_file(hub, f"docs/{did}.{ext}", data, ext)}
        hub.failure("document", f"document-{did}", f"document did not load ({status})", url=durl, page=url)
        if status == "missing" or attempt >= 3:
            return did, {**rec, **NO_FILE, "file_status": status}
        return None

    todo = limit(docs)
    drecs = [d for d in pmap(hub, one, todo) if d]
    return [(page["page_id"], page)] + rows + drecs, len(todo) - len(drecs), None, False


def handle(hub, kind, item):
    pl, attempt = {**item["payload"], "key": item["key"]}, int(item.get("attempts") or 1)
    if kind == "project-details":
        return handle_project_details(hub, pl, attempt)
    if kind == "pages":
        return handle_page(hub, pl, attempt)
    return handle_list(hub, kind, pl, attempt)


# ---------------------------------------------------------------------------------------------------------------------------

class LocalHub:
    """Stand-in for HubClient so `--local` runs without the hub: counters go to stderr, records to stdout, no uploads."""
    settings = {"threads": 2, "delay_ms": 500, "paused": False}

    def __init__(self):
        self.counts = {}
        self.lock = threading.Lock()

    def metric(self, name, value=1):
        with self.lock:
            self.counts[name] = self.counts.get(name, 0) + value

    def error(self, n=1):
        self.metric("errors", n)

    def block(self, n=1):
        self.metric("blocked", n)

    def failure(self, kind, key, reason="", **detail):
        print(f"failure {kind} {key}: {reason} {detail}", file=sys.stderr)

    def clear_failure(self, kind, key):
        pass

    def upload_many(self, files):
        for p, data, ct in files:
            print(f"(local: would upload {p}, {len(data)} bytes, {ct})", file=sys.stderr)
        return [{"path": p, "key": f"local/{p}", "bytes": len(d), "sha256": ""} for p, d, _ in files]


def local(kind, arg):
    global LOCAL_LIMIT
    LOCAL_LIMIT = int(os.environ.get("LOCAL_LIMIT", "3"))
    hub = LocalHub()
    payload = {"kind": kind}
    if kind == "court-cases-archived":
        kind, payload["archived"] = "court-cases", True
    if kind == "street-lights":
        payload["date"] = arg or day(time.strftime("%Y-%m-%d"))
    elif kind == "pages":
        payload["url"] = arg or SITE + "/"
    else:
        payload["page"] = int(arg or 1)
        payload["size"] = 25 if kind == "project-details" else 100
    out = handle(hub, kind, {"key": f"local-{kind}", "payload": payload, "attempts": 1})
    if out is None:
        print(f"the item did not load, counters {hub.counts}", file=sys.stderr)
        return
    recs, failed, raw, more = out
    for rid, data in recs:
        print(json.dumps({"record_id": rid, "data": data}, ensure_ascii=False))
    print(f"{len(recs)} records, {failed} failed, next page: {more}, counters {hub.counts}", file=sys.stderr)


def main():
    from scrape_hub_client import HubClient      # imported here so --local works without it
    hub = HubClient(PROJECT, job_index=int(os.environ.get("SCRAPE_JOB_INDEX", "0")), total_jobs=int(os.environ.get("SCRAPE_TOTAL_JOBS", "1")))
    try:
        while time.time() < DEADLINE:
            if hub.settings.get("paused"):
                time.sleep(30)                    # the hub holds us (site down, or the family owner paused it)
                continue
            items = hub.claim(1, steal=True, retry_failed=True)
            if not items:
                break                             # nothing left for this project: exit, the worker asks the hub for more
            item = items[0]
            key, pl = item["key"], item["payload"]
            kind = pl.get("kind") or KIND
            out = handle(hub, kind, item)
            if out is None:
                hub.complete([key], "failed", error="the page or API did not load")
                time.sleep(2)
                continue
            recs, failed, raw, more = out
            if pl.get("last") and more:
                hub.failure("queue_short", f"{kind}-{key}", "the site has more pages than the queue: raise TOTALS in family.py and run Setup")
            if raw is not None:
                hub.upload_many([(f"pages/{key}.json.gz", gzip.compress(json.dumps(raw, ensure_ascii=False).encode()), "application/gzip")])
            for rid, data in recs:
                hub.record(rid, data)
            if failed:
                hub.complete([key], "failed", error=f"{failed} row(s) or file(s) did not load; retried later", fetched=len(recs))
            else:
                hub.complete([key], "done", fetched=len(recs))
        hub.close()
    except BaseException as e:
        hub.close("failed", f"{type(e).__name__}: {e}"[:200])
        raise


if __name__ == "__main__":
    if sys.argv[1:2] == ["--local"]:
        local(sys.argv[2] if len(sys.argv) > 2 else "circulars", sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        main()
