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

  ws.onopen = () => { wsRetry = 1000; };
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

function renderRepeaters() {
  const list = $('#repeaterList');
  const cs = (STATE.contacts || []);
  if (!cs.length) {
    list.innerHTML = `<div class="card"><div class="empty">
      No contacts yet.<br><br>Your node hears repeaters only when they advertise
      <b>on the same radio settings</b>. Check the Node tab, then use
      <b>Discover repeaters</b> or import a contact URI above.</div></div>`;
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
      <span>path: ${esc(path)}</span>
      <span>advert: ${esc(ago(c.last_advert))}</span>
      ${auth}
      ${c.adv_lat ? `<span>${c.adv_lat.toFixed(4)}, ${c.adv_lon.toFixed(4)}</span>` : ''}
    </div>
    <div class="actions">
      <button class="btn" data-rep="neighbours">Neighbours</button>
      <button class="btn" data-rep="status">Status</button>
      <button class="btn" data-rep="telemetry">Telemetry</button>
      <button class="btn" data-rep="path/discover">Find path</button>
      <button class="btn" data-rep="console">Manage…</button>
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
        <input id="repCmd" type="text" placeholder="e.g. get freq · set name X · advert"
               autocapitalize="off" spellcheck="false">
        <button class="btn btn-primary" id="repSend">Send</button>
      </div>
      <div class="quickcmds">
        ${['ver','clock','get freq','get tx','advert','neighbors','status','log start','log stop']
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

function renderChatSide() {
  $('#chanList').innerHTML = CHANNELS.length
    ? CHANNELS.map((c) => {
        const on = ACTIVE.kind === 'channel' && ACTIVE.id === c.channel_idx;
        return `<button class="chat-item${on ? ' on' : ''}"
                  data-kind="channel" data-id="${c.channel_idx}">
                  <span class="hash">#</span>${esc(c.channel_name.toLowerCase())}</button>`;
      }).join('')
    : '<div class="empty" style="padding:10px">no channels</div>';

  const dms = (STATE.contacts || []);
  $('#dmList').innerHTML = dms.length
    ? dms.map((c) => {
        const on = ACTIVE.kind === 'dm' && ACTIVE.id === c.public_key;
        return `<button class="chat-item${on ? ' on' : ''}"
                  data-kind="dm" data-id="${esc(c.public_key)}">
                  <span class="hash">@</span>${esc(c.adv_name || c.key_prefix)}</button>`;
      }).join('')
    : '<div class="empty" style="padding:10px">no contacts yet</div>';
}

function renderMessages() {
  const box = $('#chatMsgs');
  const mine = (STATE.self || {}).name || 'me';
  const list = MESSAGES.filter(isActive);

  if (ACTIVE.kind === 'channel') {
    $('#chatTitle').textContent = '#' + chanName(ACTIVE.id).toLowerCase();
    $('#chatSub').textContent = `channel ${ACTIVE.id}`;
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
    if (m.snr != null) meta.push(`SNR ${m.snr}`);
    return `<div class="msg${out ? ' msg-out' : ''}">
      <div class="msg-who">${esc(who)}</div>
      <div class="msg-body">${esc(m.text)}</div>
      <div class="msg-meta">${esc(meta.join(' · '))}</div>
    </div>`;
  }).join('') : '<div class="empty">No messages yet. Say something.</div>';
  box.scrollTop = box.scrollHeight;
}

function onChatMessage(m) {
  MESSAGES.push(m);
  lastMsgId = Math.max(lastMsgId, m.id);
  if (isActive(m) && $('#tab-chat').classList.contains('active')) {
    renderMessages();
  } else if (m.dir === 'in') {
    unread++;
    const b = $('#chatBadge');
    b.hidden = false;
    b.textContent = unread;
  }
}

$('#chanList').addEventListener('click', (e) => selectThread(e));
$('#dmList').addEventListener('click', (e) => selectThread(e));

function selectThread(e) {
  const b = e.target.closest('.chat-item');
  if (!b) return;
  ACTIVE = { kind: b.dataset.kind,
             id: b.dataset.kind === 'channel' ? Number(b.dataset.id) : b.dataset.id };
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
  const name = $('#chanName').value.trim();
  const key = $('#chanKey').value.trim();
  if (!name) return toast('name the channel', 'err');
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
    toast(`#${name.toLowerCase()} added`, 'ok');
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
      <span>path: ${esc(path)}</span>
      <span>advert: ${esc(ago(c.last_advert))}</span>
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
    unread = 0; $('#chatBadge').hidden = true;
    renderMessages(); $('#chatText').focus();
  }
});

/* ---------------- boot ---------------- */

logEl().innerHTML = '<div class="empty">waiting for events…</div>';
refreshState().catch(() => setLink(false));
loadChat();
loadAutoAdd();
connectWS();
