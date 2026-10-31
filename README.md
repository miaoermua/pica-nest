# Pica nest

Pica 社区软件源。仓库只保存**元数据**：软件包定义在 `packages/`，索引在 `r/`，
构建产物（`.pkg.tar.gz`）只发布到 GitHub Release，永远不进 Git。

## 客户端配置

软件源地址填 **`r/` 目录**，客户端自己读 `r/index.json` 再按它找各后端索引：

| 方式 | 地址 |
|---|---|
| GitHub raw | `https://raw.githubusercontent.com/miaoermua/pica-nest/main/r` |
| Cloudflare Worker | `https://<你的 worker 域名>/r`（部署见 [`mirror/`](mirror/README.md)） |
| 局域网镜像 | `http://<机器 IP>:8080/r`（`python3 mirror/serve.py`） |

```bash
pica setup     # 向导：填地址，自动与本机后端求交集
pica -S        # 同步
```

或手写 `/etc/pica/pica.json`：

```json
{
  "repos": [
    { "name": "pica-nest", "url": "https://raw.githubusercontent.com/miaoermua/pica-nest/main/r", "backends": ["opkg", "pica"] }
  ]
}
```

opkg 设备填 `["opkg", "pica"]`，apk 设备填 `["apk", "pica"]`。

## 目录

```
packages/<mgr>/<id>/      一个软件包一个目录：manifest + build.json (+ scripts/ root/)
r/index.json              入口描述（手写）：源名称、描述、各后端索引的路径
r/<mgr>/index.json        后端索引（CI 生成，勿手改）
ci/config.json            唯一的上游配置：pica 版本、发布 Release、软件源（feed）地址
ci/nest/                  构建工具（python3 -m ci.nest）
ci/schema/ ci/templates/  build.json 的 JSON Schema 与新包模板
mirror/                   Cloudflare Worker 与局域网镜像
docs/                     文档
```

## 文档

- [docs/README.md](docs/README.md) — 架构、数据流、发布与索引
- [docs/packaging.md](docs/packaging.md) — 添加/维护软件包（manifest、build.json）
- [docs/ci.md](docs/ci.md) — 工作流、`ci/config.json`、切换 ImmortalWrt 版本、日常运维
- [mirror/README.md](mirror/README.md) — Worker 与局域网镜像

## 声明

部分应用打包自 ImmortalWrt / OpenWrt 官方软件源及各上游项目的发行版，版权归原作者所有。
