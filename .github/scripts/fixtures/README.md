# 校验器的失败样本

这两个目录**故意是坏的**，用来证明 `validate_repo.py` 拦得住"字段都合法、位置错了"的 job。
它们各自是一个最小仓库树（`ci/` + `src/`），不在默认扫描范围里（默认只扫仓库根的 `ci/*/*/build.json`），
所以不会影响 CI；只有下面这些命令会读到它们。

| 目录 | 坏在哪 | 期望报错 |
| --- | --- | --- |
| `dir-mismatch/` | job 放在 `ci/opkg/demo/`，`mgr` 却是 `apk` | `lives in ci/opkg/ but mgr is 'apk'` |
| `manifest-mismatch/` | `mgr` 是 `opkg`，`manifest` 却指向 `src/apk/demo/manifest` | `a job in ci/opkg/demo/ reads 'src/opkg/demo/manifest'` |

复现（在各自目录里跑，路径才是 `ci/<mgr>/<id>/build.json` 的形状）：

```sh
cd .github/scripts/fixtures/dir-mismatch      && python3 ../../validate_repo.py
cd .github/scripts/fixtures/manifest-mismatch && python3 ../../validate_repo.py
```

两者都以非 0 退出。修好任意一个（把目录改名 / 把 `manifest` 指向本后端的副本）它就通过。
