import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createDocumentSaveQueue } from '../../frontend/document_saves.js';

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test('a slow save coalesces newer snapshots and acknowledges Saved only for the latest', async () => {
  const first = deferred(), next = deferred(), admitted = deferred();
  const bodies = [], acknowledgements = [];
  const queue = createDocumentSaveQueue({ id: 'A', revision: 0 }, {
    put: body => { bodies.push(body); if (bodies.length === 1) { admitted.resolve(); return first.promise; } return next.promise; },
    acknowledged: (row, snapshot, latest) => acknowledgements.push({ row, snapshot, latest }),
  });
  const draining = queue.save({ content: 'old', generated_spans: [] });
  await admitted.promise;
  queue.save({ content: 'middle', generated_spans: [] });
  const final = queue.save({ content: 'newest', generated_spans: [{ start: 0, end: 6 }] });
  first.resolve({ id: 'A', revision: 1, content: 'old' });
  await Promise.resolve(); await Promise.resolve();
  assert.equal(bodies.length, 2);
  assert.equal(bodies[1].content, 'newest');
  assert.equal(bodies[1].expected_revision, 1);
  assert.equal(acknowledgements[0].latest, false);
  next.resolve({ id: 'A', revision: 2, content: 'newest' });
  await Promise.all([draining, final]);
  assert.equal(acknowledgements.at(-1).latest, true);
  assert.equal(queue.row.content, 'newest');
});

test('a conflict retains the local draft and blocks writes until explicit rebase', async () => {
  const error = Object.assign(new Error('conflict'), { status: 409 });
  const bodies = [];
  let conflict = true;
  const failures = [];
  const queue = createDocumentSaveQueue({ id: 'A', revision: 0 }, {
    put: async body => { bodies.push(body); if (conflict) throw error; return { id: 'A', revision: 5, content: body.content }; },
    failed: failure => failures.push(failure),
  });
  await assert.rejects(queue.save({ content: 'local' }), /conflict/);
  await assert.rejects(queue.save({ content: 'newer local' }), /conflict/);
  assert.equal(queue.draft.content, 'newer local');
  assert.equal(bodies.length, 1);
  // The blocked save re-offers the conflict instead of failing silently.
  assert.deepEqual(failures, [error, error]);
  queue.rebase({ id: 'A', revision: 4, content: 'remote' });
  conflict = false;
  await queue.save(queue.draft);
  assert.equal(bodies[1].expected_revision, 4);
  assert.equal(queue.row.content, 'newer local');
});

test('Reload discards a conflicting draft so a later rename cannot submit it', async () => {
  const bodies = [];
  let conflicted = true;
  const queue = createDocumentSaveQueue({ id: 'A', revision: 0 }, {
    put: async body => {
      bodies.push(body);
      if (conflicted) throw Object.assign(new Error('conflict'), { status: 409 });
      return { ...queue.row, title: body.title, revision: 5 };
    },
  });
  await assert.rejects(queue.save({ content: 'discarded local draft' }), /conflict/);
  queue.discard({ id: 'A', revision: 4, content: 'remote saved text' });
  conflicted = false;
  await queue.save({ title: 'Renamed' });
  assert.deepEqual(bodies[1], { title: 'Renamed', expected_revision: 4 });
  assert.equal(queue.row.content, 'remote saved text');
});
