"""Self-checks for the pure helpers and pagination. No network. Run: python test_fetcher.py"""
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import fetcher
from fetcher import CFG


class Resp:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class FakeSess:
    """Serves 3 pages of 2 TLs each."""

    def __init__(self):
        self.pages = []

    def post(self, url, headers, json, timeout):
        p = json["pagination"]["page"]
        self.pages.append(p)
        rows = [{"displayId": f"PL{p}{i}"} for i in range(2)]
        return Resp({"data": {"tlDetails": rows}, "pagination": {"totalPages": 3}})


def test_window():
    s, e = fetcher.window()
    assert s.endswith("T00:00:00.000+05:30"), s
    assert e.endswith("T23:59:59.999+05:30"), e
    span = datetime.fromisoformat(e) - datetime.fromisoformat(s)
    assert span == timedelta(days=CFG["days_back"] + CFG["days_forward"] + 1) - timedelta(milliseconds=1), span


def test_list_body():
    b = fetcher.list_body("S", "E", 2)
    assert b["pagination"] == {"page": 2, "pageSize": CFG["page_size"]}
    assert b["tableFilters"]["status"] == CFG["status"]
    assert b["startDate"] == "S" and b["endDate"] == "E"
    assert b["sorting"]["sortingField"] == "externalPicklistId"  # unique key, see list_body


def test_list_walks_all_pages():
    fake = FakeSess()
    fetcher.sess = lambda: fake
    items = fetcher.list_tls({"headers": {}, "cookie": ""})
    assert fake.pages == [1, 2, 3], fake.pages
    assert len(items) == 6


def test_flatten():
    d = {"displayId": "PL9", "area": {"value": "44"},
         "containerDetails": [{"id": 5, "type": "BOX", "label": "B1"}, {"id": 6, "type": "TOTE", "label": "T1"}],
         "picklistItemGroups": [{"picklistItems": [
             {"status": "COMPLETED", "destinationLocationId": 5, "ean": "1,2", "fsn": "F1", "wid": "W1",
              "productTitle": "Title 1", "sourceLocation": {"label": "L1"}},
             {"status": "COMPLETED", "destinationLocationId": 5, "fsn": "F4",  # no ean, WID via itemIdentifier
              "itemIdentifier": {"type": "WID", "value": "W7"}, "attributes": {"productTitle": "Title 4"},
              "sourceLocation": {"label": "L2"}},
             {"status": "ITEM_NOT_FOUND", "destinationLocationId": 5, "fsn": "F2", "sourceLocation": None},
             {"status": "COMPLETED", "destinationLocationId": 99, "ean": "3", "fsn": "F3", "wid": "W3"},  # no box
         ]}]}
    rows = fetcher.flatten("PL9", d)
    assert rows == [
        {"tl_id": "PL9", "floor": "44", "box": "B1", "ean": "1,2", "wid": "W1", "fsn": "F1",
         "product_title": "Title 1", "source_location": "L1"},
        {"tl_id": "PL9", "floor": "44", "box": "B1", "ean": None, "wid": "W7", "fsn": "F4",
         "product_title": "Title 4", "source_location": "L2"},
    ], rows


def test_item_field_reads_attributes():
    assert fetcher.item_field({"attributes": {"fsn": "F9", "eans": "8,9"}}, "fsn") == "F9"
    assert fetcher.item_field({"attributes": {"fsn": "F9", "eans": "8,9"}}, "ean", "eans") == "8,9"
    assert fetcher.item_field({"fsn": "F1", "attributes": {"fsn": "F9"}}, "fsn") == "F1"  # top level wins


def test_merge_fsn():
    con = sqlite3.connect(":memory:")
    con.executescript(fetcher.DDL["fsn_ean_wid"])
    fetcher.merge_fsn(con, [{"fsn": "F1", "ean": "1,2", "wid": "W1", "product_title": None}])
    fetcher.merge_fsn(con, [{"fsn": "F1", "ean": "2,3", "wid": "W2", "product_title": "Title A"},
                            {"fsn": None, "ean": "9", "wid": "W9", "product_title": "X"}])
    fetcher.merge_fsn(con, [{"fsn": "F1", "ean": None, "wid": None, "product_title": "Title B"}])  # first title kept
    fetcher.merge_fsn(con, [{"fsn": "F2", "ean": None, "wid": "W5", "product_title": "Title C"}])  # no EAN: still a row
    assert con.execute("SELECT ean, wid, product_title FROM fsn_ean_wid WHERE fsn='F1'").fetchone() == \
        ("1,2,3", "W1,W2", "Title A")
    assert con.execute("SELECT ean, wid, product_title FROM fsn_ean_wid WHERE fsn='F2'").fetchone() == \
        (None, "W5", "Title C")
    assert con.execute("SELECT COUNT(*) FROM fsn_ean_wid").fetchone()[0] == 2


def test_write_items_idempotent():
    con = sqlite3.connect(":memory:")
    con.executescript(fetcher.DDL["tl_items"])
    rows = [{"tl_id": "PL1", "floor": "44", "box": "B1", "ean": None, "wid": "W1", "fsn": "F1",
             "product_title": "Title 1", "source_location": "L1"}]
    fetcher.write_items(con, "PL1", rows)
    fetcher.write_items(con, "PL1", rows)  # re-run after a crash
    assert con.execute("SELECT COUNT(*) FROM tl_items").fetchone()[0] == 1


def test_tl_items_gains_title_column():
    old = fetcher.HERE
    with tempfile.TemporaryDirectory() as tmp:
        fetcher.HERE = Path(tmp)
        try:
            pre = sqlite3.connect(Path(tmp) / CFG["db_files"]["tl_items"])  # pre-title schema
            pre.executescript("CREATE TABLE tl_items (tl_id TEXT, floor TEXT, box TEXT, ean TEXT, wid TEXT, "
                              "fsn TEXT, source_location TEXT);")
            pre.close()
            con = fetcher.open_db("tl_items")
            cols = [c[1] for c in con.execute("PRAGMA table_info(tl_items)")]
            con.close()
        finally:
            fetcher.HERE = old
    assert cols[-1] == "product_title", cols


def test_one_db_file_per_table():
    old = fetcher.HERE
    with tempfile.TemporaryDirectory() as tmp:
        fetcher.HERE = Path(tmp)
        try:
            for table in fetcher.DDL:
                fetcher.open_db(table).close()
            made = sorted(p.name for p in Path(tmp).iterdir())
        finally:
            fetcher.HERE = old
    assert made == sorted(CFG["db_files"].values()), made


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
