import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from app import ProcessLock, backup_database, create_app
from storage import Store

logging.getLogger("qc").addHandler(logging.NullHandler())


def entry(number=1, **changes):
    data = {"boxId": f"000{number}", "packingSlip": f"000{number}", "tableNo": "01",
            "qcDoneBy": "Test operator", "remark": "Done", "requestId": f"test-submission-{number:08d}"}
    data.update(changes)
    return data


def workbook_rows(path):
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        rows = []
        for row in root.findall("x:sheetData/x:row", ns):
            values = []
            for cell in row.findall("x:c", ns):
                if cell.get("t") == "inlineStr":
                    values.append("".join(cell.itertext()))
                else:
                    values.append(cell.findtext("x:v", namespaces=ns))
            rows.append(values)
        return rows, root


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qc-tests-")
        self.folder = Path(self.temp.name)
        self.app = create_app({"DATA_DIR": self.folder, "TESTING": True}, start_worker=False)
        self.runtime = self.app.extensions["qc"]
        self.store = self.runtime.store
        self.exporter = self.runtime.exporter

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def test_page_and_validation(self):
        client = self.app.test_client()
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(client.get("/static/qc.js").status_code, 200)
        for payload in (None, [], [entry()], {}, entry(tableNo=1), entry(boxId="OTHER"), entry(remark="invalid"), entry(qcDoneBy="a\nb")):
            response = client.post("/save", json=payload)
            self.assertEqual(response.status_code, 400, payload)
            self.assertFalse(response.json["saved"])
        self.assertEqual(client.post("/save", json=entry()).status_code, 201)
        self.assertEqual(client.post("/save", json=entry(2, remark="Bypass")).status_code, 201)
        self.assertEqual(self.store.counts()["saved"], 2)

    def test_concurrent_retries_have_exactly_one_row(self):
        def send(_):
            with self.app.test_client() as client:
                return client.post("/save", json=entry()).json
        with ThreadPoolExecutor(max_workers=30) as pool:
            replies = list(pool.map(send, range(30)))
        self.assertTrue(all(r["saved"] for r in replies))
        self.assertEqual(len({r["id"] for r in replies}), 1)
        self.assertEqual(sum(not r["duplicate"] for r in replies), 1)
        self.assertEqual(self.store.counts()["saved"], 1)
        response = self.app.test_client().post("/save", json=entry(tableNo="02"))
        self.assertEqual(response.status_code, 409)

    def test_restart_keeps_filename_records_and_retry_identity(self):
        first = self.store.save(entry())
        self.assertEqual(self.exporter.sync(), {})
        filename = first["excelFile"]
        for _ in range(3):
            restarted = create_app({"DATA_DIR": self.folder, "TESTING": True}, start_worker=False)
            try:
                result = restarted.extensions["qc"].store.save(entry())
                self.assertTrue(result["duplicate"])
                self.assertEqual(result["excelFile"], filename)
                self.assertEqual(restarted.extensions["qc"].exporter.sync(), {})
            finally:
                restarted.extensions["qc"].close()
        second = self.store.save(entry(2))
        self.assertEqual(second["excelFile"], filename)
        self.exporter.sync()
        rows, _ = workbook_rows(self.exporter.folder / filename)
        self.assertEqual(len(rows), 3)

    def test_daily_and_row_rollover_preserved_after_restart(self):
        for mode in ("daily", "rows"):
            folder = self.folder / mode
            store = Store(folder, mode, max_rows=2)
            store.initialize()
            day1 = datetime.fromisoformat("2026-09-10T23:59:58+05:30")
            day2 = datetime.fromisoformat("2026-09-11T00:00:01+05:30")
            names = [store.save(entry(i), now=day1)["excelFile"] for i in range(1, 4)]
            self.assertEqual(names[0], names[1])
            self.assertNotEqual(names[1], names[2])
            store.initialize()
            fourth = store.save(entry(4), now=day2)["excelFile"]
            self.assertEqual(fourth == names[2], mode == "rows")
            self.assertTrue(all(f["row_count"] <= 2 for f in store.files()))

    def test_locked_excel_keeps_old_file_and_recovers(self):
        name = self.store.save(entry())["excelFile"]
        self.exporter.sync()
        path = self.exporter.folder / name
        old = path.read_bytes()
        self.store.save(entry(2))
        with patch("excel_export.os.replace", side_effect=PermissionError("file in use")):
            errors = self.exporter.sync()
        self.assertIn("open in Excel", errors[name])
        self.assertEqual(path.read_bytes(), old)
        self.assertEqual(self.store.counts()["pending"], 1)
        response = self.app.test_client().get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.json["warnings"])
        self.assertEqual(self.app.test_client().post("/save", json=entry(3)).status_code, 201)
        self.assertEqual(self.exporter.sync(), {})
        self.assertEqual(self.store.counts()["pending"], 0)
        self.assertEqual(len(workbook_rows(path)[0]), 4)
        self.assertEqual(self.app.test_client().get("/health").status_code, 200)

    def test_save_during_export_is_not_marked_exported_early(self):
        self.store.save(entry())
        import os
        replace = os.replace
        def concurrent_save(source, destination):
            self.store.save(entry(2))
            return replace(source, destination)
        with patch("excel_export.os.replace", side_effect=concurrent_save):
            self.assertEqual(self.exporter.sync(), {})
        self.assertEqual(self.store.counts()["saved"], 2)
        self.assertEqual(self.store.counts()["pending"], 1)
        self.exporter.sync()
        self.assertEqual(self.store.counts()["pending"], 0)

    def test_missing_excel_is_rebuilt_and_ids_remain_text(self):
        name = self.store.save(entry(boxId="=1+1", packingSlip="=1+1"))["excelFile"]
        self.store.save(entry(2, boxId="0012345678901234567890", packingSlip="0012345678901234567890"))
        self.exporter.sync()
        path = self.exporter.folder / name
        path.unlink()
        self.assertEqual(self.exporter.sync(), {})
        rows, root = workbook_rows(path)
        self.assertEqual(rows[1][2], "=1+1")
        self.assertEqual(rows[2][2], "0012345678901234567890")
        ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        self.assertEqual(root.findall(".//x:f", ns), [])
        self.assertIsNotNone(root.find(".//x:pane", ns))
        self.assertIsNotNone(root.find("x:autoFilter", ns))

    def test_original_schema_migration_retains_existing_data(self):
        folder = self.folder / "legacy"
        folder.mkdir()
        db = sqlite3.connect(folder / "qc_data.db")
        db.execute("CREATE TABLE qc_entries(id INTEGER PRIMARY KEY AUTOINCREMENT,timestamp TEXT NOT NULL,box_id TEXT NOT NULL,packing_slip TEXT NOT NULL,table_no TEXT NOT NULL,qc_done_by TEXT NOT NULL,remark TEXT NOT NULL)")
        db.execute("INSERT INTO qc_entries VALUES(9,'2026-09-09 12:00:00','0001','0001','01','Legacy operator','Done')")
        db.commit()
        db.close()
        store = Store(folder)
        store.initialize()
        store.initialize()
        self.assertEqual(store.counts()["saved"], 1)
        with store.connect() as conn:
            row = conn.execute("SELECT * FROM qc_entries WHERE id=9").fetchone()
            self.assertEqual(row["box_id"], "0001")
            self.assertEqual(row["export_file"], "qc_2026-09-09_001.xlsx")

    def test_backup_includes_committed_wal_entries(self):
        self.store.save(entry())
        with self.store.connect() as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM qc_entries").fetchall()
            self.store.save(entry(2))
            backup = backup_database(self.folder)
        db = sqlite3.connect(backup)
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM qc_entries").fetchone()[0], 2)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            db.close()

    def test_database_failure_returns_diagnostic_and_retry_id(self):
        error = sqlite3.OperationalError("database or disk is full")
        error.sqlite_errorcode = sqlite3.SQLITE_FULL
        error.sqlite_errorname = "SQLITE_FULL"
        with patch.object(self.store, "save", side_effect=error):
            response = self.app.test_client().post("/save", json=entry())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["code"], "SQLITE_FULL")
        self.assertIn("disk is full", response.json["error"])
        self.assertIsNone(response.json["saved"])
        self.assertTrue(response.json["incident"])

    def test_second_server_is_rejected_and_lock_releases(self):
        lock_path = self.folder / ".qc-server.lock"
        first = ProcessLock(lock_path)
        try:
            with self.assertRaisesRegex(RuntimeError, "SERVER_ALREADY_RUNNING"):
                ProcessLock(lock_path)
        finally:
            first.close()
        second = ProcessLock(lock_path)
        second.close()

    def test_setting_changes_do_not_reassign_old_workbooks(self):
        with self.assertRaisesRegex(ValueError, "Stored excel_mode"):
            Store(self.folder, mode="rows").initialize()
        self.assertEqual(self.store.counts()["saved"], 0)


if __name__ == "__main__":
    unittest.main()
