import { toast, esc, copyText, goStep, clipboardWrite } from './utils.js';
import { azFetch } from './azure.js';
import { getTasks, addRow, updateCount } from './tasks.js';
import { STORAGE_KEY } from './constants.js';
import { refreshMonitor } from './monitor.js';
import { saveState } from './persistence.js';

let _expandedRepoList = null;

export function getExpandedRepoList() { return _expandedRepoList; }
export function setExpandedRepoList(val) { _expandedRepoList = val; }

export function generate() {
  const tasks = getTasks();
  if (!tasks.length) { alert('Add at least one task'); return; }
  const expanded = [];
  for (const t of tasks) {
    const n = t.num_cases || 3;
    for (let i = 0; i < n; i++) {
      const item = { repo: t.repo, case_index: i };
      if (t.category) item.category = t.category;
      if (t.difficulty) item.difficulty = t.difficulty;
      expanded.push(item);
    }
  }
  _expandedRepoList = expanded;
  const totalCases = expanded.length;
  const repoMap = {};
  for (const e of expanded) {
    if (!repoMap[e.repo]) repoMap[e.repo] = { count: 0, cat: e.category || 'AI decides', diff: e.difficulty || 'AI decides' };
    repoMap[e.repo].count++;
  }
  const repos = Object.keys(repoMap).length;

  let html = `<div class="summary-grid">
    <div class="stat-card"><div class="stat-val">${repos}</div><div class="stat-label">Repositories</div></div>
    <div class="stat-card"><div class="stat-val">${totalCases}</div><div class="stat-label">Batch Nodes</div></div>
    <div class="stat-card"><div class="stat-val" style="color:var(--purple)">${document.getElementById('azModel').value.split('-').pop()}</div><div class="stat-label">Model</div></div>
    <div class="stat-card"><div class="stat-val" style="color:var(--orange)">${document.getElementById('azTimeout').value}s</div><div class="stat-label">Timeout</div></div>
  </div>`;
  html += `<div style="overflow-x:auto"><table class="summary-table"><tr><th>Repository</th><th>Cases</th><th>Category</th><th>Difficulty</th></tr>`;
  for (const [repo, info] of Object.entries(repoMap)) {
    html += `<tr><td style="font-family:monospace;font-size:12px;color:var(--accent)">${esc(repo.replace('https://github.com/', ''))}</td>
      <td style="font-weight:600">${info.count}</td><td style="color:var(--muted);font-size:12px">${esc(info.cat)}</td>
      <td style="color:var(--muted);font-size:12px">${esc(info.diff)}</td></tr>`;
  }
  html += '</table></div>';
  html += `<div style="margin-top:12px;display:flex;gap:8px;align-items:center">
    <button class="btn-copy" onclick="window._app.cpRepoList()">📋 Copy JSON</button>
    <details style="flex:1"><summary style="cursor:pointer;font-size:11px;color:var(--muted)">Raw repo_list JSON</summary>
    <pre style="margin-top:6px;padding:10px;background:var(--bg);border:1px solid var(--border);border-radius:6px;font-size:11px;overflow-x:auto;white-space:pre-wrap;color:var(--green);max-height:200px">${esc(JSON.stringify(expanded, null, 2))}</pre>
    </details></div>`;
  document.getElementById('resultBody').innerHTML = html;
  document.getElementById('resultPanel').classList.add('visible');
  document.getElementById('btnTrigger').disabled = false;
  toast('Plan generated — ready to trigger');
}

export function cpRepoList() {
  if (!_expandedRepoList) return;
  clipboardWrite(JSON.stringify(_expandedRepoList)).then(() => toast('JSON copied!'));
}

export function resetAll() {
  document.getElementById('taskBody').innerHTML = '';
  document.getElementById('resultPanel').classList.remove('visible');
  document.getElementById('btnTrigger').disabled = true;
  const s = document.getElementById('triggerStatus'); s.className = 'status-banner'; s.innerHTML = '';
  _expandedRepoList = null; updateCount(); addRow();
  localStorage.removeItem(STORAGE_KEY); toast('Reset');
}

export async function triggerPipeline() {
  if (!_expandedRepoList?.length) { alert('Generate first'); return; }
  const el = document.getElementById('triggerStatus'), btn = document.getElementById('btnTrigger');
  btn.disabled = true;
  el.className = 'status-banner show'; el.style.background = 'var(--surface2)'; el.style.border = '1px solid var(--border)'; el.style.color = 'var(--muted)';
  el.innerHTML = '<span class="spin"></span> Triggering pipeline…';
  try {
    const params = { repo_list: _expandedRepoList };
    const gh = document.getElementById('azGhToken').value.trim();
    if (gh) {
      if (gh.startsWith('{') || gh.startsWith('[')) {
        el.className = 'status-banner show err'; el.style = '';
        el.innerHTML = '✕ GitHub Token looks like JSON (possibly pasted from token import). Please paste a raw ghp_xxx / github_pat_xxx token or leave it empty.';
        btn.disabled = false; return;
      }
      params.github_token = gh;
    }
    const mo = document.getElementById('azModel').value.trim(); if (mo) params.copilot_model = mo;
    const to = document.getElementById('azTimeout').value.trim(); if (to) params.copilot_timeout = to;
    const data = await azFetch('POST', `/pipelines/${encodeURIComponent(document.getElementById('azPipeline').value.trim())}/createRun?api-version=2018-06-01`, params);
    el.className = 'status-banner show ok'; el.style = '';
    el.innerHTML = `✓ Pipeline triggered — Run ID: <code style="font-size:12px;color:var(--green);cursor:pointer" onclick="event.stopPropagation();window._app.copyText('${esc(data.runId)}','Run ID')">${esc(data.runId)}</code>
      <button class="btn-copy" style="margin-left:8px" onclick="event.stopPropagation();window._app.copyText('${esc(data.runId)}','Run ID')">📋</button>`;
    toast('Pipeline triggered!');
    document.getElementById('step-configure').classList.add('done');
    document.getElementById('conn-1').classList.add('done');
    setTimeout(() => { goStep('monitor'); refreshMonitor(); }, 1500);
  } catch (e) { el.className = 'status-banner show err'; el.style = ''; el.innerHTML = `✕ ${esc(e.message)}`; }
  finally { btn.disabled = false; }
}
