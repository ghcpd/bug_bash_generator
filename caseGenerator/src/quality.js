import { toast, esc } from './utils.js';
import { blobBaseUrl, blobHeaders, corsErrorHtml } from './azure.js';

const QA_BASE = 'data_processing_demo/quansen/case_analysis';

const LEVEL_COLORS = { L1: 'var(--green)', L2: '#8fbc3a', L3: 'var(--orange)', L4: '#d83b01', L5: 'var(--red)' };
const LEVEL_LABELS = { L1: 'Trivial', L2: 'Easy', L3: 'Medium', L4: 'Hard', L5: 'Very Hard' };
const RATE_COLORS = { high: 'var(--green)', medium: 'var(--orange)', low: 'var(--red)' };

const DIM_META = [
  { key: 'localization', label: 'Localization', desc: '1 = file/function named · 5 = scattered, non-obvious locations' },
  { key: 'understanding', label: 'Understanding', desc: '1 = obvious typo · 5 = deep multi-subsystem knowledge' },
  { key: 'fix_complexity', label: 'Fix Complexity', desc: '1 = one-liner · 5 = architectural change' },
  { key: 'ambiguity', label: 'Ambiguity', desc: '1 = fully specified · 5 = extremely vague' },
  { key: 'regression_risk', label: 'Regression Risk', desc: '1 = isolated · 5 = affects fundamental behavior' },
];

let _rawDiffCases = [], _rawSummary = null, _rawResults = [];
let _blobDateMap = {}; // instance_id → YYYYMMDD from blob Last-Modified

async function fetchBlob(path) {
  const url = blobBaseUrl() + '/' + path;
  const resp = await fetch(url, { headers: blobHeaders() });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.text();
}

function parseJsonl(text) {
  return text.trim().split('\n').filter(Boolean).map(l => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
}

function levelColor(lvl) { return LEVEL_COLORS[lvl] || 'var(--muted)'; }

function getCaseDate(id) {
  return _blobDateMap[id] || null;
}

function formatDateStr(d) {
  if (!d || d.length !== 8) return d || '';
  return `${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}`;
}

async function fetchBlobDates() {
  try {
    const folder = document.getElementById('azOutputFolder').value.trim();
    const prefix = folder + '/jsonl/';
    const url = blobBaseUrl() + `?restype=container&comp=list&prefix=${encodeURIComponent(prefix)}&delimiter=`;
    const resp = await fetch(url, { headers: blobHeaders() });
    if (!resp.ok) return;
    const xml = await resp.text();
    const doc = new DOMParser().parseFromString(xml, 'text/xml');
    const blobs = doc.querySelectorAll('Blob');
    const map = {};
    blobs.forEach(b => {
      const name = b.querySelector('Name')?.textContent || '';
      const modified = b.querySelector('Last-Modified')?.textContent || '';
      if (!name.endsWith('.jsonl') || !modified) return;
      const shortName = name.replace(prefix, '').replace(/\.jsonl$/, '');
      const dt = new Date(modified);
      if (!isNaN(dt)) {
        const y = dt.getFullYear(), m = String(dt.getMonth() + 1).padStart(2, '0'), d = String(dt.getDate()).padStart(2, '0');
        map[shortName] = `${y}${m}${d}`;
      }
    });
    _blobDateMap = map;
  } catch {}
}

// ── Filter ──

function collectFilterOptions(cases) {
  const dates = new Set(), levels = new Set(), categories = new Set(),
    subTypes = new Set(), rates = new Set(), localizations = new Set(), contextDeps = new Set();
  for (const c of cases) {
    const d = getCaseDate(c.instance_id); if (d) dates.add(d);
    const lbl = c.existing_labels || {};
    if (lbl.category) categories.add(lbl.category);
    if (lbl.sub_type) subTypes.add(lbl.sub_type);
    if (lbl.localization) localizations.add(lbl.localization);
    if (lbl.context_dependency) contextDeps.add(lbl.context_dependency);
    const ev = c.evaluation;
    if (ev?.overall?.level) levels.add(ev.overall.level);
    if (ev?.estimated_agent_success_rate) rates.add(ev.estimated_agent_success_rate);
  }
  return { dates: [...dates].sort(), levels: [...levels].sort(), categories: [...categories].sort(),
    subTypes: [...subTypes].sort(), rates: [...rates].sort(), localizations: [...localizations].sort(),
    contextDeps: [...contextDeps].sort() };
}

function renderFilterBar(opts) {
  const sel = (id, label, options) => {
    const optH = options.map(o => `<option value="${esc(o)}">${esc(o)}</option>`).join('');
    return `<div class="qe-filter-item"><label>${label}</label><select id="${id}" onchange="window._app.applyQualityFilters()"><option value="">All</option>${optH}</select></div>`;
  };
  return `<div class="qe-filter-bar">
    <div class="qe-filter-row">
      <div class="qe-filter-item"><label>Case Date From</label><input type="date" id="qeFilterDateFrom" onchange="window._app.applyQualityFilters()" value=""></div>
      <div class="qe-filter-item"><label>Case Date To</label><input type="date" id="qeFilterDateTo" onchange="window._app.applyQualityFilters()" value=""></div>
      ${sel('qeFilterLevel', 'Difficulty', opts.levels)}
      ${sel('qeFilterCategory', 'Category', opts.categories)}
      ${sel('qeFilterSubType', 'Sub-Type', opts.subTypes)}
      ${sel('qeFilterRate', 'Predicted Success', opts.rates)}
      ${sel('qeFilterLocal', 'Localization', opts.localizations)}
      ${sel('qeFilterCtxDep', 'Context Dep.', opts.contextDeps)}
      <div class="qe-filter-item"><label>Min Resolve %</label><input type="number" id="qeFilterMinRes" min="0" max="100" placeholder="0" style="width:64px" onchange="window._app.applyQualityFilters()"></div>
      <div class="qe-filter-item"><label>Max Resolve %</label><input type="number" id="qeFilterMaxRes" min="0" max="100" placeholder="100" style="width:64px" onchange="window._app.applyQualityFilters()"></div>
      <div class="qe-filter-item" style="align-self:end"><button class="btn-sm" onclick="window._app.resetQualityFilters()">↺ Reset</button></div>
    </div>
    <div id="qeFilterSummary" class="qe-filter-summary"></div>
  </div>`;
}

function getFilters() {
  const v = id => document.getElementById(id)?.value || '';
  const n = id => { const el = document.getElementById(id); return el?.value !== '' ? +el.value : null; };
  return { dateFrom: v('qeFilterDateFrom').replace(/-/g, ''), dateTo: v('qeFilterDateTo').replace(/-/g, ''),
    level: v('qeFilterLevel'), category: v('qeFilterCategory'), subType: v('qeFilterSubType'),
    rate: v('qeFilterRate'), localization: v('qeFilterLocal'), contextDep: v('qeFilterCtxDep'),
    minResolve: n('qeFilterMinRes'), maxResolve: n('qeFilterMaxRes') };
}

function filterCases(cases, summary, f) {
  return cases.filter(c => {
    const d = getCaseDate(c.instance_id), ev = c.evaluation, lbl = c.existing_labels || {};
    if ((f.dateFrom || f.dateTo) && !d) return false;
    if (f.dateFrom && d < f.dateFrom) return false;
    if (f.dateTo && d > f.dateTo) return false;
    if (f.level && ev?.overall?.level !== f.level) return false;
    if (f.category && lbl.category !== f.category) return false;
    if (f.subType && lbl.sub_type !== f.subType) return false;
    if (f.rate && ev?.estimated_agent_success_rate !== f.rate) return false;
    if (f.localization && lbl.localization !== f.localization) return false;
    if (f.contextDep && lbl.context_dependency !== f.contextDep) return false;
    if (summary?.per_case && (f.minResolve !== null || f.maxResolve !== null)) {
      const r = summary.per_case[c.instance_id]?.resolve_rate ?? -1;
      if (f.minResolve !== null && r < f.minResolve) return false;
      if (f.maxResolve !== null && r > f.maxResolve) return false;
    }
    return true;
  });
}

export function applyQualityFilters() {
  const filtered = filterCases(_rawDiffCases, _rawSummary, getFilters());
  const el = document.getElementById('qeFilterSummary');
  if (el) el.textContent = `Showing ${filtered.length} of ${_rawDiffCases.length} cases`;
  renderSections(filtered, _rawSummary, _rawResults);
}

export function resetQualityFilters() {
  ['qeFilterDateFrom','qeFilterDateTo','qeFilterLevel','qeFilterCategory','qeFilterSubType',
    'qeFilterRate','qeFilterLocal','qeFilterCtxDep','qeFilterMinRes','qeFilterMaxRes'].forEach(id => {
    const el = document.getElementById(id); if (el) el.value = '';
  });
  applyQualityFilters();
}

// ── Bar chart helpers ──

function renderBarChart(data, colorMap) {
  const sorted = Object.entries(data).sort((a, b) => b[1] - a[1]);
  const max = Math.max(...sorted.map(e => e[1]), 1);
  return sorted.map(([k, v]) => {
    const pct = (v / max * 100).toFixed(0);
    const color = colorMap?.[k] || 'var(--accent)';
    return `<div class="qe-bar-row">
      <span class="qe-bar-label">${esc(k)}</span>
      <div class="qe-bar-track"><div class="qe-bar-fill" style="width:${pct}%;background:${color}"></div></div>
      <span class="qe-bar-val">${v}</span>
    </div>`;
  }).join('');
}

function renderDimensionBars(totals, counts) {
  return DIM_META.map(d => {
    const avg = counts[d.key] ? (totals[d.key] / counts[d.key]) : 0;
    const pct = (avg / 5 * 100).toFixed(0);
    const color = avg <= 2 ? 'var(--green)' : avg <= 3 ? 'var(--orange)' : 'var(--red)';
    return `<div class="qe-bar-row">
      <span class="qe-bar-label" title="${esc(d.desc)}">${esc(d.label)}</span>
      <div class="qe-bar-track"><div class="qe-bar-fill" style="width:${pct}%;background:${color}"></div></div>
      <span class="qe-bar-val">${avg.toFixed(1)}</span>
    </div>`;
  }).join('') +
  `<div style="font-size:10px;color:var(--muted);margin-top:6px;line-height:1.4">Scale: 1 (trivial) → 5 (extremely hard). Lower is easier.</div>`;
}

// ── Key Challenges (grouped by theme) ──

function renderKeyChallenges(cases) {
  const valid = cases.filter(c => c.evaluation?.key_challenges?.length);
  if (!valid.length) return '';

  // Group challenges by keywords
  const themes = {
    'Code Navigation': /navigat|find|locat|search|grep|file.*among|identify.*file/i,
    'Domain Knowledge': /domain|understand|knowledge|convention|semantic|concept|finance|crypto|energy|protocol/i,
    'Language Semantics': /python|slice|operator|precedence|type.*check|encoding|unicode/i,
    'Edge Cases': /edge|boundary|corner|special.*case|zero|empty|null|inf|overflow/i,
    'Regression Verification': /regress|break|side.*effect|doesn.*affect|verify.*fix|confirm/i,
    'Bug Pattern Recognition': /recogniz|identify|single.*character|subtle|distinguish|difference/i,
  };

  const grouped = {};
  const ungrouped = [];
  for (const c of valid) {
    for (const ch of c.evaluation.key_challenges) {
      let matched = false;
      for (const [theme, re] of Object.entries(themes)) {
        if (re.test(ch)) {
          if (!grouped[theme]) grouped[theme] = [];
          grouped[theme].push({ text: ch, id: c.instance_id });
          matched = true;
          break;
        }
      }
      if (!matched) ungrouped.push({ text: ch, id: c.instance_id });
    }
  }
  if (ungrouped.length) grouped['Other'] = ungrouped;

  const sorted = Object.entries(grouped).sort((a, b) => b[1].length - a[1].length);

  let h = `<div class="qe-challenges-grid">`;
  for (const [theme, items] of sorted) {
    h += `<div class="qe-challenge-group">
      <div class="qe-challenge-theme">
        <span>${esc(theme)}</span>
        <span class="qe-challenge-count">${items.length}</span>
      </div>
      <ul class="qe-challenge-list">`;
    // Show up to 5, collapse rest
    const show = items.slice(0, 4), rest = items.slice(4);
    for (const it of show) {
      h += `<li title="${esc(it.id)}">${esc(it.text)}</li>`;
    }
    if (rest.length) {
      h += `<li class="qe-challenge-more" onclick="this.parentElement.querySelectorAll('.qe-challenge-hidden').forEach(e=>e.style.display='list-item');this.style.display='none'">+ ${rest.length} more…</li>`;
      for (const it of rest) {
        h += `<li class="qe-challenge-hidden" style="display:none" title="${esc(it.id)}">${esc(it.text)}</li>`;
      }
    }
    h += `</ul></div>`;
  }
  h += `</div>`;
  return h;
}

// ── Difficulty Analysis ──

function renderDifficultyOverview(cases) {
  const valid = cases.filter(c => c.evaluation && !c.error);
  const levels = {}, cats = {}, successRates = {};
  const dimTotals = {}, dimCounts = {};
  DIM_META.forEach(d => { dimTotals[d.key] = 0; dimCounts[d.key] = 0; });
  let scoreSum = 0, scoreCount = 0;

  for (const c of valid) {
    const ev = c.evaluation, lbl = c.existing_labels || {};
    const lvl = ev.overall?.level || '?';
    levels[lvl] = (levels[lvl] || 0) + 1;
    cats[lbl.category || 'Unknown'] = (cats[lbl.category || 'Unknown'] || 0) + 1;
    const sr = ev.estimated_agent_success_rate || 'unknown';
    successRates[sr] = (successRates[sr] || 0) + 1;
    if (ev.overall?.score) { scoreSum += ev.overall.score; scoreCount++; }
    for (const dim of Object.keys(dimTotals)) {
      const s = ev.dimensions?.[dim]?.score;
      if (s != null) { dimTotals[dim] += s; dimCounts[dim]++; }
    }
  }

  const avgScore = scoreCount ? (scoreSum / scoreCount).toFixed(2) : '-';
  const levelEntries = Object.entries(levels).sort();

  let h = `<div class="qe-stats-grid">
    <div class="qe-stat"><div class="qe-stat-val">${valid.length}<span style="font-size:13px;color:var(--muted)">/${cases.length}</span></div><div class="qe-stat-label">Evaluated</div></div>
    <div class="qe-stat"><div class="qe-stat-val">${avgScore}</div><div class="qe-stat-label">Avg Difficulty</div></div>
    ${levelEntries.map(([l, n]) => `<div class="qe-stat"><div class="qe-stat-val" style="color:${levelColor(l)}">${n}</div><div class="qe-stat-label">${l} ${LEVEL_LABELS[l] || ''}</div></div>`).join('')}
  </div>`;

  h += `<div class="qe-row">`;
  h += `<div class="qe-chart-card">
    <div class="qe-chart-title">Dimension Avg Scores</div>
    ${renderDimensionBars(dimTotals, dimCounts)}
  </div>`;
  h += `<div class="qe-chart-card">
    <div class="qe-chart-title">Estimated Agent Success Rate</div>
    <div class="qe-bar-chart">${renderBarChart(successRates, RATE_COLORS)}</div>
    <div class="qe-chart-title" style="margin-top:14px">Category Distribution</div>
    <div class="qe-bar-chart">${renderBarChart(cats)}</div>
  </div>`;
  h += `</div>`;

  // Key Challenges
  const challengesHtml = renderKeyChallenges(valid);
  if (challengesHtml) {
    h += `<div class="qe-chart-card" style="margin-top:12px">
      <div class="qe-chart-title">Key Challenges by Theme</div>
      ${challengesHtml}
    </div>`;
  }

  return h;
}

// ── Mini Model Verification ──

function renderModelResults(summary, results, filteredIds) {
  // Filter summary per_case to match filtered cases
  const pc = summary.per_case || {};
  const entries = filteredIds
    ? Object.entries(pc).filter(([id]) => filteredIds.has(id))
    : Object.entries(pc);
  const totalRuns = entries.reduce((s, [, d]) => s + d.rounds, 0);
  const totalResolved = entries.reduce((s, [, d]) => s + d.resolved, 0);
  const resolveRate = totalRuns ? (totalResolved / totalRuns * 100).toFixed(1) : '0';
  const rateColor = +resolveRate >= 90 ? 'var(--green)' : +resolveRate >= 70 ? 'var(--orange)' : 'var(--red)';

  let h = `<div class="qe-stats-grid">
    <div class="qe-stat"><div class="qe-stat-val">${esc(summary.model)}</div><div class="qe-stat-label">Model</div></div>
    <div class="qe-stat"><div class="qe-stat-val">${entries.length}</div><div class="qe-stat-label">Cases</div></div>
    <div class="qe-stat"><div class="qe-stat-val">${totalResolved}/${totalRuns}</div><div class="qe-stat-label">Resolved/Runs</div></div>
    <div class="qe-stat"><div class="qe-stat-val" style="color:${rateColor}">${resolveRate}%</div><div class="qe-stat-label">Resolve Rate</div></div>
  </div>`;

  const sorted = entries.sort((a, b) => a[1].resolve_rate - b[1].resolve_rate);
  h += `<table class="qe-table"><thead><tr>
    <th>Instance ID</th><th>Resolved</th><th>Rate</th><th></th>
  </tr></thead><tbody>`;
  for (const [id, data] of sorted) {
    const rate = data.resolve_rate;
    const color = rate === 100 ? 'var(--green)' : rate >= 50 ? 'var(--orange)' : 'var(--red)';
    h += `<tr>
      <td class="qe-case-id">${esc(id)}</td>
      <td style="text-align:center">${data.resolved}/${data.rounds}</td>
      <td style="text-align:center;color:${color};font-weight:700">${rate.toFixed(0)}%</td>
      <td style="width:120px"><div class="qe-bar-track" style="height:6px"><div class="qe-bar-fill" style="width:${rate}%;background:${color};height:6px"></div></div></td>
    </tr>`;
  }
  h += `</tbody></table>`;

  // Duration stats
  const filteredResults = filteredIds
    ? results.filter(r => filteredIds.has(r.instance_id))
    : results;
  const durations = filteredResults.filter(r => r.duration_seconds).map(r => r.duration_seconds);
  if (durations.length) {
    const avg = (durations.reduce((a, b) => a + b, 0) / durations.length).toFixed(0);
    const min = Math.min(...durations).toFixed(0);
    const max = Math.max(...durations).toFixed(0);
    h += `<div class="qe-stats-grid" style="margin-top:12px;grid-template-columns:repeat(3,1fr)">
      <div class="qe-stat"><div class="qe-stat-val">${avg}s</div><div class="qe-stat-label">Avg Duration</div></div>
      <div class="qe-stat"><div class="qe-stat-val">${min}s</div><div class="qe-stat-label">Min</div></div>
      <div class="qe-stat"><div class="qe-stat-val">${max}s</div><div class="qe-stat-label">Max</div></div>
    </div>`;
  }
  return h;
}

// ── Cross Analysis ──

function renderCrossAnalysis(diffCases, summary) {
  if (!diffCases.length || !summary?.per_case) return '<p class="empty">No matching data</p>';
  const valid = diffCases.filter(c => c.evaluation && !c.error);
  const rows = [];
  for (const c of valid) {
    const pc = summary.per_case[c.instance_id];
    if (!pc) continue;
    const ev = c.evaluation;
    rows.push({ id: c.instance_id, level: ev.overall?.level || '?', score: ev.overall?.score || 0,
      category: c.existing_labels?.category || '?', predicted: ev.estimated_agent_success_rate || '?',
      resolveRate: pc.resolve_rate });
  }
  if (!rows.length) return '<p class="empty">No matching cases</p>';

  const byLevel = {};
  for (const r of rows) {
    if (!byLevel[r.level]) byLevel[r.level] = { count: 0, rateSum: 0 };
    byLevel[r.level].count++; byLevel[r.level].rateSum += r.resolveRate;
  }

  let h = `<div class="qe-stats-grid" style="margin-bottom:12px">`;
  for (const [lvl, data] of Object.entries(byLevel).sort()) {
    const avg = (data.rateSum / data.count).toFixed(1);
    const color = +avg >= 90 ? 'var(--green)' : +avg >= 70 ? 'var(--orange)' : 'var(--red)';
    h += `<div class="qe-stat"><div class="qe-stat-val" style="color:${color}">${avg}%</div><div class="qe-stat-label">${esc(lvl)} (${data.count})</div></div>`;
  }
  h += `</div>`;

  const predCorrect = rows.filter(r =>
    (r.predicted === 'high' && r.resolveRate >= 75) ||
    (r.predicted === 'medium' && r.resolveRate >= 25 && r.resolveRate < 100) ||
    (r.predicted === 'low' && r.resolveRate < 50)
  ).length;
  h += `<div style="font-size:12px;color:var(--muted);margin-bottom:10px">Prediction accuracy: <strong>${(predCorrect / rows.length * 100).toFixed(0)}%</strong> (${predCorrect}/${rows.length})</div>`;

  rows.sort((a, b) => a.resolveRate - b.resolveRate);
  h += `<table class="qe-table"><thead><tr>
    <th>Instance ID</th><th>Level</th><th>Score</th><th>Category</th><th>Predicted</th><th>Actual</th>
  </tr></thead><tbody>`;
  for (const r of rows) {
    const color = r.resolveRate === 100 ? 'var(--green)' : r.resolveRate >= 50 ? 'var(--orange)' : 'var(--red)';
    h += `<tr>
      <td class="qe-case-id">${esc(r.id)}</td>
      <td style="text-align:center"><span class="rv-badge rv-badge-diff" style="border-left:3px solid ${levelColor(r.level)}">${esc(r.level)}</span></td>
      <td style="text-align:center">${r.score.toFixed(1)}</td>
      <td><span class="rv-badge rv-badge-cat">${esc(r.category)}</span></td>
      <td style="text-align:center"><span class="rv-badge" style="background:${RATE_COLORS[r.predicted] || 'var(--muted)'};color:#fff">${esc(r.predicted)}</span></td>
      <td style="text-align:center;color:${color};font-weight:700">${r.resolveRate.toFixed(0)}%</td>
    </tr>`;
  }
  h += `</tbody></table>`;
  return h;
}

// ── Case Details ──

function renderCaseDetails(cases) {
  const valid = cases.filter(c => c.evaluation && !c.error);
  let h = '';
  for (const c of valid) {
    const ev = c.evaluation, ovr = ev.overall || {};
    h += `<div class="qe-case-card">
      <div class="qe-case-card-head" onclick="this.nextElementSibling.classList.toggle('collapsed')">
        <span class="qe-case-id" style="flex:1">${esc(c.instance_id)}</span>
        <span class="rv-badge rv-badge-diff" style="border-left:3px solid ${levelColor(ovr.level)}">${esc(ovr.level || '?')} ${esc(LEVEL_LABELS[ovr.level] || '')}</span>
        <span style="font-size:11px;color:var(--muted)">${ovr.score?.toFixed(1) || '-'}</span>
        <span class="rv-badge" style="background:${RATE_COLORS[ev.estimated_agent_success_rate] || 'var(--muted)'};color:#fff;font-size:10px">${esc(ev.estimated_agent_success_rate || '?')}</span>
        <span class="rv-case-toggle">›</span>
      </div>
      <div class="qe-case-card-body collapsed">
        <div class="qe-detail-summary">${esc(ovr.summary || '')}</div>
        <div class="qe-dims">`;
    for (const d of DIM_META) {
      const info = ev.dimensions?.[d.key]; if (!info) continue;
      const sC = info.score <= 2 ? 'var(--green)' : info.score <= 3 ? 'var(--orange)' : 'var(--red)';
      h += `<div class="qe-dim"><strong>${esc(d.label)}</strong>: <span style="color:${sC};font-weight:700">${info.score}</span>/5 <span class="qe-dim-reason">${esc(info.reason || '')}</span></div>`;
    }
    h += `</div>`;
    if (ev.key_challenges?.length) {
      h += `<div class="qe-challenges"><strong>Key Challenges:</strong><ul>${ev.key_challenges.map(ch => `<li>${esc(ch)}</li>`).join('')}</ul></div>`;
    }
    h += `</div></div>`;
  }
  return h;
}

// ── Section Rendering ──

function renderSections(filtered, summary, results) {
  const el = document.getElementById('qeSections');
  if (!el) return;
  const filteredIds = new Set(filtered.map(c => c.instance_id));

  let h = '';
  h += section('📊 Difficulty Analysis', renderDifficultyOverview(filtered), true);
  h += section(`🤖 Mini Model Verification (${esc(summary.model)})`, renderModelResults(summary, results, filteredIds), true);
  h += section('🔗 Difficulty vs Resolve Rate', renderCrossAnalysis(filtered, summary), true);
  h += section(`📋 Per-Case Details (${filtered.filter(c => c.evaluation && !c.error).length})`, renderCaseDetails(filtered), false);
  el.innerHTML = h;
}

function section(title, body, open) {
  return `<div class="qe-section">
    <div class="qe-section-title" onclick="this.nextElementSibling.classList.toggle('collapsed');this.querySelector('.rv-case-toggle').classList.toggle('open')">
      ${title} <span class="rv-case-toggle ${open ? 'open' : ''}">›</span>
    </div>
    <div class="qe-section-body${open ? '' : ' collapsed'}">${body}</div>
  </div>`;
}

// ── Main Entry ──

export async function loadQualityEvaluation() {
  const el = document.getElementById('qualityEvalContent');
  if (!el) return;
  el.innerHTML = `<p class="empty"><span class="spin"></span> Loading quality evaluation data…</p>`;

  try {
    // Fetch blob Last-Modified dates first, then evaluation data
    await fetchBlobDates();
    const [diffText, summaryText, resultsText] = await Promise.all([
      fetchBlob(`${QA_BASE}/difficult_analysis/difficulty_results.jsonl`),
      fetchBlob(`${QA_BASE}/mini_model_run_result/summary.json`),
      fetchBlob(`${QA_BASE}/mini_model_run_result/results.jsonl`),
    ]);

    _rawDiffCases = parseJsonl(diffText);
    _rawSummary = JSON.parse(summaryText);
    _rawResults = parseJsonl(resultsText);

    const opts = collectFilterOptions(_rawDiffCases);

    el.innerHTML = `<div class="qe-container">
      ${renderFilterBar(opts)}
      <div id="qeSections"></div>
    </div>`;

    applyQualityFilters();
  } catch (e) {
    el.innerHTML = `<p class="empty" style="color:var(--red)">${corsErrorHtml(e.message)}</p>`;
  }
}
