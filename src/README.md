# Pica 软件包源码 (Package Source)

本目录是 Pica 软件源的**元数据定义库**。这里不存放二进制大文件，只存放应用的“身份证”。

## 📦 核心组成

每个应用目录包含：

1.  **manifest**: 核心元数据（版本、架构、依赖、安装说明）。
2.  **scripts/**：生命周期脚本（`install` / `update` / `remove`），可选。
3.  **root/**：按目标路径镜像安装的载荷，可选。
4.  **pkgs/<backend>/**：交给后端安装的容器，由 CI 抓取上游后生成，源码区通常不放。`backend` 只能是 `opkg` 或 `apk`。

## 声明构建目标

manifest 用 `targets` 声明要构建哪些后端的产物，**不要写 `pkgmgr`**——那是 `pica-pack` 写进产物的字段，源manifest 里出现会被拒绝：

```ini
targets = opkg
targets = apk
```

`targets` 与 `ci/<backend>/<id>/build.json` 的 `mgr` 必须一致，否则构建会报
`pkgs/<backend>/ present but <backend> is not in targets`。

依赖（`app` / `base` / `kmod`）非空时不能用 `pica` 后端——它不做依赖求解，
必须交给 `opkg` 或 `apk` 安装。

## 依赖版本约束

`app` / `base` / `kmod` 的每一项都可以带版本约束，语法与 `docs/standard.md` 一致
（`name`、`name>=ver`、`name<=ver`、`name>ver`、`name<ver`、`name=ver`）：

```ini
base = curl>=7.0
base = jq
kmod = kmod-tun=0.1
```

约束只由 pica 在安装、升级时校验，**不会出现在 opkg/apk 的命令行上**——pica 永远只用裸包名
请求后端。版本比较用 `pkgver_cmp`，`1.2-1` 高于 `1.2`。需要精确版本请把容器放进
`pkgs/<backend>/`，由后端按路径安装。

约束会直接作用于用户机器，写之前先确认下限：`>=` 写高了会让老设备在预检阶段被
`E_VERSION_INCOMPATIBLE` 拒绝安装。

`pica` 后端也不收容器：`pkgs/pica/` 会让构建直接失败。pica 不解析 `.ipk`/`.apk`，
展开容器会把文件的所有权从后端数据库里抢走，也丢掉后端的依赖求解。需要容器里的文件时，
在 CI 里把它抽成 `root/` 后再打包，或改回 `targets = opkg` / `targets = apk`。

## 🛠️ 如何添加新包？

1.  在 `src/<backend>/` 下创建目录，名字即包 id。
2.  编写 `manifest`，其中 `targets` 声明构建目标。
3.  在 `ci/<backend>/<id>/build.json` 中声明上游来源与构建矩阵。
4.  本地校验：`python3 .github/scripts/validate_repo.py`。
5.  本地试构建：`pica-pack build src/<backend>/<id> --outdir /tmp/out`。

字段定义见 `schema/ci-job-v1.schema.json`（编辑器会自动补全）。

**当 `pica-pack` 运行时，它会读取此处的 manifest 来生成最终提供给路由器的安装包。**