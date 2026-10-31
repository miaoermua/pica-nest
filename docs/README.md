# 架构

```
packages/<mgr>/<id>/  ──plan──▶  上游（feed / GitHub Release / URL）
        │                           │ 下载、校验（sha256、容器元数据）
        │                           ▼
        └──────────────▶  stage/<mgr>/<id>/<arch>/   每个架构一个 pica-pack 输入目录
                                    │ pica-pack build
                                    ▼
                         dist/<backend>/*.pkg.tar.gz + index.json
                                    │ nest index
                       ┌────────────┴────────────┐
                       ▼                         ▼
         GitHub Release（ci/config.json     r/<mgr>/index.json
         里的 release.tag，默认 latest）    （entry 的 download_url 指向 Release）
```

## 三条约束

1. **Git 里只有元数据。** 产物只进 Release。`nest validate` 拒绝任何被跟踪的归档或大于 512 KB 的文件，
   `.gitignore` 也忽略 `*.pkg.tar.gz` / `*.ipk` / `*.apk`。旧仓库 1.38 GB 的历史就是把产物提交进 Git 造成的。
2. **一个包一个目录。** manifest、build.json、scripts/、root/ 放在一起，不再拆成 `src/` 与 `ci/` 两棵树。
3. **上游位置只写一处。** 软件源（feed）的地址与版本只在 `ci/config.json`；包的 build.json 只写 feed 名称与包名。
   ImmortalWrt 换版本 = 改一行，CI 只重建受影响的包。

## r/：索引

`r/index.json` 是**手写**的入口描述，决定源名称、描述，以及每个后端的索引路径：

```json
{
  "schema": 1,
  "name": "Pica 官方社区软件源",
  "description": "Pica Official Community Software Source",
  "managers": {
    "apk": { "path": "apk/index.json" },
    "opkg": { "path": "opkg/index.json" },
    "pica": { "path": "pica/index.json" }
  }
}
```

- 改 `path`（如 `v2/opkg.json`），下次 `nest index` 会把条目搬过去并删除旧文件；路径规则与客户端一致（相对路径、只用 `A-Z a-z 0-9 . _ -`）。
- 删掉某个后端，而 `packages/` 里还有该后端的包，`nest validate` 会报错。
- 后端索引由 CI 生成，格式即 pica 的 schema 1 后端索引；条目来自 pica-pack 写出的索引，CI 只补 `download_url`。
- 内容不变时不改写文件，所以没有变化的构建不会产生提交。

客户端只读 `index.json` 与它声明的后端索引。旧仓库的 `repo.json`（schema 2）已删除。

## Release

产物全部上传到 `ci/config.json` 里 `release.tag` 指定的 Release（默认滚动的 `latest`），
文件名即索引里的 `filename`。发布顺序：上传资产 → 下载回来逐个比对 sha256 → 提交 `r/` →
删除新旧两代索引都不再引用的资产（保留上一代，刚同步过的设备仍能下载）。

## 版本

- **feed / github 包不写 pkgver**：版本取自上游（feed 的包版本、Release 的 tag），每个架构单独取。
  feed 对不同架构的重建进度不同（例如 adguardhome 在 x86_64 是 0.107.79，在 aarch64 是 0.107.76），
  每个架构的产物带自己的版本，不需要手工维护 `arch_versions`。
- **url / archive 包必须写 pkgver**：URL 里用 `{version}`，升级只改 manifest 一行。
- 发布用的版本会规范化：`~` → `.`（GitHub 会改写资产名里的 `~`，导致 404）、去掉 feed 的 `-rN` 修订号、`-` → `_`。
  `pkgrel` 是本源的打包修订号。

## 与旧仓库（pica-nest-old）的差异

| 旧 | 新 |
|---|---|
| `src/<mgr>/<id>/manifest` + `ci/<mgr>/<id>/build.json` | `packages/<mgr>/<id>/{manifest,build.json}` |
| `.github/scripts/` 下 22 个脚本（约 8000 行） | `ci/nest/` 一个包，`python3 -m ci.nest <命令>` |
| http_template 里写死 feed URL 与文件名，每个包各写一份 | `"type": "feed"` 只写包名；feed 地址在 `ci/config.json` |
| `arch_url_templates` / `versioning.arch_versions` 手工追各架构版本 | 每个架构自动解析各自版本 |
| 版本 `26.1~abc` 发布后改名为 `26.1.abc`，索引与资产不一致 | 构建前就规范化，索引与资产同名 |
| `r/repo.json` + 三份视图，需校验一致 | 只有后端索引，入口由 `r/index.json` 决定 |
| 一个包失败，整次发布失败 | 每个包独立；能发布的照常发布，最后报告失败 |
| manifest 里 `platform`/`arch`/`visibility`/`source` | 由矩阵写入 / 改名 `source_availability` / 删除 |
| tarball 只能单架构 | archive 每个架构独立 root/，pica 包可多架构 |
| `worker/index.js` + `worker/worker.js`（旧副本）+ `scripts/serve_mirror.py` | `mirror/worker.js` + `mirror/serve.py` |

迁移时顺带修复的问题：`leigod-acc` 的 `visibility = mix`（pica 不接受，改为 `partial`）；
`fastfetch` 的矩阵写的是 `amd64`（不是 OpenWrt 架构名）；`apk/node` 的 feed 文件实际叫 `node127-*.apk`；
`apk/qbittorrent` 在 ImmortalWrt 25.12.2 没有 `aarch64_generic` 构建；`luci-theme-argon` 上游已发 3.1.1；
`cattools` 多带了一份从未使用的 68 KB 脚本副本。
