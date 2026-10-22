# Pica 仓库成品库 (Repository Registry)

本目录只存放 CI 自动化生成并发布的 **JSON 索引与元数据**。

## 🗂 目录结构

```text
r/
├── index.json          入口描述：名称、schema、各后端索引的相对路径
├── repo.json           schema 2 统一索引 —— 现有 pica 客户端读的就是这一份
├── apk/index.json      repo.json 中 apk 部分的视图
├── opkg/index.json     repo.json 中 opkg 部分的视图
└── pica/index.json     repo.json 中 pica 部分的视图
```

**只有一个数据来源。** `repo.json` 由每次构建的真实产物生成，`index.json` 与三个后端索引
都在同一次运行里由它派生（`.github/scripts/repo_layout.py`）。它们不是第二份清单，不能手工维护：
`validate_index_layout.py` 会在两者不一致时直接失败。

某个后端一个包都没有时，它既不会有文件，也不会被 `index.json` 声明 —— 空索引等于声明了一个
本软件源并不提供的后端。

## 📄 index.json

入口描述，告诉客户端「这个源提供哪些后端、各自的索引在哪」：

```json
{
  "schema": 1,
  "name": "Pica 官方社区软件源",
  "managers": {
    "apk": { "path": "apk/index.json" },
    "opkg": { "path": "opkg/index.json" },
    "pica": { "path": "pica/index.json" }
  }
}
```

`path` **相对于本目录**，不含协议、主机或前导斜杠。这样同一棵树放在 GitHub raw、Cloudflare
Worker 或自建镜像后面都不需要改写 —— 客户端拿自己配置的根地址去拼。

本文件只描述「有什么、在哪」，**不写下载入口、缓存 TTL 或服务器策略**：那些属于分发层，
写进来就等于把镜像钉死在某个域名上。

## 📄 repo.json

这是仓库的“核心地图”，也是 **schema 2** 的既有格式，客户端依赖的字段一个没动：

*   所有可用软件包的列表。
*   每个包的版本、架构信息。
*   包内容的 SHA256 哈希值（用于安全校验和智能缓存）。
*   每个包的 `download_url`，指向该包在 GitHub Releases 中的资产地址。

条目身份是 `(pkgmgr, pkgname, platform, arch)` —— 同一软件在五个架构下各有自己的产物与文件名。

## 📄 各后端索引

与 `repo.json` 的条目逐字相同，只是按 `pkgmgr` 分组：

```json
{
  "schema": 1,
  "mgr": "opkg",
  "generated_at": "…",
  "packages": [ … ]
}
```

字段不做裁剪：只拿到后端索引、没有 `repo.json` 的客户端，仍然要能靠 `filename`、`sha256` 和
`download_url` 解析出一个可下载的资产。

## 📦 二进制在哪里

**本目录（以及整个 Git 仓库）不保存 `.pkg.tar.gz` 产物。**

二进制归档发布于 GitHub Releases 的 `latest` 标签下，`repo.json` 中的 `download_url` 直接引用该资产：

```
https://github.com/miaoermua/pica-pkg-repo/releases/download/latest/<filename>
```

这一点很重要：`pica` 客户端在 `download_url` 为空时会回退到 `<软件源地址>/packages/<filename>`。如果索引缺少
`download_url`，为了满足该回退路径就只能把归档提交进 Git —— 那正是 `r/packages/` 曾经出现的原因，也是本仓库
体积膨胀到 GB 级的原因。现在由 CI 强制保证 `download_url` 非空，并由
`.github/scripts/check_no_binaries.py` 阻止任何产物进入版本库。

客户端因此不再需要 Git 中的二进制副本；`r/packages/` 已废弃，请勿重新创建。

## 🌐 分发说明

同一套产物通过三条渠道分发，它们只是不同的缓存/接入路径，不各自保存副本：

*   **GitHub**：`index.json`、`repo.json`、各后端索引与 Releases 资产即全部内容，客户端可直连。
*   **Cloudflare Worker**：代理全部索引，并按 Release 资产分发文件（利用 Cloudflare 缓存）。
    `/repo.json`、`/r/repo.json`、`/latest/<file>`、`/releases/<tag>/<file>` 保持不变；
    新增 `/index.json`、`/r/index.json` 与后端索引路由。
*   **自有服务器**：拉取最新 Release 并本地缓存，按现有策略淘汰旧版本。

**警告：请勿手动修改此目录下的文件，因为它们在每次 CI 运行后都会被自动覆盖。**
