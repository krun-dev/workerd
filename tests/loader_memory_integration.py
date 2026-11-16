"""Exercise memory retirement of dynamically loaded workers with the real runtime."""
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

binary = str(Path(sys.argv[1]).resolve())
result_path = Path(__file__).resolve().parent / 'loader-memory-results.json'
worker = r'''
let parent;
const source = `
  let id;
  const retained = [];
  export default {
    fetch(request) {
      id ??= crypto.randomUUID();
      if (new URL(request.url).pathname.endsWith('/allocate')) {
        retained.push(new Uint8Array(4 * 1024 * 1024).fill(1));
      }
      return new Response(id);
    }
  };
`;
export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (path === '/parent') {
      parent ??= crypto.randomUUID();
      return new Response(parent);
    }
    const name = path.split('/')[1];
    const loaded = await env.LOADER.get(name, () => ({
      compatibilityDate: '2026-09-28',
      mainModule: 'child.js',
      modules: { 'child.js': { js: source } },
      globalOutbound: null,
    }));
    return loaded.getEntrypoint().fetch(request);
  }
};
'''
config = '''using W = import "/workerd/workerd.capnp";
const config :W.Config = (
  services = [(name = "entry", worker = (
    modules = [(name = "entry.js", esModule = embed "entry.js")],
    compatibilityDate = "2026-09-28",
    compatibilityFlags = ["experimental"],
    bindings = [(name = "LOADER", workerLoader = (id = "memory-test"))]
  ))],
  sockets = [(name = "entry", address = "127.0.0.1:PORT", http = (), service = "entry")]
);
'''


def request(port, path):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        conn.request('GET', path)
        response = conn.getresponse()
        return response.status, response.read().decode()
    finally:
        conn.close()


with tempfile.TemporaryDirectory(prefix='krun-loader-memory-') as tmp:
    root = Path(tmp)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    port = listener.getsockname()[1]
    (root / 'entry.js').write_text(worker)
    (root / 'config.capnp').write_text(config.replace('PORT', str(port)))
    env = {key: value for key, value in os.environ.items() if not key.startswith('WORKERD_EXPERIMENTAL_')}
    env.update(WORKERD_EXPERIMENTAL_MEMORY_MB='64', WORKERD_EXPERIMENTAL_MEMORY_DRAIN_MS='100')
    with (root / 'workerd.log').open('w+') as log:
        proc = subprocess.Popen([binary, 'serve', 'config.capnp', '--experimental',
                                 f'--socket-fd=entry={listener.fileno()}'],
                                cwd=root, env=env, stdout=log, stderr=log,
                                pass_fds=(listener.fileno(),))
        listener.close()
        try:
            deadline = time.monotonic() + 15
            while True:
                if proc.poll() is not None:
                    raise RuntimeError('workerd exited during startup')
                try:
                    status, parent = request(port, '/parent')
                    if status == 200:
                        break
                except (OSError, http.client.HTTPException):
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError('workerd did not start')
                time.sleep(.05)
            assert request(port, '/a')[0] == 200
            _, a_before = request(port, '/a')
            status, b_before = request(port, '/b')
            assert status == 200
            for _ in range(24):
                try:
                    request(port, '/a/allocate')
                except (OSError, http.client.HTTPException):
                    pass
                if proc.poll() is not None:
                    raise RuntimeError('workerd exited during allocation')
            deadline = time.monotonic() + 15
            reloaded = False
            while time.monotonic() < deadline:
                try:
                    status, current = request(port, '/a')
                    if status == 200 and current != a_before:
                        reloaded = True
                        break
                except (OSError, http.client.HTTPException):
                    pass
                time.sleep(.05)
            checks = {
                'child_reloaded': reloaded,
                'other_child_unchanged': request(port, '/b') == (200, b_before),
                'parent_unchanged': request(port, '/parent') == (200, parent),
                'runtime_alive': proc.poll() is None,
            }
            result_path.write_text(json.dumps({'checks': checks}, indent=2) + '\n')
            if not all(checks.values()):
                raise AssertionError(f'Loader memory checks failed: {checks}')
            print(json.dumps(checks))
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read(), file=sys.stderr)
            raise
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
