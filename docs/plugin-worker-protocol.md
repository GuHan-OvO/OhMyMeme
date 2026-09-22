# 插件 worker 协议 v1

本文档冻结宿主进程与插件 worker 进程之间的线路协议。协议版本为 `1`，与插件 API 版本（`PLUGIN_API_VERSION = 1`）独立维护；任一方不兼容时必须拒绝握手并给出可读原因。

## 传输与帧

- 传输：标准输入/输出管道（stdio）。stdout 只承载协议帧，stderr 只承载日志行。
- 帧格式：`Content-Length: <字节数>\r\n\r\n<UTF-8 JSON>`。
- 单帧上限 16 MiB；超限即协议错误并关闭通道。
- 携带大文件的数据不进入 RPC：宿主在操作开始时下发临时工作区路径，插件把产物写入工作区，宿主通过 sink 完成入库。

## 消息

消息体为 JSON-RPC 2.0 风格对象：请求带 `id` 与 `method`，响应带 `id` 与 `result` 或 `error`，通知不带 `id`。错误对象为 `{code, message, data?}`，标准区间沿用 JSON-RPC 定义，宿主域错误码见 `ohmymeme/core/plugins/runtime/protocol.py`（`reject`、`unsupported`、`operation_busy`、`operation_unknown`、`limit_exceeded`、`worker_failed`）。

## 握手与加载

1. worker 启动后以请求 `hello` 上报：`{protocol_version, plugin_id, token, pid, python}`。
2. 宿主校验协议版本、插件 ID、一次性 token 与已解析的插件版本，回 `welcome {accepted, protocol_version, plugin{id, version, entry, package_dir, package_root, kind, capabilities}}`；不匹配时回 `{accepted: false, reason: "reject"}`。
3. worker 把 `package_dir` 加入 `sys.path`，按 `entry`（`module:create_plugin`）加载插件并校验 `provider_id`/`api_version`，随后以请求 `ready {ok, error}` 上报加载结果。加载失败时宿主记录 `plugin_load_failed` 并终止进程。

## 方法表（固定集合）

宿主到 worker：

| 方法 | 参数 | 结果 |
| --- | --- | --- |
| `ping` | 无 | `{pong: true}` |
| `plugin.query` | `{method, params, query_id}` | 查询结果（当前允许 `inspect`） |
| `operation.start` | `{op_id, kind, request, config, secrets, workspace, listing}` | `{accepted, reason?}` |
| `operation.cancel` | `{op_id}` | `{ok: true}` |
| `shutdown` | 无 | `{ok: true}`，随后进程退出 |

worker 到宿主：

| 方法 | 参数 | 结果 |
| --- | --- | --- |
| `sink.import_batch` | `{op_id, requests: [{path, name}]}` | `{ids, rejected}` |
| `sink.import_path` | `{op_id, request: {path, name}}` | `{ids, rejected}` |
| `config.set` | `{op_id, key, value}` | `{ok: true}`，宿主校验并持久化 |
| `nickname.lookup` | `{qq, query_id}` | 昵称字符串，仅查询期间开放 |

worker 到宿主通知：

| 通知 | 参数 | 说明 |
| --- | --- | --- |
| `progress` | `{op_id, value}` | 插件进度快照，宿主直接刷新状态 |
| `log` | `{op_id, value}` | 操作日志行，宿主按现有语义处理 |
| `operation.finished` | `{op_id, ok, result?, error?}` | 操作结束，结果需可 JSON 序列化 |
| `process.registered` | `{op_id, pid}` | 插件自建子进程登记，回收以进程树为准 |

## 操作语义

- 每个 worker 同一时刻至多执行一个操作；`operation.start` 被占用时返回 `{accepted: false, reason: "operation_busy"}`。
- `kind` 决定插件侧入口：`import` → `import_media`，`list` → `list_stickers`。
- 宿主提供 `config`（非密钥）与 `secrets`（按操作注入，只读）；`workspace` 为该操作的临时根目录，插件只允许在其中写入。
- 取消：宿主发 `operation.cancel`，worker 置取消标志并调用插件 `stop()`；插件应尽快返回，宿主在宽限期后仍以进程树回收兜底。
- 结束：worker 在插件方法返回后调用插件 `finish()`（如定义），再发送 `operation.finished`。

## 生命周期

- 心跳：宿主每 5 秒 `ping`，连续 2 次失败判定失联并整树回收。
- 崩溃退避：1s/2s/4s，10 分钟内 3 次崩溃即熔断隔离，下一次启动前拒绝并展示原因。
- 空闲回收：无在途操作且空闲超过 300 秒后停止 worker，下次操作惰性重启。
- 退出：宿主退出时先发 `shutdown`，再按 Job Object（Windows）或进程组（POSIX）整树回收，保证无孤儿进程。

## 安全边界

子进程只提供崩溃与资源隔离，不是安全沙箱。协议不接受任意方法或属性调用；worker 侧只暴露固定方法表与固定查询名，宿主不向插件传递持久配置对象、数据库连接或宿主路径对象。未签名插件允许安装，安装确认是唯一信任门槛。
