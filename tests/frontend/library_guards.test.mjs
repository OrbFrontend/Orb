import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { JSDOM } from 'jsdom';

const dom = new JSDOM(readFileSync(new URL('../../frontend/index.html', import.meta.url), 'utf8'), { url: 'https://orb.invalid' });
globalThis.window = dom.window;
for (const name of ['document', 'Node', 'NodeFilter', 'Element', 'DocumentFragment', 'HTMLElement', 'DOMParser', 'localStorage']) globalThis[name] = dom.window[name];
window.matchMedia = () => ({ matches: false, addEventListener() {} });
window.Element.prototype.scrollTo = () => {};
window.Element.prototype.scrollIntoView = () => {};
globalThis.requestAnimationFrame = fn => setTimeout(fn, 0);
globalThis.cancelAnimationFrame = clearTimeout;
globalThis.fetch = async () => Response.json([]);
const { S } = await import('../../frontend/state.js');
const { renderCharacters } = await import('../../frontend/library_sidebar.js');
const { showCharacterBrowserModal } = await import('../../frontend/library_browser.js');
const { showCharEditModal } = await import('../../frontend/library.js');
const settle = () => new Promise(setImmediate);

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test('legacy character IDs stay literal in sidebar, browser, editor and avatar markup', async () => {
  const id = 'legacy"><img src="x" onerror="globalThis.injected = true">';
  const card = { id, name: 'Legacy', tags: [], has_avatar: true };
  S.characters = S.allCharacters = [card];
  S.conversations = [];
  renderCharacters();
  for (const el of document.querySelectorAll('#char-list [data-char-id]')) assert.equal(el.dataset.charId, id);
  assert.equal(document.querySelectorAll('#char-list [onerror]').length, 0);
  for (const view of ['grid', 'list']) {
    await showCharacterBrowserModal({ view });
    const el = document.querySelector('[data-wf-action="browser:pick"]');
    assert.equal(el.dataset.charId, id);
    assert.equal(document.querySelectorAll('[onerror]').length, 0);
    assert.ok(el.querySelector('img').getAttribute('src').includes(encodeURIComponent(id)));
  }
  showCharEditModal(card);
  for (const el of document.querySelectorAll('[data-wf-action^="library:"][data-char-id]')) assert.equal(el.dataset.charId, id);
  assert.equal(document.querySelectorAll('[onerror]').length, 0);
});

for (const action of ['searchInternet', 'randomize']) {
  test(`${action}: source switches discard stale responses without releasing the current search`, async (t) => {
    const requests = [];
    let imported;
    t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
      if (/\/characters\/(browse|randomize)\?/.test(String(url))) {
        const reply = deferred();
        requests.push({ url: new URL(url, 'https://orb.invalid'), ...reply });
        return reply.promise;
      }
      if (String(url).endsWith('/characters/import-url')) {
        imported = JSON.parse(options.body);
        return Response.json({ detail: 'test stops after verifying the source' }, { status: 400 });
      }
      return Response.json([]);
    });
    await showCharacterBrowserModal({ view: 'internet' });
    const choose = source => {
      const select = document.querySelector('[data-wf-action="browser:source"]');
      select.value = source;
      select.dispatchEvent(new window.Event('change', { bubbles: true }));
    };
    const start = () => document.querySelector(`[data-wf-action="browser:${action}"]`).click();
    choose('characterhub');
    start();
    choose('botbooru');
    start();
    assert.equal(requests.length, 2, 'a new source can search while the old source is pending');
    requests[0].resolve(Response.json({ results: [{ name: 'Wrong source', full_path: 'wrong/path' }], has_more: true }));
    await settle();
    assert.equal(document.querySelectorAll('.internet-result-card').length, 0);
    start();
    assert.equal(requests.length, 2, 'the stale completion must not clear the newer request loading flag');
    requests[1].resolve(Response.json({ results: [{ name: 'Current source', full_path: 'correct/path' }], has_more: true }));
    await settle();
    assert.match(document.getElementById('internet-results').textContent, /Current source/);
    assert.doesNotMatch(document.getElementById('internet-results').textContent, /Wrong source/);
    document.querySelector('[data-wf-action="browser:importInternet"]').click();
    await settle();
    assert.deepEqual(imported, { source: 'botbooru', full_path: 'correct/path' });

    // A late failure also cannot erase a newer completed result or paint a stale error.
    choose('characterhub');
    start();
    choose('botbooru');
    start();
    requests[3].resolve(Response.json({ results: [{ name: 'Latest', full_path: 'latest/path' }] }));
    await settle();
    requests[2].reject(new Error('obsolete request failed'));
    await settle();
    assert.match(document.getElementById('internet-results').textContent, /Latest/);
    assert.doesNotMatch(document.body.textContent, /obsolete request failed/);
  });
}

test('a failed load-more request retries the same page and original query', async (t) => {
  const pages = [];
  t.mock.method(globalThis, 'fetch', async url => {
    const parsed = new URL(url, 'https://orb.invalid');
    if (parsed.pathname.endsWith('/browse')) {
      pages.push([parsed.searchParams.get('page'), parsed.searchParams.get('q')]);
      if (pages.length === 2) return Response.json({ detail: 'retry' }, { status: 503 });
      return Response.json({ results: [{ name: `Page ${pages.length}`, full_path: 'some/path' }], has_more: true });
    }
    return Response.json([]);
  });
  await showCharacterBrowserModal({ view: 'internet' });
  document.getElementById('internet-search-input').value = 'original';
  document.querySelector('[data-wf-action="browser:searchInternet"]').click();
  await settle();
  document.getElementById('internet-search-input').value = 'unsent edit';
  document.querySelector('[data-wf-action="browser:loadMore"]').click();
  await settle();
  document.querySelector('[data-wf-action="browser:loadMore"]').click();
  await settle();
  assert.deepEqual(pages, [['1', 'original'], ['2', 'original'], ['2', 'original']]);
});
