# 软件包

每个包是 `packages/<mgr>/<id>/` 下的一个目录：

```
manifest      pica manifest：身份、依赖、生命周期脚本
build.json    载荷从哪来、构建哪些架构
scripts/      可选，生命周期脚本（install / update / remove，POSIX sh）
root/         可选，按目标路径铺到设备上的文件
```

`<mgr>` 是这个任务**下载容器所属的后端**（`opkg` / `apk`），或 `pica`（无容器，只有 root/）。
manifest 的 `targets` 决定产出哪些产物，必须包含 `<mgr>`；`targets = opkg, pica` 会从同一份输入出两种产物。

## 新建

```bash
python3 -m ci.nest new apk/hello --source feed      # feed | github | url | archive
# 编辑 packages/apk/hello/manifest 与 build.json
python3 -m ci.nest validate                         # 静态检查，不联网
python3 -m ci.nest plan apk/hello --probe           # 解析上游并 HEAD 每个 URL，不下载
python3 -m ci.nest build apk/hello --out out --pica-pack /path/to/pica-pack   # 本地完整构建
```

pica-pack 来自 [miaoermua/pica](https://github.com/miaoermua/pica)：`cargo build --release --bin pica-pack`。

## manifest

格式与字段以 pica 的 [standard.md](https://github.com/miaoermua/pica/blob/main/docs/standard.md) 为准。本源额外要求：

| 规则 | 原因 |
|---|---|
| 必须有 `pkgname` `appname` `os` `targets` | pica-pack 必需字段 |
| 不写 `pkgmgr` `platform` `arch` `builddate` `size` | 由 pica-pack 或矩阵写入 |
| 不写 `visibility`，用 `source_availability = open\|partial\|closed` | 旧字段已废弃 |
| feed / github 包**不写** `pkgver`（跟随上游）；url / archive 包**必须写** | 只有前两者能提供版本 |
| `pkgver` 只用 `A-Z a-z 0-9 . _ + -`，不含 `~` | GitHub 会改写资产名里的 `~` |
| `targets` 含 `pica` 时，依赖写成 `base@opkg = …` | pica 后端不装依赖，裸 `base` 会被拒 |
| 下载的每个容器都要在 `app`/`base`/`kmod`/`app_i18n` 里出现 | 没人声明的容器无法卸载 |
| 已在 feed 里的公共依赖加 `?`（`base = luci-base?`） | 必选名字命中 feed 会让整次安装改走 feed |

命名：`pkgname` 是本源身份，`appname` 是用户敲的名字。只有二进制的写 `xxx-bin`，第三方版本写 `xxx@作者`，
`luci-app-` 前缀不进 `pkgname`/`appname`。

## build.json

编辑器会按 `$schema`（`ci/schema/build.schema.json`）补全。顶层：

| 字段 | |
|---|---|
| `matrix` | 构建的 OpenWrt 架构，取自 `ci/config.json` 的 `platforms`；与架构无关的包写 `["all"]` |
| `source` | 载荷来源，四种之一，见下 |
| `upstream` | 可选，url / archive 包的版本来源：`"github:owner/repo"`，供 `nest bump` 检查新版本 |

### feed — 从 ImmortalWrt / OpenWrt 等软件源按包名取

```json
{
  "matrix": ["x86_64", "aarch64_generic", "aarch64_cortex-a53", "aarch64_cortex-a72", "mipsel_24kc"],
  "source": {
    "type": "feed",
    "feed": "immortalwrt",
    "packages": ["adguardhome", "luci-app-adguardhome"],
    "optional": ["luci-i18n-adguardhome-zh-cn"],
    "version_from": "adguardhome"
  }
}
```

- `feed` 是 `ci/config.json` 里 `feeds` 的名字，地址按 `<mgr>` 选 opkg 或 apk 那一条。
- 每个架构读 feed 自己的索引（opkg `Packages`，apk `index.json` + 目录列表），在 `feed_repos`（base、packages、luci…）里按顺序找，
  不用关心包在哪个子目录；opkg 索引里的 sha256 与大小会用于校验下载。
- `version_from` 决定 pkgver 取哪个包的版本，默认 `packages` 的第一个。
- apk 的 ABI 包（`node` 实际文件是 `node127-*.apk`）两种名字都能写：写 `node` 跟随 ABI 升级，写 `node127` 固定 ABI。

### github — Release 资产

```json
{
  "matrix": ["x86_64", "aarch64_generic", "mipsel_24kc"],
  "source": {
    "type": "github",
    "repo": "SunBK201/UA3F",
    "tag_regex": "^v?[0-9]+(?:\\.[0-9]+)*$",
    "arch_map": { "aarch64_generic": "aarch64" },
    "assets": [
      { "match": "^ua3f_[0-9].*-r?[0-9]+[_-]([A-Za-z0-9_+.-]+)\\.ipk$" },
      { "match": "^luci-app-ua3f_.*_all\\.ipk$", "optional": true }
    ]
  }
}
```

- 选 Release：`tag`（精确，可写 `v{version}`）或 `tag_regex`（最新匹配的稳定版），都不写则取最新稳定版；`prerelease: true` 允许预发布。
- `match` 是资产名正则，`{version}` 会替换为 manifest 的 pkgver。**一个捕获组 = 架构**，经 `arch_map` 反查矩阵；没有捕获组的资产放进每个架构。
- `unzip`：资产是 zip 时，解出名字匹配该正则的成员。
- 写了 pkgver 时，tag 或资产名必须带这个版本，否则构建失败并提示“上游已更新”——由 `nest bump` 开 PR 升级。

### url — 固定地址的容器

```json
{ "matrix": ["all"], "source": { "type": "url", "urls": ["https://istore.istoreos.com/repo/all/store/luci-app-store_0.2.1-r1_all.ipk"] } }
```

`{arch}` `{platform}` `{version}` 会被替换。能用 feed 的包不要用 url。

### archive — 压缩包或单个二进制，解到 root/

```json
{
  "matrix": ["x86_64", "aarch64_generic", "mipsel_24kc"],
  "upstream": "github:tailscale/tailscale",
  "source": {
    "type": "archive",
    "url": "https://pkgs.tailscale.com/stable/tailscale_{version}_{arch}.tgz",
    "arch_map": { "x86_64": "amd64", "aarch64_generic": "arm64", "mipsel_24kc": "mipsle" },
    "strip": 1,
    "include": ["tailscaled", "tailscale"],
    "dest": "usr/sbin"
  }
}
```

| 字段 | |
|---|---|
| `strip` | 去掉前几层目录（`name-ver-arch/…` 写 1） |
| `include` | 只保留这些路径前缀（不带手册、补全等） |
| `rename` | 结果只有一个文件时改名并设为可执行（裸二进制下载用它） |
| `dest` | 放到 `root/` 下的哪个目录 |

每个架构解到各自的 root/，所以 pica 包可以多架构。

## 构建时的检查

`nest build` 对每个包：plan（解析上游）→ 下载（校验长度与 sha256）→ 读取容器元数据 → 打包。以下任一情况该包失败、其他包继续：

- 容器实际安装名没有在 manifest 里声明；容器架构与矩阵不符；
- 写了 pkgver 但容器版本对不上；`app = luci-*` 声明了但没有容器提供；
- pica-pack 没有产出某个 target × 架构，或产物名不能作为 Release 资产。

apk-tools 3.x（ADB）容器的元数据只能读出名字与版本；读不出的记为“未校验”，不当作通过。

## pica-maker-skill

[pica-maker-skill](https://github.com/miaoermua/pica-maker-skill) 按本布局编写：`SKILL.md` 是加包流程，
`references/habits.md` 收录 CI 里出现过的报错与原因，`scripts/feed_lookup.py` 用本仓库的解析器按架构查询 feed：

```bash
python3 ../pica-maker-skill/scripts/feed_lookup.py --nest . --mgr apk qbittorrent luci-app-qbittorrent
```
