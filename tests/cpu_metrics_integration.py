"""Validate CPU attribution through a real dynamic Worker streaming tail."""
import http.client
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time

BINARY = str(pathlib.Path(sys.argv[1]).resolve())
WORKER = '''import { WorkerEntrypoint } from "cloudflare:workers";
let outcome = null;
export class Observer extends WorkerEntrypoint {
  tailStream() { return ({event}) => { if (event.type === "outcome") outcome = event; }; }
}
export default {
  async fetch(request, env, ctx) {
    const path = new URL(request.url).pathname;
    if (path === "/result") return Response.json(outcome);
    outcome = null;
    const child = env.LOADER.get(path, () => ({
      compatibilityDate: "2026-09-28", mainModule: "child.js", allowExperimental: true,
      modules: {"child.js": {js: `export default {async fetch(request) {
        const path = new URL(request.url).pathname;
        if (path === "/sleep") await new Promise(resolve => setTimeout(resolve, 200));
        else if (path === "/loop") while (true) Math.sqrt(Math.random());
        else { let n = 0; for (let i = 0; i < 20000000; i++) n += Math.sqrt(i); }
        return new Response("ok");
      }}`}}, streamingTails: [ctx.exports.Observer({props:{}})], globalOutbound: null
    }));
    return child.getEntrypoint().fetch(request);
  }
};'''
CONFIG = '''using W = import "/workerd/workerd.capnp";
const config :W.Config = (services = [(name="entry",worker=(compatibilityDate="2026-09-28",compatibilityFlags=["experimental"],modules=[(name="entry.js",esModule=embed "entry.js")],bindings=[(name="LOADER",workerLoader=(id="probe"))]))],sockets=[(name="entry",address="127.0.0.1:0",http=(),service="entry")]);'''


def get(port, path):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        conn.request('GET', path)
        response = conn.getresponse()
        return response.status, response.read().decode()
    finally:
        conn.close()


def run_case(enabled, limit, paths):
    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory)
        (root / 'entry.js').write_text(WORKER)
        (root / 'config.capnp').write_text(CONFIG)
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        sock.listen()
        port = sock.getsockname()[1]
        env = os.environ.copy()
        env['WORKERD_EXPERIMENTAL_CPU_METRICS'] = '1' if enabled else '0'
        env['WORKERD_EXPERIMENTAL_CPU_MS'] = str(limit)
        env['WORKERD_EXPERIMENTAL_MEMORY_MB'] = '0'
        proc = subprocess.Popen([BINARY, 'serve', 'config.capnp', '--experimental',
                                 f'--socket-fd=entry={sock.fileno()}'], cwd=root, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                pass_fds=(sock.fileno(),))
        sock.close()
        try:
            results = {}
            for path in paths:
                status, _ = get(port, path)
                deadline = time.monotonic() + 5
                event = None
                while time.monotonic() < deadline:
                    _, body = get(port, '/result')
                    event = json.loads(body)
                    if event is not None:
                        break
                    time.sleep(.02)
                if event is None:
                    raise AssertionError(f'no streaming tail outcome for {path}')
                results[path] = {'status': status, 'event': event}
            return results
        finally:
            proc.terminate()
            proc.wait(timeout=5)


disabled = run_case(False, 0, ['/burn'])
measured = run_case(True, 0, ['/burn', '/sleep'])
limited = run_case(True, 50, ['/loop'])
burn = measured['/burn']['event']['cpuTime']
sleep = measured['/sleep']['event']['cpuTime']
checks = {
    'disabled_reports_zero': disabled['/burn']['event']['cpuTime'] == 0,
    'cpu_work_reports_positive': measured['/burn']['status'] == 200 and burn > 1,
    'io_wait_is_excluded': measured['/sleep']['status'] == 200 and 0 < sleep < 100,
    'cpu_limit_reports_time': limited['/loop']['event']['cpuTime'] > 0,
    'cpu_limit_reports_exceeded': limited['/loop']['event']['outcome'] == 'exceededCpu',
}
result = {'checks': checks, 'disabled': disabled, 'measured': measured, 'limited': limited}
pathlib.Path(__file__).with_name('cpu-metrics-results.json').write_text(json.dumps(result, indent=2))
print(json.dumps(result), flush=True)
if not all(checks.values()):
    raise SystemExit('CPU metrics integration checks failed')
