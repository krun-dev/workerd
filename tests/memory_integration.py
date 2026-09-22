"""Per-isolate retirement tests against the real Linux workerd binary; Python stdlib only."""
import concurrent.futures
import contextlib
import http.client
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent
FIXTURES = ROOT / 'fixtures' if (ROOT / 'fixtures').exists() else ROOT
BINARY = str(pathlib.Path(sys.argv[1]).resolve())
RESULTS = {'cases': [], 'checks': {}}
CURRENT_CASE = ''


def request(port, path='/', timeout=8):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    started = time.monotonic()
    try:
        connection.request('GET', path)
        response = connection.getresponse()
        return {'status': response.status, 'body': response.read().decode(),
                'wall_ms': round((time.monotonic() - started) * 1000, 2)}
    finally:
        connection.close()


def wait_for(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(.02)
    raise AssertionError('condition did not become true before timeout')


class Server:
    def __init__(self, directory, mb, cpu=0, startup=False, external_port=1, durable=False, drain=500):
        self.directory = pathlib.Path(directory)
        listeners = [socket.socket(), socket.socket()]
        for listener in listeners:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
        self.a, self.b = (listener.getsockname()[1] for listener in listeners)
        a_code = (FIXTURES / 'memory-a.js').read_text()
        if startup:
            a_code = 'const junk = []; for (;;) junk.push(new Array(16384).fill(1));\n' + a_code
        if durable:
            a_code += '\nexport class Counter { fetch() { return new Response("counter"); } }\n'
        (self.directory / 'a.js').write_text(a_code)
        (self.directory / 'b.js').write_text((FIXTURES / 'memory-b.js').read_text())
        config = '''using Workerd = import "/workerd/workerd.capnp";
const config :Workerd.Config = (
  services = [
    (name = "a", worker = (compatibilityDate = "2026-09-20",
      modules = [(name = "a.js", esModule = embed "a.js")],
      bindings = [(name = "B", service = "b"), (name = "UPSTREAM", service = "upstream")])),
    (name = "b", worker = (compatibilityDate = "2026-09-20",
      modules = [(name = "b.js", esModule = embed "b.js")],
      bindings = [(name = "A", service = "a"),
                  (name = "RPC", service = (name = "a", entrypoint = "Rpc"))])),
    (name = "upstream", external = (address = "127.0.0.1:PORT_EXTERNAL", http = ()))
  ], sockets = [
    (name = "a", address = "127.0.0.1:PORT_A", http = (), service = "a"),
    (name = "b", address = "127.0.0.1:PORT_B", http = (), service = "b")
  ]
);
'''.replace('PORT_A', str(self.a)).replace('PORT_B', str(self.b)).replace('PORT_EXTERNAL', str(external_port))
        if durable:
            config = config.replace('worker = (compatibilityDate',
                'worker = (durableObjectNamespaces = [(className = "Counter", uniqueKey = "counter")], '
                'durableObjectStorage = (inMemory = void), compatibilityDate', 1)
        (self.directory / 'config.capnp').write_text(config)
        self.log_path = self.directory / 'workerd.log'
        self.log = self.log_path.open('w')
        env = {k: v for k, v in os.environ.items() if not k.startswith('WORKERD_EXPERIMENTAL_')}
        env.update(WORKERD_EXPERIMENTAL_MEMORY_MB=str(mb), WORKERD_EXPERIMENTAL_CPU_MS=str(cpu),
                   WORKERD_EXPERIMENTAL_MEMORY_DRAIN_MS=str(drain))
        # Keep both ports reserved through exec; the CLI adopts these listening sockets.
        try:
            self.proc = subprocess.Popen(
                [BINARY, 'serve', 'config.capnp',
                 f'--socket-fd=a={listeners[0].fileno()}',
                 f'--socket-fd=b={listeners[1].fileno()}'],
                pass_fds=tuple(listener.fileno() for listener in listeners),
                cwd=self.directory, stdout=self.log, stderr=self.log, env=env)
        finally:
            for listener in listeners:
                listener.close()
        # A failed limiter test must not exhaust the test machine. This is a test harness
        # emergency stop, never counted as successful workerd enforcement.
        self.watch_stop = threading.Event()
        self.watch_killed = False

        def monitor():
            while not self.watch_stop.wait(.05) and self.proc.poll() is None:
                try:
                    if self.rss() > 768 * 1024:
                        self.watch_killed = True
                        self.proc.kill()
                        return
                except (OSError, TypeError):
                    return
        self.watcher = threading.Thread(target=monitor, daemon=True)
        self.watcher.start()

    def ready(self):
        def check():
            if self.proc.poll() is not None:
                raise AssertionError(f'workerd exited {self.proc.returncode}: {self.logs()}')
            try:
                return request(self.a, timeout=.1)['body'] == 'a-ok'
            except OSError:
                return False
        wait_for(check)

    def logs(self):
        return self.log_path.read_text()

    def disposed(self):
        return 'isolate disposed; name = a' in self.logs()

    def stopped(self):
        wait_for(self.disposed)
        assert self.proc.poll() is None, self.logs()
        for _ in range(10):
            assert request(self.a)['body'] == 'a-ok'
        assert request(self.b)['body'] == 'b-ok'
        assert request(self.b, '/binding/')['body'] == 'a-ok'
        assert json.loads(request(self.a, '/state')['body'])['retained'] == 0

    def rss(self):
        for line in pathlib.Path(f'/proc/{self.proc.pid}/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1])

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=5)
        self.watch_stop.set()
        self.watcher.join(timeout=1)
        self.log.close()


@contextlib.contextmanager
def server(mb=64, cpu=0, startup=False, external_port=1, durable=False, drain=500):
    with tempfile.TemporaryDirectory(prefix='workerd-memory-') as directory:
        instance = Server(directory, mb, cpu, startup, external_port, durable, drain)
        try:
            if not startup and not durable:
                instance.ready()
            yield instance
        except BaseException:
            print(instance.logs()[-20000:], file=sys.stderr)
            raise
        finally:
            instance.close()
            (ROOT / f'memory-{CURRENT_CASE}.log').write_text(instance.logs())
            assert not instance.watch_killed, 'test watchdog killed workerd above 768 MiB RSS'


def run_case(name, function):
    global CURRENT_CASE
    CURRENT_CASE = name
    started = time.monotonic()
    try:
        data = function() or {}
        RESULTS['checks'][name] = True
    except Exception as error:
        data = {'error': repr(error)}
        RESULTS['checks'][name] = False
    entry = {'name': name, 'seconds': round(time.monotonic() - started, 2), **data}
    RESULTS['cases'].append(entry)
    print(json.dumps(entry), flush=True)
    (ROOT / 'memory-results.json').write_text(json.dumps(RESULTS, indent=2))


def allocation(path, cpu=0):
    with server(cpu=cpu) as s:
        baseline = s.rss()
        result = request(s.a, path)
        assert result['status'] == 503, result
        s.stopped()
        return {'response': result, 'rss_before_kib': baseline, 'rss_after_kib': s.rss(),
                'isolate_disposed': True}


def cumulative():
    with server() as s:
        peak = s.rss()
        for count in range(40):
            result = request(s.a, '/retain')
            peak = max(peak, s.rss())
            if 'retiring worker; name = a' in s.logs():
                break
            assert result['status'] == 200
        else:
            raise AssertionError('retained buffers did not hit the limit')
        wait_for(s.disposed)
        after = s.rss()
        s.stopped()
        assert peak - after > 24 * 1024, (peak, after)
        return {'requests_until_stopped': count + 1, 'peak_rss_kib': peak, 'after_rss_kib': after}


def pending_and_background():
    with server() as s:
        assert request(s.a, '/arm')['status'] == 200
        wait_for(lambda: int(request(s.b, '/ticks')['body']) >= 3)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            pending = [pool.submit(request, s.a, '/pending') for _ in range(3)]
            time.sleep(.1)
            assert request(s.a, '/heap')['status'] == 503
            responses = [future.result(timeout=3) for future in pending]
            assert all(r['status'] == 503 for r in responses), responses
        s.stopped()
        ticks = request(s.b, '/ticks')['body']
        time.sleep(.15)
        assert request(s.b, '/ticks')['body'] == ticks
        return {'pending_responses': responses, 'background_stopped_at': int(ticks)}


def streaming():
    with server() as s:
        connection = http.client.HTTPConnection('127.0.0.1', s.a, timeout=3)
        try:
            connection.request('GET', '/stream')
            response = connection.getresponse()
            assert response.status == 200
            assert response.read(8) == b'started\n'
            assert request(s.a, '/heap')['status'] == 503
            try:
                rest = response.read()
            except http.client.IncompleteRead:
                pass
            else:
                assert rest == b'', 'stopped worker completed the stream'
            s.stopped()
        finally:
            connection.close()


def disabled():
    with server(mb=0) as s:
        assert request(s.a, '/large-buffer')['status'] == 200
        assert request(s.b, '/binding/')['body'] == 'a-ok'
        assert request(s.b, '/rpc')['body'] == 'rpc-ok'
        assert not s.disposed()


def rpc_rejected():
    with server() as s:
        assert request(s.b, '/rpc')['body'] == 'rpc-rejected'
        assert request(s.a)['body'] == 'a-ok'


def websocket():
    with server() as s:
        with socket.create_connection(('127.0.0.1', s.a), timeout=3) as connection:
            connection.sendall(b'GET /ws HTTP/1.1\r\nHost: localhost\r\n'
                               b'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                               b'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n'
                               b'Sec-WebSocket-Version: 13\r\n\r\n')
            headers = b''
            while b'\r\n\r\n' not in headers:
                chunk = connection.recv(4096)
                assert chunk
                headers += chunk
            assert headers.startswith(b'HTTP/1.1 101'), headers
            assert request(s.a, '/heap')['status'] == 503
            # The revocation helper closes the transport; tolerate a close frame first.
            while connection.recv(4096):
                pass
            s.stopped()


def external_fetch_cancelled():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        listener.settimeout(5)
        received = threading.Event()

        def upstream():
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(5)
                headers = b''
                while b'\r\n\r\n' not in headers:
                    data = connection.recv(4096)
                    assert data
                    headers += data
                received.set()
                assert connection.recv(1) == b'', 'upstream connection was not closed'

        with server(external_port=listener.getsockname()[1]) as s:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                peer = pool.submit(upstream)
                pending = pool.submit(request, s.a, '/external')
                assert received.wait(3), 'outbound request never reached the peer'
                assert request(s.a, '/heap')['status'] == 503
                assert pending.result(timeout=3)['status'] == 503
                peer.result(timeout=3)
            s.stopped()


def startup():
    with server(startup=True) as s:
        status = s.proc.wait(timeout=8)
        assert status > 0, (status, s.logs())
        assert 'experimental memory limit: stopping worker' in s.logs()
        assert 'Fatal process out of memory' not in s.logs()
        return {'exit_code': status}


def durable_rejected():
    with server(durable=True) as s:
        status = s.proc.wait(timeout=8)
        assert status > 0, (status, s.logs())
        assert 'stateless workers only' in s.logs(), s.logs()
        return {'exit_code': status}


def graceful_requests():
    with server(drain=1200) as s:
        original = json.loads(request(s.a, '/state')['body'])['id']
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            slow = pool.submit(request, s.a, '/slow')
            wait_for(lambda: int(request(s.b, '/ticks')['body']) >= 1)
            soft = request(s.a, '/soft')
            assert soft['status'] == 200 and soft['body'] == original, soft
            assert not s.disposed(), 'in-flight request was not allowed to finish'
            queued = [pool.submit(request, s.a, '/state') for _ in range(8)]
            assert request(s.b)['body'] == 'b-ok'
            assert slow.result()['body'] == original
            responses = [f.result() for f in queued]
        states = [json.loads(r['body']) for r in responses]
        assert len({state['id'] for state in states}) == 1, states
        assert states[0]['id'] != original and states[0]['retained'] == 0, states
        wait_for(s.disposed)
        assert soft['wall_ms'] < 600, soft
        return {'new_generation': states[0]['id'], 'queued_requests': len(states)}


def idle_retirement():
    with server(drain=2000) as s:
        original = json.loads(request(s.a, '/state')['body'])['id']
        started = time.monotonic()
        assert request(s.a, '/soft')['status'] == 200
        # No new request to A is needed for destruction of global state.
        wait_for(s.disposed, timeout=1)
        elapsed = round((time.monotonic() - started) * 1000, 2)
        assert request(s.b)['body'] == 'b-ok'
        fresh = json.loads(request(s.b, '/binding/state')['body'])
        assert fresh['id'] != original and fresh['retained'] == 0, fresh
        return {'dispose_ms_without_new_requests': elapsed}


def background_grace():
    with server(drain=1500) as s:
        assert request(s.a, '/background-once')['status'] == 200
        assert request(s.a, '/soft')['status'] == 200
        assert not s.disposed()
        wait_for(s.disposed)
        assert int(request(s.b, '/ticks')['body']) == 1
        s.stopped()


def bounded_drain():
    with server(drain=400) as s:
        assert request(s.a, '/arm')['status'] == 200
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(request, s.a, '/pending')
            time.sleep(.08)
            start = time.monotonic()
            assert request(s.a, '/soft')['status'] == 200
            assert not s.disposed()
            result = pending.result(timeout=3)
            elapsed = (time.monotonic() - start) * 1000
            assert result['status'] == 503 and 300 <= elapsed < 2000, (result, elapsed)
        s.stopped()
        ticks = request(s.b, '/ticks')['body']
        time.sleep(.1)
        assert request(s.b, '/ticks')['body'] == ticks
        return {'drain_ms': round(elapsed, 2)}


def synchronous_drain_deadline(path='/retire-spin'):
    with server(drain=300, cpu=0) as s:
        result = request(s.a, path)
        assert result['status'] == 503 and 200 <= result['wall_ms'] < 2000, result
        s.stopped()
        return result


def stream_grace():
    with server(drain=1500) as s:
        connection = http.client.HTTPConnection('127.0.0.1', s.a, timeout=3)
        try:
            connection.request('GET', '/short-stream')
            response = connection.getresponse()
            assert response.read(8) == b'started\n'
            assert request(s.a, '/soft')['status'] == 200
            assert not s.disposed()
            assert response.read() == b'finished\n'
            wait_for(s.disposed)
            s.stopped()
        finally:
            connection.close()


def websocket_grace():
    with server(drain=500) as s:
        with socket.create_connection(('127.0.0.1', s.a), timeout=3) as connection:
            connection.sendall(b'GET /ws HTTP/1.1\r\nHost: localhost\r\n'
                               b'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                               b'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n'
                               b'Sec-WebSocket-Version: 13\r\n\r\n')
            headers = b''
            while b'\r\n\r\n' not in headers:
                headers += connection.recv(4096)
            assert headers.startswith(b'HTTP/1.1 101'), headers
            started = time.monotonic()
            assert request(s.a, '/soft')['status'] == 200
            # Masked client text frame; echo proves old JS still runs during retirement.
            connection.sendall(b'\x81\x82\x01\x02\x03\x04ig')
            echo = b''
            while len(echo) < 4:
                chunk = connection.recv(4 - len(echo))
                assert chunk, 'WebSocket closed before the drain deadline'
                echo += chunk
            assert echo == b'\x81\x02he'
            assert not s.disposed()
            while connection.recv(4096):
                pass
            elapsed = (time.monotonic() - started) * 1000
            assert 350 <= elapsed < 2000, elapsed
            s.stopped()
            return {'connection_closed_ms': round(elapsed, 2)}


def repeated_generations():
    with server() as s:
        ids = set()
        for i in range(12):
            state = json.loads(request(s.a, '/state')['body'])
            assert state['id'] not in ids and state['retained'] == 0, state
            ids.add(state['id'])
            assert request(s.a, '/soft')['status'] == 200
            wait_for(lambda: s.logs().count('isolate disposed; name = a') >= i + 1)
            assert request(s.b, '/binding/')['body'] == 'a-ok'
        assert s.proc.poll() is None
        return {'generations': len(ids), 'rss_kib': s.rss()}


def immediate_mode():
    with server(drain=0) as s:
        result = request(s.a, '/soft')
        assert result['status'] == 503, result
        s.stopped()


cases = {
    'graceful_requests': graceful_requests,
    'idle_retirement': idle_retirement,
    'background_grace': background_grace,
    'bounded_drain': bounded_drain,
    'synchronous_drain_deadline': synchronous_drain_deadline,
    'gc_drain_deadline': lambda: synchronous_drain_deadline('/gc-spin'),
    'stream_grace': stream_grace,
    'websocket_grace': websocket_grace,
    'repeated_generations': repeated_generations,
    'immediate_mode': immediate_mode,
    'disabled': disabled,
    'heap': lambda: allocation('/heap'),
    'native-iterator': lambda: allocation('/native-iterator'),
    'buffers': lambda: allocation('/buffers'),
    'large-buffer': lambda: allocation('/large-buffer'),
    'cumulative_and_rss_released': cumulative,
    'pending_and_background_cancelled': pending_and_background,
    'stream_aborted': streaming,
    'websocket_closed': websocket,
    'external_fetch_cancelled': external_fetch_cancelled,
    'rpc_rejected': rpc_rejected,
    'memory_with_cpu_budget': lambda: allocation('/buffers', cpu=1000),
    'startup_memory': startup,
    'durable_objects_rejected': durable_rejected,
}
selected = set(sys.argv[2:]) or set(cases)
if selected - cases.keys():
    raise SystemExit(f'Unknown cases: {selected - cases.keys()}')
for name, function in cases.items():
    if name in selected:
        run_case(name, function)
sys.exit(0 if all(RESULTS['checks'].values()) else 1)
