# harnessctl v0.2.0 — Custom Gates（profile schema v2）实现规格

状态：实现完成，目标版本 v0.2.0。背景：agent_marketplace 采纳 v0.1.0 时触发停止条件——其 profile 含引擎无法表达的项目专属 gate（openapi_hash、cache_runtime_self_test、legacy_imports、dependency_boundaries、codex_config、harness_workflows）。

## 1. 设计决策（已定，不再讨论）

1. 引擎只拥有循环机制（change scope、gate 编排、日志/证据/封印、版本锁、approval）；检查本身可插拔。
2. 扩展点只有一种：**command gate**——消费方在 profile 里声明 `gate 名 + 仓库内脚本路径`，引擎用与内置 gate 完全相同的 `recorded_run` 语义执行。
3. **不引入 DSL**：profile 里不出现条件、模板、表达式。gate 内部的条件逻辑写在消费方脚本里（没事可查就打印原因并 exit 0）。
4. 晋升规则：某 custom gate 只有当 ≥2 个消费方实现本质相同的检查时才提炼为内置 gate。
5. approval 语义分歧不在本版处理；消费方可按命令渐进采纳（先 verify 后 approval）。

## 2. 契约变更一：profile schema v2

### 2.1 新增顶层键 `custom_gates`

```json
"schema_version": 2,
"custom_gates": {
  "openapi_hash":      { "run": "scripts/gates/check_openapi_hash.sh" },
  "harness_workflows": { "run": "scripts/check_harness_workflows.sh" }
}
```

校验规则（`harness_config.py::load_policy`，全部 fail-closed，违反即 ConfigError）：

- v2 的必需键集合 = v1 全部键 ∪ `{custom_gates, symlinks}`，仍是**封闭集合**（多键少键都拒绝）。`custom_gates`、`symlinks` 允许为空。
- gate 名：`^[a-z][a-z0-9_]{1,31}$`，不得与 `BUILTIN_GATES` 冲突。
- 每个 value 是封闭对象 `{"run": <str>}`。产物声明复用现有 `gate_artifacts`（按 gate 名挂），不新增字段。
- `run`：POSIX 相对路径，过 `normalize_relative`，不得含 TSV 行协议分隔符（TAB、CR、LF），且**必须位于 `scripts/` 或 `harness/` 之下**——这两个目录已被 `_verifier_inputs` 整体记录 sha256，脚本防篡改因此免费获得。绝对路径、`..`、其他目录一律拒绝。
- `gate_sets` 成员校验（v1 目前不校验，v2 收紧）：每个条目必须 ∈ `BUILTIN_GATES` ∪ `custom_gates` 键集；每个 `custom_gates` 键必须出现在至少一个 gate_set（拒绝死配置）。
- 顺序约束：每个 gate_set 内 custom gate 必须构成**连续尾段**；若存在 `release_context_after`，它必须是唯一排在 custom 段之后的元素。即顺序为 `内置…, custom…, [release_context_after]`。
- `skippable_gates` 不得包含 custom gate；`conditional_gates` 键集不变（仍精确等于 `{test_race, benchmarks}`），不接受 custom 名。

### 2.2 `BUILTIN_GATES` 常量

在 `harness_config.py` 定义（来源：三个 verify 脚本实际产出的 gate 名，共 20 个）：

```
change_scope, toolchain, symlinks, gofmt, build, vet, golangci,
changed_package_tests, test_unit_coverage, govulncheck, gitleaks,
ai_boundaries, coverage_threshold, test_race, migration_safety,
prompt_evals, spec_registry, benchmarks,
release_context_before, release_context_after
```

### 2.3 v1 兼容

引擎同时接受 `schema_version` 1 和 2。v1 在加载后归一化为 v2：`custom_gates = {}`，`symlinks` = 现行硬编码的 4 对模板符号链接（见 §4），行为完全不变。已发布的 ai-first-go-template 消费方零改动。

## 3. 契约变更二：custom gate 执行

### 3.1 运行时契约（写进 README，属对外契约）

- cwd = 消费方仓库根；stdout+stderr 合并写入 `logs/<name>.log`；exit 0 = passed，非 0 = failed 且整轮验证立即中止（与内置 gate 的 `set -e` 行为一致）。
- 环境变量：`HARNESS_PROJECT_ROOT`、`HARNESS_ARTIFACT_DIR`、`HARNESS_SNAPSHOT_FILE`、`HARNESS_SNAPSHOT_SHA256`、`HARNESS_COMPARE_SHA`、`HARNESS_HEAD_SHA`、`HARNESS_PROFILE`、`HARNESS_EVIDENCE_MODE`。**不**暴露 `HARNESS_ENGINE_DIR`——消费方不得依赖引擎内部文件。
- gate 不得改动工作树（release 档的 `release_context_after` 会兜底抓获）。
- 产物：脚本写到 `$HARNESS_ARTIFACT_DIR/<gate_artifacts 声明的相对路径>`；gate passed 后由 runner 统一封印。
- 执行前校验：脚本必须是 regular、非符号链接、可执行文件，否则该 gate 直接 failed（原因写入日志）。

### 3.2 执行位置

每个 verify 脚本各加一处 `run_custom_gates` 调用，与 §2.1 顺序约束一一对应：

- `verify_change.sh`：`spec_registry` 之后、`runner_complete` 之前。
- `verify_release.sh`（candidate 复用同一脚本）：benchmarks 封印块之后、`release_context_after` 之前。

custom gate 串行执行（v0.2.0 不并行；需要时再加，避免无谓复杂度）。

### 3.3 实现落点

- `harness_config.py` 新增两个子命令：
  - `custom-gates --profile <name>`：按 gate_set 顺序输出 custom gate 的 `name\trun` 行；
  - `gate-artifacts --gate <name>`：输出该 gate 在 `gate_artifacts` 里声明的产物相对路径（内置/custom 通用）。
- `verify_runner.sh` 新增 `run_custom_gates`：遍历上述 TSV → 校验脚本 → `recorded_run "$name" env HARNESS_…=… "$ROOT_DIR/$run"` → passed 则对 `gate-artifacts` 输出逐个 `seal_artifact`。
- `write_release_summary.py` / `finalize_approval.py` / `evidence.py` **无需改动**：`validate_summary_gates` 按 `profile_gates` 序列泛化比较，`required_evidence` 已按 `gate_artifacts` 泛化并入封印集合。这是本设计成立的关键验证点，实现时用测试确认而不是默认相信。
- `cli.go` 无改动（profile 发现、lock 校验、引擎提取均不变）。

## 4. 契约变更三：`symlinks` gate 配置化（顺带修复引擎杂质）

现状：`verify_release.sh::check_symlinks` 把 `internal/risk/CLAUDE.md → AGENTS.md` 等 4 对模板项目路径硬编码在引擎里，违反 AGENTS.md"引擎不含消费方策略"，且对任何目录结构不同的消费方（含 agent_marketplace）必然失败。

- schema v2 新增顶层键 `symlinks`：`[{"link": "CLAUDE.md", "target": "AGENTS.md"}, …]`，link 与 target 均过 `normalize_relative`，且不得含 TSV 行协议分隔符（TAB、CR、LF）。`gate_artifacts` 路径适用相同限制。
- `harness_config.py` 新增子命令 `symlinks`，输出 `link\ttarget` 行；`check_symlinks` 改为遍历该输出。空列表时 gate 平凡通过并在日志注明。
- v1 归一化注入现行 4 对，模板消费方行为不变。

## 5. 测试要求（AGENTS.md：行为先有测试；消费方 fixture 只含配置不含引擎源码）

加载器（扩展现有 checker 自测套件）：

1. v1 profile 原样通过；v2 含 custom_gates/symlinks 通过。
2. 拒绝：与内置名冲突、gate_sets 出现未知名、custom gate 未被任何 gate_set 引用、custom gate 出现在 skippable_gates 或 conditional_gates、`run` 为绝对路径/含 `..`/不在 scripts|harness 下、custom 段不连续或位于 release_context_after 之后、v2 缺键或多键。

集成（新增 config-only 消费方 fixture，经 Go CLI 全链路跑）：

3. 含一个 passing custom gate 的 `verify change`：gates.tsv 顺序与 gate_set 一致、日志与 sha 进 manifest、声明的产物被封印、summary overall=passed。
4. failing custom gate：整轮中止、evidence 记 failed、退出码非 0。
5. 脚本缺失/不可执行/是符号链接：gate failed 而非引擎崩溃。
6. custom gate 存在时 `approval finalize` 对 pull_request 证据的复验通过（验证 §3.3 的"无需改动"论断）。
7. symlinks 配置化：自定义 link/target 通过；v1 fixture 仍按旧 4 对校验。

## 6. 发布

- `version.go`/构建注入版本 0.2.0，语义化 tag `v0.2.0`。
- README：custom gate 契约（§3.1）+ schema v2 迁移说明（v1→v2：加两个键、schema_version 改 2、lock 版本改 0.2.0）。
- agent_marketplace 采纳映射：六个专属 gate + workflow validator（binding/机械预算/净行数预算走 `harness_workflows` custom gate，脚本留在其仓库）全部落 `custom_gates`；approval 暂留其本地流程，迁移另行设计。

## 7. 明确不做（v0.2.0 范围外）

- custom gate 的条件触发（path_prefixes）与并行执行——等真实消费方需要再加。
- custom gate 进入 skippable_gates。
- approval/finalizer 语义合并。
- profile 里任何形式的表达式、继承、模板。
