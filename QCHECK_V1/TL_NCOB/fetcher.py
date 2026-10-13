"""Completed NCOB TL (picklist) fetcher for Flo Lite.

List:   POST {api_base}/pendency     paged, status filter, dispatch-by-date window
Detail: GET  {api_base}/{displayId}  all items inside the TL
Stores: one SQLite file per table (names in config.json):
        tl_items.db (one row per completed item in a box), FSN_EAN_WID_Title.db (one row per FSN),
        fetched_tls.db (TL IDs already processed, skipped on re-runs).
Login:  env vars FLO_USERNAME / FLO_PASSWORD. Cookies + headers cached in auth.json.
"""
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HERE = Path(__file__).parent
CFG = json.loads((HERE / "config.json").read_text())
AUTH_FILE = HERE / "auth.json"
IST = timezone(timedelta(hours=5, minutes=30))
KEEP = ("csrf-token", "x-client-id", "x-facility-id", "x-proxy-user", "x-tenant-id", "origin", "referer")
TO = (10, 60)  # (connect, read) seconds
T = CFG["timeout_ms"]
_tl = threading.local()

ITEM_SQL = ("INSERT INTO tl_items (tl_id, floor, box, ean, wid, fsn, source_location, product_title) "
            "VALUES (:tl_id, :floor, :box, :ean, :wid, :fsn, :source_location, :product_title)")
DDL = {  # one table per file
    "tl_items": "CREATE TABLE IF NOT EXISTS tl_items (tl_id TEXT, floor TEXT, box TEXT, ean TEXT, wid TEXT, "
                "fsn TEXT, source_location TEXT, product_title TEXT); CREATE INDEX IF NOT EXISTS ix_tl_items_tl ON tl_items (tl_id);",
    "fsn_ean_wid": "CREATE TABLE IF NOT EXISTS fsn_ean_wid (fsn TEXT PRIMARY KEY, ean TEXT, wid TEXT, product_title TEXT);",
    "fetched_tls": "CREATE TABLE IF NOT EXISTS fetched_tls (tl_id TEXT PRIMARY KEY);",
}


def sess():
    """One keep-alive session per thread, with retry/backoff on connect errors and 429/5xx."""
    s = getattr(_tl, "s", None)
    if s is None:
        s = requests.Session()
        ad = HTTPAdapter(max_retries=Retry(total=4, connect=4, read=2, backoff_factor=1.5,
                                           status_forcelist=(429, 502, 503, 504), allowed_methods=None))
        s.mount("http://", ad)
        s.mount("https://", ad)
        _tl.s = s
    return s


def hdrs(a):
    t = str(int(time.time() * 1000))
    return {**a["headers"], "Cookie": a["cookie"], "content-type": "application/json", "Accept": "*/*",
            "x-event-time": t, "x-trace-id": t}


def login(page):
    page.goto(CFG["page_url"])
    pwd = page.locator("input[type=password]")
    try:
        pwd.first.wait_for(state="visible", timeout=5000)
    except PWTimeout:
        return  # already logged in
    page.locator("input[type=text], input[type=email], input:not([type])").first.fill(os.environ["FLO_USERNAME"])
    pwd.first.fill(os.environ["FLO_PASSWORD"])
    page.locator("button[type=submit], button:has-text('Login'), button:has-text('Sign in')").first.click()
    pwd.first.wait_for(state="hidden", timeout=T)
    if CFG["page_url"] not in page.url:
        page.goto(CFG["page_url"])


def refresh_auth():
    """Log in with the browser, capture cookies + proxy headers from the page's own list call."""
    print("refreshing auth via browser...")
    cap = {}
    with sync_playwright() as p:
        br = None
        for ch in (CFG["browser_channel"], "msedge"):
            try:
                br = p.chromium.launch(channel=ch, headless=CFG["headless"])
                break
            except Exception:
                pass
        if br is None:
            raise SystemExit("Chrome/Edge not found")
        ctx = br.new_context(viewport={"width": 1900, "height": 950})
        page = ctx.new_page()
        page.set_default_timeout(T)
        page.on("request", lambda r: cap.update(r.headers) if "/outbound-picking-proxy/" in r.url and not cap else None)
        login(page)
        page.wait_for_timeout(3000)  # page fires its own pendency call on load
        cookie = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies())
        br.close()
    if not cap:
        raise RuntimeError("no outbound-picking-proxy request captured")
    a = {"cookie": cookie, "headers": {k: v for k, v in cap.items() if k in KEEP}}
    AUTH_FILE.write_text(json.dumps(a))
    return a


def load_auth():
    if AUTH_FILE.exists():
        return json.loads(AUTH_FILE.read_text())
    return refresh_auth()


def window():
    """Dispatch-by-date window in IST, formatted the way the page sends it."""
    today = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=CFG["days_back"])
    end = today + timedelta(days=CFG["days_forward"], hours=23, minutes=59, seconds=59, milliseconds=999)
    return start.isoformat(timespec="milliseconds"), end.isoformat(timespec="milliseconds")


def list_body(start, end, page):
    # Unique sort key. breachingDate has many ties, and the server reshuffled them between pages.
    return {"flow": CFG["flow"], "flowType": CFG["flow_type"], "startDate": start, "endDate": end,
            "rangeColumn": CFG["range_column"],
            "pagination": {"page": page, "pageSize": CFG["page_size"]},
            "sorting": {"sortAsc": True, "sortingField": "externalPicklistId"},
            "tableFilters": {"destination": [], "assignedTo": [], "status": CFG["status"], "area": [],
                             "pickzone": [], "planType": [], "bucket": [], "pickableType": [],
                             "orderId": None, "picklistId": None}}


def list_tls(a):
    """Walk every page of the TL list. Returns {displayId: list_row}."""
    start, end = window()
    out, page, seen = {}, 1, 0
    while True:
        r = sess().post(f"{CFG['api_base']}/pendency", headers=hdrs(a),
                        json=list_body(start, end, page), timeout=TO)
        if r.status_code in (401, 403):
            raise PermissionError(r.status_code)
        r.raise_for_status()
        try:
            body = r.json()
        except ValueError:  # HTML (login page) instead of JSON means the session expired
            raise PermissionError("non-JSON response")
        rows = body["data"]["tlDetails"]
        seen += len(rows)
        for row in rows:
            out[row["displayId"]] = row
        total_pages = body["pagination"]["totalPages"]
        if not rows or page >= total_pages:
            print(f"list: {seen} rows on {page} pages, {len(out)} unique TLs")
            return out
        page += 1


def get_detail(a, tl_id):
    try:
        r = sess().get(f"{CFG['api_base']}/{tl_id}", headers=hdrs(a), timeout=TO)
        r.raise_for_status()
        return tl_id, r.json(), None
    except Exception as e:
        return tl_id, None, str(e)[:150]


def item_field(it, *keys):
    """Item's own key first, then its attributes block. The API puts some fields in either place."""
    attrs = it.get("attributes") or {}
    for k in keys:
        for src in (it, attrs):
            if src.get(k):
                return src[k]
    return None


def wid_of(it):
    """WID is the item's own wid, else its itemIdentifier when that is of type WID."""
    ident = it.get("itemIdentifier") or {}
    return item_field(it, "wid") or (ident.get("value") if ident.get("type") == "WID" else None)


def flatten(tl_id, d):
    """Rows for tl_items: COMPLETED items that sit in a BOX. Everything else is dropped."""
    floor = (d.get("area") or {}).get("value")
    boxes = {c["id"]: c["label"] for c in d.get("containerDetails") or [] if c.get("type") == "BOX"}
    rows = []
    for g in d.get("picklistItemGroups") or []:
        for it in g.get("picklistItems") or []:
            box = boxes.get(it.get("destinationLocationId"))
            if it.get("status") == "COMPLETED" and box is not None:
                rows.append({"tl_id": tl_id, "floor": floor, "box": box,
                             "ean": item_field(it, "ean", "eans"), "wid": wid_of(it),
                             "fsn": item_field(it, "fsn"), "product_title": item_field(it, "productTitle"),
                             "source_location": (it.get("sourceLocation") or {}).get("label")})
    return rows


def _split(s):
    return [x.strip() for x in s.split(",") if x.strip()] if s else []


def merge_fsn(con, rows):
    """fsn_ean_wid: one row per FSN. New EANs and WIDs are appended; old ones stay. Title is unique per FSN, so the
    first non-empty one is kept. A new FSN always gets a row."""
    for r in rows:
        if not r["fsn"]:
            continue
        got = con.execute("SELECT ean, wid, product_title FROM fsn_ean_wid WHERE fsn=?", (r["fsn"],)).fetchone()
        old_ean, old_wid, old_title = (_split(got[0]), _split(got[1]), got[2]) if got else ([], [], None)
        ean = list(dict.fromkeys(old_ean + _split(r["ean"])))  # keeps order, drops duplicates
        wid = list(dict.fromkeys(old_wid + _split(r["wid"])))
        title = old_title or r["product_title"]  # ponytail: first title wins; a renamed product keeps its old title
        if got is None or ean != old_ean or wid != old_wid or title != old_title:
            con.execute("INSERT OR REPLACE INTO fsn_ean_wid (fsn, ean, wid, product_title) VALUES (?,?,?,?)",
                        (r["fsn"], ",".join(ean) or None, ",".join(wid) or None, title))


def open_db(table):
    """Connection to this table's own SQLite file, with the table created."""
    con = sqlite3.connect(HERE / CFG["db_files"][table])
    con.executescript(DDL[table])
    if table == "tl_items" and "product_title" not in {c[1] for c in con.execute("PRAGMA table_info(tl_items)")}:
        con.execute("ALTER TABLE tl_items ADD COLUMN product_title TEXT")  # tl_items.db made before titles existed
    return con


def write_items(con, tl_id, rows):
    """Replace this TL's rows, so a re-run after a crash can't leave duplicates."""
    con.execute("DELETE FROM tl_items WHERE tl_id=?", (tl_id,))
    con.executemany(ITEM_SQL, rows)


def commit(items_db, fsn_db, done_db):
    """Items, then FSN, then the done-marker. A marker on disk implies its rows are on disk too."""
    items_db.commit()
    fsn_db.commit()
    done_db.commit()


def run():
    items_db, fsn_db, done_db = open_db("tl_items"), open_db("fsn_ean_wid"), open_db("fetched_tls")
    failed = pending = 0
    try:
        a = load_auth()
        try:
            items = list_tls(a)
        except PermissionError:
            a = refresh_auth()
            items = list_tls(a)
        done = {r[0] for r in done_db.execute("SELECT tl_id FROM fetched_tls")}
        todo = [t for t in items if t not in done]  # processed TLs are never fetched again
        print(f"{len(items)} completed TLs in window, {len(todo)} not yet processed")
        with ThreadPoolExecutor(CFG["workers"]) as ex:
            for n, (tl_id, data, err) in enumerate(ex.map(lambda t: get_detail(a, t), todo), 1):
                if err:
                    failed += 1
                    print(f"FAIL {tl_id}: {err}")  # not marked done, so retried next run
                    continue
                try:
                    rows = flatten(tl_id, data)
                except Exception as e:
                    failed += 1
                    print(f"FAIL {tl_id}: unexpected detail shape: {e}")
                    continue
                if any(r["fsn"] is None or r["wid"] is None for r in rows):  # EAN may be legitimately empty
                    print(f"WARN {tl_id}: some items have no fsn or wid")
                write_items(items_db, tl_id, rows)
                merge_fsn(fsn_db, rows)
                done_db.execute("INSERT OR IGNORE INTO fetched_tls VALUES (?)", (tl_id,))
                pending += 1
                if pending == 25:
                    commit(items_db, fsn_db, done_db)
                    pending = 0
                    print(f"  {n}/{len(todo)} done")
    finally:
        commit(items_db, fsn_db, done_db)  # keep progress even if interrupted
        for db in (items_db, fsn_db, done_db):
            db.close()
    print(f"Done. {len(todo) - failed} TLs processed, {failed} failed (retried next run).")


if __name__ == "__main__":
    while True:
        try:
            run()
        except Exception as e:
            print(f"run failed: {e}")
        print("sleeping 10 min...")
        time.sleep(600)
