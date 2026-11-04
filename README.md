# pwd-delhi

This family of the s1d-labs scrape pool collects the public data of the Public Works Department (PWD), Government of NCT of
Delhi, from <https://www.pwddelhi.gov.in>. The scraper reads the JSON APIs that the pages of the site use, and the pages of the
sitemap.

## Data sets

| Hub project | Source | Record | Rows on 2026-11-04 |
|---|---|---|---|
| `pwd-delhi-projects` | `/api/projects/search` | one work or project (cost, tender, contractor, progress, dates) | 13,139 |
| `pwd-delhi-project-details` | `/api/projects/<id>` and its progress, location, inspection and photo calls | one project: status history, monthly progress, tender details, counts | 13,139 |
| `pwd-delhi-circulars` | `/circular/api/search` | one circular or office order, with its file | 3,582 |
| `pwd-delhi-court-cases` | `/api/litigation/court-cases/search` | one court case (active and archived) | 818 + 1,331 |
| `pwd-delhi-arbitration` | `/api/litigation/arbitration-cases/search` | one arbitration case | 390 |
| `pwd-delhi-contractors` | `/pms/api/contractors` | one contractor in the register | 9,014 |
| `pwd-delhi-buildings` | `/api/maintenance/buildings/search` | one building | 1,011 |
| `pwd-delhi-flyovers` | `/api/maintenance/flyovers/search` | one flyover | 78 |
| `pwd-delhi-fobs` | `/api/maintenance/fobs/search` | one foot-over-bridge | 15 |
| `pwd-delhi-subways` | `/api/maintenance/subways/search` | one subway | 62 |
| `pwd-delhi-roads` | `/api/maintenance/roads/search` and `/api/maintenance/roads/<id>` | one road, with its details | 1,297 + 18 unnamed |
| `pwd-delhi-street-lights` | `/street-light-inspection/api/data?date=` | one division on one day | about 3 per day |
| `pwd-delhi-offices` | `/api/nodal-officers`, `/pms/api/users-tree` | one nodal office, or one office with its officers | 363 + 141 |
| `pwd-delhi-pages` | the 170 pages of `sitemap.xml` (English and Hindi) | one page, one table row, or one linked document | 170 pages |

- Each queue item is one API page (100 rows, or 25 projects for the project details), one day, or one site page.
- The record id is the id that the site gives to the row.
- Each record is flat. Numbers are numbers, dates are `YYYY-MM-DD`, and flags are `true` or `false`.
- Amounts are the numbers that the API gives. The site shows them divided by 100,000, as lakh.
- In `pwd-delhi-pages`, the field `record_type` tells the kind of record: `page`, `table_row` or `document`.

The site has no data for these parts on 2026-11-04:

- The list of arbitrators, the list of consultants and the drain desilting page show "No data found".
- The sanction search has no API. Its script says that the backend is not ready.
- The complaints system (PWD Sewa) and the administration pages need a login. The scraper does not use them.

## Files in the bucket

- `<project>/pages/<key>.json.gz` or `.html.gz`: the API answer or the page for each queue item.
- `pwd-delhi-circulars/files/<id>.<ext>`: the file of each circular.
- `pwd-delhi-pages/docs/<doc id>.<ext>`: each document that a page links. The record of the document gives its URL.
- `pwd-delhi-project-details/files/<project id>/<n>.<ext>`: the photos of a project, if the site has photos for it.

## Contact data

The records and the raw files do not keep phone numbers, e-mail addresses, fax numbers, PAN, bank details or login names. The records
also do not keep the private addresses of parties in court cases. The function `scrub()` in `parsers.py` removes them.
The records keep the names of firms, courts, arbitrators, advocates and officers.

## Access from India only

The site refuses foreign IP addresses. On 2026-11-04 it also refused the team proxies in the range `191.219.x`.
It accepted only the proxies in the range `95.134.119.x` (in-08 to in-10). For this reason, each project uses
`proxy="IN"` and `proxy_only=["95.134.119."]`. Files on S3 and the uploads go direct, not through the proxy.

## Speed

- Each worker thread waits 1 second after each request to the site.
- Each worker uses 2 threads, or 4 threads for the circulars and the pages, where most of the work is file downloads from S3.
- Not more than 3 workers run at the same time, because only 3 proxies get into the site.

## Commands

Run these commands in a team box:

```
python -m pytest tests                                               # parsing tests on saved API answers and pages
myproxy run python crawl.py --local project-details 1                # one queue item, no hub (3 rows that need more calls)
myproxy run scrape-check --items 2 --project pwd-delhi-buildings      # setup and task against a local test hub
```

- If `myproxy` gives a proxy in the range `191.219.x`, the site answers 403. Do `myproxy release` and try again.
- The **check** workflow on GitHub cannot reach the site. Its parsing tests are valid. Its scrape-check step can fail.

## A new full pass

1. In `family.py`, set `CRAWL` to a new tag, for example `"2026-12"`.
2. Set `FILES_SINCE` to the date of the last pass. The scraper then skips older circulars and their files.
3. If the site has more rows, increase the numbers in `TOTALS`.
4. Push the change, then run the **setup** workflow. Setup also adds the street-light days since the last run.
