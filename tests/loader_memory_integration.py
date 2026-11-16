"""Exercise memory retirement of dynamically loaded workers with the real runtime."""
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

binary = str(Path(sys.argv[1]).resolve())
result_path = Path(__file__).resolve().parent / 'loader-memory-results.json'
worker = r'''
import { WorkerEntrypoint } from 'cloudflare:workers';
let parent;
let tailOnsets = 0;
const tailOutcomes = [];
const parentRetained = [];
export class Observer extends WorkerEntrypoint {
  tailStream(onset) {
    tailOnsets++;
    return ({ event }) => {
      if (event.type === 'outcome') {
        tailOutcomes.push({ entrypoint: onset.event.entrypoint ?? null, outcome: event.outcome });
      }
    };
  }
}
const source = `
  import { WorkerEntrypoint } from 'cloudflare:workers';
  export class Rpc extends WorkerEntrypoint {
    async hardExceed() {
      console.log('RPC about to exceed memory');
      await new Promise(resolve => setTimeout(resolve, 100));
      const retained = new Uint8Array(96 * 1024 * 1024).fill(1);
      await new Promise(resolve => setTimeout(resolve, 60000));
      return retained.length;
    }
  }
  let id;
  const retained = [];
  export default {
    async fetch(request) {
      id ??= crypto.randomUUID();
      if (new URL(request.url).pathname.endsWith('/hold')) {
        await new Promise(resolve => setTimeout(resolve, 60000));
      }
      if (new URL(request.url).pathname.endsWith('/hard-exceed')) {
        console.log('about to exceed memory');
        await new Promise(resolve => setTimeout(resolve, 100));
        retained.push(new Uint8Array(96 * 1024 * 1024).fill(1));
      }
      if (new URL(request.url).pathname.endsWith('/allocate')) {
        retained.push(new Uint8Array(4 * 1024 * 1024).fill(1));
      }
      return new Response(id);
    }
  };
`;
export default {
  async fetch(request, env, ctx) {
    const path = new URL(request.url).pathname;
    if (path === '/parent') {
      parent ??= crypto.randomUUID();
      return new Response(parent);
    }
    if (path === '/tails') return new Response(String(tailOnsets));
    if (path === '/outcomes') return Response.json(tailOutcomes);
    if (path === '/parent-allocate') {
      parentRetained.push(new Uint8Array(4 * 1024 * 1024).fill(1));
      return new Response('ok');
    }
    const name = path.split('/')[1];
    const loaded = await env.LOADER.get(name, () => ({
      compatibilityDate: '2026-09-28',
      mainModule: 'child.js',
      modules: { 'child.js': { js: source } },
      allowExperimental: true,
      streamingTails: [ctx.exports.Observer({ props: {} })],
      globalOutbound: null,
    }));
    if (path.endsWith('/rpc-exceed')) {
      try {
        return new Response(String(await loaded.getEntrypoint('Rpc').hardExceed()));
      } catch {
        return new Response('rpc canceled', { status: 503 });
      }
    }
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
                'tail_events_delivered': int(request(port, '/tails')[1]) > 0,
                'successful_tail_outcome': any(event['outcome'] == 'ok' for event in json.loads(request(port, '/outcomes')[1])),
                'child_reloaded': reloaded,
                'other_child_unchanged': request(port, '/b') == (200, b_before),
                'parent_unchanged': request(port, '/parent') == (200, parent),
                'runtime_alive': proc.poll() is None,
            }
            # Once a tail has started, a hard memory stop must emit a final error outcome;
            # krun uses it to release the in-flight invocation and to report failures.
            previous_outcomes = len(json.loads(request(port, '/outcomes')[1]))
            status, _ = request(port, '/a/hard-exceed')
            checks['hard_limit_returns_503'] = status == 503
            deadline = time.monotonic() + 5
            hard_outcomes = []
            while time.monotonic() < deadline:
                hard_outcomes = json.loads(request(port, '/outcomes')[1])[previous_outcomes:]
                if any(event['outcome'] == 'exceededMemory' and event['entrypoint'] != 'Rpc' for event in hard_outcomes):
                    break
                time.sleep(.02)
            checks['hard_limit_tail_outcome'] = any(event['outcome'] == 'exceededMemory' and event['entrypoint'] != 'Rpc' for event in hard_outcomes)

            # RPC sessions have their own outcome and must report the same hard-limit reason.
            previous_outcomes = len(json.loads(request(port, '/outcomes')[1]))
            status, _ = request(port, '/a/rpc-exceed')
            checks['rpc_hard_limit_returns_503'] = status == 503
            deadline = time.monotonic() + 5
            rpc_outcomes = []
            while time.monotonic() < deadline:
                rpc_outcomes = json.loads(request(port, '/outcomes')[1])[previous_outcomes:]
                if any(event['outcome'] == 'exceededMemory' and event['entrypoint'] == 'Rpc' for event in rpc_outcomes):
                    break
                time.sleep(.02)
            checks['rpc_hard_limit_tail_outcome'] = any(event['outcome'] == 'exceededMemory' and event['entrypoint'] == 'Rpc' for event in rpc_outcomes)

            # Keep a streaming-tail session alive while its receiver exceeds the limit. The
            # session must be canceled and the parent isolate must restart without killing workerd.
            def hold_child():
                try:
                    request(port, '/b/hold')
                except (OSError, http.client.HTTPException):
                    pass
            tail_count = int(request(port, '/tails')[1])
            held = threading.Thread(target=hold_child, daemon=True)
            held.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and int(request(port, '/tails')[1]) <= tail_count:
                time.sleep(.02)
            checks['tail_session_started'] = int(request(port, '/tails')[1]) > tail_count
            for _ in range(24):
                try:
                    request(port, '/parent-allocate')
                except (OSError, http.client.HTTPException):
                    pass
                if proc.poll() is not None:
                    break
            deadline = time.monotonic() + 15
            parent_reloaded = False
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    status, new_parent = request(port, '/parent')
                    if status == 200 and new_parent != parent:
                        parent_reloaded = True
                        break
                except (OSError, http.client.HTTPException):
                    pass
                time.sleep(.05)
            checks['tail_receiver_reloaded'] = parent_reloaded
            checks['runtime_alive_after_tail_receiver_retirement'] = proc.poll() is None
            checks['child_available_after_tail_receiver_retirement'] = request(port, '/b')[0] == 200
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
