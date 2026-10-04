# 特殊教育支持计划合规

纯Python标准库实现的特殊教育支持计划合规原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、同意、服务履约、复查期限、计划版本、冻结与接手快照。
- `src/repository.py`：SQLite建表、聚合事务和查询。
- `src/handoff.py`：跨校交接链（转出冻结、确认接手、拒绝、途中撤回、批次恢复）。
- `src/service.py`：用例编排、机构归属写入边界、越权/冲突写入隔离和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景与交接链测试。

## 交接链（转学场景）

支持计划、机构归属、监护人同意通过批次号（`batch_no`）串成可追溯链路：

1. **原校发起转出 `transfer_out`**：计划进入 `transferring` 并冻结（字段不可改），
   建立 `pending` 交接单，归属与批次落库；状态变更时原校未完成待办自动失效并要求重新确认。
2. **确认前补录窗口**：冻结期仅原建档机构可继续 `log_service`，新校/外部机构的写入被隔离；
   家长撤回同意后，任何服务登记都被隔离。
3. **新校确认 `confirm_transfer`**：交接单完成，计划回到 `active`，机构归属切到新校，
   payload 中的 `handover` 快照只携带有效目标（`active_goals`）和服务进度（分钟数、履约率）。
4. **原校只读**：确认后原校角色可查看审计时间线，但任何写入返回403并进入隔离区。
5. **新校拒绝 `decline_transfer`** 或 **家长途中撤回 `withdraw_consent`**：计划进入
   `transfer_failed`，交接单标记 `declined`/`withdrawn`，晚到的确认返回409。
6. **失败恢复**：家长可 `grant_consent` 重新授权，原校执行 `restore_batch` 恢复原批次，
   归属回到原校、计划可续做，批次内失效待办自动重新打开。
7. **并发安全**：版本乐观锁 + `BEGIN IMMEDIATE`；两校同时提交交接或晚到写入一律返回409，
   原始输入写入 `quarantined_writes`，响应体携带 `quarantine_id` 与 `original_input`。
8. **旧数据回填**：`POST /api/batches/{batch_no}/backfill-ownership` 按迁移批次把缺失
   `owning_org` 的记录回填为原建档机构，每条记录留下审计事件。

## 启动

```bash
python3 app.py --db ./data.db --port 8328
```

默认端口为`8328`，默认数据库位于项目目录。服务启动时自动建表（含旧库列迁移）。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/records/{id}/transfers`：交接单链。
- `GET /api/records/{id}/consents`：监护人同意链。
- `GET /api/records/{id}/todos`：待办列表。
- `GET /api/quarantine`：隔离写入（可带`batch`）。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录（`X-Org`作为建档机构归属）。
- `POST /api/records/{id}/actions/{action}`：业务动作：
  基础流程 `consent`/`activate`/`log_service`/`review`/`amend`/`close`/`withdraw_consent`/`grant_consent`；
  交接 `transfer_out`（`data.to_org`、可选`batch_no`）、`confirm_transfer`、`decline_transfer`、`restore_batch`。
- `POST /api/records/{id}/todos`：建待办；`POST /api/records/{id}/todos/{tid}/complete|confirm`。
- `POST /api/batches`：登记迁移批次；`POST /api/batches/{batch_no}/backfill-ownership`：回填机构归属。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，跨校写入校验依赖`X-Org`。
越权返回403、晚到冲突返回409，响应体均带`quarantine_id`与`original_input`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及冻结补录、并发交接、
途中撤回、失败恢复、待办失效重确认与旧数据机构归属回填。
