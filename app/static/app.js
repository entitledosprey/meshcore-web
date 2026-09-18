'use strict';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

let STATE = { connected: false, contacts: [], self: {}, device: {}, counts: {} };
let ws = null, wsRetry = 1000;
// Per-contact command output. Kept here because renderRepeaters()
// rebuilds every card, which would otherwise discard it.
const RESULTS = Object.create(null);

/* ---------------- helpers ---------------- */

const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  $('#toasts').append(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 250); }, 3800);
}

function ago(ts) {
  if (!ts) return '—';
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 0 || s > 3.15e9) return '—';
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}

/** Run an async job while showing a spinner on the button that started it. */
async function withBusy(btn, fn) {
  if (btn) { btn.classList.add('busy'); btn.disabled = true; }
  try { return await fn(); }
  finally { if (btn) { btn.classList.remove('busy'); btn.disabled = false; } }
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' }, ...opts,
  });
  let body = null;
  try { body = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new Error(body?.detail || `${res.status} ${res.statusText}`);
  return body;
}

/** Most endpoints answer {text, data}; F.result renders it for humans. */
const present = (r) => F.result(r);

/* ---------------- websocket ---------------- */

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    // Pull anything that arrived while the socket was down; the server no
    // longer replays chat over the event stream.
    if (lastMsgId > 0) {
      api(`/api/chat/messages?since=${lastMsgId}`)
        .then((r) => (r.messages || []).forEach(onChatMessage))
        .catch(() => {});
    }
    wsRetry = 1000;
  };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === 'state') { STATE = m.state; renderAll(); }
    else if (m.type === 'event') {
      if (m.event.kind === 'chat') onChatMessage(m.event.payload);
      else pushEvent(m.event);
    }
  };
  ws.onclose = () => {
    setLink(false, 'stream lost');
    setTimeout(connectWS, wsRetry);
    wsRetry = Math.min(wsRetry * 2, 15000);
  };
  ws.onerror = () => ws.close();
}

/* ---------------- event log ---------------- */

const logEl = () => $('#eventLog');

const KIND_LABEL = {
  RX_LOG_DATA: 'rf', DISCOVER_RESPONSE: 'discover', ADVERTISEMENT: 'advert',
  TELEMETRY_RESPONSE: 'telemetry', STATUS_RESPONSE: 'status',
  NEIGHBOURS_RESPONSE: 'neighbours', CONTACT_MSG_RECV: 'message',
  CHANNEL_MSG_RECV: 'channel', NEW_CONTACT: 'contact', CONTACT_DELETED: 'contact',
  DEVICE_INFO: 'device', SELF_INFO: 'node', CURRENT_TIME: 'clock',
  PATH_UPDATE: 'path', LOGIN_SUCCESS: 'login', LOGIN_FAILED: 'login',
  CONTACTS: 'contacts', DISCONNECTED: 'link', ERROR: 'error',
  clock_synced: 'clock', contacts_updated: 'contacts',
};
const prettyKind = (k) => KIND_LABEL[k] || String(k).toLowerCase().replace(/_/g, ' ');

function pushEvent(ev) {
  const el = logEl();
  if (el.querySelector('.empty')) el.innerHTML = '';
  const row = document.createElement('div');
  row.className = 'log-row';
  const t = new Date(ev.ts * 1000).toLocaleTimeString();
  const summary = F.event(ev.kind, ev.payload);
  const raw = ev.payload && typeof ev.payload === 'object'
    ? JSON.stringify(ev.payload, null, 2) : String(ev.payload ?? '');
  row.innerHTML =
    `<span class="log-t">${t}</span>` +
    `<span class="log-k k-${esc(ev.kind)}">${esc(prettyKind(ev.kind))}</span>` +
    `<span class="log-p">${esc(summary)}</span>`;
  if (raw && raw !== '{}') {
    row.classList.add('has-raw');
    row.title = 'click for raw payload';
    row.onclick = () => {
      let pre = row.nextElementSibling;
      if (pre && pre.classList.contains('log-raw')) { pre.remove(); return; }
      pre = document.createElement('pre');
      pre.className = 'log-raw';
      pre.textContent = raw;
      row.after(pre);
    };
  }
  el.append(row);
  while (el.children.length > 400) el.firstChild.remove();
  if ($('#autoscroll').checked) el.scrollTop = el.scrollHeight;
}

/* ---------------- rendering ---------------- */

function setLink(ok, note) {
  const pill = $('#linkPill');
  pill.className = 'pill ' + (ok ? 'pill-ok' : 'pill-bad');
  pill.textContent = ok ? 'connected' : (note || 'offline');
}

function renderAll() {
  const s = STATE.self || {}, d = STATE.device || {};
  setLink(STATE.connected, STATE.error ? 'error' : 'offline');

  $('#nodeName').textContent = s.name || 'MeshCore';
  $('#nodeSub').textContent = (s.public_key || '').slice(0, 12) || STATE.port || '';

  $('#radioSummary').innerHTML = s.radio_freq
    ? `<span><b>${s.radio_freq}</b> MHz</span><span>BW <b>${s.radio_bw}</b></span>` +
      `<span>SF <b>${s.radio_sf}</b></span><span>CR <b>${s.radio_cr}</b></span>` +
      `<span>TX <b>${s.tx_power}</b>dBm</span>`
    : '';

  $('#nodeStats').innerHTML = kv({
    'Name': s.name, 'Public key': (s.public_key || '').slice(0, 24) || '—',
    'Frequency': s.radio_freq ? s.radio_freq + ' MHz' : '—',
    'Bandwidth': s.radio_bw ? s.radio_bw + ' kHz' : '—',
    'Spreading factor': s.radio_sf, 'Coding rate': s.radio_cr,
    'TX power': s.tx_power != null ? `${s.tx_power} / ${s.max_tx_power} dBm` : '—',
    'Contacts': STATE.counts?.total ?? 0,
    'Repeaters / rooms': STATE.counts?.infra ?? 0,
    'Serial port': STATE.port,
  });

  $('#deviceStats').innerHTML = kv({
    'Model': d.model, 'Firmware': d.ver, 'Build': d.fw_build,
    'Protocol': d['fw ver'], 'Max contacts': d.max_contacts,
    'Max channels': d.max_channels, 'Repeat mode': d.repeat ? 'on' : 'off',
  });

  // Only seed the settings inputs when untouched, so typing is never clobbered.
  setIfIdle('#rFreq', s.radio_freq); setIfIdle('#rBw', s.radio_bw);
  setIfIdle('#rSf', s.radio_sf);     setIfIdle('#rCr', s.radio_cr);
  setIfIdle('#nName', s.name);       setIfIdle('#nTx', s.tx_power);

  $('#repCount').textContent = STATE.counts?.infra ?? 0;
  renderRepeaters();
  renderMine();
  renderContacts();
  renderChatSide();
}

function kv(obj) {
  return Object.entries(obj)
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v ?? '—')}</dd>`).join('');
}

function setIfIdle(sel, val) {
  const el = $(sel);
  if (el && document.activeElement !== el && val != null && el.value === '') el.value = val;
}

let repQuery = '';

function renderRepeaters() {
  const list = $('#repeaterList');
  // Infrastructure only — companions belong on the Contacts tab.
  const all = (STATE.contacts || []).filter((c) => c.is_infra);
  const q = repQuery.trim().toLowerCase();
  const cs = q
    ? all.filter((c) => (c.adv_name || '').toLowerCase().includes(q)
                     || (c.public_key || '').toLowerCase().includes(q))
    : all;

  const badge = $('#repCount');
  if (badge) badge.textContent = q ? `${cs.length} of ${all.length}` : `${all.length}`;

  if (!cs.length) {
    list.innerHTML = `<div class="card"><div class="empty">${
      all.length ? 'No repeaters match that search.' : `
No repeaters yet. A repeater is only heard when its radio settings match
      this node's exactly, so check those first on the Node tab. Then run
      <b>Find &amp; add nodes</b>, or add one by URI above.`}</div></div>`;
    return;
  }
  list.innerHTML = cs.map(repCard).join('');
}

function repCard(c) {
  const isRep = c.type === 2, isRoom = c.type === 3;
  const tag = isRep ? '<span class="tag tag-rep">repeater</span>'
            : isRoom ? '<span class="tag tag-room">room</span>'
            : `<span class="tag">${esc(typeName(c.type))}</span>`;
  const path = (c.out_path_len === -1 || c.out_path_len == null)
    ? 'flood' : `${c.out_path_len} hop${c.out_path_len === 1 ? '' : 's'}`;
  const auth = c.logged_in ? '<span class="auth auth-in">unlocked</span>'
             : c.has_password ? '<span class="auth auth-saved">key saved</span>'
             : '<span class="auth auth-none">no password</span>';
  const id = esc(c.public_key);
  return `<div class="rep" data-key="${id}">
    <div class="rep-head">
      <div>
        <div class="rep-name">${esc(c.adv_name || '(unnamed)')}</div>
        <div class="rep-key">${esc(c.key_prefix)}</div>
      </div>${tag}
    </div>
    <div class="rep-meta">
      <span>${esc(path)}</span>
      <span>heard ${esc(ago(c.last_advert))}</span>
      ${auth}
      ${c.adv_lat ? `<span>${c.adv_lat.toFixed(4)}, ${c.adv_lon.toFixed(4)}</span>` : ''}
    </div>
    <div class="actions">
      <button class="btn" data-rep="neighbours">Neighbours</button>
      <button class="btn" data-rep="status">Status</button>
      <button class="btn" data-rep="telemetry">Telemetry</button>
      <button class="btn" data-rep="path/discover">Find path</button>
      <button class="btn" data-rep="console">Manage…</button>
      <button class="btn${c.owned ? ' on' : ''}" data-mine="1">${c.owned ? 'Mine ✓' : 'Mine'}</button>
      <button class="btn btn-danger" data-rep="reboot">Reboot</button>
    </div>
    <div class="out rep-out"${RESULTS[c.public_key] ? '' : ' hidden'}>${
      RESULTS[c.public_key] || ''}</div>
  </div>`;
}

const typeName = (t) => ({ 1: 'companion', 2: 'repeater', 3: 'room', 4: 'sensor' }[t] || 'node ' + t);

/* ---------------- dashboard actions ---------------- */

const DASH = {
  advert:    () => api('/api/node/advert', { method: 'POST' }),
  floodadv:  () => api('/api/node/advert?flood=true', { method: 'POST' }),
  discover:  () => api('/api/node/discover?filter=2', { method: 'POST' }),
  discoveradd: async () => {
    const r = await api('/api/node/discover-add?filter=255', { method: 'POST' });
    await refreshState();
    return r;
  },
  clocksync: () => api('/api/node/clock-sync', { method: 'POST' }),
  selftel:   () => api('/api/node/telemetry'),
  card:      () => api('/api/node/card'),
  reboot:    () => api('/api/node/reboot', { method: 'POST' }),
};

function showResult(title, text) {
  $('#resultCard').hidden = false;
  $('#resultCard h2').firstChild.textContent = title + ' ';
  $('#resultOut').innerHTML = text;
  $('#resultCard').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

$$('[data-act]').forEach((btn) => btn.addEventListener('click', () => {
  const act = btn.dataset.act;
  if (act === 'reboot' && !confirm('Reboot the local companion radio?')) return;
  withBusy(btn, async () => {
    try {
      const r = await DASH[act]();
      showResult(btn.textContent.trim(), present(r));
      toast(btn.textContent.trim() + ' — done', 'ok');
    } catch (e) { toast(e.message, 'err'); }
  });
}));

/* ---------------- repeater actions ---------------- */

$('#repeaterList').addEventListener('click', async (ev) => {
  const mineBtn = ev.target.closest('[data-mine]');
  if (mineBtn) {
    const k = mineBtn.closest('.rep').dataset.key;
    const on = mineBtn.classList.contains('on');
    return withBusy(mineBtn, async () => {
      await api(`/api/contact/${k}/owned`, { method: on ? 'DELETE' : 'PUT' });
      await refreshState();
    });
  }
  const btn = ev.target.closest('[data-rep]');
  if (!btn) return;
  const card = btn.closest('.rep');
  const key = card.dataset.key;
  const act = btn.dataset.rep;
  const out = $('.rep-out', card);

  if (act === 'console') return openSheet(key);
  if (act === 'reboot' && !confirm('Reboot this repeater?')) return;

  await withBusy(btn, async () => {
    out.hidden = false;
    out.innerHTML = '<span class="muted">working…</span>';
    let html;
    try {
      const r = await api(`/api/contact/${key}/${act}`, { method: 'POST' });
      html = present(r);
      if (r.auth && !r.auth.has_password) {
        html += '<div class="fmt-note">This repeater needs a password. Open ' +
                'Manage… to set one — it is reused automatically after that.</div>';
      }
    } catch (e) {
      html = `<div class="fmt-err">${esc(e.message)}</div>`;
    }
    RESULTS[key] = html;
    out.innerHTML = html;
    await refreshState();   // safe now: repCard() re-renders RESULTS[key]
  });
});

/* ---------------- my repeaters ---------------- */

/* The command surface below is meshcli's own repeater_completion_list, split
   by how each command actually reaches the repeater:

     query  - meshcli issues it and correlates the reply  (/req/<verb>)
     cli    - literal text forwarded to the repeater's CLI (/cmd)

   Getting that split wrong is silent: a remote verb sent locally configures
   this node instead of the repeater. */

const MINE_QUERIES = [
  ['req_status',     'Status'],
  ['req_neighbours', 'Neighbours'],
  ['req_telemetry',  'Telemetry'],
  ['req_acl',        'Access list'],
  ['req_owner',      'Owner'],
  ['req_regions',    'Regions'],
  ['req_clock',      'Clock'],
];

const MINE_ACTIONS = [
  ['ver',                 'Version',            false],
  ['advert',              'Send advert',        false],
  ['neighbors',           'Neighbour table',    false],
  ['discover.neighbors',  'Discover neighbours', false],
  ['clock sync',          'Sync clock',         false],
  ['powersaving on',      'Power saving on',    false],
  ['powersaving off',     'Power saving off',   false],
  ['log start',           'Log start',          false],
  ['log stop',            'Log stop',           false],
  ['log erase',           'Log erase',          true],
  ['start ota',           'Start OTA',          true],
  ['reboot',              'Reboot',             true],
  ['clkreboot',           'Clock reboot',       true],
  ['erase',               'Erase contacts',     true],
];

const MINE_GET_VARS = ['name', 'role', 'radio', 'freq', 'tx', 'af', 'repeat',
  'allow.read.only', 'flood.advert.interval', 'flood.max', 'advert.interval',
  'guest.password', 'owner.info', 'rxdelay', 'txdelay', 'direct.tx_delay',
  'public.key', 'lat', 'lon', 'telemetry', 'status', 'timeout', 'acl',
  'bridge.enabled', 'bridge.delay', 'bridge.source', 'bridge.baud',
  'bridge.secret', 'bridge.type', 'path.hash.mode'];

const MINE_SET_VARS = [
  { v: 'name',                  hint: 'text' },
  { v: 'radio',                 hint: 'freq,bw,sf,cr' },
  { v: 'freq',                  hint: 'MHz' },
  { v: 'tx',                    hint: 'dBm' },
  { v: 'af',                    hint: 'number' },
  { v: 'repeat',                opts: ['on', 'off'] },
  { v: 'allow.read.only',       opts: ['on', 'off'] },
  { v: 'flood.advert.interval', hint: 'minutes' },
  { v: 'flood.max',             hint: 'hops' },
  { v: 'advert.interval',       hint: 'minutes' },
  { v: 'guest.password',        hint: 'text' },
  { v: 'owner.info',            hint: 'text' },
  { v: 'rxdelay',               hint: 'ms' },
  { v: 'txdelay',               hint: 'ms' },
  { v: 'direct.txdelay',        hint: 'ms' },
  { v: 'lat',                   hint: 'degrees' },
  { v: 'lon',                   hint: 'degrees' },
  { v: 'timeout',               hint: 'seconds' },
  { v: 'path.hash.mode',        opts: ['0', '1', '2'] },
  { v: 'bridge.enabled',        opts: ['on', 'off'] },
  { v: 'bridge.delay',          hint: 'ms' },
  { v: 'bridge.source',         hint: 'id' },
  { v: 'bridge.baud',           hint: 'baud' },
  { v: 'bridge.secret',         hint: 'text' },
];

const MINE_REGION_OPS = ['get', 'allowf', 'denyf', 'put', 'remove', 'save', 'home'];
const MINE_GPS_OPS = ['on', 'off', 'sync'];

/* A card's open groups and half-typed values live only in the DOM, but every
   action ends in refreshState() -> renderMine(), which rebuilds these cards.
   Capture that state first so the card comes back the way it was left. */
const MINE_OPEN = Object.create(null);    // card key -> Set of group names
const MINE_FIELDS = Object.create(null);  // card key -> {role: value}

const openAttr = (c, grp, dflt = false) => {
  const s = MINE_OPEN[c.public_key];
  return (s ? s.has(grp) : dflt) ? ' open' : '';
};

function captureMine() {
  for (const card of $$('#mineList .mine')) {
    const key = card.dataset.key;
    const open = new Set();
    for (const d of $$('details.grp', card)) if (d.open) open.add(d.dataset.grp);
    MINE_OPEN[key] = open;
    const f = Object.create(null);
    for (const el of $$('[data-role]', card)) {
      // Passwords are deliberately not carried across a re-render.
      if (el.type !== 'password' && el.value) f[el.dataset.role] = el.value;
    }
    MINE_FIELDS[key] = f;
  }
}

function restoreMine() {
  for (const card of $$('#mineList .mine')) {
    const f = MINE_FIELDS[card.dataset.key];
    if (!f) continue;
    for (const el of $$('[data-role]', card)) {
      const v = f[el.dataset.role];
      if (v != null) el.value = v;
    }
    const sel = $('[data-role="setvar"]', card);
    const hint = sel?.selectedOptions[0]?.dataset.hint || '';
    const box = $('[data-role="sethint"]', card);
    if (box) box.textContent = hint ? `expects: ${hint}` : '';
  }
}

function renderMine() {
  const list = $('#mineList');
  captureMine();
  const cs = (STATE.contacts || []).filter((c) => c.owned);
  const badge = $('#mineCount');
  if (badge) badge.textContent = cs.length;
  if (!cs.length) {
    list.innerHTML = `<div class="card"><div class="empty">
      No repeaters marked yet. Open the <b>Repeaters</b> tab and press
      <b>Mine</b> on the ones you run.</div></div>`;
    return;
  }
  list.innerHTML = cs.map(mineCard).join('');
  restoreMine();
}

function mineCard(c) {
  const id = esc(c.public_key);
  const auth = c.logged_in ? '<span class="auth auth-in">unlocked</span>'
             : c.has_password ? '<span class="auth auth-saved">key saved</span>'
             : '<span class="auth auth-none">no password</span>';
  const path = (c.out_path_len === -1 || c.out_path_len == null)
    ? 'flood' : `${c.out_path_len} hop${c.out_path_len === 1 ? '' : 's'}`;

  const btns = (arr) => arr.map(([cmd, label, danger]) =>
    `<button class="btn${danger ? ' btn-danger' : ''}" data-cli="${esc(cmd)}"
      ${danger ? 'data-confirm="1"' : ''}>${esc(label)}</button>`).join('');

  const queries = MINE_QUERIES.map(([v, label]) =>
    `<button class="btn" data-query="${esc(v)}">${esc(label)}</button>`).join('');

  const getOpts = MINE_GET_VARS.map((v) =>
    `<option value="${esc(v)}">${esc(v)}</option>`).join('');
  const setOpts = MINE_SET_VARS.map((s) =>
    `<option value="${esc(s.v)}" data-hint="${esc(s.hint || (s.opts || []).join('|'))}">${esc(s.v)}</option>`).join('');

  return `<div class="mine" data-key="${id}">
    <div class="rep-head">
      <div>
        <div class="rep-name">${esc(c.adv_name || '(unnamed)')}</div>
        <div class="rep-key">${esc(c.key_prefix)}</div>
      </div>
      <div class="mine-head-right">
        ${auth}
        <button class="btn btn-small" data-unmine="1" title="Remove from My Repeaters">Unmark</button>
      </div>
    </div>
    <div class="rep-meta">
      <span>${esc(path)}</span>
      <span>heard ${esc(ago(c.last_advert))}</span>
      ${c.adv_lat ? `<span>${c.adv_lat.toFixed(4)}, ${c.adv_lon.toFixed(4)}</span>` : ''}
    </div>

    <details class="grp" data-grp="queries"${openAttr(c, 'queries', true)}><summary>Queries</summary>
      <div class="actions">${queries}</div></details>

    <details class="grp" data-grp="actions"${openAttr(c, 'actions')}><summary>Actions</summary>
      <div class="actions">${btns(MINE_ACTIONS)}</div></details>

    <details class="grp" data-grp="config"${openAttr(c, 'config')}><summary>Configuration</summary>
      <div class="cfg-row">
        <select data-role="getvar">${getOpts}</select>
        <button class="btn" data-getvar="1">Get</button>
      </div>
      <div class="cfg-row">
        <select data-role="setvar">${setOpts}</select>
        <input type="text" data-role="setval" placeholder="value" autocapitalize="off" spellcheck="false">
        <button class="btn btn-primary" data-setvar="1">Set</button>
      </div>
      <p class="hint" data-role="sethint"></p>
    </details>

    <details class="grp" data-grp="gps"${openAttr(c, 'gps')}><summary>Location &amp; GPS</summary>
      <div class="actions">
        ${MINE_GPS_OPS.map((o) => `<button class="btn" data-cli="gps ${o}">gps ${o}</button>`).join('')}
      </div>
      <div class="cfg-row">
        <input type="text" data-role="gpsadv" placeholder="none | share | prefs" autocapitalize="off">
        <button class="btn" data-gpsadv="1">Set advert policy</button>
      </div>
    </details>

    <details class="grp" data-grp="region"${openAttr(c, 'region')}><summary>Region</summary>
      <div class="cfg-row">
        <select data-role="regionop">${MINE_REGION_OPS.map((o) =>
          `<option value="${esc(o)}">${esc(o)}</option>`).join('')}</select>
        <input type="text" data-role="regionarg" placeholder="argument (optional)" autocapitalize="off">
        <button class="btn" data-region="1">Run</button>
      </div>
    </details>

    <details class="grp" data-grp="session"${openAttr(c, 'session')}><summary>Session &amp; permissions</summary>
      <div class="cfg-row">
        <input type="password" data-role="pwd" placeholder="repeater password" autocomplete="off">
        <button class="btn btn-primary" data-savepwd="1">Save &amp; log in</button>
        <button class="btn" data-logout="1">Log out</button>
      </div>
      <div class="cfg-row">
        <input type="text" data-role="newpwd" placeholder="new admin password" autocomplete="off">
        <button class="btn btn-danger" data-newpwd="1" data-confirm="1">Change password</button>
      </div>
      <div class="cfg-row">
        <input type="text" data-role="permwho" placeholder="contact name" autocapitalize="off">
        <input type="text" data-role="permval" placeholder="permission">
        <button class="btn" data-setperm="1">Set permission</button>
      </div>
    </details>

    <details class="grp" data-grp="raw"${openAttr(c, 'raw')}><summary>Raw command</summary>
      <div class="cfg-row">
        <input type="text" data-role="raw" placeholder="sent verbatim to the repeater CLI"
               autocapitalize="off" spellcheck="false">
        <button class="btn" data-raw="1">Send</button>
      </div>
    </details>

    <div class="out rep-out"${RESULTS[c.public_key] ? '' : ' hidden'}>${
      RESULTS[c.public_key] || ''}</div>
  </div>`;
}

// Show the expected value format as the selected variable changes.
$('#mineList').addEventListener('change', (ev) => {
  const sel = ev.target.closest('[data-role="setvar"]');
  if (!sel) return;
  const hint = sel.selectedOptions[0]?.dataset.hint || '';
  const card = sel.closest('.mine');
  $('[data-role="sethint"]', card).textContent = hint ? `expects: ${hint}` : '';
});

async function mineRun(card, fn, label) {
  const key = card.dataset.key;
  const out = $('.rep-out', card);
  out.hidden = false;
  out.innerHTML = '<span class="muted">working…</span>';
  let html;
  try {
    const r = await fn();
    html = present(r);
    if (r.auth && !r.auth.has_password) {
      html += '<div class="fmt-note">This repeater has no saved password. ' +
              'Save one under Session &amp; permissions.</div>';
    }
  } catch (e) {
    html = `<div class="fmt-err">${esc(e.message)}</div>`;
  }
  RESULTS[key] = html;
  out.innerHTML = html;
  await refreshState();
}

$('#mineList').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button');
  if (!btn) return;
  const card = btn.closest('.mine');
  if (!card) return;
  const key = card.dataset.key;
  const d = btn.dataset;
  const val = (role) => ($(`[data-role="${role}"]`, card)?.value || '').trim();

  if (d.confirm && !confirm(`${btn.textContent.trim()} — send to this repeater?`)) return;

  const send = (cmd) => api(`/api/contact/${key}/cmd`,
    { method: 'POST', body: JSON.stringify({ cmd }) });

  await withBusy(btn, async () => {
    if (d.unmine) {
      await api(`/api/contact/${key}/owned`, { method: 'DELETE' });
      await refreshState();
      return;
    }
    if (d.query)  return mineRun(card, () => api(`/api/contact/${key}/req/${d.query}`, { method: 'POST' }));
    if (d.cli)    return mineRun(card, () => send(d.cli));
    if (d.getvar) return mineRun(card, () => send(`get ${$('[data-role="getvar"]', card).value}`));
    if (d.setvar) {
      const v = $('[data-role="setvar"]', card).value;
      const x = val('setval');
      if (!x) return alert('Enter a value to set.');
      return mineRun(card, () => send(`set ${v} ${x}`));
    }
    if (d.gpsadv) {
      const x = val('gpsadv');
      if (!x) return alert('Enter none, share or prefs.');
      return mineRun(card, () => send(`gps advert ${x}`));
    }
    if (d.region) {
      const op = $('[data-role="regionop"]', card).value;
      const arg = val('regionarg');
      return mineRun(card, () => send(`region ${op}${arg ? ' ' + arg : ''}`));
    }
    if (d.savepwd) {
      const pwd = val('pwd');
      if (!pwd) return alert('Enter the repeater password.');
      return mineRun(card, () => api(`/api/contact/${key}/password`,
        { method: 'PUT', body: JSON.stringify({ password: pwd, remember: true }) }));
    }
    if (d.logout)  return mineRun(card, () => api(`/api/contact/${key}/logout`, { method: 'POST' }));
    if (d.newpwd) {
      const x = val('newpwd');
      if (!x) return alert('Enter the new password.');
      return mineRun(card, () => send(`password ${x}`));
    }
    if (d.setperm) {
      const who = val('permwho'), p = val('permval');
      if (!who || !p) return alert('Enter a contact and a permission.');
      return mineRun(card, () => send(`setperm ${who} ${p}`));
    }
    if (d.raw) {
      const x = val('raw');
      if (!x) return;
      return mineRun(card, () => send(x));
    }
  });
});

/* ---------------- repeater console sheet ---------------- */

let sheetKey = null;

function openSheet(key) {
  const c = STATE.contacts.find((x) => x.public_key === key);
  sheetKey = key;
  $('#sheetTitle').textContent = c?.adv_name || 'Repeater';
  const saved = !!c?.has_password;
  $('#sheetBody').innerHTML = `
    <div class="card" style="background:var(--card2);margin-bottom:14px">
      <h2>Password</h2>
      <p class="hint">${saved
        ? 'A password is saved for this repeater and is applied automatically.'
        : 'Management commands need the repeater password. Save it once and it is reused.'}</p>
      <div class="row">
        <input id="repPwd" type="password" placeholder="${saved ? 'replace saved password' : 'repeater password'}"
               autocomplete="off" autocapitalize="off">
        <button class="btn btn-primary" id="repSave">Save &amp; log in</button>
      </div>
      <label class="chk" style="margin:10px 0 0;justify-content:flex-start">
        <input type="checkbox" id="repRemember" checked> remember on the Pi
      </label>
      ${saved ? '<div class="actions" style="margin-top:10px">' +
                '<button class="btn btn-danger" id="repForget">Forget password</button></div>' : ''}
      <pre class="out" id="repAuthOut" style="margin-top:12px" hidden></pre>
    </div>
    <div class="card" style="background:var(--card2)">
      <h2>Send command</h2>
      <div class="row">
        <input id="repCmd" type="text" placeholder="try: get freq, set name X, advert"
               autocapitalize="off" spellcheck="false">
        <button class="btn btn-primary" id="repSend">Send</button>
      </div>
      <div class="quickcmds">
        ${['ver','clock','get name','get freq','get tx','advert','neighbors',
           'log start','log stop']
          .map((q) => `<button class="qc" data-q="${esc(q)}">${esc(q)}</button>`).join('')}
      </div>
      <div class="out" id="repOut" style="margin-top:12px" hidden></div>
    </div>`;
  $('#sheetBackdrop').hidden = false;

  $('#repSave').onclick = (e) => withBusy(e.target, async () => {
    const pwd = $('#repPwd').value;
    if (!pwd) return toast('enter a password', 'err');
    const o = $('#repAuthOut'); o.hidden = false; o.textContent = 'logging in…';
    try {
      const r = await api(`/api/contact/${sheetKey}/password`, {
        method: 'PUT',
        body: JSON.stringify({ password: pwd, remember: $('#repRemember').checked }),
      });
      o.textContent = r.ok
        ? `${r.message}${r.remembered ? ' — saved on the Pi' : ' — not saved'}`
        : `failed: ${r.message}`;
      toast(r.ok ? 'logged in' : r.message, r.ok ? 'ok' : 'err');
      $('#repPwd').value = '';
      await refreshState();
      if (r.ok) openSheet(sheetKey);
    } catch (err) { o.textContent = 'error: ' + err.message; }
  });

  const forget = $('#repForget');
  if (forget) forget.onclick = (e) => withBusy(e.target, async () => {
    try {
      await api(`/api/contact/${sheetKey}/password`, { method: 'DELETE' });
      toast('password forgotten', 'ok');
      await refreshState();
      openSheet(sheetKey);
    } catch (err) { toast(err.message, 'err'); }
  });

  const send = (e) => withBusy(e.target?.dataset?.q ? null : e.target, async () => {
    const cmd = $('#repCmd').value.trim();
    if (!cmd) return;
    showRepOut('<span class="muted">working…</span>');
    try {
      const r = await api(`/api/contact/${sheetKey}/cmd`, {
        method: 'POST', body: JSON.stringify({ cmd }),
      });
      showRepOut(present(r));
    } catch (err) { showRepOut(`<div class="fmt-err">${esc(err.message)}</div>`); }
  });
  $('#repSend').onclick = send;
  $('#repCmd').addEventListener('keydown', (e) => { if (e.key === 'Enter') send(e); });
  $$('#sheetBody .qc').forEach((q) => q.onclick = () => { $('#repCmd').value = q.dataset.q; });
}

function showRepOut(h) { const o = $('#repOut'); o.hidden = false; o.innerHTML = h; }

const closeSheet = () => { $('#sheetBackdrop').hidden = true; sheetKey = null; };
$('#sheetClose').onclick = closeSheet;
$('#sheetBackdrop').addEventListener('click', (e) => {
  if (e.target === $('#sheetBackdrop')) closeSheet();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeSheet(); });

/* ---------------- console tab ---------------- */

const QUICK = ['infos', 'ver', 'clock', 'contacts', 'advert', 'floodadv',
  'self_telemetry', 'get_channels', 'reload_contacts', 'node_discover 2', 'card', '?'];

$('#quickCmds').innerHTML = QUICK
  .map((q) => `<button class="qc" data-q="${esc(q)}">${esc(q)}</button>`).join('');
$$('#quickCmds .qc').forEach((b) => b.onclick = () => {
  $('#cliInput').value = b.dataset.q; $('#cliInput').focus();
});

const history = [];
let histIdx = -1;

function termWrite(text, cls = '') {
  const el = $('#term');
  const div = document.createElement('div');
  if (cls) div.className = cls;
  div.textContent = text;
  el.append(div);
  el.scrollTop = el.scrollHeight;
}

$('#cliForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = $('#cliInput');
  const line = input.value.trim();
  if (!line) return;
  history.push(line); histIdx = history.length;
  input.value = '';
  termWrite('> ' + line, 'term-echo');
  const btn = $('#cliForm button');
  await withBusy(btn, async () => {
    try {
      const r = await api('/api/cli', { method: 'POST', body: JSON.stringify({ line }) });
      termWrite((r.text || '').replace(/\n+$/, '') || '(no output)');
    } catch (err) { termWrite('error: ' + err.message, 'term-err'); }
  });
  input.focus();
});

$('#cliInput').addEventListener('keydown', (e) => {
  if (e.key === 'ArrowUp' && histIdx > 0) {
    e.preventDefault(); $('#cliInput').value = history[--histIdx];
  } else if (e.key === 'ArrowDown') {
    e.preventDefault();
    histIdx = Math.min(histIdx + 1, history.length);
    $('#cliInput').value = history[histIdx] ?? '';
  }
});

$('#btnClearTerm').onclick = () => { $('#term').innerHTML = ''; };
$('#clearResult').onclick = () => { $('#resultCard').hidden = true; };

/* ---------------- node settings ---------------- */

$('#btnRadio').onclick = (e) => withBusy(e.target, async () => {
  const body = {
    freq: parseFloat($('#rFreq').value), bw: parseFloat($('#rBw').value),
    sf: parseInt($('#rSf').value, 10), cr: parseInt($('#rCr').value, 10),
  };
  if (Object.values(body).some((v) => Number.isNaN(v))) return toast('fill all radio fields', 'err');
  if (!confirm(`Set radio to ${body.freq} MHz / BW ${body.bw} / SF ${body.sf} / CR ${body.cr}?\n\n` +
               'Nodes on other settings will stop hearing this one.')) return;
  try {
    await api('/api/node/radio', { method: 'POST', body: JSON.stringify(body) });
    await refreshState();
    toast('radio updated', 'ok');
  } catch (err) { toast(err.message, 'err'); }
});

$$('[data-preset]').forEach((b) => b.onclick = () => {
  const [f, bw, sf, cr] = b.dataset.preset.split(',');
  $('#rFreq').value = f; $('#rBw').value = bw; $('#rSf').value = sf; $('#rCr').value = cr;
});

$('#btnName').onclick = (e) => withBusy(e.target, async () => {
  try {
    await api('/api/node/name', { method: 'POST', body: JSON.stringify({ name: $('#nName').value }) });
    await refreshState();
    toast('name set', 'ok');
  } catch (err) { toast(err.message, 'err'); }
});

$('#btnTx').onclick = (e) => withBusy(e.target, async () => {
  try {
    await api('/api/node/tx-power', {
      method: 'POST', body: JSON.stringify({ tx_power: parseInt($('#nTx').value, 10) }),
    });
    await refreshState();
    toast('tx power set', 'ok');
  } catch (err) { toast(err.message, 'err'); }
});

async function loadAutoAdd() {
  try {
    const r = await api('/api/node/autoadd');
    $('#aaChat').checked = r.chat;     $('#aaRep').checked = r.repeater;
    $('#aaRoom').checked = r.room;     $('#aaSensor').checked = r.sensor;
    $('#aaOver').checked = r.overwrite;
    $('#aaNow').textContent = `currently ${r.hex}` + (r.flags ? '' : ' — nothing is added automatically');
  } catch (e) { /* offline */ }
}

$('#btnAutoAdd').onclick = (e) => withBusy(e.target, async () => {
  try {
    const r = await api('/api/node/autoadd', {
      method: 'POST',
      body: JSON.stringify({
        chat: $('#aaChat').checked, repeater: $('#aaRep').checked,
        room: $('#aaRoom').checked, sensor: $('#aaSensor').checked,
        overwrite: $('#aaOver').checked,
      }),
    });
    $('#aaNow').textContent = `currently ${r.hex}`;
    toast('auto-add set to ' + r.hex, 'ok');
  } catch (err) { toast(err.message, 'err'); }
});

$('#btnImport').onclick = (e) => withBusy(e.target, async () => {
  const uri = $('#importUri').value.trim();
  if (!uri) return;
  try {
    const r = await api('/api/node/import', { method: 'POST', body: JSON.stringify({ uri }) });
    toast('imported', 'ok'); $('#importUri').value = '';
    await refreshState();
  } catch (err) { toast(err.message, 'err'); }
});

$('#btnRefresh').onclick = (e) => withBusy(e.target, async () => {
  try {
    await api('/api/node/contacts/refresh', { method: 'POST' });
    await refreshState();
    toast('contacts reloaded', 'ok');
  } catch (err) { toast(err.message, 'err'); }
});

async function refreshState() { STATE = await api('/api/state'); renderAll(); }


/* ---------------- chat ---------------- */

let CHANNELS = [];
let MESSAGES = [];
let lastMsgId = 0;
// {kind:'channel', id:0} or {kind:'dm', id:'<pubkey>'}
let ACTIVE = { kind: 'channel', id: 0 };
let unread = 0;
const UNREAD = Object.create(null);          // thread key -> count
const threadKey = (m) => (m.kind === 'channel' ? 'c:' + m.channel : 'd:' + m.contact_key);
const activeKey = () => (ACTIVE.kind === 'channel' ? 'c:' + ACTIVE.id : 'd:' + ACTIVE.id);

const chanName = (idx) => {
  const c = CHANNELS.find((x) => x.channel_idx === idx);
  return c ? c.channel_name : `channel ${idx}`;
};

const isActive = (m) => (ACTIVE.kind === 'channel'
  ? m.kind === 'channel' && m.channel === ACTIVE.id
  : m.kind === 'dm' && m.contact_key === ACTIVE.id);

async function loadChat() {
  try {
    const [ch, ms] = await Promise.all([
      api('/api/chat/channels'),
      api('/api/chat/messages?since=0'),
    ]);
    CHANNELS = ch.channels || [];
    MESSAGES = ms.messages || [];
    lastMsgId = ms.last_id || 0;
    renderChatSide();
    renderMessages();
  } catch (e) { /* offline; the websocket will catch us up */ }
}

// Newest traffic first. Message ids are monotonic, so the highest id in a
// thread is its most recent activity; threads that have never carried a
// message keep their natural order underneath.
function lastActivity() {
  const seen = Object.create(null);
  for (const m of MESSAGES) {
    const k = threadKey(m);
    const id = m.id || 0;
    if (!(k in seen) || id > seen[k]) seen[k] = id;
  }
  return seen;
}

function byRecency(items, keyOf) {
  const act = lastActivity();
  return items
    .map((x, i) => ({ x, i, at: act[keyOf(x)] || 0 }))
    .sort((a, b) => (b.at - a.at) || (a.i - b.i))
    .map((e) => e.x);
}

function renderChatSide() {
  const chans = byRecency(CHANNELS, (c) => 'c:' + c.channel_idx);
  const $chanList = $('#chanList');
  $chanList.innerHTML = chans.length
    ? chans.map((c) => {
        const on = ACTIVE.kind === 'channel' && ACTIVE.id === c.channel_idx;
        const n = UNREAD['c:' + c.channel_idx] || 0;
        const nm = c.channel_name.replace(/^#/, '').toLowerCase();
        return `<button class="chat-item${on ? ' on' : ''}"
                  data-kind="channel" data-id="${c.channel_idx}">
                  <span class="hash">#</span><span class="ci-name">${esc(nm)}</span>
                  ${n ? `<span class="ci-badge">${n}</span>` : ''}</button>`;
      }).join('')
    : '<div class="empty" style="padding:10px">no channels</div>';

  const dms = byRecency(STATE.contacts || [], (c) => 'd:' + c.public_key);
  $('#dmList').innerHTML = dms.length
    ? dms.map((c) => {
        const on = ACTIVE.kind === 'dm' && ACTIVE.id === c.public_key;
        const n = UNREAD['d:' + c.public_key] || 0;
        return `<button class="chat-item${on ? ' on' : ''}"
                  data-kind="dm" data-id="${esc(c.public_key)}">
                  <span class="hash">@</span>
                  <span class="ci-name">${esc(c.adv_name || c.key_prefix)}</span>
                  ${n ? `<span class="ci-badge">${n}</span>` : ''}</button>`;
      }).join('')
    : '<div class="empty" style="padding:10px">no contacts yet</div>';
}

function renderMessages() {
  const box = $('#chatMsgs');
  const mine = (STATE.self || {}).name || 'me';
  const list = MESSAGES.filter(isActive);

  if (ACTIVE.kind === 'channel') {
    $('#chatTitle').textContent = '#' + chanName(ACTIVE.id).toLowerCase();
    $('#chatSub').textContent = `slot ${ACTIVE.id}`;
    $('#btnDelChan').hidden = ACTIVE.id === 0;
    $('#chatText').placeholder = 'Message #' + chanName(ACTIVE.id).toLowerCase();
  } else {
    const c = (STATE.contacts || []).find((x) => x.public_key === ACTIVE.id);
    $('#chatTitle').textContent = c?.adv_name || 'direct message';
    $('#chatSub').textContent = c ? c.key_prefix : '';
    $('#btnDelChan').hidden = true;
    $('#chatText').placeholder = 'Message ' + (c?.adv_name || 'contact');
  }

  box.innerHTML = list.length ? list.map((m) => {
    const out = m.dir === 'out';
    const who = out ? mine : (m.sender || (m.kind === 'channel' ? 'unknown' : 'them'));
    const meta = [new Date(m.ts * 1000).toLocaleTimeString()];
    if (m.snr != null) meta.push(`snr ${m.snr}dB`);
    return `<div class="msg${out ? ' msg-out' : ''}">
      <div class="msg-who">${esc(who)}</div>
      <div class="msg-body">${esc(m.text)}</div>
      <div class="msg-meta">${esc(meta.join('   '))}</div>
    </div>`;
  }).join('') : '<div class="empty">Nothing here yet. Anything you send goes out over the air.</div>';
  box.scrollTop = box.scrollHeight;
}

function onChatMessage(m) {
  // A reconnect can redeliver a message we already have; counting it again
  // would resurrect a badge the user has already cleared.
  if (m.id != null && MESSAGES.some((x) => x.id === m.id)) return;
  MESSAGES.push(m);
  lastMsgId = Math.max(lastMsgId, m.id);
  const onThisThread = isActive(m) && $('#tab-chat').classList.contains('active');
  if (onThisThread) {
    renderMessages();
    return;
  }
  if (m.dir !== 'in') return;
  const k = threadKey(m);
  UNREAD[k] = (UNREAD[k] || 0) + 1;
  unread = Object.values(UNREAD).reduce((a, b) => a + b, 0);
  const b = $('#chatBadge');
  b.hidden = false;
  b.textContent = unread;
  renderChatSide();
}

function clearUnread() {
  delete UNREAD[activeKey()];
  unread = Object.values(UNREAD).reduce((a, b) => a + b, 0);
  const b = $('#chatBadge');
  b.textContent = unread;
  b.hidden = unread === 0;
  renderChatSide();
}

$('#chanList').addEventListener('click', (e) => selectThread(e));
$('#dmList').addEventListener('click', (e) => selectThread(e));

function selectThread(e) {
  const b = e.target.closest('.chat-item');
  if (!b) return;
  ACTIVE = { kind: b.dataset.kind,
             id: b.dataset.kind === 'channel' ? Number(b.dataset.id) : b.dataset.id };
  clearUnread();
  renderChatSide();
  renderMessages();
  $('#chatText').focus();
}

$('#chatForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = $('#chatText');
  const text = input.value.trim();
  if (!text) return;
  const btn = $('#chatForm button');
  await withBusy(btn, async () => {
    try {
      const body = ACTIVE.kind === 'channel'
        ? { text, channel: ACTIVE.id } : { text, contact_key: ACTIVE.id };
      await api('/api/chat/send', { method: 'POST', body: JSON.stringify(body) });
      input.value = '';
    } catch (err) { toast(err.message, 'err'); }
  });
  input.focus();
});

$('#btnAddChan').onclick = () => {
  $('#chanName').value = ''; $('#chanKey').value = '';
  $('#chanOut').hidden = true;
  $('#chanBackdrop').hidden = false;
  $('#chanName').focus();
};
const closeChan = () => { $('#chanBackdrop').hidden = true; };
$('#chanClose').onclick = closeChan;
$('#chanBackdrop').addEventListener('click', (e) => {
  if (e.target === $('#chanBackdrop')) closeChan();
});

$('#chanCreate').onclick = (e) => withBusy(e.target, async () => {
  let name = $('#chanName').value.trim();
  const key = $('#chanKey').value.trim();
  if (!name) return toast('name the channel', 'err');
  // The key derivation hashes the name verbatim, so '#tejas' and 'tejas' are
  // different channels. Default to the '#' form the other apps use.
  if (!key && !name.startsWith('#')) name = '#' + name;
  if (key && !/^[0-9a-fA-F]{32}$/.test(key)) {
    return toast('key must be 32 hex characters', 'err');
  }
  const o = $('#chanOut'); o.hidden = false; o.textContent = 'creating…';
  try {
    const r = await api('/api/chat/channels', {
      method: 'POST', body: JSON.stringify({ name, key: key || null }),
    });
    await loadChat();
    ACTIVE = { kind: 'channel', id: r.channel_idx };
    renderChatSide(); renderMessages();
    closeChan();
    const k = r.channel && r.channel.channel_secret;
    toast(`${name.toLowerCase()} added${r.derived_key && k
      ? ' (key ' + k.slice(0, 8) + '…)' : ''}`, 'ok');
  } catch (err) { o.textContent = 'error: ' + err.message; }
});

$('#btnDelChan').onclick = (e) => withBusy(e.target, async () => {
  if (ACTIVE.kind !== 'channel' || ACTIVE.id === 0) return;
  if (!confirm(`Remove #${chanName(ACTIVE.id).toLowerCase()}?`)) return;
  try {
    await api(`/api/chat/channels/${ACTIVE.id}`, { method: 'DELETE' });
    ACTIVE = { kind: 'channel', id: 0 };
    await loadChat();
    toast('channel removed', 'ok');
  } catch (err) { toast(err.message, 'err'); }
});


/* ---------------- contacts ---------------- */

let ctcFilter = 'all';
let ctcQuery = '';

function renderContacts() {
  const list = $('#ctcList');
  if (!list) return;
  const q = ctcQuery.trim().toLowerCase();
  const all = STATE.contacts || [];
  $('#ctcCount').textContent = all.length;

  const shown = all.filter((c) => {
    if (ctcFilter !== 'all' && String(c.type) !== ctcFilter) return false;
    if (!q) return true;
    return (c.adv_name || '').toLowerCase().includes(q)
        || (c.public_key || '').toLowerCase().includes(q);
  });

  if (!shown.length) {
    list.innerHTML = `<div class="card"><div class="empty">${
      all.length ? 'No contacts match that filter.'
                 : 'No contacts yet. Nodes appear here once they advertise — ' +
                   'check Auto-add on the Node tab.'}</div></div>`;
    return;
  }
  list.innerHTML = shown.map(ctcCard).join('');
}

function ctcCard(c) {
  const t = c.type;
  const tag = t === 2 ? '<span class="tag tag-rep">repeater</span>'
            : t === 3 ? '<span class="tag tag-room">room</span>'
            : t === 4 ? '<span class="tag">sensor</span>'
            : '<span class="tag tag-chat">companion</span>';
  const path = (c.out_path_len === -1 || c.out_path_len == null)
    ? 'flood' : `${c.out_path_len} hop${c.out_path_len === 1 ? '' : 's'}`;
  const loc = c.adv_lat ? `${c.adv_lat.toFixed(4)}, ${c.adv_lon.toFixed(4)}` : null;
  return `<div class="ctc" data-key="${esc(c.public_key)}">
    <div class="rep-head">
      <div>
        <div class="rep-name">${esc(c.adv_name || '(unnamed)')}</div>
        <div class="rep-key">${esc(c.key_prefix)}</div>
      </div>${tag}
    </div>
    <div class="rep-meta">
      <span>${esc(path)}</span>
      <span>heard ${esc(ago(c.last_advert))}</span>
      ${loc ? `<span>${esc(loc)}</span>` : ''}
    </div>
    <div class="actions">
      <button class="btn btn-primary" data-ctc="msg">Message</button>
      <button class="btn" data-ctc="path/discover">Find path</button>
      <button class="btn" data-ctc="path/reset">Reset path</button>
      <button class="btn" data-ctc="export">Share URI</button>
      <button class="btn btn-danger" data-ctc="remove">Remove</button>
    </div>
    <div class="out ctc-out" hidden></div>
  </div>`;
}

$('#ctcSearch').addEventListener('input', (e) => {
  ctcQuery = e.target.value; renderContacts();
});
$('#repSearch').addEventListener('input', (e) => {
  repQuery = e.target.value; renderRepeaters();
});
$('#ctcFilter').addEventListener('click', (e) => {
  const b = e.target.closest('.seg-btn');
  if (!b) return;
  $$('#ctcFilter .seg-btn').forEach((x) => x.classList.remove('on'));
  b.classList.add('on');
  ctcFilter = b.dataset.f;
  renderContacts();
});
$('#ctcRefresh').onclick = (e) => withBusy(e.target, async () => {
  try {
    await api('/api/node/contacts/refresh', { method: 'POST' });
    await refreshState();
    toast('contacts reloaded', 'ok');
  } catch (err) { toast(err.message, 'err'); }
});

$('#ctcList').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('[data-ctc]');
  if (!btn) return;
  const card = btn.closest('.ctc');
  const key = card.dataset.key;
  const act = btn.dataset.ctc;
  const out = $('.ctc-out', card);
  const c = (STATE.contacts || []).find((x) => x.public_key === key);

  if (act === 'msg') {
    ACTIVE = { kind: 'dm', id: key };
    $$('.tab').forEach((x) => x.classList.remove('active'));
    $$('.panel').forEach((x) => x.classList.remove('active'));
    $('.tab[data-tab="chat"]').classList.add('active');
    $('#tab-chat').classList.add('active');
    clearUnread();
    renderChatSide(); renderMessages(); $('#chatText').focus();
    return;
  }
  if (act === 'remove' && !confirm(`Remove ${c?.adv_name || 'this contact'}?`)) return;

  await withBusy(btn, async () => {
    out.hidden = false;
    out.innerHTML = '<span class="muted">working…</span>';
    try {
      const r = act === 'export'
        ? await api(`/api/contact/${key}/export`)
        : act === 'remove'
          ? await api(`/api/contact/${key}`, { method: 'DELETE' })
          : await api(`/api/contact/${key}/${act}`, { method: 'POST' });
      out.innerHTML = present(r);
      if (act === 'remove') { await refreshState(); return; }
    } catch (e) {
      out.innerHTML = `<div class="fmt-err">${esc(e.message)}</div>`;
    }
  });
});

/* ---------------- tabs ---------------- */

$$('.tab').forEach((t) => t.onclick = () => {
  $$('.tab').forEach((x) => x.classList.remove('active'));
  $$('.panel').forEach((p) => p.classList.remove('active'));
  t.classList.add('active');
  $('#tab-' + t.dataset.tab).classList.add('active');
  if (t.dataset.tab === 'console') $('#cliInput').focus();
  if (t.dataset.tab === 'chat') {
    clearUnread();
    renderMessages(); $('#chatText').focus();
  }
});

/* ---------------- boot ---------------- */

logEl().innerHTML = '<div class="empty">Listening. Anything the radio hears shows up here.</div>';
refreshState().catch(() => setLink(false));
loadChat();
loadAutoAdd();
connectWS();
