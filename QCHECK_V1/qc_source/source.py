"""Box data from qc_source.db (read-only) and the audit rules.

The server is the authority. The device runs the same rules for live feedback only.
"""
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from storage import InputError

SAFE_BOX = re.compile(r"[A-Za-z0-9_-]{1,64}")
MAX_SCANS = 1000
MAX_CODE = 64


class SourceUnavailable(Exception):
    pass


def clean_box(value):
    if not isinstance(value, str) or not SAFE_BOX.fullmatch(value.strip()):
        raise InputError("Box ID is missing or not valid.")
    return value.strip()


def clean_codes(value):
    if not isinstance(value, list) or len(value) > MAX_SCANS:
        raise InputError(f"Scans must be a list of at most {MAX_SCANS} codes.")
    codes = []
    for raw in value:
        if not isinstance(raw, str):
            raise InputError("Each scan must be text.")
        code = raw.strip().upper()
        if not 1 <= len(code) <= MAX_CODE or any(ord(c) < 33 for c in code):
            raise InputError("Each scan must be a code without spaces or control characters.")
        codes.append(code)
    return codes


def _connect(path):
    path = Path(path)
    if not path.is_file():
        raise SourceUnavailable(f"qc_source database not found: {path}")
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)


def load_box(path, box_id, revision=None):
    """One box's units and EAN lists. None if the box is not in the source data."""
    con = _connect(path)
    try:
        con.execute("BEGIN")  # One consistent view while a refresh commits in parallel.
        versioned = con.execute("SELECT 1 FROM sqlite_master WHERE name='source_sync' AND type='table'").fetchone()
        latest = con.execute("SELECT MAX(id) FROM source_sync").fetchone()[0] if versioned else None
        if revision is not None:
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1 or not versioned or not con.execute("SELECT 1 FROM source_sync WHERE id=?", (revision,)).fetchone():
                raise InputError("Source revision is invalid. Reload the box.", "SOURCE_REVISION_INVALID", 400)
        revision = latest if revision is None else revision
        if revision is not None:
            version = con.execute("SELECT id FROM box_versions WHERE box_label=? AND sync_id<=? ORDER BY sync_id DESC LIMIT 1", (box_id, revision)).fetchone()
            units = con.execute("SELECT fsn,wid FROM box_version_units WHERE version_id=? ORDER BY seq", (version[0],)).fetchall() if version else []
        else:
            units = con.execute("SELECT fsn, wid FROM box_units WHERE box_label = ?", (box_id,)).fetchall()
        if not units:
            return None
        fsns = sorted({fsn for fsn, _ in units if fsn is not None})
        eans, titles = defaultdict(list), {}
        if fsns:
            marks = ",".join("?" * len(fsns))
            if revision is not None:
                rows = con.execute(f"SELECT v.fsn,c.ean FROM fsn_versions v JOIN fsn_version_codes c ON c.version_id=v.id "
                                   f"WHERE v.id IN (SELECT MAX(id) FROM fsn_versions WHERE fsn IN ({marks}) AND sync_id<=? GROUP BY fsn) ORDER BY v.fsn,c.ean", (*fsns, revision))
            else:
                rows = con.execute(f"SELECT fsn, ean FROM fsn_codes WHERE fsn IN ({marks}) ORDER BY fsn, ean", fsns)
            for fsn, ean in rows:
                eans[fsn].append(ean)
            if revision is not None:
                titles = dict(con.execute(f"SELECT fsn, product_title FROM fsn_versions WHERE id IN "
                                          f"(SELECT MAX(id) FROM fsn_versions WHERE fsn IN ({marks}) AND sync_id<=? GROUP BY fsn)",
                                          (*fsns, revision)))
        return {"units": units, "eans": dict(eans), "titles": titles, "revision": revision}
    finally:
        con.close()


def groups(box):
    """Per-FSN view sent to the device: expected units, EANs, and WIDs with unit counts."""
    expected = Counter(fsn for fsn, _ in box["units"])
    wid_units = defaultdict(Counter)
    for fsn, wid in box["units"]:
        if wid:
            wid_units[fsn][wid] += 1
    return [{"fsn": fsn, "expected": expected[fsn], "title": box["titles"].get(fsn), "eans": box["eans"].get(fsn, []),
             "wids": [{"wid": w, "units": n} for w, n in sorted(wid_units[fsn].items())]}
            for fsn in sorted(expected)]


def reconcile(box, codes):
    """Apply the audit rules to a scan log. Returns (accepted scans, errors).

    Rules: a WID or EAN must belong to this box. A WID that belongs to one unit can be
    scanned once only. Multi-unit WIDs count every scan. Each FSN total must equal its
    expected count. A code shared by two FSNs in the box is rejected.
    """
    wid_fsns = defaultdict(set)
    wid_units = Counter()
    for fsn, wid in box["units"]:
        if wid:
            wid_fsns[wid.upper()].add(fsn)
            wid_units[wid.upper()] += 1
    ean_fsns = defaultdict(set)
    for fsn, eans in box["eans"].items():
        for ean in eans:
            ean_fsns[ean.upper()].add(fsn)

    expected_units = Counter(f for f, _ in box["units"])
    accepted, errors, counts, seen_wids = [], [], Counter(), Counter()
    for i, code in enumerate(codes, 1):
        if code in wid_fsns:
            if len(wid_fsns[code]) > 1:
                errors.append(f"Scan {i}: WID {code} belongs to more than one FSN in this box.")
                continue
            fsn, kind, wid = next(iter(wid_fsns[code])), "WID", code
        elif code in ean_fsns:
            if len(ean_fsns[code]) > 1:
                errors.append(f"Scan {i}: EAN {code} belongs to more than one FSN in this box.")
                continue
            fsn, kind, wid = next(iter(ean_fsns[code])), "EAN", None
        else:
            errors.append(f"Scan {i}: {code} is not an item in this box (ALIEN ITEM).")
            continue
        if counts[fsn] >= expected_units[fsn]:
            errors.append(f"Scan {i}: {code} is an extra unit (ALIEN ITEM). FSN {fsn} already has all {expected_units[fsn]} scanned.")
            continue
        if kind == "WID":
            if seen_wids[code] >= wid_units[code]:
                errors.append(f"Scan {i}: ALIEN ITEM: WID {code} was already scanned for all its units.")
                continue
            seen_wids[code] += 1
        counts[fsn] += 1
        accepted.append({"code": code, "type": kind, "fsn": fsn, "wid": wid})

    for fsn, expected in sorted(Counter(f for f, _ in box["units"]).items()):
        if counts[fsn] != expected:
            errors.append(f"FSN {fsn}: scanned {counts[fsn]} of {expected}.")
    return accepted, errors
