#!/usr/bin/env python3
"""Build a pinned workerd plus an ordered patch series. Python stdlib only."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import versioning

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / 'upstream/workerd'
BUILD = ROOT / '.build'
SOURCE = BUILD / 'workerd'
TARGET = '//src/workerd/server:workerd'


def run(args, cwd=ROOT, **kwargs):
    print('+ ' + ' '.join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), cwd=cwd, check=True, **kwargs)


def output(args, cwd=ROOT):
    return subprocess.check_output(list(map(str, args)), cwd=cwd, text=True).strip()


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + '\n')


def architecture():
    if platform.system() != 'Linux':
        raise RuntimeError('Build and integration tests require native Linux.')
    targets = {'x86_64': ('x86_64', 'amd64', '64'),
               'aarch64': ('arm64', 'arm64', 'arm64'),
               'arm64': ('arm64', 'arm64', 'arm64')}
    try:
        return targets[platform.machine()]
    except KeyError:
        raise RuntimeError('Supported Linux architectures: x86_64 and ARM64.') from None


def inputs():
    if not (UPSTREAM / '.git').exists():
        raise RuntimeError('Run git submodule update --init --recursive first.')
    if output(['git', 'status', '--porcelain'], UPSTREAM):
        raise RuntimeError('Keep the upstream submodule clean; maintain changes in patches/.')
    commit = output(['git', 'rev-parse', 'HEAD'], UPSTREAM)
    entry = output(['git', 'ls-files', '--stage', 'upstream/workerd']).split()
    if len(entry) < 2 or entry[0] != '160000' or entry[1] != commit:
        raise RuntimeError('Submodule checkout differs from the gitlink. Stage the intended upstream revision.')
    version = (ROOT / 'VERSION').read_text().strip()
    upstream_version = versioning.upstream_version(UPSTREAM)
    versioning.validate(version, upstream_version)
    patches = []
    for name in (ROOT / 'patches/series').read_text().splitlines():
        name = name.strip()
        if not name or name.startswith('#'):
            continue
        if Path(name).name != name or not name.endswith('.patch'):
            raise RuntimeError('Each series entry must be a patch filename, without directories.')
        patches.append({'file': name, 'sha256': sha(ROOT / 'patches' / name)})
    if not patches:
        raise RuntimeError('Empty patch series.')
    files = ['scripts/distro.py', 'scripts/versioning.py', 'docker/Dockerfile.build', 'tests/integration.py']
    files += [str(p.relative_to(ROOT)) for p in sorted((ROOT / 'tests/fixtures').iterdir()) if p.is_file()]
    return {'version': version, 'upstream_version': upstream_version,
            'upstream_commit': commit, 'patches': patches,
            'recipe': {p: sha(ROOT / p) for p in files}}


def next_version():
    if output(['git', 'status', '--porcelain'], UPSTREAM):
        raise RuntimeError('Keep the upstream submodule clean before selecting a version.')
    version = versioning.next_version(
        versioning.upstream_version(UPSTREAM), (ROOT / 'VERSION').read_text().strip(),
        output(['git', 'tag', '--list', 'v*-krun.*']).splitlines())
    (ROOT / 'VERSION').write_text(version + '\n')
    print('Next distribution version:', version)


def prepare():
    data = inputs()
    BUILD.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='source-', dir=BUILD))
    try:
        archive = BUILD / 'upstream.tar'
        run(['git', 'archive', '--format=tar', '-o', archive, data['upstream_commit']], cwd=UPSTREAM)
        run(['tar', '-xf', archive, '-C', staging])
        archive.unlink()
        env = os.environ.copy()
        # The exported source is intentionally outside Git; don't discover the outer repo.
        env['GIT_CEILING_DIRECTORIES'] = str(BUILD)
        for patch in data['patches']:
            path = ROOT / 'patches' / patch['file']
            run(['git', 'apply', '--check', path], cwd=staging, env=env)
            run(['git', 'apply', path], cwd=staging, env=env)
        if SOURCE.exists():
            shutil.rmtree(SOURCE)
        staging.rename(SOURCE)
        write_json(BUILD / 'inputs.json', data)
        for stale in ['build.json', 'test.json']:
            (BUILD / stale).unlink(missing_ok=True)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    print('Prepared clean upstream + patches:', SOURCE)


def build():
    arch, docker_arch, _ = architecture()
    prepare()
    image = os.environ.get('WORKERD_BUILD_IMAGE', f'workerd-builder:ubuntu24-clang19-bazel9.2.0-{docker_arch}')
    if 'WORKERD_BUILD_IMAGE' not in os.environ:
        run(['docker', 'build', '--platform', f'linux/{docker_arch}',
             '--build-arg', f'TARGETARCH={docker_arch}',
             '-f', 'docker/Dockerfile.build', '-t', image, 'docker'])
    image_id = output(['docker', 'image', 'inspect', '--format', '{{.Id}}', image])
    cache = Path(os.environ.get('WORKERD_BUILD_CACHE', str(BUILD / 'cache'))).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    jobs = int(os.environ.get('WORKERD_BUILD_JOBS', '4'))
    memory = int(os.environ.get('WORKERD_BUILD_MEMORY_MB', '8000'))
    if jobs < 1 or memory < 1024:
        raise RuntimeError('Use positive jobs and at least 1024 MiB build memory.')
    flags = ['--repo_env=CC=clang-19', '--repository_cache=/cache/repository',
             '--config=release_linux', '--strip=always',
             f'--jobs={jobs}', f'--local_resources=memory={memory}', TARGET]
    network = os.environ.get('WORKERD_BUILD_NETWORK', 'bridge')
    proxy_args = []
    for key in ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy']:
        if key in os.environ:
            proxy_args += ['-e', key]  # Pass by name so credentials never appear in command logs.
    run(['docker', 'run', '--rm', f'--platform=linux/{docker_arch}',
         '--network', network, *proxy_args,
         '--user', f'{os.getuid()}:{os.getgid()}', '-e', 'HOME=/tmp',
         '-v', f'{ROOT}:/repo', '-v', f'{cache}:/cache',
         '-w', '/repo/.build/workerd', image,
         'bazel', '--output_user_root=/cache/bazel', '--batch', 'build', *flags])
    # bazel-bin is a symlink into a container path. Copy the binary while that path is mounted.
    staged_binary = BUILD / 'workerd-release.new'
    staged_binary.unlink(missing_ok=True)
    run(['docker', 'run', '--rm', f'--platform=linux/{docker_arch}',
         '--user', f'{os.getuid()}:{os.getgid()}',
         '-v', f'{ROOT}:/repo', '-v', f'{cache}:/cache', image,
         'cp', '/repo/.build/workerd/bazel-bin/src/workerd/server/workerd', '/repo/.build/workerd-release.new'])
    binary = BUILD / 'workerd-release'
    staged_binary.chmod(0o755)
    staged_binary.replace(binary)
    data = json.loads((BUILD / 'inputs.json').read_text())
    data.update({'binary_sha256': sha(binary), 'build_image': image, 'build_image_id': image_id,
                 'bazel_flags': flags, 'platform': f'linux-{arch}',
                 'build_network': network,
                 'libc_baseline': 'Ubuntu 24.04 / glibc 2.39',
                 'distro_commit': output(['git', 'rev-parse', 'HEAD']),
                 'distro_dirty': bool(output(['git', 'status', '--porcelain']))})
    write_json(BUILD / 'build.json', data)


def test(binary=None):
    architecture()
    binary = Path(binary or BUILD / 'workerd-release').resolve()
    # Fixed test ports are owned only by this test run. Fail before starting if unavailable.
    import socket
    sockets = []
    try:
        for port in [18871, 18872, 18873]:
            sock = socket.socket()
            sockets.append(sock)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', port))
    finally:
        for sock in sockets:
            sock.close()
    directory = BUILD / 'test-run'
    if directory.exists():
        shutil.rmtree(directory)
    shutil.copytree(ROOT / 'tests/fixtures', directory)
    shutil.copy2(ROOT / 'tests/integration.py', directory / 'integration.py')
    (BUILD / 'test.json').unlink(missing_ok=True)
    env = os.environ.copy()
    for key in list(env):
        if key.startswith('WORKERD_EXPERIMENTAL_'):
            del env[key]
    # Tests address only local Workers, even when dependency downloads need a proxy.
    env['NO_PROXY'] = env['no_proxy'] = '127.0.0.1,localhost'
    run([sys.executable, directory / 'integration.py', binary], env=env)
    results = json.loads((directory / 'results.json').read_text())
    if len(results['checks']) != 14 or not all(results['checks'].values()):
        raise RuntimeError('CPU budget integration checks failed.')
    write_json(BUILD / 'test.json', {'binary_sha256': sha(binary), 'inputs': inputs(),
                                   'checks': results['checks'], 'kernel': platform.release()})


def package():
    binary = BUILD / 'workerd-release'
    build_data = json.loads((BUILD / 'build.json').read_text())
    tests = json.loads((BUILD / 'test.json').read_text())
    current = inputs()
    if any(build_data[k] != v for k, v in current.items()) or tests['inputs'] != current:
        raise RuntimeError('Source/recipe changed after build or tests; rebuild before packaging.')
    if build_data['binary_sha256'] != sha(binary) or tests['binary_sha256'] != sha(binary):
        raise RuntimeError('Tests and build must describe this exact binary.')
    if output(['git', 'status', '--porcelain']) or build_data['distro_dirty']:
        raise RuntimeError('Release packaging requires a clean, committed distribution repository.')
    if build_data['distro_commit'] != output(['git', 'rev-parse', 'HEAD']):
        raise RuntimeError('Distribution commit changed since build.')
    arch, _, asset_arch = architecture()
    if build_data['platform'] != f'linux-{arch}':
        raise RuntimeError('Build architecture does not match the packaging host.')
    name = f"workerd-{current['version']}-linux-{arch}"
    dist = ROOT / 'dist'
    dist.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='package-', dir=BUILD) as tmp:
        stage = Path(tmp) / name
        stage.mkdir()
        shutil.copy2(binary, stage / 'workerd')
        (stage / 'workerd').chmod(0o755)
        shutil.copy2(UPSTREAM / 'LICENSE', stage / 'LICENSE.workerd')
        for item in ['README.md', 'CHANGES.md', 'VERSION']:
            shutil.copy2(ROOT / item, stage / item)
        shutil.copytree(ROOT / 'patches', stage / 'patches')
        write_json(stage / 'build-info.json', build_data)
        write_json(stage / 'test-info.json', tests)
        with tarfile.open(dist / f'{name}.tar.gz', 'w:gz') as archive:
            archive.add(stage, arcname=name)
    shutil.copy2(BUILD / 'build.json', dist / f'{name}.build-info.json')
    shutil.copy2(BUILD / 'test.json', dist / f'{name}.test-info.json')
    # Match upstream's directly downloadable gzip-compressed Linux executable.
    compressed = dist / f'workerd-linux-{asset_arch}.gz'
    with binary.open('rb') as source, compressed.open('wb') as target:
        with gzip.GzipFile(filename='workerd', mode='wb', fileobj=target, mtime=0) as archive:
            shutil.copyfileobj(source, archive)
    artifacts = [dist / f'{name}{suffix}' for suffix in ['.tar.gz', '.build-info.json', '.test-info.json']]
    artifacts.append(compressed)
    (dist / f'{name}.sha256').write_text(''.join(f'{sha(p)}  {p.name}\n' for p in artifacts))
    print('Release assets:', dist)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['next-version', 'prepare', 'build', 'test', 'package', 'release'])
    parser.add_argument('--binary', help='Existing binary for test only; never relabelled as a release build.')
    args = parser.parse_args()
    if args.binary and args.command != 'test':
        parser.error('--binary is only supported for test')
    if args.command == 'release':
        build()
        test()
        package()
    elif args.command == 'test':
        test(args.binary)
    else:
        globals()[args.command.replace('-', '_')]()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
