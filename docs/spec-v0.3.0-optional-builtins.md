# harnessctl v0.3.0 — 成员驱动的内置 gate 与 boundary glob 实现规格

状态:待实现。背景:agent_marketplace 采纳 v0.2.1 时触发两个硬冲突——内置 `spec_registry` 固定模板 spec 契约(`specs/index.json`、数组型 `workflow_classes`),内置 boundary parser 拒绝 `*.pem` 类 glob。二者都不能用 custom gate 绕过,因为 verify 脚本无条件执行同名内置 gate。

## 1. 设计决策(已定)

1. **修引擎,不改写消费方契约**:agent_marketplace 的 Specification 布局和 `.ai-boundaries.yml` 安全规则保持原样。
2. **两个冲突 gate 区别对待**:
   - `spec_registry` 是**方法论 gate**(模板的 spec-first 契约),降为 opt-in;不同 spec 契约的消费方用自己的 custom gate 替代。
   - `ai_boundaries` 是**安全底线**,永不可选、永不可替换——finalize 的可信复算依赖引擎独占 boundary 语义。修法是扩展 pattern 语言(§3)。
3. **统一规则而非逐 gate 开关**:内置 gate 改为由 gate_set 成员驱动执行(listed = run,absent = not run),配一个小的强制核心。不为任何内置 gate 引入行为配置方言;**引擎不把消费方 spec 布局参数化**(拒绝 `spec_index_path` 之类的配置项)。
4. approval 变量分歧(`AI_BOUNDARY_APPROVAL_REF`+active spec vs `AI_BOUNDARY_APPROVAL_EVIDENCE`)仍不在本版处理;消费方继续按命令渐进采纳。

## 2. 契约变更一:成员驱动执行

### 2.1 加载器(`harness_config.py`)

- 新增 `BUILTIN_GATE_ORDER`:单一全序元组,即现有 20 个内置 gate 按 runner 实际执行顺序排列:
  `change_scope, release_context_before, toolchain, symlinks, gofmt, build, vet, golangci, changed_package_tests, test_unit_coverage, govulncheck, gitleaks, ai_boundaries, coverage_threshold, test_race, migration_safety, prompt_evals, spec_registry, benchmarks, release_context_after`
  (已验证:change 与 release 两个 runner 的现有顺序都是它的子序列;`BUILTIN_GATES` 集合从此派生,删除重复定义。)
- 每个 gate_set 的内置成员必须构成 `BUILTIN_GATE_ORDER` 的**子序列**,否则 ConfigError(把原本拖到 summary 阶段才暴露的顺序错误提前到加载期)。
- **强制核心**:每个 gate_set 必含 `change_scope` 和 `ai_boundaries`;被 evidence mode 为 candidate/release 的 profile 引用的 gate_set 还必含 `release_context_before` 和 `release_context_after`。缺失即 ConfigError——安全底线不可配置移除。
- 依赖约束:gate_set 含 `coverage_threshold` 则必含 `test_unit_coverage`。
- 新增子命令 `profile-gates --profile <name>`:按序输出该 profile 的全部 gate 名(内置+custom),runner 用它一次性建 enabled 集合。
- schema 仍为 2:本版所有收紧只拒绝"以前也不可能通过 summary 校验"的配置(runner 过去无条件执行内置 gate,gate_set 缺了它们必然序列不匹配),不存在被破坏的存量;v1 归一化路径不变。

### 2.2 Runner(`verify_runner.sh` + 两个 verify 脚本)

- `runner_init` 调一次 `profile-gates` 建关联数组;新增 `gate_enabled <name>` 谓词。
- 每个**非强制核心**内置 gate 的 `recorded_run` / `recorded_skip` / `seal_artifact` 调用整体包进 `gate_enabled` guard(skip 记录同样受 membership 约束——未列出的 gate 不产生任何 gates.tsv 行,例如 verify_change 里"no changed Go files"批量 skip 只 skip 已启用的 gate)。
- 条件 gate(`test_race`/`benchmarks`)先过 membership 再走 `runner_gate_reason`;未列出则完全不执行、不记 skip。
- custom gate 逻辑不变(v0.2 语义原样保留)。

## 3. 契约变更二:boundary basename glob(`check_ai_boundaries.py`)

只新增一种条目形式,其余照旧拒绝:

- **接受**:不含 `/`、含至少一个 `*`、且含至少一个非 `*` 字符、不以 `/` 结尾的条目(如 `*.pem`、`*secret*`)。语义:`fnmatch.fnmatchcase` 匹配仓库相对路径的**最后一段**(basename),任意目录深度生效。
- **继续拒绝**(报错信息注明支持的形式):`?`、`[]`、同时含 `/` 与 `*` 的条目、纯 `*`、`**`。
- 分类优先级**不变**:现有 `classify_policy` 已是"最严格分类胜"(forbidden > approval_required > allowed),glob 条目作为 `_matches` 的第三种形式自然并入,无需新优先级规则。
- 重复条目检测(`seen_entries`)对 glob 按字面值判重即可。
- finalize 的可信复算与 verify 共用同一 parser,语义自动一致——用测试确认(§5),不默认相信。

## 4. 明确不做(v0.3.0 范围外)

- `ai_boundaries`、`change_scope`、release context 对的可选化或可替换。
- 引擎内配置化消费方 spec 布局(index 路径、workflow map 形态等)。
- 路径级 glob(含 `/` 的 pattern)、`?`/`[]`/`**`。
- approval 变量语义合并(后续 adapter 单独设计)。

## 5. 测试要求

加载器(扩展 `harness_config_test.sh`):

1. 拒绝:gate_set 缺 `change_scope`/`ai_boundaries`;candidate/release profile 的 gate_set 缺 release context 对;内置成员违反 `BUILTIN_GATE_ORDER` 子序列;`coverage_threshold` 无 `test_unit_coverage`。
2. 接受:change set 去掉 `spec_registry`+`toolchain` 等任意可选内置;`profile-gates` 输出与 gate_set 一致。
3. v1 profile(全集)回归:行为与 v0.2.1 完全一致。

集成(Go CLI 全链路,config-only fixture):

4. change profile 无 `spec_registry`、以 custom gate `spec_contract` 替代:通过,gates.tsv 恰好等于 gate_set 序列,无 spec_registry.json 产物,summary passed。
5. release profile 省略 `symlinks`/`migration_safety`/`prompt_evals`/`spec_registry`:通过;省略的 gate 无 skip 记录。
6. verify_change 的"no changed Go files"路径在裁剪后的 gate_set 下只 skip 已启用 gate。
7. boundary glob:改动匹配 `*.pem` 的 forbidden 文件 → 验证失败且分类为 forbidden;`*.key` 在 approval_required 生效;`?`/`[`/带 `/` 的 glob/纯 `*` 被拒绝且报错含支持形式说明。
8. finalize:对无 `spec_registry.json` 的候选证据复验通过;含 glob 规则的策略下可信复算与 verify 分类一致。

## 6. 发布与 agent_marketplace 采纳映射

- engine v0.3.0,tag `v0.3.0`;README 增补成员驱动规则、强制核心、glob 形式说明。
- agent_marketplace 侧(消费方配置,不动其契约本体):
  - gate_sets/evidence_sets/machine_status_artifacts 中删除 `spec_registry`(引擎证据层对缺失的 `spec_registry.json` 本就优雅降级,已核实 `_load_active_specs` 与 machine status 的 skip-if-missing 行为);
  - 自家 spec 契约(docs/specifications + 对象型 workflow map)与 workflow validator 落为 custom gate;
  - `.ai-boundaries.yml` 的 `*.pem`/`*.key` 原样保留;
  - approval 仍走本地流程。
