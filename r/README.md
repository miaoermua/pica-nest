# Pica 仓库成品库 (Repository Registry)

本目录只存放 CI 自动化生成并发布的 **JSON 索引与元数据**。

## 📄 repo.json

这是仓库的“核心地图”。它包含了：

*   所有可用软件包的列表。
*   每个包的版本、架构信息。
*   包内容的 SHA256 哈希值（用于安全校验和智能缓存）。
*   每个包的 `download_url`，指向该包在 GitHub Releases 中的资产地址。

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

*   **GitHub**：`repo.json` 与 Releases 资产即全部内容，客户端可直连。
*   **Cloudflare Worker**：代理 `repo.json`，并按 Release 资产分发文件（利用 Cloudflare 缓存）。
*   **自有服务器**：拉取最新 Release 并本地缓存，按现有策略淘汰旧版本。

**警告：请勿手动修改此目录下的文件，因为它们在每次 CI 运行后都会被自动覆盖。**
