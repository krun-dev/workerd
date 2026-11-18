"""Check actual WorkerLoader memory samples with memory enforcement disabled."""
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

ENTRY = r"""
const child = `
import { abortIsolate } from "cloudflare:workers";
const retained = [];
export default { fetch(request) {
  const path = new URL(request.url).pathname;
  if (path.endsWith("/allocate")) retained.push(new Uint8Array(8 * 1024 * 1024).fill(1));
  if (path.endsWith("/abort")) abortIsolate("memory metrics test");
  return new Response("ok");
}};
`;
export default { async fetch(request, env) {
  const [, id] = new URL(request.url).pathname.split("/");
  if (id === "metrics") return Response.json(env.LOADER.getMemoryUsage());
  if (id === "other") return Response.json(env.OTHER.getMemoryUsage());
  const stub = env.LOADER.get(id, () => ({
    compatibilityDate: "2026-09-28",
    allowExperimental: true,
    mainModule: "child.js",
    modules: {"child.js": {js: id === "broken" ? "export default {" : child}},
    globalOutbound: null,
  }));
  try {
    return await stub.getEntrypoint().fetch(request);
  } catch {
    return new Response("failed", {status: 503});
  }
}};
"""

CONFIG = """
using W = import "/workerd/workerd.capnp";
const config :W.Config = (
  services = [(name = "entry", worker = (
    compatibilityDate = "2026-09-28",
    compatibilityFlags = ["experimental"],
    modules = [(name = "entry.js", esModule = embed "entry.js")],
    bindings = [
      (name = "LOADER", workerLoader = (id = "test")),
      (name = "OTHER", workerLoader = (id = "other"))
    ]
  ))],
  sockets = [(name = "entry", address = "127.0.0.1:0", http = (), service = "entry")]
);
"""


def get(port, path):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        conn.request('GET', path)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def run(binary, enabled):
    with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
        root = Path(directory)
        (root / 'entry.js').write_text(ENTRY)
        (root / 'config.capnp').write_text(CONFIG)
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        fd = listener.fileno()
        env = dict(os.environ, WORKERD_EXPERIMENTAL_MEMORY_MB='0',
                   WORKERD_EXPERIMENTAL_MEMORY_METRICS=str(int(enabled)))
        with subprocess.Popen([binary, 'serve', 'config.capnp', '--experimental',
                               f'--socket-fd=entry={fd}'], cwd=root, env=env, pass_fds=(fd,)) as proc:
            listener.close()
            try:
                def samples(path='/metrics'):
                    status, data = get(port, path)
                    assert status == 200, (status, data)
                    return {row['id']: row['bytes'] for row in json.loads(data)}

                cold = samples()
                assert get(port, '/a')[0] == get(port, '/b')[0] == 200
                baseline = samples()
                assert get(port, '/a/allocate')[0] == 200
                allocated = samples()
                assert get(port, '/broken')[0] != 200
                healthy = samples()
                separate = samples('/other')
                assert get(port, '/a/abort')[0] != 200
                after_abort = samples()
                return cold, baseline, allocated, healthy, separate, after_abort
            finally:
                proc.terminate()
                proc.wait(timeout=5)


binary = str(Path(sys.argv[1]).resolve())
cold, baseline, allocated, healthy, separate, after_abort = run(binary, True)
checks = {
    'disabled_samples_are_empty': all(not sample for sample in run(binary, False)),
    'cold_loader_is_empty': cold == {},
    'serving_workers_are_sampled': baseline.keys() == {'a', 'b'} and all(baseline.values()),
    'array_buffer_memory_is_attributed': allocated['a'] - baseline['a'] >= 7 * 1024 * 1024
        and abs(allocated['b'] - baseline['b']) < 1024 * 1024,
    'scoped_to_ready_workers_and_namespace': healthy.keys() == {'a', 'b'} and separate == {},
    'aborted_worker_is_removed': after_abort.keys() == {'b'},
}
Path(__file__).with_name('memory-metrics-results.json').write_text(json.dumps({'checks': checks}))
print(json.dumps(checks), flush=True)
if not all(checks.values()):
    raise SystemExit('Memory metrics integration checks failed')
