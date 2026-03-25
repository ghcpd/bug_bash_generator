import { toast, esc, copyText, fmtDuration, fmtTime, statusBadge } from './utils.js';
import { batchFetch } from './azure.js';
import { openModal } from './modal.js';

let _batchJobsCache = [];
let _batchTasksCache = {};
let _taskSortMode = 'id-asc';
let _taskUuidFilter = '';
let _activeBatchJobId = '';
let _batchSearchQuery = '';
let _batchSearchMatchedJobIds = null;
let _linkedBatchTaskIds = new Set();
let _linkedBatchJobIds = new Set();
let _linkedBatchMatchMap = {};
let _linkedBatchSource = '';

export function setBatchLinkStatus(message, isError) {
  const el = document.getElementById('batchLinkStatus');
  if (!el) return;
  el.innerHTML = message;
  el.style.color = isError ? 'var(--red)' : 'var(--muted)';
  el.style.borderColor = isError ? 'rgba(209,52,56,.3)' : 'var(--border)';
  el.style.background = isError ? 'var(--red-dim)' : 'var(--surface2)';
}

export function clearBatchLinks() {
  _linkedBatchTaskIds = new Set();
  _linkedBatchJobIds = new Set();
  _linkedBatchMatchMap = {};
  _linkedBatchSource = '';
  setBatchLinkStatus('Use Open Batch on any pipeline run to list all currently running Batch tasks in the selected pool.', false);
}

async function fetchBatchTasks(jobId) {
  if (_batchTasksCache[jobId]) return _batchTasksCache[jobId];
  let allTasks = [];
  let url = `/jobs/${encodeURIComponent(jobId)}/tasks`;
  while (url) {
    const resp = await batchFetch(url);
    const data = await resp.json();
    allTasks = allTasks.concat(data.value || []);
    url = data['odata.nextLink'] || null;
  }
  _batchTasksCache[jobId] = allTasks;
  return allTasks;
}

async function fetchBatchJobsForPool(poolFilter) {
  let allJobs = [];
  let url = '/jobs?maxresults=200';
  while (url) {
    const resp = await batchFetch(url);
    const data = await resp.json();
    allJobs = allJobs.concat(data.value || []);
    url = data['odata.nextLink'] || null;
  }
  if (poolFilter) {
    allJobs = allJobs.filter(j => {
      const pool = (j.poolInfo?.poolId || '').toLowerCase();
      const execPool = (j.executionInfo?.poolId || '').toLowerCase();
      const pf = poolFilter.toLowerCase();
      return pool === pf || execPool === pf;
    });
  }
  return allJobs;
}

export async function listBatchJobs(options = {}) {
  const { silent = false } = options;
  const el = document.getElementById('batchJobsContent');
  if (!silent) {
    el.innerHTML = '<p class="empty"><span class="spin"></span> Loading Batch jobs…</p>';
    document.getElementById('batchTasksDetail').innerHTML = '';
  }
  try {
    const poolFilter = document.getElementById('batchPoolFilter').value.trim();
    const jobs = await fetchBatchJobsForPool(poolFilter);
    _batchJobsCache = jobs;
    _batchSearchQuery = '';
    _batchSearchMatchedJobIds = null;
    const searchInput = document.getElementById('batchSearch');
    if (searchInput) searchInput.value = '';
    if (!silent) {
      if (!jobs.length) { el.innerHTML = '<p class="empty">No Batch jobs found</p>'; return jobs; }
      renderBatchJobs();
    }
    return jobs;
  } catch (e) {
    if (!silent) el.innerHTML = `<p class="empty" style="color:var(--red)">Error: ${esc(e.message)}</p>`;
    throw e;
  }
}

export async function searchBatchExplorer() {
  const input = document.getElementById('batchSearch');
  const query = (input?.value || '').trim().toLowerCase();
  _batchSearchQuery = query;

  if (!query) {
    _batchSearchMatchedJobIds = null;
    renderBatchJobs();
    if (_activeBatchJobId && _batchTasksCache[_activeBatchJobId]) renderBatchTasks(_activeBatchJobId, 'all', _taskSortMode, _taskUuidFilter);
    return;
  }

  _linkedBatchJobIds = new Set();
  _linkedBatchTaskIds = new Set();
  _linkedBatchMatchMap = {};
  _linkedBatchSource = '';
  setBatchLinkStatus(`<span class="spin"></span> Searching Batch jobs and task IDs for <code>${esc(query)}</code>...`, false);
  try {
    const poolFilter = document.getElementById('batchPoolFilter').value.trim();
    const cachedJobs = _batchJobsCache.slice();
    const fetchedJobs = await fetchBatchJobsForPool(poolFilter);
    const jobsById = new Map();
    for (const job of [...cachedJobs, ...fetchedJobs]) {
      if (job?.id) jobsById.set(job.id, job);
    }
    for (const cachedJobId of Object.keys(_batchTasksCache)) {
      if (!jobsById.has(cachedJobId)) {
        jobsById.set(cachedJobId, { id: cachedJobId, state: 'cached', creationTime: '', poolInfo: { poolId: poolFilter || '-' } });
      }
    }
    const jobs = [...jobsById.values()];
    _batchJobsCache = jobs;
    const matchedJobIds = new Set();
    let matchedTaskJobId = '';
    let scannedTaskCount = 0;

    for (const job of jobs) {
      const rawJobId = job.id || '';
      const jobId = rawJobId.toLowerCase();
      const poolId = (job.poolInfo?.poolId || '').toLowerCase();
      if (jobId.includes(query) || poolId.includes(query)) matchedJobIds.add(rawJobId);

      const tasks = await fetchBatchTasks(rawJobId);
      scannedTaskCount += tasks.length;
      if (tasks.some(task => (task.id || '').toLowerCase().includes(query))) {
        matchedJobIds.add(rawJobId);
        if (!matchedTaskJobId) matchedTaskJobId = rawJobId;
      }
    }

    _batchSearchMatchedJobIds = matchedJobIds;
    renderBatchJobs();

    if (!matchedJobIds.size) {
      document.getElementById('batchTasksDetail').innerHTML = '';
      setBatchLinkStatus(`No Batch jobs or task IDs matched <code>${esc(query)}</code> in pool <code>${esc(poolFilter || '-')}</code>. Searched ${jobs.length} jobs and ${scannedTaskCount} tasks.`, false);
      return;
    }

    const focusJobId = matchedTaskJobId || [...matchedJobIds][0];
    await loadBatchTasks(focusJobId);
    renderBatchTasks(focusJobId, 'all', 'id-asc', query);
    setBatchLinkStatus(`Matched ${matchedJobIds.size} Batch job(s) for <code>${esc(query)}</code> in pool <code>${esc(poolFilter || '-')}</code>. Searched ${jobs.length} jobs and ${scannedTaskCount} tasks. Opened ${esc(focusJobId)} and filtered tasks below.`, false);
  } catch (e) {
    setBatchLinkStatus(`Batch search failed for <code>${esc(query)}</code>: ${esc(e.message)}`, true);
  }
}

export async function linkBatchByTaskIds(taskIds, sourceLabel) {
  if (!taskIds.size) return;
  const jobs = _batchJobsCache.length ? _batchJobsCache : await listBatchJobs({ silent: true });
  _linkedBatchJobIds = new Set();
  _linkedBatchMatchMap = {};
  _linkedBatchTaskIds = new Set(taskIds);
  _linkedBatchSource = sourceLabel;
  let focusJobId = '';
  for (const job of jobs) {
    const jobId = job.id || '';
    const tasks = await fetchBatchTasks(jobId);
    for (const task of tasks) {
      if (taskIds.has(task.id || '')) {
        _linkedBatchJobIds.add(jobId);
        if (!_linkedBatchMatchMap[jobId]) _linkedBatchMatchMap[jobId] = new Set();
        _linkedBatchMatchMap[jobId].add(task.id);
        if (!focusJobId) focusJobId = jobId;
      }
    }
  }
  renderBatchJobs();
  if (focusJobId) {
    await loadBatchTasks(focusJobId);
    renderBatchTasks(focusJobId, 'all', 'id-asc', '');
  }
  return { matchedJobs: _linkedBatchJobIds.size, matchedTasks: _linkedBatchTaskIds.size, focusJobId };
}

export async function renderActiveBatchTasksFallback(sourceLabel) {
  const jobs = _batchJobsCache.length ? _batchJobsCache : await listBatchJobs({ silent: true });
  const candidates = [];
  for (const job of jobs) {
    const jobId = job.id || '';
    const tasks = await fetchBatchTasks(jobId);
    for (const task of tasks) {
      const state = (task.state || '').toLowerCase();
      if (!['running', 'active', 'preparing'].includes(state)) continue;
      candidates.push({ jobId, task });
    }
  }

  const jobsWithCandidates = [...new Set(candidates.map(item => item.jobId))];
  _linkedBatchJobIds = new Set(jobsWithCandidates);
  _linkedBatchMatchMap = {};
  _linkedBatchTaskIds = new Set();
  for (const { jobId, task } of candidates) {
    if (!_linkedBatchMatchMap[jobId]) _linkedBatchMatchMap[jobId] = new Set();
    _linkedBatchMatchMap[jobId].add(task.id || '');
    _linkedBatchTaskIds.add(task.id || '');
  }
  renderBatchJobs();

  const el = document.getElementById('batchTasksDetail');
  if (!candidates.length) {
    el.innerHTML = `<div class="batch-tasks-panel"><div class="batch-tasks-head">No active Batch tasks found in the selected pool</div></div>`;
    return;
  }

  let h = `<div class="batch-tasks-panel">
    <div class="batch-tasks-head">
      <span style="flex:1">Active Batch Tasks Fallback</span>
      <span style="font-size:11px;color:var(--muted)">${candidates.length} active task(s) from ${esc(sourceLabel)}</span>
    </div>`;
  for (const jobId of jobsWithCandidates) {
    const jobTasks = candidates.filter(item => item.jobId === jobId).map(item => item.task);
    h += `<div style="padding:10px 12px;border-top:1px solid var(--border)">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <span style="font-size:11px;color:var(--muted)">Job</span>
        <span style="font-family:monospace;font-size:11px;color:var(--cyan)">${esc(jobId)}</span>
        <button class="btn-copy" onclick="window._app.copyText('${esc(jobId)}','Job ID')">📋</button>
        <button class="btn-sm" onclick="window._app.loadBatchTasks('${esc(jobId)}')">Open Full Job</button>
      </div>
      <table class="act-table"><tr><th>Task ID</th><th>State</th><th>Started</th><th>Duration</th><th>Logs</th></tr>`;
    for (const task of jobTasks) {
      const tid = task.id || '';
      const ei = task.executionInfo || {};
      h += `<tr style="background:rgba(0,120,212,.08)">
        <td><span style="font-family:monospace;font-size:11px;color:var(--cyan)">${esc(tid)}</span>
          <button class="btn-copy" onclick="window._app.copyText('${esc(tid)}','Task ID')">📋</button></td>
        <td>${statusBadge(task.state)}</td>
        <td style="font-size:11px">${fmtTime(ei.startTime || task.creationTime)}</td>
        <td style="font-size:11px">${fmtDuration(ei.startTime || task.creationTime, ei.endTime)}</td>
        <td class="act-actions">
          <button class="btn-log stdout" onclick="window._app.viewBatchFile('${esc(jobId)}','${esc(tid)}','stdout.txt')">stdout</button>
          <button class="btn-log stderr" onclick="window._app.viewBatchFile('${esc(jobId)}','${esc(tid)}','stderr.txt')">stderr</button>
        </td></tr>`;
    }
    h += '</table></div>';
  }
  h += '</div>';
  el.innerHTML = h;
}

function renderBatchJobs() {
  const el = document.getElementById('batchJobsContent');
  if (!el) return;
  const jobs = _batchJobsCache;
  if (!jobs || !jobs.length) { el.innerHTML = '<p class="empty">No Batch jobs found</p>'; return; }

  let filtered = jobs.slice();
  if (_linkedBatchJobIds.size && !_batchSearchQuery) filtered = filtered.filter(job => _linkedBatchJobIds.has(job.id || ''));
  if (_batchSearchMatchedJobIds && _batchSearchQuery) filtered = filtered.filter(job => _batchSearchMatchedJobIds.has(job.id || ''));
  filtered.sort((a, b) => new Date(b.creationTime || 0) - new Date(a.creationTime || 0));

  let summary = (_linkedBatchJobIds.size && !_batchSearchQuery) ? `Showing Batch jobs linked to ${esc(_linkedBatchSource || 'the selected run')}.` : 'Showing recent Batch jobs for the selected pool.';
  if (_batchSearchQuery) summary += ` Search: <code>${esc(_batchSearchQuery)}</code>.`;
  let h = `<div class="monitor-summary">${summary}</div>`;
  h += `<table class="batch-jobs-table"><tr><th>Job ID</th><th>State</th><th>Created</th><th>Pool</th><th></th></tr>`;
  if (!filtered.length) {
    h += `<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:16px">No jobs found</td></tr>`;
  }
  for (const j of filtered) {
    const jid = j.id || '';
    const shortId = jid.length > 32 ? jid.substring(0, 32) + '…' : jid;
    const linkedCount = (_linkedBatchMatchMap[jid] && _linkedBatchMatchMap[jid].size) || 0;
    const isLinked = _linkedBatchJobIds.has(jid);
    const rowStyle = isLinked ? 'background:rgba(0,120,212,.08);outline:1px solid rgba(0,120,212,.25)' : '';
    h += `<tr class="clickable" onclick="window._app.loadBatchTasks('${esc(jid)}')">
      <td style="${rowStyle}"><span style="font-family:monospace;font-size:11px;color:var(--cyan)" title="${esc(jid)}">${esc(shortId)}</span>
        ${linkedCount ? ` <span style="font-size:10px;color:var(--accent);font-weight:700">+${linkedCount} linked</span>` : ''}
        <button class="btn-copy" onclick="event.stopPropagation();window._app.copyText('${esc(jid)}','Job ID')">📋</button></td>
      <td style="${rowStyle}">${statusBadge(j.state || 'unknown')}</td>
      <td style="${rowStyle};font-size:11px">${fmtTime(j.creationTime)}</td>
      <td style="${rowStyle};font-size:11px;color:var(--muted)">${esc(j.poolInfo?.poolId || '-')}</td>
      <td style="${rowStyle}"><button class="btn-sm" onclick="event.stopPropagation();window._app.loadBatchTasks('${esc(jid)}')">Tasks →</button></td>
    </tr>`;
  }
  h += '</table>';
  el.innerHTML = h;
}

function classifyTask(t) {
  const s = (t.state || '').toLowerCase();
  if (s === 'running') return 'running';
  if (s === 'completed') { return (t.executionInfo?.exitCode === 0) ? 'succeeded' : 'failed'; }
  if (s === 'active' || s === 'preparing') return 'pending';
  return 'other';
}

export function renderBatchTasks(jobId, filter, sortMode, uuidFilter) {
  if (sortMode !== undefined) _taskSortMode = sortMode;
  if (uuidFilter !== undefined) _taskUuidFilter = uuidFilter;
  const el = document.getElementById('batchTasksDetail');
  const tasks = _batchTasksCache[jobId];
  if (!tasks) return;

  let nS = 0, nF = 0, nR = 0, nP = 0;
  for (const t of tasks) { const c = classifyTask(t); if (c === 'succeeded') nS++; else if (c === 'failed') nF++; else if (c === 'running') nR++; else nP++; }
  const pct = tasks.length ? Math.round((nS + nF) / tasks.length * 100) : 0;

  let filtered = filter === 'all' ? tasks.slice() : tasks.filter(t => classifyTask(t) === filter);

  const uf = (_taskUuidFilter || '').trim().toLowerCase();
  if (uf) filtered = filtered.filter(t => (t.id || '').toLowerCase().includes(uf));

  filtered.sort((a, b) => {
    switch (_taskSortMode) {
      case 'id-desc': return (b.id || '').localeCompare(a.id || '', undefined, { numeric: true });
      case 'time-desc': { const at = a.executionInfo?.startTime || a.creationTime || ''; const bt = b.executionInfo?.startTime || b.creationTime || ''; return new Date(bt || 0) - new Date(at || 0); }
      case 'time-asc': { const at2 = a.executionInfo?.startTime || a.creationTime || ''; const bt2 = b.executionInfo?.startTime || b.creationTime || ''; return new Date(at2 || 0) - new Date(bt2 || 0); }
      case 'state': return classifyTask(a).localeCompare(classifyTask(b));
      case 'exit': { const ea = a.executionInfo?.exitCode ?? 999; const eb = b.executionInfo?.exitCode ?? 999; return ea - eb; }
      default: return (a.id || '').localeCompare(b.id || '', undefined, { numeric: true });
    }
  });

  const btnStyle = (f) => `cursor:pointer;padding:3px 10px;border-radius:10px;font-size:11px;border:1px solid var(--border);background:${filter === f ? 'var(--accent)' : 'var(--surface2)'};color:${filter === f ? '#fff' : 'var(--text-sec)'}`;
  let h = `<div class="batch-tasks-panel">
    <div class="batch-tasks-head">
      <span style="flex:1">Tasks — <code style="color:var(--cyan);cursor:pointer" onclick="window._app.copyText('${esc(jobId)}','Job ID')">${esc(jobId.length > 30 ? jobId.substring(0, 30) + '…' : jobId)}</code>
        <button class="btn-copy" onclick="window._app.copyText('${esc(jobId)}','Job ID')">📋</button></span>
      <span style="font-size:11px;color:var(--muted)">${tasks.length} tasks &middot; ${pct}% done</span>
    </div>
    <div style="display:flex;gap:6px;padding:8px 12px;flex-wrap:wrap;align-items:center">
      <span style="${btnStyle('all')}" onclick="window._app.renderBatchTasks('${esc(jobId)}','all','${esc(_taskSortMode)}','${esc(_taskUuidFilter)}')">All (${tasks.length})</span>
      <span style="${btnStyle('failed')}" onclick="window._app.renderBatchTasks('${esc(jobId)}','failed','${esc(_taskSortMode)}','${esc(_taskUuidFilter)}')">❌ Failed (${nF})</span>
      <span style="${btnStyle('running')}" onclick="window._app.renderBatchTasks('${esc(jobId)}','running','${esc(_taskSortMode)}','${esc(_taskUuidFilter)}')">🔄 Running (${nR})</span>
      <span style="${btnStyle('succeeded')}" onclick="window._app.renderBatchTasks('${esc(jobId)}','succeeded','${esc(_taskSortMode)}','${esc(_taskUuidFilter)}')">✅ Succeeded (${nS})</span>
      <span style="${btnStyle('pending')}" onclick="window._app.renderBatchTasks('${esc(jobId)}','pending','${esc(_taskSortMode)}','${esc(_taskUuidFilter)}')">⏳ Pending (${nP})</span>
      <span style="flex:1"></span>
      <label style="font-size:11px;color:var(--muted)">Sort:</label>
      <select id="taskSort" style="font-size:11px;padding:3px 6px" onchange="window._app.renderBatchTasks('${esc(jobId)}','${esc(filter)}',this.value,document.getElementById('taskUuidFilter').value)">
        <option value="id-asc" ${_taskSortMode === 'id-asc' ? 'selected' : ''}>ID ↑ (A→Z)</option>
        <option value="id-desc" ${_taskSortMode === 'id-desc' ? 'selected' : ''}>ID ↓ (Z→A)</option>
        <option value="time-desc" ${_taskSortMode === 'time-desc' ? 'selected' : ''}>Time ↓ (newest)</option>
        <option value="time-asc" ${_taskSortMode === 'time-asc' ? 'selected' : ''}>Time ↑ (oldest)</option>
        <option value="state" ${_taskSortMode === 'state' ? 'selected' : ''}>State</option>
        <option value="exit" ${_taskSortMode === 'exit' ? 'selected' : ''}>Exit Code</option>
      </select>
      <input id="taskUuidFilter" placeholder="Filter Task ID…" value="${esc(_taskUuidFilter)}" style="width:180px;font-size:11px;padding:4px 8px" oninput="window._app.renderBatchTasks('${esc(jobId)}','${esc(filter)}',document.getElementById('taskSort').value,this.value)">
      <span style="font-size:11px;color:var(--muted)">${filtered.length}/${tasks.length}</span>
    </div>
    <table class="act-table"><tr><th>Task ID</th><th>State</th><th>Exit</th><th>Duration</th><th>Logs</th></tr>`;

  if (!filtered.length) {
    h += `<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:16px">No tasks match filter</td></tr>`;
  }
  for (const t of filtered) {
    const tid = t.id || '';
    const ei = t.executionInfo || {};
    const exitCode = ei.exitCode;
    const exitColor = exitCode === 0 ? 'var(--green)' : exitCode !== undefined ? 'var(--red)' : 'var(--muted)';
    const isLinked = !!_linkedBatchMatchMap[jobId]?.has(tid);
    const rowStyle = isLinked ? 'background:rgba(0,120,212,.08);outline:1px solid rgba(0,120,212,.25)' : '';

    h += `<tr>
      <td style="${rowStyle}"><span style="font-family:monospace;font-size:11px;color:var(--cyan)">${esc(tid)}</span>${isLinked ? ` <span style="font-size:10px;color:var(--accent);font-weight:700">linked</span>` : ''}
        <button class="btn-copy" onclick="window._app.copyText('${esc(tid)}','Task ID')">📋</button></td>
      <td style="${rowStyle}">${statusBadge(t.state)}</td>
      <td style="${rowStyle};font-family:monospace;font-size:11px;color:${exitColor}">${exitCode !== undefined ? exitCode : '-'}</td>
      <td style="${rowStyle};font-size:11px">${fmtDuration(ei.startTime, ei.endTime)}</td>
      <td class="act-actions" style="${rowStyle}">
        <button class="btn-log stdout" onclick="window._app.viewBatchFile('${esc(jobId)}','${esc(tid)}','stdout.txt')">stdout</button>
        <button class="btn-log stderr" onclick="window._app.viewBatchFile('${esc(jobId)}','${esc(tid)}','stderr.txt')">stderr</button>
      </td></tr>`;
  }
  h += '</table></div>';
  el.innerHTML = h;
}

export async function loadBatchTasks(jobId) {
  const el = document.getElementById('batchTasksDetail');
  el.innerHTML = `<div class="batch-tasks-panel"><div class="batch-tasks-head"><span class="spin"></span> Loading tasks for ${esc(jobId.substring(0, 24))}…</div></div>`;
  try {
    const tasks = await fetchBatchTasks(jobId);
    if (!tasks.length) { el.innerHTML = `<div class="batch-tasks-panel"><div class="batch-tasks-head">No tasks in job</div></div>`; return; }

    _taskSortMode = 'id-asc';
    _taskUuidFilter = '';
    _batchTasksCache[jobId] = tasks;

    const hasFailures = tasks.some(t => classifyTask(t) === 'failed');
    renderBatchTasks(jobId, hasFailures ? 'failed' : 'all', _taskSortMode, _taskUuidFilter);
  } catch (e) { el.innerHTML = `<div class="batch-tasks-panel"><div class="batch-tasks-head" style="color:var(--red)">Error: ${esc(e.message)}</div></div>`; }
}

let _modalSource = null;

export async function viewBatchFile(jobId, taskId, filename) {
  _modalSource = { jobId, taskId, filename };
  openModal(`${filename} — ${taskId}`, 'Loading…', true);
  try {
    const resp = await batchFetch(`/jobs/${encodeURIComponent(jobId)}/tasks/${encodeURIComponent(taskId)}/files/${encodeURIComponent(filename)}`);
    const text = await resp.text();
    document.getElementById('logModalContent').textContent = text || '(empty)';
  } catch (e) { document.getElementById('logModalContent').textContent = 'Error: ' + e.message; }
}

export async function refreshModal() {
  if (!_modalSource) return;
  const { jobId, taskId, filename } = _modalSource;
  document.getElementById('logModalContent').textContent = 'Refreshing…';
  try {
    const resp = await batchFetch(`/jobs/${encodeURIComponent(jobId)}/tasks/${encodeURIComponent(taskId)}/files/${encodeURIComponent(filename)}`);
    const text = await resp.text();
    document.getElementById('logModalContent').textContent = text || '(empty)';
    toast('Refreshed');
  } catch (e) { document.getElementById('logModalContent').textContent = 'Error: ' + e.message; }
}
