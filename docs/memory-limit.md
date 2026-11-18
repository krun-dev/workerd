# 实验性 Worker 内存预算

```sh
WORKERD_EXPERIMENTAL_MEMORY_MB=128 \
WORKERD_EXPERIMENTAL_MEMORY_DRAIN_MS=5000 \
./workerd serve config.capnp
```

`WORKERD_EXPERIMENTAL_MEMORY_MB` 单位为 MiB；默认 `0`（关闭），开启时接受 `16..2048`。配置中的每个 Worker isolate 分别使用这份预算，同一 isolate 的并发请求共享额度。它不是整个 workerd 进程的内存上限。

`WORKERD_EXPERIMENTAL_MEMORY_DRAIN_MS` 默认 `5000`，接受 `0..60000`。它是超限后允许在途工作继续执行的最长墙钟时间；设置 `0` 表示立即终止。这是发行版自己的策略，不代表 Cloudflare 内部使用相同参数。

## 生命周期

1. **运行**：正常接收请求。
2. **退出接单**：达到内存预算后停止向旧 isolate 派发新请求。已经进入的 HTTP 请求和 `waitUntil` 可在宽限时间内完成；即使内存随后下降，也不再复用这个实例。
3. **清理**：在途请求与后台任务完成后立即清理，不等待下一次请求。达到截止时间或紧急内存阈值时强制取消任务和连接，撤销入口引用，释放 isolate。
4. **切换**：具名 Worker Loader 收到同一 key 的下一次 `get()` 后，按需创建新实例并重新执行模块初始化。旧实例继续排空，已交给它的请求保留原来的请求体、响应流和执行状态。新请求共享一次启动。每个 Loader key 的内存轮换最多同时保留两代实例；两代都退出接单时，下一代等任一旧实例清理完再启动。

请求进入 isolate 后就由该实例处理至结束或被强制终止。已取得的 Loader stub 绑定原实例，应用应通过新的 `get()` 为新请求获取当前实例。静态 Worker 的新请求在旧实例清理后启动替代实例。其他业务仍可处理请求。同步 JS 阻塞事件循环时，会影响其他业务直至中断；截止时间也不是操作系统硬实时保证。

强制终止时，尚未发送响应头的在途 HTTP 请求返回 `503 Service Unavailable`；已经开始的流式响应和 WebSocket 只能中断连接。运行时不自动重放失败请求，已经发送到外部系统的写入不会因取消而撤销。业务的 `finally` 和 JS disposer 不保证执行，清理由宿主执行。

接受过 WebSocket 或 CONNECT 的实例保守地等待完整宽限时间，再通过上游连接撤销机制清理；即使连接提前关闭，也可能等到截止时间。这避免依赖 HTTP 响应结束来错误判断长连接已结束。

## 实现与范围

内存状态、入口引用和截止时间管理位于 `experimental-memory-limit.h`。具名 Loader 的在途实例引用、接单代次和并发启动由 `server/loader.rs` 管理。开启 V8 内存采样后，同一 key 的内存样本包含仍在排空的实例。`factory/worker-factory.c++` 和 `server/worker.rs` 分别接入上游限额接口、WorkerService 清理和创建/链接流程；`factory/BUILD.bazel` 声明新头文件。不修改 CPU 补丁、V8、JSG 或 I/O 核心实现。

正常实例不创建内存监控线程。只有进入退出阶段的实例才创建一个等待截止时间的线程；它不轮询，通过线程安全的 `TerminateExecution()` 中断阻塞事件循环的 JS。清理前取消并 join 该线程，避免其访问已销毁的 V8 isolate。KJ 定时器负责异步请求的截止时间，KJ 通知与资源撤销都在事件循环线程执行。

预算检查使用 V8 的 `used_heap_size + external_memory`，在 GC 回调、退出 JavaScript及发送响应头前执行。预算以上保留 16 MiB 的紧急余量；继续增长超过余量或触发 NearHeapLimit 时立即终止，不再等待。在 NearHeapLimit 路径额外给 V8 留清理空间，以避免直接进入进程 fatal OOM。

这是有超调的实验性预算，**不是 RSS 硬上限**。未登记到 V8 的原生分配不计入；一次很大的原生分配仍可能先触发进程 OOM。释放 isolate 也不意味着 RSS 立即等量下降，分配器可能保留空闲内存供后续复用。Linux cgroup `memory.max` 可作进程级兜底，但不能单独杀死同进程中的某个 isolate。

已验证静态与动态加载的 JavaScript Worker、service binding `fetch()`、Workers RPC 和 streaming tail。强制清理时取消尚未结束的会话，并在有界时间内尝试发送 tail outcome；超过时间仍可能丢失。内存检查存在超调，RPC 结果可能在超限被检测到之前返回。开启预算时拒绝 Durable Object / Workflow 配置及其他尚未纳入生命周期管理的 CustomEvent；Python 和 Inspector 等路径尚未验证。关闭预算时保持上游行为。

## 验证

```sh
python3 scripts/distro.py build
python3 scripts/distro.py test
```

Linux AMD64 的发布验证覆盖堆、ArrayBuffer、累计分配、在途任务排空和取消、重复重建、流和 WebSocket、外部 fetch、RPC、streaming tail、动态 Worker 隔离和 CPU 限制。具名 Loader 的回归用例在旧代接收 POST 后暂扣请求体并触发超限，检查新代开始接单、旧代完成原请求及 `waitUntil`、连续超限受两代上限约束。

AMD64 真实二进制已通过 25 项内存、15 项 CPU 和 14 项动态加载检查；另有 10 项发行脚本单元测试。构建与测试核对同一二进制 SHA-256，CI 分别验证两个目标架构。

在隔离的 AMD64 Kubernetes Pod 中完成源码构建和实测；使用上游版本指定的 Bazel、LLVM 和 Rust 工具链。

## 官方行为与参考

Cloudflare 文档描述超限实例停止接单、让在途请求完成并为后续请求创建新 isolate。这个发行版的宽限时间、紧急余量和 Loader 每个 key 的代数上限是本地策略。

- [Cloudflare Workers 内存限制](https://developers.cloudflare.com/workers/platform/limits/#memory)
- [workerd #1627：本地堆上限与自动重建讨论（未合并）](https://github.com/cloudflare/workerd/pull/1627#issuecomment-1939193728)
- [V8 ResourceConstraints](https://v8.github.io/api/head/classv8_1_1ResourceConstraints.html)
- [Linux 6.6 cgroup v2](https://docs.kernel.org/6.6/admin-guide/cgroup-v2.html)
