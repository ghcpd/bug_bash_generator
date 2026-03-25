import { esc } from './utils.js';
import { openModal } from './modal.js';

/** Parse unified diff into structured file hunks */
function parseDiff(diffText) {
  if (!diffText) return [];
  const files = [];
  let currentFile = null, currentHunk = null;
  for (const line of diffText.split('\n')) {
    if (line.startsWith('diff --git')) {
      currentFile = { header: line, oldFile: '', newFile: '', hunks: [] };
      files.push(currentFile);
    } else if (line.startsWith('---') && currentFile) {
      currentFile.oldFile = line.replace(/^---\s+[ab]\//, '');
    } else if (line.startsWith('+++') && currentFile) {
      currentFile.newFile = line.replace(/^\+\+\+\s+[ab]\//, '');
    } else if (line.startsWith('@@') && currentFile) {
      const m = line.match(/@@ -(\d+),?\d* \+(\d+),?\d* @@(.*)/);
      currentHunk = { header: line, oldStart: m ? +m[1] : 1, newStart: m ? +m[2] : 1, context: m ? m[3].trim() : '', lines: [] };
      currentFile.hunks.push(currentHunk);
    } else if (currentHunk) {
      if (line.startsWith('+')) currentHunk.lines.push({ type: 'add', text: line.slice(1) });
      else if (line.startsWith('-')) currentHunk.lines.push({ type: 'del', text: line.slice(1) });
      else currentHunk.lines.push({ type: 'ctx', text: line.startsWith(' ') ? line.slice(1) : line });
    }
  }
  return files;
}

/** Render a single diff file block as GitHub-style HTML */
function renderDiffFile(file) {
  const fileName = file.newFile || file.oldFile || 'unknown';
  let h = `<div class="diff-file">
    <div class="diff-file-header"><span class="diff-file-name">📄 ${esc(fileName)}</span></div>
    <table class="diff-table"><tbody>`;
  for (const hunk of file.hunks) {
    h += `<tr class="diff-hunk-row"><td class="diff-ln"></td><td class="diff-ln"></td><td class="diff-hunk-text">${esc(hunk.header)}</td></tr>`;
    let oldLine = hunk.oldStart, newLine = hunk.newStart;
    for (const ln of hunk.lines) {
      const cls = ln.type === 'add' ? 'diff-add' : ln.type === 'del' ? 'diff-del' : '';
      const oln = ln.type === 'add' ? '' : oldLine++;
      const nln = ln.type === 'del' ? '' : newLine++;
      const prefix = ln.type === 'add' ? '+' : ln.type === 'del' ? '-' : ' ';
      h += `<tr class="${cls}"><td class="diff-ln">${oln}</td><td class="diff-ln">${nln}</td><td class="diff-code"><span class="diff-prefix">${prefix}</span>${esc(ln.text)}</td></tr>`;
    }
  }
  h += `</tbody></table></div>`;
  return h;
}

function renderBadge(text, cls) {
  return `<span class="rv-badge ${cls}">${esc(text)}</span>`;
}

/** Render a single case card */
function renderCaseCard(caseData, idx) {
  const labels = caseData.labels || {};
  const patches = caseData.patches || {};
  const diffFiles = parseDiff(patches.gold_patch);
  const patchDiffFiles = parseDiff(patches.patch);
  const failTests = caseData.fail_to_pass || [];
  const passTests = caseData.pass_to_pass || [];

  let badges = '';
  if (labels.category) badges += renderBadge(labels.category, 'rv-badge-cat');
  if (labels.difficulty) badges += renderBadge(labels.difficulty, 'rv-badge-diff');
  if (labels.sub_type) badges += renderBadge(labels.sub_type, 'rv-badge-sub');
  if (caseData.mutation_type) badges += renderBadge(caseData.mutation_type, 'rv-badge-mut');

  let h = `<div class="rv-case" id="rv-case-${idx}">
    <div class="rv-case-header" onclick="this.parentElement.querySelector('.rv-case-body').classList.toggle('collapsed')">
      <span class="rv-case-title">${esc(caseData.instance_id || `Case ${idx + 1}`)}</span>
      <span class="rv-case-repo">${esc(caseData.repo || '')}</span>
      <span class="rv-case-badges">${badges}</span>
      <span class="rv-case-toggle">›</span>
    </div>
    <div class="rv-case-body">`;

  // Issue description
  if (caseData.issue_text) {
    h += `<div class="rv-section">
      <div class="rv-section-title">📋 Issue</div>
      <div class="rv-issue-text">${esc(caseData.issue_text)}</div>
    </div>`;
  }

  // Mutation info
  if (caseData.mutation_description) {
    h += `<div class="rv-section">
      <div class="rv-section-title">🧬 Mutation</div>
      <div class="rv-mutation">${esc(caseData.mutation_description)}</div>
      ${caseData.mutation_file ? `<div class="rv-mutation-file">File: <code>${esc(caseData.mutation_file)}</code></div>` : ''}
    </div>`;
  }

  // Golden Patch (reverse diff — fixes the regression)
  h += `<div class="rv-section"><div class="rv-section-title">Golden Patch</div>`;
  if (diffFiles.length) {
    const stats = { add: 0, del: 0 };
    diffFiles.forEach(f => f.hunks.forEach(hk => hk.lines.forEach(l => { if (l.type === 'add') stats.add++; if (l.type === 'del') stats.del++; })));
    h += `<div class="rv-diff-stats" style="margin-bottom:6px"><span class="rv-stat-add">+${stats.add}</span> <span class="rv-stat-del">-${stats.del}</span></div>`;
    h += diffFiles.map(f => renderDiffFile(f)).join('');
  } else {
    h += `<p class="empty">No golden patch found</p>`;
  }
  h += `</div>`;

  // Model Patch (forward diff — model's actual changes)
  h += `<div class="rv-section"><div class="rv-section-title">Model Patch</div>`;
  if (patchDiffFiles.length) {
    const pStats = { add: 0, del: 0 };
    patchDiffFiles.forEach(f => f.hunks.forEach(hk => hk.lines.forEach(l => { if (l.type === 'add') pStats.add++; if (l.type === 'del') pStats.del++; })));
    h += `<div class="rv-diff-stats" style="margin-bottom:6px"><span class="rv-stat-add">+${pStats.add}</span> <span class="rv-stat-del">-${pStats.del}</span></div>`;
    h += patchDiffFiles.map(f => renderDiffFile(f)).join('');
  } else {
    h += `<p class="empty">No model patch found</p>`;
  }
  h += `</div>`;

  // Tests
  if (failTests.length || passTests.length) {
    h += `<div class="rv-section"><div class="rv-section-title">🧪 Tests</div>`;
    if (failTests.length) {
      h += `<div class="rv-test-group"><span class="rv-test-label rv-test-fail">fail_to_pass (${failTests.length})</span>
        <ul class="rv-test-list">${failTests.map(t => `<li>${esc(t)}</li>`).join('')}</ul></div>`;
    }
    if (passTests.length) {
      h += `<div class="rv-test-group"><span class="rv-test-label rv-test-pass">pass_to_pass (${passTests.length})</span>
        <ul class="rv-test-list">${passTests.map(t => `<li>${esc(t)}</li>`).join('')}</ul></div>`;
    }
    h += `</div>`;
  }

  // Quality
  if (caseData.quality) {
    const q = caseData.quality;
    h += `<div class="rv-section"><div class="rv-section-title">✅ Quality</div>
      <div class="rv-quality">Status: <span class="rv-badge rv-badge-${q.status === 'pending_audit' ? 'mut' : 'cat'}">${esc(q.status || 'unknown')}</span></div></div>`;
  }

  h += `</div></div>`;
  return h;
}

/** Main entry: render review view from raw JSONL text */
export function renderReview(rawText, containerEl) {
  if (!rawText || !containerEl) return;
  const lines = rawText.trim().split('\n').filter(Boolean);
  const cases = [];
  for (const line of lines) {
    try { cases.push(JSON.parse(line)); } catch {}
  }
  if (!cases.length) {
    containerEl.innerHTML = '<p class="empty">No valid cases found in this JSONL file.</p>';
    return;
  }

  // Summary bar
  const cats = {}, diffs = {};
  cases.forEach(c => {
    const l = c.labels || {};
    if (l.category) cats[l.category] = (cats[l.category] || 0) + 1;
    if (l.difficulty) diffs[l.difficulty] = (diffs[l.difficulty] || 0) + 1;
  });

  let h = `<div class="rv-summary">
    <span class="rv-summary-count">${cases.length} case${cases.length > 1 ? 's' : ''}</span>
    ${Object.entries(cats).map(([k, v]) => `<span class="rv-badge rv-badge-cat">${esc(k)} (${v})</span>`).join('')}
    ${Object.entries(diffs).map(([k, v]) => `<span class="rv-badge rv-badge-diff">${esc(k)} (${v})</span>`).join('')}
  </div>`;
  h += cases.map((c, i) => renderCaseCard(c, i)).join('');
  containerEl.innerHTML = h;
}
