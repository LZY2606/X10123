# SchemaGate · 兼容性闸门

判断一组生产者/消费者能否安全跨版本通信的 schema 演进闸门。
纯 Python 标准库实现（`sqlite3` + `http.server`），不接外网、不启动独立数据库。

## 安装与运行

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q
.venv/bin/python -m schemagate --host 127.0.0.1 --port 5212
# 打开 http://127.0.0.1:5212 （页面标题“兼容性闸门”）
```

## 语义

- **方向**：`backward`（新版本读旧数据）、`forward`（旧版本读新数据）、`full`（双向）。
  每个方向独立按 writer→reader 做结构化检查，不做字符串 diff。
- **差异**：每个不兼容点绑定结构路径（如 `/items/[]/level`）、受影响消费者与最小反例，
  按严重度（error > warning）排序。
- **方向不对称**：缩窄数字范围只破坏 backward；union 新增分支 / 枚举新增值只破坏
  forward；删除带默认值字段双向安全；新增无默认值必填字段破坏 backward。
- **重命名**：只有登记了显式迁移映射（`POST /api/migrations`）才可过渡；映射链成环
  （含自环）会被拒绝（400）。
- **消费者声明带版本**：发布决策把当时解析出的声明快照存入决策行，重放
  （`GET /api/decisions/{id}/replay`）永远使用历史声明。
- **指纹**：内容递归排序后取 SHA-256，键顺序不同指纹相同。
- **发布**：`POST /api/publish` 携带 `idempotency_key`，重复请求返回原决策
  （`idempotent_replay: true`）；`base_version` 与当前 head 不一致时返回 409，
  同一旧版本并发发布只有一个成为新 head。
- **持久化与迁移**：所有版本、决策、反例存于 SQLite；`GET /api/export` /
  `POST /api/import` 做 JSON 导入导出，重建后指纹与决策编号一致。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/schemas` | 登记 schema 版本（首个版本自动成为 head） |
| POST | `/api/consumers` | 登记消费者声明（name+version+读取字段/默认值） |
| POST | `/api/migrations` | 添加字段重命名映射（成环拒绝） |
| POST | `/api/check` | 临时检查 old/new + 方向 + 消费者集合 |
| POST | `/api/publish` | 计划发布（幂等键 + head 乐观锁） |
| GET  | `/api/decisions/{id}/replay` | 用历史声明重放旧决策 |
| GET  | `/api/export` / POST `/api/import` | JSON 导入导出 |
| GET  | `/api/state` | 页面所需的全量状态 |
