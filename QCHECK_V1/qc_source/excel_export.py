"""One background writer. SQLite remains authoritative if Excel is locked."""
import logging
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path

import xlsxwriter

LOG = logging.getLogger("qc")


class ExcelExporter:
    def __init__(self, store, interval=30):
        self.store = store
        self.folder = store.folder / "exports"
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = None
        self.guard = threading.Lock()
        self.run_guard = threading.Lock()
        self.errors = {}
        self.last_check = None

    def start(self):
        self.thread = threading.Thread(target=self.loop, name="excel-export", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=15)

    def loop(self):
        while not self.stop_event.is_set():
            self.sync()
            self.stop_event.wait(self.interval)

    def state(self):
        with self.guard:
            return {"errors": dict(self.errors), "lastCheck": self.last_check,
                    "workerAlive": bool(self.thread and self.thread.is_alive())}

    def sync(self, force=False):
        with self.run_guard:
            errors = {}
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                for info in self.store.files():
                    name = info["filename"]
                    if force or info["row_count"] != info["exported_count"] or not (self.folder / name).is_file():
                        try:
                            self.write_file(name)
                        except Exception as error:
                            errors[name] = self.explain(error, name)
                            LOG.exception("EXCEL_EXPORT_FAILED file=%s", name)
            except Exception as error:
                errors["exporter"] = self.explain(error, "exports folder / database")
                LOG.exception("EXCEL_EXPORT_FAILED")
            with self.guard:
                self.errors = errors
                self.last_check = datetime.now().astimezone().isoformat(timespec="seconds")
            return errors

    @staticmethod
    def explain(error, name):
        if isinstance(error, PermissionError) or getattr(error, "winerror", None) in (5, 32):
            return f"Cannot update {name}: file is open in Excel or access is denied. Close Excel and check folder permissions; export will retry automatically."
        return f"Cannot update {name}: {type(error).__name__}: {error}. Saved entries remain in the database; export will retry automatically."

    def write_file(self, filename):
        if Path(filename).name != filename:
            raise ValueError("Invalid Excel filename in database")
        target = self.folder / filename
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".qc-", suffix=".tmp", dir=self.folder, delete=False) as temp:
                temp_path = Path(temp.name)
            with self.store.connect() as db:
                db.execute("BEGIN")
                rows = db.execute("SELECT id,timestamp,box_id,packing_slip,table_no,qc_done_by,remark FROM qc_entries WHERE export_file=? ORDER BY id", (filename,))
                count = 0
                with xlsxwriter.Workbook(str(temp_path), {"constant_memory": True, "tmpdir": str(self.folder), "strings_to_formulas": False, "strings_to_urls": False}) as book:
                    sheet = book.add_worksheet("QC Entries")
                    normal = book.add_format({"font_name": "Arial", "font_size": 11})
                    header = book.add_format({"font_name": "Arial", "font_size": 11, "bold": True, "bg_color": "#17365D", "font_color": "#FFFFFF", "align": "center", "valign": "vcenter"})
                    date_format = book.add_format({"font_name": "Arial", "font_size": 11, "num_format": "dd-mmm-yyyy hh:mm:ss"})
                    sheet.hide_gridlines(2)
                    sheet.freeze_panes(1, 0)
                    sheet.set_default_row(20)
                    sheet.set_row(0, 28)
                    sheet.set_column(0, 0, 12, normal)
                    sheet.set_column(1, 1, 29, date_format)
                    sheet.set_column(2, 3, 28, normal)
                    sheet.set_column(4, 4, 14, normal)
                    sheet.set_column(5, 5, 28, normal)
                    sheet.set_column(6, 6, 14, normal)
                    sheet.write_row(0, 0, ["Entry ID", "Timestamp (server local)", "BOX ID", "Packing Slip", "Table No", "QC Done By", "Remark"], header)
                    for count, row in enumerate(rows, 1):
                        if count > self.store.max_rows:
                            raise ValueError("Workbook row allocation exceeded the configured limit")
                        sheet.write_number(count, 0, row[0])
                        stamp = datetime.fromisoformat(row[1]).replace(tzinfo=None)
                        sheet.write_datetime(count, 1, stamp, date_format)
                        for column in range(2, 7):
                            # Identifiers stay text, including leading zeros and = prefixes.
                            sheet.write_string(count, column, str(row[column]))
                    sheet.autofilter(0, 0, count, 6)
                    sheet.set_landscape()
                    sheet.fit_to_pages(1, 0)
                    sheet.repeat_rows(0)
                db.rollback()
            with temp_path.open("rb+") as handle:
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
            with self.store.write_lock, self.store.connect() as db:
                db.execute("UPDATE excel_files SET exported_count=?,exported_at=? WHERE filename=?", (count, datetime.now().astimezone().isoformat(timespec="seconds"), filename))
                db.commit()
            LOG.info("EXCEL_UPDATED file=%s rows=%d", filename, count)
        finally:
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    LOG.exception("EXCEL_TEMP_CLEANUP_FAILED file=%s", temp_path)
