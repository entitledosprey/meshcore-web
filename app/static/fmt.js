'use strict';
/* Turns MeshCore's wire payloads into something a person can read.
   Field names and units come from meshcore/parsing.py and reader.py. */

const F = (() => {
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  /* ---------- scalar helpers ---------- */

  function dur(sec) {
    sec = Math.max(0, Math.floor(Number(sec) || 0));
    const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600);
    const m = Math.floor(sec % 3600 / 60), s = sec % 60;
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}h ${m}m`;
    if (m) return `${m}m ${s}s`;
    return `${s}s`;
  }

  function agoSecs(sec) {
    sec = Number(sec);
    if (!isFinite(sec) || sec < 0) return '—';
    return sec < 60 ? `${Math.floor(sec)}s ago` : dur(sec) + ' ago';
  }

  const volts = (mv) => (Number(mv) > 100 ? (Number(mv) / 1000).toFixed(2) + ' V'
                                          : Number(mv) + ' mV');
  const shortKey = (k) => String(k || '').slice(0, 12);
  const nodeType = (t) => ({ 1: 'companion', 2: 'repeater', 3: 'room', 4: 'sensor' }[t]
                            || (t == null ? 'node' : 'type ' + t));

  /* ---------- field labels + units ---------- */

  const LABEL = {
    bat: 'Battery', tx_queue_len: 'TX queue', noise_floor: 'Noise floor',
    last_rssi: 'Last RSSI', last_snr: 'Last SNR', nb_recv: 'Packets received',
    nb_sent: 'Packets sent', airtime: 'TX airtime', rx_airtime: 'RX airtime',
    uptime: 'Uptime', sent_flood: 'Sent (flood)', sent_direct: 'Sent (direct)',
    recv_flood: 'Received (flood)', recv_direct: 'Received (direct)',
    full_evts: 'Dropped events', direct_dups: 'Duplicates (direct)',
    flood_dups: 'Duplicates (flood)', recv_errors: 'Receive errors',
    pubkey_pre: 'Node', pubkey_prefix: 'Node', pubkey: 'Public key',
    public_key: 'Public key', adv_name: 'Name', node_type: 'Type',
    snr: 'SNR', rssi: 'RSSI', SNR: 'SNR', RSSI: 'RSSI', SNR_in: 'SNR (inbound)',
    secs_ago: 'Last heard', path_len: 'Path length', recv_time: 'Received',
    results_count: 'Returned', neighbours_count: 'Neighbours',
    radio_freq: 'Frequency', radio_bw: 'Bandwidth', radio_sf: 'Spreading factor',
    radio_cr: 'Coding rate', tx_power: 'TX power', max_tx_power: 'Max TX power',
    out_path_len: 'Path hops', last_advert: 'Last advert', tag: 'Tag',
  };

  const label = (k) => LABEL[k]
    || String(k).replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());

  function value(k, v) {
    if (v === null || v === undefined || v === '') return '—';
    switch (k) {
      case 'bat': return volts(v);
      case 'uptime': case 'airtime': case 'rx_airtime': return dur(v);
      case 'secs_ago': return agoSecs(v);
      case 'noise_floor': case 'last_rssi': case 'rssi': case 'RSSI':
        return v + ' dBm';
      case 'last_snr': case 'snr': case 'SNR': case 'SNR_in': return v + ' dB';
      case 'radio_freq': return v + ' MHz';
      case 'radio_bw': return v + ' kHz';
      case 'tx_power': case 'max_tx_power': return v + ' dBm';
      case 'node_type': case 'type': return nodeType(v);
      case 'recv_time': case 'last_advert':
        return Number(v) > 1e9 ? new Date(v * 1000).toLocaleString() : String(v);
      case 'pubkey': case 'public_key': case 'pubkey_pre': case 'pubkey_prefix':
        return shortKey(v);
      default:
        if (typeof v === 'boolean') return v ? 'yes' : 'no';
        if (typeof v === 'object') return JSON.stringify(v);
        return String(v);
    }
  }

  /* ---------- block renderers ---------- */

  const rows = (pairs) => `<dl class="kv">${pairs
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`;

  const generic = (o) => rows(Object.entries(o)
    .filter(([k]) => k !== 'tag')
    .map(([k, v]) => [label(k), value(k, v)]));

  function telemetry(d) {
    const UNIT = { voltage: ' V', temperature: ' °C', humidity: ' %',
                   luminosity: ' lux', pressure: ' hPa' };
    const items = (d.lpp || []).map((s) => {
      const name = String(s.type || 'value').replace(/^./, (c) => c.toUpperCase());
      const u = UNIT[s.type] || '';
      const ch = (d.lpp.filter((x) => x.type === s.type).length > 1)
        ? ` (ch ${s.channel})` : '';
      return [name + ch, `${s.value}${u}`];
    });
    if (!items.length) return '<span class="muted">no sensor data</span>';
    const who = d.pubkey_pre || d.pubkey_prefix;
    return (who ? `<div class="fmt-head">Telemetry from ${esc(shortKey(who))}</div>` : '')
      + rows(items);
  }

  function neighbours(d) {
    const list = d.neighbours || [];
    const head = `<div class="fmt-head">${list.length} of ${d.neighbours_count ?? list.length} neighbour${
      (d.neighbours_count ?? list.length) === 1 ? '' : 's'}</div>`;
    if (!list.length) return head + '<span class="muted">none reported</span>';
    const sorted = [...list].sort((a, b) => (a.secs_ago ?? 1e9) - (b.secs_ago ?? 1e9));
    return head + `<table class="fmt-table">
      <thead><tr><th>Node</th><th>SNR</th><th>Last heard</th></tr></thead>
      <tbody>${sorted.map((n) => `<tr>
        <td class="mono">${esc(shortKey(n.pubkey))}</td>
        <td>${esc(n.snr)} dB</td>
        <td>${esc(agoSecs(n.secs_ago))}</td></tr>`).join('')}</tbody></table>`;
  }

  function status(d) {
    const g = (k) => value(k, d[k]);
    const head = `<div class="fmt-head">Up ${esc(dur(d.uptime))} · battery ${esc(volts(d.bat))}</div>`;
    const order = ['bat', 'uptime', 'last_snr', 'last_rssi', 'noise_floor',
      'tx_queue_len', 'nb_sent', 'nb_recv', 'sent_direct', 'sent_flood',
      'recv_direct', 'recv_flood', 'direct_dups', 'flood_dups',
      'airtime', 'rx_airtime', 'full_evts', 'recv_errors'];
    const pairs = order.filter((k) => d[k] !== undefined).map((k) => [label(k), g(k)]);
    return head + rows(pairs);
  }

  /* ---------- entry points ---------- */

  /** Render a command result object ({text, data}) as HTML. */
  function result(r) {
    const d = r && r.data;
    if (d && typeof d === 'object' && !Array.isArray(d)) {
      if (d.error) return `<div class="fmt-err">${esc(d.error)}</div>`;
      if (Array.isArray(d.lpp)) return telemetry(d);
      if (Array.isArray(d.neighbours)) return neighbours(d);
      if (d.uptime !== undefined && d.bat !== undefined) return status(d);
      return generic(d);
    }
    const t = ((r && r.text) || '').trim();
    if (!t) return '<span class="muted">(no output)</span>';
    // meshcli printed prose, or JSON we didn't recognise -- show it as-is.
    return `<pre class="raw">${esc(t)}</pre>`;
  }

  /** One-line human summary of a live event for the log panel. */
  function event(kind, p) {
    const o = (p && typeof p === 'object') ? p : {};
    switch (kind) {
      case 'RX_LOG_DATA': {
        const bytes = o.payload ? Math.floor(String(o.payload).length / 2) : null;
        return `RF packet · SNR ${o.snr ?? '?'} dB · RSSI ${o.rssi ?? '?'} dBm`
             + (bytes ? ` · ${bytes} B` : '');
      }
      case 'DISCOVER_RESPONSE':
        return `found ${nodeType(o.node_type)} ${shortKey(o.pubkey)}`
             + ` · SNR ${o.SNR ?? '?'} dB · RSSI ${o.RSSI ?? '?'} dBm`;
      case 'ADVERTISEMENT':
        return `advert from ${o.adv_name || shortKey(o.public_key || o.pubkey) || 'unknown'}`;
      case 'NEW_CONTACT':
        return `new contact: ${o.adv_name || shortKey(o.public_key)}`;
      case 'CONTACT_DELETED':
        return `contact removed: ${shortKey(o.public_key || o.pubkey)}`;
      case 'TELEMETRY_RESPONSE': {
        const bits = (o.lpp || []).map((s) => {
          const u = { voltage: 'V', temperature: '°C', humidity: '%' }[s.type] || '';
          return `${s.value}${u ? ' ' + u : ''}`;
        });
        return `telemetry from ${shortKey(o.pubkey_pre || o.pubkey_prefix)}`
             + (bits.length ? ` · ${bits.join(', ')}` : '');
      }
      case 'STATUS_RESPONSE':
        return `status from ${shortKey(o.pubkey_pre || o.pubkey_prefix)}`
             + (o.uptime !== undefined ? ` · up ${dur(o.uptime)}` : '')
             + (o.bat !== undefined ? ` · ${volts(o.bat)}` : '');
      case 'NEIGHBOURS_RESPONSE':
        return `${o.results_count ?? (o.neighbours || []).length} neighbour(s) reported`;
      case 'CONTACT_MSG_RECV':
        return `message from ${shortKey(o.pubkey_pre)}: ${o.text ?? ''}`;
      case 'CHANNEL_MSG_RECV':
        return `channel ${o.channel_idx ?? '?'}: ${o.text ?? ''}`;
      case 'PATH_UPDATE':
        return `path updated for ${shortKey(o.public_key || o.pubkey)}`;
      case 'LOGIN_SUCCESS': return `logged in to ${shortKey(o.pubkey_pre)}`;
      case 'LOGIN_FAILED':  return `login failed for ${shortKey(o.pubkey_pre)}`;
      case 'ACK':           return 'message acknowledged';
      case 'CONTACTS':      return 'contact list refreshed';
      case 'DEVICE_INFO':   return `${o.model || 'device'} · ${o.ver || ''}`.trim();
      case 'SELF_INFO':     return `node info · ${o.name || ''}`.trim();
      case 'CURRENT_TIME':  return o.time ? new Date(o.time * 1000).toLocaleString() : 'clock read';
      case 'clock_synced':
        return `node clock corrected (was off by ${dur(Math.abs(o.drift_seconds || 0))})`;
      case 'connected':     return `radio connected on ${o.port || '?'}`;
      case 'disconnected':  return `radio disconnected — ${o.reason || 'unknown'}`;
      case 'DISCONNECTED':  return `link lost — ${o.reason || 'unknown'}`;
      case 'ERROR':         return `device error: ${o.code_string || o.error_code || '?'}`;
      case 'error':         return String(o.error || 'error');
      default: {
        const keys = Object.keys(o);
        if (!keys.length) return '';
        return keys.slice(0, 4)
          .map((k) => `${label(k).toLowerCase()} ${value(k, o[k])}`).join(' · ');
      }
    }
  }

  return { result, event, dur, agoSecs, volts, shortKey, nodeType, esc, label, value };
})();
