import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_server import DEMO_PLANS, MODEL_PROBES, PLAN_SCHEMA, app, model_plan
from shellpilot_cli import ask
from shellpilot_demo_web import DemoWeb
from shellpilot_tools import ToolError, executable, session


PHRASE = {plan.action: text for text, plan in DEMO_PLANS.items() if plan.profile != "conda"}


def free_ports(count):
    with ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in range(count)]
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]


class FakeModel(BaseHTTPRequestHandler):
    content = '{"action":"resources"}'
    received = None

    def respond(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.models_request = (self.path, self.headers.get("Authorization"))
        self.respond({"data": [{"id": "spt-test"}]}, getattr(self.server, "models_status", 200))

    def do_POST(self):
        self.server.received = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = self.server.received["messages"][1]["content"]
        content = getattr(self.server, "answers", {}).get(text, getattr(self.server, "content", self.content))
        self.respond({"choices": [{"message": {"content": content}}]})

    def log_message(self, *args):
        pass


class ShellPilotTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root, agent, backend, front, stale = self.stack.enter_context(tempfile.TemporaryDirectory()), *free_ports(4)
        self.root = Path(root)
        self.stack.enter_context(patch.dict(os.environ, {"SPT_STATE_DIR": root, "SPT_AGENT_PORT": str(agent),
                                                        "SPT_BACKEND_PORT": str(backend), "SPT_FRONT_PORT": str(front),
                                                        "SPT_STALE_PORT": str(stale), "SPT_ENABLE_FIXTURE": "1",
                                                        "SPT_MODEL_MODE": "local", "SPT_MODEL_API_KEY": "test-secret"}))
        self.api = self.stack.enter_context(TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 10000)))
        self.auth = {"Authorization": "Bearer " + app.state.token}
        self.agent = app.state.agent

    def start_http(self, handler, port=0):
        server = self.stack.enter_context(ThreadingHTTPServer(("127.0.0.1", port), handler))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.stack.callback(server.shutdown)
        return server

    def run_task(self, **fields):
        response = self.api.post("/agent/run", json=fields, headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def preview(self, action):
        result = self.run_task(fixture=True) if action == "fault" else self.run_task(user_input=PHRASE[action], demo=True)
        self.assertEqual(result["status"], "pending", result)
        return result

    def decide(self, preview, approve=True, status="verified"):
        response = self.api.post("/agent/decision", headers=self.auth,
                                 json={"plan_id": preview["task_id"], "approve": approve})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], status, response.json())
        return response.json()

    def test_resources_and_audit(self):
        result = self.run_task(user_input=PHRASE["resources"], demo=True)
        self.assertEqual(result["status"], "verified", result)
        for label in ("CPU:", "RAM:", "Disk (", "NVIDIA GPU"):
            self.assertTrue(any(line.startswith(label) for line in result["details"]), result)
        audit = (self.root / "audit.jsonl").read_text(encoding="utf-8")
        event = json.loads(audit)
        self.assertEqual(event["risk"], "read_only")
        self.assertNotIn("test-secret", audit)
        self.assertNotIn(PHRASE["resources"], audit)

    def test_real_environment_and_refusal(self):
        path = self.agent.env.path("venv")
        first = self.preview("environment")
        self.decide(first, False, "cancelled")
        self.assertFalse(path.exists())
        second = self.preview("environment")
        details = self.decide(second)["details"]
        self.assertIn("import json/http.server: OK", details[0])
        self.assertTrue((path / "pyvenv.cfg").is_file())
        self.decide(second, status="expired")

    def test_failed_environment_is_cleaned(self):
        pending = self.preview("environment")
        with patch("shellpilot_tools.checked", side_effect=ToolError("simulated failure")):
            response = self.decide(pending, status="failed")
        self.assertIn("removed the incomplete environment", response["details"][0])
        self.assertFalse(self.agent.env.path("venv").exists())

    def test_expired_and_unmanaged_environment_are_not_changed(self):
        first = self.preview("environment")
        self.agent.pending[first["task_id"]].expires = 0
        self.decide(first, status="expired")
        path = self.agent.env.path("venv")
        path.mkdir(parents=True)
        file = path / "existing.txt"
        file.write_text("keep", encoding="ascii")
        self.decide(self.preview("environment"), status="failed")
        self.assertEqual(file.read_text(encoding="ascii"), "keep")

    def test_blocked_auth_and_payload(self):
        self.assertEqual(self.api.post("/agent/run", json={}).status_code, 401)
        self.assertEqual(self.api.post("/agent/run", json={}, headers={**self.auth, "Origin": "https://example.org"}).status_code, 403)
        self.assertEqual(self.api.post("/agent/run", json={"demo": "true"}, headers=self.auth).status_code, 422)
        self.assertEqual(self.api.post("/agent/run", json={"user_input": "x" * 9000}, headers=self.auth).status_code, 413)
        with patch("agent_server.model_plan") as planner:
            self.assertEqual(self.run_task(user_input="sudo rm -rf /")["status"], "blocked")
            planner.assert_not_called()
        with patch("shellpilot_cli.sys.stdin.isatty", return_value=False):
            self.assertFalse(ask())
        with patch.object(self.agent, "fixture_enabled", False):
            self.assertEqual(self.run_task(fixture=True)["status"], "failed")

    def test_model_http_strict_plan(self):
        service = self.start_http(FakeModel)
        service.content = '{"action":"resources"}'
        with patch.dict(os.environ, {"SPT_MODEL_URL": f"http://127.0.0.1:{service.server_port}/v1/chat/completions"}):
            task = "看看这台机器的内存"
            self.assertEqual(self.run_task(user_input=task)["status"], "verified")
            self.assertEqual(service.received["messages"][1]["content"], task)
            self.assertEqual(service.received["response_format"], {"type": "json_object", "schema": PLAN_SCHEMA})
            for bad in ('{"action":"deploy","port":80}', 'echo bad', '{"action":"fault"}'):
                service.content = bad
                self.assertEqual(self.run_task(user_input=task)["status"], "failed")
            service.content = '{"action":"deploy"}'
            self.assertEqual(self.run_task(user_input="把另一个项目接到 Nginx")["status"], "blocked")
            self.assertEqual(self.agent.pending, {})
            self.assertIsNone(self.agent.proxy.proc)
        with patch.dict(os.environ, {"SPT_MODEL_URL": "http://example.org/model", "SPT_MODEL_MODE": "cloud"}):
            with self.assertRaisesRegex(ToolError, "HTTPS"):
                model_plan("query")

    def test_cli_help(self):
        result = subprocess.run([sys.executable, "-X", "utf8", "shellpilot_cli.py", "--help"],
                                cwd=Path(__file__).resolve().parent, text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--demo-fault", result.stdout)
        self.assertIn("--check-model", result.stdout)

    def test_check_model_without_running_tools(self):
        fake = self.start_http(FakeModel)
        fake.answers = {text: plan.model_dump_json(exclude_none=True) for text, plan in MODEL_PROBES}
        path = "/agent/check-model"
        with patch.dict(os.environ, {"SPT_MODEL_URL": f"http://127.0.0.1:{fake.server_port}/v1/chat/completions",
                                     "SPT_MODEL_NAME": "spt-test"}):
            before = set(self.root.rglob("*"))
            self.assertEqual(self.api.get(path).status_code, 401)
            self.assertFalse(hasattr(fake, "models_request"))
            data = self.api.get(path, headers=self.auth).json()
            self.assertTrue(data["ok"], data)
            self.assertEqual([c["check"] for c in data["checks"]],
                             ["models", "environment/venv", "resources", "deploy", "repair"])
            self.assertEqual(fake.models_request, ("/v1/models", "Bearer test-secret"))
            self.assertEqual(fake.received["model"], "spt-test")
            fake.answers[MODEL_PROBES[0][0]] = '{"action":"resources"}'
            fake.answers[MODEL_PROBES[1][0]] = '{"action":"resources","port":80}'
            checks = self.api.get(path, headers=self.auth).json()["checks"]
            self.assertEqual([c["ok"] for c in checks], [True, False, False, True, True])
            self.assertIn("allowed tool schema", checks[2]["detail"])
            with patch.dict(os.environ, {"SPT_MODEL_NAME": "wrong-id"}):
                data = self.api.get(path, headers=self.auth).json()
                self.assertFalse(data["ok"])
                self.assertIn("not listed", data["checks"][0]["detail"])
            fake.models_status = 503
            data = self.api.get(path, headers=self.auth).json()
            self.assertFalse(data["ok"])
            self.assertIn("HTTP 503", data["checks"][0]["detail"])
            self.assertEqual(set(self.root.rglob("*")), before)
            self.assertEqual(self.agent.pending, {})
            self.assertIsNone(self.agent.proxy.proc)

    def test_cli_to_live_agent(self):
        project = Path(__file__).resolve().parent
        port = int(os.environ["SPT_AGENT_PORT"])
        fake = self.start_http(FakeModel)
        fake.answers = {text: plan.model_dump_json(exclude_none=True) for text, plan in MODEL_PROBES}
        env = {**os.environ, "SPT_STATE_DIR": str(self.root / "live"), "SPT_MODEL_NAME": "spt-test",
               "SPT_MODEL_URL": f"http://127.0.0.1:{fake.server_port}/v1/chat/completions"}
        with subprocess.Popen([sys.executable, "agent_server.py"], cwd=project, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env=env) as server:
            try:
                until = time.monotonic() + 12
                with session() as client:
                    while server.poll() is None and time.monotonic() < until:
                        try:
                            if client.get(f"http://127.0.0.1:{port}/health", timeout=1).ok:
                                break
                        except OSError:
                            time.sleep(0.1)
                    else:
                        self.fail("Live Agent failed to start")
                def cli(*args):
                    return subprocess.run([sys.executable, "-X", "utf8", "shellpilot_cli.py", *args], cwd=project,
                                          text=True, encoding="utf-8", capture_output=True, timeout=10, env=env)

                result = cli("--check-model")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.count("[PASS]"), 5)
                fake.models_status = 503
                result = cli("--check-model")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("[FAIL]", result.stdout)
                result = cli("--demo", PHRASE["resources"])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("CPU:", result.stdout)
            finally:
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()

    @unittest.skipUnless(executable(os.environ.get("SPT_NGINX_EXE", "nginx")), "real Nginx binary required")
    def test_real_nginx_deploy_fault_refuse_repair_and_rollback(self):
        proxy = self.agent.proxy
        backend = self.start_http(DemoWeb, proxy.backend_port)
        self.decide(self.preview("deploy"))
        pid = proxy.proc.pid
        self.assertEqual(proxy.probe(proxy.front_port)[:2], (200, True))
        self.decide(self.preview("fault"))
        self.assertEqual(proxy.probe(proxy.front_port)[0], 502)
        self.assertEqual(proxy.probe(backend.server_port)[:2], (200, True))
        read = self.run_task(user_input=PHRASE["diagnose"], demo=True)
        self.assertTrue(any("stale upstream" in line for line in read["details"]), read)
        self.decide(self.preview("repair"), False, "cancelled")
        self.assertEqual(proxy.probe(proxy.front_port)[0], 502)
        verify = proxy._verify

        def fail_new(port):
            if port == proxy.backend_port:
                raise ToolError("simulated new-route verification failure")
            return verify(port)

        with patch.object(proxy, "_verify", side_effect=fail_new):
            failure = self.decide(self.preview("repair"), status="failed")
        self.assertIn("Restored and verified the earlier route", failure["details"][0])
        self.assertEqual(proxy.probe(proxy.front_port)[0], 502)
        first, stale = self.preview("repair"), self.preview("repair")
        self.decide(first)
        self.decide(stale, status="failed")
        self.assertEqual(proxy.probe(proxy.front_port)[:2], (200, True))
        self.assertEqual(proxy.proc.pid, pid)


if __name__ == "__main__":
    unittest.main()
