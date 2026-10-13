"""Self-checks for request building and input validation. No network. Run: python test_fetcher.py"""
from datetime import datetime

import fetcher


def test_containers_body_dates():
    b = fetcher.containers_body()
    # same format the page sends: "YYYY-MM-DD HH:MM:SS", no timezone suffix
    assert b["startDate"].endswith(" 00:00:00"), b["startDate"]
    assert b["endDate"].endswith(" 23:59:59"), b["endDate"]
    s = datetime.strptime(b["startDate"], "%Y-%m-%d %H:%M:%S")
    e = datetime.strptime(b["endDate"], "%Y-%m-%d %H:%M:%S")
    assert (e - s).days == fetcher.CFG["days_back"] + fetcher.CFG["days_forward"], (s, e)


def test_containers_body_fields():
    b = fetcher.containers_body()
    assert b["flow"] == fetcher.CFG["flow"]
    assert b["subflow"] == fetcher.CFG["subflow"]
    assert b["tableFilters"] == fetcher.CFG["table_filters"]
    assert b["pagination"] == {"page": 1, "pageSize": fetcher.CFG["page_size"]}


def test_id_validation():
    assert fetcher.SAFE_ID.fullmatch("B0VDTI2A") and fetcher.SAFE_ID.fullmatch("PL426062101")
    assert not fetcher.SAFE_ID.fullmatch("../etc")
    assert not fetcher.SAFE_ID.fullmatch("A/B")
    assert not fetcher.SAFE_ID.fullmatch("")


def test_box_request():
    seen = []
    fetcher.fetch = lambda path, body: seen.append((path, body)) or {"ok": True}
    fetcher.box_details("B0VDTI2A", "PL426062101")
    assert seen == [("containers/details/label/B0VDTI2A", {"externalPicklistId": "PL426062101"})], seen


def test_containers_body_pages_and_sort():
    assert fetcher.containers_body(3)["pagination"] == {"page": 3, "pageSize": fetcher.CFG["page_size"]}
    # every row is PACKED, so sorting by status ties and the server reorders between pages
    assert fetcher.CFG["sorting"]["sortingField"] != "status"


class FakePages:
    """3 pages; page 2 repeats B2 from page 1, as happens when sort keys tie."""

    def __init__(self):
        self.pages = []

    def __call__(self, path, body):
        p = body["pagination"]["page"]
        self.pages.append(p)
        labels = {1: ["B1", "B2"], 2: ["B2", "B3"], 3: ["B4", "B5"]}[p]
        return {"data": [{"boxLabel": b, "externalPicklistId": "PL1"} for b in labels],
                "pagination": {"totalPages": 3, "totalItems": 6}}


def test_list_walks_pages_and_dedupes():
    fake = FakePages()
    fetcher.fetch = fake
    rows = fetcher.list_boxes()
    assert fake.pages == [1, 2, 3], fake.pages
    assert [r["boxLabel"] for r in rows] == ["B1", "B2", "B3", "B4", "B5"], rows


def test_box_rows():
    d = {"boxLabel": "B0VDTI2A", "assignedTo": "ca.4074539", "consignmentId": "SN8872731962",
         "destination": "jai_107_wh_hl_01", "dispatchByDate": "2026-10-10T18:00",
         "externalPicklistId": "PL426062101", "planType": "PBD",
         "picklistItems": [{"attributes": {"fsn": "F1"}, "itemIdentifier": {"type": "WID", "value": "W1"}},
                           {"fsn": "F2"}, {}]}  # real shape, then fallback
    box, items = fetcher.box_rows(d)
    assert box == {"box_label": "B0VDTI2A", "assigned_to": "ca.4074539", "consignment_id": "SN8872731962",
                   "destination": "jai_107_wh_hl_01", "dispatch_by_date": "2026-10-10T18:00",
                   "external_picklist_id": "PL426062101", "plan_type": "PBD"}, box
    assert [i["fsn"] for i in items] == ["F1", "F2", None], items
    assert [i["wid"] for i in items] == ["W1", None, None], items
    assert all(i["plan_type"] == "PBD" and i["box_label"] == "B0VDTI2A" for i in items), items


def test_store_is_idempotent():
    boxes, items = fetcher.connect_dbs(":memory:", ":memory:")
    d = {"boxLabel": "B1", "planType": "PBD", "picklistItems": [{"fsn": "F1"}, {"fsn": "F2"}]}
    for _ in range(2):  # a re-run must not duplicate rows
        fetcher.store(boxes, items, *fetcher.box_rows(d))
    assert boxes.execute("SELECT COUNT(*) FROM boxes").fetchone()[0] == 1
    assert items.execute("SELECT COUNT(*) FROM box_items").fetchone()[0] == 2


def test_box_rejects_bad_label():
    try:
        fetcher.box_details("A/B", "PL1")
    except ValueError:
        return
    raise AssertionError("bad label accepted")


class HtmlSess:
    """Returns 200 with a login page instead of JSON, which is what an expired session looks like."""

    def post(self, *a, **k):
        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                raise ValueError("<html>login</html>")
        return R()


def test_login_page_counts_as_expired():
    fetcher.S = HtmlSess()
    try:
        fetcher.call({"headers": {}, "cookie": ""}, "x", {})
    except PermissionError:
        return
    raise AssertionError("HTML response not treated as expired")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
