"""Start with python app.py. A single Waitress process serves concurrent users."""
import argparse
import atexit
import hashlib
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
from datetime import datetime
import uuid

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from build_source import SourceUpdater
from storage import InputError, Store, clean_request_id, clean_text
import source

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("qc")


def check_layout(folder=ROOT):
    folder = Path(folder)
    missing = []
    for relative in (Path("templates/index.html"), Path("static/qc.js"), Path("templates/audit.html"), Path("static/audit.js"), Path("static/audit.css")):
        destination = folder / relative
        if not destination.is_file():
            misplaced = folder / relative.name
            if misplaced.is_file():
                missing.append(f"Move {misplaced} into {destination.parent}. Required location: {destination}")
            else:
                missing.append(f"Missing file: {destination}. Create the {relative.parent} folder and put {relative.name} inside it.")
    if missing:
        raise FileNotFoundError("INSTALLATION_FILES_MISSING:\n" + "\n".join(missing))


def local_ipv4_addresses():
    addresses = set()

    def add(value):
        try:
            address = ipaddress.IPv4Address(value)
            if not (address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local):
                addresses.add(str(address))
        except ipaddress.AddressValueError:
            pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM):
            add(info[4][0])
    except OSError:
        pass
    if not addresses:
        try:
            # Select the OS's outbound interface without sending any application data.
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("192.0.2.1", 9))
                add(probe.getsockname()[0])
        except OSError:
            pass
    return sorted(addresses, key=lambda value: (not ipaddress.IPv4Address(value).is_private, int(ipaddress.IPv4Address(value))))


def print_server_addresses(host, port):
    if host == "0.0.0.0":
        print(f"On this PC: http://127.0.0.1:{port}", flush=True)
        addresses = local_ipv4_addresses()
        for address in addresses:
            print(f"Open on other PCs: http://{address}:{port}", flush=True)
        if not addresses:
            print("NETWORK_ADDRESS_NOT_FOUND: no usable IPv4 address was detected. Connect this PC to the local network and restart the server.", flush=True)
    else:
        display_host = f"[{host}]" if ":" in host else host
        print(f"Server address: http://{display_host}:{port}", flush=True)
        if host in ("127.0.0.1", "localhost", "::1"):
            print("This server is configured for this PC only (QC_HOST).", flush=True)


class ProcessLock:
    """OS-released lock: a killed server never leaves an unremovable stale lock."""
    def __init__(self, path):
        self.handle = open(path, "a+b")
        try:
            self.handle.seek(0, os.SEEK_END)
            if self.handle.tell() == 0:
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.handle.close()
            raise RuntimeError("SERVER_ALREADY_RUNNING: another server process is using this data folder. Close that process first.") from error

    def close(self):
        if not self.handle.closed:
            self.handle.close()


def configure_logging(folder):
    folder.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("qc")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)
        file_handler = RotatingFileHandler(folder / "qc.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)


def configuration(overrides=None):
    folder = Path(os.environ.get("QC_DATA_DIR", str(ROOT)))
    if not folder.is_absolute():
        folder = ROOT / folder
    values = {"DATA_DIR": folder.resolve(), "DB_TIMEOUT": 5, "TESTING": False,
              "SOURCE_DB": Path(os.environ.get("QC_SOURCE_DB", str(ROOT / "qc_source.db"))),
              "ITEMS_DB": Path(os.environ.get("QC_ITEMS_DB", str(ROOT.parent / "Container-Box-Details" / "box_items.db"))),
              "FSN_DB": Path(os.environ.get("QC_FSN_DB", str(ROOT.parent / "TL_NCOB" / "FSN_EAN_WID_Title.db"))),
              "SOURCE_INTERVAL": float(os.environ.get("QC_SOURCE_INTERVAL", "5"))}
    values.update(overrides or {})
    for key in ("DATA_DIR", "SOURCE_DB", "ITEMS_DB", "FSN_DB"):
        values[key] = Path(values[key]).resolve()
    if not 0.5 <= values["SOURCE_INTERVAL"] <= 3600:
        raise ValueError("QC_SOURCE_INTERVAL must be between 0.5 and 3600 seconds.")
    if values["SOURCE_DB"] == values["DATA_DIR"] / "qc_data.db":
        raise ValueError("QC_SOURCE_DB must be different from qc_data.db")
    return values


def database_error(error):
    name = getattr(error, "sqlite_errorname", "DATABASE_ERROR")
    primary = getattr(error, "sqlite_errorcode", 0) & 255
    actions = {
        sqlite3.SQLITE_BUSY: "Database is busy. Retry this same entry in a few seconds.",
        sqlite3.SQLITE_LOCKED: "Database is locked. Close other database tools and retry this same entry.",
        sqlite3.SQLITE_FULL: "The database disk is full. Free disk space and retry this same entry.",
        sqlite3.SQLITE_READONLY: "Database is read-only. Restore write permission on the data folder.",
        sqlite3.SQLITE_CANTOPEN: "Database cannot be opened. Check that the data folder exists and is writable.",
        sqlite3.SQLITE_CORRUPT: "Database is damaged. Stop the server and restore a verified backup.",
        sqlite3.SQLITE_NOTADB: "The database file is invalid. Stop the server and restore a verified backup.",
        sqlite3.SQLITE_IOERR: "Database disk input/output failed. Check the drive and qc.log before retrying.",
    }
    return name, actions.get(primary, "Database operation failed. Check qc.log for the exact exception and retry using the same submission.")


class Runtime:
    def __init__(self, store, updater, lock=None):
        self.store, self.updater, self.lock = store, updater, lock

    def close(self):
        self.updater.stop()
        self.store.close()
        if self.lock and not (self.updater.thread and self.updater.thread.is_alive()):
            self.lock.close()


def create_app(config=None, start_worker=True):
    check_layout()
    app = Flask(__name__)
    app.config.update(configuration(config))
    app.config["MAX_CONTENT_LENGTH"] = 65536
    folder = app.config["DATA_DIR"]
    folder.mkdir(parents=True, exist_ok=True)
    if not app.config["TESTING"]:
        configure_logging(folder)
    lock = ProcessLock(folder / ".qc-server.lock") if start_worker else None
    store = None
    updater = None
    try:
        store = Store(folder, timeout=app.config["DB_TIMEOUT"])
        store.initialize()
        updater = SourceUpdater(app.config["ITEMS_DB"], app.config["FSN_DB"], app.config["SOURCE_DB"], app.config["SOURCE_INTERVAL"])
        runtime = Runtime(store, updater, lock)
        app.extensions["qc"] = runtime
        if start_worker:
            updater.sync()
            updater.start()
            atexit.register(runtime.close)
    except BaseException:
        if updater:
            updater.stop()
        if store:
            store.close()
        if lock:
            lock.close()
        raise

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/")
    def index():
        return render_template("audit.html")

    @app.get("/classic")
    def classic_form():
        return render_template("index.html")

    @app.post("/save")
    def save():
        result = store.save(request.get_json(silent=True))
        # Nothing that can fail on disk after commit: a successful save must stay successful.
        return jsonify(status="success", saved=True, **result), (200 if result["duplicate"] else 201)

    def box_or_raise(box_id, revision=None):
        box = source.load_box(app.config["SOURCE_DB"], box_id, revision)
        if box is None:
            raise InputError("Box not found in the source data. Use BYPASS.", "BOX_NOT_FOUND", 404)
        if any(fsn is None for fsn, _ in box["units"]):
            raise InputError("This box has units with no FSN and cannot be audited. Use BYPASS.", "BOX_NOT_AUDITABLE", 422)
        return box

    @app.get("/audit")
    def audit_page():
        return render_template("audit.html")

    @app.get("/api/box/<box_id>")
    def api_box(box_id):
        box_id = source.clean_box(box_id)
        box = box_or_raise(box_id)
        return jsonify(status="ok", box_id=box_id, total_units=len(box["units"]), groups=source.groups(box),
                       previous=store.latest_audit(box_id), sourceRevision=box["revision"])

    @app.post("/api/qc_audit")
    def api_qc_audit():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise InputError("Send a JSON object containing the audit.")
        request_id = clean_request_id(body.get("requestId"))
        box_id = source.clean_box(body.get("boxId"))
        table_no = clean_text(body.get("tableNo"), "Table No", 80)
        casper_id = clean_text(body.get("casperId"), "Casper ID", 200)
        codes = source.clean_codes(body.get("scans"))
        revision = body.get("sourceRevision")
        identity = [box_id, table_no, casper_id, codes]
        if revision is not None:
            identity.append(revision)
        digest = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        result = store.audit_retry(request_id, digest)
        if result is None:
            resolved, errors = source.reconcile(box_or_raise(box_id, revision), codes)
            if errors:
                return jsonify(error="Audit not accepted: items or counts do not match the box.", code="AUDIT_MISMATCH",
                               errors=errors[:50], saved=False), 400
            result = store.save_audit(request_id, box_id, table_no, casper_id, resolved, digest, source_revision=revision)
        return jsonify(status="success", saved=True, auditId=result["id"], submittedAt=result["submittedAt"],
                       boxId=box_id, tableNo=table_no, casperId=casper_id, duplicate=result["duplicate"]), \
            (200 if result["duplicate"] else 201)

    @app.post("/api/packing_slip")
    def api_packing_slip():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise InputError("Send a JSON object containing the entry fields.")
        audit_id = body.get("auditId")
        if audit_id is not None and (not isinstance(audit_id, int) or isinstance(audit_id, bool)):
            raise InputError("Audit ID is invalid.")
        if body.get("remark") == "Done" and audit_id is None:
            raise InputError("QC entries need a completed audit. Use BYPASS if the box cannot be audited.", "AUDIT_REQUIRED", 400)
        if body.get("remark") == "Bypass" and audit_id is not None:
            raise InputError("BYPASS entries cannot be linked to an audit.", "BYPASS_WITH_AUDIT", 400)
        result = store.save(body, audit_id=audit_id)
        return jsonify(status="success", saved=True, **result), (200 if result["duplicate"] else 201)

    @app.get("/health")
    def health():
        counts = store.counts()
        state = updater.state()
        warnings = []
        if start_worker:
            if state["error"]:
                warnings.append("Source refresh failed; using the last complete box data. Tell the supervisor.")
            if not state["workerAlive"]:
                warnings.append("Source refresh has stopped. Restart the server.")
        if not app.config["SOURCE_DB"].is_file():
            warnings.append("Box data is unavailable. Use BYPASS or tell the supervisor.")
        free = shutil.disk_usage(folder).free
        if free < 500 * 1024 * 1024:
            warnings.append(f"Low disk space: {free // (1024 * 1024)} MB remains on the data drive.")
        return jsonify(status="warning" if warnings else "ok", database="ok", **counts,
                       source=state, warnings=warnings), (503 if warnings else 200)

    @app.errorhandler(InputError)
    def bad_input(error):
        return jsonify(error=str(error), code=error.code, saved=False), error.status

    @app.errorhandler(source.SourceUnavailable)
    def source_missing(error):
        return jsonify(error="Box data is unavailable. Use BYPASS or tell the supervisor.", code="SOURCE_UNAVAILABLE", saved=None), 503

    @app.errorhandler(sqlite3.Error)
    def database_failure(error):
        code, message = database_error(error)
        incident = uuid.uuid4().hex[:10]
        LOG.exception("DATABASE_FAILED incident=%s code=%s path=%s", incident, code, store.path)
        return jsonify(error=message, code=code, incident=incident, saved=None), 503

    @app.errorhandler(HTTPException)
    def http_failure(error):
        return jsonify(error=error.description, code=f"HTTP_{error.code}", saved=False), error.code

    @app.errorhandler(Exception)
    def unexpected(error):
        incident = uuid.uuid4().hex[:10]
        LOG.exception("UNEXPECTED_ERROR incident=%s", incident)
        return jsonify(error="Unexpected server error. Check qc.log using this incident ID. Retry the same submission to confirm whether it was saved.", code="UNEXPECTED_ERROR", incident=incident, saved=None), 500

    return app


def backup_database(folder):
    folder = Path(folder).resolve()
    source_db = folder / "qc_data.db"
    if not source_db.is_file():
        raise FileNotFoundError(f"No database exists at {source_db}")
    destination = folder / "backups"
    destination.mkdir(parents=True, exist_ok=True)
    name = f"qc_data_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.db"
    temporary = destination / (name + ".tmp")
    try:
        src = sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            dst = sqlite3.connect(temporary)
            try:
                src.backup(dst, pages=1000, sleep=0.05)
                check = dst.execute("PRAGMA integrity_check").fetchone()[0]
                if check != "ok":
                    raise sqlite3.DatabaseError(f"Backup integrity check failed: {check}")
            finally:
                dst.close()
        finally:
            src.close()
        with temporary.open("rb+") as handle:
            os.fsync(handle.fileno())
        final = destination / name
        os.replace(temporary, final)
        return final
    finally:
        if temporary.exists():
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description="QC entry server and local maintenance")
    parser.add_argument("--backup", action="store_true", help="Create a consistent database backup; server may stay running")
    args = parser.parse_args()
    runtime = None
    try:
        config = configuration()
        configure_logging(config["DATA_DIR"])
        if args.backup:
            print(f"BACKUP_COMPLETE: {backup_database(config['DATA_DIR'])}")
            return 0
        from waitress import serve
        port = int(os.environ.get("QC_PORT", "5000"))
        host = os.environ.get("QC_HOST", "0.0.0.0")
        if not 1 <= port <= 65535:
            raise ValueError("QC_PORT must be between 1 and 65535")
        app = create_app(config)
        runtime = app.extensions["qc"]
        LOG.info("SERVER_START database=%s source=%s refresh=%ss", runtime.store.path, config["SOURCE_DB"], config["SOURCE_INTERVAL"])
        print_server_addresses(host, port)
        print("QCheck running. Saves go directly to the database. Source refresh is automatic. Ctrl+C to stop.", flush=True)
        serve(app, host=host, port=port, threads=32, connection_limit=200,
              channel_timeout=60, max_request_body_size=65536, expose_tracebacks=False)
        return 0
    except KeyboardInterrupt:
        LOG.info("SERVER_STOP requested by operator; committed entries will resume after restart")
        return 0
    except Exception as error:
        LOG.exception("STARTUP_OR_MAINTENANCE_FAILED")
        print(f"STARTUP_OR_MAINTENANCE_FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        if runtime:
            runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
