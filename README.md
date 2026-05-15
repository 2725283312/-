# AI API Proxy

基于 FastAPI + SQLite 实现的高可用 AI 接口反向代理，支持多上游端点负载均衡、自动健康检测与故障转移、流式响应透传，内置 Web 管理界面。

---

## 功能特性

- **多端点负载均衡**：请求随机分发到多个上游端点
- **自动故障转移**：请求失败时自动切换到其他健康端点，对调用方无感知
- **双池管理**：端点分为「存活池」与「复活池」，失效端点自动隔离，31 天后自动探活并尝试恢复
- **健康检测**：端点达到调用阈值后自动触发健康检测，失败立即移入复活池
- **流式响应支持**：完整支持 `stream: true` 的 SSE 流式输出，先确认首个数据块再提交端点，空流立即切换
- **URL 文件同步**：支持从服务器本地文件批量同步 URL，读取后自动清空，方便外部脚本持续写入
- **Web 管理界面**：内置可视化仪表盘，查看端点状态、添加 URL、触发文件同步
- **OpenAI 格式兼容**：`/v1/...` 路由直接转发，可对接任意兼容 OpenAI 接口的客户端

---

## 快速开始

### 使用 Docker Compose（推荐）

```bash
cp .env.example .env
# 编辑 .env，填写 ADMIN_TOKEN、CLIENT_API_KEY、UPSTREAM_API_KEY
docker compose up -d --build
```

服务监听 `http://localhost:7788`，Web 管理界面：`http://localhost:7788/ui/`

### 直接运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 7788
```

---

## 环境变量

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `ADMIN_TOKEN` | `changeme` | 管理接口鉴权 Token，**必须修改** |
| `CLIENT_API_KEY` | `changeme` | 客户端调用代理时使用的 Bearer Key，**必须修改** |
| `UPSTREAM_API_KEY` | _(空)_ | 代理转发到上游时携带的真实 API Key，**必须填写** |
| `DB_PATH` | `/app/data/proxy.db` | SQLite 数据库文件路径 |
| `REQUEST_TIMEOUT` | `60` | 上游请求超时时间（秒） |
| `HEALTH_CHECK_TIMEOUT` | `10` | 健康检测超时时间（秒） |
| `REVIVAL_CHECK_INTERVAL` | `30` | 复活池后台轮询间隔（秒） |
| `MAX_CALLS_BEFORE_CHECK` | `3` | 端点每调用多少次触发一次健康检测 |
| `DEFAULT_MODEL` | `gpt-4o-mini` | 健康检测使用的模型 |
| `URL_SYNC_FILE` | _(空)_ | 批量 URL 同步的文件路径，空则禁用 |
| `URL_SYNC_INTERVAL` | `3600` | URL 文件同步轮询间隔（秒） |

---

## 使用方式

### 1. 添加上游 URL

通过 Web 界面（`/ui/`）直接粘贴 URL，或使用 API：

```bash
curl -X POST http://localhost:7788/admin/urls \
  -H "Authorization: Bearer <ADMIN_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"urls": ["https://api.example.com", "https://api2.example.com"]}'
```

### 2. 通过代理调用 AI 接口

将客户端的 Base URL 指向本服务，`Authorization` 填写 `CLIENT_API_KEY`：

```bash
curl http://localhost:7788/v1/chat/completions \
  -H "Authorization: Bearer <CLIENT_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "Hello!"}]}'
```

流式调用加 `"stream": true` 即可，代理先确认有真实数据流出再向客户端提交：

```bash
curl http://localhost:7788/v1/chat/completions \
  -H "Authorization: Bearer <CLIENT_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "Hello!"}], "stream": true}'
```

---

## 端点池工作机制

```
          请求到达
             │
    校验 CLIENT_API_KEY
             │
       从存活池随机选取端点
             │
    ┌────────┴────────────┐
    │ 调用次数 < 阈值      │ 调用次数 >= 阈值
    │                     │
  直接转发            先发起健康检测
    │               ┌─────┴─────┐
    │             通过         失败
    │               │           │
    │           重置计数     移入复活池
    │               │       → 重选端点
    └───────────────┘
         │
    转发成功 → 调用计数 +1
    转发失败 → 移入复活池 → 切换端点自动重试
```

**复活池**：端点进入复活池满 31 天后（以加入系统的时间为基准），后台任务定期探活。成功则重新移回存活池，并重置 31 天计时。

---

## 管理接口

所有管理接口需携带 `Authorization: Bearer <ADMIN_TOKEN>`。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 服务健康检查 |
| `GET` | `/admin/urls` | 列出所有端点及状态 |
| `POST` | `/admin/urls` | 添加 URL（`{"urls": [...]}`) |
| `POST` | `/admin/url-sync/run` | 手动触发文件同步（可选传 `{"file": "..."}` 覆盖路径） |

---

## URL 标准化规则

提交 URL 时只填域名即可：
- 若不含路径或路径为 `/`，自动补全为 `/api`
- 转发时构造完整路径，例如 `https://example.com/api/v1/chat/completions`

---

## 项目结构

```
├── app/
│   ├── main.py          # FastAPI 应用入口，路由定义
│   ├── pool_manager.py  # 端点池核心逻辑（选取、健康检测、故障转移）
│   ├── config.py        # 环境变量配置加载
│   ├── db.py            # SQLite 数据库初始化与迁移
│   ├── schemas.py       # Pydantic 请求/响应数据模型
│   ├── url_utils.py     # URL 标准化工具
│   └── static/
│       └── index.html   # Web 管理界面
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── requirements.txt
```

---

## 注意事项

- 部署前务必修改 `ADMIN_TOKEN`、`CLIENT_API_KEY`，并填写 `UPSTREAM_API_KEY`
- Docker 部署数据库通过 `./data` 目录挂载持久化，确保该目录有写权限
- 已有旧版数据库（含分组结构）会自动迁移，URL 数据保留，分组表删除
