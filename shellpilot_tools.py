import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil
import requests

from shellpilot_demo_web import HEALTH_BODY


class ToolError(Exception):
    pass


def state_dir():
    return Path(os.environ.get("SPT_STATE_DIR", str(Path.home() / ".local/state/shellpilot"))).expanduser().resolve()


def port_setting(name, default):
    value = int(os.environ.get(name, default))
    if not 1024 <= value <= 65535:
        raise ValueError(f"{name}: expected a TCP port between 1024 and 65535")
    return value


def session():
    client = requests.Session()
    client.trust_env = False
    return client


def executable(value):
    path = shutil.which(value)
    if path and Path(path).suffix.lower() not in {".bat", ".cmd"}:
        return str(Path(path).resolve())
    return None


def child_env():
    return {key: value for key, value in os.environ.items() if key != "SPT_MODEL_API_KEY"}


def checked(args, timeout, **options):
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout, env=child_env(), **options)
    except (OSError, subprocess.TimeoutExpired):
        raise ToolError(f"{Path(args[0]).name}: could not start or timed out ({timeout}s)") from None
    if result.returncode:
        detail = " ".join((result.stderr or result.stdout).split())[-480:]
        raise ToolError(f"{Path(args[0]).name}: exit {result.returncode}. {detail}")
    return result.stdout.strip()


def port_free(port):
    with socket.socket() as sock:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def resources(root):
    gib = 1024 ** 3
    cpu = psutil.cpu_percent(interval=0.25)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(str(root))
    details = [f"CPU: {cpu:.1f}% / {psutil.cpu_count()} logical cores",
               f"RAM: {mem.percent:.1f}% used; {mem.available/gib:.2f} / {mem.total/gib:.2f} GiB available",
               f"Disk ({root}): {disk.percent:.1f}% used; {disk.free/gib:.2f} / {disk.total/gib:.2f} GiB free"]
    smi = executable("nvidia-smi")
    if not smi:
        return details + ["NVIDIA GPU: nvidia-smi not installed; GPU usage not reported"]
    try:
        report = checked([smi, "--query-gpu=name,driver_version,utilization.gpu,memory.used,memory.total",
                          "--format=csv,noheader"], 5)
        return details + ["NVIDIA GPU (name, driver, usage, VRAM used/total): " + (report[:1200] or "none reported")]
    except ToolError as exc:
        return details + [f"NVIDIA GPU: unavailable ({exc})"]


class ProjectEnvironment:
    def __init__(self, root):
        self.project = root / "projects" / "demo-web"
        self.conda = executable(os.environ["SPT_CONDA_EXE"]) if os.environ.get("SPT_CONDA_EXE") else None

    def path(self, profile):
        path = self.project / {"venv": ".venv", "conda": ".conda-env"}[profile]
        if path.resolve() != path:
            raise ToolError("Environment target must not contain symbolic links")
        return path

    def preview(self, profile):
        path = self.path(profile)
        if profile == "conda" and not self.conda:
            raise ToolError("Conda is not configured; set SPT_CONDA_EXE or request Python venv")
        return [f"Target: {path}",
                "Verify managed environment; do not overwrite" if path.exists() else
                ("Create Python venv with bundled pip (offline)" if profile == "venv" else
                 "Create offline Conda environment: python=3.11, pip (requires cached packages)"),
                "Verify Python prefix and imports: pip, json, http.server"]

    def apply(self, profile, existed):
        path = self.path(profile)
        marker = path / ".shellpilot-managed"
        if path.exists() != existed:
            raise ToolError("Environment changed after preview; request a fresh plan")
        if existed and (not marker.is_file() or marker.is_symlink() or marker.read_text() != profile):
            raise ToolError("Target exists and is not managed by ShellPilot; left unchanged")
        if not existed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.mkdir()
        try:
            if not existed and profile == "venv":
                checked([sys.executable, "-I", "-m", "venv", str(path)], 100)
            if not existed and profile == "conda":
                checked([self.conda, "create", "--offline", "--yes", "--no-default-packages", "--prefix",
                         str(path), "python=3.11", "pip"], 150)
            if os.name == "nt":
                python = path / ("Scripts/python.exe" if profile == "venv" else "python.exe")
            else:
                python = path / "bin/python"
            verify = "import http.server,json,pip,sys; print(json.dumps([sys.prefix,sys.version.split()[0],pip.__version__]))"
            prefix, version, pip_version = json.loads(checked([str(python), "-I", "-c", verify], 10))
            if Path(prefix).resolve() != path or (profile == "conda" and not version.startswith("3.11.")):
                raise ToolError("Unexpected environment prefix or Python version")
            if not existed:
                marker.write_text(profile, encoding="ascii")
            return [f"Python {version}, pip {pip_version}; import json/http.server: OK", f"Verified prefix: {prefix}"]
        except Exception as exc:
            if not existed:
                # Only remove the directory created by this operation.
                try:
                    shutil.rmtree(path)
                except OSError:
                    raise ToolError(f"Environment failed; cleanup failed, inspect {path}") from None
            detail = str(exc) if isinstance(exc, ToolError) else "Environment verification failed"
            suffix = "; removed the incomplete environment" if not existed else "; existing environment left unchanged"
            raise ToolError(detail + suffix) from None


class ManagedNginx:
    expected = HEALTH_BODY

    def __init__(self, root):
        self.root = root
        self.backend_port = port_setting("SPT_BACKEND_PORT", 18081)
        self.front_port = port_setting("SPT_FRONT_PORT", 18080)
        self.stale_port = port_setting("SPT_STALE_PORT", 18082)
        if len({self.backend_port, self.front_port, self.stale_port, port_setting("SPT_AGENT_PORT", 8000)}) != 4:
            raise ValueError("Agent, backend, front and stale ports must be distinct")
        self.exe = executable(os.environ.get("SPT_NGINX_EXE", "nginx"))
        self.path = None
        self.proc = None
        self.active = None
        self.marker = os.urandom(8).hex()
        self.revision = 0

    def url(self, port):
        return f"http://127.0.0.1:{port}/health"

    def probe(self, port):
        try:
            with session() as client, client.get(self.url(port), timeout=(0.7, 5), stream=True,
                                                  allow_redirects=False) as response:
                marker = response.headers.get("X-ShellPilot-Route", "")
                healthy = (response.status_code == 200 and response.headers.get("Content-Length") == str(len(self.expected))
                           and next(response.iter_content(chunk_size=len(self.expected)), b"") == self.expected)
                return response.status_code, healthy, marker
        except (requests.RequestException, OSError, ValueError):
            return None, False, ""

    def _marker(self, upstream):
        return f"{self.marker}-{upstream}"

    def _pid(self):
        return self.proc.pid if self.proc is not None and self.proc.poll() is None else None

    def snapshot(self):
        return self.revision, self._pid()

    def inspect(self):
        back, healthy, _ = self.probe(self.backend_port)
        front, front_healthy, marker = self.probe(self.front_port)
        own = self.active is not None and self._pid() is not None and marker == self._marker(self.active)
        line = (f"Diagnosis: stale upstream 127.0.0.1:{self.stale_port}; backend is healthy"
                if own and self.active == self.stale_port and front == 502 and healthy else
                "Diagnosis: managed route is healthy" if own and front_healthy else
                "Diagnosis: no verified healthy or stale managed route")
        details = [f"Backend {self.url(self.backend_port)}: {back or 'unreachable'}; expected body: {'yes' if healthy else 'no'}",
                   f"Nginx entry {self.url(self.front_port)}: {front or 'unreachable'}; managed: {'yes' if own else 'no'}", line]
        return details, healthy, own, front

    def preview(self, action):
        if not self.exe:
            raise ToolError("Nginx binary not found; install nginx or set SPT_NGINX_EXE")
        details, healthy, own, front = self.inspect()
        if not healthy:
            raise ToolError("Registered backend did not return the expected /health response")
        if action == "repair" and not (own and self.active == self.stale_port and front == 502):
            raise ToolError("Repair refused: no verified stale route with HTTP 502")
        if action == "fault" and not (own and self.active == self.backend_port and front == 200 and port_free(self.stale_port)):
            raise ToolError("Fault fixture requires a healthy managed route and a free stale port")
        if self._pid() is not None and not own:
            raise ToolError("Managed Nginx ownership not verified; no change made")
        if self._pid() is None and not port_free(self.front_port):
            raise ToolError("Front port is occupied; existing services are not changed")
        upstream = self.stale_port if action == "fault" else self.backend_port
        result = "HTTP 502 (fixture)" if action == "fault" else "HTTP 200 + expected response body"
        return details + [f"Proposed route: 127.0.0.1:{self.front_port} -> 127.0.0.1:{upstream}",
                          f"Run nginx -t, start/reload only the managed instance, verify {result}"]

    def _conf(self, port):
        return f"""worker_processes 1;
error_log logs/error.log warn;
pid nginx.pid;
events {{ worker_connections 64; }}
http {{
    access_log off;
    client_body_temp_path temp/client_body;
    proxy_temp_path temp/proxy;
    fastcgi_temp_path temp/fastcgi;
    uwsgi_temp_path temp/uwsgi;
    scgi_temp_path temp/scgi;
    client_max_body_size 1m;
    client_body_timeout 5s;
    client_header_timeout 5s;
    keepalive_timeout 5s;
    server {{
        listen 127.0.0.1:{self.front_port};
        location / {{
            proxy_connect_timeout 4s;
            proxy_send_timeout 4s;
            proxy_read_timeout 4s;
            proxy_hide_header X-ShellPilot-Route;
            add_header X-ShellPilot-Route {self._marker(port)} always;
            proxy_pass http://127.0.0.1:{port};
        }}
    }}
}}
"""

    def _write(self, text):
        stage = self.path / "nginx.new"
        stage.write_text(text, encoding="ascii")
        stage.replace(self.path / "nginx.conf")

    def _args(self):
        return [self.exe, "-p", self.path.as_posix() + "/", "-c", "nginx.conf", "-e", "stderr", "-g", "daemon off;"]

    def _test(self):
        checked([*self._args(), "-t"], 5, cwd=self.path)

    def _signal(self, action):
        if not self._pid() or int((self.path / "nginx.pid").read_text()) != self._pid():
            raise ToolError("Managed Nginx PID mismatch; signal refused")
        checked([*self._args(), "-s", action], 5, cwd=self.path)

    def _start(self):
        if not port_free(self.front_port):
            raise ToolError("Front port is no longer free")
        with (self.path / "logs/startup.log").open("ab") as out:
            self.proc = subprocess.Popen(self._args(), cwd=self.path, stdin=subprocess.DEVNULL,
                                         stdout=out, stderr=out, env=child_env())

    def _verify(self, upstream):
        until = time.monotonic() + 9
        while time.monotonic() < until and self._pid():
            code, healthy, marker = self.probe(self.front_port)
            if marker == self._marker(upstream) and (code == 502 if upstream == self.stale_port else healthy):
                return code
            time.sleep(0.15)
        raise ToolError("Managed Nginx route did not return expected status/body")

    def apply(self, action, snapshot):
        if self.snapshot() != snapshot:
            raise ToolError("Route changed after preview; request a fresh plan")
        self.preview(action)
        previous = self.active if self._pid() else None
        target = self.stale_port if action == "fault" else self.backend_port
        if previous == target:
            code = self._verify(target)
            return ["Route already configured; no change made", f"Verified {self.url(self.front_port)}: HTTP {code}"]
        if previous is None:
            self.path = Path(tempfile.mkdtemp(prefix="nginx-", dir=self.root))
            (self.path / "logs").mkdir()
            (self.path / "temp").mkdir()
        changed_process = False
        self.revision += 1
        try:
            self._write(self._conf(target))
            self._test()
            changed_process = True
            self._start() if previous is None else self._signal("reload")
            code = self._verify(target)
            self.active = target
            return [f"nginx -t: OK; managed Nginx PID {self._pid()} {'started' if previous is None else 'reloaded'}",
                    f"Verified {self.url(self.front_port)}: HTTP {code}" + ("; expected body matched" if code == 200 else "; fixture active")]
        except (ToolError, OSError, ValueError) as exc:
            try:
                if previous is None:
                    self.close()
                    message = "Stopped the new instance (no earlier route)"
                else:
                    self._write(self._conf(previous))
                    if changed_process:
                        self._test()
                        self._signal("reload")
                    self._verify(previous)
                    message = "Restored and verified the earlier route"
            except (ToolError, OSError, ValueError):
                message = f"ROLLBACK NOT VERIFIED; inspect {self.path}"
            raise ToolError(f"{exc}; {message}") from None

    def close(self):
        if self._pid():
            try:
                self._signal("quit")
                self.proc.wait(timeout=5)
            except (ToolError, OSError, ValueError, subprocess.TimeoutExpired):
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=3)
        self.active = None
