# 微孔板样本编排 API (Microplate Sample Arrangement API)

基于 **Python + FastAPI + OR-Tools (CP-SAT)** 的微孔板样本布局优化服务。

提交板型、样本（含实验组与批次）、固定孔位、禁用孔位与 seed 后，服务返回一份
**字典序最优**的布局：

1. **第一级（组间平衡）**：最小化各实验组在行、列方向上的最大数量差
   （每组分别统计其在每一行/列的样本数，取行极差与列极差中的较大者，
   再对所有组取最大，记为 `D`；空行/空列按 0 计入）。
2. **第二级（批次聚集）**：在 `D` 最优的前提下，最小化同批次样本
   正交相邻（上下左右，不含对角）的孔位对数，记为 `A`。
3. **确定性决胜**：在前两级最优解集合中，以 seed 控制的确定性搜索
   固定唯一布局 —— **相同输入与相同 seed 必然得到完全相同的布局**。

## 快速启动（单条命令）

```bash
docker compose up --build
```

服务启动后：

- API 文档（Swagger UI）: http://localhost:8000/docs
- 健康检查： http://localhost:8000/health
- 编排接口： `POST http://localhost:8000/api/v1/arrangements`
- 纠位接口： `POST http://localhost:8000/api/v1/arrangements/reconcile`
- 空间质控接口： `POST http://localhost:8000/api/v1/arrangements/spatial-qc`

不使用 Docker 时（Python 3.11+):

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 请求示例

```bash
curl -X POST http://localhost:8000/api/v1/arrangements \
  -H "Content-Type: application/json" \
  -d '{
    "plate": {"type": "96"},
    "samples": [
      {"id": "S01", "group": "treated", "batch": "B1"},
      {"id": "S02", "group": "treated", "batch": "B1"},
      {"id": "S03", "group": "control", "batch": "B2"},
      {"id": "S04", "group": "control", "batch": "B2"},
      {"id": "S05", "group": "control", "batch": "B1"}
    ],
    "fixed": [{"sample_id": "S01", "well": "C5"}],
    "blocked_wells": ["A1", "H12"],
    "seed": 42
  }'
```

### 请求字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `plate.type` | string | 板型：`"24"`(4×6)、`"48"`(6×8)、`"96"`(8×12)、`"384"`(16×24) 或 `"custom"`（需同时给 `rows`/`columns`，行列乘积 ≤ 400) |
| `samples[]` | array | 样本列表，每项 `{id, group, batch}`;`id` 必须唯一，`group`/`batch` 必填 |
| `fixed[]` | array | 固定孔位 `{sample_id, well}`，求解器不得移动；固定孔与禁用孔冲突、多样本固定同一孔都会判为无解 |
| `blocked_wells[]` | array | 禁用孔位（如 `"A1"`)，不可放置任何样本 |
| `seed` | int | 随机种子（≥0)，控制确定性决胜与求解搜索 |

孔位命名：行字母 + 列数字（`A1`…`H12` 等），大小写不敏感。

### 响应（200)

```json
{
  "seed": 42,
  "plate": {"type": "96", "rows": 8, "columns": 12,
            "blocked_wells": ["A1", "H12"], "available_wells": ["A2", "..."]},
  "layout": {"S01": "C5", "S02": "D3", "...": "..."},
  "metrics": {
    "level1_group_balance": {
      "global_max_spread": 1,
      "groups": [{"group": "control", "row_counts": [0,1,1,0,1,0,0,0],
                  "row_spread": 1, "col_spread": 1, "...": "..."}]
    },
    "level2_batch_adjacency": {
      "same_batch_adjacent_pairs_count": 0,
      "same_batch_adjacent_pairs": [],
      "total_orthogonal_edges": 172
    }
  },
  "validation": {
    "all_passed": true,
    "checks": {
      "one_well_per_sample": {"passed": true},
      "blocked_wells_respected": {"passed": true},
      "fixed_assignments_respected": {"passed": true},
      "wells_in_range": {"passed": true},
      "adjacency_metric_consistent": {"passed": true}
    }
  },
  "solver": {
    "status": "OPTIMAL",
    "stages": [{"stage": "1_balance", "objective_value": 1, "status": "OPTIMAL"},
               {"stage": "2_adjacency", "objective_value": 0, "status": "OPTIMAL"},
               {"stage": "3_tiebreak", "status": "OPTIMAL"}]
  }
}
```

- `layout`：孔位映射（样本 id → 孔位）。
- `metrics.level1_group_balance`：第一级指标（每组行/列计数与极差、全局最大极差 `D`)。
- `metrics.level2_batch_adjacency`：第二级指标（同批次正交相邻对数 `A` 及具体孔位对）。
- `validation`：对返回布局的独立约束校验（每样本恰好一孔、禁用孔未被占用、
  固定孔未被改写、孔位在板内、邻接指标一致）。
- `solver.status`:`OPTIMAL` 表示两级目标均已证明最优；`FEASIBLE` 表示
  在确定性时间预算内返回了当前最优可行解（大实例可能出现，见下文配置）。

### 无解（422)

无解时**不返回任何部分布局**，而是返回导致不可行的约束类别：

```json
{
  "detail": "No feasible layout exists for the submitted constraints.",
  "infeasibility_categories": ["CAPACITY_EXCEEDED"],
  "messages": ["25 samples but only 24 available wells (24 total, 0 blocked)"]
}
```

可能的类别：

| 类别 | 含义 |
| --- | --- |
| `SAMPLE_IDS_DUPLICATED` | 样本 id 重复 |
| `GROUP_BATCH_INVALID` | 样本缺少实验组或批次 |
| `PLATE_DIMENSION_INVALID` | 板型尺寸非法（自定义缺尺寸/超上限/与预设不符） |
| `WELL_REFERENCE_INVALID` | 孔位名称非法、越界，或固定孔引用了不存在的样本 |
| `FIXED_CONFLICT` | 同一样本被固定到多个孔 / 多个样本固定到同一孔 |
| `FIXED_ON_BLOCKED` | 固定孔落在禁用孔上 |
| `CAPACITY_EXCEEDED` | 样本数超过可用孔数 |
| `SOLVER_INFEASIBLE` | 求解器证明约束组合不可满足 |
| `REQUEST_VALIDATION_ERROR` | 请求体结构不合法（字段缺失/类型错误等） |

请求体结构错误同样返回 422（类别 `REQUEST_VALIDATION_ERROR`)。

## 扫描纠位接口 `POST /api/v1/arrangements/reconcile`

把编排接口返回的 `layout` 作为目标映射，与整板扫描结果核对，并生成
**最少步数**的逐孔纠位指令。每步只把一份样本移入**当前为空**的可用孔
（绝不直接交换两个非空孔）。

```bash
curl -X POST http://localhost:8000/api/v1/arrangements/reconcile \
  -H "Content-Type: application/json" \
  -d '{
    "plate": {"type": "24"},
    "target": {"S1": "A1", "S2": "A2", "S3": "A3", "S4": "B1"},
    "scan":   {"A1": "S2", "A2": "S1", "A3": "S3", "C2": "S4"},
    "blocked_wells": ["D6"],
    "staging_well": "D5"
  }'
```

### 请求字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `plate` | object | 板型，与编排接口相同（默认 `"96"`) |
| `target` | object | 目标映射：样本 id → 目标孔位，即编排接口响应中的 `layout` |
| `scan` | object | 完整扫描得到的孔位 → 样本 id 映射，**空孔不列出** |
| `blocked_wells[]` | array | 禁用孔位，不可放置任何样本 |
| `staging_well` | string | 可选。空闲、非目标、非禁用的暂存孔，用于解开闭环错位；结束时恢复为空 |

### 核对（任一不满足即 422，不生成任何搬移指令）

目标孔位重复（`TARGET_WELLS_DUPLICATED`)、孔位名称非法或越界
（`WELL_REFERENCE_INVALID`)、目标孔落在禁用孔上（`TARGET_ON_BLOCKED`)、
扫描孔位重复（`SCAN_WELLS_DUPLICATED`，如 `"a1"` 与 `"A1"`)、扫描样本重复
（`SCAN_SAMPLES_DUPLICATED`)、计划样本缺失（`SAMPLES_MISSING`)、计划外样本
（`UNPLANNED_SAMPLES`)、禁用孔被占用（`BLOCKED_WELL_OCCUPIED`)。
响应在 `infeasibility_categories` 与 `messages` 中列出全部问题。

### 响应（200)

```json
{
  "misplaced_wells": [
    {"well": "A1", "expected": "S1", "actual": "S2"},
    {"well": "A2", "expected": "S2", "actual": "S1"},
    {"well": "B1", "expected": "S4", "actual": null},
    {"well": "C2", "expected": null, "actual": "S4"}
  ],
  "move_count": 4,
  "cycles": [["A1", "A2"]],
  "staging_well": "D5",
  "instructions": [
    {"step": 1, "sample_id": "S4", "from": "C2", "to": "B1"},
    {"step": 2, "sample_id": "S2", "from": "A1", "to": "D5"},
    {"step": 3, "sample_id": "S1", "from": "A2", "to": "A1"},
    {"step": 4, "sample_id": "S2", "from": "D5", "to": "A2"}
  ],
  "final_mapping": {"S1": "A1", "S2": "A2", "S3": "A3", "S4": "B1"},
  "verification": {
    "final_matches_target": true,
    "every_destination_empty_at_move": true,
    "staging_well_restored_empty": true
  }
}
```

- `misplaced_wells`：错孔清单（`expected`/`actual` 为 `null` 表示该孔应为空/当前为空）。
- `instructions`：确定性的最少步数指令。错位分解为**空孔链**与**闭环**:
  空孔链先行，链上每份样本直接移入已腾空的最终目标孔（k 份样本 k 步）;
  每个闭环借用 `staging_well` 暂存一份样本后逐个归位，最后把暂存样本移回
  目标孔（k 份样本 k+1 步，暂存孔恢复为空）。总步数
  `错位样本数 + 闭环数` 为理论最小值；相同输入必然得到相同指令序列。
- `final_mapping`：指令重放后的最终映射（样本 id → 孔位），与 `target` 一致。
- `verification`：对指令序列的独立重放校验（终态与目标一致、每步目的孔
  均为空、暂存孔最终恢复为空）。

### 闭环缺暂存孔（409)

存在闭环错位但未提供可用暂存孔（未提供、被占用、是目标孔、被禁用或非法）
时返回 409，列出全部闭环，**不输出部分指令**:

```json
{
  "detail": "Closed-loop misplacements require a usable staging well; no move instructions were generated.",
  "cycles": [["A1", "A2"]],
  "staging_well": null,
  "problems": ["no staging well provided"]
}
```

无闭环时 `staging_well` 可省略（即使提供也不使用）。

## 空间质控接口 `POST /api/v1/arrangements/spatial-qc`

对已完成编排的板做空间效应质控：先按**实验组中位数中心化**读数（避免组间
基线差异混入空间效应），再用**行列中位数交替剥离（median polish)**
累计总体、逐行、逐列效应，并以 seed 控制的**组内置换**给出最大行列效应的
**加一修正经验 p 值**。读数允许缺失（孔位省略或显式 `null`)。

端口 `8000` 调用示例：

```bash
curl -X POST http://localhost:8000/api/v1/arrangements/spatial-qc \
  -H "Content-Type: application/json" \
  -d '{
    "plate": {"type": "24"},
    "layout": {
      "S1": "A1", "S2": "A2", "S3": "B1", "S4": "B2",
      "S5": "C1", "S6": "C2", "S7": "D1", "S8": "D2"
    },
    "samples": [
      {"id": "S1", "group": "treated"}, {"id": "S2", "group": "control"},
      {"id": "S3", "group": "treated"}, {"id": "S4", "group": "control"},
      {"id": "S5", "group": "treated"}, {"id": "S6", "group": "control"},
      {"id": "S7", "group": "treated"}, {"id": "S8", "group": "control"}
    ],
    "readings": {
      "A1": 1.05, "A2": 0.98, "B1": 1.02, "B2": 1.01,
      "C1": 1.04, "C2": 0.99, "D1": 1.03,
      "D2": null
    },
    "seed": 42,
    "permutations": 1000,
    "tolerance": 0.000001,
    "max_iterations": 1000
  }'
```

### 请求字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `plate` | object | 板型，与编排接口相同（默认 `"96"`) |
| `layout` | object | 布局映射：样本 id → 孔位（编排接口响应中的 `layout`) |
| `samples[]` | array | 样本列表，每项 `{id, group}`;`id` 必须与 `layout` 一一对应 |
| `readings` | object | 孔位 → 数值读数；**孔位省略或显式 `null` 表示读数缺失**;`NaN`/`Infinity` 不允许 |
| `seed` | int | 随机种子（≥0)，控制组内置换；相同输入与 seed 结果完全一致 |
| `permutations` | int | 组内置换次数，默认 `1000`，范围 0–1,000,000;`0` 表示跳过置换（`p_value` 为 `null`) |
| `tolerance` | float | 收敛阈值（>0)，默认 `1e-6`；本轮最大行列校正量 ≤ tolerance 即收敛 |
| `max_iterations` | int | 行列交替剥离的最大轮数，默认 `1000`(≥1)；未达 tolerance 即返回 422 |

### 算法

1. **组中位数中心化**：每个有效读数减去其所在 `group` 的读数中位数。
2. **行列中位数交替剥离**：每轮先逐行减去行内（非缺失）中位数并累加到行
   效应，再逐列减去列内中位数并累加到列效应；记录本轮行、列及最大校正量。
   当本轮**最大校正量 ≤ `tolerance`** 时收敛，否则继续，最多 `max_iterations`
   轮。收敛后将行、列效应中位数依次归零（偏移量最终并入总体效应），使效应
   分解与剥离顺序无关。
3. **校正读数**：`校正读数 = 组中位数 + 残差 = 原读数 −（总体 + 行 + 列）`,
   即剥离拟合空间面但保留组基线；缺失孔不出现在输出中。
4. **置换检验**：在**每个 group 内部**，以 seed 驱动的确定性随机数把有效
   读数在该组有效孔位间重新分配（缺失孔位置与每组有效孔数保持不变），
   对每次置换重复同一校正流程；统计量为**全部行、列效应绝对值的最大值**。
   经验 p 值采用加一修正：`p = (1 + #{T_perm ≥ T_obs}) / (1 + permutations)`,
   因此取值范围恒为 `(0, 1]`，永远不会为 0。

### 响应（200)

```json
{
  "seed": 42,
  "plate": {"type": "24", "rows": 4, "columns": 6},
  "parameters": {"permutations": 1000, "tolerance": 1e-6, "max_iterations": 1000},
  "groups": [
    {"group": "control", "valid_readings": 3, "valid_rows": 3,
     "valid_columns": 2, "median": 0.99},
    {"group": "treated", "valid_readings": 4, "valid_rows": 4,
     "valid_columns": 2, "median": 1.035}
  ],
  "effects": {
    "overall": 0.0,
    "row_effects": [0.0, 0.0, 0.0, 0.0],
    "col_effects": [0.02, -0.02, 0.0, 0.0, 0.0, 0.0],
    "max_abs_row_col_effect": 0.02
  },
  "readings": [
    {"well": "A1", "sample_id": "S1", "group": "treated",
     "reading": 1.05, "centred_reading": 0.015,
     "corrected_reading": 1.015}
  ],
  "convergence": {
    "converged": true,
    "iterations": 2,
    "tolerance": 1e-6,
    "trajectory": [
      {"iteration": 1, "row_correction": 0.02, "col_correction": 0.02,
       "max_correction": 0.02}
    ]
  },
  "permutation_test": {
    "permutations": 1000,
    "observed_statistic": 0.02,
    "exceedances": 24,
    "p_value": 0.024975,
    "method": "within-group well permutation; p=(1+#{T_perm>=T_obs})/(1+n)"
  }
}
```

- `groups`：各组有效读数数、覆盖的行数/列数与组中位数（中心化基准）。
- `effects`：总体效应、逐行/逐列效应（长度等于板行数/列数）及统计量
  `max_abs_row_col_effect`。
- `readings`：逐孔给出原始读数、组内中心化读数与校正读数（按行优先顺序，
  仅含有效孔）。
- `convergence.trajectory`：逐轮行校正量、列校正量与最大校正量。
- `permutation_test`：观测统计量、达到或超过它的置换次数与加一修正 p 值；
  `permutations = 0` 时 `p_value` 为 `null`。

### 不可分析（422，不输出部分结论）

满足以下任一条件即返回 422，**不返回任何效应、校正读数或 p 值**，响应在
`infeasibility_categories` 与 `messages` 中列出全部问题：

| 类别 | 含义 |
| --- | --- |
| `SPATIAL_GROUP_INSUFFICIENT` | 任一组有效读数少于 2 |
| `SPATIAL_GRID_INSUFFICIENT` | 有效孔未覆盖至少 2 个不同行或 2 个不同列 |
| `SPATIAL_NOT_CONVERGED` | 观测数据（或任一次置换重复）在 `max_iterations` 内未收敛 |
| `SPATIAL_SAMPLE_DUPLICATED` | `samples` 中样本 id 重复 |
| `SPATIAL_SAMPLE_UNKNOWN` | `layout` 引用了 `samples` 中不存在的样本 |
| `SPATIAL_LAYOUT_CONFLICT` | 多样本占用同一孔，或样本未在 `layout` 中安排孔位 |
| `SPATIAL_READING_INVALID` | 读数对应孔位在 `layout` 中无人占用 |
| `SPATIAL_READING_DUPLICATED` | 同一孔位（大小写不敏感）出现多个读数 |
| `WELL_REFERENCE_INVALID` | 孔位名称非法或越界 |
| `PLATE_DIMENSION_INVALID` | 板型尺寸非法 |
| `REQUEST_VALIDATION_ERROR` | 请求体结构不合法（含 `NaN`/`Infinity` 读数等） |

## 确定性保证

- 求解器固定为**单 worker**、固定 `random_seed`，并以**确定性时间预算**
  (`max_deterministic_time`）而非墙钟时间作为主要停止条件；
- 第三阶段把第二阶段的最优解作为完整 hint 回注并取首个解，
  因此相同（输入， seed）在相同依赖版本下必然产生逐孔一致的布局；
- 响应中的 `layout` 可直接作为回归测试基准；
- 空间质控接口的组内置换使用 seed 初始化的独立确定性随机流，相同
  （输入， seed)（含 `permutations`/`tolerance`/`max_iterations`)
  必然得到逐位一致的效应、校正读数、收敛轨迹与 p 值。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CP_NUM_WORKERS` | `1` | CP-SAT 搜索线程数（>1 会破坏确定性，不建议修改） |
| `CP_DETERMINISTIC_TIME_SECONDS` | `4` | 每个优化阶段的确定性时间预算 |
| `CP_WALL_TIME_SECONDS` | `12` | 每个优化阶段的墙钟时间硬上限（安全阀） |
| `CP_FAST_DETERMINISTIC_TIME_SECONDS` | `1` | 快速保底/决胜阶段的确定性预算 |
| `CP_FAST_WALL_TIME_SECONDS` | `8` | 快速保底/决胜阶段的墙钟上限 |

预算耗尽时返回当前最优可行解（`solver.status = FEASIBLE`)，两级指标仍是
字典序意义下预算内最优；找不到任何可行解时返回 503（不返回部分布局）。

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests -q
```

## 项目结构

```
app/
  main.py      FastAPI 应用与异常处理（200/409/422/503)
  schemas.py   请求/响应模型与不可行类别常量
  planner.py   三阶段字典序 CP-SAT 求解（平衡 → 邻接 → 确定性决胜）
  reconciler.py 扫描核对与最少步数纠位指令生成（空孔链 → 闭环暂存）
  spatial_qc.py 组中位数中心化 + 行列中位数剥离（median polish）与组内置换检验
  metrics.py   指标独立重算与布局约束校验
  plates.py    板型预设与孔位命名解析
tests/         pytest 测试（解析、求解、API、纠位、空间质控）
Dockerfile     单容器构建
docker-compose.yml  单命令启动
```
