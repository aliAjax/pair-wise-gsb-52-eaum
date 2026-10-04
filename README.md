# 特殊教育支持计划合规

纯Python标准库实现的特殊教育支持计划合规原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、同意、服务履约、复查期限、计划版本、冻结与冲突检查。
- `src/repository.py`：SQLite建表、事务和查询（记录、交接批次、待办、隔离写入、迁移批次）。
- `src/service.py`：用例编排、机构归属与权限检查、乐观并发、越权隔离和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、交接链和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8328
```

默认端口为`8328`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/records/{id}/transfers`：交接链（批次、原校、新校、状态、快照）。
- `GET /api/records/{id}/todos`：待办列表（open/done/invalidated）。
- `GET /api/records/{id}/quarantine`：被隔离的越权/晚到写入及原输入。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/migrations/org-backfill`：按迁移批次回填旧数据的机构归属，请求体为`{"batch_id":"...","org":"..."}`，仅管理员。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 转学交接链

- `transfer_initiate`（原校，请求体`data.to_org`）：发起转出即冻结计划，生成批次快照；冻结期间仅可`log_service`补录服务和监护人重新`consent`。
- `transfer_confirm`（新校）：确认后新校接手有效目标与服务进度，原校角色只读，越权写入被隔离并保留原输入（409 `write_quarantined`）。
- `withdraw_consent`（监护人）：撤回同意；交接进行中时批次转为失败，此后外部机构`log_service`等晚到写入被隔离。
- `transfer_restore`（原校）：交接失败后恢复原批次快照并续做，重新进入待确认状态。
- `todo_create`/`todo_complete`/`todo_reconfirm`：交接状态一变，原校未完成待办即失效，需重新确认后才能完成。
- 两校同时提交交接或版本过期时，晚到写入返回冲突并隔离保留原输入；旧数据缺少机构归属时按迁移批次回填原建档机构（优先取`created_by_org`）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
