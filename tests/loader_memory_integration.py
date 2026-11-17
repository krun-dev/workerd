"""Exercise memory retirement of dynamically loaded workers with the real runtime."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
let rotationLoads = 0;
let rotationQueued = 0;
let gateOpen = false;
let backgroundStarted = false;
let backgroundDone = null;
export class DrainGate extends WorkerEntrypoint {
  async wait() {
    backgroundStarted = true;
    while (!gateOpen) await new Promise(resolve => setTimeout(resolve, 10));
  }
  done(id) {
    backgroundDone = id;
  }
}
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
  let posts = 0;
  const retained = [];
  export default {
    async fetch(request, env, ctx) {
      id ??= crypto.randomUUID();
      const path = new URL(request.url).pathname;
      if (path.endsWith('/upload')) {
        const invocation = ++posts;
        return Response.json({ id, invocation, body: await request.text() });
      }
      if (path.endsWith('/posts')) return new Response(String(posts));
      if (path.endsWith('/background')) {
        ctx.waitUntil((async () => {
          await env.GATE.wait();
          await env.GATE.done(id);
        })());
      }
      if (path.endsWith('/hold')) {
        await new Promise(resolve => setTimeout(resolve, 60000));
      }
      if (path.endsWith('/hard-exceed')) {
        console.log('about to exceed memory');
        await new Promise(resolve => setTimeout(resolve, 100));
        retained.push(new Uint8Array(96 * 1024 * 1024).fill(1));
      }
      if (path.endsWith('/allocate')) {
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
    if (path === '/rotation-status') {
      return Response.json({ rotationLoads, rotationQueued, backgroundStarted, backgroundDone });
    }
    if (path === '/rotation-memory') return Response.json(env.LOADER.getMemoryUsage());
    if (path === '/rotation-release') {
      gateOpen = true;
      return new Response('ok');
    }
    const name = path.split('/')[1];
    if (path === '/rotation/queued') rotationQueued++;
    const loaded = await env.LOADER.get(name, () => {
      if (name === 'rotation') rotationLoads++;
      return {
        compatibilityDate: '2026-09-28',
        mainModule: 'child.js',
        modules: { 'child.js': { js: source } },
        allowExperimental: true,
        env: { GATE: ctx.exports.DrainGate({ props: {} }) },
        streamingTails: [ctx.exports.Observer({ props: {} })],
        globalOutbound: null,
      };
    });
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


@contextmanager
def open_upload(port):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=6)
    try:
        conn.putrequest('POST', '/rotation/upload')
        conn.putheader('Content-Length', '7')
        conn.endheaders()
        yield conn
    finally:
        conn.close()


def finish_upload(conn):
    conn.send(b'payload')
    response = conn.getresponse()
    return response.status, json.loads(response.read())


def wait_until(predicate, description):
    deadline = time.monotonic() + 4
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(description)
        time.sleep(.01)


def rotation_checks(port):
    budget = 64 * 1024 * 1024

    def state():
        status, body = request(port, '/rotation-status')
        assert status == 200
        return json.loads(body)

    def memory_sample():
        status, body = request(port, '/rotation-memory')
        assert status == 200
        samples = [row['bytes'] for row in json.loads(body) if row['id'] == 'rotation']
        assert len(samples) == 1, samples
        return samples[0]

    def exhaust(generation, prior_bytes=0):
        for _ in range(20):
            assert request(port, '/rotation/allocate') == (200, generation)
            used = memory_sample()
            if used - prior_bytes > budget:
                return used
        raise AssertionError(f'generation {generation} did not cross the soft memory limit')

    with open_upload(port) as first:
        wait_until(lambda: request(port, '/rotation/posts') == (200, '1'),
                   'first upload did not reach the Worker')
        status, first_id = request(port, '/rotation/background')
        assert status == 200
        wait_until(lambda: state()['backgroundStarted'], 'waitUntil did not start')
        first_bytes = exhaust(first_id)
        status, second_id = request(port, '/rotation')
        assert status == 200
        assert second_id != first_id
        assert state()['backgroundDone'] is None
        overlap = memory_sample() > first_bytes

        with open_upload(port) as second, ThreadPoolExecutor(max_workers=3) as pool:
            wait_until(lambda: request(port, '/rotation/posts') == (200, '1'),
                       'second upload did not reach the Worker')
            exhaust(second_id, first_bytes)

            queued = [pool.submit(request, port, '/rotation/queued') for _ in range(3)]
            wait_until(lambda: state()['rotationQueued'] == len(queued),
                       'replacement requests did not reach the Loader')
            bounded = state()['rotationLoads'] == 2 and not any(f.done() for f in queued)

            first_completed = finish_upload(first) == (
                200, {'id': first_id, 'invocation': 1, 'body': 'payload'})
            assert state()['backgroundDone'] is None
            waits_for_background = not any(f.done() for f in queued)
            assert request(port, '/rotation-release') == (200, 'ok')
            wait_until(lambda: state()['backgroundDone'] == first_id,
                       'retired generation lost its waitUntil task')

            results = [f.result() for f in queued]
            status, third_id = results[0]
            single_startup = (status == 200 and third_id not in {first_id, second_id}
                              and all(result == results[0] for result in results)
                              and state()['rotationLoads'] == 3)
            second_completed = finish_upload(second) == (
                200, {'id': second_id, 'invocation': 1, 'body': 'payload'})

        wait_until(lambda: 0 < memory_sample() < budget, 'drained generations still reported')
        current_preserved = (request(port, '/rotation') == (200, third_id)
                             and request(port, '/rotation/posts') == (200, '0')
                             and state()['rotationLoads'] == 3)
    return {
        'replacement_serves_during_drain': overlap,
        'accepted_post_finishes_on_original_generation': first_completed,
        'second_accepted_post_finishes_on_original_generation': second_completed,
        'post_bodies_are_not_replayed': current_preserved,
        'accepted_wait_until_finishes_before_slot_reuse': waits_for_background,
        'overlap_capped_at_two_generations': bounded,
        'queued_requests_share_one_replacement': single_startup,
    }


with tempfile.TemporaryDirectory(prefix='krun-loader-memory-') as tmp:
    root = Path(tmp)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    port = listener.getsockname()[1]
    (root / 'entry.js').write_text(worker)
    (root / 'config.capnp').write_text(config.replace('PORT', str(port)))
    env = {key: value for key, value in os.environ.items() if not key.startswith('WORKERD_EXPERIMENTAL_')}
    env.update(WORKERD_EXPERIMENTAL_MEMORY_MB='64', WORKERD_EXPERIMENTAL_MEMORY_DRAIN_MS='5000',
               WORKERD_EXPERIMENTAL_MEMORY_METRICS='1')
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
            rotation = rotation_checks(port)
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
                **rotation,
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
