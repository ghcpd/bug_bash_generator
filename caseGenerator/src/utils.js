import { STEPS } from './constants.js';

export function toast(m) {
  const t = document.getElementById('toast');
  t.textContent = m;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2200);
}

export function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

export function toggleCollapse(el) {
  el.classList.toggle('open');
  el.nextElementSibling.classList.toggle('collapsed');
}

export function clipboardWrite(text) {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.cssText = 'position:fixed;left:-9999px';
  document.body.appendChild(ta); ta.select();
  document.execCommand('copy');
  document.body.removeChild(ta);
  return Promise.resolve();
}

export async function clipboardRead() {
  if (navigator.clipboard?.readText) return navigator.clipboard.readText();
  throw new Error('Clipboard read not available on HTTP — please paste tokens into the text box below instead');
}

export function copyText(text, label) {
  clipboardWrite(text).then(() => toast((label || 'Text') + ' copied!'));
}

export function goStep(name) {
  STEPS.forEach(s => {
    const el = document.getElementById('step-' + s);
    el.classList.toggle('active', s === name);
  });
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === 'panel-' + name));
  try { localStorage.setItem('swebench_active_tab', name); } catch {}
}

export function fmtDuration(start, end) {
  if (!start) return '-';
  const sec = Math.floor(((end ? new Date(end) : new Date()) - new Date(start)) / 1000);
  if (sec < 60) return sec + 's';
  if (sec < 3600) return Math.floor(sec / 60) + 'm ' + (sec % 60) + 's';
  return Math.floor(sec / 3600) + 'h ' + Math.floor((sec % 3600) / 60) + 'm';
}

export function fmtTime(iso) {
  if (!iso) return '-';
  return new Date(iso).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
}

export function fmtBytes(b) {
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
  return (b / 1048576).toFixed(1) + ' MB';
}

export function statusBadge(status) {
  const s = (status || '').toLowerCase().replace(/\s/g, '');
  let c = 'badge-queued';
  if (s === 'succeeded') c = 'badge-succeeded';
  else if (s === 'failed') c = 'badge-failed';
  else if (s === 'inprogress') c = 'badge-inprogress';
  else if (s === 'cancelling' || s === 'cancelled') c = 'badge-cancelled';
  return `<span class="badge ${c}">${esc(status || 'Unknown')}</span>`;
}
