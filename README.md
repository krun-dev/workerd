# workerd CPU-budget distribution

基于官方 workerd 的独立 Linux x86_64 发行仓库，增加同进程、每请求 CPU 执行预算。上游作为 Git submodule 固定在一个 commit；自己的代码改动保存在 `patches/`，构建时应用到临时源码目录。

## 仓库结构

```text
upstream/workerd/       官方 submodule，保持干净
patches/series         补丁顺序
patches/*.patch        本发行版代码改动
VERSION               独立发行版本号
scripts/distro.py      prepare / build / test / package / release
docker/Dockerfile.build 固定 Bazel / Clang 主版本的构建环境
tests/                CPU 限额集成测试与两个 Worker 的配置
.github/workflows/    GitHub Actions 校验、构建和 Release
.build/               临时源码、缓存、二进制与测试记录，不提交
dist/                 发布包、构建记录、测试记录和 SHA-256，不提交
```

首版上游为 `v1.20260916.1`，commit `adda2635656d09e541b0feeea796da9d2a8bc10e`，发行版本为 `1.20260916.1-cpu.1`。submodule 的 gitlink 是上游版本的权威来源，不跟随上游 main 自动漂移。

## 构建 Release

需要 Linux x86_64、Git、Python 3.10+、Docker。完整首次构建会编译 V8；建议 16 核、32 GiB 内存并预留至少 60 GiB 磁盘。小机器可降低并行度，构建时间会增加。

```sh
git clone --recurse-submodules <你的发行仓库地址>
cd <发行仓库目录>
WORKERD_BUILD_JOBS=12 WORKERD_BUILD_MEMORY_MB=18000 \
  python3 scripts/distro.py release
```

该命令依次导出上游源码、检查并应用补丁、使用 `--config=release_linux --strip=always` 构建、运行 14 项限额集成检查，全部通过后打包。测试会临时占用本机 18871～18873 端口。CPU 限额默认未开启；测试显式开启预算，并使用默认 10 ms 检查间隔。

也可逐步执行：

```sh
python3 scripts/distro.py prepare
python3 scripts/distro.py build
python3 scripts/distro.py test
python3 scripts/distro.py package
```

`build` 会重新 prepare。`package` 要求发行仓库已提交且干净，并校验当前源码/补丁/构建脚本、构建产物 SHA 与通过测试的二进制一致，防止误打包旧产物。产物包含二进制、上游许可证、补丁、版本和构建/测试记录；`workerd --version` 仍显示上游版本，发行版版本以随包 `VERSION` 和 `build-info.json` 为准。

可用环境变量：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `WORKERD_BUILD_JOBS` | 4 | 编译并行度 |
| `WORKERD_BUILD_MEMORY_MB` | 8000 | Bazel 内存调度预算，非容器硬限额 |
| `WORKERD_BUILD_CACHE` | `.build/cache` | 持久化 Bazel 缓存目录 |
| `WORKERD_BUILD_IMAGE` | 不设置 | 设置后使用已有本地构建镜像，跳过镜像构建 |
| `WORKERD_BUILD_NETWORK` | `bridge` | Docker 构建进程的网络；Linux 上使用宿主机回环代理时设为 `host` |

构建进程会按名称传入已设置的 HTTP(S)/ALL/NO_PROXY 环境变量，不将代理凭证打印到命令日志。若测试机不能直连 GitHub，可使用自己的网络代理；不能把本机回环代理地址直接用于 bridge 网络容器。

当前构建镜像使用 Ubuntu 24.04、Clang 19、Bazel 9.2.0。原生二进制按 glibc 2.39 环境构建，不能承诺兼容 Ubuntu 22.04 的 glibc 2.35。若部署发行版较旧，应在所需最旧用户态环境中构建并验证；内核 6.6 本身具备此补丁需要的接口。镜像的 apt 包未锁定补丁版本，因此记录镜像 ID 便于追溯，但不宣称逐字节可复现。初版不提供 ARM64、npm 包或 OCI 发布。

## GitHub 构建和发布

先创建自己的 GitHub 仓库，再推送本仓库。此模板没有预设远端地址，也不会上传到 Cloudflare 官方仓库。

```sh
git remote add origin <你的发行仓库地址>
git push -u origin main
```

- PR 和 main 推送运行轻量校验：补丁可应用、源码语法检查。
- Actions 的 `Build and release` 支持手动运行，构建并保存可下载的 artifact。
- 推送与 `VERSION` 对应的 `v*-cpu.*` tag，构建和测试通过后自动创建 GitHub Release 并上传发布包与校验文件。
- 可用仓库变量 `WORKERD_RUNNER` 指定 Linux x86_64 大规格 runner 标签；未配置时使用 `ubuntu-24.04`。CI 默认并行度 2、内存调度预算 6000 MiB，可用仓库变量 `WORKERD_BUILD_JOBS` / `WORKERD_BUILD_MEMORY_MB` 调整。
- 持久化 runner 可设置仓库变量 `WORKERD_BUILD_CACHE` 为专用绝对路径，提高后续构建速度；默认临时 runner 每次冷构建。构建任务没有发布权限；发布由单独任务使用 `contents: write`。

```sh
git tag v1.20260916.1-cpu.1
git push origin v1.20260916.1-cpu.1
```

GitHub 自动生成的源码压缩包不包含完整 submodule 内容；开发者应使用 `git clone --recurse-submodules`。GitLab 或其他 CI 也可直接调用同一条 `python3 scripts/distro.py release`，再上传 `dist/`。

## 升级上游

```sh
git -C upstream/workerd fetch origin tag <目标官方tag>
git -C upstream/workerd checkout --detach <目标官方tag>
git add upstream/workerd
python3 scripts/distro.py prepare
```

补丁冲突会立即失败，不会静默跳过。解决方式是在导出的临时源码中适配修改，重新生成相对目标上游的补丁，再运行 prepare。更新 `VERSION`，提交 gitlink、补丁和版本后再 release。无需维护一套 V8 分支。

修改自己的补丁时，可在独立 worktree 中编辑正常的 C++ 文件，避免手工维护 diff 的行号。以下命令在发行仓库根目录运行，适用于当前单补丁布局：

```sh
mkdir -p .build
git -C upstream/workerd worktree add --detach ../../.build/edit-workerd HEAD
git -C .build/edit-workerd apply ../../patches/0001-request-cpu-budget.patch
# 在 .build/edit-workerd 中修改代码；升级上游导致 apply 失败时，先手工适配修改。
git -C .build/edit-workerd add -N src/workerd/server/experimental-cpu-limit.h
git -C .build/edit-workerd diff --binary > patches/0001-request-cpu-budget.patch
python3 scripts/distro.py prepare
```

补丁保存并检查后，提交发行仓库里的补丁和版本变更，再构建。临时 worktree 不属于发行仓库提交内容，上游主工作区也不会被改脏。若引入多个补丁，应逐个维护并按 `patches/series` 顺序验证。

## 使用

```sh
tar -xzf workerd-cpu-1.20260916.1-cpu.1-linux-x86_64.tar.gz
cd workerd-cpu-1.20260916.1-cpu.1-linux-x86_64
WORKERD_EXPERIMENTAL_CPU_MS=50 \
WORKERD_EXPERIMENTAL_CPU_POLL_MS=10 \
./workerd serve /path/to/config.capnp
```

- `WORKERD_EXPERIMENTAL_CPU_MS`：每请求执行线程 CPU 预算，毫秒，默认 0（关闭）。
- `WORKERD_EXPERIMENTAL_CPU_POLL_MS`：检查间隔，默认 10 ms，整数 1～100。
- `WORKERD_EXPERIMENTAL_STARTUP_CPU_MS`：开启限额后，模块启动/相关动态导入的预算，默认 1000 ms。

配置在进程启动前提供，按进程统一配置；不提供热更新或按业务独立配置。超过预算终止执行，不支持暂停后继续。轮询不是硬实时限制；50 ms 预算不保证恰好在 50 ms 截断。只计执行线程 CPU，不包含后台线程全部成本；不提供内存硬隔离或租户公平调度。大量超限请求仍需要入口限流。此补丁保持实验特性身份。

## 参考

- [Git submodule 文档](https://git-scm.com/docs/git-submodule)
- [上游 Release 配置（固定基线）](https://github.com/cloudflare/workerd/blob/adda2635656d09e541b0feeea796da9d2a8bc10e/.bazelrc)
- [GitHub Release 管理](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)
