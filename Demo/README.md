# Demo 发布包

`CoreGeek.tar.gz` 与官方 Demo 使用相同的顶层 `CoreGeek/` 目录结构，包含修改后的源码、启动脚本、说明和测试。源码基线为 `619df62`。

```bash
tar -xzf CoreGeek.tar.gz
bash CoreGeek/run.sh 8081
```

需要 Python 3.11+，没有第三方依赖。详细策略和验证边界见 `CoreGeek/README.md`。

该发布包解压后的15项测试全部通过，内容已逐文件比对 Git 源码，启动脚本保留可执行权限和 LF 换行。

SHA256：`1c41fc1484d0c2851b1e8f5d5126b6c083a7a33b4af1684ebe466c7df97abdf2`

更新源码提交后，在仓库根目录重新打包：

```bash
git -c core.autocrlf=false archive --format=tar.gz --prefix=CoreGeek/ --output=Demo/CoreGeek.tar.gz HEAD:Demo/CoreGeek
```
