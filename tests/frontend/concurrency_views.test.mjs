import assert from 'node:assert/strict';
import { test } from 'node:test';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';

const dom = new JSDOM(readFileSync(new URL('../../frontend/index.html', import.meta.url), 'utf8'), { url: 'https://orb.invalid' });
globalThis.window = dom.window;
for (const name of ['document', 'Node', 'NodeFilter', 'Element', 'DocumentFragment', 'HTMLElement', 'DOMParser', 'localStorage']) globalThis[name] = dom.window[name];
window.matchMedia = () => ({ matches: false, addEventListener() {} });
window.Element.prototype.scrollTo = () => {};
window.Element.prototype.scrollIntoView = () => {};
globalThis.requestAnimationFrame = fn => setTimeout(fn, 0);
globalThis.cancelAnimationFrame = clearTimeout;
globalThis.fetch = async () => Response.json({});
const { S } = await import('../../frontend/state.js');
const { selectConversation } = await import('../../frontend/chat_conversations.js');
const { canStartGeneration } = await import('../../frontend/chat_core.js');
const documents = await import('../../frontend/document.js');

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test('A to B to A selection rejects both older responses and blocks sending during history load', async (t) => {
  S.conversations = [{ id: 'a', title: 'A' }, { id: 'b', title: 'B' }];
  const reads = [deferred(), deferred(), deferred()];
  const entered = [deferred(), deferred(), deferred()];
  let n = 0;
  t.mock.method(globalThis, 'fetch', async url => {
    if (String(url).endsWith('/messages')) { const i = n++; entered[i].resolve(); return reads[i].promise; }
    return Response.json(String(url).endsWith('/worlds') ? { world_ids: [] } : {});
  });
  const oldA = selectConversation('a');
  await entered[0].promise;
  assert.equal(canStartGeneration(), false);
  assert.equal(document.getElementById('send-btn').disabled, true);
  document.getElementById('chat-input').value = 'A draft';
  const oldB = selectConversation('b');
  await entered[1].promise;
  const latestA = selectConversation('a');
  await entered[2].promise;
  reads[2].resolve(Response.json([{ id: 3, role: 'user', content: 'latest A' }]));
  await latestA;
  reads[1].resolve(Response.json([{ id: 2, role: 'user', content: 'old B' }]));
  reads[0].resolve(Response.json([{ id: 1, role: 'user', content: 'old A' }]));
  await Promise.all([oldA, oldB]);
  assert.equal(S.activeConvId, 'a');
  assert.equal(S.messages[0]?.content, 'latest A', document.body.textContent.slice(-1800));
  assert.equal(document.getElementById('chat-input').value, 'A draft');
  assert.equal(document.getElementById('send-btn').disabled, false);
});

test('opening a chat with inline Inspector loads saved replies and their logs', async (t) => {
  const inline = S.settings.inspector_inline;
  S.settings.inspector_inline = true;
  t.after(() => { S.settings.inspector_inline = inline; });
  S.conversations = [{ id: 'inline-chat', title: 'Inline chat' }];
  const messages = [
    { id: 20, role: 'assistant', content: 'First saved reply.' },
    { id: 21, role: 'user', content: 'Continue.' },
    { id: 22, role: 'assistant', content: 'Latest saved reply.' },
  ];
  const logs = {
    20: { reasoning_writer: 'First saved thoughts.' },
    22: { reasoning_writer: 'Latest saved thoughts.' },
  };
  const reads = [];
  t.mock.method(globalThis, 'fetch', async url => {
    const path = String(url);
    reads.push(path);
    const body = path.endsWith('/messages') ? messages
      : path.includes('/director-logs?') ? logs
      : path.endsWith('/director-log') ? logs[22]
      : path.endsWith('/worlds') ? { world_ids: [] } : {};
    return Response.json(body);
  });

  await selectConversation('inline-chat');
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(S.conversationLoading, false, document.getElementById('toast-stack').textContent);
  assert.equal(document.getElementById('chat-input').disabled, false);
  assert.equal(document.getElementById('send-btn').disabled, false);
  assert.deepEqual(S.messages.map(message => message.id), [20, 21, 22]);
  assert.ok(reads.includes('/api/conversations/inline-chat/director-logs?ids=20,22'));
  const firstReply = document.querySelector('.message[data-msg-id="20"]');
  assert.match(firstReply.textContent, /First saved reply\./);
  assert.match(firstReply.querySelector('.msg-reasoning').textContent, /First saved thoughts\./);
});

test('dirty Generate reserves one run before saving and Stop blocks a replacement until settled', async (t) => {
  let row = { id: 'doc-a', title: 'A', revision: 0, content: 'Start', generated_spans: [] };
  S.documents = [row];
  const saved = deferred(), saving = deferred(), started = deferred(), stopping = deferred(), stopReply = deferred();
  let generateCalls = 0;
  let savingPaused = true;
  let controller;
  const encoder = new TextEncoder();
  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    const path = String(url).split('?')[0];
    if (options.method === 'PUT') {
      const body = JSON.parse(options.body);
      assert.equal(body.expected_revision, row.revision);
      if (savingPaused) { saving.resolve(); await saved.promise; }
      row = { ...row, ...body, revision: row.revision + 1 };
      return Response.json(row);
    }
    if (path.endsWith('/generate')) {
      generateCalls++;
      const body = new ReadableStream({ start(value) { controller = value; } });
      started.resolve();
      return new Response(body);
    }
    if (path.endsWith('/stop')) { if (!generateCalls) return Response.json({ active: false, settled: true }); stopping.resolve(); return stopReply.promise; }
    return Response.json(row);
  });
  await documents.openDocument(row.id);
  document.getElementById('doc-page').textContent = 'Draft';
  S.docDirty = true;
  const first = documents.docGenerate();
  await saving.promise;
  const duplicate = documents.docGenerate();
  documents.docStop();
  savingPaused = false;
  saved.resolve();
  await Promise.all([first, duplicate]);
  assert.equal(generateCalls, 0, 'Stop before the dirty save finishes starts no generation');
  assert.equal(row.content, 'Draft');
  const run = documents.docGenerate();
  await started.promise;
  documents.docStop();
  await stopping.promise;
  await documents.docGenerate();
  assert.equal(generateCalls, 1, 'Stop still owns the document while settlement is pending');
  controller.enqueue(encoder.encode('event: token\ndata:  kept\n\nevent: done\ndata: {}\n\n'));
  controller.close();
  stopReply.resolve(Response.json({ active: true, settled: true }));
  await run;
  assert.equal(S.docStreaming, false);
  assert.match(row.content, /kept/);
  assert.equal(document.getElementById('doc-save-state').textContent, 'Saved');
  assert.equal(localStorage.getItem('orb-doc-draft:doc-a'), null);
});

test('a closed duplicate scan settles before a reopened panel can start another', async (t) => {
  const { dedupeToolHtml, mountLibraryDedupe, unmountLibraryDedupe } = await import('../../frontend/library_dedupe.js');
  const started = deferred(), stopping = deferred(), reply = deferred();
  let channel;
  t.mock.method(globalThis, 'fetch', async url => {
    if (String(url).includes('/scan')) {
      started.resolve();
      return new Response(new ReadableStream({ start(controller) { channel = controller; } }));
    }
    if (String(url).includes('/stop')) { stopping.resolve(); return reply.promise; }
    return Response.json({});
  });
  const old = document.createElement('div');
  old.innerHTML = dedupeToolHtml();
  document.body.appendChild(old);
  mountLibraryDedupe(old, { characterCount: 2 });
  old.querySelector('[data-dupe-action="scan"]').click();
  await started.promise;
  const operation = [...S.operations.values()].find(op => op.kind === 'duplicate-scan');
  unmountLibraryDedupe();
  await stopping.promise;
  const next = document.createElement('div');
  next.innerHTML = dedupeToolHtml();
  document.body.appendChild(next);
  mountLibraryDedupe(next, { characterCount: 2 });
  assert.equal(next.querySelector('[data-dupe-action="scan"]').disabled, true);
  channel.enqueue(new TextEncoder().encode('event: done\ndata: {"groups":[{"name":"old results"}]}\n\n'));
  channel.close();
  reply.resolve(Response.json({ active: true, settled: true }));
  await operation.completion;
  assert.equal(next.querySelector('[data-dupe-action="scan"]').disabled, false);
  assert.doesNotMatch(next.textContent, /old results/);
  unmountLibraryDedupe();
  old.remove(); next.remove();
});

test('a peer tab media notice repaints without echoing back to the sender', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] });
  const posted = [];
  class FakeChannel {
    constructor() { FakeChannel.last = this; }
    postMessage(message) { posted.push(message.type); }
  }
  const original = globalThis.BroadcastChannel;
  globalThis.BroadcastChannel = FakeChannel;
  t.after(() => { globalThis.BroadcastChannel = original; });
  const { initTabLock } = await import('../../frontend/tabLock.js');
  const { initWorkflowMutationListener } = await import('../../frontend/chat_workflow.js');
  initTabLock();
  initWorkflowMutationListener();
  t.mock.method(globalThis, 'fetch', async () => Response.json([{ id: 93, role: 'user', content: 'peer render' }]));
  S.activeConvId = 'a';
  S.documentMode = false;
  S.conversationViewToken++;
  posted.length = 0;
  FakeChannel.last.onmessage({ data: { type: 'WORKFLOW_MUTATION', tabId: 'peer', payload: { convId: 'a', msgId: 93 } } });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(S.messages[0].id, 93);
  assert.ok(!posted.includes('WORKFLOW_MUTATION'));
});

test('media refresh across A to B to A leaves the newer view alone', async (t) => {
  const { refreshConversationMessages } = await import('../../frontend/chat_workflow.js');
  const fetched = deferred();
  t.mock.method(globalThis, 'fetch', () => fetched.promise);
  S.activeConvId = 'a';
  S.documentMode = false;
  S.conversationViewToken++;
  S.messages = [{ id: 91, role: 'user', content: 'earlier A' }];
  const refresh = refreshConversationMessages(91, 'a');
  S.activeConvId = 'b'; S.conversationViewToken++;
  S.activeConvId = 'a'; S.conversationViewToken++;
  S.messages = [{ id: 92, role: 'user', content: 'current A' }];
  fetched.resolve(Response.json([{ id: 91, role: 'user', content: 'old response' }]));
  await refresh;
  assert.equal(S.messages[0].id, 92);
});

test('saved inspection leaves live reasoning and pass selection intact', async (t) => {
  const { inspectMessage, selectReasoningPass } = await import('../../frontend/chat_inspector.js');
  S.activeConvId = 'a';
  S.reasoningWriter = 'live writer';
  S.reasoningDirector = 'live director';
  S.reasoningPassSelected = 1;
  S.reasoningUserOverride = false;
  t.mock.method(globalThis, 'fetch', async () => Response.json({ reasoning_writer: 'saved thinking' }));
  await inspectMessage(91);
  selectReasoningPass(0);
  assert.equal(S.inspectedReasoning.writer, 'saved thinking');
  assert.equal(S.reasoningWriter, 'live writer');
  assert.equal(S.reasoningDirector, 'live director');
  assert.equal(S.reasoningPassSelected, 1);
  assert.equal(S.reasoningUserOverride, false);
});

test('compression Cancel and a late summary never stop or replace an unrelated reply', async (t) => {
  const compression = await import('../../frontend/chat_conversations.js');
  const { begin, finish } = await import('../../frontend/operations.js');
  const started = deferred(), stopping = deferred(), stopped = deferred();
  const calls = [];
  let channel;
  t.mock.method(globalThis, 'fetch', async url => {
    const path = new URL(String(url), 'https://orb.invalid');
    calls.push(path);
    if (path.pathname.endsWith('/summarize')) {
      started.resolve();
      return new Response(new ReadableStream({ start(value) { channel = value; } }));
    }
    if (path.pathname.endsWith('/stop')) { stopping.resolve(); return stopped.promise; }
    return Response.json({});
  });
  S.activeConvId = 'compress-a';
  S.messages = Array.from({ length: 6 }, (_, id) => ({ id, role: 'user', content: 'Earlier story' }));
  S.isStreaming = false;
  compression.showCompressModal();
  const run = compression.generateCompressionSummary();
  await started.promise;
  const summary = [...S.operations.values()].find(op => op.kind === 'compression');
  S.activeConvId = 'reply-b';
  S.isStreaming = true;
  const reply = begin('chat', { conversationId: 'reply-b' });
  let unrelatedStops = 0;
  reply.stop = () => { unrelatedStops++; };
  compression.cancelCompression();
  await stopping.promise;
  assert.equal(unrelatedStops, 0);
  assert.equal(calls.at(-1).pathname, '/api/conversations/compress-a/stop');
  assert.equal(calls.at(-1).searchParams.get('operation_id'), summary.id);
  channel.enqueue(new TextEncoder().encode('event: token\ndata: partial summary\n\nevent: done\ndata: {}\n\n'));
  channel.close();
  stopped.resolve(Response.json({ active: true, settled: true }));
  await run;
  assert.equal(S.activeConvId, 'reply-b');
  assert.equal(S.isStreaming, true);
  assert.equal(document.getElementById('modal-root').textContent, '');
  assert.equal(S.operations.get(reply.id), reply);
  finish(reply);
  S.isStreaming = false;
});

test('saving an applied patch keeps the audit run current for another patch', async (t) => {
  documents.initDocumentMode();
  const audit = await import('../../frontend/document_audit.js');
  let row = { id: 'audit-a', title: 'A', revision: 4, content: 'First draft.', generated_spans: [] };
  let patches = 0;
  t.mock.method(globalThis, 'fetch', async (url, opts = {}) => {
    const path = String(url);
    if (path.endsWith('/patch')) {
      patches++;
      return Response.json({ patch_count: 1, patched_draft: `Patched ${patches}.`, report_after: { total_issues: 1 } });
    }
    if (opts.method === 'PUT') {
      const body = JSON.parse(opts.body);
      row = { ...row, ...body, revision: row.revision + 1 };
      return Response.json(row);
    }
    return Response.json(row);
  });
  S.docDirty = false;
  S.documents = [row];
  await documents.openDocument('audit-a');
  S.docAuditResults = { docId: 'audit-a', runStart: 0, draft: 'First draft.', report: { total_issues: 1 } };
  S.docAuditBusy = false;
  await audit.runPatch();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(S.documentSessions.get('audit-a').row.revision, 5);
  await audit.runPatch();
  assert.equal(patches, 2);
  assert.equal(document.getElementById('doc-page').textContent, 'Patched 2.');
});

test('a delayed patch cannot edit another document with identical text and offset', async (t) => {
  documents.initDocumentMode();
  const audit = await import('../../frontend/document_audit.js');
  const entered = deferred(), patched = deferred();
  const content = 'A shared sentence.';
  const rows = {
    'patch-a': { id: 'patch-a', title: 'A', revision: 4, content, generated_spans: [] },
    'patch-b': { id: 'patch-b', title: 'B', revision: 4, content, generated_spans: [] },
  };
  t.mock.method(globalThis, 'fetch', async url => {
    const path = String(url);
    if (path.endsWith('/patch')) { entered.resolve(); return patched.promise; }
    return Response.json(rows[path.split('/').at(-1)] || {});
  });
  S.docDirty = false;
  S.documents = Object.values(rows);
  await documents.openDocument('patch-a');
  const run = { docId: 'patch-a', runStart: 0, draft: content, report: { total_issues: 1 } };
  S.docAuditResults = run;
  S.docAuditBusy = false;
  const patch = audit.runPatch();
  await entered.promise;
  await documents.openDocument('patch-b');
  patched.resolve(Response.json({ patch_count: 1, patched_draft: 'Rewritten A.', report_after: { total_issues: 0 } }));
  await patch;
  assert.equal(S.activeDocId, 'patch-b');
  assert.equal(document.getElementById('doc-page').textContent, content);
  assert.equal(S.docDirty, false);
  assert.equal(S.documentSessions.get('patch-b').row.revision, 4);
});

test('state written with no chat selected never seeds a later conversation', async () => {
  const { conversationState } = await import('../../frontend/state.js');
  S.activeConvId = null;
  S.queuedEdits[7] = 'idle';
  S.activeWorldIds.add('idle-world');
  assert.deepEqual(conversationState('fresh-after-idle').queuedEdits, {});
  assert.equal(conversationState('fresh-after-idle').activeWorldIds.size, 0);
  delete S.queuedEdits[7];
  S.activeWorldIds.clear();
});

test('a full local draft store never blocks the document save it backs up', async (t) => {
  documents.initDocumentMode();
  let row = { id: 'quota-doc', title: 'Q', revision: 1, content: 'Saved.', generated_spans: [] };
  const puts = [];
  t.mock.method(globalThis, 'fetch', async (url, opts = {}) => {
    if (opts.method === 'PUT') {
      puts.push(JSON.parse(opts.body));
      row = { ...row, ...puts.at(-1), revision: row.revision + 1 };
    }
    return Response.json(row);
  });
  S.docDirty = false;
  S.documents = [row];
  await documents.openDocument('quota-doc');
  t.mock.method(window.Storage.prototype, 'setItem', () => {
    throw new window.DOMException('full', 'QuotaExceededError');
  });
  document.getElementById('doc-page').textContent = 'Edited.';
  S.docDirty = true;
  document.getElementById('doc-page').dispatchEvent(new window.Event('blur'));
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(puts.at(-1)?.content, 'Edited.');
  assert.equal(S.docDirty, false);
});

test('a conversation left behind drops its retained view unless it still holds work or intent', async () => {
  const { conversationState, releaseConversationState } = await import('../../frontend/state.js');
  S.activeConvId = 'still-open';
  conversationState('still-open');
  conversationState('idle-left').messages = [{ id: 1, role: 'user', content: 'hi' }];
  Object.assign(conversationState('drafted-left'), { draft: 'half a thought' });
  for (const cid of ['idle-left', 'drafted-left', 'still-open']) releaseConversationState(cid);
  assert.equal(S.conversationStates.has('idle-left'), false);
  assert.equal(S.conversationStates.has('drafted-left'), true);
  assert.equal(S.conversationStates.has('still-open'), true);
});
