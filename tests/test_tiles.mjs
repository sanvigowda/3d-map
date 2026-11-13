// Checks the tile builder the map runs in its background threads.
// Run: node --test tests/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { gunzipSync } from 'node:zlib';
import { buildTile, LOD_SIZES } from '../map3d/frontend/tiles.js';

const DATA = new URL('../map3d/frontend/data/', import.meta.url);
const manifest = JSON.parse(readFileSync(new URL('manifest.json', DATA)));
const opts = { chunk: manifest.chunk, sub: manifest.sub, q: manifest.q };
const load = (cx, cy) => { const b = gunzipSync(readFileSync(new URL(`c_${cx}_${cy}.bin`, DATA))); return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength); };

test('every building in a tile is meshed, and detail levels shrink with size', () => {
  for (const entry of manifest.chunks.filter((c) => c.nb > 3000).slice(0, 6)) {
    const out = buildTile(load(entry.cx, entry.cy), opts);
    assert.equal(out.blocks.reduce((a, b) => a + b.count, 0), entry.nb);
    for (const blk of out.blocks) {
      assert.equal(blk.lod.length, LOD_SIZES.length);
      assert.equal(blk.lod[0], blk.idx.length, 'level 0 draws every building');
      for (let j = 1; j < blk.lod.length; j++) assert.ok(blk.lod[j] <= blk.lod[j - 1], 'larger size cut-offs draw fewer triangles');
      assert.equal(blk.lod[0] % 3, 0);
      const verts = blk.pos.length / 3;
      for (let i = 0; i < blk.idx.length; i++) assert.ok(blk.idx[i] < verts);
    }
  }
});

test('buildings are ordered largest first inside each block', () => {
  const big = manifest.chunks.reduce((a, c) => (c.nb > a.nb ? c : a));
  const out = buildTile(load(big.cx, big.cy), opts);
  const blk = out.blocks.reduce((a, b) => (b.count > a.count ? b : a));
  // the first building written must be at least as tall as the last one
  const h = (v) => blk.pos[v * 3 + 1];
  assert.ok(Math.max(...Array.from({ length: 8 }, (_, k) => h(k))) >= h(blk.pos.length / 3 - 1));
  assert.ok(blk.lod[LOD_SIZES.length - 1] < blk.lod[0], 'a dense block has small buildings to skip at a distance');
});

test('roads and lamps come back as flat typed arrays', () => {
  const lit = manifest.chunks.find((c) => c.nl > 50);
  const out = buildTile(load(lit.cx, lit.cy), opts);
  assert.equal(out.lamps.length, lit.nl * 3);
  assert.ok(out.roads.idx.length > 0 && out.roads.pos.length % 3 === 0);
  assert.ok(out.transfer.length >= 4);
});
