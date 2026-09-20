# Distribution changes

## 1.20260916.1-cpu.1

- Base: Cloudflare workerd v1.20260916.1, commit adda2635656d09e541b0feeea796da9d2a8bc10e.
- Add experimental per-request execution-thread CPU accounting and termination in the standalone server host.
- Add startup CPU budgeting and configurable watchdog polling; polling defaults to 10 ms.
- Runtime changes are limited to server.c++, BUILD.bazel and a new experimental-cpu-limit.h. V8 source is unchanged by this distribution.
- The distribution is maintained independently and is not an official Cloudflare release.
