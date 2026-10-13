# QCheck

QCheck loads a box, checks its item scans, and records the packing slip directly in SQLite. Source data refreshes continuously while the server is running. The landing page fits desktop and handheld screens; QC is green and BYPASS is red.

## Start

From this folder:

```bat
python -m pip install -r requirements.txt
python app.py
```

Or double-click `START_QCHECK.cmd` after installing the requirements. Keep the server window open. The window prints the local and network URLs (port 5000 by default). Starting the app also starts source refresh; a separate builder process is unnecessary.

The default input/output layout is:

```text
QCHECK/
  Container-Box-Details/box_items.db
  TL_NCOB/FSN_EAN_WID_Title.db
  qc_source/
    app.py
    build_source.py
    source.py
    storage.py
    qc_source.db
    qc_data.db
    static/audit.js
    static/audit.css
    static/qc.js
    templates/audit.html
    templates/index.html
```

For a different deployment layout, set `QC_ITEMS_DB` and `QC_FSN_DB` to the actual full source paths before starting. `QC_SOURCE_DB` sets the source output path. `QC_DATA_DIR` sets the folder for audit/packing records and logs. `QC_SOURCE_INTERVAL` sets the refresh check interval in seconds (default 5; allowed 0.5–3600). `QC_HOST` and `QC_PORT` set the server address/port. Source and record databases must be different files.

## Continuous source history

The updater reads the fetcher databases read-only, including committed WAL changes. It detects file/WAL changes and appends changed box and FSN/EAN versions in a transaction. An unchanged pass adds no history rows. A failed pass keeps the last complete revision and retries; `/health` and the page report refresh problems.

Existing `qc_source.db` data is retained. On the first successful refresh, its original `box_units` / `fsn_codes` tables are captured as a legacy baseline in the new history tables. Those original tables stay intact; the app reads the revision tables after migration.

- `source_sync`: import revision, timestamp and input paths.
- `box_versions` / `box_version_units`: complete unit snapshots for each changed box. Duplicate source rows count as separate physical units.
- `fsn_versions` / `fsn_version_codes`: EAN mapping snapshots for each changed FSN.
- `current_box_units` / `current_fsn_codes`: SQL views for inspecting the latest effective data. `current_box_items` gives box, fsn, ean, wid and product title for each unit (EANs comma-joined).

History tables reject UPDATE and DELETE with database triggers. A changed box gets a new complete snapshot: its previous and current counts are not added together. Boxes and FSNs absent from a later input remain available at their last known version. There is no automatic history cleanup.

Each loaded box carries a source revision. Server validation uses that same revision throughout the audit, including retries, so a refresh during scanning cannot change that audit's expected contents. The revision is recorded in `qc_audit.source_revision`. A new box load uses the latest committed revision.

For a standalone continuous updater, run `python build_source.py`. Optional arguments: `--items PATH --fsn PATH --out PATH --interval 5`. Use `--once` to intentionally perform one refresh and exit. Ctrl+C stops continuous operation. The upstream fetchers keep their existing schedules; QCheck follows their database changes.

## Scan and save

1. Enter/scan Box ID, Casper ID and Table No. Operator details stay editable and are remembered for four hours.
2. Choose QC or BYPASS. Missing/unauditable boxes can use BYPASS.
3. In QC, scan EANs or WIDs. A unique WID counts once. A WID shared by multiple units can repeat up to that WID's unit count. EANs can repeat until the FSN's expected total is reached. WID and EAN scans can be mixed. Repeated/excess/unknown codes are rejected as ALIEN ITEM; ambiguous codes are rejected too.
4. Submit QC once all FSN totals match, then scan the matching Packing Slip. BYPASS goes straight to this packing step.
5. Save returns to the next box with Box ID focused.

Scan feedback runs locally without a server call per item. On the QC and packing screens, keyboard-scanner characters and Enter/Tab suffixes go to the scan field even after another control receives focus. Undo removes the last accepted scan; rejected scans never increment counts. The scan field suppresses the handheld software keyboard while remaining editable with a physical keyboard/scanner.

Save confirmation is sent after the SQLite commit. Network timeouts retain the submission ID for Retry save; a repeated request cannot create another entry. The server can confirm an already committed audit even when source data is temporarily unavailable. Redo adds a new audit and preserves the previous audit and scans.

## Databases and maintenance

`qc_data.db` stores packing entries in `qc_entries`, audit headers in `qc_audit`, and item scans in `qc_audit_scans`. The app uses SQLite WAL and FULL durability. Excel generation, export workers, Excel status messages, and the XlsxWriter dependency have been removed from this app. Existing Excel files and old export metadata remain intact.

For a consistent record database backup while the server runs:

```bat
python app.py --backup
```

This backs up `qc_data.db` into `backups/` and verifies its integrity. To back up source history, use SQLite's backup API for `qc_source.db`, or stop all source writers before copying it together with any WAL/SHM sidecars. Keep active databases on the server's local disk.

## Validation

```bat
python -m unittest discover -p "test_*.py" -q
node test_audit_js.cjs
node frontend.cjs
```

Validated locally with synthetic data: 28 backend tests, matcher/scanner routing checks, classic-form retry checks, desktop/390px handheld layouts, QC and BYPASS saves, packing-slip mismatch blocking, and Undo followed by scanning. Tests cover history preservation, legacy migration, failed refresh recovery, live WAL updates, stable in-progress audits, backups and concurrent retries.

Local HTTP measurements used Waitress 3.0.2 with 32 threads and a synthetic five-FSN/ten-unit box. Median box load was 5.06 ms; audit save 7.96 ms; packing save 3.48 ms. With ten concurrent clients, 30 BYPASS saves had median 39.27 ms and p95 60.62 ms. These are Windows localhost observations, not a microsecond guarantee or measurements of the actual handheld/network. Physical scanner behavior and deployment throughput still require verification on the target device.
