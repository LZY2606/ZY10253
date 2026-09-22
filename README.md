# 移液执行证言台（Pipette Execution Testimony Stand）

用离散事件模拟器重放自动移液执行：接收固定 deck 与实验协议，产生拾取吸头、吸液、
排液、模块移动、退吸头与故障事件；页面展示 deck 布局、操作时线、每孔成分谱系与当前
模拟时钟，支持单步执行、注入三类故障，并从最后一个可提交检查点恢复（恢复走新分支，
绝不覆盖原失败运行）。

## 安装与演示

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 5593
```

浏览器访问 <http://127.0.0.1:5593>，页面标题为“移液执行证言台”。

- 「新建运行」后反复点「单步执行」即可重放全部微操作。
- 选择故障类型后点「注入故障」，再单步触发；失败后点「从检查点恢复（新分支）」。
- 「重放当前步」用事件的 `replay_key` 重放同一事件，结果幂等。
- 「导出 JSON / 清空数据库 / 导入复核」完成运行记录的导出与清空后重新核对。

## 数据口径（为什么容量不会凭空增减）

- 所有容量、时钟、成分分量都用标准库 `fractions.Fraction` 表示，序列化文本为
  `整数` 或 `分子/分母`（例如 `75`、`1/3`）；`lab/state.py:frac` 明确拒绝 `float`。
- 页面上的小数只是 `Fraction` 的展示形式；NumPy 成分矩阵（`lab/engine.py:composition_matrix`）
  仅用于可视化和守恒交叉核对，从不回写账本。
- 按比例吸液时尾差并入最后一种成分（`lab/engine.py:_split_proportional`），
  分数级恒等成立；每个运行的 `conservation.exact_delta` 必须为 `0`。
- 微操作耗时为有理数秒：移动 3、拾取 2、吸/排 4、退出 2；故障检测 2 秒但不改变物理状态。
  完整 7 步协议干净重放结束时钟为 `165 s`。

## 执行模型与提交点

复合步骤被展开为确定的微操作序列（`lab/fixtures.py:step_ops`）：

```
move_head(吸头架) → pick_tip → move_head(源) → aspirate
→ move_head(目标) → dispense(排液成功 = 复合提交点) → move_head(弃吸头箱) → eject_tip
```

- 一个复合转移只有在液体、吸头、运动全部成功（排液成功）后才提交。
- 每个微操作都把**事件 + 物理检查点**在同一 SQLite 事务内落盘（`lab/store.py:_persist_event`），
  事件保留 `before_json` / `after_json` 状态摘要。
- 中途任何失败都把运行显式置为 `failed`（待恢复），而不是“只回滚数据库”——
  尤其吸液成功后排液前失败时，检查点里的吸头仍真实持有液体。

### 三类故障

| 故障 | 注入微操作 | 物理后果 | 恢复起点 |
| --- | --- | --- | --- |
| `tip_not_ready` | `pick_tip` | 未拾取吸头 | 该复合步骤起点（重新移动/拾取） |
| `level_fail` | `aspirate` | 液体未移动 | 该复合步骤起点 |
| `motion_conflict` | `move_head`（可用 `op_index` 精确定位） | 若发生在吸液后，则吸头带液停在原地 | 失败的移动微操作本身，保留吸头持液，直接继续去排液 |

恢复（`lab/store.py:resume`）创建新的 `runs` 行（`parent_id` 指向失败运行，同 `root_id`），
并写入一个 `resume_branch` 事件记录恢复依据：

- 吸液已成功而排液未完成：依据吸液后检查点（`physical_post_aspirate`），
  游标指向失败微操作，恢复分支**不会再次吸液**，只把现实液体排到目标孔。
- 吸液前失败：依据失败步骤之前最近的复合提交点（`last_compound_commit`）或初始检查点。

失败父运行保留现场并标记为 `recovered`，页面运行树中以恢复分支呈现，不被覆盖。

## 验收场景：吸液后、排液前注入故障

固定协议步骤 `X3`（`SRC:A1` → `PLT:A3`，75 µL）在“吸液后移动到目标孔板”的
`move_head`（`op_index = 4`）注入 `motion_conflict`：

```json
{"fault": "motion_conflict", "op_name": "move_head", "op_index": 4, "step_id": "X3"}
```

自动化测试 `tests/test_store.py::test_acceptance_mid_compound_failure_keeps_physical_liquid`
断言：

1. 父运行 `failed`，现实吸头 `TIP:T3` 持有 `ReagentA = 75 µL`，复合转移未提交；
2. 恢复两次返回同一分支，恢复事件携带持液吸头状态，游标为 `step_index=2, op_index=4`；
3. 恢复分支不包含第二次 `aspirate`，只执行后续移动与 `dispense`；
4. `PLT:A3` 精确得到 75 µL，精确守恒差额为 0，谱系与物理状态对账一致；
5. 用 `replay_key="resume"` 重放恢复事件两次返回同一事件，全程幂等。

幂等键格式为 `s{step}o{op}a{attempt}`（恢复事件固定为 `resume`），
`events(run_id, replay_key)` 有唯一约束；重复提交已存在的键只返回原事件，不重复施加物理操作。

## 运行记录导出 / 清空 / 导入复核

- `GET /api/export` 导出 `runs / events / checkpoints` 全量 JSON（格式 `pipette-testimony/1`）。
- 导入前 `Store.verify_export` 独立重放：主运行从初始状态逐条重放微操作，
  恢复分支从 `resume_branch` 指向的检查点起算，并逐检查点核对时钟与完整物理状态。
- `POST /api/reset` 清空数据库；`POST /api/import` 先重放校验再写回，
  因此可以在清空数据库后重新导入并复核失败现场（包括吸头持液）。

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/fixture` | 固定 deck、协议微操作展开与初始状态 |
| POST | `/api/runs` | 新建运行（可带 `faults` 计划） |
| GET | `/api/runs/{id}` | 状态、事件、检查点、谱系、守恒核对、持液吸头 |
| POST | `/api/runs/{id}/step` | 单步执行；body 可带 `replay_key` 重放 |
| POST | `/api/runs/{id}/inject` | 实时注入 `tip_not_ready / level_fail / motion_conflict` |
| POST | `/api/runs/{id}/resume` | 从最后可提交检查点分叉恢复（幂等） |
| GET | `/api/export` / POST `/api/import` / POST `/api/reset` | 记录导出导入与清空 |

## 代码结构

```
app.py               FastAPI 入口与路由
lab/state.py         Fraction 容量、Well/Container/Tip/LabState
lab/fixtures.py      固定 deck、协议与微操作展开、静态容量核对
lab/engine.py        纯函数微操作转移、故障、前后摘要、成分矩阵与守恒
lab/store.py         SQLite 事务、检查点、恢复分叉、谱系、导出导入校验
web/index.html       操作页面（deck / 时线 / 谱系 / 时钟 / 控制）
tests/               引擎、存储恢复、HTTP API 的自动化测试
```
