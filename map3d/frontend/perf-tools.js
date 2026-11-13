// Live performance readout and a scripted benchmark, so frame rates can be
// measured on the actual laptop or phone the map will run on.
//   ?stats=1   show the readout          ?bench=1   run the benchmark on load
// Both are also in the About panel.

import { escapeHtml } from './engine.js';

// time (s), x, y (metres from the city centre), camera distance (m), heading, pitch
const PATH = [
  [0, 3199, -3968, 600, -20, 66],      // street level, Koramangala
  [6, 3199, -3968, 600, 40, 66],       // turn around
  [10, 3199, -3968, 1800, 40, 56],     // pull back to the neighbourhood
  [15, 1341, 418, 1300, 28, 58],       // across to MG Road
  [20, 1300, -1500, 4500, 10, 50],     // district view
  [24, 1000, 1000, 48000, 0, 14],      // whole city
];
const DURATION = PATH[PATH.length - 1][0];

const smooth = (t) => t * t * (3 - 2 * t);
function pathAt(t) {
  let i = 0;
  while (i < PATH.length - 2 && PATH[i + 1][0] <= t) i++;
  const a = PATH[i], b = PATH[i + 1];
  const k = smooth(Math.min(1, Math.max(0, (t - a[0]) / (b[0] - a[0]))));
  const lerp = (u, v) => u + (v - u) * k;
  return { x: lerp(a[1], b[1]), y: lerp(a[2], b[2]), distance: Math.exp(lerp(Math.log(a[3]), Math.log(b[3]))), heading: lerp(a[4], b[4]), pitch: lerp(a[5], b[5]) };
}

const median = (arr) => { if (!arr.length) return null; const s = [...arr].sort((a, b) => a - b); return s[Math.floor(s.length / 2)]; };
const fmtM = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(2)} M` : n >= 1e3 ? `${Math.round(n / 1e3)} k` : String(n));
const QUALITY_NAME = { lite: 'Lite', low: 'Low', medium: 'Medium', high: 'High' };

export function createPerfTools(map, { root, onResult = () => {} }) {
  // ---- live readout --------------------------------------------------------
  const panel = document.createElement('div');
  panel.id = 'stats';
  panel.className = 'panel stats';
  panel.hidden = true;
  panel.setAttribute('aria-label', 'Performance readout');
  root.appendChild(panel);
  let timer = null;

  function liveFps() {
    const now = performance.now(), rt = map.stats.renderTimes;
    if (rt.length < 2 || now - rt[rt.length - 1] > 400) return null;      // nothing is being redrawn
    const recent = rt.filter((t) => now - t <= 1000);
    if (recent.length < 2) return null;
    return (recent.length - 1) / ((recent[recent.length - 1] - recent[0]) / 1000);
  }

  function renderPanel() {
    const s = map.stats, fps = liveFps();
    const tiles = [...map.chunks.values()];
    const shown = tiles.filter((c) => c.state === 'ready').length;
    const loading = tiles.length - shown + (map.pendingTiles || 0);
    const tileMed = median(s.tileMs);
    const w = map.renderer.domElement.width, h = map.renderer.domElement.height;
    panel.innerHTML = `
      <div class="fps">${fps == null ? '<b>idle</b><span>redraws only when something moves</span>' : `<b>${Math.round(fps)}</b> fps<span>${(1000 / fps).toFixed(1)} ms per frame</span>`}</div>
      <dl>
        <dt>Triangles</dt><dd>${fmtM(s.triangles)}</dd>
        <dt>Draw calls</dt><dd>${s.drawCalls}</dd>
        <dt>Tiles</dt><dd>${shown} shown${loading ? `, ${loading} loading` : ''}</dd>
        <dt>Tile work</dt><dd>${tileMed == null ? 'none yet' : `${tileMed.toFixed(1)} ms/tile`} · ${s.workers ? `${s.workers} threads` : 'main thread'}</dd>
        <dt>Detail</dt><dd>${QUALITY_NAME[map.qualityKey] || map.qualityKey} · ${w}×${h} px</dd>
        <dt>GPU</dt><dd title="${escapeHtml(gpu())}">${escapeHtml(gpu())}</dd>
      </dl>`;
  }

  let gpuCache = null;
  const gpu = () => gpuCache || (gpuCache = map.gpuName().replace(/^ANGLE \((.*)\)$/, '$1').replace(/\s*Direct3D.*$/, '').slice(0, 60));

  function showStats(on) {
    panel.hidden = !on;
    clearInterval(timer);
    if (on) { renderPanel(); timer = setInterval(renderPanel, 500); }
  }

  // ---- benchmark -------------------------------------------------------------
  const card = document.createElement('section');
  card.id = 'bench';
  card.className = 'panel bench';
  card.hidden = true;
  card.setAttribute('aria-label', 'Benchmark');
  root.appendChild(card);
  let running = false;

  async function runBenchmark() {
    if (running) return;
    running = true;
    card.hidden = false;
    card.innerHTML = `<h2>Measuring this device</h2>
      <p>${DURATION} seconds of scripted flying over the city, loading tiles as it goes. Keep this tab in front and leave the mouse alone.</p>
      <div class="meter"><i id="benchBar"></i></div>`;
    const bar = card.querySelector('#benchBar');
    const long = [];
    let po = null;
    try { po = new PerformanceObserver((l) => { for (const e of l.getEntries()) long.push(e.duration); }); po.observe({ type: 'longtask' }); } catch { po = null; }
    const qualityStart = map.qualityKey;
    map.fly = null;
    map.controls.enabled = false;
    map.forceRender = true;
    map.stats.tileMs = [];
    const frames = [], tris = [], calls = [];
    map.setView(pathAt(0));
    await new Promise((r) => setTimeout(r, 300));
    const t0 = performance.now();
    await new Promise((resolve) => {
      const step = (now) => {
        const t = (now - t0) / 1000;
        if (t >= DURATION) { resolve(); return; }
        map.fly = null;
        map.setView(pathAt(t));
        frames.push(now);
        tris.push(map.stats.triangles);
        calls.push(map.stats.drawCalls);
        bar.style.width = `${Math.round((100 * t) / DURATION)}%`;
        requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    });
    map.forceRender = false;
    map.controls.enabled = true;
    if (po) po.disconnect();

    const iv = [];
    for (let i = 1; i < frames.length; i++) iv.push(frames[i] - frames[i - 1]);
    const sorted = [...iv].sort((a, b) => a - b);
    const p99 = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.99))] || 0;
    const avgFps = (frames.length - 1) / ((frames[frames.length - 1] - frames[0]) / 1000);
    const r = {
      average_fps: Math.round(avgFps),
      low_1pct_fps: Math.round(1000 / p99),
      median_frame_ms: +median(iv).toFixed(1),
      worst_frame_ms: Math.round(sorted[sorted.length - 1] || 0),
      page_freezes_over_50ms: long.length,
      longest_freeze_ms: Math.round(Math.max(0, ...long)),
      tile_processing_ms_median: map.stats.tileMs.length ? +median(map.stats.tileMs).toFixed(1) : null,
      tiles_processed: map.stats.tileMs.length,
      tile_threads: map.stats.workers || 'main thread',
      triangles_avg: Math.round(tris.reduce((a, b) => a + b, 0) / Math.max(1, tris.length)),
      triangles_max: Math.max(0, ...tris),
      draw_calls_max: Math.max(0, ...calls),
      first_buildings_s: map.stats.firstTileAt ? +(map.stats.firstTileAt / 1000).toFixed(2) : null,
      map_ready_s: map.stats.readyAt ? +(map.stats.readyAt / 1000).toFixed(2) : null,
      detail: qualityStart === map.qualityKey ? QUALITY_NAME[map.qualityKey] : `${QUALITY_NAME[qualityStart]}, lowered to ${QUALITY_NAME[map.qualityKey]} during the run`,
      render_size_px: `${map.renderer.domElement.width}×${map.renderer.domElement.height}`,
      gpu: map.gpuName(),
      cpu_threads: navigator.hardwareConcurrency || null,
      device_memory_gb: navigator.deviceMemory || null,
      browser: navigator.userAgent,
      measured: new Date().toISOString(),
    };
    running = false;
    showResult(r);
    onResult(r);
    console.info('SafeNisha map benchmark', r);
    return r;
  }

  function verdict(fps) {
    if (fps >= 50) return ['good', 'Smooth on this device'];
    if (fps >= 30) return ['ok', 'Usable on this device'];
    return ['bad', 'Choppy on this device. Choose Low or Lite detail.'];
  }

  function showResult(r) {
    const [cls, summary] = verdict(r.average_fps);
    const rows = [
      ['Average frame rate', `${r.average_fps} fps`],
      ['Slowest 1% of frames', `${r.low_1pct_fps} fps`],
      ['Median frame time', `${r.median_frame_ms} ms`],
      ['Page freezes over 50 ms', r.page_freezes_over_50ms ? `${r.page_freezes_over_50ms}, longest ${r.longest_freeze_ms} ms` : 'none'],
      ['Tile processing', r.tile_processing_ms_median == null ? 'no new tiles' : `${r.tile_processing_ms_median} ms per tile, ${r.tiles_processed} tiles, ${typeof r.tile_threads === 'number' ? `${r.tile_threads} background threads` : r.tile_threads}`],
      ['Triangles drawn', `${fmtM(r.triangles_avg)} average, ${fmtM(r.triangles_max)} peak`],
      ['First buildings after opening', r.first_buildings_s == null ? 'not recorded' : `${r.first_buildings_s} s`],
      ['Detail', r.detail],
      ['Picture size', `${r.render_size_px} px`],
      ['GPU', r.gpu],
    ];
    card.innerHTML = `<h2>Benchmark result</h2>
      <div class="big ${cls}"><b>${r.average_fps}</b> fps average<span>${escapeHtml(summary)}</span></div>
      <dl>${rows.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd>`).join('')}</dl>
      <textarea id="benchText" readonly aria-label="Benchmark results as text" hidden></textarea>
      <div class="actions"><button class="btn solid" type="button" id="benchCopy">Copy results</button>
        <button class="btn solid" type="button" id="benchAgain">Run again</button>
        <button class="btn" type="button" id="benchClose">Close</button></div>`;
    const text = rows.map(([k, v]) => `${k}: ${v}`).join('\n') + `\nCPU threads: ${r.cpu_threads ?? 'unknown'}\nBrowser: ${r.browser}\nMeasured: ${r.measured}`;
    card.querySelector('#benchCopy').addEventListener('click', (e) => {
      const btn = e.currentTarget;
      const fallback = () => { const ta = card.querySelector('#benchText'); ta.hidden = false; ta.value = text; ta.select(); btn.textContent = 'Select and copy the text'; };
      try {
        navigator.clipboard.writeText(text).then(() => { btn.textContent = 'Copied'; }, fallback);
      } catch { fallback(); }
    });
    card.querySelector('#benchAgain').addEventListener('click', () => runBenchmark());
    card.querySelector('#benchClose').addEventListener('click', () => { card.hidden = true; });
  }

  return { showStats, runBenchmark, get running() { return running; } };
}
