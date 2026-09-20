# 静态链接验证

2026-09-20，基于首版发行提交 `ef5349550e09a5c27c832538cddbd9be6046b0c0` 和官方 workerd `v1.20260916.1` 完成验证。此实验没有替换首版发布产物。

## 方法

复用 Ubuntu 24.04 / Clang 19 / glibc 2.39 的 `release_linux` 编译对象，将 Bazel 生成的最终链接参数中的 `-pie` 改成 `-static-pie`，指定独立输出文件，再使用 `clang-19 @link.params` 链接。没有修改 workerd 或 V8 源码，也没有迁移 musl。

实验二进制 SHA256：`9f4a84992a8cb9e158bbb55831f9f370b40824b50e8e92cb83599a7365e9ecbd`。

`file` 确认为 static-pie；`readelf` 确认没有 PT_INTERP 或 DT_NEEDED。静态 PIE 仍包含重定位用动态节。

## 实测

- 原有 CPU 预算集成测试 14/14 通过。
- 在 FROM scratch 镜像中运行；只提供二进制、Worker 配置/JS、CA 证书，无 glibc 共享库或动态加载器。Docker 提供 hosts/resolv.conf 等基础文件。
- UID 65534，CPU 预算 50ms，默认检查间隔 10ms。
- HTTP 正常返回 200；死循环约 60.5ms 返回 500，后续正常请求恢复 200。
- 出站 DNS 和 HTTPS 请求成功。需设置 `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt`；首次未设置证书路径时失败，补上后通过，没有禁用证书校验。
- 实测内核为 Linux 6.19.11；尚未在生产目标 Linux 6.6 上重测。

## 发布前仍需完成

将静态选项接入完整 Bazel 构建、测试、打包与 CI；验证 Linux 6.6 和目标网络配置。当前只有最终链接阶段的独立实验，不能称为已发布的静态发行版。

该方案在已测基础环境中不依赖宿主机 glibc 版本，但不保证特殊 NSS 模块（如 LDAP、SSSD、mDNS）或其他 dlopen 路径兼容。仍依赖适用的 CPU 架构、内核、DNS 配置、CA 证书等。静态包含的 glibc 更新需重新构建二进制。

参考：[workerd 历史讨论](https://github.com/cloudflare/workerd/discussions/1515)、[glibc 内置 nss_dns 的实现](https://sourceware.org/pipermail/glibc-cvs/2021q3/073768.html)、[静态 NSS 后续讨论](https://sourceware.org/pipermail/libc-alpha/2023-May/148682.html)。历史讨论不能简单解读为静态 glibc 一定无法进行 DNS 查询；当前实测基础 DNS/HTTPS 已通过。
