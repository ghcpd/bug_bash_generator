import { toast, esc, copyText, fmtDuration, fmtTime, statusBadge, goStep } from './utils.js';
import { azFetch, blobBaseUrl, blobHeaders, corsErrorHtml } from './azure.js';
import { openModal } from './modal.js';
import { RUN_TYPES } from './constants.js';
import { clearBatchLinks, setBatchLinkStatus, linkBatchByTaskIds, renderActiveBatchTasksFallback } from './batch.js';

let _autoRefreshTimer = null;
let _activityCache = {};
let _runsCache = { generate: [], audit: [], genRubric: [], autoRunBugbash: [] };

function getRunType(kind) { return RUN_TYPES[kind] || RUN_TYPES.generate; }
function getPipelineName(kind) { return document.getElementById(getRunType(kind).pipelineInputId).value.trim(); }

function syncMonitorLabels() {
  for (const [kind, cfg] of Object.entries(RUN_TYPES)) {
    const el = document.getElementById(`monitor_${kind}_PipelineName`);
    if (el) el.textContent = getPipelineName(kind) || '-';
  }
}

function getRunWorkload(run) {
  return run.parameters?.repo_list
    ? (() => { try { return JSON.parse(run.parameters.repo_list).length + ' cases'; } catch { return '? cases'; } })()
    : (run.parameters?.audit_level ? `level ${run.parameters.audit_level}` : (run.parameters?.input_folder ? 'audit run' : '-'));
}

function getRunDurationSeconds(run) {
  if (!run.runStart) return 0;
  return Math.max(0, Math.floor(((run.runEnd ? new Date(run.runEnd) : new Date()) - new Date(run.runStart)) / 1000));
}

export function getRunsCache() { return _runsCache; }

async function fetchRuns(kind = 'generate') {
  const pipelineName = getPipelineName(kind);
  if (!pipelineName) return [];
  const data = await azFetch('POST', '/queryPipelineRuns?api-version=2018-06-01', {
    lastUpdatedAfter: new Date(Date.now() - 7 * 86400000).toISOString(),
    lastUpdatedBefore: new Date(Date.now() + 86400000).toISOString(),
    filters: [{ operand: 'PipelineName', operator: 'Equals', values: [pipelineName] }],
    orderBy: [{ orderBy: 'RunStart', order: 'DESC' }],
  });
  return (data.value || []).map(run => ({ ...run, __kind: kind, __pipelineName: pipelineName }));
}

export function renderCombinedRuns() {
  syncMonitorLabels();
  const el = document.getElementById('runsContent');
  const detail = document.getElementById('activityDetailMonitor');
  const pipelineFilter = document.getElementById('monitorPipelineFilter')?.value || 'all';
  const statusFilter = document.getElementById('monitorStatusFilter')?.value || 'all';
  const sortMode = document.getElementById('monitorSort')?.value || 'start-desc';
  const query = (document.getElementById('monitorSearch')?.value || '').trim().toLowerCase();
  const combined = Object.values(_runsCache).flat();
  if (!combined.length) { el.innerHTML = '<p class="empty">No pipeline runs loaded yet</p>'; if (detail) detail.innerHTML = ''; return; }

  let rows = combined.filter(run => {
    if (pipelineFilter !== 'all' && run.__kind !== pipelineFilter) return false;
    if (statusFilter !== 'all' && (run.status || '') !== statusFilter) return false;
    if (query) {
      const haystack = `${run.runId || ''} ${run.__pipelineName || ''} ${run.__kind || ''}`.toLowerCase();
      if (!haystack.includes(query)) return false;
    }
    return true;
  });

  rows.sort((a, b) => {
    switch (sortMode) {
      case 'start-asc': return new Date(a.runStart || 0) - new Date(b.runStart || 0);
      case 'duration-desc': return getRunDurationSeconds(b) - getRunDurationSeconds(a);
      case 'duration-asc': return getRunDurationSeconds(a) - getRunDurationSeconds(b);
      case 'pipeline': return (a.__pipelineName || '').localeCompare(b.__pipelineName || '');
      case 'status': return (a.status || '').localeCompare(b.status || '');
      default: return new Date(b.runStart || 0) - new Date(a.runStart || 0);
    }
  });

  let h = `<div class="monitor-summary">Loaded ${combined.length} runs across configured pipelines. Showing ${rows.length} after filters.</div>`;
  h += `<table class="rtable"><tr><th>Pipeline</th><th>Run ID</th><th>Status</th><th>Started</th><th>Duration</th><th>Workload</th><th>Batch</th><th></th></tr>`;
  for (const run of rows.slice(0, 50)) {
    const kind = run.__kind;
    const cfg = getRunType(kind);
    const cancel = (run.status === 'InProgress' || run.status === 'Queued')
      ? `<button class="btn-del" style="color:var(--red)" onclick="event.stopPropagation();window._app.cancelRun('${run.runId}','${kind}')">Cancel</button>` : '';
    h += `<tr>
      <td><span class="badge ${cfg.badgeClass}">${esc(run.__pipelineName || cfg.label)}</span></td>
      <td><span style="font-family:monospace;color:var(--accent);font-size:11px;word-break:break-all" title="${esc(run.runId || '')}">${esc(run.runId || '')}</span>
        <button class="btn-copy" onclick="event.stopPropagation();window._app.copyText('${esc(run.runId || '')}','Run ID')" title="Copy Run ID">📋</button></td>
      <td>${statusBadge(run.status)}</td>
      <td style="font-size:11px">${fmtTime(run.runStart)}</td>
      <td style="font-size:11px">${fmtDuration(run.runStart, run.runEnd)}</td>
      <td style="font-size:11px;color:var(--muted)">${esc(getRunWorkload(run))}</td>
      <td><button class="btn-open-batch" onclick="window._app.openBatchForRun('${run.runId}','${kind}')" title="Open matching Batch tasks for this run">Open Batch</button></td>
      <td>${cancel}<button class="btn-copy" onclick="event.stopPropagation();window._app.copyText(JSON.stringify(${esc(JSON.stringify(run))},null,2),'Run JSON')" title="Copy run details">{ }</button></td>
    </tr>`;
  }
  h += '</table>';
  if (rows.length > 50) h += `<p style="font-size:11px;color:var(--muted);margin-top:6px;text-align:center">Showing 50 of ${rows.length}</p>`;
  el.innerHTML = h;
}

export async function openBatchForRun(runId, kind = 'generate') {
  const cfg = getRunType(kind);
  const shortId = (runId || '').substring(0, 8);
  const batchLink = document.getElementById('batchLinkStatus');
  if (batchLink) {
    setBatchLinkStatus(`<span class="spin"></span> Fetching Batch task IDs from ADF activities for ${esc(cfg.label)} ${esc(shortId)}…`, false);
    batchLink.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  try {
    clearBatchLinks();
    // Fetch activity runs to extract Batch task IDs
    const data = await azFetch('POST', `/pipelineruns/${encodeURIComponent(runId)}/queryActivityruns?api-version=2018-06-01`,
      { lastUpdatedAfter: '2024-01-01T00:00:00Z', lastUpdatedBefore: new Date(Date.now() + 86400000).toISOString() });
    const acts = data.value || [];
    const taskIds = new Set();
    for (const a of acts) {
      try { const tid = a.output?.executionDetails?.[0]?.taskId; if (tid) taskIds.add(tid); } catch {}
    }
    if (!taskIds.size) {
      // No task IDs found in activities — fall back to showing active tasks
      await renderActiveBatchTasksFallback(`${cfg.label} ${shortId}…`);
      setBatchLinkStatus(`No Batch task IDs found in ${acts.length} activities for ${esc(cfg.label)} ${esc(shortId)}… (run may still be queued). Showing active tasks instead.`, false);
      return;
    }
    setBatchLinkStatus(`<span class="spin"></span> Found ${taskIds.size} Batch task IDs, linking in Batch Explorer…`, false);
    const result = await linkBatchByTaskIds(taskIds, `${cfg.label} ${shortId}…`);
    setBatchLinkStatus(`Linked ${result.matchedTasks} task(s) across ${result.matchedJobs} job(s) for ${esc(cfg.label)} <code>${esc(shortId)}…</code>. Extracted from ${acts.length} ADF activities.`, false);
  } catch (e) {
    setBatchLinkStatus(`Failed to open Batch view for ${esc(cfg.label)} ${esc(shortId)}…: ${esc(e.message)}`, true);
  }
}

export async function loadActivities(runId, kind = 'generate') {
  const cfg = getRunType(kind);
  const el = document.getElementById('activityDetailMonitor');
  el.innerHTML = `<div class="act-panel"><div class="act-head"><span class="spin"></span> Loading activities…</div></div>`;
  try {
    const data = await azFetch('POST', `/pipelineruns/${encodeURIComponent(runId)}/queryActivityruns?api-version=2018-06-01`,
      { lastUpdatedAfter: '2024-01-01T00:00:00Z', lastUpdatedBefore: new Date(Date.now() + 86400000).toISOString() });
    const acts = data.value || [];
    _activityCache[`${kind}:${runId}`] = acts;
    if (!acts.length) { el.innerHTML = '<div class="act-panel"><div class="act-head">No activities</div></div>'; return; }

    let nS = 0, nF = 0, nR = 0, nQ = 0;
    for (const a of acts) {
      const s = (a.status || '').toLowerCase();
      if (s === 'succeeded') nS++; else if (s === 'failed') nF++; else if (s === 'inprogress') nR++; else nQ++;
    }
    const total = acts.length, pctDone = total ? Math.round((nS + nF) / total * 100) : 0;
    const barColor = nF > 0 ? `linear-gradient(90deg,var(--green) ${nS / total * 100}%,var(--red) ${nS / total * 100}% ${(nS + nF) / total * 100}%,var(--surface3) ${(nS + nF) / total * 100}%)`
      : `linear-gradient(90deg,var(--green) ${pctDone}%,var(--surface3) ${pctDone}%)`;

    let h = `<div class="act-panel">
      <div class="act-head">
        <span style="flex:1">${esc(cfg.label)} activities — <code style="color:var(--accent);cursor:pointer" onclick="window._app.copyText('${esc(runId)}','Run ID')">${esc(runId.substring(0, 8))}…</code>
          <button class="btn-copy" onclick="window._app.copyText('${esc(runId)}','Run ID')">📋</button></span>
        <div class="act-progress">
          ${nS ? `<span class="ap-s">✓ ${nS}</span>` : ''}
          ${nF ? `<span class="ap-f">✕ ${nF}</span>` : ''}
          ${nR ? `<span class="ap-r">◐ ${nR}</span>` : ''}
          ${nQ ? `<span class="ap-q">○ ${nQ}</span>` : ''}
          <span style="color:var(--text-sec)">${pctDone}%</span>
        </div>
      </div>
      <div style="padding:0 16px 0"><div class="progress-bar"><div class="progress-fill" style="width:${pctDone}%;background:${barColor}"></div></div></div>
      <table class="act-table"><tr><th>Name</th><th>Status</th><th>Duration</th><th>Batch Task</th><th>Error</th><th>Actions</th></tr>`;

    for (let i = 0; i < acts.length; i++) {
      const a = acts[i];
      const err = a.error?.message || '';
      let batchTaskId = '-';
      try { if (a.output?.executionDetails?.length) { batchTaskId = a.output.executionDetails[0].taskId || '-'; } } catch {}
      const exitCode = a.output?.exitcode;
      const exitBadge = exitCode !== undefined ? `<span style="font-size:10px;color:${exitCode === 0 ? 'var(--green)' : 'var(--red)'}"> (exit:${exitCode})</span>` : '';

      h += `<tr>
        <td style="font-size:11px">${esc(a.activityName || '-')}</td>
        <td>${statusBadge(a.status)}${exitBadge}</td>
        <td style="font-size:11px">${fmtDuration(a.activityRunStart, a.activityRunEnd)}</td>
        <td>${batchTaskId !== '-' ? `<span class="batch-tid">${esc(batchTaskId)}</span> <button class="btn-copy" onclick="event.stopPropagation();window._app.copyText('${esc(batchTaskId)}','Task ID')">📋</button>` : `<span style="color:var(--muted)">-</span>`}</td>
        <td>${err ? `<span class="err-text" title="${esc(err)}">${esc(err.substring(0, 120))}${err.length > 120 ? '…' : ''}</span> <button class="btn-copy" onclick="event.stopPropagation();window._app.copyText(\`${esc(err.replace(/`/g, "'"))}\`,'Error')">📋</button>` : '<span style="color:var(--muted)">-</span>'}</td>
        <td class="act-actions">
          <button class="btn-log json" onclick="event.stopPropagation();window._app.viewActivityOutput(${i},'${esc(runId)}','${kind}')" title="View full output JSON">{ }</button>
          ${err ? `<button class="btn-log stderr" onclick="event.stopPropagation();window._app.copyText(\`${esc(err.replace(/`/g, "'"))}\`,'Error')" title="Copy error">⚠</button>` : ''}
        </td></tr>`;
    }
    h += '</table></div>';
    el.innerHTML = h;
  } catch (e) { el.innerHTML = `<div class="act-panel"><div class="act-head" style="color:var(--red)">Error: ${esc(e.message)}</div></div>`; }
}

export function viewActivityOutput(idx, runId, kind = 'generate') {
  const acts = _activityCache[`${kind}:${runId}`];
  if (!acts || !acts[idx]) return;
  const a = acts[idx];
  const title = `Output — ${a.activityName || 'Activity ' + idx}`;
  const content = JSON.stringify(a.output || {}, null, 2);
  openModal(title, content);
}

export async function cancelRun(runId, kind = 'generate') {
  if (!confirm('Cancel this run?')) return;
  try { await azFetch('POST', `/pipelineruns/${encodeURIComponent(runId)}/cancel?api-version=2018-06-01`); toast('Cancel requested'); setTimeout(refreshMonitor, 1500); }
  catch (e) { alert('Failed: ' + e.message); }
}

export async function backfillRun(runId) {
  const allRuns = Object.values(_runsCache).flat();
  const run = allRuns.find(r => r.runId === runId);
  if (!run?.parameters?.repo_list) { alert('No repo_list found in run parameters'); return; }
  let repoList;
  try { repoList = JSON.parse(run.parameters.repo_list); } catch { alert('Cannot parse repo_list'); return; }
  if (!repoList.length) { alert('repo_list is empty'); return; }

  const el = document.getElementById('activityDetailMonitor');
  el.innerHTML = `<div class="act-panel"><div class="act-head"><span class="spin"></span> Checking blob storage for completed cases…</div></div>`;

  try {
    const folder = document.getElementById('azOutputFolder').value.trim();
    const prefix = folder + '/jsonl/';
    const url = blobBaseUrl() + `?restype=container&comp=list&prefix=${encodeURIComponent(prefix)}&delimiter=`;
    const resp = await fetch(url, { headers: blobHeaders() });
    if (!resp.ok) throw new Error('Blob listing failed: HTTP ' + resp.status);
    const xml = await resp.text();
    const parser = new DOMParser();
    const doc = parser.parseFromString(xml, 'text/xml');
    const blobs = doc.querySelectorAll('Blob');

    const existingKeys = new Set();
    blobs.forEach(b => {
      const name = b.querySelector('Name')?.textContent || '';
      const m = name.match(/([^/]+)-synthetic-\d+-(\d+)\.jsonl$/);
      if (m) existingKeys.add(m[1] + '::' + m[2]);
    });

    const missing = [];
    for (const item of repoList) {
      const repoUrl = item.repo || '';
      const m2 = repoUrl.match(/github\.com[/:]([^/]+)\/([^/.]+)/);
      if (!m2) continue;
      const slug = m2[1] + '__' + m2[2];
      const idx = item.case_index ?? 0;
      if (!existingKeys.has(slug + '::' + idx)) missing.push(item);
    }

    if (!missing.length) {
      el.innerHTML = `<div class="act-panel"><div class="act-head" style="color:var(--green)">✓ All ${repoList.length} cases already exist — no backfill needed</div></div>`;
      toast('All cases complete!');
      return;
    }

    const repoMap = {};
    for (const item of missing) {
      const key = item.repo; if (!repoMap[key]) repoMap[key] = [];
      repoMap[key].push(item.case_index ?? 0);
    }
    let h = `<div class="act-panel">
      <div class="act-head">
        <span style="flex:1">Backfill: ${missing.length} of ${repoList.length} cases missing</span>
        <button class="btn btn-purple" style="padding:5px 14px;font-size:12px" onclick="window._app.doBackfill()">🔄 Trigger Backfill (${missing.length} cases)</button>
      </div>
      <table class="act-table"><tr><th>Repository</th><th>Missing Indices</th><th>Category</th></tr>`;
    for (const [repo, indices] of Object.entries(repoMap)) {
      const cat = missing.find(m => m.repo === repo)?.category || '-';
      h += `<tr><td style="font-family:monospace;font-size:11px;color:var(--cyan)">${esc(repo.replace('https://github.com/', ''))}</td>
        <td style="font-size:11px">${indices.sort((a, b) => a - b).join(', ')}</td>
        <td style="font-size:11px;color:var(--muted)">${esc(cat)}</td></tr>`;
    }
    h += '</table></div>';
    el.innerHTML = h;

    window._backfillList = missing;
    window._backfillRunParams = run.parameters;
  } catch (e) {
    el.innerHTML = `<div class="act-panel"><div class="act-head" style="color:var(--red)">${corsErrorHtml(e.message)}</div></div>`;
  }
}

export async function doBackfill() {
  if (!window._backfillList?.length) { alert('No backfill data'); return; }
  const params = { repo_list: window._backfillList };
  const rp = window._backfillRunParams || {};
  if (rp.github_token) params.github_token = rp.github_token;
  if (rp.copilot_model) params.copilot_model = rp.copilot_model;
  if (rp.copilot_timeout) params.copilot_timeout = rp.copilot_timeout;
  try {
    const data = await azFetch('POST', `/pipelines/${encodeURIComponent(document.getElementById('azPipeline').value.trim())}/createRun?api-version=2018-06-01`, params);
    toast(`Backfill triggered! Run ID: ${data.runId.substring(0, 8)}…`);
    setTimeout(refreshMonitor, 1500);
  } catch (e) { alert('Backfill trigger failed: ' + e.message); }
}

export function toggleAutoRefresh() {
  if (document.getElementById('autoRefresh').checked) {
    refreshMonitor();
    _autoRefreshTimer = setInterval(refreshMonitor, 15000);
  } else {
    if (_autoRefreshTimer) { clearInterval(_autoRefreshTimer); _autoRefreshTimer = null; }
  }
}

export async function refreshMonitor() {
  syncMonitorLabels();
  const el = document.getElementById('runsContent');
  el.innerHTML = '<p class="empty"><span class="spin"></span> Loading…</p>';
  try {
    const kinds = Object.keys(RUN_TYPES);
    const results = await Promise.all(kinds.map(k => fetchRuns(k)));
    kinds.forEach((k, i) => { _runsCache[k] = results[i]; });
    renderCombinedRuns();
  } catch (e) { el.innerHTML = `<p class="empty" style="color:var(--red)">Error: ${esc(e.message)}</p>`; }
}
