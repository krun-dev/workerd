# workerd

基于官方 [Cloudflare workerd](https://github.com/cloudflare/workerd)，以尽量少的改动满足部署需求。官方源码通过 submodule 固定版本，所需改动以补丁维护。

## 目录结构

```text
upstream/workerd/        官方源码 submodule
patches/                自维护补丁及应用顺序
scripts/                版本管理、构建、测试和打包脚本
docker/                 Linux AMD64 构建环境
tests/                  测试脚本与 Worker 示例
docs/                   实验和验证记录
.github/workflows/      GitHub 检查及 Release 构建流程
VERSION                 上游版本 + krun 修订号
CHANGES.md              版本变更记录
LICENSE                 许可证
.build/                 临时源码、缓存和构建记录（不提交）
dist/                   二进制及打包产物（不提交）
```
