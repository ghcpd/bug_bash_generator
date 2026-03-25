import { toast, esc, copyText, fmtBytes, fmtTime, fmtDuration, statusBadge, goStep } from './utils.js';
import { azFetch, blobBaseUrl, blobHeaders, corsErrorHtml } from './azure.js';
import { openModal } from './modal.js';
import { refreshMonitor } from './monitor.js';

let _bbCases = [];
let _bbGenRubricRunId = null;
let _bbRubricPaths = {};
let _bbPollTimer = null;

export async function loadBugBashCases() {
  const el = document.getElementById('bbCasesContent');
  el.innerHTML = '<p class="empty"><span class="spin"></span> Loading JSONL and tar.gz files…</p>';
  try {
    const folder = document.getElementById('azOutputFolder').value.trim();
    const jsonlPrefix = folder + '/jsonl/';
    const jsonlUrl = blobBaseUrl() + `?restype=container&comp=list&prefix=${encodeURIComponent(jsonlPrefix)}&delimiter=`;
    const jsonlResp = await fetch(jsonlUrl, { headers: blobHeaders() });
    if (!jsonlResp.ok) throw new Error('JSONL listing failed: HTTP ' + jsonlResp.status);
    const jsonlXml = await jsonlResp.text();
    const parser = new DOMParser();
    const jsonlDoc = parser.parseFromString(jsonlXml, 'text/xml');
    const jsonlBlobs = jsonlDoc.querySelectorAll('Blob');

    const tarPrefix = folder + '/tar.gz/';
    const tarUrl = blobBaseUrl() + `?restype=container&comp=list&prefix=${encodeURIComponent(tarPrefix)}&delimiter=`;
    const tarResp = await fetch(tarUrl, { headers: blobHeaders() });
    let tarNames = new Set();
    let tarMap = {};
    if (tarResp.ok) {
      const tarXml = await tarResp.text();
      const tarDoc = parser.parseFromString(tarXml, 'text/xml');
      tarDoc.querySelectorAll('Blob').forEach(b => {
        const name = b.querySelector('Name')?.textContent || '';
        const baseName = name.split('/').pop().replace('.tar.gz', '');
        tarNames.add(baseName);
        tarMap[baseName] = name;
      });
    }

    if (!jsonlBlobs.length) { el.innerHTML = '<p class="empty">No JSONL files found in ' + esc(jsonlPrefix) + '</p>'; return; }

    _bbCases = [];
    jsonlBlobs.forEach(b => {
      const name = b.querySelector('Name')?.textContent || '';
      if (!name.endsWith('.jsonl')) return;
      const shortName = name.replace(jsonlPrefix, '');
      const baseName = shortName.replace('.jsonl', '');
      const tarGzPath = tarMap[baseName] || '';
      _bbCases.push({ name, shortName, baseName, tarGzPath, selected: true, data: null, loaded: false });
    });
    _bbCases.sort((a, b) => a.shortName.localeCompare(b.shortName));

    el.innerHTML = `<p class="empty"><span class="spin"></span> Downloading ${_bbCases.length} JSONL files…</p>`;
    let loaded = 0;
    const batchSize = 5;
    for (let i = 0; i < _bbCases.length; i += batchSize) {
      const batch = _bbCases.slice(i, i + batchSize);
      await Promise.all(batch.map(async c => {
        try {
          const url = blobBaseUrl() + '/' + encodeURIComponent(c.name).replace(/%2F/g, '/');
          const resp = await fetch(url, { headers: blobHeaders() });
          if (!resp.ok) throw new Error('HTTP ' + resp.status);
          const text = await resp.text();
          c.data = JSON.parse(text.trim().split('\n')[0]);
          c.loaded = true;
          loaded++;
        } catch (e) { c.data = { _error: e.message }; c.loaded = true; }
      }));
    }

    renderBugBashCases();
    toast(`Loaded ${loaded} cases, ${_bbCases.filter(c => c.tarGzPath).length} have tar.gz`);
  } catch (e) { el.innerHTML = `<p class="empty" style="color:var(--red)">${corsErrorHtml(e.message)}</p>`; }
}

export function renderBugBashCases() {
  const el = document.getElementById('bbCasesContent');
  const nSel = _bbCases.filter(c => c.selected).length;
  const nTar = _bbCases.filter(c => c.tarGzPath).length;
  let h = `<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap">
    <span style="font-size:12px;color:var(--muted)">${_bbCases.length} cases, ${nSel} selected, ${nTar} with tar.gz</span>
    <button class="btn-sm" onclick="window._app.bbSelectAll()">Select All</button>
    <button class="btn-sm" onclick="window._app.bbSelectNone()">Select None</button>
    <button class="btn-sm" onclick="window._app.bbSelectWithTar()">Select with tar.gz</button>
  </div>`;
  h += `<div style="max-height:450px;overflow-y:auto;border:1px solid var(--border);border-radius:var(--r)">`;
  h += `<table style="width:100%;border-collapse:collapse;font-size:12px">`;
  h += `<tr style="position:sticky;top:0;background:var(--surface2);z-index:1">
    <th style="padding:6px 8px;text-align:left;width:30px"></th>
    <th style="padding:6px 8px;text-align:left">Instance ID</th>
    <th style="padding:6px 8px;text-align:left">Prompt (issue_text) Preview</th>
    <th style="padding:6px 8px;text-align:left;width:80px">tar.gz</th>
    <th style="padding:6px 8px;text-align:center;width:60px">Actions</th>
  </tr>`;
  _bbCases.forEach((c, i) => {
    const iid = c.data?.instance_id || c.baseName || '';
    const prompt = (c.data?.issue_text || '').substring(0, 120);
    const hasErr = c.data?._error;
    const hasTar = !!c.tarGzPath;
    h += `<tr style="border-bottom:1px solid var(--border);cursor:pointer${c.selected ? '' : ';opacity:0.6'}" onclick="window._app.toggleBBCase(${i})">
      <td style="padding:4px 8px"><input type="checkbox" ${c.selected ? 'checked' : ''} onclick="event.stopPropagation();window._app.toggleBBCase(${i})"></td>
      <td style="padding:4px 8px;font-family:monospace;font-size:11px;color:var(--cyan)" title="${esc(iid)}">${esc(iid.length > 40 ? iid.substring(0, 40) + '…' : iid)}</td>
      <td style="padding:4px 8px;color:${hasErr ? 'var(--red)' : 'var(--text-sec)'};font-size:11px">${hasErr ? 'Error: ' + esc(c.data._error) : esc(prompt) + (prompt.length >= 120 ? '…' : '')}</td>
      <td style="padding:4px 8px;text-align:center">${hasTar ? '<span style="color:var(--green)">✓</span>' : '<span style="color:var(--red)">✕</span>'}</td>
      <td style="padding:4px 8px;text-align:center">
        <button class="btn-sm" style="font-size:10px" onclick="event.stopPropagation();window._app.viewBBCase(${i})" title="View full data">{ }</button>
      </td>
    </tr>`;
  });
  h += `</table></div>`;
  el.innerHTML = h;
  document.getElementById('btnGenRubric').disabled = nSel === 0;
}

export function bbSelectAll() { _bbCases.forEach(c => c.selected = true); renderBugBashCases(); }
export function bbSelectNone() { _bbCases.forEach(c => c.selected = false); renderBugBashCases(); }
export function bbSelectWithTar() { _bbCases.forEach(c => { c.selected = !!c.tarGzPath; }); renderBugBashCases(); }

export function toggleBBCase(idx) {
  _bbCases[idx].selected = !_bbCases[idx].selected;
  renderBugBashCases();
}

export function viewBBCase(idx) {
  const c = _bbCases[idx];
  if (!c || !c.data) return;
  openModal(c.data.instance_id || c.shortName, JSON.stringify(c.data, null, 2));
}

let _pendingGenRubricParams = null;

export function triggerBugBashGenRubric() {
  const selected = _bbCases.filter(c => c.selected && c.data && !c.data._error);
  if (!selected.length) { alert('Select at least one valid case'); return; }

  const pipelineName = document.getElementById('bbGenRubricPipeline').value.trim();
  if (!pipelineName) { alert('Set Gen Rubric pipeline name'); return; }

  const storageAccount = document.getElementById('azStorage').value.trim();
  const container = document.getElementById('azContainer').value.trim();
  const outputFolder = document.getElementById('bbRubricOutputFolder').value.trim();
  const promptsFolder = document.getElementById('bbPromptsFolder').value.trim();
  const inputFolder = document.getElementById('azOutputFolder').value.trim();
  const ghToken = document.getElementById('azGhToken').value.trim();
  if (ghToken && (ghToken.startsWith('{') || ghToken.startsWith('['))) {
    alert('GitHub Token looks like JSON (possibly pasted from token import). Please paste a raw ghp_xxx / github_pat_xxx token or leave it empty.');
    return;
  }

  // Build repos array: tar.gz base names of selected cases
  const repos = selected
    .filter(c => c.tarGzPath)
    .map(c => c.tarGzPath.split('/').pop().replace('.tar.gz', ''));

  // Build task_prompt: path to the prompts folder containing {instanceId}.md files
  const taskPromptPath = promptsFolder || '';

  // Pre-fill parameter values
  const params = {
    input:           { type: 'string', value: inputFolder + '/tar.gz' },
    output:          { type: 'string', value: outputFolder },
    rubric_prompt:   { type: 'string', value: outputFolder },
    storage_account: { type: 'string', value: storageAccount },
    container:       { type: 'string', value: container },
    github_token:    { type: 'string', value: ghToken },
    repos:           { type: 'array',  value: repos },
    task_prompt:     { type: 'string', value: taskPromptPath },
    usage_model:     { type: 'string', value: document.getElementById('azModel').value.trim() },
  };

  _pendingGenRubricParams = { pipelineName, params };
  renderParamPreview();
}

function renderParamPreview() {
  if (!_pendingGenRubricParams) return;
  const { pipelineName, params } = _pendingGenRubricParams;

  const el = document.getElementById('bbProgress');
  let h = `<div class="card">
    <div class="card-head">
      <span class="icon">📦</span>
      <span style="flex:1">Pipeline Parameters — <code style="color:var(--accent)">${esc(pipelineName)}</code></span>
      <button class="btn btn-primary" style="padding:5px 14px;font-size:12px" onclick="window._app.confirmTriggerGenRubric()">🚀 Trigger Pipeline</button>
      <button class="btn-sm" style="margin-left:4px" onclick="window._app.copyText(JSON.stringify(window._app.getGenRubricPayload(),null,2),'Params')">📋 Copy JSON</button>
    </div>
    <div class="card-body">
      <table style="width:100%;border-collapse:collapse;font-size:12px">
        <tr style="background:var(--surface2)">
          <th style="padding:6px 10px;text-align:left;color:var(--muted);font-size:11px;border-bottom:1px solid var(--border);width:160px">Parameter</th>
          <th style="padding:6px 10px;text-align:left;color:var(--muted);font-size:11px;border-bottom:1px solid var(--border);width:60px">Type</th>
          <th style="padding:6px 10px;text-align:left;color:var(--muted);font-size:11px;border-bottom:1px solid var(--border)">Value</th>
        </tr>`;

  for (const [name, spec] of Object.entries(params)) {
    const isArray = spec.type === 'array';
    const displayVal = isArray ? JSON.stringify(spec.value) : (spec.value || '');
    const inputId = `bbParam_${name}`;

    if (isArray) {
      h += `<tr style="border-bottom:1px solid var(--border)">
        <td style="padding:5px 10px;font-family:monospace;font-size:11px;color:var(--cyan)">${esc(name)}</td>
        <td style="padding:5px 10px;color:var(--muted);font-size:11px">${esc(spec.type)}</td>
        <td style="padding:5px 10px">
          <textarea id="${inputId}" rows="3" style="width:100%;font-size:11px;font-family:'Cascadia Code',monospace;padding:6px 8px">${esc(JSON.stringify(spec.value, null, 2))}</textarea>
          <span style="font-size:10px;color:var(--muted)">${spec.value.length} item(s)</span>
        </td>
      </tr>`;
    } else {
      h += `<tr style="border-bottom:1px solid var(--border)">
        <td style="padding:5px 10px;font-family:monospace;font-size:11px;color:var(--cyan)">${esc(name)}</td>
        <td style="padding:5px 10px;color:var(--muted);font-size:11px">${esc(spec.type)}</td>
        <td style="padding:5px 10px">
          <input id="${inputId}" type="${name.includes('token') ? 'password' : 'text'}" value="${esc(displayVal)}" style="width:100%;font-size:12px;padding:5px 8px">
        </td>
      </tr>`;
    }
  }

  h += `</table>
    </div>
  </div>`;
  el.innerHTML = h;
}

export function getGenRubricPayload() {
  if (!_pendingGenRubricParams) return {};
  const { params } = _pendingGenRubricParams;
  const payload = {};
  for (const [name, spec] of Object.entries(params)) {
    const el = document.getElementById(`bbParam_${name}`);
    if (!el) continue;
    const raw = el.value.trim();
    if (!raw) continue;
    if (spec.type === 'array') {
      try { payload[name] = JSON.parse(raw); } catch { payload[name] = raw; }
    } else {
      payload[name] = raw;
    }
  }
  return payload;
}

export async function confirmTriggerGenRubric() {
  if (!_pendingGenRubricParams) { alert('No parameters prepared'); return; }
  const { pipelineName } = _pendingGenRubricParams;
  const payload = getGenRubricPayload();

  const statusEl = document.getElementById('bbProgress');
  statusEl.innerHTML = `<div class="status-banner show" style="background:var(--surface2);border:1px solid var(--border);color:var(--muted)"><span class="spin"></span> Triggering ${esc(pipelineName)}…</div>`;
  document.getElementById('btnGenRubric').disabled = true;

  try {
    const data = await azFetch('POST', `/pipelines/${encodeURIComponent(pipelineName)}/createRun?api-version=2018-06-01`, payload);
    _bbGenRubricRunId = data.runId;
    _pendingGenRubricParams = null;
    statusEl.innerHTML = `<div class="status-banner show ok">✓ ${esc(pipelineName)} triggered — Run ID:
      <code style="font-size:12px;color:var(--green);cursor:pointer" onclick="window._app.copyText('${esc(data.runId)}','Run ID')">${esc(data.runId)}</code>
      <button class="btn-copy" style="margin-left:8px" onclick="window._app.copyText('${esc(data.runId)}','Run ID')">📋</button>
    </div>`;
    toast(pipelineName + ' triggered!');
    pollGenRubricPipeline(data.runId);
  } catch (e) {
    statusEl.innerHTML = `<div class="status-banner show err">✕ ${esc(e.message)}</div>`;
    document.getElementById('btnGenRubric').disabled = false;
  }
}

async function pollGenRubricPipeline(runId) {
  const pipelineEl = document.getElementById('bbPipelineStatus');
  pipelineEl.innerHTML = `<div class="card"><div class="card-head"><span class="spin"></span> Waiting for gen_rubric pipeline to complete… (polling every 15s)</div><div class="card-body" id="bbPollBody"><p class="empty">Polling…</p></div></div>`;

  const poll = async () => {
    try {
      const run = await azFetch('GET', `/pipelineruns/${encodeURIComponent(runId)}?api-version=2018-06-01`);
      const status = (run.status || '').toLowerCase();
      const elapsed = fmtDuration(run.runStart, run.runEnd || new Date().toISOString());

      if (status === 'succeeded') {
        clearInterval(_bbPollTimer); _bbPollTimer = null;
        await extractRubricPaths(runId);
        pipelineEl.innerHTML = `<div class="card"><div class="card-head" style="color:var(--green)">✓ gen_rubric completed successfully (${elapsed})</div>
          <div class="card-body" id="bbPollBody">${renderRubricPaths()}</div></div>`;
        document.getElementById('btnAutoRun').disabled = false;
        document.getElementById('btnGenRubric').disabled = false;

        if (document.getElementById('bbAutoTrigger').checked) {
          toast('Auto-triggering auto_run_bugbash…');
          setTimeout(() => triggerBugBashAutoRun(), 1500);
        }
        return;
      }
      if (status === 'failed' || status === 'cancelled' || status === 'cancelling') {
        clearInterval(_bbPollTimer); _bbPollTimer = null;
        pipelineEl.innerHTML = `<div class="card"><div class="card-head" style="color:var(--red)">✕ gen_rubric ${run.status} (${elapsed})</div>
          <div class="card-body"><p style="color:var(--red);font-size:12px">${esc(run.message || 'Check Monitor tab for details')}</p>
          <button class="btn-sm" style="margin-top:8px" onclick="window._app.goStep('monitor');window._app.refreshMonitor()">View in Monitor →</button></div></div>`;
        document.getElementById('btnGenRubric').disabled = false;
        return;
      }

      let actHtml = `<p style="font-size:12px;color:var(--muted)">Status: ${statusBadge(run.status)} &middot; Elapsed: ${elapsed}</p>`;
      try {
        const actData = await azFetch('POST', `/pipelineruns/${encodeURIComponent(runId)}/queryActivityruns?api-version=2018-06-01`,
          { lastUpdatedAfter: '2024-01-01T00:00:00Z', lastUpdatedBefore: new Date(Date.now() + 86400000).toISOString() });
        const acts = actData.value || [];
        if (acts.length) {
          let nS = 0, nF = 0, nR = 0;
          for (const a of acts) { const s = (a.status || '').toLowerCase(); if (s === 'succeeded') nS++; else if (s === 'failed') nF++; else nR++; }
          const pct = acts.length ? Math.round((nS + nF) / acts.length * 100) : 0;
          actHtml += `<div style="margin-top:8px;font-size:12px">Activities: <span style="color:var(--green)">✓${nS}</span> <span style="color:var(--red)">✕${nF}</span> <span style="color:var(--orange)">◐${nR}</span> (${pct}%)</div>`;
          actHtml += `<div class="progress-bar" style="margin-top:6px"><div class="progress-fill" style="width:${pct}%;background:var(--green)"></div></div>`;
        }
      } catch {}
      const bodyEl = document.getElementById('bbPollBody');
      if (bodyEl) bodyEl.innerHTML = actHtml;
    } catch (e) {
      const bodyEl = document.getElementById('bbPollBody');
      if (bodyEl) bodyEl.innerHTML = `<p style="color:var(--red);font-size:12px">Poll error: ${esc(e.message)}</p>`;
    }
  };

  await poll();
  _bbPollTimer = setInterval(poll, 15000);
}

async function extractRubricPaths(runId) {
  _bbRubricPaths = {};
  try {
    const actData = await azFetch('POST', `/pipelineruns/${encodeURIComponent(runId)}/queryActivityruns?api-version=2018-06-01`,
      { lastUpdatedAfter: '2024-01-01T00:00:00Z', lastUpdatedBefore: new Date(Date.now() + 86400000).toISOString() });
    const acts = actData.value || [];
    for (const a of acts) {
      if (a.status === 'Succeeded' && a.output) {
        const out = a.output;
        if (out.rubric_path) _bbRubricPaths[a.activityName || ''] = out.rubric_path;
        else if (out.output_path) _bbRubricPaths[a.activityName || ''] = out.output_path;
        else if (out.customOutput) {
          try {
            const co = typeof out.customOutput === 'string' ? JSON.parse(out.customOutput) : out.customOutput;
            if (co.rubric_path) _bbRubricPaths[a.activityName || ''] = co.rubric_path;
          } catch {}
        }
      }
    }

    const rubricFolder = document.getElementById('bbRubricOutputFolder').value.trim();
    if (rubricFolder) {
      const prefix = rubricFolder + '/';
      const url = blobBaseUrl() + `?restype=container&comp=list&prefix=${encodeURIComponent(prefix)}&delimiter=`;
      const resp = await fetch(url, { headers: blobHeaders() });
      if (resp.ok) {
        const xml = await resp.text();
        const doc = new DOMParser().parseFromString(xml, 'text/xml');
        doc.querySelectorAll('Blob').forEach(b => {
          const name = b.querySelector('Name')?.textContent || '';
          const shortName = name.replace(prefix, '');
          _bbRubricPaths[shortName] = name;
        });
      }
    }
  } catch (e) { console.error('extractRubricPaths:', e); }
}

function renderRubricPaths() {
  const keys = Object.keys(_bbRubricPaths);
  if (!keys.length) return '<p style="color:var(--muted);font-size:12px">No rubric paths detected from pipeline output. Check the rubric output folder or Monitor tab for details.</p>';
  let h = '<div style="font-size:12px;margin-bottom:8px;color:var(--muted)">' + keys.length + ' rubric file(s) found:</div>';
  for (const [k, v] of Object.entries(_bbRubricPaths)) {
    h += `<div class="blob-file">
      <span class="blob-icon">📊</span>
      <span class="blob-name">${esc(k)}</span>
      <span style="color:var(--muted);font-size:11px;margin-left:auto">${esc(typeof v === 'string' ? v : '')}</span>
      <button class="btn-copy" onclick="window._app.copyText('${esc(typeof v === 'string' ? v : JSON.stringify(v))}','Path')">📋</button>
    </div>`;
  }
  return h;
}

export async function triggerBugBashAutoRun() {
  const pipelineName = document.getElementById('bbAutoRunPipeline').value.trim();
  if (!pipelineName) { alert('Set Auto Run Bug Bash pipeline name'); return; }

  const rubricFolder = document.getElementById('bbRubricOutputFolder').value.trim();
  const bbOutputFolder = document.getElementById('bbBugBashOutputFolder').value.trim();
  const storageAccount = document.getElementById('azStorage').value.trim();
  const container = document.getElementById('azContainer').value.trim();

  const rubricPaths = Object.values(_bbRubricPaths).filter(v => typeof v === 'string');

  const statusEl = document.getElementById('bbPipelineStatus');
  const existingContent = statusEl.innerHTML;
  statusEl.innerHTML = existingContent + `<div class="status-banner show" style="background:var(--surface2);border:1px solid var(--border);color:var(--muted);margin-top:12px"><span class="spin"></span> Triggering auto_run_bugbash pipeline…</div>`;
  document.getElementById('btnAutoRun').disabled = true;

  try {
    const params = {
      rubric_folder: rubricFolder,
      rubric_paths: rubricPaths.length ? rubricPaths : undefined,
      output_folder: bbOutputFolder,
      storage_account: storageAccount,
      container,
    };
    if (_bbGenRubricRunId) params.gen_rubric_run_id = _bbGenRubricRunId;
    const mo = document.getElementById('azModel').value.trim();
    if (mo) params.copilot_model = mo;

    const data = await azFetch('POST', `/pipelines/${encodeURIComponent(pipelineName)}/createRun?api-version=2018-06-01`, params);
    statusEl.innerHTML = existingContent + `<div class="card" style="margin-top:12px">
      <div class="card-head" style="color:var(--green)">✓ auto_run_bugbash triggered</div>
      <div class="card-body">
        <p style="font-size:12px">Run ID: <code style="color:var(--green);cursor:pointer" onclick="window._app.copyText('${esc(data.runId)}','Run ID')">${esc(data.runId)}</code>
          <button class="btn-copy" style="margin-left:6px" onclick="window._app.copyText('${esc(data.runId)}','Run ID')">📋</button></p>
        <p style="font-size:12px;color:var(--muted);margin-top:6px">The bug bash pipeline is now running. Monitor progress in the Monitor tab.</p>
        <button class="btn-sm" style="margin-top:8px" onclick="window._app.goStep('monitor');window._app.refreshMonitor()">View in Monitor →</button>
      </div>
    </div>`;
    toast('auto_run_bugbash triggered!');
  } catch (e) {
    statusEl.innerHTML = existingContent + `<div class="status-banner show err" style="margin-top:12px">✕ ${esc(e.message)}</div>`;
    document.getElementById('btnAutoRun').disabled = false;
  }
}

export async function loadPipelineDefinition(inputId) {
  const pipelineName = document.getElementById(inputId).value.trim();
  if (!pipelineName) { alert('Pipeline name is empty'); return; }
  const el = document.getElementById('bbPipelineDefContent');
  el.innerHTML = `<p class="empty"><span class="spin"></span> Fetching pipeline definition for <b>${esc(pipelineName)}</b>…</p>`;

  try {
    const def = await azFetch('GET', `/pipelines/${encodeURIComponent(pipelineName)}?api-version=2018-06-01`);
    const props = def.properties || {};
    const params = props.parameters || {};
    const activities = props.activities || [];
    const paramKeys = Object.keys(params);

    let h = `<div style="border:1px solid var(--border);border-radius:var(--r);overflow:hidden">`;
    h += `<div style="padding:10px 14px;background:var(--surface2);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px">
      <span style="font-weight:600;font-size:13px;color:var(--accent);flex:1">📦 ${esc(pipelineName)}</span>
      <span style="font-size:11px;color:var(--muted)">${paramKeys.length} parameter(s), ${activities.length} activity/activities</span>
      <button class="btn-copy" onclick="window._app.copyText(JSON.stringify(${esc(JSON.stringify(def))},null,2),'Pipeline JSON')">📋 Copy Full JSON</button>
    </div>`;

    if (paramKeys.length) {
      h += `<div style="padding:12px 14px;border-bottom:1px solid var(--border)">
        <div style="font-size:12px;font-weight:600;color:var(--text-sec);margin-bottom:8px">Parameters</div>
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <tr style="background:var(--surface2)">
            <th style="padding:6px 10px;text-align:left;color:var(--muted);font-size:11px;border-bottom:1px solid var(--border)">Name</th>
            <th style="padding:6px 10px;text-align:left;color:var(--muted);font-size:11px;border-bottom:1px solid var(--border)">Type</th>
            <th style="padding:6px 10px;text-align:left;color:var(--muted);font-size:11px;border-bottom:1px solid var(--border)">Default Value</th>
          </tr>`;
      for (const [name, spec] of Object.entries(params)) {
        const type = spec.type || 'String';
        let defVal = spec.defaultValue;
        let defDisplay = '';
        if (defVal === undefined || defVal === null) defDisplay = '<span style="color:var(--muted);font-style:italic">(none)</span>';
        else if (typeof defVal === 'object') defDisplay = `<code style="font-size:10px;color:var(--green);cursor:pointer" onclick="window._app.openModal('${esc(name)} default',JSON.stringify(${esc(JSON.stringify(defVal))},null,2))">{${Array.isArray(defVal) ? defVal.length + ' items' : 'object'}} → click</code>`;
        else defDisplay = `<span style="word-break:break-all">${esc(String(defVal))}</span>`;
        h += `<tr style="border-bottom:1px solid var(--border)">
          <td style="padding:5px 10px;font-family:monospace;color:var(--cyan);font-size:11px">${esc(name)}</td>
          <td style="padding:5px 10px;color:var(--muted)">${esc(type)}</td>
          <td style="padding:5px 10px">${defDisplay}</td>
        </tr>`;
      }
      h += `</table></div>`;
    }

    if (activities.length) {
      h += `<div style="padding:12px 14px;border-bottom:1px solid var(--border)">
        <div style="font-size:12px;font-weight:600;color:var(--text-sec);margin-bottom:8px">Activities</div>
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <tr style="background:var(--surface2)">
            <th style="padding:6px 10px;text-align:left;color:var(--muted);font-size:11px;border-bottom:1px solid var(--border)">Name</th>
            <th style="padding:6px 10px;text-align:left;color:var(--muted);font-size:11px;border-bottom:1px solid var(--border)">Type</th>
            <th style="padding:6px 10px;text-align:left;color:var(--muted);font-size:11px;border-bottom:1px solid var(--border)">Depends On</th>
            <th style="padding:6px 10px;text-align:center;color:var(--muted);font-size:11px;border-bottom:1px solid var(--border)">Details</th>
          </tr>`;
      for (const act of activities) {
        const deps = (act.dependsOn || []).map(d => d.activity || '').filter(Boolean).join(', ') || '-';
        h += `<tr style="border-bottom:1px solid var(--border)">
          <td style="padding:5px 10px;font-family:monospace;font-size:11px;color:var(--accent)">${esc(act.name || '-')}</td>
          <td style="padding:5px 10px;color:var(--muted);font-size:11px">${esc(act.type || '-')}</td>
          <td style="padding:5px 10px;font-size:11px">${esc(deps)}</td>
          <td style="padding:5px 10px;text-align:center"><button class="btn-log json" onclick="window._app.openModal('Activity: ${esc(act.name || '')}',JSON.stringify(${esc(JSON.stringify(act))},null,2))">{ }</button></td>
        </tr>`;
      }
      h += `</table></div>`;
    }

    if (props.description || props.annotations?.length) {
      h += `<div style="padding:10px 14px;font-size:11px;color:var(--muted)">`;
      if (props.description) h += `<div><b>Description:</b> ${esc(props.description)}</div>`;
      if (props.annotations?.length) h += `<div><b>Annotations:</b> ${props.annotations.map(a => esc(String(a))).join(', ')}</div>`;
      h += `</div>`;
    }

    h += `<div style="padding:10px 14px"><details>
      <summary style="cursor:pointer;font-size:11px;color:var(--muted);font-weight:600">Full Pipeline Definition JSON</summary>
      <pre style="margin-top:6px;padding:10px;background:var(--bg);border:1px solid var(--border);border-radius:var(--r);font-size:11px;overflow-x:auto;white-space:pre-wrap;color:var(--green);max-height:500px">${esc(JSON.stringify(def, null, 2))}</pre>
    </details></div>`;

    h += `</div>`;
    el.innerHTML = h;
  } catch (e) {
    el.innerHTML = `<p class="empty" style="color:var(--red)">Error loading pipeline definition: ${esc(e.message)}</p>`;
  }
}

export function resetBugBash() {
  _bbCases = [];
  _bbGenRubricRunId = null;
  _bbRubricPaths = {};
  if (_bbPollTimer) { clearInterval(_bbPollTimer); _bbPollTimer = null; }
  document.getElementById('bbCasesContent').innerHTML = '<p class="empty">Click "Load from Blob" to list generated JSONL files and match tar.gz snapshots</p>';
  document.getElementById('bbProgress').innerHTML = '';
  document.getElementById('bbPipelineStatus').innerHTML = '';
  document.getElementById('bbPipelineDefContent').innerHTML = '';
  document.getElementById('btnGenRubric').disabled = false;
  document.getElementById('btnAutoRun').disabled = true;
  toast('Bug Bash reset');
}

/* ==================== Export Prompts as Markdown ==================== */

export async function exportPromptsAsMd() {
  const selected = _bbCases.filter(c => c.selected && c.data && !c.data._error);
  if (!selected.length) { alert('Select at least one valid case'); return; }

  const mdFolder = document.getElementById('bbPromptsFolder').value.trim();
  if (!mdFolder) { alert('Set Prompts Output Folder first'); return; }

  const container = document.getElementById('azContainer').value.trim();
  const storageAccount = document.getElementById('azStorage').value.trim();
  const baseUrl = `https://${storageAccount}.blob.core.windows.net/${container}`;
  const headers = { ...blobHeaders(), 'x-ms-blob-type': 'BlockBlob', 'Content-Type': 'text/markdown; charset=utf-8' };

  const statusEl = document.getElementById('bbProgress');
  statusEl.innerHTML = `<div class="status-banner show" style="background:var(--surface2);border:1px solid var(--border);color:var(--muted)"><span class="spin"></span> Uploading ${selected.length} prompt(s) as Markdown…</div>`;

  let ok = 0, fail = 0;
  const errors = [];
  for (const c of selected) {
    const instanceId = c.data.instance_id || c.baseName;
    const issueText = c.data.issue_text || '';
    const mdContent = `# ${instanceId}\n\n${issueText}`;
    const blobPath = `${mdFolder}/${instanceId}.md`;
    const url = baseUrl + '/' + encodeURIComponent(blobPath).replace(/%2F/g, '/');

    try {
      const resp = await fetch(url, { method: 'PUT', headers, body: mdContent });
      if (!resp.ok) { let m = `HTTP ${resp.status}`; try { m = await resp.text(); } catch {} throw new Error(m); }
      ok++;
    } catch (e) {
      fail++;
      errors.push(`${instanceId}: ${e.message}`);
    }
  }

  if (fail === 0) {
    statusEl.innerHTML = `<div class="status-banner show ok">✓ Exported ${ok} prompt(s) to <code>${esc(mdFolder)}/</code></div>`;
    toast(`${ok} prompts exported`);
  } else {
    statusEl.innerHTML = `<div class="status-banner show err">Exported ${ok}, failed ${fail}. Errors: ${errors.slice(0, 3).map(e => esc(e)).join('; ')}${errors.length > 3 ? '…' : ''}</div>`;
  }
}
