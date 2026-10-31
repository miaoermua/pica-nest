# 镜像

两种镜像只改写索引里的 `download_url`，`sha256` 等字段原样返回，所以设备照常校验它下载到的字节。

## Cloudflare Worker（worker.js）

```bash
npx wrangler deploy --config mirror/wrangler.toml
```

`wrangler.toml` 的 `GITHUB_REPO` / `BRANCH` 指向本仓库。设备源地址填 `https://<worker 域名>/r`。

| 路由 | 内容 | 缓存 |
|---|---|---|
| `/` `/index.json` `/r` `/r/` `/r/index.json` | `r/index.json` | 60 s |
| `/<path>` `/r/<path>` | 入口描述**声明过**的后端索引，`download_url` 改写到本域名 | 60 s |
| `/releases/<tag>/<file>` | 本仓库的 Release 资产 | 索引里有 sha256：按 sha256 缓存 7 天；没有：300 s 且不入缓存 |

- 没在 `r/index.json` 声明的路径一律 404，Worker 不会变成任意 raw 文件的代理。
- 只改写指向本仓库 Release、且文件名与条目一致的地址；其他地址（局域网、别的仓库）原样保留。
- Git 里的索引保留 GitHub 地址，配了其他镜像的设备不受 Worker 影响。

测试：`node --test mirror/worker.test.mjs`（缓存与上游均为桩，不联网）。

## 局域网镜像（serve.py）

```bash
python3 mirror/serve.py                                  # 0.0.0.0:8080，自动探测本机局域网地址
python3 mirror/serve.py --port 9000 --advertised 192.168.1.10
```

设备源地址填 `http://<本机 IP>:8080/r`。pica 对私有网段默认允许明文 HTTP。
资产首次请求时下载、比对 sha256 后缓存在 `.mirror-cache/`。只依赖 Python 标准库。
