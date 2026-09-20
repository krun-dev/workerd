# Distribution changes

## 1.20260920.1-krun.0

- Upgrade the pinned official release to v1.20260920.1 (90faec3319d87b3ca98f8f522a1cc4295538d5ef). The CPU budget patch applies unchanged.
- Build and test natively on Linux x86_64 and ARM64.
- Match upstream Release assets: workerd-linux-64.gz and workerd-linux-arm64.gz only; provenance and full packages remain in Actions artifacts.

## Versioning and packaging changes (previously unreleased)

- Preserve the exact upstream release version and append `-krun.N`, starting at zero for each new upstream version.
- Add `next-version` to increment the downstream revision or reset it when changing upstream, and validate VERSION against the pinned upstream tag.
- Publish `workerd-linux-64.gz` alongside the full distribution archive, following upstream's Linux binary naming.
- Keep the historical `1.20260916.1-cpu.1` release unchanged.

## 1.20260916.1-cpu.1

- Base: Cloudflare workerd v1.20260916.1, commit adda2635656d09e541b0feeea796da9d2a8bc10e.
- Add experimental per-request execution-thread CPU accounting and termination in the standalone server host.
- Add startup CPU budgeting and configurable watchdog polling; polling defaults to 10 ms.
- Runtime changes are limited to server.c++, BUILD.bazel and a new experimental-cpu-limit.h. V8 source is unchanged by this distribution.
- The distribution is maintained independently and is not an official Cloudflare release.
