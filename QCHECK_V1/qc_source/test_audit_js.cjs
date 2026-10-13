'use strict';
// Run from the qc_source folder: node test_audit_js.cjs
const assert = require('assert');
const {createMatcher} = require('./static/audit.js');

const groups = [
  {fsn: 'F1', expected: 2, eans: ['111', '112'], wids: [{wid: 'W1', units: 1}, {wid: 'W2', units: 1}]},
  {fsn: 'F2', expected: 1, eans: [], wids: [{wid: 'W3', units: 1}]},
  {fsn: 'F3', expected: 3, eans: ['333'], wids: [{wid: 'W4', units: 3}]},
];

// Valid log: EAN and WID both count; repeated EAN and repeated multi-unit WID count.
let m = createMatcher(groups);
for (const code of ['111', 'w2', 'W3', '333', '333', 'W4']) assert.strictEqual(m.scan(code).ok, true, code);
assert.strictEqual(m.complete(), true);

// Single-unit WID cannot be scanned twice.
m = createMatcher(groups);
assert.strictEqual(m.scan('W1').ok, true);
const dup = m.scan('W1');
assert.strictEqual(dup.ok, false);
assert.match(dup.message, /already scanned/);

// Multi-unit WID repeats are all counted.
m = createMatcher(groups);
for (const code of ['W4', 'W4', 'W4']) assert.strictEqual(m.scan(code).ok, true);
assert.strictEqual(m.counts().get('F3'), 3);

// Alien item is rejected and not counted.
m = createMatcher(groups);
const alien = m.scan('999');
assert.strictEqual(alien.ok, false);
assert.match(alien.message, /ALIEN ITEM/);
assert.strictEqual(m.codes().length, 0);

// Undo removes the last accepted scan.
m = createMatcher(groups);
m.scan('111');
m.scan('W3');
assert.strictEqual(m.undo().code, 'W3');
assert.strictEqual(m.counts().get('F2') || 0, 0);

// Not complete until every FSN total matches.
m = createMatcher(groups);
m.scan('111');
m.scan('W2');
m.scan('W3');
assert.strictEqual(m.complete(), false);

// EAN shared by two FSNs in the same box is rejected.
const shared = createMatcher([
  {fsn: 'A', expected: 1, eans: ['5'], wids: [{wid: 'X', units: 1}]},
  {fsn: 'B', expected: 1, eans: ['5'], wids: [{wid: 'Y', units: 1}]},
]);
const sharedResult = shared.scan('5');
assert.strictEqual(sharedResult.ok, false);
assert.match(sharedResult.message, /more than one FSN/);

// A repeated WID after its FSN is complete is an alien, not an "already scanned" error.
m = createMatcher(groups);
m.scan('W3');
const repeatAfterComplete = m.scan('W3');
assert.strictEqual(repeatAfterComplete.ok, false);
assert.match(repeatAfterComplete.message, /ALIEN ITEM/);

// Scans beyond the expected count are aliens (FSN F3 expects 3).
m = createMatcher(groups);
for (const code of ['333', '333', '333']) assert.strictEqual(m.scan(code).ok, true, code);
const extra = m.scan('W4');
assert.strictEqual(extra.ok, false);
assert.match(extra.message, /extra unit/);
assert.strictEqual(m.counts().get('F3'), 3);

console.log('audit matcher tests OK');
