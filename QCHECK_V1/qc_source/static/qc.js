(function (root) {
  'use strict';
  function createController(env) {
    const doc = env.document;
    const get = id => doc.getElementById(id);
    const fieldIds = ['boxId', 'packingSlip', 'tableNo', 'qcDoneBy'];
    const controls = fieldIds.concat(['btnDone', 'btnBypass']).map(get);
    const button = get('submitBtn');
    const status = get('statusMsg');
    const storageKey = 'qc-pending-submission-v1';
    let busy = false;
    let pending = null;
    let recoveryError = false;

    function message(text, kind) {
      status.textContent = text;
      status.className = kind;
    }
    function values() {
      const data = {};
      fieldIds.forEach(id => { data[id] = get(id).value.trim(); });
      data.remark = get('btnDone').checked ? 'Done' : (get('btnBypass').checked ? 'Bypass' : '');
      return data;
    }
    function valid(data) {
      return fieldIds.every(id => !!data[id]) && !!data.remark && data.boxId === data.packingSlip;
    }
    function update() {
      const data = values();
      const mismatch = data.boxId && data.packingSlip && data.boxId !== data.packingSlip;
      get('matchError').textContent = mismatch ? 'Packing Slip does not match BOX ID.' : '';
      controls.forEach(control => { control.disabled = busy || !!pending || recoveryError; });
      button.disabled = busy || recoveryError || (!pending && !valid(data));
      button.textContent = busy ? 'Saving…' : (pending ? 'Retry save' : 'Submit');
    }
    function restore() {
      try {
        const raw = env.storage.getItem(storageKey);
        if (raw) {
          const recovered = JSON.parse(raw);
          if (!recovered || typeof recovered.requestId !== 'string' || !valid(recovered)) {
            throw new Error('The saved pending entry could not be read.');
          }
          pending = recovered;
          fieldIds.forEach(id => { get(id).value = pending[id]; });
          get('btnDone').checked = pending.remark === 'Done';
          get('btnBypass').checked = pending.remark === 'Bypass';
          message('An unconfirmed entry was recovered. Use Retry save to confirm it without creating a duplicate.', 'loading');
        }
      } catch (error) {
        recoveryError = true;
        message('Browser recovery storage is unavailable or damaged. Nothing new was submitted. Ask the supervisor to check this tab before clearing browser data. ' + error.message, 'error');
      }
      update();
    }
    async function submit(event) {
      if (event) event.preventDefault();
      if (busy || recoveryError) return;
      if (!pending) {
        const data = values();
        if (!valid(data)) { update(); return; }
        data.requestId = env.makeId();
        try {
          env.storage.setItem(storageKey, JSON.stringify(data));
        } catch (error) {
          message('Entry was not sent: browser recovery storage cannot be written. Allow browser storage and try again.', 'error');
          return;
        }
        pending = data;
      }
      busy = true;
      update();
      message('Saving entry…', 'loading');
      const controller = new env.AbortController();
      const timer = env.setTimeout(() => controller.abort(), 15000);
      try {
        const response = await env.fetch('/save', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(pending), signal: controller.signal
        });
        let body;
        try { body = await response.json(); }
        catch (_) { throw new Error('Server returned HTTP ' + response.status + ' without a valid JSON response.'); }
        if (!response.ok || body.saved !== true) {
          const detail = (body.code ? body.code + ': ' : '') + (body.error || 'Save could not be confirmed.') + (body.incident ? ' Incident: ' + body.incident + '.' : '');
          // A validation rejection is known not to have written an entry.
          if (body.saved === false && response.status === 400) {
            env.storage.removeItem(storageKey);
            pending = null;
          }
          throw new Error(detail);
        }
        pending = null;
        let storageWarning = '';
        try { env.storage.removeItem(storageKey); }
        catch (_) { storageWarning = ' Browser recovery storage could not be cleared; reloading may show this already-saved entry again.'; }
        get('qcForm').reset();
        message('Saved entry #' + body.id + (body.duplicate ? ' (already saved; no duplicate created).' : '.') +
          storageWarning, storageWarning ? 'loading' : 'success');
        get('boxId').disabled = false;
        get('boxId').focus();
      } catch (error) {
        const detail = error.name === 'AbortError' ? 'The server did not respond within 15 seconds.' : error.message;
        message(detail + (pending ? ' Save is unconfirmed. Keep this tab and use Retry save; it reuses the same submission ID.' : ' Correct the entry and submit again.'), 'error');
      } finally {
        env.clearTimeout(timer);
        busy = false;
        update();
      }
    }
    fieldIds.forEach(id => get(id).addEventListener('input', update));
    ['btnDone', 'btnBypass'].forEach(id => get(id).addEventListener('change', update));
    get('qcForm').addEventListener('submit', submit);
    restore();
    return {submit, update, hasPending: () => !!pending};
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {createController};
  } else {
    let storage;
    try { storage = root.sessionStorage; }
    catch (_) { storage = {getItem() {throw new Error('Session storage is blocked.');}}; }
    const controller = createController({
      document: root.document, fetch: root.fetch.bind(root), storage,
      AbortController: root.AbortController, setTimeout: root.setTimeout.bind(root), clearTimeout: root.clearTimeout.bind(root),
      makeId() {
        const bytes = new Uint8Array(16);
        root.crypto.getRandomValues(bytes);
        return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
      }
    });
    root.addEventListener('beforeunload', event => {
      if (controller.hasPending()) { event.preventDefault(); event.returnValue = ''; }
    });
    async function health() {
      const banner = root.document.getElementById('serverStatus');
      const abort = new root.AbortController();
      const timeout = root.setTimeout(() => abort.abort(), 10000);
      try {
        const response = await root.fetch('/health', {cache: 'no-store', signal: abort.signal});
        const state = await response.json();
        if (state.database !== 'ok') throw new Error((state.code ? state.code + ': ' : '') + (state.error || 'Server health check failed.') + (state.incident ? ' Incident: ' + state.incident : ''));
        banner.className = state.warnings.length ? 'warning' : '';
        banner.textContent = state.warnings.length ? state.warnings.join('\n') :
          'Server connected. Entries save directly to the database.';
      } catch (error) {
        banner.className = 'error';
        banner.textContent = 'Server status unavailable: ' + (error.name === 'AbortError' ? 'connection timed out.' : error.message) + ' Check the server window and network.';
      } finally {
        root.clearTimeout(timeout);
        root.setTimeout(health, 15000);
      }
    }
    health();
    root.qcAppReady = true;
  }
})(typeof window === 'undefined' ? globalThis : window);
