import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import create_app
from build_source import build

logging.getLogger("qc").addHandler(logging.NullHandler())

# Box B1: F1 has 2 units (WIDs W1, W2, each unique), F2 has 1 unit (W3),
# F3 has 3 units that all share WID W4. F1 has EANs 111 and 112, F3 has EAN 333, F2 has none.
# Box B2 has a unit with no FSN. Box B3 is a single-unit box.
ALL_GOOD = ["111", "W2", "W3", "333", "333", "W4"]


def make_source(path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE box_units (box_label TEXT NOT NULL, fsn TEXT, wid TEXT, plan_type TEXT);
        CREATE TABLE fsn_codes (fsn TEXT NOT NULL, ean TEXT NOT NULL, PRIMARY KEY (fsn, ean));
    """)
    con.executemany("INSERT INTO box_units VALUES (?, ?, ?, ?)", [
        ("B1", "F1", "W1", "PBD"), ("B1", "F1", "W2", "PBD"), ("B1", "F2", "W3", "PBD"),
        ("B1", "F3", "W4", "PBD"), ("B1", "F3", "W4", "PBD"), ("B1", "F3", "W4", "PBD"),
        ("B2", None, "W9", "PBD"),
        ("B3", "F9", "W9", "PBD"),
    ])
    con.executemany("INSERT INTO fsn_codes VALUES (?, ?)", [("F1", "111"), ("F1", "112"), ("F3", "333")])
    con.commit()
    con.close()


def audit(scans, request_id="audit-req-0000000001", box="B1", table="01", casper="Casper A"):
    return {"requestId": request_id, "boxId": box, "tableNo": table, "casperId": casper, "scans": scans}


def packing(request_id, remark, box="B1", table="01", casper="Casper A", audit_id=None):
    body = {"requestId": request_id, "boxId": box, "packingSlip": box, "tableNo": table,
            "qcDoneBy": casper, "remark": remark}
    if audit_id is not None:
        body["auditId"] = audit_id
    return body


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qc-audit-tests-")
        self.folder = Path(self.temp.name)
        self.source = self.folder / "qc_source.db"
        make_source(self.source)
        self.app = create_app({"DATA_DIR": self.folder, "TESTING": True, "SOURCE_DB": self.source}, start_worker=False)
        self.runtime = self.app.extensions["qc"]
        self.client = self.app.test_client()

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def test_box_groups_and_errors(self):
        r = self.client.get("/api/box/B1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["total_units"], 6)
        groups = {g["fsn"]: g for g in r.json["groups"]}
        self.assertEqual(groups["F1"]["expected"], 2)
        self.assertEqual(groups["F1"]["eans"], ["111", "112"])
        self.assertEqual(groups["F2"]["eans"], [])
        self.assertEqual(groups["F3"]["wids"], [{"wid": "W4", "units": 3}])
        self.assertEqual(self.client.get("/api/box/NOPE").status_code, 404)
        self.assertEqual(self.client.get("/api/box/bad%20id").status_code, 400)
        self.assertEqual(self.client.get("/api/box/B2").status_code, 422)  # unit with no FSN

    def test_source_missing_returns_503(self):
        other = create_app({"DATA_DIR": self.folder / "other", "TESTING": True,
                            "SOURCE_DB": self.folder / "missing.db"}, start_worker=False)
        try:
            r = other.test_client().get("/api/box/B1")
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.json["code"], "SOURCE_UNAVAILABLE")
        finally:
            other.extensions["qc"].close()

    def test_matching_scans_are_accepted(self):
        r = self.client.post("/api/qc_audit", json=audit(ALL_GOOD))
        self.assertEqual(r.status_code, 201, r.json)
        self.assertEqual(r.json["tableNo"], "01")
        self.assertEqual(r.json["casperId"], "Casper A")
        self.assertIn("submittedAt", r.json)

    def test_multi_unit_wid_repeats_are_counted(self):
        r = self.client.post("/api/qc_audit", json=audit(["111", "W2", "W3", "W4", "W4", "W4"], "audit-req-0000000002"))
        self.assertEqual(r.status_code, 201, r.json)

    def test_repeat_single_unit_wid_is_rejected(self):
        r = self.client.post("/api/qc_audit", json=audit(["W1", "W1", "111", "W2", "W3", "333", "333", "W4"], "audit-req-0000000003"))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json["code"], "AUDIT_MISMATCH")
        self.assertTrue(any("already scanned" in e for e in r.json["errors"]), r.json)

    def test_repeat_wid_after_fsn_complete_is_alien(self):
        r = self.client.post("/api/qc_audit", json=audit(["W3", "W3"], "audit-req-0000000010"))
        self.assertEqual(r.status_code, 400)
        self.assertTrue(any(e.startswith("Scan 2:") and "extra unit" in e for e in r.json["errors"]), r.json)

    def test_alien_item_is_rejected(self):
        r = self.client.post("/api/qc_audit", json=audit(ALL_GOOD + ["999"], "audit-req-0000000004"))
        self.assertEqual(r.status_code, 400)
        self.assertTrue(any("ALIEN" in e for e in r.json["errors"]), r.json)

    def test_count_mismatch_is_rejected(self):
        r = self.client.post("/api/qc_audit", json=audit(["111", "W2", "W3", "333", "W4"], "audit-req-0000000005"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("FSN F3: scanned 2 of 3.", r.json["errors"])

    def test_duplicate_request_returns_same_audit_and_conflicts_on_change(self):
        first = self.client.post("/api/qc_audit", json=audit(ALL_GOOD))
        again = self.client.post("/api/qc_audit", json=audit(ALL_GOOD))
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(first.json["auditId"], again.json["auditId"])
        # Different but still valid scans (F1 via W1 and 112, F3 via one EAN and two WIDs).
        changed = self.client.post("/api/qc_audit", json=audit(["112", "W1", "W3", "333", "W4", "W4"]))
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.json["code"], "SUBMISSION_CONFLICT")

    def test_packing_slip_rules_and_audit_link(self):
        client = self.client
        bypass = packing("pack-bypass-0000001", "Bypass")
        self.assertEqual(client.post("/api/packing_slip", json=bypass).status_code, 201)

        done_without_audit = client.post("/api/packing_slip", json=packing("pack-done-noaudit-1", "Done"))
        self.assertEqual(done_without_audit.status_code, 400)
        self.assertEqual(done_without_audit.json["code"], "AUDIT_REQUIRED")

        bypass_with_audit = client.post("/api/packing_slip", json=packing("pack-bypass-0000002", "Bypass", audit_id=1))
        self.assertEqual(bypass_with_audit.status_code, 400)
        self.assertEqual(bypass_with_audit.json["code"], "BYPASS_WITH_AUDIT")

        a = client.post("/api/qc_audit", json=audit(ALL_GOOD)).json["auditId"]
        mismatch = client.post("/api/packing_slip", json=packing("pack-done-mismatch1", "Done", table="99", audit_id=a))
        self.assertEqual(mismatch.status_code, 400)
        self.assertEqual(mismatch.json["code"], "AUDIT_MISMATCH")

        linked = client.post("/api/packing_slip", json=packing("pack-done-000000001", "Done", audit_id=a))
        self.assertEqual(linked.status_code, 201, linked.json)
        with self.runtime.store.connect() as db:
            entry = db.execute("SELECT entry_id FROM qc_audit WHERE id=?", (a,)).fetchone()[0]
        self.assertEqual(entry, linked.json["id"])

        again_other_request = client.post("/api/packing_slip", json=packing("pack-done-000000002", "Done", audit_id=a))
        self.assertEqual(again_other_request.status_code, 409)
        self.assertEqual(again_other_request.json["code"], "AUDIT_ALREADY_LINKED")

        same_request_retry = client.post("/api/packing_slip", json=packing("pack-done-000000001", "Done", audit_id=a))
        self.assertEqual(same_request_retry.status_code, 200)
        self.assertTrue(same_request_retry.json["duplicate"])

    def test_missing_audit_is_not_found(self):
        r = self.client.post("/api/packing_slip", json=packing("pack-done-nofound-1", "Done", audit_id=999))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json["code"], "AUDIT_NOT_FOUND")

    def test_extra_unit_beyond_expected_is_alien(self):
        r = self.client.post("/api/qc_audit", json=audit(["111", "W2", "W3", "333", "333", "333", "W4"], "audit-req-0000000006"))
        self.assertEqual(r.status_code, 400)
        self.assertTrue(any("extra unit" in e for e in r.json["errors"]), r.json)

    def test_box_reports_previous_audit(self):
        self.assertIsNone(self.client.get("/api/box/B1").json["previous"])
        a = self.client.post("/api/qc_audit", json=audit(ALL_GOOD, "audit-req-0000000007")).json["auditId"]
        prev = self.client.get("/api/box/B1").json["previous"]
        self.assertEqual(prev["auditId"], a)
        self.assertEqual(prev["casperId"], "Casper A")

    def test_redo_overwrites_previous_audit(self):
        first = self.client.post("/api/qc_audit", json=audit(ALL_GOOD, "audit-req-0000000008")).json["auditId"]
        second = self.client.post("/api/qc_audit", json=audit(["112", "W1", "W3", "333", "W4", "W4"], "audit-req-0000000009")).json["auditId"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.client.get("/api/box/B1").json["previous"]["auditId"], second)
        with self.runtime.store.connect() as db:
            replaced = db.execute("SELECT replaced_by FROM qc_audit WHERE id=?", (first,)).fetchone()[0]
        self.assertEqual(replaced, second)

    def test_audit_page_is_served(self):
        self.assertEqual(self.client.get("/audit").status_code, 200)
        self.assertIn(b"QC Box Audit", self.client.get("/").data)
        self.assertIn(b"QC Data Entry", self.client.get("/classic").data)


class TitleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qc-title-tests-")
        self.folder = Path(self.temp.name)
        self.items, self.fsn_db = self.folder / "box_items.db", self.folder / "FSN_EAN_WID_Title.db"
        con = sqlite3.connect(self.items)
        con.executescript("CREATE TABLE box_items (box_label TEXT, fsn TEXT, wid TEXT, plan_type TEXT);")
        con.executemany("INSERT INTO box_items VALUES (?, ?, ?, ?)",
                        [("B1", "F1", "W1", "PBD"), ("B1", "F1", "W2", "PBD"), ("B1", "F2", "W3", "PBD")])
        con.commit()
        con.close()
        con = sqlite3.connect(self.fsn_db)
        con.executescript("CREATE TABLE fsn_ean_wid (fsn TEXT PRIMARY KEY, ean TEXT, wid TEXT, product_title TEXT);")
        con.executemany("INSERT INTO fsn_ean_wid VALUES (?, ?, ?, ?)",
                        [("F1", "111,112", "W1", "Title One"), ("F2", None, "W3", None)])
        con.commit()
        con.close()
        self.source = self.folder / "qc_source.db"
        build(self.items, self.fsn_db, self.source)
        self.app = create_app({"DATA_DIR": self.folder / "data", "TESTING": True, "SOURCE_DB": self.source},
                              start_worker=False)
        self.runtime = self.app.extensions["qc"]
        self.client = self.app.test_client()

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def query(self, sql):
        db = sqlite3.connect(self.source)
        try:
            return db.execute(sql).fetchall()
        finally:
            db.close()

    def test_title_reaches_view_and_box_api(self):
        rows = self.query("SELECT box, fsn, ean, wid, product_title FROM current_box_items ORDER BY fsn, wid")
        self.assertEqual(rows, [("B1", "F1", "111,112", "W1", "Title One"),
                                ("B1", "F1", "111,112", "W2", "Title One"),
                                ("B1", "F2", None, "W3", None)])
        groups = {g["fsn"]: g for g in self.client.get("/api/box/B1").json["groups"]}
        self.assertEqual(groups["F1"]["title"], "Title One")
        self.assertIsNone(groups["F2"]["title"])

    def test_title_change_adds_version_and_keeps_history(self):
        con = sqlite3.connect(self.fsn_db)
        con.execute("UPDATE fsn_ean_wid SET product_title='Title Two' WHERE fsn='F1'")
        con.commit()
        con.close()
        build(self.items, self.fsn_db, self.source)
        self.assertEqual(self.query("SELECT product_title FROM fsn_versions WHERE fsn='F1' ORDER BY id"),
                         [("Title One",), ("Title Two",)])
        self.assertEqual(self.query("SELECT DISTINCT product_title FROM current_box_items WHERE fsn='F1'"),
                         [("Title Two",)])


if __name__ == "__main__":
    unittest.main()
