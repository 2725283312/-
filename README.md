# AI API 代理系统（OpenAI 格式）

这是一个可部署在 Linux 服务器上的 AI API 代理服务，支持：

1. 一个上游 API Key 绑定多个 URL，统一管理。  
2. 外部以 OpenAI 兼容格式调用 `/v1/*`。  
3. 仅从**存活池（alive）**选择 URL。  
4. 每个 URL 默认被调用 3 次后触发标准检测（发送最简单的 AI 测试消息到 `/v1/chat/completions`，模型优先使用该分组最近一次真实调用的 model）。  
5. 检测空回或报错时，将 URL 丢入**复活池（revival）**。  
6. 复活池仅在 URL 添加时间满 31 天后才参与探活，探活成功后自动回到存活池。
7. 支持后端定时读取文件中的 URL，自动加入存活池（alive）。

## 快速启动（Docker，推荐）

```bash
docker compose up -d --build
```

服务端口默认 `8080`。
内置监控前端：`http://127.0.0.1:8080/ui/`（或直接访问 `/` 自动跳转）。

## 管理接口

管理接口需要 `Authorization: Bearer <ADMIN_TOKEN>`（默认 `QQliutao011007`）。

### 1) 创建分组并写入 URL

```bash
curl -X POST "http://127.0.0.1:8080/admin/groups" \
  -H "Authorization: Bearer QQliutao011007" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "default",
    "client_api_key": "client-key-001",
    "upstream_api_key": "sk-upstream-xxx",
    "urls": [
      "https://api-1.example.com",
      "https://api-2.example.com",
      "https://api-3.example.com"
    ]
  }'
```

`client_api_key` 是外部调用你代理时使用的 Key。  
`upstream_api_key` 是代理请求上游 URL 时使用的真实 Key。

### 2) 给已有分组追加 URL

```bash
curl -X POST "http://127.0.0.1:8080/admin/groups/1/urls" \
  -H "Authorization: Bearer QQliutao011007" \
  -H "Content-Type: application/json" \
  -d '{"urls": ["https://api-4.example.com"]}'
```

### 3) 查看池状态

```bash
curl -H "Authorization: Bearer QQliutao011007" \
  "http://127.0.0.1:8080/admin/groups"
```

```bash
curl -H "Authorization: Bearer QQliutao011007" \
  "http://127.0.0.1:8080/admin/groups/1"
```

### 4) 前端实时监控

打开 `/ui/` 页面，输入 `ADMIN_TOKEN` 后可实时看到：
- 分组总览（alive/revival 总数）
- 每组下每个 URL 的池状态、调用次数、添加时间、最近检测和错误，以及分组最近调用模型
- 自动刷新（默认 5 秒，可调）
- 支持页面内直接创建分组、一次性批量添加 URL、给已有分组追加 URL

前端已将 `ADMIN_TOKEN`、`client_api_key`、`upstream_api_key` 默认预填为 `QQliutao011007`（可改）。
前端 URL 支持不带 `/api`，后端会自动补成 `/api`。

### 5) 文件自动导入 URL（后端定时任务）

在 URL 文件中一行一个地址（支持 `#` 注释行），例如：

```txt
# url_source.txt
https://repli--gungfipom6794.replit.app
https://api-2.example.com/api
```

配置项：
- `URL_SYNC_FILE`：URL 文件路径（例：`/data/url_source.txt`）
- `URL_SYNC_GROUP_ID`：导入到哪个分组
- `URL_SYNC_INTERVAL`：扫描间隔秒数（默认 3600，即 1 小时）

逻辑：
- 文件中的 URL 会被标准化后写入分组存活池
- 已存在 URL 会被重新拉回 alive 池并清空错误状态
- 每次读取并导入后，文件内容会被清空

可手动触发一次文件导入：

```bash
curl -X POST "http://127.0.0.1:8080/admin/url-sync/run" \
  -H "Authorization: Bearer QQliutao011007"
```

## 外部 OpenAI 格式调用

示例：`/v1/chat/completions`

```bash
curl -X POST "http://127.0.0.1:8080/v1/chat/completions" \
  -H "Authorization: Bearer client-key-001" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [
      {"role": "user", "content": "hello"}
    ]
  }'
```

## 配置项

- `ADMIN_TOKEN`：管理接口令牌。  
- `DB_PATH`：SQLite 数据库路径。  
- `REQUEST_TIMEOUT`：上游请求超时秒数。  
- `HEALTH_CHECK_TIMEOUT`：标准检测超时秒数。  
- `REVIVAL_CHECK_INTERVAL`：复活池探活间隔秒数。  
- `URL_SYNC_INTERVAL`：URL 文件扫描间隔秒数。  
- `URL_SYNC_FILE`：URL 文件路径（为空则关闭文件扫描导入）。  
- `URL_SYNC_GROUP_ID`：文件导入目标分组 ID（>0 才生效）。  
- `MAX_CALLS_BEFORE_CHECK`：单 URL 调用次数阈值（默认 3）。
- `DEFAULT_MODEL`：无历史调用模型时，健康检测使用的兜底模型（默认 `gpt-4o-mini`）。
