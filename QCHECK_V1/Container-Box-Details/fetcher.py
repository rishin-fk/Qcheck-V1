"""Flo Lite NCOB container + box details fetcher.

list:  POST {api_base}/containers/details                  filters + date window, all pages walked
box:   POST {api_base}/containers/details/label/{label}    body {"externalPicklistId": ...}
run:   list -> box details -> boxes.db (one row per box) + box_items.db (one row per item)
Login: username + password saved in Windows Credential Manager (asked once). Cookies + proxy headers cached in auth.json.
Usage:
    python fetcher.py login  save or change the Flo username/password
    python fetcher.py run    every interval_min, Ctrl+C to stop
    python fetcher.py box <BOX_LABEL> <EXTERNAL_PICKLIST_ID>    raw JSON to out/, for inspection
"""
import getpass
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import keyring
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HERE = Path(__file__).parent
CFG = json.loads((HERE / "config.json").read_text())
AUTH_FILE = HERE / "auth.json"
KR = "Container-Box-Details"  # service name in Windows Credential Manager
OUT = HERE / "out"
IST = timezone(timedelta(hours=5, minutes=30))
KEEP = ("csrf-token", "x-client-id", "x-facility-id", "x-proxy-user", "x-tenant-id", "origin", "referer")
TO = (10, 60)  # (connect, read) seconds
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")  # use with fullmatch: labels/ids go into URLs and filenames

S = requests.Session()
_retry = HTTPAdapter(max_retries=Retry(total=4, connect=4, read=2, backoff_factor=1.5,
                                       status_forcelist=(429, 502, 503, 504), allowed_methods=None))
S.mount("http://", _retry)
S.mount("https://", _retry)


def hdrs(a):
    t = str(int(time.time() * 1000))
    return {**a["headers"], "Cookie": a["cookie"], "content-type": "application/json", "Accept": "*/*",
            "x-event-time": t, "x-trace-id": t}


def creds(change=False):
    """Flo login from Windows Credential Manager. Prompts and saves only if nothing is stored, or change=True."""
    u, p = keyring.get_password(KR, "username"), keyring.get_password(KR, "password")
    if u and p and not change:
        return u, p
    u = input("Flo username: ")
    p = getpass.getpass("Flo password: ")
    keyring.set_password(KR, "username", u)
    keyring.set_password(KR, "password", p)
    return u, p


def login(page, user, pw):
    page.goto(CFG["page_url"])
    pwd = page.locator("input[type=password]")
    try:
        pwd.first.wait_for(state="visible", timeout=5000)
    except PWTimeout:
        return  # already logged in
    page.locator("input[type=text], input[type=email], input:not([type])").first.fill(user)
    pwd.first.fill(pw)
    page.locator("button[type=submit], button:has-text('Login'), button:has-text('Sign in')").first.click()
    pwd.first.wait_for(state="hidden", timeout=CFG["timeout_ms"])
    if CFG["page_url"] not in page.url:
        page.goto(CFG["page_url"])


def refresh_auth():
    """Browser login, then capture cookies + proxy headers from the page's own proxy calls."""
    print("refreshing auth via browser...")
    user, pw = creds()  # ask before the browser opens
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
        page.set_default_timeout(CFG["timeout_ms"])
        page.on("request", lambda r: cap.update(r.headers) if "/outbound-picking-proxy/" in r.url and not cap else None)
        try:
            login(page, user, pw)
        except PWTimeout:  # password form never went away: saved login is wrong or changed
            raise RuntimeError("login failed: run 'python fetcher.py login' to update saved credentials")
        page.wait_for_timeout(3000)  # page fires its own proxy calls on load
        cookie = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies())
        br.close()
    if not cap:
        raise RuntimeError("no outbound-picking-proxy request captured")
    a = {"cookie": cookie, "headers": {k: v for k, v in cap.items() if k in KEEP}}
    AUTH_FILE.write_text(json.dumps(a))
    return a


def load_auth():
    return json.loads(AUTH_FILE.read_text()) if AUTH_FILE.exists() else refresh_auth()


def call(a, path, body):
    r = S.post(f"{CFG['api_base']}/{path}", headers=hdrs(a), json=body, timeout=TO)
    if r.status_code in (401, 403):
        raise PermissionError(r.status_code)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:  # login page came back instead of JSON: session expired
        raise PermissionError("non-JSON response")


_last_refresh = 0.0


def fetch(path, body):
    global _last_refresh
    try:
        return call(load_auth(), path, body)
    except PermissionError:  # session expired: re-login, at most once per 5 min so one bad box can't open a browser per call
        if time.time() - _last_refresh < 300:
            raise
        _last_refresh = time.time()
        return call(refresh_auth(), path, body)


def containers_body(page=1):
    """Same window the page sends: today - days_back .. today + days_forward (IST), 'YYYY-MM-DD HH:MM:SS'."""
    today = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=CFG["days_back"])
    end = today + timedelta(days=CFG["days_forward"], hours=23, minutes=59, seconds=59)
    fmt = "%Y-%m-%d %H:%M:%S"
    return {"subflow": CFG["subflow"], "flow": CFG["flow"],
            "startDate": start.strftime(fmt), "endDate": end.strftime(fmt),
            "rangeColumn": CFG["range_column"],
            "pagination": {"page": page, "pageSize": CFG["page_size"]},
            "sorting": CFG["sorting"], "tableFilters": CFG["table_filters"]}


def box_details(label, picklist_id):
    if not SAFE_ID.fullmatch(label):  # label comes from the list response and goes into the URL path
        raise ValueError(f"bad boxLabel: {label!r}")
    return fetch(f"containers/details/label/{label}", {"externalPicklistId": picklist_id})


def list_boxes():
    """Walk every page of the container list. Returns one row per boxLabel."""
    out, page = {}, 1
    while True:
        body = fetch("containers/details", containers_body(page))
        for row in body["data"]:
            out.setdefault(row["boxLabel"], row)
        if not body["data"] or page >= body["pagination"]["totalPages"]:
            break
        page += 1
    total = body["pagination"]["totalItems"]
    if len(out) != total:  # pages overlapped or rows were missed; sort key must be unique
        print(f"WARNING: list has {total} rows but {len(out)} unique boxLabels")
    return list(out.values())


def box_rows(d):
    """Split one box-details response into the box row and its item rows."""
    label = d["boxLabel"]
    box = {"box_label": label, "assigned_to": d.get("assignedTo"), "consignment_id": d.get("consignmentId"),
           "destination": d.get("destination"), "dispatch_by_date": d.get("dispatchByDate"),
           "external_picklist_id": d.get("externalPicklistId"), "plan_type": d.get("planType")}
    # fsn lives in picklistItems[].attributes.fsn (top-level as fallback); WID is itemIdentifier.value
    items = [{"box_label": label, "fsn": (it.get("attributes") or {}).get("fsn") or it.get("fsn"),
              "wid": (it.get("itemIdentifier") or {}).get("value"), "plan_type": d.get("planType")}
             for it in d.get("picklistItems") or []]
    return box, items


def connect_dbs(boxes_file, items_file):
    boxes = sqlite3.connect(boxes_file)
    boxes.execute("CREATE TABLE IF NOT EXISTS boxes (box_label TEXT PRIMARY KEY, assigned_to TEXT, "
                  "consignment_id TEXT, destination TEXT, dispatch_by_date TEXT, "
                  "external_picklist_id TEXT, plan_type TEXT)")
    items = sqlite3.connect(items_file)
    items.execute("CREATE TABLE IF NOT EXISTS box_items (box_label TEXT, fsn TEXT, wid TEXT, plan_type TEXT)")
    items.execute("CREATE INDEX IF NOT EXISTS ix_box_items_label ON box_items (box_label)")
    return boxes, items


def store(boxes_db, items_db, box, item_rows):
    with items_db:  # replace this box's items, so a re-run after a crash never duplicates them
        items_db.execute("DELETE FROM box_items WHERE box_label = ?", (box["box_label"],))
        items_db.executemany("INSERT INTO box_items (box_label, fsn, wid, plan_type) "
                             "VALUES (:box_label, :fsn, :wid, :plan_type)", item_rows)
    with boxes_db:  # written last: a box counts as done only once its items are in
        boxes_db.execute("INSERT OR REPLACE INTO boxes (box_label, assigned_to, consignment_id, destination, "
                         "dispatch_by_date, external_picklist_id, plan_type) VALUES (:box_label, "
                         ":assigned_to, :consignment_id, :destination, :dispatch_by_date, "
                         ":external_picklist_id, :plan_type)", box)


def run():
    boxes_db, items_db = connect_dbs(HERE / CFG["boxes_db"], HERE / CFG["items_db"])
    try:
        rows = list_boxes()
        done = {r[0] for r in boxes_db.execute("SELECT box_label FROM boxes")}
        todo = [r for r in rows if r["boxLabel"] not in done]  # stored boxes are skipped
        print(f"{len(rows)} boxes in window, {len(todo)} not yet stored")
        failed = 0
        # ponytail: sequential, one call per box. Add a thread pool like TL_NCOB if a run is too slow.
        for n, r in enumerate(todo, 1):
            try:
                store(boxes_db, items_db, *box_rows(box_details(r["boxLabel"], r["externalPicklistId"])))
            except Exception as e:
                failed += 1
                print(f"FAIL {r['boxLabel']}: {str(e)[:150]}")  # not stored, so retried next run
                continue
            if n % 25 == 0:
                print(f"  {n}/{len(todo)} done")
        print(f"Done. {len(todo) - failed} stored, {failed} failed (retried next run).")
    finally:
        boxes_db.close()
        items_db.close()


def loop():
    """Run forever. A failed pass is logged and retried on the next tick; stored boxes are skipped."""
    while True:
        print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S}] pass start")
        try:
            run()
        except Exception as e:
            print(f"run failed: {e}")
        print(f"sleeping {CFG['interval_min']} min...")
        time.sleep(CFG["interval_min"] * 60)


def save(name, data):
    OUT.mkdir(exist_ok=True)
    path = OUT / f"{name}_{datetime.now(IST):%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["login"]:
        creds(change=True)
        print("saved to Windows Credential Manager")
    elif args == ["run"]:
        creds()  # ask once up front if nothing is saved; the loop can't stop to prompt later
        loop()
    elif len(args) == 3 and args[0] == "box" and all(SAFE_ID.fullmatch(x) for x in args[1:]):
        print("saved", save(f"box_{args[1]}", box_details(args[1], args[2])))
    else:
        sys.exit(__doc__)
