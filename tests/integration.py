"""Real workerd HTTP integration test. stdlib only; kills only its own child processes."""
import concurrent.futures
import contextlib
import json
import os
import pathlib
import platform
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
BINARY = str(pathlib.Path(sys.argv[1]).resolve())
results = {'kernel': platform.release(), 'binary': BINARY, 'cases': []}


def request(port=18871, path='/', timeout=3):
    started = time.monotonic()
    received_status = None
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=timeout) as response:
            received_status = response.status
            out = {'status': response.status, 'body': response.read().decode()[:100]}
    except urllib.error.HTTPError as error:
        out = {'status': error.code, 'body': error.read().decode()[:100]}
    except Exception as error:
        out = {'error': str(error)}
        if received_status is not None:
            out['headers_status'] = received_status
    out['wall_ms'] = round((time.monotonic() - started) * 1000, 3)
    return out


def record(name, **data):
    entry = {'name': name, **data}
    results['cases'].append(entry)
    print(json.dumps(entry), flush=True)
    (ROOT / 'results.json').write_text(json.dumps(results, indent=2))


def process_stats(proc):
    fields = pathlib.Path(f'/proc/{proc.pid}/stat').read_text().split(') ', 1)[1].split()
    status = pathlib.Path(f'/proc/{proc.pid}/status').read_text().splitlines()
    return {'cpu_ms': (int(fields[11]) + int(fields[12])) * 1000 / os.sysconf('SC_CLK_TCK'),
            'status': [line for line in status if line.startswith(('VmRSS:', 'Threads:'))]}


def healthy_benchmark(proc, name):
    before = process_stats(proc)
    start = time.monotonic()
    responses = [request(18871 if i % 2 else 18872) for i in range(1000)]
    after = process_stats(proc)
    record(name, wall_ms=round((time.monotonic() - start) * 1000, 3),
           workerd_cpu_ms=after['cpu_ms'] - before['cpu_ms'], status=after['status'],
           failures=sum(r.get('status') != 200 for r in responses))


@contextlib.contextmanager
def server(budget, name):
    env = os.environ.copy()
    env['WORKERD_EXPERIMENTAL_CPU_MS'] = str(budget)
    env['WORKERD_EXPERIMENTAL_STARTUP_CPU_MS'] = '1000'
    with open(ROOT / f'{name}.log', 'w') as log:
        proc = subprocess.Popen([BINARY, 'serve', 'config.capnp'], cwd=ROOT,
                                stdout=log, stderr=log, env=env)
        try:
            for _ in range(200):
                if proc.poll() is not None:
                    raise RuntimeError(f'workerd exited {proc.returncode}: {name}.log')
                if request(timeout=.1).get('body') == 'a-ok':
                    break
                time.sleep(.025)
            else:
                raise RuntimeError('workerd did not start')
            yield proc
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


with server(0, 'baseline') as proc:
    record('baseline_healthy', a=request(), b=request(18872), pid=proc.pid)
    healthy_benchmark(proc, 'baseline_1000_short_requests')
    n = 10_000_000
    # Calibrate finite work to ~20ms, independent of workerd's frozen Date/performance clocks.
    for _ in range(5):
        samples = [request(path=f'/burn?n={n}')['wall_ms'] for _ in range(5)]
        median = statistics.median(samples)
        n = max(1000, int(n * 20 / max(median, .1)))
    record('calibration', iterations=n, samples_ms=samples)
    record('baseline_chunks', result=request(path=f'/chunks?n={n}&count=8'))
    with concurrent.futures.ThreadPoolExecutor() as pool:
        loop = pool.submit(request, 18871, '/loop', .5)
        time.sleep(.05)
        b = request(18872, timeout=.2)
        record('baseline_infinite_loop', a=loop.result(), b=b,
               b_after_client_timeout=request(18872, timeout=.2), alive=proc.poll() is None)

with server(50, 'limited') as proc:
    record('limited_healthy', a=request(), b=request(18872), pid=proc.pid)
    healthy_benchmark(proc, 'limited_1000_short_requests')
    record('io_wait_excluded', result=request(path='/sleep'))
    before = process_stats(proc)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        waiting = pool.submit(request, 18871, '/sleep')
        other = []
        while not waiting.done():
            other.append(request(18872, f'/burn?n={n // 4}'))
        record('other_worker_cpu_excluded', a=waiting.result(), b_requests=len(other),
               b_failures=sum(r.get('status') != 200 for r in other),
               process_cpu_ms=process_stats(proc)['cpu_ms'] - before['cpu_ms'])
    record('binding_works', result=request(path='/binding'))
    record('finite_cpu', result=request(path=f'/burn?n={n}'))
    record('cumulative_across_await', result=request(path=f'/chunks?n={n}&count=8'),
           b=request(18872), a_next=request())
    for path in ['/loop', '/microtasks']:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            bad = pool.submit(request, 18871, path)
            time.sleep(.01)
            b = request(18872)
            record(path, a=bad.result(), b=b, a_next=request(), alive=proc.poll() is None)
            assert b.get('status') == 200 and proc.poll() is None, 'healthy worker blocked or process died'

    latencies, failures, bad_statuses = [], [], []
    with concurrent.futures.ThreadPoolExecutor() as pool:
        for i in range(100):
            bad = pool.submit(request, 18871, '/loop')
            time.sleep(.003)
            good = request(18872)
            latencies.append(good['wall_ms'])
            bad_statuses.append(bad.result().get('status'))
            if good.get('status') != 200 or request().get('status') != 200:
                failures.append(i)
    record('100_attacks', healthy_failures=failures, attack_statuses=sorted(set(bad_statuses), key=str),
           b_median_ms=statistics.median(latencies), b_max_ms=max(latencies), alive=proc.poll() is None)
    boundary = []
    for i in range(60):
        result = request(path=f'/burn?n={int(n * (2.2 + (i % 7) * .1))}')
        a_next, b = request(), request(18872)
        boundary.append({'result': result, 'a_next': a_next, 'b': b})
        assert proc.poll() is None, 'process died near the budget boundary'
    record('budget_boundary', failures=sum(r['a_next'].get('status') != 200 or
                                           r['b'].get('status') != 200 for r in boundary),
           results=boundary)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        burst = [pool.submit(request, 18871, '/loop') for _ in range(5)]
        time.sleep(.01)
        b = request(18872)
        record('five_concurrent_attacks', attacks=[f.result() for f in burst], b=b,
               alive=proc.poll() is None)
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        good = list(pool.map(lambda i: request(18871 if i % 2 else 18872), range(2000)))
    record('2000_healthy_requests', failures=sum(r.get('status') != 200 for r in good),
           alive=proc.poll() is None)
    record('dynamic_import_loop', result=request(path='/dynamic'), b=request(18872), a_next=request())

env = os.environ.copy()
env['WORKERD_EXPERIMENTAL_CPU_MS'] = '0'
try:
    startup = subprocess.run([BINARY, 'serve', 'startup-loop.capnp'], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=.5)
    record('startup_loop_unlimited', exit_code=startup.returncode)
except subprocess.TimeoutExpired:
    record('startup_loop_unlimited', timed_out=True)
env['WORKERD_EXPERIMENTAL_CPU_MS'] = '50'
env['WORKERD_EXPERIMENTAL_STARTUP_CPU_MS'] = '100'
started = time.monotonic()
try:
    startup = subprocess.run([BINARY, 'serve', 'startup-loop.capnp'], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=5)
    (ROOT / 'startup.log').write_text(startup.stdout + startup.stderr)
    record('startup_loop', exit_code=startup.returncode,
           message=(startup.stdout + startup.stderr).strip(),
           wall_ms=round((time.monotonic() - started) * 1000, 3))
except subprocess.TimeoutExpired:
    record('startup_loop', error='timed out after 5 seconds')

cases = {case['name']: case for case in results['cases']}
checks = {
    'baseline_loop_blocks_b': 'error' in cases['baseline_infinite_loop']['b'],
    'io_wait_not_cpu': cases['io_wait_excluded']['result'].get('status') == 200,
    'other_cpu_not_charged': cases['other_worker_cpu_excluded']['a'].get('status') == 200 and
                             cases['other_worker_cpu_excluded']['process_cpu_ms'] > 50,
    'binding_works': cases['binding_works']['result'].get('body') == 'b-ok',
    'finite_cpu_works': cases['finite_cpu']['result'].get('status') == 200,
    'budget_accumulates': cases['cumulative_across_await']['result'].get('status', 0) >= 500,
    'no_false_kills': cases['2000_healthy_requests']['failures'] == 0,
    'boundary_no_stale_termination': cases['budget_boundary']['failures'] == 0,
    'attacks_contained': cases['100_attacks']['healthy_failures'] == [] and
                        all(status and status >= 500 for status in cases['100_attacks']['attack_statuses']),
    'startup_loop_stopped': cases['startup_loop_unlimited'].get('timed_out', False) and
                            cases['startup_loop'].get('exit_code', 0) != 0 and
                            cases['startup_loop'].get('wall_ms', 5000) < 1000,
    'dynamic_import_contained': cases['dynamic_import_loop']['result'].get('status', 0) >= 500 and
                                cases['dynamic_import_loop']['b'].get('status') == 200 and
                                cases['dynamic_import_loop']['a_next'].get('status') == 200,
    'burst_contained': cases['five_concurrent_attacks']['b'].get('status') == 200 and
                       all(r.get('status', 0) >= 500 for r in cases['five_concurrent_attacks']['attacks']),
}
for path in ['/loop', '/microtasks']:
    case = cases[path]
    checks[path] = case['a'].get('status', 0) >= 500 and case['b'].get('status') == 200 and case['a_next'].get('status') == 200
results['checks'] = checks
(ROOT / 'results.json').write_text(json.dumps(results, indent=2))
print(json.dumps(results, indent=2))
sys.exit(0 if all(checks.values()) else 1)
