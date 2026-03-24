# SWE-bench Synthetic Case Generator — 交接文档

英文版对应文档为 `README.md`。
本文档覆盖 `scripts/generate_case/` 目录下的全部后端脚本、`bugbash-case-generator` 前端工程，以及它们是如何通过 Azure Data Factory + Azure Batch 串联起来跑的。

---

## 目录

1. [一句话概览](#一句话概览)
2. [运行环境与账号信息](#运行环境与账号信息)
3. [整体架构图](#整体架构图)
4. [核心流程详解](#核心流程详解)
   - [4.1 Generate Pipeline（生成 case）](#41-generate-pipeline生成-case)
   - [4.2 Audit Pipeline（审计 case）](#42-audit-pipeline审计-case)
5. [产物与数据结构](#产物与数据结构)
  - [5.1 输出目录](#51-输出目录)
  - [5.2 Generate Case JSONL 全字段说明](#52-generate-case-jsonl-全字段说明)
  - [5.3 横向对比推荐指标](#53-横向对比推荐指标)
6. [目录结构与文件速查](#目录结构与文件速查)
  - [6.1 后端脚本 `scripts/generate_case/`](#61-后端脚本-scriptsgenerate_case)
  - [6.2 前端工程 `bugbash-case-generator/`](#62-前端工程-bugbash-case-generator)
  - [6.3 ADF Pipeline 定义](#63-adf-pipeline-定义)
7. [后端脚本逐文件详解](#后端脚本逐文件详解)
8. [前端部署与鉴权](#前端部署与鉴权)
  - [8.1 部署架构](#81-部署架构)
  - [8.2 Token / 鉴权机制](#82-token--鉴权机制)
9. [常见操作手册](#常见操作手册)
10. [踩过的坑](#踩过的坑)

---

## 一句话概览

这套系统用 **Copilot CLI** 作为 AI 引擎，自动给 GitHub 上任意 Python 仓库**注入 bug → 写测试 → 验证 FAIL→PASS → LLM Critic 审查 → 打包成 SWE-bench 标准格式**。编排层是 **Azure Data Factory (ADF)**，计算层是 **Azure Batch**（一个 case = 一个 Batch 节点），前端是一个 **Vite 单页应用**，部署在 Traefik 反代后面。

---

## 运行环境与账号信息

```jsonc
{
  "subscription": "Azure Vision Machine Learning Platform Team",
  "subscription_id": "3cfa45ba-9ce3-42fb-9e1a-47374e9dea5b",
  "resource_group": "acv-dp-wu2-p-001-rg",
  "region": "West US 2",
  "adf_name": "acv-dp-wu2-p-001-adf",             // ADF 实例名
  "batch_endpoint": "acvdpwu2p002ba.westus2.batch.azure.com",
  "batch_pool": "gen_rubric",                       // Batch 计算池
  "storage_account": "genaitextdatawu2",            // Blob 存储账号
  "storage_container": "code",                      // Blob 容器
  "vm_name": "Ubuntu20-4C-16G",                     // 可视化服务器
  "vm_ip": "10.249.238.9",                          // 私有 IP（需 VPN）
  "vm_port": 2382,                                   // Traefik 入口端口
  "frontend_path": "/caseGenerator"                  // 前端在 Traefik 上的路径
}
```

---

## 整体架构图

```
┌──────────────────────────────────────────────────────────────────┐
│  浏览器（前端 UI）                                               │
│  http://VM:2382/caseGenerator                                    │
│                                                                  │
│  功能：配置任务 → 触发 ADF pipeline → 监控运行 → 查看结果/审计  │
│  鉴权：通过 Azure REST API 直接调 Azure（不经过后端）            │
└──────────┬───────────────────────────────────────────────────────┘
           │ Azure Management API
           ▼
┌──────────────────────────────────────────────────────────────────┐
│  Azure Data Factory (ADF)                                        │
│  acv-dp-wu2-p-001-adf                                           │
│                                                                  │
│  Pipeline: bug_bash_batch_generate_pipeline                      │
│    └─ ForEach task_item (batchCount=并行数)                      │
│       └─ AzureBatchLinkedService → gen_rubric 计算池             │
│          └─ 执行 generate/generate_step01_generate_case.sh       │
│                                                                  │
│  Pipeline: bug_bash_audit_case_pipeline                          │
│    └─ 执行 audit/audit_step01_dispatch_audit.sh                 │
└──────────┬───────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────┐
│  Azure Batch 计算节点                                            │
│  Endpoint: acvdpwu2p002ba.westus2.batch.azure.com               │
│  Pool: gen_rubric                                                │
│                                                                  │
│  每个节点：                                                      │
│    1. 挂载 Blob Storage → /mnt/batch/tasks/fsmounts/...         │
│    2. bash generate/generate_step01_generate_case.sh <base64_task> <token> <prompt> │
│    3. gh copilot 生成 → 验证 → critic → 输出                    │
│    4. 结果写回挂载的 Blob 目录                                   │
└──────────┬───────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────┐
│  Azure Blob Storage                                              │
│  Account: genaitextdatawu2                                       │
│  Container: code                                                 │
│                                                                  │
│  输出路径: data_processing_demo/owen/generate-case/              │
│    ├── tar.gz/        ← 注入 bug 后的代码快照                    │
│    ├── jsonl/         ← case 元数据（issue、patch、标签）        │
│    └── metrics/       ← Copilot CLI token/时间统计               │
└──────────────────────────────────────────────────────────────────┘
```

## 核心流程详解

### 4.1 Generate Pipeline（生成 case）

这是核心功能。一次 pipeline run 会为指定的 repo 列表批量生成 synthetic case。

#### 到底是怎么“注入问题”的

这里最容易误解的一点是：**这套系统不是靠预先写好的规则脚本去机械地改某几行代码**，而是把仓库 clone 下来之后，把“怎么挑文件、怎么改、怎么验证”的任务交给 `gh copilot` 这个 agent 去完成。

也就是说，真正执行“注入问题”的主体是 **Copilot CLI + prompt**，不是 `generate/generate_step01_generate_case.sh` 里某个固定的 `sed` / AST rewrite / regex 替换逻辑。

更具体地说，注入过程分成 4 层：

1. **脚本先准备上下文**
  - `generate/generate_step01_generate_case.sh` 会先 clone 目标仓库。
   - 收集 repo tree、base commit、repo owner/name、case index。
  - 再把这些信息和 `generate/default_prompt.md` 拼成一个完整 prompt。

2. **prompt 约束 agent 怎么改代码**
  - `generate/default_prompt.md` 明确要求 agent：
     - 只改 **一个 source file**
     - bug 必须是 **1–5 行** 的微小改动
     - 先读源码，再自己选注入点
     - 写 `test_synthetic_N.py`
     - 在 buggy 状态下跑到 FAIL
     - 回退 bug 后跑到 PASS
     - 最后再把 bug 放回去，让仓库停在 buggy 状态
   - category / difficulty 也是通过 prompt 约束的，不是脚本硬编码决定的。

3. **agent 真正在工作目录里改源码**
   - agent 会直接修改 clone 下来的 repo 文件。
   - 典型 mutation 不是“大改重构”，而是像下面这种很像真实开发手滑的改法：
     - 比较符号改错
     - off-by-one
     - 默认值错了
     - 少了一个 null check
     - 返回值或参数顺序错了
   - 这一步改完后，仓库里已经是真正的 buggy 代码，而不是某个“描述性的 patch 对象”。

4. **脚本不相信 agent 的口头描述，只认实际 diff**
  - agent 改完以后，`generate/generate_step01_generate_case.sh` 会运行：
     - `git diff -R -- '*.py' ':!test_synthetic_*'`
   - 这里拿到的 reverse diff 就是后续保存到 JSONL 里的 `gold_patch`。
   - 换句话说：
     - 工作目录当前状态 = buggy code
     - `gold_patch` = 把 buggy code 修回正确代码的补丁
  - 后面 `generate/generate_step04_package_case_artifacts.py` 再通过 `git apply --reverse gold_patch` 来构造最终 tar.gz 快照。

所以这套系统里的“注入问题”，本质上是：

```text
prompt 约束 agent 选点并改源码
        ↓
agent 在 clone 下来的 repo 里真的改出一个 bug
        ↓
脚本用 git diff 把这次真实修改固化成 gold_patch
        ↓
再把 buggy 状态的仓库打包成 tar.gz
```

> 这不是规则引擎在“生成 patch”，而是 agent 先把 bug 真改进代码里，脚本再从实际 diff 里把这个 bug 提炼成 benchmark 样本。

#### 一个 case 从头到尾经历了什么

```
1. ADF ForEach 分发任务
   └─ 每个 task: { repo, case_index, category?, difficulty? }
   └─ Base64 编码后传给 Batch 节点

2. Batch 节点启动 generate/generate_step01_generate_case.sh
   ├─ 检测 Python、安装 pytest
   ├─ 校验 GitHub Token（fail-fast）
   ├─ 检查 resume（如果 case 已存在就跳过）
   ├─ git clone --depth 1 目标仓库
   └─ 进入重试循环（最多 MAX_RETRIES=3 次）

3. 每次 Attempt 内部：
  ├─ 拼 prompt（generate/default_prompt.md + 仓库上下文 + 前案警示列表）
   ├─ 调 gh copilot agent
   │   ├─ 探索源码 → 选择注入点 → 注入 bug（1-5 行）
   │   ├─ 写测试文件 test_synthetic_N.py
   │   ├─ 验证测试在 buggy 代码上 FAIL
   │   ├─ 还原 → 验证测试 PASS
   │   ├─ 重新注入 bug
   │   ├─ git diff → 根据实际 diff 写 issue_text
   │   └─ 输出 CASE_START { ... metadata ... } CASE_END
   │
   ├─ Host-side 独立验证（不信 agent 自报）
   │   ├─ 测试 FAIL on buggy code ✓
   │   ├─ git apply gold_patch → 测试 PASS ✓
   │   └─ 还原到 buggy state
   │
   ├─ LLM Critic（第二个 gh copilot 调用）
   │   ├─ 检查 issue_text 有没有泄露文件名/函数名
   │   ├─ 检查 issue 和 patch 是否一致
   │   ├─ 检查测试是否确定性
   │   ├─ 检查 patch 是否最小化
   │   └─ 打分 0-6，低于 4 分重试
   │
   └─ 通过所有检查 → break

4. 后处理 (generate/generate_step04_package_case_artifacts.py)
   ├─ 解析 agent 输出中的 CASE_START/CASE_END JSON
   ├─ 创建 buggy code snapshot (tar.gz)
   │   └─ git apply --reverse gold_patch，删 .git
   ├─ 写 JSONL 元数据
  │   └─ 字段定义见下文“Generate Case JSONL 全字段说明”
   └─ 写 metrics JSON

5. 输出文件
   ├─ tar.gz/{instance_id}.tar.gz     ← 注入 bug 后的项目快照
   ├─ jsonl/{instance_id}.jsonl       ← case 完整元数据
   └─ metrics/{instance_id}.metrics.json  ← Copilot CLI 消耗统计
```

#### 关键设计决策

| 决策 | 为什么 |
|------|--------|
| 一个 case = 一个 Batch 节点 | `gh copilot` 是单实例的，不能在一个节点并行跑多个 |
| Host-side FAIL→PASS 验证 | 不信任 agent 的自我报告，独立验证测试结果 |
| LLM Critic | 防止 issue_text 泄露答案、测试不确定性等质量问题 |
| resume skip | 同 repo + case_index 有已存在的 JSONL + tar.gz 就跳过；如果只剩 JSONL 没有配套 tar.gz，会先删掉不完整记录再重跑 |
| `git diff` 后写 issue_text | 强制 agent 根据**实际** diff 写 bug 描述，而不是凭记忆编造 |
| Anti-cheat: 快照不含 .git | 防止评测时模型通过 git log 看到答案 |
| Anti-cheat: issue_text 在 JSONL 里，不在 repo 里 | 防止模型在快照里找到 issues.md |

### 4.2 Audit Pipeline（审计 case）

审计是生成后的质量验证，7 层审计维度：

```
L1  JSON 完整性     — 必填字段是否齐全，枚举值是否合法
L2  快照完整性       — workspace 目录存在、无 .git 泄露、可安装
L3  Patch 可应用性   — gold_patch 能正向/反向 apply
L4  FAIL→PASS 复现   — 在隔离环境里独立验证测试先 FAIL 后 PASS
L5  Patch-Issue 一致性 — patch 修改的文件/符号与 issue 描述相关
L6  标签合理性       — difficulty/localization 与 patch 大小交叉验证
L7  反作弊           — 快照中无答案泄露（无 .git、无 gold_patch 内容）
```

运行方式：
- **单 case**：`bash audit/audit_step01_dispatch_audit.sh <jsonl_blob_path> <mount_root> <audit_level> <output_dir>`
- **批量**：`bash audit/audit_step01_dispatch_audit.sh --batch <input_folder> <output_folder> [level]`
- **Python 直接调**：`python audit/audit_step03_validate_instance.py instance.json --level L4 --json-output result.json`

## 产物与数据结构

### 5.1 输出目录

生成任务的核心输出都落在：

```text
data_processing_demo/owen/generate-case/
├── tar.gz/
├── jsonl/
└── metrics/
```

三类产物分别对应：

- `tar.gz/<instance_id>.tar.gz`：注入 bug 后的仓库快照，用于后续评测
- `jsonl/<instance_id>.jsonl`：case 元数据主文件
- `metrics/<instance_id>.metrics.json`：同一次成功任务的独立 metrics sidecar

### 5.2 Generate Case JSONL 全字段说明

下面这份 `jsonc` 示例按当前 `generate/generate_step04_package_case_artifacts.py` 的真实写法整理，覆盖 generate case JSONL 的全部常见字段。

注意：

- 这段是解释用示例，不是严格 JSON。
- 真正落盘的 `.jsonl` 文件不会带注释。
- `critic_review`、`benchmark` 都是可选字段。

```jsonc
{
  "instance_id": "agoenergy__ptx-boa-synthetic-20260311055306-3", // case 唯一 ID；文件名、tar.gz 名、默认 workspace_dir 都用它
  "repo": "agoenergy/ptx-boa", // owner/name
  "base_commit": "abc123def456...", // 生成时基于的 commit；复现与评测都从这里出发
  "workspace_dir": "agoenergy__ptx-boa-synthetic-20260311055306-3", // tar.gz 解压根目录；默认等于 instance_id
  "source": "synthetic_mutation", // 来源类型；常见值 synthetic_mutation / real_extraction，这条链路固定 synthetic_mutation
  "setup_command": "SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 pip install -e .", // 安装仓库的默认命令
  "test_command": "python -m pytest test_synthetic_3.py -xvs", // 执行测试的默认命令；通常只跑这次生成的 synthetic test 文件
  "issue_text": "...", // 给模型看的问题描述；只写现象，不泄露文件/函数/修法
  "hints_text": "", // 附加提示；当前通常留空
  "patches": {
    "gold_patch": "diff --git a/... b/...\n..." // unified diff；把 buggy code 修回正确代码，也是最终标准答案
  },
  "fail_to_pass": [
    "test_synthetic_3.py::test_some_behavior"
  ], // 这些测试在 buggy snapshot 上必须 FAIL，打上 gold_patch 后必须 PASS
  "pass_to_pass": [], // 这些测试在修复前后都应保持 PASS；当前 synthetic generate 通常留空
  "labels": {
    "category": "Logic & Algorithm", // 大类标签；来自任务配置或 agent 输出
    "sub_type": "off_by_one", // 更细的 mutation 子类型；同时也会同步到顶层 mutation_type
    "difficulty": "L1", // 难度等级；当前常见为 L1-L4
    "localization": "explicit", // 定位难度；explicit / implicit / cross_file / cross_module，默认 explicit
    "context_dependency": "self_contained", // 上下文依赖范围；self_contained / local_context / global_context，默认 self_contained
    "test_modality": "unit_test", // 测试类型；unit_test / integration_test / regression_test / performance_test，默认 unit_test
    "capabilities": ["reasoning", "pytest"], // 能力标签；自由列表，不是强枚举
    "multi_solution": false // 是否明显多解；false 更接近单一标准修复
  },
  "quality": {
    "status": "verified_pending_audit", // 已完成 host-side 验证，但还没跑独立 audit pipeline
    "generation_success": true, // agent 是否成功生成了完整 case 元数据
    "verification_success": true, // host-side FAIL→PASS 验证是否通过
    "critic_success": true, // critic 阶段是否通过；如果 critic_review.verdict=fail，这里会被改成 false
    "audit_success": null, // 独立 audit 结果；在正式跑 audit 前通常为 null
    "test_fail_before_patch": true, // gold_patch 应用前，fail_to_pass 测试确实失败
    "test_pass_after_patch": true // 应用 gold_patch 后，fail_to_pass 测试确实通过
  },
  "created_at": "2026-03-11T05:53:06.123456+00:00", // JSONL 写入时间，UTC ISO 8601
  "mutation_type": "off_by_one", // 顶层镜像字段，当前直接复用 labels.sub_type
  "mutation_description": "Loop upper bound skips the last element.", // 对这次 mutation 的简短说明，给人工排查和审计用
  "mutation_file": "src/foo/bar.py", // 实际被注入 bug 的源码文件路径
  "num_files_changed": 1, // 当前 generate 约束只改一个 source file，所以这里固定为 1
  "num_lines_changed": 4, // 按 gold_patch 中 +/- 行数粗略统计；用于标签和审计，不是 AST 级精确统计
  "critic_review": {
    "verdict": "pass",
    "score": 6,
    "issues": [],
    "summary": "Issue matches patch and no answer leakage found."
  }, // 可选：只有 critic 返回结构化结果时才会写入
  "benchmark": {
    "schema_version": 1, // metrics schema 版本，目前固定为 1
    "collection_method": "copilot_cli_debug_logs", // 采集来源：直接解析 Copilot CLI debug telemetry
    "task_run_id": "agoenergy__ptx-boa-task-20260311055306-3", // 这轮 repo + case_index 任务的唯一 ID
    "instance_id": "agoenergy__ptx-boa-synthetic-20260311055306-3", // 成功生成后的 case ID；失败任务通常没有
    "repo": "agoenergy/ptx-boa", // 仓库名，格式 owner/name
    "case_index": "3", // 这个 repo 下请求生成的 synthetic case 编号
    "model": "claude-opus-4.6", // 本次任务使用的 Copilot 模型
    "max_retries": 3, // 允许的最大重试次数
    "attempts_used": 1, // 实际走到了第几次 attempt
    "successful_attempt": 1, // 第几次 attempt 最终成功；失败任务通常是 null
    "pipeline_success": true, // 这次 generate 任务是否真的产出了最终有效 case
    "failure_reason": null, // 如果任务失败，这里记录最终失败原因
    "generate": {
      "invocations_count": 1, // generate 阶段调了几次 gh copilot
      "model_calls_count": 24, // generate 阶段底层模型调用次数
      "prompt_tokens": 1297309, // generate 阶段输入 token 总数
      "completion_tokens": 16098, // generate 阶段输出 token 总数
      "total_tokens": 1313407, // generate 阶段总 token = 输入 + 输出
      "duration_ms": 281813, // generate 阶段模型侧耗时之和
      "wall_time_ms": 344468, // generate 阶段墙钟耗时，也就是用户真实等待时间
      "telemetry_cost": 69, // telemetry 内部 cost 计数，可做相对比较，但不要当真实账单金额
      "attempts": [
        {
          "schema_version": 1, // 单条 attempt 明细的 schema 版本
          "collection_method": "copilot_cli_debug_logs", // 这条明细也来自 Copilot CLI debug logs
          "invocation_type": "generate", // 这条明细属于哪个阶段：generate / critic / audit
          "attempt": 1, // 第几次重试
          "model": "claude-opus-4.6", // 这次 invocation 实际记录到的模型名
          "log_dir": "/tmp/tmp.91sUtcJ8Cm/copilot_logs/generate_attempt_1", // 这次调用对应的本地 debug 日志目录
          "log_files_count": 1, // 这次解析了多少个日志文件
          "model_calls_count": 24, // 这次 invocation 内部触发的底层模型调用次数
          "prompt_tokens": 1297309, // 这次 invocation 的输入 token
          "completion_tokens": 16098, // 这次 invocation 的输出 token
          "total_tokens": 1313407, // 这次 invocation 的总 token
          "duration_ms": 281813, // 这次 invocation 的模型侧耗时
          "wall_time_ms": 344468, // 这次 invocation 的墙钟耗时
          "telemetry_cost": 69, // 这次 invocation 的 telemetry cost 计数
          "exit_code": 0, // CLI 退出码；0 表示正常结束
          "timed_out": false // 是否超时；当前按退出码 124 判断
        }
      ]
    },
    "critic": {
      "invocations_count": 1,
      "model_calls_count": 1,
      "prompt_tokens": 21067,
      "completion_tokens": 580,
      "total_tokens": 21647,
      "duration_ms": 12559,
      "wall_time_ms": 19566,
      "telemetry_cost": 3,
      "attempts": [] // 结构与 generate.attempts 相同，只是 invocation_type 会变成 critic
    },
    "audit": {
      "invocations_count": 0,
      "model_calls_count": 0,
      "prompt_tokens": 0,
      "completion_tokens": 0,
      "total_tokens": 0,
      "duration_ms": 0,
      "wall_time_ms": 0,
      "telemetry_cost": 0,
      "attempts": [] // audit pipeline 还没正式接入这套 telemetry 时，通常全是 0
    },
    "totals": {
      "invocations_count": 2, // generate + critic 一共多少次 CLI 调用
      "model_calls_count": 25, // 整个任务一共多少次底层模型调用
      "prompt_tokens": 1318376, // 整个任务输入 token 总数
      "completion_tokens": 16678, // 整个任务输出 token 总数
      "total_tokens": 1335054, // 整个任务总 token
      "duration_ms": 294372, // 整个任务模型侧耗时之和
      "wall_time_ms": 364034, // 整个任务墙钟耗时之和
      "telemetry_cost": 72 // 整个任务 telemetry cost 计数之和
    }
  } // 可选：成功 case 才有，内嵌 metrics 汇总
}
```

### 5.3 横向对比推荐指标

| 指标 | 公式 | 含义 |
|------|------|------|
| Success Rate | 成功 case 数 / 总尝试数 | 模型可靠性 |
| Total Tokens | `totals.total_tokens` 求和 | 绝对消耗 |
| Tokens per Success | 全部任务的 total_tokens 总和 / 成功数 | 单位成本 |
| Seconds per Success | 全部任务的 wall_time_ms 总和 / 成功数 | 单位耗时 |

> ⚠️ 后两个指标**用全部任务（含失败）的消耗除以成功数**，防把"成功率低但偶尔中"的模型误判为便宜。

---

## 目录结构与文件速查

### 6.1 后端脚本 `scripts/generate_case/`

```
scripts/generate_case/
├── generate/
│   ├── generate_step01_generate_case.sh   ← 核心：生成一个 case 的完整流程
│   ├── generate_step02_extract_copilot_metrics.py ← 提取 CLI debug log → 单次 invocation metrics
│   ├── generate_step03_aggregate_case_metrics.py ← 汇总多次 attempt → 整个任务的 metrics
│   ├── generate_step04_package_case_artifacts.py ← agent 输出 → tar.gz + jsonl 后处理
│   └── default_prompt.md             ← 给 Copilot CLI 的 mutation 指令模板
├── audit/
│   ├── audit_step01_dispatch_audit.sh      ← 审计入口脚本（单 case + 批量模式）
│   ├── audit_step02_batch_audit_cases.py   ← 批量审计的 Python 封装
│   └── audit_step03_validate_instance.py   ← L1-L7 审计引擎
├── auth/
│   ├── token_server.ps1          ← 本地 token HTTP 代理（开发用）
│   ├── token_server.sh           ← 同上的 Linux 版
│   ├── get_tokens.ps1            ← 一键取 token → 剪贴板
│   └── prompt_generator.html     ← 旧版单体 HTML（已拆分到独立 repo）
├── README.md                     ← 英文版对应文档
└── requirements.txt              ← pytest>=7.0, setuptools
```

### 6.2 前端工程 `bugbash-case-generator/`

独立 repo: `https://github.com/ghcpd/bugbash-case-generator.git`

```
bugbash-case-generator/
├── index.html                    ← 唯一的 HTML 入口
├── vite.config.js                ← Vite 构建配置（base: /caseGenerator/）
├── package.json                  ← Vite 6.2
├── Dockerfile                    ← 多阶段构建：node build → nginx serve
├── nginx.conf                    ← SPA 路由 + 静态资源缓存
├── .dockerignore
├── src/
│   ├── main.js                   ← JS 总入口：import 所有模块 → 挂 window._app
│   ├── constants.js              ← 常量：CATEGORIES, DIFFS, STEPS, RUN_TYPES
│   ├── auth.js                   ← Token 管理：导入/导出/持久化/粘贴 fallback
│   ├── azure.js                  ← Azure REST API 封装（azFetch, blobFetch）
│   ├── generate.js               ← 生成任务：拼 task list → 触发 ADF pipeline
│   ├── monitor.js                ← 监控面板：pipeline runs + activities 轮询
│   ├── batch.js                  ← Batch Explorer：jobs/tasks/files 浏览
│   ├── results.js                ← Blob 结果浏览：列文件 + 预览内容
│   ├── audit.js                  ← 前端审计：L1 快速审计 + 触发 audit pipeline
│   ├── bugbash.js                ← Bug Bash 模式：批量 case 管理 + gen_rubric
│   ├── tasks.js                  ← 任务表单：repo URL + category + difficulty
│   ├── modal.js                  ← 通用弹窗组件
│   ├── persistence.js            ← 表单自动保存/恢复（localStorage）
│   ├── utils.js                  ← 工具函数：toast, clipboard fallback, goStep
│   └── styles/
│       └── main.css              ← 全局样式
└── dist/                         ← 构建产物（不提交 git）
```

### 6.3 ADF Pipeline 定义

位于仓库根目录：

```
Skills/
├── bug_bash_batch_generate_pipeline.json    ← Generate pipeline 定义
├── bug_bash_audit_case_pipeline.json        ← Audit pipeline 定义
└── trigger.py                                ← Python SDK 触发 ADF 的入口
```

---

## 后端脚本逐文件详解

这一节按目录来读，不再按“根目录平铺文件名”来记。

- `generate/`：生成 case 的主链路，从 gh copilot 调用一直到 tar.gz/jsonl 落盘。
- `audit/`：生成后独立审计链路，既支持单 case，也支持批量。
- `auth/`：本地调试时用的 token 获取和 legacy HTML 工具。

### `generate/` 目录

### `generate/generate_step01_generate_case.sh` — 生成的主脚本

**参数**（由 ADF Custom Activity 传入）：

```bash
bash generate/generate_step01_generate_case.sh \
  <task_json>       \  # Base64 编码的 JSON: {"repo":"https://...","case_index":0,"category":"...","difficulty":"..."}
  <github_token>    \  # GitHub Token，给 gh copilot 鉴权
  <prompt_path>     \  # prompt 文件路径（挂载的 Blob 上）
  <output_base>        # 输出根目录（挂载的 Blob 上）
```

**关键变量**：

| 变量 | 默认值 | 含义 |
|------|--------|------|
| `COPILOT_TIMEOUT` | 600 | gh copilot 超时秒数 |
| `COPILOT_MODEL` | claude-sonnet-4.6 | 使用的模型 |
| `MAX_RETRIES` | 3 | 最多重试次数 |
| `VERIFY_TIMEOUT` | 120 | pytest 验证超时 |
| `CRITIC_TIMEOUT` | 120 | LLM critic 超时 |

**内部流程**（伪代码版）：

```python
# 1. 环境准备
parse_task_json(base64_decode(arg1))
find_python_with_pip()
install_pytest()
verify_github_auth()  # fail-fast 如果 token 无效

# 2. Resume 检查
if case_already_exists(jsonl_dir, targz_dir):
    exit(0)  # skip

# 3. 克隆 + 准备
git_clone(repo_url, depth=1)
collect_prev_mutations()  # 防止重复注入同一个文件

# 4. 重试循环
for attempt in range(1, MAX_RETRIES+1):
    reset_repo()
    
    # 4a. 拼 prompt
    prompt = template + repo_context + prev_mutations_hint
    
    # 4b. 调 gh copilot（生成阶段）
    gh_copilot(prompt, log_dir, timeout=COPILOT_TIMEOUT)
    parse_copilot_metrics(log_dir → generate_metrics.json)
    
    # 4c. 从 git diff 提取 gold_patch 和 test_code
    gold_patch = git_diff_reverse()
    test_code = read(test_synthetic_N.py)
    
    # 4d. Host-side FAIL→PASS 验证
    assert pytest(buggy_code) == FAIL
    git_apply(gold_patch)
    assert pytest(fixed_code) == PASS
    git_apply_reverse(gold_patch)
    
    # 4e. LLM Critic
    critic_verdict = gh_copilot(critic_prompt)
    parse_copilot_metrics(log_dir → critic_metrics.json)
    if critic_score < 4:
        continue  # 重试
    
    # 4f. 全部通过
    break

# 5. 汇总 metrics
summarize_case_metrics(all_attempt_metrics → case_metrics.json)

# 6. 后处理
process_cases(agent_output → tar.gz + jsonl)
```

### `generate/generate_step02_extract_copilot_metrics.py` — 提取 CLI 日志指标

从 Copilot CLI 的 debug 日志中提取 telemetry 数据：

```python
# 在日志文件中搜索两种标记：
# 1. "[Telemetry] cli.model_call:" → 每次底层模型调用的 token/耗时
# 2. "[Telemetry] cli.telemetry:" kind=assistant_usage → cost 计数

# 输出单次 invocation 的统计：
{
  "schema_version": 1,
  "collection_method": "copilot_cli_debug_logs",
  "invocation_type": "generate",     // 或 "critic" / "audit"
  "attempt": 1,
  "model": "claude-opus-4.6",
  "model_calls_count": 24,          // 一次 CLI 调用内部触发了 24 次模型请求
  "prompt_tokens": 1297309,
  "completion_tokens": 16098,
  "total_tokens": 1313407,
  "duration_ms": 281813,            // 模型侧耗时之和
  "wall_time_ms": 344468,           // 整次 CLI 调用的墙钟耗时
  "telemetry_cost": 69,
  "exit_code": 0,
  "timed_out": false
}
```

**关键概念**：
- **invocation**：脚本对 `gh copilot` 的一次调用
- **model_call**：一次 invocation 内部可能触发 N 次底层模型请求
- `invocations_count=1` 但 `model_calls_count=24` 是正常的

### `generate/generate_step03_aggregate_case_metrics.py` — 汇总 metrics

把一个 case 所有 attempt 的 generate + critic metrics 汇总成最终报告：

```jsonc
{
  "schema_version": 1,
  "task_run_id": "owner__repo-task-20260311-0",   // 任务唯一 ID
  "instance_id": "owner__repo-synthetic-20260311-0", // case ID（成功时有）
  "repo": "owner/repo",
  "model": "claude-opus-4.6",
  "max_retries": 3,
  "attempts_used": 1,                // 实际用了几次尝试
  "successful_attempt": 1,           // 第几次成功（失败时为 null）
  "pipeline_success": true,
  "generate": { /* 按阶段汇总 */ },
  "critic": { /* 按阶段汇总 */ },
  "audit": { /* 预留，当前为 0 */ },
  "totals": { /* 全部汇总 */ }
}
```

### `generate/generate_step04_package_case_artifacts.py` — 打包 case 产物

AI 输出 → SWE-bench 标准格式：

1. 从 agent 输出中解析 `CASE_START { ... } CASE_END` JSON
2. 没有 CASE_START 标记？从 `git diff + test file` 自动构建 fallback 元数据
3. 创建 buggy code snapshot：
  - `shutil.copytree`（排除 `.git`, `.github`, `__pycache__`, `issues.md`）
   - `git apply --reverse` gold_patch → 变成 buggy 代码
   - 删掉 `.git` 目录（anti-cheat）
   - 写入 test file
4. 打包 `tar.gz`（atomic write: 先写 `.tmp` 再 `os.replace`）
5. 写 `jsonl`（同样 atomic write）

### `audit/` 目录

这一组不是 generate 的后续“子步骤”，而是生成完成后的独立审计链路：

- `audit_step01_dispatch_audit.sh`：审计入口，负责单 case / 批量两种模式分发。
- `audit_step02_batch_audit_cases.py`：遍历 `jsonl/` 和 `tar.gz/`，把批量审计串起来。
- `audit_step03_validate_instance.py`：真正执行 L1-L7 检查。

### `audit/audit_step03_validate_instance.py` — 7 层审计引擎

独立的审计脚本，可以在本地或 Batch 上运行：

```bash
# 审计单个 case
python audit/audit_step03_validate_instance.py instance.json --level L4

# 随机抽样 2 个
python audit/audit_step03_validate_instance.py *.json --sample 2 --seed 42

# 输出 JSON 结果
python audit/audit_step03_validate_instance.py instance.json --json-output audit.json
```

每层的检查逻辑：

| 层级 | 做什么 | 怎么做 |
|------|--------|--------|
| L1 | JSON 校验 | 必填字段 + 枚举值合法性 |
| L2 | 快照校验 | 目录存在 + 无 .git + pyproject.toml/setup.py 存在 + test 文件存在 |
| L3 | Patch 校验 | 在临时目录里 `git apply --check` + 反向 apply 测试 |
| L4 | FAIL→PASS | 临时目录 `pip install -e .` → pytest FAIL → apply patch → pytest PASS |
| L5 | 一致性 | 从 diff 提取文件名/符号名，检查 issue_text 中是否提及 |
| L6 | 标签 | difficulty vs patch 行数交叉验证 |
| L7 | 反作弊 | 快照中搜索 gold_patch 的内容片段，检查是否泄露 |

### `generate/default_prompt.md` — Mutation Prompt 模板

给 Copilot CLI 的指令，定义了：
- 10 个 bug 类别（Logic & Algorithm, Data Handling, API Contract, ...）
- 4 个难度等级（L1 简单 → L4 跨模块）
- 工作流 8 步：探索 → 注入 → 写测试 → 验证 FAIL → 验证 PASS → 重注入 → 看 diff 写 issue → 输出 JSON
- 输出格式：`CASE_START { ... } CASE_END`

### `auth/` 目录

`auth/` 里的东西都属于本地辅助工具，不参与 Batch 上的 generate / audit 主链路：

- `token_server.ps1` / `token_server.sh`：本地起一个 HTTP 服务，给页面自动取 Azure token。
- `get_tokens.ps1`：一键取 token 并复制到剪贴板。
- `prompt_generator.html`：旧版单体 HTML，已经被独立前端 repo 取代，但还保留给本地排查用。

### `auth/token_server.ps1 / .sh` — 本地 Token 服务

在 `localhost:18923` 启动一个 HTTP server，前端页面点 "Fetch Tokens" 时调用：

```
GET /tokens → 执行 az CLI 取 3 个 token → 返回 JSON
GET /health → { "ok": true }
```

三种 token：
- **Management**: `https://management.azure.com` — 调 ADF API
- **Storage**: `https://storage.azure.com` — 读写 Blob
- **Batch**: `https://batch.core.windows.net` — 查 Batch jobs/tasks

还会自动从 ADF Linked Service 探测 Batch endpoint。

### `auth/get_tokens.ps1` — 一键取 token

```powershell
.\scripts\generate_case\auth\get_tokens.ps1
# → 取 3 个 token + Batch endpoint → 复制到剪贴板
# → 打开前端页面 → 点"Import from Clipboard"
```
---

## 前端部署与鉴权

### 8.1 部署架构

前端这部分可以按“Blob 同步源码/构建上下文 -> VM 上 docker compose -> Traefik 暴露路径”来理解。

#### VM 部署结构

```
/mnt/batch/tasks/fsmounts/genaitextdatawu2_code/
  scripts/jianxin/visualization_traefik/
  ├── docker-compose.yml              ← 所有服务定义
  ├── re_start.sh                     ← docker compose down && up --build -d
  ├── traefik/
  │   └── traefik.yml                 ← Traefik 配置（监听 :2382）
  ├── caseGenerator/                  ← 我们的前端项目（从 Blob 同步）
  │   ├── Dockerfile
  │   ├── nginx.conf
  │   ├── package.json
  │   ├── vite.config.js
  │   ├── index.html
  │   └── src/...
  ├── dataVisualization/              ← 其他服务
  ├── bugbashVisualization/
  └── ...
```

#### docker-compose.yml 中的 case_generator 服务

```yaml
case_generator:
  build: ./caseGenerator
  expose:
    - "80"
  labels:
    - traefik.enable=true
    - traefik.http.routers.case_generator.rule=PathPrefix(`/caseGenerator`)
    - traefik.http.services.case_generator.loadbalancer.server.port=80
    - traefik.http.middlewares.case_generator-strip.stripprefix.prefixes=/caseGenerator
    - traefik.http.routers.case_generator.middlewares=case_generator-strip@docker
  networks:
    - proxy
```

#### 请求链路

```
浏览器请求 http://VM:2382/caseGenerator/assets/index-xxx.js
    │
    ▼  Traefik 匹配 PathPrefix(`/caseGenerator`)
    │  剥掉 /caseGenerator 前缀
    ▼
    nginx:80 收到 /assets/index-xxx.js
    │
    ▼  从 dist/ 返回静态文件
```

#### Dockerfile（多阶段构建）

```dockerfile
# Stage 1: node:20-alpine 跑 npm ci + npm run build → dist/
# Stage 2: nginx:alpine 只拷贝 dist/ + nginx.conf → 镜像不含 node/源码
```

#### 如何更新前端

```bash
# 1. 本地改代码 → commit → push
git push

# 2. 重新上传到 Blob（或在 VM 上 git pull）
az storage blob upload-batch \
  --account-name genaitextdatawu2 --destination code \
  --destination-path "scripts/jianxin/visualization_traefik/caseGenerator" \
  --source ./bugbash-case-generator --auth-mode login --overwrite

# 3. SSH 到 VM 重建容器
ssh azureuser@10.249.238.9
cd /mnt/batch/tasks/fsmounts/genaitextdatawu2_code/scripts/jianxin/visualization_traefik
bash re_start.sh
```

### 8.2 Token / 鉴权机制

**前端没有后端服务**，所有 Azure REST API 调用都从浏览器直接发到 Azure。需要 3 种 token：

| Token | Resource | 用途 |
|-------|----------|------|
| Management | `https://management.azure.com` | 调 ADF API：触发/监控 pipeline、查结果 |
| Storage | `https://storage.azure.com` | 读写 Blob Storage：列文件、预览 JSONL |
| Batch | `https://batch.core.windows.net` | 查 Batch jobs/tasks/files、看 stdout/stderr |

#### 获取方式（三选一）

1. **本地 token server**：运行 `auth/token_server.ps1` → 前端点 "Fetch Tokens"
2. **PowerShell 一键**：运行 `auth/get_tokens.ps1` → 剪贴板 → 前端 "Import from Clipboard"
3. **手动**：`az account get-access-token --resource <url>` → 粘贴到 UI

#### Token 有效期

Azure AD token 默认 **1 小时过期**。过期后前端的 API 调用会返回 401，需要重新获取。

#### Storage CORS

第一次使用前需要在 Blob Storage 上开 CORS，前端 Settings 面板里有一键按钮：

```
PUT https://management.azure.com/.../blobServices/default/properties
  → cors: { allowedOrigins: ["*"], allowedMethods: ["GET","HEAD","OPTIONS"], ... }
```

---

## 常见操作手册

### 生成 case

1. 打开前端 → Configure 面板
2. 填写 Azure 配置（Subscription、RG、ADF、Storage Account、Batch Endpoint）
3. 获取 Token（任选一种方式）
4. 添加 repo URL（支持批量粘贴）
5. 选择 Category / Difficulty（可选）
6. 点 "Generate Plan" → 确认 → "Trigger Pipeline"
7. 切到 Monitor 面板观察运行

### 监控运行

- Monitor 面板自动刷新（10s 间隔）
- 点某个 run → 展开 activities → 看 batch task
- 点 task → 看 stdout.txt / stderr.txt

### 查看结果

- Results 面板 → 选择 blob 路径 → 列文件
- 点击 `.jsonl` 预览 case 元数据
- 点击 `.metrics.json` 查看 token 消耗

### 本地运行审计

```bash
cd scripts/generate_case
python audit/audit_step03_validate_instance.py /path/to/instance.json --level L4 --json-output result.json
```

### 手动调脚本时要注意

- 三个 shell 入口脚本目前都按 ADF / Batch 调用方式设计，手动运行时要严格遵守它们头部 `Usage` 里的参数顺序。
- 运行环境至少要先具备 `git`、`gh`、`python3`、`pip`、`curl`，否则第一次失败往往会表现为后续步骤报错，而不是非常友好的 usage 提示。
- `generate/generate_step01_generate_case.sh` 的 repo 名称解析、resume 命名、instance_id 生成都依赖 GitHub URL 解析结果，所以建议优先使用标准的 `https://github.com/owner/repo` 形式。

### 重新部署前端

```bash
cd bugbash-case-generator
# ... 改代码 ...
git add -A && git commit -m "fix: ..." && git push

# 上传到 blob
az storage blob upload-batch \
  --account-name genaitextdatawu2 --destination code \
  --destination-path "scripts/jianxin/visualization_traefik/caseGenerator" \
  --source . --auth-mode login --overwrite

# SSH 到 VM 重建
ssh azureuser@10.249.238.9
cd /mnt/.../visualization_traefik && bash re_start.sh
```

---

## 踩过的坑

### 1. `navigator.clipboard` 在 HTTP 下不可用

**现象**：部署到 VM 后点 "Import from Clipboard" 报 `Cannot read properties of undefined (reading 'readText')`

**原因**：`navigator.clipboard` API 只在 Secure Context (HTTPS/localhost) 下可用

**修复**：`clipboardWrite()` 用 `execCommand('copy')` fallback；`importTokens()` 检测不可用时弹粘贴对话框

### 2. Batch 上 pytest 找不到

**现象**：`ModuleNotFoundError: No module named 'pytest'`

**原因**：Azure Batch 节点的 Python 可能是 externally-managed (PEP 668)，`pip install` 会被拒绝

**修复**：依次尝试 `--user --break-system-packages → --user → 无 flag`，还不行就 `get-pip.py` 强装

### 3. Resume skip 导致 metrics 不更新

**现象**：跑了 pipeline 但 metrics 文件数量没增加

**原因**：resume 逻辑发现已有 JSONL + tar.gz 就直接 exit 0，不会重新调 CLI

**解释**：这是设计行为，不是 bug。需要新 metrics 就删掉旧的 JSONL/tar.gz 重跑；如果只剩 JSONL 没有 tar.gz，脚本会把这条不完整记录删掉后自动重跑

### 4. Agent 没输出 CASE_START/CASE_END

**现象**：`generate/generate_step04_package_case_artifacts.py` 报找不到 case 定义

**修复**：脚本有 fallback 逻辑——从 `git diff` + test file 自动构建元数据，不依赖 agent 的格式化输出

### 5. Blob Storage CORS 问题

**现象**：前端列 blob 文件报 CORS error

**修复**：前端 Settings 面板有 "Enable CORS on Storage" 一键按钮，会通过 Azure Management API 开 CORS

### 6. Vite base path 与 Traefik PathPrefix

**现象**：部署后页面空白，JS/CSS 404

**原因**：Vite 默认 `base: '/'`，但 Traefik 需要 `/caseGenerator/` 前缀

**修复**：`vite.config.js` 设 `base: mode === 'production' ? '/caseGenerator/' : '/'`
