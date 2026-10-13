const assert = require('node:assert/strict');
const {createController} = require('./static/qc.js');

function setup(storage = new Map()) {
  const nodes = {};
  for (const id of ['boxId','packingSlip','tableNo','qcDoneBy','btnDone','btnBypass','submitBtn','statusMsg','matchError','qcForm']) {
    nodes[id] = {value: '', checked: false, disabled: false, listeners: {}, focus() {}, addEventListener(name, fn) {this.listeners[name] = fn;}};
  }
  Object.assign(nodes.boxId, {value: '0001'});
  Object.assign(nodes.packingSlip, {value: '0001'});
  Object.assign(nodes.tableNo, {value: '01'});
  Object.assign(nodes.qcDoneBy, {value: 'Test'});
  nodes.btnDone.checked = true;
  nodes.qcForm.reset = () => Object.values(nodes).forEach(n => {n.value = ''; n.checked = false;});
  const requests = [];
  const controller = createController({
    document: {getElementById: id => nodes[id]},
    storage: {getItem: k => storage.get(k), setItem: (k,v) => storage.set(k,v), removeItem: k => storage.delete(k)},
    makeId: () => 'test-submission-12345678', AbortController,
    setTimeout: () => 1, clearTimeout() {},
    fetch: (_, options) => new Promise((resolve, reject) => requests.push({payload: JSON.parse(options.body), resolve, reject}))
  });
  return {nodes, requests, controller, storage};
}

(async () => {
  const a = setup();
  const first = a.controller.submit();
  a.controller.update();
  assert.equal(a.nodes.submitBtn.disabled, true);
  assert.equal(a.nodes.boxId.disabled, true);
  assert.equal(a.nodes.btnDone.disabled, true);
  await a.controller.submit();
  assert.equal(a.requests.length, 1, 'duplicate click while saving must not send another request');
  a.requests[0].reject(new Error('Network disconnected'));
  await first;
  assert.equal(a.nodes.submitBtn.textContent, 'Retry save');
  assert.equal(a.nodes.boxId.disabled, true);
  const previous = a.requests[0].payload;
  const restored = setup(a.storage);
  assert.equal(restored.controller.hasPending(), true);
  const retry = restored.controller.submit();
  assert.deepEqual(restored.requests[0].payload, previous);
  restored.requests[0].resolve({ok: true, status: 200, json: async () => ({saved: true, id: 5, duplicate: true})});
  await retry;
  assert.equal(restored.storage.size, 0);
  assert.equal(restored.nodes.boxId.value, '');
  assert.equal(restored.nodes.boxId.disabled, false);
  assert.match(restored.nodes.statusMsg.textContent, /no duplicate/);

  const b = setup();
  const rejected = b.controller.submit();
  b.requests[0].resolve({ok: false, status: 400, json: async () => ({saved: false, error: 'Bad input', code: 'INVALID_INPUT'})});
  await rejected;
  assert.equal(b.nodes.boxId.disabled, false);
  assert.equal(b.nodes.boxId.value, '0001');
  assert.equal(b.storage.size, 0);

  const c = setup();
  const invalidResponse = c.controller.submit();
  c.requests[0].resolve({ok: false, status: 502, json: async () => {throw new Error('HTML');}});
  await invalidResponse;
  assert.equal(c.controller.hasPending(), true);
  assert.match(c.nodes.statusMsg.textContent, /HTTP 502/);
  console.log('Frontend checks passed: submission lock, safe retry, reload recovery, validation rejection, non-JSON error.');
})().catch(error => {console.error(error); process.exitCode = 1;});
