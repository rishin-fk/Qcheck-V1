(function (root) {
  'use strict';
  const OPERATOR_KEY = 'audit-operator-v1';
  const PENDING_KEY = 'audit-pending-v1';
  const CURRENT_KEY = 'audit-current-v1';
  const FOUR_HOURS = 4 * 60 * 60 * 1000;

  function addTo(map, key, value) {
    if (!map.has(key)) map.set(key, new Set());
    map.get(key).add(value);
  }

  // Mirrors source.reconcile() in Python for live feedback. The server re-checks every audit.
  function createMatcher(groups) {
    const widFsns = new Map(), widUnits = new Map(), eanFsns = new Map(), expected = new Map();
    groups.forEach(g => {
      expected.set(g.fsn, g.expected);
      g.eans.forEach(e => addTo(eanFsns, e.toUpperCase(), g.fsn));
      g.wids.forEach(w => {
        const key = w.wid.toUpperCase();
        addTo(widFsns, key, g.fsn);
        widUnits.set(key, (widUnits.get(key) || 0) + w.units);
      });
    });
    const accepted = [], attempts = [], totals = new Map(), usedWids = new Map();

    function check(code) {
      let kind, fsn;
      if (widFsns.has(code)) {
        const fsns = widFsns.get(code);
        if (fsns.size > 1) return {ok: false, message: 'WID ' + code + ' belongs to more than one FSN in this box.'};
        kind = 'WID'; fsn = [...fsns][0];
      } else if (eanFsns.has(code)) {
        const fsns = eanFsns.get(code);
        if (fsns.size > 1) return {ok: false, message: 'EAN ' + code + ' belongs to more than one FSN in this box.'};
        kind = 'EAN'; fsn = [...fsns][0];
      } else {
        return {ok: false, message: code + ' is not an item in this box. ALIEN ITEM.'};
      }
      if ((totals.get(fsn) || 0) >= expected.get(fsn)) {
        return {ok: false, message: 'ALIEN ITEM: ' + code + ' is an extra unit. FSN ' + fsn + ' already has all ' + expected.get(fsn) + ' units scanned.'};
      }
      if (kind === 'WID' && (usedWids.get(code) || 0) >= widUnits.get(code)) {
        return {ok: false, message: 'ALIEN ITEM: WID ' + code + ' was already scanned for all its units.'};
      }
      return {ok: true, kind: kind, code: code, fsn: fsn};
    }

    function counts() {
      return new Map(totals);
    }

    return {
      scan(raw) {
        const code = String(raw).trim().toUpperCase();
        const result = check(code);
        attempts.push({code: code, kind: result.kind, fsn: result.fsn, ok: result.ok});
        if (result.ok) {
          accepted.push({code: result.code, kind: result.kind, fsn: result.fsn});
          totals.set(result.fsn, (totals.get(result.fsn) || 0) + 1);
          if (result.kind === 'WID') usedWids.set(code, (usedWids.get(code) || 0) + 1);
        }
        return result;
      },
      undo() {
        const last = attempts.pop();
        if (!last) return null;
        if (!last.ok) return last; // A rejected attempt never changed successful counts.
        accepted.pop();
        totals.set(last.fsn, totals.get(last.fsn) - 1);
        if (last.kind === 'WID') usedWids.set(last.code, usedWids.get(last.code) - 1);
        return last;
      },
      canUndo() { return attempts.length > 0; },
      codes() { return accepted.map(s => s.code); },
      counts: counts,
      fsns() { return [...expected.keys()]; },
      expected(fsn) { return expected.get(fsn) || 0; },
      complete() {
        return expected.size > 0 && [...expected.keys()].every(f => (totals.get(f) || 0) === expected.get(f));
      }
    };
  }

  // A keyboard-wedge scanner must never send its Enter suffix to a button.
  function routeScanner(doc, input, active, submit, ready) {
    function focus() {
      if (active() && ready()) input.focus({preventScroll: true});
    }
    doc.addEventListener('keydown', e => {
      if (!active() || e.ctrlKey || e.altKey || e.metaKey || e.isComposing) return;
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        e.stopImmediatePropagation();
        if (ready() && input.value.trim()) submit();
        focus();
      } else if (e.target !== input && (e.key.length === 1 || e.key === 'Backspace')) {
        e.preventDefault();
        e.stopImmediatePropagation();
        if (ready()) {
          input.value = e.key === 'Backspace' ? input.value.slice(0, -1) : input.value + e.key;
          focus();
        }
      } else if (!ready() && e.target === input) {
        e.preventDefault();
      }
    }, true);
    doc.addEventListener('focusin', e => { if (e.target !== input) focus(); });
    doc.addEventListener('pointerup', () => doc.defaultView.setTimeout(focus, 0));
    return focus;
  }

  function loadJson(store, key) {
    try { return JSON.parse(store.getItem(key)); } catch (_) { return null; }
  }
  function saveJson(store, key, value) {
    try { store.setItem(key, JSON.stringify(value)); return true; } catch (_) { return false; }
  }
  function clearKey(store, key) {
    try { store.removeItem(key); } catch (_) { /* nothing to clear */ }
  }

  function init(win) {
    const doc = win.document;
    const $ = id => doc.getElementById(id);
    const session = win.sessionStorage, local = win.localStorage;
    let currentBoxId = '', box = null, matcher = null, busy = false, activeScreen = '';
    const qcRows = new Map();
    let pending = loadJson(session, PENDING_KEY);
    let currentAudit = loadJson(session, CURRENT_KEY);

    function newId() {
      const bytes = new Uint8Array(16);
      win.crypto.getRandomValues(bytes);
      return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
    }
    function show(id) {
      activeScreen = id;
      ['screen-box', 'screen-choice', 'screen-qc', 'screen-packing', 'screen-redo'].forEach(s => $(s).classList.toggle('active', s === id));
    }
    function say(el, text, kind) {
      el.textContent = text || '';
      el.className = 'msg' + (kind ? ' ' + kind : '');
      el.hidden = !text;
    }
    function cell(text) {
      const td = doc.createElement('td');
      td.textContent = String(text);
      return td;
    }
    function row(values, className) {
      const tr = doc.createElement('tr');
      if (className) tr.className = className;
      values.forEach(v => tr.appendChild(cell(v)));
      return tr;
    }
    async function call(method, path, body) {
      const abort = new win.AbortController();
      const timeout = win.setTimeout(() => abort.abort(), 15000);
      const options = {method: method, signal: abort.signal};
      if (body) {
        options.headers = {'Content-Type': 'application/json'};
        options.body = JSON.stringify(body);
      }
      try {
        const res = await win.fetch(path, options);
        let json;
        try { json = await res.json(); } catch (_) { json = {error: 'Server returned HTTP ' + res.status + ' without JSON.'}; }
        return {ok: res.ok, status: res.status, body: json};
      } finally { win.clearTimeout(timeout); }
    }

    // Casper ID and Table No: kept on this device for 4 hours, then asked again.
    function operatorValues() {
      return {casperId: $('casperId').value.trim(), tableNo: $('tableNo').value.trim()};
    }
    function operatorStored() { return loadJson(local, OPERATOR_KEY) || {}; }
    function operatorFresh() {
      const o = operatorStored(), v = operatorValues();
      return !!o.confirmedAt && Date.now() - o.confirmedAt < FOUR_HOURS &&
        o.casperId === v.casperId && o.tableNo === v.tableNo;
    }
    function fillOperator() {
      const o = operatorStored();
      $('casperId').value = o.casperId || '';
      $('tableNo').value = o.tableNo || '';
      const expired = !o.confirmedAt || Date.now() - o.confirmedAt >= FOUR_HOURS;
      say($('operatorNote'), expired ? 'Confirm your Casper ID and Table No. They are asked again every 4 hours.' : '', 'info');
    }

    function updateRetry(text) {
      $('retryBanner').hidden = !pending;
      if (pending) $('retryNote').textContent = text || 'An unconfirmed submission is waiting. Press Retry to send it again with the same submission ID.';
      $('btnSubmitPacking').disabled = !!pending || busy;
      $('btnSubmitAudit').disabled = !!pending || busy || !matcher || !matcher.complete();
      $('btnUndo').disabled = !!pending || busy || !matcher || !matcher.canUndo();
      $('scanInput').readOnly = !!pending || busy;
      $('btnLoadBox').disabled = !!pending || busy;
      $('btnQc').disabled = !!pending || busy || !box;
      $('btnBypass').disabled = !!pending || busy;
      $('btnBackChoice').disabled = !!pending || busy;
    }

    async function loadBox() {
      if (pending || busy) return;
      const boxId = $('boxId').value.trim();
      const v = operatorValues();
      if (currentAudit) return enterPacking('Finish the Packing Slip for box ' + currentAudit.boxId + ' first.');
      if (!boxId) return say($('msg'), 'Scan or type a Box ID.', 'bad');
      if (!v.casperId || !v.tableNo) return say($('msg'), 'Casper ID and Table No are required.', 'bad');
      if (!operatorFresh()) saveJson(local, OPERATOR_KEY, {casperId: v.casperId, tableNo: v.tableNo, confirmedAt: Date.now()});
      say($('operatorNote'), '', '');
      say($('msg'), 'Loading box…', 'info');
      currentBoxId = boxId;
      busy = true;
      updateRetry();
      let r;
      try { r = await call('GET', '/api/box/' + encodeURIComponent(boxId)); }
      catch (e) { return say($('msg'), 'Box data could not be loaded: ' + e.message, 'bad'); }
      finally { busy = false; updateRetry(); }
      say($('msg'), '', '');
      if (r.ok) {
        box = r.body;
        matcher = createMatcher(r.body.groups);
        if (r.body.previous) return askRedo(r.body.previous);
        showChoice('', true);
      } else {
        box = null;
        matcher = null;
        showChoice(r.body.error || 'Box data could not be loaded. Use BYPASS.', false);
      }
    }

    function askRedo(prev) {
      $('redoText').textContent = 'Box ' + currentBoxId + ' was already audited on ' + prev.submittedAt +
        ' (Table No ' + prev.tableNo + ', Casper ID ' + prev.casperId + '). Redo QC and keep the previous audit in history?';
      show('screen-redo');
    }

    function showChoice(message, auditable) {
      $('choiceTitle').textContent = 'Box ' + currentBoxId;
      const body = $('choiceBody');
      body.textContent = '';
      if (auditable) box.groups.forEach(g => body.appendChild(row([g.fsn, g.title || '-', g.expected])));
      say($('choiceMsg'), message, auditable ? 'info' : 'bad');
      $('btnQc').disabled = !auditable;
      show('screen-choice');
    }

    function startQc() {
      if (pending || busy || !matcher) return;
      qcRows.clear();
      $('qcBody').textContent = '';
      $('qcTitle').textContent = 'QC: box ' + currentBoxId;
      $('scanInput').value = '';
      say($('scanFeedback'), '', '');
      renderQc();
      show('screen-qc');
      $('scanInput').focus();
    }
    function renderQc() {
      const counts = matcher.counts();
      const titleOf = fsn => (box.groups.find(g => g.fsn === fsn) || {}).title || '-';
      matcher.fsns().forEach(fsn => {
        let tr = qcRows.get(fsn);
        if (!tr) {
          tr = row([fsn, titleOf(fsn), 0, matcher.expected(fsn)]);
          qcRows.set(fsn, tr);
          $('qcBody').appendChild(tr);
        }
        const scanned = counts.get(fsn) || 0;
        tr.children[2].textContent = scanned;
        tr.className = scanned === matcher.expected(fsn) ? 'done' : 'short';
      });
      const done = [...counts.values()].reduce((n, count) => n + count, 0);
      $('scanCount').textContent = done + ' / ' + box.total_units;
      $('scanProgress').max = box.total_units;
      $('scanProgress').value = done;
      updateRetry();
    }
    function onScan() {
      if (pending || busy) return;
      const input = $('scanInput');
      const raw = input.value;
      input.value = '';
      if (!raw.trim() || !matcher) return;
      const r = matcher.scan(raw);
      say($('scanFeedback'), r.ok ? 'OK ' + r.code + ' counted for ' + r.fsn : r.message, r.ok ? 'ok' : 'bad');
      renderQc();
      $('scanInput').focus({preventScroll: true});
    }
    function onUndo() {
      if (pending || busy || !matcher) return;
      const last = matcher.undo();
      const text = !last ? 'Nothing to undo.' : last.ok
        ? 'Undid ' + last.code + ' (' + last.fsn + ').'
        : 'Cleared rejected scan ' + last.code + '. Successful counts unchanged.';
      say($('scanFeedback'), text, 'info');
      renderQc();
      $('scanInput').focus({preventScroll: true});
    }

    function queue(entry) {
      if (pending || busy) return;
      if (!saveJson(session, PENDING_KEY, entry)) {
        return say($('msg'), 'Browser storage is blocked, so nothing was sent. Allow browser storage and try again.', 'bad');
      }
      pending = entry;
      updateRetry();
      return sendPending();
    }
    function submitAudit() {
      if (pending || busy || !matcher || !matcher.complete()) return;
      const v = operatorValues();
      queue({kind: 'audit', path: '/api/qc_audit', body: {
        requestId: newId(), boxId: currentBoxId, tableNo: v.tableNo, casperId: v.casperId, scans: matcher.codes(), sourceRevision: box.sourceRevision}});
    }

    // Same submission ID on every retry, so a lost response cannot create a duplicate.
    async function sendPending() {
      if (!pending || busy) return;
      busy = true;
      updateRetry();
      try { await send(); } finally { busy = false; updateRetry(); }
    }
    async function send() {
      const entry = pending;
      let r;
      try {
        r = await call('POST', entry.path, entry.body);
      } catch (e) {
        return updateRetry('Not confirmed (' + e.message + '). Press Retry to send the same submission again.');
      }
      if (r.ok && r.body.saved === true) {
        pending = null;
        clearKey(session, PENDING_KEY);
        if (entry.kind === 'audit') {
          currentAudit = {auditId: r.body.auditId, boxId: r.body.boxId, tableNo: r.body.tableNo,
            casperId: r.body.casperId, submittedAt: r.body.submittedAt};
          saveJson(session, CURRENT_KEY, currentAudit);
          return enterPacking('Audit #' + currentAudit.auditId + ' saved at ' + currentAudit.submittedAt +
            '. Table No: ' + currentAudit.tableNo + '. Casper ID: ' + currentAudit.casperId +
            '. Scan the Packing Slip for box ' + currentAudit.boxId + '.');
        }
        return afterPacking('Saved entry #' + r.body.id + (r.body.duplicate ? ' (already saved, no duplicate created).' : '.'));
      }
      if (r.status >= 500 || r.ok) {
        return updateRetry('Save not confirmed (HTTP ' + r.status + '). Press Retry to send the same submission again.');
      }
      // A 4xx means the server rejected the submission, so nothing was saved.
      pending = null;
      clearKey(session, PENDING_KEY);
      const detail = (r.body.errors || []).join(' ');
      say($('msg'), (r.body.error || 'Not saved.') + (detail ? ' ' + detail : ''), 'bad');
      updateRetry();
    }

    function enterPacking(info) {
      $('screen-packing').classList.toggle('bypass', !currentAudit);
      $('packingTitle').textContent = currentAudit ? 'QC complete' : 'BYPASS';
      $('packingBox').textContent = currentAudit ? currentAudit.boxId : currentBoxId;
      $('packingSlip').value = '';
      say($('packingInfo'), info, 'info');
      say($('packingMsg'), '', '');
      show('screen-packing');
      updateRetry();
      $('packingSlip').focus();
    }
    function submitPacking() {
      if (pending || busy) return;
      const packing = $('packingSlip').value.trim();
      const qcBox = currentAudit ? currentAudit.boxId : currentBoxId;
      if (!packing) return say($('packingMsg'), 'Scan the Packing Slip.', 'bad');
      if (packing !== qcBox) return say($('packingMsg'), 'Packing Slip does not match BOX ID.', 'bad');
      const v = operatorValues();
      const body = currentAudit
        ? {requestId: newId(), boxId: currentAudit.boxId, packingSlip: packing, tableNo: currentAudit.tableNo,
          qcDoneBy: currentAudit.casperId, remark: 'Done', auditId: currentAudit.auditId}
        : {requestId: newId(), boxId: currentBoxId, packingSlip: packing, tableNo: v.tableNo,
          qcDoneBy: v.casperId, remark: 'Bypass'};
      queue({kind: 'packing', path: '/api/packing_slip', body: body});
    }
    function afterPacking(text) {
      currentAudit = null;
      clearKey(session, CURRENT_KEY);
      box = null;
      matcher = null;
      currentBoxId = '';
      $('boxId').value = '';
      say($('msg'), text, 'ok');
      show('screen-box');
      fillOperator();
      updateRetry();
      $('boxId').focus();
    }

    $('btnLoadBox').addEventListener('click', loadBox);
    $('boxId').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); loadBox(); } });
    $('btnQc').addEventListener('click', startQc);
    $('btnBypass').addEventListener('click', () => enterPacking('BYPASS: scan the Packing Slip for box ' + currentBoxId + '.'));
    $('btnBackBox').addEventListener('click', () => show('screen-box'));
    $('btnBackChoice').addEventListener('click', () => show('screen-choice'));
    routeScanner(doc, $('scanInput'), () => activeScreen === 'screen-qc', onScan, () => !pending && !busy);
    $('btnUndo').addEventListener('click', onUndo);
    $('btnSubmitAudit').addEventListener('click', submitAudit);
    routeScanner(doc, $('packingSlip'), () => activeScreen === 'screen-packing', submitPacking, () => !pending && !busy);
    $('btnSubmitPacking').addEventListener('click', submitPacking);
    $('btnRetry').addEventListener('click', sendPending);
    $('btnRedoYes').addEventListener('click', () => showChoice('Redo QC: a new audit is saved when you submit.', true));
    $('btnRedoNo').addEventListener('click', () => {
      currentBoxId = '';
      box = null;
      matcher = null;
      $('boxId').value = '';
      show('screen-box');
      $('boxId').focus();
    });

    fillOperator();
    if (currentAudit) {
      currentBoxId = currentAudit.boxId;
      enterPacking('Audit #' + currentAudit.auditId + ' is waiting. Scan the Packing Slip for box ' + currentAudit.boxId + '.');
    } else {
      show('screen-box');
    }
    if (activeScreen === 'screen-box') $('boxId').focus();
    updateRetry(pending ? 'An unconfirmed submission is waiting in this tab. Press Retry to confirm it without creating a duplicate.' : '');
    async function health() {
      try {
        const r = await call('GET', '/health');
        say($('sourceWarning'), (r.body.warnings || []).join(' '), 'bad');
      } catch (_) { /* Save/load errors are shown at the point of use. */ }
      win.setTimeout(health, 30000);
    }
    health();
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {createMatcher, routeScanner, init};
  } else {
    init(root);
  }
})(typeof window === 'undefined' ? globalThis : window);
