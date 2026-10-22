# Pica CI 配置中心 (Continuous Integration)

本目录存放 Pica 软件源的**自动化抓取逻辑**。

## 📂 目录结构

*   `opkg/`：针对基于 OpenWrt opkg 管理的应用构建规则。
*   `apk/`：针对基于 OpenWrt apk 管理的应用构建规则。
*   `pica/`：针对不需要系统包管理器、由 Pica 直接铺放 `root/` 的应用构建规则。

目录名就是该 job 的 `mgr`（见各 `build.json` 的 `mgr` 字段），两者必须一致，`validate_repo.py` 会检查。

## ⚙️ build.json 职能

每个子目录下的 `build.json` 都是一个“抓取任务单”：
*   **Source**: 指定从 GitHub 或 HTTP 镜像站抓取什么。
*   **Matrix**: 定义要支持的硬件架构（如 x86_64, aarch64）。
*   **Pack**: 指示 `pica-pack` 如何将这些文件打包。

**修改此处的 JSON 后，GitHub Actions 会自动触发对应的抓取和打包任务。**

## 工作流职责

*   `.github/workflows/validate.yml`：PR 和可复用的配置验证。只做静态检查、单元测试和版本/架构一致性检查，不下载上游资源。
*   `.github/workflows/upstream-check.yml`：定时或手动运行。只查询 Release 元数据并用 HEAD/Range 探测资源是否存在，不下载大文件、不打包、不发布。
*   `.github/workflows/build-and-release.yml`：合并到 `main` 或手动触发。顺序是 Plan（不下载）→ Fetch（消费 Plan）→ Verify → Package → Test；Publish 是独立 Job，只消费构建 Job 上传的已验收产物。

## 版本一致性

`manifest` 的 `pkgver` 必须能被实际下载的资产或 URL 版本证明。`check_versions.py` 在每次构建前检查：

* HTTP 模板 URL 中的版本与 `manifest` 是否一致；
* URL 路径里的架构是否与矩阵条目一致（防止把 x86_64 的包放进 arm64 产物）；
* 固定 GitHub tag 是否与 `manifest` 一致。

上游 feed 确实给不同架构发布了不同版本时，在 `build.json` 的 `versioning.arch_versions` 里逐个架构写明版本；未写明的差异会让检查失败。

定时任务和工作流手动触发时还会运行 `check_versions.py --online`，用上游最新稳定版的资产名核对 `manifest`。上游更新只产生 warning，不阻断构建：是否升级仍由人判断兼容性。
