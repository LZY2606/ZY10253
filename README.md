# 移液执行证言台

用离散事件模拟器重放自动移液系统的执行过程，核对容量、容器位置、
吸头状态与失败恢复点。本地服务基于 FastAPI + SQLite + NumPy，
页面提供 deck 布局、操作时线、每孔成分谱系与当前模拟时钟。

## 安装与运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q && .venv/bin/uvicorn app:app --host 127.0.0.1 --port 5593
```

访问 http://127.0.0.1:5593 可见“移液执行证言台”。

## 数据口径

- **容量**: 全部以有理微升 (`fractions.Fraction`) 表达，序列化为
  `{"__frac__": "n/d"}`。吸液/排液按成分比例精确抽取，
  `sum(抽取) == 目标体积` 恒成立，不存在浮点累积导致的容量增减。
  fixture 中含一笔 `100/3 µL` 转移用于验证。
- **双层状态**: 每孔同时维护 `committed`（数据库已提交视图）与
  `physical`（物理现实）。复合转移 (拾取→吸液→排液→退吸头) 只有
  全部成功才提交（committed 对齐 physical）；中途失败时 committed
  回滚到最近检查点，但 physical 保留现实——例如吸头里已吸入的液体。
- **事件**: 每个原子步骤产生一条事件，记录前后状态摘要
  （状态哈希 + 非空孔体积 + 吸头）。`event_id` 确定性生成且全局唯一，
  重复应用同一事件幂等（`INSERT OR IGNORE`，直接返回原记录）。
- **检查点**: 每个脚本操作开始前自动落一个“可提交检查点”
  （此时 committed == physical）。
- **恢复**: 从最后一个可提交检查点派生新分支 run（`run-1~rec0`），
  committed 视图取检查点快照，physical 现实（含吸头余液、孔位、
  吸头架、deck 位置）整体继承，从失败步骤继续执行——不重复吸液、
  不丢失物理状态。原失败运行完整保留（状态置为 `superseded`），
  恢复分支不覆盖原运行。恢复事件本身幂等：同一恢复重放两次返回
  同一分支，事件数不变。

## 重放方式

- **单步 / 连续执行**: 页面按钮或 `POST /api/step`、`POST /api/run_all`。
- **故障注入**: `POST /api/inject`，`kind` ∈
  `tip_not_ready`（吸头未就绪）/ `lld_failure`（液面检测失败）/
  `motion_conflict`（运动冲突）。注入为一次性事件，在下一个匹配
  类别的步骤触发；在吸液后、排液前注入 `lld_failure` 即复现
  “吸头有液而事务未提交”的场景。
- **恢复**: `POST /api/recover`，从最后检查点派生恢复分支。
- **导出 / 复核**: `GET /api/export` 导出全部运行记录
  （runs/events/checkpoints，格式 `testimony-dump-v1`）；
  `POST /api/import` 在清空数据库后原样导入，逐 run 比对
  `state_json` 哈希即可复核。`POST /api/reset` 重置为固定 fixture。

## 验收场景复现

```bash
# 吸液后、排液前注入液面检测失败
curl -X POST localhost:5593/api/step   -d '{"run_id":"run-1"}'   # pickup
curl -X POST localhost:5593/api/step   -d '{"run_id":"run-1"}'   # aspirate
curl -X POST localhost:5593/api/inject -d '{"run_id":"run-1","kind":"lld_failure"}'
curl -X POST localhost:5593/api/step   -d '{"run_id":"run-1"}'   # dispense 失败 -> awaiting_recovery
curl -X POST localhost:5593/api/recover -d '{"run_id":"run-1"}'  # -> run-1~rec0
curl -X POST localhost:5593/api/recover -d '{"run_id":"run-1"}'  # 幂等: 同一分支
curl -X POST localhost:5593/api/run_all -d '{"run_id":"run-1~rec0"}'
```

（以上 POST 需加请求头 `-H 'Content-Type: application/json'`。）
恢复后 `op1` 的 aspirate 事件全库仅一条（不重复吸液），物理总量
守恒（270 µL），`dst_plate/A2` 精确为 `100/3 µL`。

## 结构

- `sim/state.py` — 有理微升与状态序列化/哈希
- `sim/store.py` — SQLite 持久层（runs / events / checkpoints）
- `sim/engine.py` — 离散事件引擎：步骤展开、提交/回滚、故障、恢复
- `sim/fixtures.py` — 固定 fixture（deck、试剂、脚本）
- `sim/deck.py` — NumPy deck 占用矩阵与孔板体积矩阵
- `app.py` — FastAPI 入口与视图组装
- `static/index.html` — 操作页面
- `tests/test_sim.py` — 9 项自动化测试（含验收场景）
