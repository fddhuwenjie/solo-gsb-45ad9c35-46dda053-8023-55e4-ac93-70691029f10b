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

## 确定性保证

- 求解器固定为**单 worker**、固定 `random_seed`，并以**确定性时间预算**
  (`max_deterministic_time`）而非墙钟时间作为主要停止条件；
- 第三阶段把第二阶段的最优解作为完整 hint 回注并取首个解，
  因此相同（输入， seed）在相同依赖版本下必然产生逐孔一致的布局；
- 响应中的 `layout` 可直接作为回归测试基准。

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
  metrics.py   指标独立重算与布局约束校验
  plates.py    板型预设与孔位命名解析
tests/         pytest 测试（解析、求解、API、纠位）
Dockerfile     单容器构建
docker-compose.yml  单命令启动
```
