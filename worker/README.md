# Pica Cloudflare 分发引擎 (Worker)

本目录包含仓库的 **Cloudflare Worker** 代理逻辑。`wrangler.toml` 部署的是 `worker/index.js`；
`worker/worker.js` 是已被取代的历史副本，仅供对照，请勿部署。

## ⚡ 路由

| 路由 | 上游 | 缓存 |
|---|---|---|
| `/` | `r/index.json` | 60s |
| `/index.json`、`/r/index.json` | `r/index.json` | 60s |
| `/repo.json`、`/r/repo.json` | `r/repo.json` | 60s |
| `/<mgr>/index.json`、`/r/<mgr>/index.json` | 描述文件声明的路径 | 60s |
| `/latest/<file>` | `releases/download/latest/<file>` | 有 SHA256 → 7 天 immutable；无 → 300s 且不入缓存 |
| `/releases/<tag>/<file>` | `releases/download/<tag>/<file>` | 同上 |

`/repo.json` 与 `/latest/`、`/releases/` 是原有路由，行为不变：老客户端不受影响。
新增的只有入口与后端索引。

### 为什么后端索引要走描述文件

Worker 里没有包清单。`/apk/index.json` 这类请求会先读 `r/index.json`，只有当该路径
**被描述文件声明**时才向上游取；否则直接 404。没有这一步，这个路由就是一台开放的
`raw.githubusercontent.com` 读取器。

### 二进制为什么以 SHA256 做缓存键

文件名可以复用，字节不行。`repo.json` 里每个条目带 `sha256`，Worker 用它拼缓存键：
哈希不变就命中 Cloudflare 缓存，哈希一变就是新键，不会把旧字节发给拿到新索引的客户端。

上游地址优先取条目里的 `download_url` —— 让 Worker 跟着发布规则走，而不是自己从文件名
反推。但只跟 `https://github.com/<本仓库>/releases/` 下的地址：镜像若把 `download_url`
改写成自己，Worker 跟随就会打回自己，那是请求循环。

## 🚀 部署

* 基于 `wrangler.toml`（`main = "worker/index.js"`），`GITHUB_USER` / `GITHUB_REPO` 为环境变量。
* 测试：`node --test worker/test_worker.mjs`（缓存与上游均为内存桩，不联网）。

**地址**: `https://pica-pkg-repo.<username>.workers.dev/`

## ⚠️ 已知边界

`repo.json` 目前原样透传，条目里的 `download_url` 仍指向 GitHub Releases。也就是说：
**客户端正常安装时，二进制不走 Worker**，只有显式请求 `/latest/<file>` 才会经过 CDN。

把 `download_url` 改写成 `https://<worker>/latest/<file>` 可以让 CDN 真正接管下载，但那是
一次路由决策而非索引格式变更：Worker 一旦不可用，所有经过它同步的客户端都装不了包。
这一条留给下一批单独决定，本批只保证路由与格式正确。
