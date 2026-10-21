# Pica CI 自动化工具文档

这是 Pica 软件包仓库的核心自动化引擎，负责从全球各地的上游源抓取二进制文件，并将其转换为标准 Pica 格式。

## 🛠️ 脚本角色说明

### 1. `fetch_assets.py` (资源抓取器)
它是仓库的“采购员”，负责寻找、匹配并下载 `.ipk` 或 `.tar.gz` 二进制资源。

*   **GitHub Provider**: 通过 GitHub API 抓取指定 Release 各架构的资产。
*   **HTTP Template Provider**: 通过 URL 模板（如北大镜像站）批量拼接下载链接。
*   **落位规则**: 抓到的资产放入 `pkgs/<mgr>/<platform>/<arch>/`，其中 `<mgr>` 取自 build.json 的 `mgr`，需与 manifest 的 `targets` 一致。

### 2. `gen_repo_index.py` (索引聚合器)
它是仓库的“索引师”，负责在每个 CI 周期完成后，扫描 `build_output` 下的所有包，合并生成单一的 `repo.json` 索引文件。

### 3. `changed_packages.py` (变更映射器)
它回答“这次提交要重建哪些包”：`src/<mgr>/<pkg>/**` 与 `ci/<mgr>/<pkg>/build.json`
（含新增、删除、重命名两侧）都映射到对应包；`schema/`、`.github/scripts/*.py`、
`.github/workflows/**` 只触发全量**校验**，不会重新打包全部包；无法解析 diff 基准时
显式失败或安全全量。映射逻辑的测试：`python3 .github/scripts/test_changed_packages.py`。

---

## 📂 build.json 配置指南

每个应用在 `ci/opkg/<app_id>/build.json` 中定义其抓取逻辑。

### 示例 A：GitHub 抓取模式
```json
"source": {
  "provider": "github",
  "repo": "user/repo",
  "release": {
    "tag": { "regex": "^v?[0-9.]+$" },
    "assets": [
      {
        "regex": "^app_.*_(x86_64|aarch64)\\.ipk$",
        "capture_to": { "arch": 1 }
      }
    ]
  }
}
```

### 示例 B：HTTP 模板模式 (如镜像站)
```json
"source": {
  "provider": "http_template",
  "version": "1.2.3",
  "url_template": "https://mirrors.com/path/{arch}/file_{version}_{arch}.ipk"
}
```
*   **{arch}**: 会自动被 `build.matrix` 里的架构替换。
*   **{version}**: 会自动被 `source.version` 替换。

---

## 🚀 仓库结构规范

1.  **src/<backend>/<app>/manifest**: 软件包的身份信息元数据，用 `targets` 声明构建目标。
2.  **src/<backend>/<app>/scripts/ [install|update|remove]**: 软件包的生命周期脚本（可选）。
3.  **src/<backend>/<app>/root/**: 按目标路径镜像安装的载荷（可选）。
4.  **ci/<backend>/<app>/build.json**: 软件包在 CI 周期中的自动化规则，字段见 `schema/ci-job-v1.schema.json`。
5.  **r/repo.json**: `pica -S` 拉取的索引（schema 2），产物同时位于 `r/packages/`。

---

## ✅ 本地校验

改动配置后先跑校验，比等CI 报错快得多：

```bash
python3 .github/scripts/validate_repo.py
```

它会用 `schema/` 下的 JSON Schema 检查全部 `build.json` 与 `r/repo.json`；
未安装 `jsonschema` 时自动降级为内置检查，仍能拦住会真正导致构建失败的项
（`mgr` 非法、`include_dirs` 用了旧目录名、manifest 声明了 `pkgmgr`、
`targets` 与 `mgr` 不一致等）。

另有两条与文件位置有关的检查，**两种模式下都会跑**（Schema 看不到文件在哪个目录）：

* `ci/<mgr>/<id>/build.json` 的目录名必须等于 `mgr`；
* `manifest` 必须正好是 `src/<mgr>/<id>/manifest`。

`.github/scripts/fixtures/` 下有两个故意写坏的样本，用来验证这两条确实会失败，
用法见该目录的 `README.md`。

## 📐 四条铁律

1. **manifest 里不要写 `pkgmgr`**，那是 `pica-pack` 写进产物的字段。
   源manifest 里出现会被直接拒绝，请用 `targets`。
2. **`build.json` 的 `mgr` 必须与 manifest 的 `targets` 一致**，
   否则 `pica-pack` 报 `pkgs/<backend>/ present but <backend> is not in targets`。
3. **目录名就是后端名**：job 放在 `ci/<mgr>/`，包放在 `src/<mgr>/`。
   目录与 `mgr` 不一致时，workflow 会按目录找 job，产物却按 `mgr` 落位。
4. **`manifest` 指向本后端自己的那份**：`src/<mgr>/<id>/manifest`，
   不要指向另一个后端的副本。
