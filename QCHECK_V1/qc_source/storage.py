"""Durable database-only QC entries, audits, and scan history."""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class InputError(ValueError):
    def __init__(self, message, code="INVALID_INPUT", status=400):
        super().__init__(message)
        self.code, self.status = code, status


FIELDS = {"boxId": ("BOX ID", 200), "packingSlip": ("Packing Slip", 200),
          "tableNo": ("Table No", 80), "qcDoneBy": ("QC Done By", 200),
          "remark": ("Remark", 10)}


def validate(data):
    if not isinstance(data, dict):
        raise InputError("Send a JSON object containing the entry fields.")
    clean = {}
    for key, (label, limit) in FIELDS.items():
        value = data.get(key)
        if not isinstance(value, str):
            raise InputError(f"{label} must be text.")
        value = value.strip()
        if not value:
            raise InputError(f"{label} is required.")
        if len(value) > limit or any(ord(c) < 32 for c in value):
            raise InputError(f"{label} must be at most {limit} characters without control characters.")
        clean[key] = value
    if clean["boxId"] != clean["packingSlip"]:
        raise InputError("BOX ID and Packing Slip do not match.")
    if clean["remark"] not in ("Done", "Bypass"):
        raise InputError("Remark must be Done or Bypass.")
    request_id = data.get("requestId")
    if not isinstance(request_id, str) or not 16 <= len(request_id) <= 80:
        raise InputError("Submission ID is missing or invalid. Reload the updated page.")
    if not all(c.isascii() and (c.isalnum() or c in "-_") for c in request_id):
        raise InputError("Submission ID contains invalid characters.")
    clean["requestId"] = request_id
    return clean


def clean_text(value, label, limit):
    if not isinstance(value, str):
        raise InputError(f"{label} must be text.")
    value = value.strip()
    if not value:
        raise InputError(f"{label} is required.")
    if len(value) > limit or any(ord(c) < 32 for c in value):
        raise InputError(f"{label} must be at most {limit} characters without control characters.")
    return value


def clean_request_id(value):
    if not isinstance(value, str) or not 16 <= len(value) <= 80:
        raise InputError("Submission ID is missing or invalid. Reload the updated page.")
    if not all(c.isascii() and (c.isalnum() or c in "-_") for c in value):
        raise InputError("Submission ID contains invalid characters.")
    return value


class Store:
    def __init__(self, folder, timeout=5):
        self.folder = Path(folder).resolve()
        self.path = self.folder / "qc_data.db"
        self.timeout = timeout
        self.write_lock = threading.Lock()
        self.keeper = None

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=self.timeout)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            check = db.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                raise sqlite3.DatabaseError(f"Database integrity check failed: {check}")
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS qc_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                box_id TEXT NOT NULL, packing_slip TEXT NOT NULL,
                table_no TEXT NOT NULL, qc_done_by TEXT NOT NULL, remark TEXT NOT NULL)""")
            columns = {r[1] for r in db.execute("PRAGMA table_info(qc_entries)")}
            for column in ("request_id",):
                if column not in columns:
                    db.execute(f"ALTER TABLE qc_entries ADD COLUMN {column} TEXT")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_qc_request ON qc_entries(request_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_qc_timestamp ON qc_entries(timestamp)")
            db.execute("""CREATE TABLE IF NOT EXISTS qc_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL UNIQUE,
                box_id TEXT NOT NULL, table_no TEXT NOT NULL, casper_id TEXT NOT NULL,
                submitted_at TEXT NOT NULL, fingerprint TEXT NOT NULL, entry_id INTEGER UNIQUE, replaced_by INTEGER)""")
            db.execute("""CREATE TABLE IF NOT EXISTS qc_audit_scans (
                audit_id INTEGER NOT NULL, seq INTEGER NOT NULL, code TEXT NOT NULL,
                code_type TEXT NOT NULL, fsn TEXT, wid TEXT, PRIMARY KEY (audit_id, seq))""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_qc_audit_box ON qc_audit(box_id)")
            if "replaced_by" not in {r[1] for r in db.execute("PRAGMA table_info(qc_audit)")}:
                db.execute("ALTER TABLE qc_audit ADD COLUMN replaced_by INTEGER")
            if "source_revision" not in {r[1] for r in db.execute("PRAGMA table_info(qc_audit)")}:
                db.execute("ALTER TABLE qc_audit ADD COLUMN source_revision INTEGER")
            db.commit()
        # Keep the WAL open between requests. Otherwise closing the last
        # connection checkpoints it after every save, adding disk work.
        self.keeper = sqlite3.connect(self.path, timeout=self.timeout, check_same_thread=False)
        self.keeper.execute("PRAGMA journal_mode=WAL")

    def close(self):
        if self.keeper is not None:
            self.keeper.close()
            self.keeper = None

    def save(self, data, now=None, audit_id=None):
        clean = validate(data)
        payload = tuple(clean[k] for k in FIELDS)
        timestamp = (now or datetime.now().astimezone()).isoformat(sep=" ", timespec="seconds")
        with self.write_lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            audit = None
            if audit_id is not None:
                audit = db.execute("SELECT box_id, table_no, casper_id, entry_id FROM qc_audit WHERE id=?", (audit_id,)).fetchone()
                if audit is None:
                    raise InputError("Audit not found.", "AUDIT_NOT_FOUND", 404)
                if (audit["box_id"], audit["table_no"], audit["casper_id"]) != (clean["boxId"], clean["tableNo"], clean["qcDoneBy"]):
                    raise InputError("BOX ID, Table No or QC Done By does not match the audit.", "AUDIT_MISMATCH", 400)
            existing = db.execute("SELECT * FROM qc_entries WHERE request_id=?", (clean["requestId"],)).fetchone()
            if existing:
                old = tuple(existing[k] for k in ("box_id", "packing_slip", "table_no", "qc_done_by", "remark"))
                if old != payload:
                    raise InputError("This submission ID already belongs to different data. Contact the supervisor.", "SUBMISSION_CONFLICT", 409)
                if audit is not None and audit["entry_id"] != existing["id"]:
                    raise InputError("This audit is linked to a different entry.", "AUDIT_ALREADY_LINKED", 409)
                return {"id": existing["id"], "duplicate": True}
            if audit is not None and audit["entry_id"] is not None:
                raise InputError("This audit is already linked to another entry.", "AUDIT_ALREADY_LINKED", 409)
            cursor = db.execute("""INSERT INTO qc_entries
                (timestamp,box_id,packing_slip,table_no,qc_done_by,remark,request_id)
                VALUES (?,?,?,?,?,?,?)""", (timestamp, *payload, clean["requestId"]))
            entry_id = cursor.lastrowid
            if audit is not None:
                db.execute("UPDATE qc_audit SET entry_id=? WHERE id=?", (entry_id, audit_id))
            db.commit()
        return {"id": entry_id, "duplicate": False}

    def audit_retry(self, request_id, fingerprint):
        """Confirm a committed audit even if source data is temporarily unavailable."""
        with self.connect() as db:
            row = db.execute("SELECT id,submitted_at,fingerprint FROM qc_audit WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise InputError("This submission ID already belongs to a different audit. Contact the supervisor.", "SUBMISSION_CONFLICT", 409)
        return {"id": row["id"], "submittedAt": row["submitted_at"], "duplicate": True}

    def save_audit(self, request_id, box_id, table_no, casper_id, scans, fingerprint, now=None, source_revision=None):
        timestamp = (now or datetime.now().astimezone()).isoformat(sep=" ", timespec="seconds")
        with self.write_lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id, submitted_at, fingerprint FROM qc_audit WHERE request_id=?", (request_id,)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise InputError("This submission ID already belongs to a different audit. Contact the supervisor.", "SUBMISSION_CONFLICT", 409)
                return {"id": row["id"], "submittedAt": row["submitted_at"], "duplicate": True}
            cursor = db.execute("INSERT INTO qc_audit (request_id, box_id, table_no, casper_id, submitted_at, fingerprint, source_revision) VALUES (?,?,?,?,?,?,?)",
                                (request_id, box_id, table_no, casper_id, timestamp, fingerprint, source_revision))
            audit_id = cursor.lastrowid
            db.execute("UPDATE qc_audit SET replaced_by=? WHERE box_id=? AND replaced_by IS NULL AND id<>?", (audit_id, box_id, audit_id))
            db.executemany("INSERT INTO qc_audit_scans (audit_id, seq, code, code_type, fsn, wid) VALUES (?,?,?,?,?,?)",
                           [(audit_id, seq, s["code"], s["type"], s["fsn"], s["wid"]) for seq, s in enumerate(scans, 1)])
            db.commit()
        return {"id": audit_id, "submittedAt": timestamp, "duplicate": False}

    def latest_audit(self, box_id):
        with self.connect() as db:
            row = db.execute("SELECT id, submitted_at, table_no, casper_id FROM qc_audit WHERE box_id=? AND replaced_by IS NULL ORDER BY id DESC LIMIT 1", (box_id,)).fetchone()
        if row is None:
            return None
        return {"auditId": row["id"], "submittedAt": row["submitted_at"], "tableNo": row["table_no"], "casperId": row["casper_id"]}

    def counts(self):
        with self.connect() as db:
            return {"saved": db.execute("SELECT COUNT(*) FROM qc_entries").fetchone()[0],
                    "audits": db.execute("SELECT COUNT(*) FROM qc_audit").fetchone()[0]}
