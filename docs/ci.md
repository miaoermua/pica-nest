# CI 与运维

## ci/config.json —— 唯一的上游配置

```json
{
  "pica":    { "repo": "miaoermua/pica", "ref": "main" },
  "release": { "repo": "miaoermua/pica-nest", "tag": "latest" },
  "feeds": {
    "immortalwrt": {
      "opkg": "https://downloads.immortalwrt.org/releases/24.10.0/packages/{arch}",
      "apk":  "https://downloads.immortalwrt.org/releases/25.12.2/packages/{arch}"
    },
    "immortalwrt-snapshot": { "apk": "https://downloads.immortalwrt.org/snapshots/packages/{arch}" },
    "openwrt":   { "opkg": "…/releases/24.10.8/packages/{arch}", "apk": "…/releases/25.12.0/packages/{arch}" },
    "fantastic": { "opkg": "…", "apk": "…" }
  },
  "feed_repos": ["base", "packages", "luci", "routing", "telephony"],
  "platforms": { "all": "all", "x86_64": "amd64", "aarch64_generic": "arm64", "…": "…", "mipsel_24kc": "mipsel" }
}
```

| 键 | 作用 |
|---|---|
| `pica.repo` / `pica.ref` | 用哪个 pica 版本编译 pica-pack（分支、tag 或 commit；按 commit 缓存） |
| `release.repo` / `release.tag` | 产物发布到的 Release；`download_url` 由它拼出。`repo` 必须是本仓库，发布前会校验 |
| `feeds.<名字>.<opkg\|apk>` | 软件源的架构目录，`{arch}` 由矩阵替换。**URL 里的版本号就是 ImmortalWrt/OpenWrt 的版本开关** |
| `feed_repos` | 在 feed 下按顺序查找包的子目录 |
| `platforms` | OpenWrt 架构 → 产物文件名里的 platform；新增架构先加在这里 |

### 切换 ImmortalWrt 版本

改 `feeds.immortalwrt.apk` 里的 `25.12.2` 为 `25.12.3`，推送到 main。CI 比较前后两版 config，
**只重建用到 `immortalwrt` 的 apk 包**，其余不动。合并前可以在本地先看结果：

```bash
python3 -m ci.nest plan --all --probe                                  # 全量
python3 -m ci.nest plan apk/adguardhome apk/luci-app-passwall --probe  # 指定
```

某个包在新版本里消失或改名会在 plan 里直接报 “… is not in feed immortalwrt”。
新 feed 想先试一部分包：在 config 里加一个新名字（如 `immortalwrt-2512`），只把那几个包的 `feed` 改过去。

每日的 upstream 工作流会运行 `nest feeds`，报告同系列是否有更新的点版本（例如 24.10.0 → 24.10.6），
**只报告不自动修改**：feed 版本一动就是一批包同时变化，必须人来决定。

## 工作流

| 文件 | 触发 | 做什么 |
|---|---|---|
| `check.yml` | PR；被 build.yml 调用 | 单元测试、Worker 测试、`nest validate`；PR 还会对改动的包做联网 plan（HEAD 每个 URL） |
| `build.yml` | push 到 main（`packages/` `ci/` `r/index.json`）；手动 | 选包 → 编译 pica-pack → `nest build` → 上传 Release → 校验 → 提交 `r/` → 清理旧资产 → 报告 |
| `upstream.yml` | 每天 04:17 UTC；手动 | `nest feeds`；`nest bump --apply` 开/更新 `upstream/bump` PR；全量 plan --probe |

### build.yml 细节

- **选包**：push 时用 `nest changed --base <before> --head <sha>`：`packages/<mgr>/<id>/**` 有改动的包，加上 feed 地址变了的包；
  删除的包从索引里移除。首次推送或 force push 没有可用的 base，全量重建。
- **手动**：`packages` 填 `opkg/ttyd apk/node`，或勾 `all`；`publish` 取消勾选则只构建不发布（产物在 artifact `dist` 里）。
- **部分成功**：失败的包不进入产物，能发布的照常发布，最后的 `report` job 列出失败包并标红。
  上游不可达（限流、超时）会标注 “upstream unreachable”，与“上游确实没有”分开。
- **顺序**：上传资产 → 下载回来比对 sha256 → `nest validate` → 提交 `r/`（`[skip ci]`）→ 删除两代索引都不引用的资产。
  中途失败时 `r/` 保持原样，重跑即可。
- 构建串行（`concurrency: build-main`），每次发布都合并进上一次提交的 `r/`。
- **已发布的文件名不可变**：pica-pack 每次打包都写入 `builddate`，同版本重建会得到同名不同字节的产物。
  这种情况保留已发布的条目和资产，不覆盖（否则持有旧索引的设备会校验失败），并给出 warning。
  改了 scripts/、root/ 或 manifest 想重新发布同一版本，**把 `pkgrel` 加 1**。

### 手动触发（Run workflow）

| 输入 | 说明 |
|---|---|
| Use workflow from | 选 `main` 才会发布；其他分支 / tag 只构建，产物在 artifact `dist` 里保留 7 天 |
| packages | 空格分隔：`opkg/ttyd apk/node`（不要用逗号）。名字写错整次构建直接失败 |
| all | 重建全部包，勾上后忽略 packages；两个都空会报错 |
| publish | 默认勾选。取消则只构建不发布，用来试 feed 新版本或排查问题 |

- 重建的包如果版本没变（同名），不会重新上传，`r/` 也不会变——“全部重建”可以放心点，它只会发布真正有新版本的包。
- 同一时间只跑一个构建。排队时 GitHub 只保留**最新的一个**等待中的运行，更早排队的会被取消；
  连续点了几次只会生效最后一次，被取消的那次需要时再点。
- 有包失败时，其余成功的包照常发布，`report` job 标红并列出失败原因；修好后只需手动重跑失败的那几个包。

## 本地命令

```bash
python3 -m ci.nest validate                       # 全部静态检查（含 Git 二进制拦截）
python3 -m ci.nest plan --all --probe             # 全量解析上游 + HEAD
python3 -m ci.nest build opkg/ttyd --out out --pica-pack ./pica-pack --cache ~/.cache/pica-nest
python3 -m ci.nest index --build out              # 把构建合并进 r/（本地调试用，别提交）
python3 -m ci.nest bump                           # 预览可升级的固定版本包；--apply 写入
python3 -m ci.nest feeds                          # feed 有没有更新的点版本
python3 -m ci.nest config release.tag             # 读配置
python3 -m unittest discover -s ci/tests -t .     # 单元测试
node --test mirror/worker.test.mjs                # Worker 测试
```

只依赖 Python 3.12+ 标准库，无需 pip 安装。

## 限制

- 一个 Release 最多 1000 个资产。当前约 70 个包 × 平均 4 个架构，加上保留的上一代，余量足够；
  包数量翻倍时考虑把 `release.tag` 按后端拆开。
- apk-tools 3.x 容器的大小 pica-pack 读不出，索引里 `size` 为 0（pica 视为“未测量”，不影响安装）。
