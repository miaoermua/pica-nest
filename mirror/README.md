# 镜像

两种镜像只改写索引里的 `download_url`，`sha256` 等字段原样返回，所以设备照常校验它下载到的字节。

## Cloudflare Worker（worker.js）

```bash
npx wrangler deploy --config mirror/wrangler.toml
```

`wrangler.toml` 的 `GITHUB_REPO` / `BRANCH` 指向本仓库。设备源地址填 `https://<worker 域名>/r`。

| 路由 | 内容 | 缓存 |
|---|---|---|
| `/__self` | 本部署读的是哪个仓库 | 不入缓存 |
| `/` `/index.json` `/r` `/r/` `/r/index.json` | `r/index.json` | 60 s |
| `/<path>` `/r/<path>` | 入口描述**声明过**的后端索引，`download_url` 改写到本域名 | 60 s |
| `/releases/<tag>/<file>` | 本仓库的 Release 资产 | 索引里有 sha256：按 sha256 缓存 7 天；没有：300 s 且不入缓存 |

- 没在 `r/index.json` 声明的路径一律 404，Worker 不会变成任意 raw 文件的代理。
- 只改写指向本仓库 Release、且文件名与条目一致的地址；其他地址（局域网、别的仓库）原样保留。
- Git 里的索引保留 GitHub 地址，配了其他镜像的设备不受 Worker 影响。

测试：`node --test mirror/worker.test.mjs`（缓存与上游均为桩，不联网）。

### 改名之后必须重新部署

部署自带一份仓库名：`GITHUB_REPO` 是 deploy 那一刻写进环境变量的，`r/` 却是**每次请求**按这个名字去读的。所以仓库改名、只改 Git 里的 `wrangler.toml` 而不重新部署时，线上仍在读**旧名字**：那份 `r/` 停在改名那天，索引里的资产地址在旧仓库的 Release 里 404，而本仓库的 `r/` 完全健康——从仓库内部看不出来。

两个自查入口：

```bash
curl -s https://pica-nest.miaoer.net/__self                 # {"repo":"miaoermua/pica-nest","branch":"main"}
curl -sI https://pica-nest.miaoer.net/r/opkg/index.json     # x-pica-upstream: miaoermua/pica-nest@main
python3 -m ci.nest mirror --url https://pica-nest.miaoer.net --repo miaoermua/pica-nest
```

`ci.nest mirror` 会比对两件事：`__self` 报的仓库是不是本仓库，以及三份后端索引的 `generated_at`／条目数是否与 `r/` 一致。部署早于 `/__self` 时它报「这个部署比 `mirror/` 里的代码旧」。

CI 里 `.github/workflows/mirror.yml` 负责两件事：`mirror/**` 变更时跑测试并 `wrangler deploy`（需要 `CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID` 两个 secret，没有就跳过并留一条 notice），以及每天 03:17 与手动触发时跑 `ci.nest mirror`——这正是「改名没重新部署」会红的地方。

## 局域网镜像（serve.py）

```bash
python3 mirror/serve.py                                  # 0.0.0.0:8080，自动探测本机局域网地址
python3 mirror/serve.py --port 9000 --advertised 192.168.1.10
```

设备源地址填 `http://<本机 IP>:8080/r`。pica 对私有网段默认允许明文 HTTP。
资产首次请求时下载、比对 sha256 后缓存在 `.mirror-cache/`。只依赖 Python 标准库。
