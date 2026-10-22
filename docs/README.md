# Pica 软件源架构与核心说明

本文档是 Pica 官方/第三方应用软件源的架构总览与构建标准说明。本软件源设计的核心目标是：**以 GitHub 发行为主体，辅以 Cloudflare Worker 及服务器部署缓存，实现全球高可用、低成本的分布式静态分发网络**。

---

## 1. 软件源结构设计

仓库被严格划分为以下主要功能目录，保证了源码维护、自动构建与发布产物的彻底隔离。

### `src/`：软件包源码定义区 (面向开发者/维护者)
**功能描述：** 这里是整个源仓库的唯一事实来源 (Single Source of Truth)。所有软件包的元数据、编译和挂载脚本都在这里被定义。
- 采用 `<分类>/<包名>` 的双层嵌套结构（例如 `src/opkg/adguardhome/`）。
- **`manifest`**：定义包的基础信息（版本、架构、引用关系）。
- **`scripts/`**：生命周期脚本（`install` / `update` / `remove`），由 Pica 在装卸时执行，并按需保留到 `/usr/lib/pica/scripts/<pkgname>/`。
- **`root/`**：按目标路径镜像安装的载荷。`root/etc/init.d/hello` 会装到 `/etc/init.d/hello`，不依赖任何包管理器。
- **`pkgs/<backend>/<platform>/<arch>/`**：交给后端安装的 `.ipk` / `.apk` 容器，由 `fetch_assets.py` 从上游抓取后落在这里。

### `ci/`：自动化构建控制区 (面向 DevOps)
**功能描述：** 存放用于生成发布产物、聚合索引的各类工具脚本。
- **`.github/scripts/gen_repo_index.py`**：核心聚合脚本。用于每次 CI 完成打包后，提取包清单信息，自动维护与更新全局无历史状态的 `r/repo.json` 索引库。
- **`.github/scripts/check_no_binaries.py`**：硬性闸门。任何 `.pkg.tar.gz` / `.ipk` / `.apk` / `.adb` 出现在待提交内容或已跟踪文件中，直接使任务失败。

### `r/`：代理发布点与静态索引库 (最终的发行终点)
**功能描述：** “Repository”的缩写。这是向外暴露、真正被客户端所请求的源路径起点。**本目录只保存 JSON 元数据，不保存任何二进制。**
- **`repo.json`**：极度精简的列表索引。每次有新包入库，旧版本的同名包定义即被彻底抛弃和覆盖，以此保证设备在解析配置时的极低资源占用。
- 其中的 `filename` 被强制“扁平化”（去掉所有路径分割符），它只是 Release 资产的**文件名**，不是仓库内的路径。
- **`download_url` 是下载路径的唯一来源。** 客户端在 `download_url` 为空时会回退到 `<软件源>/packages/<filename>`；一旦索引漏填该字段，为满足这条回退就只能把归档提交进 Git —— 这正是历史上 `r/packages/` 出现、仓库膨胀到 GB 级的原因。CI 现在强制每个条目都带指向 Releases 的 `download_url`，该回退路径在正常分发中不再被触发，`r/packages/` 已废弃。

---

## 2. 自动化流水线 (`CI` 处理机制)

软件源的每次更新，都借由 GitHub Actions 做自动化托管，人工零干预：
1. **触发检测**：任何开发者针对 `src/` 中的内容提交 PR 或 Push 动作，会被 GitHub Actions (基于 `.github/workflows/build-and-release.yml`) 立即监听到。
2. **矩阵构建**：CI 使用 `pica-pack` 工具，只针对本次增量变更的包执行构建打出 `.pkg.tar.gz` 固件。
3. **产物校验**：`.github/scripts/verify_artifacts.py` 比对归档内嵌 `manifest`、SHA256、载荷，并校验 `download_url` 与发布基址一致。
4. **发行主权**：先把编译好的离线包上架到 GitHub Releases（快照标签 + 累积的 `latest` 标签），再 `HEAD` 探测确认资产可下载；**只有资产确认存在后**，才把带 `download_url` 的 `repo.json` 反向 Commit 进源码库。上传失败则索引保持原样，不会出现指向空地址的 JSON。
5. **入库闸门**：`.github/scripts/check_no_binaries.py` 在提交前拦截，二进制归档永远不进入版本库。

---

## 3. 分发与代理节点架构存储

我们的架构奉行“白嫖全球 CDN”的分布式网络原则。分发路线与资源所在位置如下：

### 主体存储 (GitHub Releases)
- **存放内容**：所有打包完毕的 `<pkg>.pkg.tar.gz` 实体文件。
- **优势归属**：无需承担高昂的存储费用和原生出口带宽问题。

### 边缘缓存与加速 (Cloudflare Worker代理)
实际部署入口是 `worker/index.js`（见 `wrangler.toml` 的 `main`）；`worker/worker.js` 是早期版本，仅作参考，不要部署。
- 作为国内客户端连接的桥梁（如路由器配置 Pica 软件源指向：`https://<your-worker-domain>/r`）。
- **索引投递**：`/repo.json` 与 `/r/repo.json` 均从 GitHub Raw 取回，带 60 秒缓存。
- **资源分发**：`/latest/<file>` 与 `/releases/<tag>/<file>` 代理到对应 Release 资产，缓存键混入索引中的 `sha256`，因此同一文件名的不同版本不会串缓存；命中索引时按 7 天 `immutable` 缓存，未命中时只缓存 300 秒。
- **不依赖 Git 中的二进制**：Worker 只从 Releases 取文件，与 `r/` 目录里是否有归档无关。

### 服务器部署缓存 (可选进阶)
针对特定环境或国内严重阻断的异常情况，允许补充自建服务器：
通过 Caddy/Nginx 对该源仓库或镜像进行简单的主机静态反代，或挂载定时 rsync 爬取任务拉走 GitHub Releases 的实体文件做局域网私服部署加速。此时 `src/` 到 `r/` 这套统一的标准体系能够保证任何节点只要遵循 `r/repo.json` 的映射，它就能完美被挂载！

---

## 4. 如何提交新的应用 (贡献指南)

如果你想向 Pica 软件源提交新的软件包，请遵循以下简单流：

1. **Fork 本仓库**：将当前软件源仓库 Fork 到你自己的 GitHub 账号下。
2. **规范目录**：在 `src/<类别>/` (例如 `src/opkg/` 或 `src/mgr/`) 下新建你的应用包文件夹，例如 `my_app`。
3. **编写数据清单**：在 `src/<类别>/my_app/` 内，必须创建 `manifest` 声明文件，包含包的基础信息。
4. **添加脚本 (可选)**：根据需要提供生命周期脚本，例如 `scripts/install`。
5. **提交 Pull Request**：将你添加的包推送到你的 Fork 分支，并向官方仓库提交 PR。
   *一旦你的 PR 被管理员合并入主分支，这套系统里的 GitHub Actions CI 便会自动接管。它会提取你新加入的应用，为其生成 Release 二进制包，更新 `r/repo.json` 的中心索引。短短几秒钟后，全网客户端即可安装你的新应用！*
