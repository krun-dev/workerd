# workerd

基于官方 [Cloudflare workerd](https://github.com/cloudflare/workerd)，以尽量少的改动满足部署需求。官方源码通过 submodule 固定版本，所需改动以补丁维护。

## 已实现能力

资源限制扩展集中在 `server` 宿主层，复用 workerd 的限额与生命周期接口，不修改 V8、JSG 或 I/O 核心实现。CPU 和内存预算均为实验性能力，默认关闭，可以单独或同时启用。

| 能力 | 行为 |
| --- | --- |
| 请求 CPU 预算 | 按请求执行上下文累计执行线程的 CPU 时间，不计入等待 I/O 或其他 Worker 执行的时间；超限后中断当前 JavaScript 执行。它限制累计 CPU 时间，不限制 CPU 使用百分比。 |
| 启动 CPU 预算 | 限制模块初始化及动态导入阶段的 JavaScript CPU 时间，避免初始化死循环。 |
| isolate 内存预算 | 按 V8 报告的 JS 堆与外部内存计量，包括 ArrayBuffer backing store；同一 isolate 的并发请求共享额度，各 isolate 分别限制。 |
| 内存超限延迟销毁 | 旧实例停止接单，允许在途请求和后台任务在宽限期内完成，完成后提前清理；到期或内存继续增长超过紧急阈值时强制终止。没有后续请求也会清理旧实例。 |
| 按需重新加载 | 内存超限实例清理后，后续请求通过原配置创建新实例，重新初始化全局状态；退出期间的新请求等待，并发请求共享一次重建。不会自动重放失败请求。 |
| 连接与资源清理 | 强制取消在途任务、后台任务和可撤销连接，释放入口引用与 isolate；支持普通 HTTP、流式响应、WebSocket 和 service binding `fetch()` 的相关路径。 |
| Linux AMD64 构建与发版 | GitHub Actions 构建、测试后发布 `workerd-linux-64.gz`；版本沿用上游版本并追加 `-krun.N`，同一上游版本递增，更换上游版本后从 `-krun.0` 开始。 |

以上描述当前 `main` 的能力。内存预算尚未包含在 `v1.20260920.1-krun.0` 的已发布二进制中；各版本包含的改动见 [CHANGES.md](CHANGES.md)。

## 配置与运行

环境变量对进程中各 Worker 使用相同的预算配置，但分别计量；不是整个进程共用一个 CPU 或内存额度。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WORKERD_EXPERIMENTAL_CPU_MS` | `0` | 请求 CPU 预算，单位 ms；`0` 关闭，最大 `3600000`。 |
| `WORKERD_EXPERIMENTAL_STARTUP_CPU_MS` | `1000` | 启动及动态导入 CPU 预算，单位 ms；仅在请求 CPU 预算开启时生效，范围 `1..3600000`。 |
| `WORKERD_EXPERIMENTAL_CPU_POLL_MS` | `10` | CPU watchdog 检查间隔，单位 ms，范围 `1..100`；实际中断时间可能超过预算。 |
| `WORKERD_EXPERIMENTAL_MEMORY_MB` | `0` | 每个 isolate 的内存预算，单位 MiB；`0` 关闭，开启时范围 `16..2048`。 |
| `WORKERD_EXPERIMENTAL_MEMORY_DRAIN_MS` | `5000` | 内存超限后的最长等待时间，单位 ms，范围 `0..60000`；`0` 表示立即终止。 |

例如，限制请求 CPU 时间为 50 ms、每个 isolate 内存为 128 MiB，并允许超限实例最多等待 5 秒：

```sh
WORKERD_EXPERIMENTAL_CPU_MS=50 \
WORKERD_EXPERIMENTAL_MEMORY_MB=128 \
WORKERD_EXPERIMENTAL_MEMORY_DRAIN_MS=5000 \
./workerd serve config.capnp
```

CPU 超限中断当前执行，不自动重建整个 isolate。内存超限则淘汰旧 isolate；强制终止时，尚未发送响应头的 HTTP 请求返回 `503`，已开始的流式响应和 WebSocket 只能中断连接。

## 支持范围与限制

- 当前构建和发版目标为 **Linux AMD64**，采用 Ubuntu 24.04 / glibc 2.39 构建环境。静态链接仅完成独立实验，尚未接入正式发行流程，见 [静态链接验证](docs/static-linking.md)。
- 内存预算的部署范围为静态配置的普通 JavaScript HTTP Worker。**开启内存预算时拒绝 Durable Object 配置/导出类和 CustomEvent（包括 Workers RPC）**；Python、Inspector、动态 Worker Loader 等路径未纳入本轮验证。
- 内存计量不覆盖所有原生分配，也不是 RSS 硬上限。预算以上保留 16 MiB 紧急余量，检测和中断仍可能超调；销毁 isolate 后，分配器也可能保留空闲内存供复用。
- 同步 JavaScript 阻塞事件循环时，仍可能影响其他业务直至中断；这些预算不提供完整的进程级资源隔离，也不保证业务 `finally` 或 JS disposer 执行。

内存退出、重建、watchdog 和连接清理的详细说明见 [内存预算文档](docs/memory-limit.md)。当前真实 Linux workerd 已通过 24 项内存集成检查、14 项 CPU 回归检查，另有 8 项发行脚本单元测试。实测内核为 `6.19.11-arch1-1`；目标内核 6.6 尚未实测。

## 构建与验证

需要 Git、Python 3 和 Docker，在 Linux AMD64 环境运行：

```sh
git submodule update --init --recursive
python3 scripts/distro.py build
python3 scripts/distro.py test
```

构建从干净的官方源码应用补丁；发布流程要求测试通过，并核对源码、构建记录与被测试二进制的哈希。

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
