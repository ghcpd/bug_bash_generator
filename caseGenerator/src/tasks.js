import { CATEGORIES, DIFFS } from './constants.js';
import { toast, esc } from './utils.js';

export function initSelects() {
  document.getElementById('bulkCat').innerHTML = CATEGORIES.map(c => `<option value="${c}">${c || 'AI decides'}</option>`).join('');
  document.getElementById('bulkDiff').innerHTML = DIFFS.map(d => `<option value="${d}">${d || 'AI decides'}</option>`).join('');
}

export function getExistingRepos() {
  const s = new Set();
  for (const tr of document.getElementById('taskBody').children) {
    const u = tr.querySelector('input[type=text]').value.trim().toLowerCase().replace(/\/+$/, '');
    if (u) s.add(u);
  }
  return s;
}

export function bulkAdd() {
  const raw = document.getElementById('bulkUrls').value;
  let urls = raw.split(/[\n,]+/).map(s => s.trim()).filter(s => s);
  if (!urls.length) { alert('Paste at least one URL'); return; }
  for (const tr of [...document.getElementById('taskBody').children])
    if (!tr.querySelector('input[type=text]').value.trim()) tr.remove();
  const seen = new Set();
  urls = urls.filter(u => { const k = u.toLowerCase().replace(/\/+$/, ''); if (seen.has(k)) return false; seen.add(k); return true; });
  const existing = getExistingRepos();
  const fresh = urls.filter(u => !existing.has(u.toLowerCase().replace(/\/+$/, '')));
  const skip = urls.length - fresh.length;
  const cat = document.getElementById('bulkCat').value, diff = document.getElementById('bulkDiff').value, num = parseInt(document.getElementById('bulkNum').value) || 3;
  for (const u of fresh) addRow(u, cat, diff, num);
  document.getElementById('bulkUrls').value = '';
  toast(skip > 0 ? `Added ${fresh.length}, skipped ${skip} dups` : `Added ${fresh.length} tasks`);
}

export function addRow(repo = '', cat = '', diff = '', num = 3) {
  const catOpts = CATEGORIES.map(c => `<option value="${c}">${c || 'AI decides'}</option>`).join('');
  const diffOpts = DIFFS.map(d => `<option value="${d}">${d || 'AI decides'}</option>`).join('');
  const tr = document.createElement('tr');
  tr.innerHTML = `
    <td><input type="text" value="${repo}" placeholder="https://github.com/owner/repo"></td>
    <td class="col-cat"><select>${catOpts}</select></td>
    <td><select>${diffOpts}</select></td>
    <td><input type="number" value="${num}" min="1" max="20"></td>
    <td style="text-align:center"><button class="btn-del" onclick="this.closest('tr').remove();window._app.updateCount()">✕</button></td>`;
  document.getElementById('taskBody').appendChild(tr);
  if (cat) tr.querySelectorAll('select')[0].value = cat;
  if (diff) tr.querySelectorAll('select')[1].value = diff;
  updateCount();
}

export function updateCount() {
  const n = document.getElementById('taskBody').children.length;
  document.getElementById('taskCount').textContent = n > 0 ? `${n} tasks` : '';
}

export function getTasks() {
  const tasks = [];
  for (const tr of document.getElementById('taskBody').children) {
    const repo = tr.querySelector('input[type=text]').value.trim(); if (!repo) continue;
    const cat = tr.querySelectorAll('select')[0].value, diff = tr.querySelectorAll('select')[1].value;
    const num = parseInt(tr.querySelector('input[type=number]').value) || 3;
    const t = { repo }; if (cat) t.category = cat; if (diff) t.difficulty = diff; if (num !== 3) t.num_cases = num;
    tasks.push(t);
  }
  return tasks;
}
