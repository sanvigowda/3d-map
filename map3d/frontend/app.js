// HUD, demo mode and host bridge for the SafeNisha 3D map.
//
// Three ways this page runs:
//   1. As a Streamlit component (map3d/__init__.py): state arrives as component
//      args, clicks go back as the component value.
//   2. Inside any other iframe: post {type:'safenisha:set', state} to it and
//      listen for {type:'safenisha:event', event}.
//   3. Opened directly: a small demo with real sample routes.

import { CityMap, HAZARD_LABEL, escapeHtml } from './engine.js';
import { createPerfTools } from './perf-tools.js';

const $ = (id) => document.getElementById(id);
const app = $('app');
const params = new URLSearchParams(location.search);

let host = 'pending';          // 'streamlit' | 'frame' | 'demo'
let state = { routes: [], hazards: [], start: null, destination: null, risk_cells: [], user_location: null, pick_mode: null, selected_route: null };
let seq = 0;
let manifest = null;
let lastCommand = null;
let perf = null;

const map = new CityMap({
  canvas: $('view'),
  overlay: $('overlay'),
  dataUrl: new URL('./data/', import.meta.url).href,
  quality: params.get('q') || 'auto',
  onEvent: onMapEvent,
  onStatus: (text, frac) => { $('loadText').textContent = text; $('loadBar').style.width = `${Math.round(4 + frac * 96)}%`; },
});
window.safenishaMap = map;      // handy in the console and for tests

// ---------------------------------------------------------------------------
// formatting
// ---------------------------------------------------------------------------
const fmtDistance = (m) => (m == null ? '' : m < 1000 ? `${Math.round(m / 10) * 10} m` : `${(m / 1000).toFixed(m < 10000 ? 1 : 0)} km`);
const fmtMinutes = (s) => `${Math.max(1, Math.round(s / 60))} min`;
const ROLE_NAME = { lower_risk: 'Lower estimated risk', preferred: 'Preferred route', shortest: 'Shortest route', alternative: 'Alternative route' };
const KIND_NAME = { area: 'Area', hood: 'Neighbourhood', poi: 'Landmark', rail: 'Railway station', metro: 'Metro station', water: 'Lake', park: 'Park' };
const KIND_DISTANCE = { area: 2400, hood: 1300, poi: 950, rail: 900, metro: 800, water: 2000, park: 1800 };

// ---------------------------------------------------------------------------
// state -> map + HUD
// ---------------------------------------------------------------------------
// space the floating panels take, so the map can frame content in what is left
function measureInsets() {
  const box = (el) => (el && !el.hidden && el.offsetParent !== null ? el.getBoundingClientRect() : null);
  const w = app.clientWidth, h = app.clientHeight, narrow = w <= 760;
  const top = box(document.querySelector('.row.top')), bottom = box(document.querySelector('.row.bottom'));
  const left = narrow ? null : box(document.querySelector('.stack.left')), right = narrow ? null : box($('legend'));
  const origin = app.getBoundingClientRect();
  map.insets = {
    top: top && top.height ? top.bottom - origin.top + 8 : 12,
    bottom: bottom && bottom.height ? origin.bottom - bottom.top + 8 : 12,
    left: left && left.width ? left.right - origin.left + 8 : 12,
    right: right && right.width ? origin.right - right.left + 8 : 12,
  };
}

function apply(next) {
  state = { ...state, ...next };
  renderRoutes();
  renderNotice();
  measureInsets();
  map.setState({
    start: state.start, destination: state.destination, routes: state.routes || [], hazards: state.hazards || [],
    risk_cells: state.risk_cells || [], user_location: state.user_location, pick_mode: state.pick_mode || null,
    selected_route: state.selected_route ?? null, fit: state.fit || 'auto',
  });
  $('legendRisk').hidden = !(state.risk_cells && state.risk_cells.length);
  $('legendRiskNote').textContent = state.risk_synthetic ? 'synthetic demo values, not data' : 'taller = higher estimate';
  $('synthetic').hidden = !(state.risk_synthetic && state.risk_cells && state.risk_cells.length);
}

function renderRoutes() {
  const routes = (state.routes || []).filter((r) => r.coords && r.coords.length > 1);
  const card = $('routeCard'), list = $('routeList'), legend = $('legendRoutes');
  card.hidden = !routes.length || state.show_route_card === false;
  list.innerHTML = '';
  legend.innerHTML = '';
  routes.forEach((r, i) => {
    const st = map.routeStyle(r, i, routes.length);
    const name = r.label || ROLE_NAME[st.role] || 'Route';
    const letter = String.fromCharCode(65 + i);
    const seconds = r.duration_s ?? (r.distance_m != null ? r.distance_m / 1.3 : null);
    const risk = r.risk_indicator;
    const li = document.createElement('li');
    li.setAttribute('role', 'option');
    li.setAttribute('aria-selected', String(state.selected_route === r.id));
    li.tabIndex = 0;
    li.innerHTML = `
      <span class="tag ${st.dashed ? 'coral' : ''}" ${r.color && !st.dashed ? `style="background:${escapeHtml(r.color)}"` : ''}>${letter}</span>
      <span><span class="name">${escapeHtml(name)}</span>
        <span class="meta">${[fmtDistance(r.distance_m), seconds != null ? `about ${fmtMinutes(seconds)} on foot` : ''].filter(Boolean).join(' · ')}</span></span>
      <span class="risk">${risk == null || Number.isNaN(Number(risk))
        ? '<span class="limited" title="No risk observations were supplied for this route">Data limited</span>'
        : `Estimated risk<b>${Number(risk).toFixed(2)}</b><span class="bar"><i style="width:${Math.round(Math.min(1, Math.max(0, risk)) * 100)}%"></i></span>`}</span>`;
    const pick = () => {
      const selected = state.selected_route === r.id ? null : r.id;
      apply({ selected_route: selected });
      emit({ type: 'route_select', id: selected });
    };
    li.addEventListener('click', pick);
    li.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } });
    list.appendChild(li);

    const key = document.createElement('li');
    key.innerHTML = `<i class="key line ${st.dashed ? 'dashed' : ''}" ${r.color && !st.dashed ? `style="border-top-color:${escapeHtml(r.color)}"` : ''}></i><span>${escapeHtml(name)}<small>route ${letter}${st.dashed ? ', dashed' : ', solid'}</small></span>`;
    legend.appendChild(key);
  });
}

const PICK_TEXT = {
  start: 'Click the map to set the start point',
  destination: 'Click the map to set the destination',
  hazard: 'Click the map where the hazard is',
};
let noticeOverride = null;

function renderNotice() {
  const el = $('notice');
  if (noticeOverride) {
    el.hidden = false;
    el.className = `panel notice ${noticeOverride.warn ? 'warn' : ''}`;
    el.replaceChildren(...noticeOverride.nodes);
    return;
  }
  const mode = state.pick_mode;
  el.className = 'panel notice';
  if (!mode || !PICK_TEXT[mode]) { el.hidden = true; return; }
  el.hidden = false;
  el.textContent = PICK_TEXT[mode];
  if (host === 'demo') {
    const cancel = document.createElement('button');
    cancel.className = 'btn'; cancel.type = 'button'; cancel.textContent = 'Cancel';
    cancel.addEventListener('click', () => setDemoPick(null));
    el.appendChild(cancel);
  }
}

function notice(text, { warn = false, ms = 0, extra = [] } = {}) {
  if (text == null) { noticeOverride = null; renderNotice(); return; }
  const span = document.createElement('span');
  span.textContent = text;
  const mine = { nodes: [span, ...extra], warn };
  noticeOverride = mine;
  renderNotice();
  if (ms) setTimeout(() => { if (noticeOverride === mine) { noticeOverride = null; renderNotice(); } }, ms);
}

// ---------------------------------------------------------------------------
// host bridge
// ---------------------------------------------------------------------------
const framed = window.parent && window.parent !== window;
const toParent = (msg) => { if (framed) window.parent.postMessage(msg, '*'); };

function emit(evt) {
  const value = { ...evt, seq: ++seq, ts: Date.now() };
  if (host === 'streamlit') toParent({ isStreamlitMessage: true, type: 'streamlit:setComponentValue', value, dataType: 'json' });
  else if (host === 'frame') toParent({ type: 'safenisha:event', event: value });
}

function fromHost(args) {
  const a = args || {};
  if (a.hud) app.dataset.hud = a.hud;
  if (a.quality && a.quality !== map.qualityName) { map.setQuality(a.quality); $('quality').value = a.quality; }
  if (typeof a.labels === 'boolean') setToggle('btnLabels', a.labels, (on) => map.setLabels(on));
  if (typeof a.lamps === 'boolean') setToggle('btnLamps', a.lamps, (on) => map.setLamps(on));
  if (typeof a.legend === 'boolean') setToggle('btnLegend', a.legend, (on) => { $('legend').hidden = !on; });
  if (typeof a.stats === 'boolean' && perf) { perf.showStats(a.stats); $('btnStats').setAttribute('aria-pressed', String(a.stats)); }
  const next = {};
  for (const k of ['start', 'destination', 'routes', 'hazards', 'risk_cells', 'user_location', 'pick_mode', 'selected_route', 'fit', 'show_route_card', 'risk_synthetic']) if (k in a) next[k] = a[k];
  apply(next);
  // one-shot camera command; re-runs only when its nonce changes
  const cmd = a.fly_to;
  if (cmd && JSON.stringify(cmd) !== lastCommand) {
    lastCommand = JSON.stringify(cmd);
    map.flyTo({ lat: cmd.lat, lon: cmd.lon, distance: cmd.distance, heading: cmd.heading, pitch: cmd.pitch });
  }
}

window.addEventListener('message', (e) => {
  const d = e.data;
  if (!d || typeof d !== 'object') return;
  if (d.type === 'streamlit:render') {
    if (host !== 'streamlit') { setHost('streamlit'); }
    const h = (d.args && d.args.height) || 640;
    toParent({ isStreamlitMessage: true, type: 'streamlit:setFrameHeight', height: h });
    whenReady(() => fromHost(d.args));
  } else if (d.type === 'safenisha:set') {
    if (host !== 'frame') setHost('frame');
    whenReady(() => fromHost(d.state));
  } else if (d.type === 'safenisha:fly_to') {
    whenReady(() => map.flyTo(d.view || {}));
  }
});

function setHost(h) {
  host = h;
  app.dataset.host = h;
  $('demo').hidden = h !== 'demo';
  if (h !== 'demo' && demoLoaded) { demoLoaded = false; apply({ routes: [], hazards: [], start: null, destination: null, risk_cells: [], risk_synthetic: false, pick_mode: null }); }
}

let ready = false;
const queued = [];
function whenReady(fn) { if (ready) fn(); else queued.push(fn); }

// tell a Streamlit parent we exist; harmless anywhere else
toParent({ isStreamlitMessage: true, type: 'streamlit:componentReady', apiVersion: 1 });

// ---------------------------------------------------------------------------
// map events
// ---------------------------------------------------------------------------
let lastReadout = 0;
function onMapEvent(evt) {
  if (evt.type === 'view') {
    const now = performance.now();
    $('needle').style.transform = `rotate(${-evt.view.heading}deg)`;
    if (now - lastReadout < 120) return;
    lastReadout = now;
    $('where').textContent = evt.view.distance > 30000 ? 'Bengaluru' : evt.place || 'Bengaluru';
    $('coords').textContent = `${evt.view.lat.toFixed(4)}° N  ${evt.view.lon.toFixed(4)}° E · eye ${fmtDistance(evt.view.eye)}`;
  } else if (evt.type === 'click') {
    if (host === 'demo') demoClick(evt);
    else emit({ type: 'click', lat: evt.lat, lon: evt.lon, mode: evt.mode, inside: evt.inside });
  } else if (evt.type === 'hazard_click') {
    for (const el of document.querySelectorAll('.marker.hazard')) el.classList.toggle('open', el.dataset.id === String(evt.id ?? '') && !el.classList.contains('open'));
    if (host !== 'demo') emit({ type: 'hazard_click', id: evt.id });
  } else if (evt.type === 'quality') {
    const opt = $('quality').querySelector('option[value="auto"]');
    if (opt) opt.textContent = evt.auto ? `Auto · ${evt.quality[0].toUpperCase()}${evt.quality.slice(1)}` : 'Auto detail';
  }
}

// ---------------------------------------------------------------------------
// tools
// ---------------------------------------------------------------------------
function setToggle(id, on, fn) { $(id).setAttribute('aria-pressed', String(on)); fn(on); }
function bindToggle(id, fn) {
  $(id).addEventListener('click', () => setToggle(id, $(id).getAttribute('aria-pressed') !== 'true', fn));
}
bindToggle('btnLabels', (on) => map.setLabels(on));
bindToggle('btnLamps', (on) => map.setLamps(on));
bindToggle('btnLegend', (on) => { $('legend').hidden = !on; });
$('compass').addEventListener('click', () => map.flyTo({ heading: 0 }, 700));
$('quality').addEventListener('change', (e) => map.setQuality(e.target.value));
$('btnAbout').addEventListener('click', () => { $('about').hidden = !$('about').hidden; });
$('aboutClose').addEventListener('click', () => { $('about').hidden = true; });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { $('about').hidden = true; if (host === 'demo' && state.pick_mode) setDemoPick(null); } });
if (params.get('q')) $('quality').value = params.get('q');
if (params.get('hud')) app.dataset.hud = params.get('hud');
if (window.innerWidth <= 760) setToggle('btnLegend', false, (on) => { $('legend').hidden = !on; });

// ---- search ---------------------------------------------------------------
const results = $('results'), searchInput = $('search');
let hits = [], active = -1;
function runSearch() {
  const q = searchInput.value.trim().toLowerCase();
  if (q.length < 2 || !manifest) { results.hidden = true; hits = []; return; }
  const order = { poi: 0, area: 1, rail: 2, metro: 3, water: 4, park: 5, hood: 6 };
  hits = manifest.labels
    .map((l) => ({ l, at: l.n.toLowerCase().indexOf(q) }))
    .filter((h) => h.at >= 0)
    .sort((a, b) => (a.at > 0) - (b.at > 0) || order[a.l.k] - order[b.l.k] || a.l.n.length - b.l.n.length)
    .slice(0, 8).map((h) => h.l);
  active = hits.length ? 0 : -1;
  results.innerHTML = hits.length
    ? hits.map((l, i) => `<li role="option" data-i="${i}" aria-selected="${i === active}">${escapeHtml(l.n)}<small>${KIND_NAME[l.k] || ''}</small></li>`).join('')
    : '<li class="none">No neighbourhood or landmark by that name. Try a nearby area.</li>';
  results.hidden = false;
}
function goTo(l) {
  if (!l) return;
  map.flyTo({ x: l.x, y: l.y, distance: KIND_DISTANCE[l.k] || 1500, pitch: 56 });
  searchInput.value = l.n;
  results.hidden = true;
  searchInput.blur();
}
searchInput.addEventListener('input', runSearch);
searchInput.addEventListener('focus', runSearch);
searchInput.addEventListener('keydown', (e) => {
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    if (!hits.length) return;
    e.preventDefault();
    active = (active + (e.key === 'ArrowDown' ? 1 : hits.length - 1)) % hits.length;
    [...results.children].forEach((li, i) => li.setAttribute('aria-selected', String(i === active)));
  } else if (e.key === 'Escape') { results.hidden = true; }
});
$('searchForm').addEventListener('submit', (e) => { e.preventDefault(); goTo(hits[Math.max(0, active)]); });
results.addEventListener('pointerdown', (e) => { const li = e.target.closest('li[data-i]'); if (li) { e.preventDefault(); goTo(hits[+li.dataset.i]); } });
document.addEventListener('pointerdown', (e) => { if (!e.target.closest('#searchForm')) results.hidden = true; });

// ---------------------------------------------------------------------------
// demo mode (page opened on its own)
// ---------------------------------------------------------------------------
let demoLoaded = false;
let sample = null;
const ROUTER = 'https://routing.openstreetmap.de/routed-foot/route/v1/foot';

async function startDemo() {
  setHost('demo');
  try {
    sample = await (await fetch(new URL('./data/sample_routes.json', import.meta.url))).json();
  } catch { sample = null; }
  resetDemo();
}

function resetDemo() {
  noticeOverride = null;
  $('demoRisk').checked = false;
  demoLoaded = true;
  if (sample) {
    $('demoNote').textContent = `Sample routes are real output from the public OpenStreetMap foot router, captured ${sample.captured}. No risk data is attached, so both show "Data limited".`;
    apply({ start: sample.start, destination: sample.destination, routes: sample.routes, hazards: [], risk_cells: [], risk_synthetic: false, pick_mode: null, selected_route: null, fit: 'none' });
    // open at street level, part-way along the sample route, rather than framing all of it
    const path = sample.routes[sample.routes.length - 1].coords, mid = path[Math.floor(path.length * 0.55)];
    map.flyTo({ lat: mid[0], lon: mid[1], distance: window.innerWidth <= 760 ? 760 : 640, heading: 25, pitch: 66 }, 2200);
  } else {
    $('demoNote').textContent = 'Set a start and a destination to draw a walking route.';
    apply({ start: null, destination: null, routes: [], hazards: [], risk_cells: [], risk_synthetic: false, pick_mode: null });
  }
  syncPickButtons();
}

function syncPickButtons() {
  for (const b of document.querySelectorAll('[data-pick]')) b.setAttribute('aria-pressed', String(b.dataset.pick === state.pick_mode));
}
function setDemoPick(mode) {
  noticeOverride = null;
  apply({ pick_mode: state.pick_mode === mode ? null : mode });
  syncPickButtons();
}
for (const b of document.querySelectorAll('[data-pick]')) b.addEventListener('click', () => setDemoPick(b.dataset.pick));
$('demoReset').addEventListener('click', resetDemo);

async function demoClick(evt) {
  const mode = state.pick_mode;
  if (!mode) return;
  if (!evt.inside) { notice('That point is outside the mapped area.', { warn: true, ms: 3500 }); return; }
  if (mode === 'hazard') { askHazard(evt); return; }
  const next = { [mode]: { lat: evt.lat, lon: evt.lon }, pick_mode: null, routes: [], selected_route: null, fit: 'none' };
  apply(next);
  syncPickButtons();
  if (state.start && state.destination) await demoRoute();
}

async function demoRoute() {
  const a = state.start, b = state.destination;
  notice('Asking the public walking router for routes…');
  try {
    const url = `${ROUTER}/${a.lon},${a.lat};${b.lon},${b.lat}?overview=full&geometries=geojson&alternatives=3&steps=false`;
    const res = await fetch(url, { signal: AbortSignal.timeout(12000) });
    const data = await res.json();
    if (data.code !== 'Ok' || !data.routes.length) throw new Error(data.code || 'no route');
    const routes = data.routes.slice().sort((p, q) => p.distance - q.distance).slice(0, 2).map((r, i) => ({
      id: i === 0 ? 'shortest' : 'alternative', role: i === 0 ? 'shortest' : 'alternative',
      label: i === 0 ? 'Shortest route' : 'Alternative route',
      distance_m: Math.round(r.distance), duration_s: Math.round(r.duration), risk_indicator: null,
      coords: r.geometry.coordinates.map(([lon, lat]) => [lat, lon]),
    }));
    notice(null);
    apply({ routes, fit: 'auto' });
    if (routes.length === 1) notice('The router returned one route for these points, so there is nothing to compare.', { ms: 5000 });
  } catch (err) {
    notice('The public walking router could not be reached from this page, so no route is drawn. In the SafeNisha app, routes come from the app.', { warn: true, ms: 9000 });
  }
}

function askHazard(evt) {
  const select = document.createElement('select');
  select.id = 'hazardType';
  select.setAttribute('aria-label', 'Hazard type');
  for (const [value, label] of Object.entries(HAZARD_LABEL)) select.add(new Option(label, value));
  const add = document.createElement('button');
  add.className = 'btn'; add.type = 'button'; add.textContent = 'Add report';
  const cancel = document.createElement('button');
  cancel.className = 'btn'; cancel.type = 'button'; cancel.textContent = 'Cancel';
  add.addEventListener('click', () => {
    const hazards = [...(state.hazards || []), {
      id: `demo-${Date.now()}`, latitude: evt.lat, longitude: evt.lon, hazard_type: select.value,
      description: '', reported_at: new Date().toISOString(), status: 'new', expires_at: null,
    }];
    apply({ hazards, pick_mode: null });
    syncPickButtons();
    notice('Added as an unverified report. It stays out of route scoring until someone confirms it. This demo does not save it.', { ms: 7000 });
  });
  cancel.addEventListener('click', () => { notice(null); });
  notice('Hazard type', { extra: [select, add, cancel] });
}

// Random bumps around the current view. Regenerated on every toggle so the
// pattern is plainly not a statement about any real street.
$('demoRisk').addEventListener('change', (e) => {
  if (!e.target.checked) { apply({ risk_cells: [], risk_synthetic: false }); return; }
  const v = map.getView(), cells = [], size = 64, half = 9;
  const bumps = Array.from({ length: 4 }, () => ({ x: (Math.random() - 0.5) * size * half * 1.7, y: (Math.random() - 0.5) * size * half * 1.7, r: 70 + Math.random() * 90, a: 0.5 + Math.random() * 0.5 }));
  for (let iy = -half; iy <= half; iy++) for (let ix = -half; ix <= half; ix++) {
    const x = ix * size, y = iy * size;
    let val = 0;
    for (const b of bumps) val = Math.max(val, b.a * Math.exp(-((x - b.x) ** 2 + (y - b.y) ** 2) / (2 * b.r * b.r)));
    if (val < 0.3) continue;
    const p = map.toLatLon(v.x + x, v.y + y);
    cells.push({ lat: p.lat, lon: p.lon, value: +val.toFixed(2), size_m: size * 0.82 });
  }
  apply({ risk_cells: cells, risk_synthetic: true });
});

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------
function fillAbout() {
  const s = manifest.stats.buildings, total = s.total;
  const rows = [
    ['Mapped height', s.measured, ''], ['Mapped floor count', s.floors, ''],
    ['Area estimate', s.area_estimate, 'est'], ['Default 4 m', s.default, 'def'],
  ];
  $('heightBars').innerHTML = rows.map(([name, n, cls]) =>
    `<span>${name}</span><span class="bar"><i class="${cls}" style="width:${(100 * n / total).toFixed(2)}%"></i></span><span class="n">${n.toLocaleString('en-IN')}</span>`).join('');
  const mapped = s.measured + s.floors;
  $('heightNote').textContent = `${mapped.toLocaleString('en-IN')} of ${total.toLocaleString('en-IN')} buildings (${(100 * mapped / total).toFixed(1)}%) have a mapped height or floor count. The rest use the average building height published for their ~90 m cell.`;
  $('sources').innerHTML = manifest.sources.map((t) => `<li>${escapeHtml(t)}</li>`).join('') + `<li>Model baked ${escapeHtml(manifest.built)}.</li>`;
  $('subtitle').innerHTML = `<b>${total.toLocaleString('en-IN')}</b> buildings placed from the map`;
  const chips = $('places');
  for (const p of [...manifest.presets, { n: 'Whole city', x: 1000, y: 1000, d: 52000, h: 0, p: 12 }]) {
    const b = document.createElement('button');
    b.className = 'chip'; b.type = 'button'; b.setAttribute('role', 'listitem'); b.textContent = p.n;
    b.addEventListener('click', () => map.flyTo({ x: p.x, y: p.y, distance: p.d, heading: p.h, pitch: p.p }, 2200));
    chips.appendChild(b);
  }
}

(async () => {
  try {
    if (!('DecompressionStream' in window)) throw new Error('this browser is too old to unpack the map tiles');
    await map.init();
    manifest = map.manifest;
    fillAbout();
    perf = createPerfTools(map, { root: app, onResult: (r) => emit({ type: 'benchmark', result: r }) });
    $('btnBench').addEventListener('click', () => { $('about').hidden = true; perf.runBenchmark(); });
    $('btnStats').addEventListener('click', (e) => { const on = e.currentTarget.getAttribute('aria-pressed') !== 'true'; e.currentTarget.setAttribute('aria-pressed', String(on)); perf.showStats(on); });
    if (params.get('stats') === '1') { perf.showStats(true); $('btnStats').setAttribute('aria-pressed', 'true'); }
    if (params.get('bench') === '1') setTimeout(() => perf.runBenchmark(), 2600);   // after the opening fly-in
    ready = true;
    queued.splice(0).forEach((fn) => fn());
    $('loading').classList.add('done');
    // no host spoke up: run the standalone demo
    setTimeout(() => { if (host === 'pending') { if (params.get('demo') === '0') setHost('frame'); else startDemo(); } }, framed ? 700 : 0);
  } catch (err) {
    console.error(err);
    const el = $('loadErr');
    el.hidden = false;
    el.textContent = `The map could not load (${err.message}). Check that the data folder sits next to this page and that it is served over http, not opened as a file.`;
  }
})();
