"""Continuously append source revisions. Use --once for a single sync.

Inputs are read-only. Changed boxes/mappings receive immutable versions in
one transaction; existing records are never replaced or deleted.
"""
import argparse
from collections import defaultdict
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading

ROOT = Path(__file__).resolve().parent
ITEMS_DB = Path(os.environ.get("QC_ITEMS_DB", ROOT.parent / "Container-Box-Details" / "box_items.db"))
FSN_DB = Path(os.environ.get("QC_FSN_DB", ROOT.parent / "TL_NCOB" / "FSN_EAN_WID_Title.db"))
OUT_DB = Path(os.environ.get("QC_SOURCE_DB", ROOT / "qc_source.db"))
LOG = logging.getLogger("qc.source")

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_sync (
 id INTEGER PRIMARY KEY, imported_at TEXT NOT NULL,
 items_path TEXT NOT NULL, fsn_path TEXT NOT NULL, kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS box_versions (
 id INTEGER PRIMARY KEY, sync_id INTEGER NOT NULL,
 box_label TEXT NOT NULL, fingerprint TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_box_versions_latest ON box_versions(box_label, sync_id DESC);
CREATE TABLE IF NOT EXISTS box_version_units (
 version_id INTEGER NOT NULL, seq INTEGER NOT NULL,
 fsn TEXT, wid TEXT, plan_type TEXT, PRIMARY KEY(version_id, seq)
);
CREATE TABLE IF NOT EXISTS fsn_versions (
 id INTEGER PRIMARY KEY, sync_id INTEGER NOT NULL,
 fsn TEXT NOT NULL, fingerprint TEXT NOT NULL, product_title TEXT
);
CREATE INDEX IF NOT EXISTS ix_fsn_versions_latest ON fsn_versions(fsn, sync_id DESC);
CREATE TABLE IF NOT EXISTS fsn_version_codes (
 version_id INTEGER NOT NULL, ean TEXT NOT NULL, PRIMARY KEY(version_id, ean)
);
CREATE VIEW IF NOT EXISTS current_box_units AS
 SELECT v.box_label,u.fsn,u.wid,u.plan_type FROM box_versions v
 JOIN box_version_units u ON u.version_id=v.id
 WHERE v.id IN (SELECT MAX(id) FROM box_versions GROUP BY box_label);
CREATE VIEW IF NOT EXISTS current_fsn_codes AS
 SELECT v.fsn,c.ean FROM fsn_versions v JOIN fsn_version_codes c ON c.version_id=v.id
 WHERE v.id IN (SELECT MAX(id) FROM fsn_versions GROUP BY fsn);
CREATE VIEW IF NOT EXISTS current_fsn_titles AS
 SELECT v.fsn,v.product_title,
  (SELECT group_concat(c.ean,',') FROM fsn_version_codes c WHERE c.version_id=v.id) AS ean
 FROM fsn_versions v WHERE v.id IN (SELECT MAX(id) FROM fsn_versions GROUP BY fsn);
CREATE VIEW IF NOT EXISTS current_box_items AS
 SELECT u.box_label AS box,u.fsn,t.ean,u.wid,t.product_title
 FROM current_box_units u LEFT JOIN current_fsn_titles t ON t.fsn=u.fsn;
"""


def read_only(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Source database not found: {path}")
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)


def split_eans(raw):
    return sorted({e.strip().upper() for e in (raw or "").split(",") if e.strip()})


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def boxes_from(rows):
    boxes = defaultdict(list)
    for label, fsn, wid, plan in rows:
        if not isinstance(label, str) or not label.strip():
            raise ValueError("Source has an empty box label; refresh not committed")
        boxes[label.strip()].append((fsn.strip().upper() if fsn else None,
                                     wid.strip().upper() if wid else None, plan))
    # Duplicate rows are retained: each occurrence is one physical unit.
    return {label: sorted(units, key=lambda row: tuple(v or "" for v in row)) for label, units in boxes.items()}


def append_versions(db, boxes, codes, titles, sync_id):
    for label, units in boxes.items():
        version = db.execute("INSERT INTO box_versions(sync_id,box_label,fingerprint) VALUES (?,?,?)",
                             (sync_id, label, fingerprint(units))).lastrowid
        db.executemany("INSERT INTO box_version_units VALUES (?,?,?,?,?)",
                       [(version, seq, *row) for seq, row in enumerate(units, 1)])
    for fsn, eans in codes.items():
        title = titles.get(fsn)
        version = db.execute("INSERT INTO fsn_versions(sync_id,fsn,fingerprint,product_title) VALUES (?,?,?,?)",
                             (sync_id, fsn, fingerprint([eans, title]), title)).lastrowid
        db.executemany("INSERT INTO fsn_version_codes VALUES (?,?)", [(version, ean) for ean in eans])


def build(items_path, fsn_path, out_path):
    """Append only changed versions. Unchanged passes add no records.

    Disappeared boxes/FSNs retain their last known version. A changed box gets
    an exact new unit snapshot, so old/new quantities never accumulate.
    """
    items_path, fsn_path, out_path = map(lambda p: Path(p).resolve(), (items_path, fsn_path, out_path))
    if out_path in (items_path, fsn_path):
        raise ValueError("Output database must differ from both input databases")
    src = read_only(items_path)
    try:
        boxes = boxes_from(src.execute("SELECT box_label,fsn,wid,plan_type FROM box_items"))
    finally:
        src.close()
    src = read_only(fsn_path)
    try:
        codes, titles = defaultdict(set), {}
        for fsn, raw, title in src.execute("SELECT fsn,ean,product_title FROM fsn_ean_wid"):
            if fsn:
                codes[fsn.strip().upper()].update(split_eans(raw))
                if title and title.strip():
                    titles.setdefault(fsn.strip().upper(), title.strip())  # one title per FSN; first non-empty wins
        codes = {fsn: sorted(eans) for fsn, eans in codes.items()}
    finally:
        src.close()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(out_path, timeout=10)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.executescript(SCHEMA)
        for table in ("source_sync", "box_versions", "box_version_units", "fsn_versions", "fsn_version_codes"):
            for operation in ("UPDATE", "DELETE"):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table} "
                           "BEGIN SELECT RAISE(ABORT, 'Source history is append-only'); END")
        db.execute("BEGIN IMMEDIATE")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if db.execute("SELECT COUNT(*) FROM source_sync").fetchone()[0] == 0 and {"box_units", "fsn_codes"} <= tables:
            baseline_boxes = boxes_from(db.execute("SELECT box_label,fsn,wid,plan_type FROM box_units"))
            baseline_codes = defaultdict(set)
            for fsn, ean in db.execute("SELECT fsn,ean FROM fsn_codes"):
                if fsn and ean:
                    baseline_codes[fsn.strip().upper()].add(ean.strip().upper())
            baseline = db.execute("INSERT INTO source_sync(imported_at,items_path,fsn_path,kind) VALUES (?,?,?,?)",
                                  (datetime.now().astimezone().isoformat(), "legacy", "legacy", "legacy baseline")).lastrowid
            append_versions(db, baseline_boxes, {f: sorted(es) for f, es in baseline_codes.items()}, {}, baseline)
        latest_boxes = dict(db.execute("SELECT box_label,fingerprint FROM box_versions WHERE id IN "
                                      "(SELECT MAX(id) FROM box_versions GROUP BY box_label)"))
        latest_codes = dict(db.execute("SELECT fsn,fingerprint FROM fsn_versions WHERE id IN "
                                      "(SELECT MAX(id) FROM fsn_versions GROUP BY fsn)"))
        changed_boxes = {b: rows for b, rows in boxes.items() if latest_boxes.get(b) != fingerprint(rows)}
        changed_codes = {f: es for f, es in codes.items() if latest_codes.get(f) != fingerprint([es, titles.get(f)])}
        revision = db.execute("SELECT MAX(id) FROM source_sync").fetchone()[0]
        if changed_boxes or changed_codes or revision is None:
            revision = db.execute("INSERT INTO source_sync(imported_at,items_path,fsn_path,kind) VALUES (?,?,?,?)",
                                  (datetime.now().astimezone().isoformat(), str(items_path), str(fsn_path), "refresh")).lastrowid
            append_versions(db, changed_boxes, changed_codes, titles, revision)
        db.commit()
        return {"revision": revision, "boxes": len(boxes), "units": sum(map(len, boxes.values())),
                "changed_boxes": len(changed_boxes), "changed_fsns": len(changed_codes),
                "null_fsn": sum(f is None for rows in boxes.values() for f, _, _ in rows),
                "null_wid": sum(w is None for rows in boxes.values() for _, w, _ in rows),
                "fsns_without_ean": len({f for rows in boxes.values() for f, _, _ in rows if f and not codes.get(f)})}
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def file_stamp(paths):
    result = []
    for path in paths:
        for file in (Path(path), Path(str(path) + "-wal")):
            try:
                stat = file.stat()
                result.append((stat.st_mtime_ns, stat.st_size))
            except FileNotFoundError:
                result.append(None)
    return tuple(result)


class SourceUpdater:
    def __init__(self, items_path, fsn_path, out_path, interval=5):
        self.paths = tuple(map(Path, (items_path, fsn_path, out_path)))
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.stamp = None
        self.status = {"lastCheck": None, "lastSuccess": None, "error": None, "revision": None}

    def state(self):
        with self.lock:
            return {**self.status, "workerAlive": bool(self.thread and self.thread.is_alive()), "interval": self.interval}

    def sync(self):
        stamp = file_stamp(self.paths[:2])
        now = datetime.now().astimezone().isoformat()
        try:
            if stamp != self.stamp:
                report = build(*self.paths)
                # If inputs changed while reading, the next pass detects it.
                self.stamp = stamp
                with self.lock:
                    self.status.update(report)
                if report["changed_boxes"] or report["changed_fsns"]:
                    LOG.info("SOURCE_REFRESH revision=%s boxes=%s fsns=%s", report["revision"], report["changed_boxes"], report["changed_fsns"])
            with self.lock:
                self.status.update(lastCheck=now, lastSuccess=now, error=None)
        except Exception as error:
            message = str(error)
            with self.lock:
                changed = message != self.status["error"]
                self.status.update(lastCheck=now, error=message)
            if changed:
                LOG.warning("SOURCE_REFRESH_FAILED: %s; keeping the last complete revision", message)
        return self.state()

    def start(self):
        def work():
            while not self.stop_event.is_set():
                self.sync()
                self.stop_event.wait(self.interval)
        self.thread = threading.Thread(target=work, name="source-refresh", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=12)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Append source changes continuously; Ctrl+C stops")
    parser.add_argument("--once", action="store_true", help="Perform one refresh and exit")
    parser.add_argument("--items", type=Path, default=ITEMS_DB)
    parser.add_argument("--fsn", type=Path, default=FSN_DB)
    parser.add_argument("--out", type=Path, default=OUT_DB)
    parser.add_argument("--interval", type=float, default=float(os.environ.get("QC_SOURCE_INTERVAL", "5")))
    args = parser.parse_args(argv)
    if not 0.5 <= args.interval <= 3600:
        parser.error("interval must be between 0.5 and 3600 seconds")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.once:
        print(build(args.items, args.fsn, args.out))
        return 0
    updater = SourceUpdater(args.items, args.fsn, args.out, args.interval)
    print(f"Source refresh running every {args.interval:g}s. Ctrl+C to stop.", flush=True)
    try:
        while True:
            updater.sync()
            updater.stop_event.wait(args.interval)
    except KeyboardInterrupt:
        print("Source refresh stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
