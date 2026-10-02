import assert from 'node:assert/strict';
import { test } from 'node:test';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';

const dom = new JSDOM(readFileSync(new URL('../../frontend/index.html', import.meta.url), 'utf8'), { url: 'https://orb.invalid' });
globalThis.window = dom.window;
for (const name of ['document', 'Node', 'Element', 'HTMLElement', 'localStorage']) globalThis[name] = dom.window[name];

let cards = [];
globalThis.fetch = async (url) => Response.json(String(url).includes('/characters') ? cards : []);
const { S } = await import('../../frontend/state.js');
const { loadCharacters, refreshCharacters } = await import('../../frontend/library_sidebar.js');

const card = (id, updated_at) => ({ id, name: id.toUpperCase(), updated_at });
const panel = () => [...document.querySelectorAll('#char-list .char-item-name')].map((el) => el.textContent);

test('the character panel never reorders before a page refresh', async () => {
  cards = ['a', 'b', 'c', 'd', 'e', 'f'].map((id, i) => card(id, `2026-01-0${9 - i}`));
  S.conversations = [];
  await loadCharacters();
  assert.deepEqual(panel(), ['A', 'B', 'C', 'D', 'E']);

  // Chatting with E would rank it first, and an edit renames C.
  S.conversations = [{ id: 'x', character_card_id: 'e', updated_at: '2026-09-01' }];
  cards = cards.map((c) => (c.id === 'c' ? { ...c, name: 'C2', updated_at: '2026-09-02' } : c));
  refreshCharacters();
  assert.deepEqual(panel(), ['A', 'B', 'C', 'D', 'E']);
  await loadCharacters();
  assert.deepEqual(panel(), ['A', 'B', 'C2', 'D', 'E']);

  // A new card waits for a free slot; a deleted card frees one at the bottom.
  cards = [card('g', '2026-10-01'), ...cards.filter((c) => c.id !== 'b')];
  await loadCharacters();
  assert.deepEqual(panel(), ['A', 'C2', 'D', 'E', 'G']);
});
