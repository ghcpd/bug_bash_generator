export const CATEGORIES = [
  '',
  'Logic & Algorithm',
  'Data Handling & Transformation',
  'API & Interface Contract',
  'Error Handling & Edge Cases',
  'Infrastructure & Tooling',
  'Performance & Efficiency',
  'Security & Access Control',
  'Configuration & Environment',
  'Type & Validation',
  'Documentation & Naming',
];

export const DIFFS = ['', 'L1', 'L2', 'L3', 'L4'];

export const STEPS = ['configure', 'monitor', 'results', 'audit', 'bugbash', 'dashboard'];

export const RUN_TYPES = {
  generate: { pipelineInputId: 'azPipeline', label: 'Generate pipeline', badgeClass: 'badge-generate' },
  audit: { pipelineInputId: 'auditPipelineName', label: 'Audit pipeline', badgeClass: 'badge-audit' },
  genRubric: { pipelineInputId: 'bbGenRubricPipeline', label: 'Gen Rubric pipeline', badgeClass: 'badge-gen-rubric' },
  autoRunBugbash: { pipelineInputId: 'bbAutoRunPipeline', label: 'Auto Run Bug Bash pipeline', badgeClass: 'badge-auto-run' },
};

export const STORAGE_KEY = 'swebench_prompt_gen';

export const AUDIT_REQUIRED_FIELDS = [
  'instance_id', 'repo', 'base_commit', 'workspace_dir', 'source',
  'issue_text', 'patches', 'fail_to_pass', 'labels', 'quality',
];

export const AUDIT_VALID_CATEGORIES = new Set([
  'Logic & Algorithm', 'Data Handling & Transformation', 'API & Interface Contract',
  'Error Handling & Edge Cases', 'Infrastructure & Tooling', 'Performance & Efficiency',
  'Security & Access Control', 'Configuration & Environment', 'Type & Validation',
  'Documentation & Naming',
]);

export const AUDIT_VALID_DIFFS = new Set(['L1', 'L2', 'L3', 'L4']);
export const AUDIT_VALID_SOURCES = new Set(['real_extraction', 'synthetic_mutation']);
export const AUDIT_VALID_LOCS = new Set(['explicit', 'implicit', 'cross_file', 'cross_module']);
export const AUDIT_VALID_CTXDEPS = new Set(['self_contained', 'local_context', 'global_context']);
export const AUDIT_VALID_TESTMOD = new Set(['unit_test', 'integration_test', 'regression_test', 'performance_test']);
