# XR-2000 REST API 参考

所有接口以 `/api/v1` 为前缀，请求与响应均为 JSON，字符编码 UTF-8。
除 `/healthz` 与 `/api/v1/version` 外，所有接口都需要在请求头携带
`Authorization: Bearer <token>`。

## 1. 认证

### POST /api/v1/auth/login

请求体：

```json
{"username": "admin", "password": "***", "mfa_code": "123456"}
```

响应包含 `token` 与 `expires_in`（秒）。令牌默认 8 小时有效。

### POST /api/v1/auth/logout

使当前令牌立即失效。令牌本身是无状态的，登出依赖 Redis 中的吊销列表。

## 2. 配置管理

### GET /api/v1/config

返回全部运行时配置。

### PATCH /api/v1/config

局部更新配置，只传需要改的字段。

```json
{"upstream_timeout": 60, "max_retry": 5}
```

更新立即生效，不需要重启进程，但**不会写入配置文件**。
如果希望持久化，需要在修改后调用 `POST /api/v1/config/commit`。

### POST /api/v1/config/commit

把当前内存中的配置写入 `/etc/xros/gateway.yaml`。

### POST /api/v1/config/reload

从配置文件重新加载，会丢弃所有未提交的内存修改。

## 3. 状态查询

| 接口 | 说明 |
| --- | --- |
| GET /healthz | 存活探针，不检查依赖，恒返回 200 |
| GET /readyz | 就绪探针，检查 Redis 与 License，异常返回 503 |
| GET /api/v1/version | 返回版本号与构建时间 |
| GET /api/v1/metrics/summary | 连接数、吞吐、错误率的汇总快照 |
| GET /api/v1/upstream/health | 各上游的健康状态与实时延迟 |

## 4. 错误响应格式

所有错误统一返回如下结构，HTTP 状态码与 `code` 字段对应：

```json
{
  "code": "E2001",
  "message": "认证失败：令牌已过期",
  "request_id": "b7f3c1a2-4e5d-4f6a-8b9c-0d1e2f3a4b5c"
}
```

排查问题时请提供 `request_id`，我们可以在服务端日志中定位这次请求的完整链路。

## 5. 限流

默认限流为每令牌每秒 50 次请求，超出返回 429 并带 `Retry-After` 头。
限流窗口在 Redis 中共享，所以集群模式下所有节点看到的是同一个计数器。

## 6. 版本兼容策略

API 采用 URL 版本号。`/api/v1` 内只做向后兼容的增量修改，
破坏性变更会发布 `/api/v2`，并保证 v1 继续可用至少 12 个月。
