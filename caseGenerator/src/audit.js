import { toast, esc, fmtBytes, fmtTime, goStep } from './utils.js';
import { azFetch, blobBaseUrl, blobHeaders, corsErrorHtml } from './azure.js';
import { openModal } from './modal.js';
import {
  AUDIT_REQUIRED_FIELDS, AUDIT_VALID_CATEGORIES, AUDIT_VALID_DIFFS,
  AUDIT_VALID_SOURCES, AUDIT_VALID_LOCS, AUDIT_VALID_CTXDEPS, AUDIT_VALID_TESTMOD,
} from './constants.js';

let _auditCases = [];

export function auditL1(data) {
  const checks = [];
  const missing = AUDIT_REQUIRED_FIELDS.filter(f => !(f in data));
  checks.push({ level: 'L1', name: 'required_fields', passed: !missing.length, detail: missing.length ? 'missing: ' + missing.join(', ') : '' });
  const gp = data.patches?.gold_patch;
  checks.push({ level: 'L1', name: 'gold_patch_exists', passed: !!gp, detail: gp ? '' : 'patches.gold_patch is empty' });
  const ftp = data.fail_to_pass || [];
  checks.push({ level: 'L1', name: 'fail_to_pass_nonempty', passed: ftp.length > 0, detail: ftp.length ? ftp.length + ' test(s)' : 'empty list' });
  const src = data.source || '';
  checks.push({ level: 'L1', name: 'source_valid', passed: AUDIT_VALID_SOURCES.has(src), detail: AUDIT_VALID_SOURCES.has(src) ? '' : 'invalid: ' + src });
  const lab = data.labels || {};
  const enumChecks = [['category', AUDIT_VALID_CATEGORIES], ['difficulty', AUDIT_VALID_DIFFS], ['localization', AUDIT_VALID_LOCS], ['context_dependency', AUDIT_VALID_CTXDEPS], ['test_modality', AUDIT_VALID_TESTMOD]];
  for (const [fn, validSet] of enumChecks) {
    const v = lab[fn];
    if (v) checks.push({ level: 'L1', name: 'label_' + fn, passed: validSet.has(v), detail: validSet.has(v) ? '' : 'invalid: ' + v });
  }
  return checks;
}

export function auditL5(data) {
  const checks = [];
  const gp = data.patches?.gold_patch || '';
  const issue = (data.issue_text || '').toLowerCase();
  const changedFiles = [...gp.matchAll(/^diff --git a\/(\S+)/gm)].map(m => m[1]);
  const symbols = new Set();
  for (const line of gp.split('\n')) {
    const m = line.match(/@@.*@@\s*(?:def|class)\s+(\w+)/);
    if (m) symbols.add(m[1]);
    if ((line.startsWith('+') || line.startsWith('-')) && !line.startsWith('++') && !line.startsWith('--')) {
      for (const s of line.matchAll(/\b(\w{4,})\b/g)) symbols.add(s[1]);
    }
  }
  let fileOk = false;
  for (const f of changedFiles) {
    const base = f.split('/').pop().toLowerCase();
    if (issue.includes(base)) { fileOk = true; break; }
    for (const part of f.split('/')) { if (part.length > 3 && issue.includes(part.toLowerCase())) { fileOk = true; break; } }
  }
  const commonSym = [...symbols].filter(s => s.length > 3 && issue.includes(s.toLowerCase()));
  const ok = fileOk || commonSym.length > 0;
  const mentions = [];
  if (fileOk) mentions.push('file name');
  if (commonSym.length) mentions.push('symbols: ' + commonSym.slice(0, 5).join(', '));
  checks.push({ level: 'L5', name: 'patch_issue_coherence', passed: ok,
    detail: ok ? 'issue mentions ' + mentions.join(', ') : 'patch modifies ' + changedFiles.join(',') + ' but issue has no overlap' });
  return checks;
}

export function auditL6(data) {
  const checks = [];
  const lab = data.labels || {};
  const nLines = data.num_lines_changed || 0;
  const nFiles = data.num_files_changed || 0;
  const diff = lab.difficulty || '';
  if (diff === 'L1' && nLines > 20) checks.push({ level: 'L6', name: 'difficulty_vs_size', passed: false, detail: `L1 but ${nLines} lines changed` });
  else if (diff === 'L4' && nLines < 5) checks.push({ level: 'L6', name: 'difficulty_vs_size', passed: false, detail: `L4 but only ${nLines} lines` });
  else checks.push({ level: 'L6', name: 'difficulty_vs_size', passed: true, detail: `${diff}, ${nLines} lines` });
  const loc = lab.localization || '';
  if ((loc === 'cross_file' || loc === 'cross_module') && nFiles <= 1)
    checks.push({ level: 'L6', name: 'localization_vs_files', passed: false, detail: `${loc} but only ${nFiles} file(s)` });
  else if ((loc === 'explicit' || loc === 'implicit') && nFiles > 5)
    checks.push({ level: 'L6', name: 'localization_vs_files', passed: false, detail: `${loc} but ${nFiles} files` });
  else checks.push({ level: 'L6', name: 'localization_vs_files', passed: true, detail: `${loc}, ${nFiles} files` });
  if (lab.category === 'Performance & Efficiency' && lab.test_modality === 'unit_test')
    checks.push({ level: 'L6', name: 'category_vs_test_modality', passed: false, detail: 'Performance category with unit_test' });
  else checks.push({ level: 'L6', name: 'category_vs_test_modality', passed: true });
  return checks;
}

export async function loadAuditCases() {
  const el = document.getElementById('auditCasesContent');
  el.innerHTML = '<p class="empty"><span class="spin"></span> Loading JSONL files…</p>';
  try {
    const folder = document.getElementById('azOutputFolder').value.trim();
    const prefix = folder + '/jsonl/';
    const url = blobBaseUrl() + `?restype=container&comp=list&prefix=${encodeURIComponent(prefix)}&delimiter=`;
    const resp = await fetch(url, { headers: blobHeaders() });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const xml = await resp.text(); const parser = new DOMParser(); const doc = parser.parseFromString(xml, 'text/xml');
    const blobs = doc.querySelectorAll('Blob');
    if (!blobs.length) { el.innerHTML = '<p class="empty">No JSONL files found in ' + esc(prefix) + '</p>'; return; }
    _auditCases = [];
    blobs.forEach(b => {
      const name = b.querySelector('Name')?.textContent || '';
      const size = parseInt(b.querySelector('Content-Length')?.textContent || '0');
      const modified = b.querySelector('Last-Modified')?.textContent || '';
      if (name.endsWith('.jsonl')) _auditCases.push({ name, shortName: name.replace(prefix, ''), size, modified, selected: true, data: null });
    });
    _auditCases.sort((a, b) => new Date(b.modified) - new Date(a.modified));
    renderAuditCases();
  } catch (e) { el.innerHTML = `<p class="empty" style="color:var(--red)">${corsErrorHtml(e.message)}</p>`; }
}

export function renderAuditCases() {
  const el = document.getElementById('auditCasesContent');
  const nSel = _auditCases.filter(c => c.selected).length;
  let h = `<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
    <span style="font-size:12px;color:var(--muted)">${_auditCases.length} files, ${nSel} selected</span>
    <button class="btn-sm" onclick="window._app.selectAllAudit()">Select All</button>
    <button class="btn-sm" onclick="window._app.selectNoneAudit()">Select None</button>
  </div>`;
  h += `<div style="max-height:350px;overflow-y:auto;border:1px solid var(--border);border-radius:var(--r)">`;
  _auditCases.forEach((c, i) => {
    const chk = c.selected ? 'checked' : '';
    h += `<div class="blob-file" style="cursor:pointer" onclick="window._app.toggleAuditCase(${i})">
      <input type="checkbox" ${chk} onclick="event.stopPropagation();window._app.toggleAuditCase(${i})" style="margin-right:4px">
      <span class="blob-icon">📋</span>
      <span class="blob-name">${esc(c.shortName)}</span>
      <span class="blob-size">${fmtBytes(c.size)}</span>
      <span class="blob-size">${fmtTime(c.modified)}</span>
      ${c.data ? '<span style="color:var(--green);font-size:10px;font-weight:600">✓ loaded</span>' : ''}
    </div>`;
  });
  h += '</div>';
  el.innerHTML = h;
}

export function selectAllAudit() { _auditCases.forEach(c => c.selected = true); renderAuditCases(); }
export function selectNoneAudit() { _auditCases.forEach(c => c.selected = false); renderAuditCases(); }

export function toggleAuditCase(idx) {
  _auditCases[idx].selected = !_auditCases[idx].selected;
  renderAuditCases();
}

export async function runQuickAudit() {
  const selected = _auditCases.filter(c => c.selected);
  if (!selected.length) { alert('Select at least one case'); return; }
  const el = document.getElementById('quickAuditResults');
  el.innerHTML = `<div class="card"><div class="card-head"><span class="spin"></span> Running quick audit on ${selected.length} case(s)… downloading JSONL data</div></div>`;

  for (const c of selected) {
    if (c.data) continue;
    try {
      const url = blobBaseUrl() + '/' + encodeURIComponent(c.name).replace(/%2F/g, '/');
      const resp = await fetch(url, { headers: blobHeaders() });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const text = await resp.text();
      c.data = JSON.parse(text.trim().split('\n')[0]);
    } catch (e) { c.data = { _error: e.message }; }
  }

  const allResults = [];
  for (const c of selected) {
    if (c.data?._error) {
      allResults.push({ file: c.shortName, checks: [{ level: 'ERR', name: 'download', passed: false, detail: c.data._error }] });
      continue;
    }
    const checks = [...auditL1(c.data), ...auditL5(c.data), ...auditL6(c.data)];
    allResults.push({ file: c.shortName, instanceId: c.data.instance_id || '', checks, data: c.data });
  }

  renderQuickAuditResults(allResults);
  renderAuditCases();
}

function renderQuickAuditResults(results) {
  const el = document.getElementById('quickAuditResults');
  let totalChecks = 0, totalPassed = 0, totalFiles = results.length, filesPassed = 0;
  for (const r of results) {
    const allOk = r.checks.every(c => c.passed);
    if (allOk) filesPassed++;
    for (const c of r.checks) { totalChecks++; if (c.passed) totalPassed++; }
  }

  let h = `<div class="card"><div class="card-head"><span class="icon">🔍</span> Quick Audit Results (L1 / L5 / L6)</div><div class="card-body">`;
  h += `<div class="summary-grid" style="margin-bottom:14px">
    <div class="stat-card"><div class="stat-val">${totalFiles}</div><div class="stat-label">Cases Audited</div></div>
    <div class="stat-card"><div class="stat-val" style="color:${filesPassed === totalFiles ? 'var(--green)' : 'var(--red)'}">${filesPassed}/${totalFiles}</div><div class="stat-label">All Checks Passed</div></div>
    <div class="stat-card"><div class="stat-val" style="color:${totalPassed === totalChecks ? 'var(--green)' : 'var(--orange)'}">${totalPassed}/${totalChecks}</div><div class="stat-label">Individual Checks</div></div>
  </div>`;

  for (const r of results) {
    const allOk = r.checks.every(c => c.passed);
    const failCount = r.checks.filter(c => !c.passed).length;
    const statusIcon = allOk ? '✅' : '❌';
    const statusColor = allOk ? 'var(--green)' : 'var(--red)';

    h += `<div style="border:1px solid var(--border);border-radius:var(--r);margin-bottom:8px;overflow:hidden">
      <div style="padding:8px 12px;background:var(--surface2);display:flex;align-items:center;gap:8px;cursor:pointer;font-size:12px" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='none'?'':'none'">
        <span>${statusIcon}</span>
        <span style="font-family:monospace;color:var(--cyan);flex:1">${esc(r.file)}</span>
        ${r.instanceId ? `<span style="color:var(--muted);font-size:11px">${esc(r.instanceId)}</span>` : ''}
        <span style="color:${statusColor};font-weight:600;font-size:11px">${allOk ? 'PASS' : failCount + ' FAIL'}</span>
        ${r.data ? `<button class="btn-sm" onclick="event.stopPropagation();window._app.openModal('${esc(r.file)}',JSON.stringify(${esc(JSON.stringify(r.data))},null,2))" style="font-size:10px">{ } JSON</button>` : ''}
      </div>
      <div style="display:${allOk ? 'none' : ''}">
        <table style="width:100%;border-collapse:collapse;font-size:12px">`;
    for (const c of r.checks) {
      const sym = c.passed ? '<span style="color:var(--green)">[+]</span>' : '<span style="color:var(--red)">[-]</span>';
      h += `<tr style="border-bottom:1px solid var(--border)">
        <td style="padding:4px 12px;width:32px">${sym}</td>
        <td style="padding:4px 8px;font-weight:600;width:40px;color:var(--muted)">${esc(c.level)}</td>
        <td style="padding:4px 8px;width:180px">${esc(c.name)}</td>
        <td style="padding:4px 8px;color:var(--muted);font-size:11px">${esc(c.detail || '')}</td>
      </tr>`;
    }
    h += `</table></div></div>`;
  }
  h += `</div></div>`;
  el.innerHTML = h;
}

export async function triggerAuditPipeline() {
  const level = document.getElementById('auditLevel').value;
  const pipelineName = document.getElementById('auditPipelineName').value.trim();
  const auditOutputFolder = document.getElementById('auditOutputFolder').value.trim();
  const inputFolder = document.getElementById('azOutputFolder').value.trim();
  const storageAccount = document.getElementById('azStorage').value.trim();
  const container = document.getElementById('azContainer').value.trim();
  if (!pipelineName) { alert('Set audit pipeline name'); return; }
  if (!auditOutputFolder) { alert('Set audit output folder'); return; }

  if (!confirm(`Trigger full audit pipeline (level ${level})?\nInput: ${inputFolder}\nOutput: ${auditOutputFolder}`)) return;

  const params = { input_folder: inputFolder, output_folder: auditOutputFolder, audit_level: level, storage_account: storageAccount, container };

  const el = document.getElementById('quickAuditResults');
  el.innerHTML = `<div class="status-banner show" style="background:var(--surface2);border:1px solid var(--border);color:var(--muted)"><span class="spin"></span> Triggering audit pipeline…</div>`;
  try {
    const data = await azFetch('POST', `/pipelines/${encodeURIComponent(pipelineName)}/createRun?api-version=2018-06-01`, params);
    el.innerHTML = `<div class="status-banner show ok">✓ Audit pipeline triggered — Run ID:
      <code style="font-size:12px;color:var(--green);cursor:pointer" onclick="window._app.copyText('${esc(data.runId)}','Run ID')">${esc(data.runId)}</code>
      <button class="btn-copy" style="margin-left:8px" onclick="window._app.copyText('${esc(data.runId)}','Run ID')">📋</button>
      <button class="btn-sm" style="margin-left:8px" onclick="window._app.goStep('monitor');window._app.refreshRuns()">View in Monitor →</button>
    </div>`;
    toast('Audit pipeline triggered!');
  } catch (e) {
    el.innerHTML = `<div class="status-banner show err">✕ ${esc(e.message)}</div>`;
  }
}

export async function loadAuditResults() {
  const el = document.getElementById('auditResultsContent');
  const preview = document.getElementById('auditResultPreview');
  preview.innerHTML = '';
  el.innerHTML = '<p class="empty"><span class="spin"></span> Loading audit results…</p>';
  try {
    const prefix = document.getElementById('auditOutputFolder').value.trim() + '/';
    const url = blobBaseUrl() + `?restype=container&comp=list&prefix=${encodeURIComponent(prefix)}&delimiter=`;
    const resp = await fetch(url, { headers: blobHeaders() });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const xml = await resp.text(); const parser = new DOMParser(); const doc = parser.parseFromString(xml, 'text/xml');
    const blobs = doc.querySelectorAll('Blob');
    if (!blobs.length) { el.innerHTML = '<p class="empty">No audit results found in ' + esc(prefix) + '</p>'; return; }
    const items = [];
    blobs.forEach(b => {
      const name = b.querySelector('Name')?.textContent || '';
      const size = parseInt(b.querySelector('Content-Length')?.textContent || '0');
      const modified = b.querySelector('Last-Modified')?.textContent || '';
      items.push({ name, shortName: name.replace(prefix, ''), size, modified });
    });
    items.sort((a, b) => new Date(b.modified) - new Date(a.modified));

    let h = `<div style="font-size:11px;color:var(--muted);padding:4px 0 8px">${items.length} files in <code>${esc(prefix)}</code></div>`;
    for (const f of items) {
      const isJson = f.shortName.endsWith('.json');
      h += `<div class="blob-file" ${isJson ? `style="cursor:pointer" onclick="window._app.previewAuditResult('${f.name.replace(/'/g, "\\'")}')"` : ''}>
        <span class="blob-icon">${isJson ? '📊' : '📄'}</span>
        <span class="blob-name">${esc(f.shortName)}</span>
        <span class="blob-size">${fmtBytes(f.size)}</span>
        <span class="blob-size">${fmtTime(f.modified)}</span>
        ${isJson ? `<button class="btn-sm" onclick="event.stopPropagation();window._app.previewAuditResult('${f.name.replace(/'/g, "\\'")}')">View</button>` : ''}
      </div>`;
    }
    el.innerHTML = h;
  } catch (e) { el.innerHTML = `<p class="empty" style="color:var(--red)">${corsErrorHtml(e.message)}</p>`; }
}

export async function previewAuditResult(blobName) {
  const el = document.getElementById('auditResultPreview');
  el.innerHTML = `<div class="blob-preview"><span class="spin"></span> Loading…</div>`;
  try {
    const url = blobBaseUrl() + '/' + encodeURIComponent(blobName).replace(/%2F/g, '/');
    const resp = await fetch(url, { headers: blobHeaders() });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const text = await resp.text();
    let parsed;
    try { parsed = JSON.parse(text); } catch {}

    if (Array.isArray(parsed)) {
      let h = '<div style="margin-top:12px">';
      for (const r of parsed) {
        const allOk = r.passed;
        const checks = r.checks || [];
        const failCount = checks.filter(c => !c.passed).length;
        h += `<div style="border:1px solid var(--border);border-radius:var(--r);margin-bottom:8px;overflow:hidden">
          <div style="padding:8px 12px;background:var(--surface2);display:flex;align-items:center;gap:8px;cursor:pointer;font-size:12px" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='none'?'':'none'">
            <span>${allOk ? '✅' : '❌'}</span>
            <span style="font-family:monospace;color:var(--cyan);flex:1">${esc(r.instance_id || '')}</span>
            <span style="color:${allOk ? 'var(--green)' : 'var(--red)'};font-weight:600;font-size:11px">${allOk ? 'ALL PASS' : failCount + ' FAIL'}</span>
          </div>
          <div style="display:${allOk ? 'none' : ''}"><table style="width:100%;border-collapse:collapse;font-size:12px">`;
        for (const c of checks) {
          const sym = c.passed ? '<span style="color:var(--green)">[+]</span>' : '<span style="color:var(--red)">[-]</span>';
          h += `<tr style="border-bottom:1px solid var(--border)">
            <td style="padding:4px 12px;width:32px">${sym}</td>
            <td style="padding:4px 8px;font-weight:600;width:40px;color:var(--muted)">${esc(c.level)}</td>
            <td style="padding:4px 8px;width:180px">${esc(c.name)}</td>
            <td style="padding:4px 8px;color:var(--muted);font-size:11px">${esc(c.detail || '')}</td></tr>`;
        }
        h += `</table></div></div>`;
      }
      h += '</div>';
      el.innerHTML = h;
    } else {
      el.innerHTML = `<div class="blob-preview">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
          <span style="font-weight:600;font-size:12px;color:var(--accent);flex:1">${esc(blobName.split('/').pop())}</span>
          <button class="btn-copy" onclick="window._app.clipboardWrite(document.getElementById('auditText').textContent).then(()=>window._app.toast('Copied!'))">📋 Copy</button>
        </div>
        <pre id="auditText">${esc(typeof parsed === 'object' ? JSON.stringify(parsed, null, 2) : text)}</pre>
      </div>`;
    }
  } catch (e) { el.innerHTML = `<div class="blob-preview"><p style="color:var(--red)">${corsErrorHtml(e.message)}</p></div>`; }
}
