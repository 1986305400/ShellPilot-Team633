import getpass
import json
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from shellpilot_tools import ManagedNginx, ProjectEnvironment, ToolError, port_setting, resources, session, state_dir


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Plan(StrictModel):
    action: Literal["environment", "resources", "deploy", "diagnose", "repair", "unsupported"]
    profile: Literal["venv", "conda"] | None = None

    @model_validator(mode="after")
    def check_profile(self):
        if (self.action == "environment") != (self.profile is not None):
            raise ValueError("Only environment plans require a profile")
        return self


class TaskRequest(StrictModel):
    user_input: str = Field(default="", max_length=500)
    demo: StrictBool = False
    fixture: StrictBool = False


class Decision(StrictModel):
    plan_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    approve: StrictBool


DEMO_PLANS = {
    "为 demo-web 配置 Python 虚拟环境": Plan(action="environment", profile="venv"),
    "为 demo-web 配置 Conda 环境": Plan(action="environment", profile="conda"),
    "检查当前 CPU、内存、磁盘和 GPU 使用情况": Plan(action="resources"),
    "用 Nginx 发布 demo-web 服务": Plan(action="deploy"),
    "诊断 demo-web 的 Nginx 502 故障": Plan(action="diagnose"),
    "诊断并修复 demo-web 的 Nginx 502 故障": Plan(action="repair"),
}
PROMPT = """你是 ShellPilot 的运维意图分类器。只返回一个 JSON 对象，不生成命令，不做操作。
只支持项目 demo-web。不相关的项目、指定端口或路径、多个不相关任务、危险操作，一律选 unsupported。
环境：创建 Python 隔离环境 -> {"action":"environment","profile":"venv"}；创建 Conda 环境 -> {"action":"environment","profile":"conda"}
巡检：查询板子资源、处理器、内存、存储或显卡 -> {"action":"resources"}
部署：用 Nginx 发布已有服务或给 demo-web 添加新入口 -> {"action":"deploy"}
修复：入口出故障时，用户要求修好、恢复访问、让网站重新可用。即使也要求检查原因，仍然是修复 -> {"action":"repair"}
诊断：只想知道为什么报错，不要求修复 -> {"action":"diagnose"}
其他 -> {"action":"unsupported"}
示例：定位 demo-web 的网关报错并让网页重新能打开 -> {"action":"repair"}
示例：只查 demo-web 网关报错原因，别改配置 -> {"action":"diagnose"}
"""
BLOCKED = re.compile(r"\b(?:sudo|chmod|chown|mkfs|iptables|nftables)\b|\brm\s+-\w*[rf]|"
                     r"\b(?:shutdown|reboot|poweroff)\b|\|\s*(?:ba)?sh\b|删除|关机|关闭防火墙", re.I)
PROJECT = re.compile(r"(?<![a-z0-9_-])demo-web(?![a-z0-9_-])", re.I | re.ASCII)


MODEL_PROBES = (
    ("给 demo-web 建一个隔离的 Python venv", Plan(action="environment", profile="venv")),
    ("看看这块板子的处理器、内存、存储和显卡占用", Plan(action="resources")),
    ("在 Nginx 上给 demo-web 加一个访问入口", Plan(action="deploy")),
    ("demo-web 入口返回 502，请查出原因并恢复访问", Plan(action="repair")),
)

PLAN_SCHEMA = {"anyOf": [
    {"type": "object", "properties": {"action": {"const": "environment"}, "profile": {"enum": ["venv", "conda"]}},
     "required": ["action", "profile"], "additionalProperties": False},
    {"type": "object", "properties": {"action": {"enum": ["resources", "deploy", "diagnose", "repair", "unsupported"]}},
     "required": ["action"], "additionalProperties": False},
]}


def model_settings():
    url = os.environ.get("SPT_MODEL_URL", "http://127.0.0.1:8081/v1/chat/completions")
    mode = os.environ.get("SPT_MODEL_MODE", "local")
    try:
        endpoint = urlsplit(url)
        _ = endpoint.port
    except ValueError:
        raise ToolError("Invalid model URL") from None
    is_loopback = endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
    if (endpoint.username or endpoint.password or not endpoint.hostname or
            not ((mode == "local" and is_loopback and endpoint.scheme in {"http", "https"}) or
                 (mode == "cloud" and endpoint.scheme == "https"))):
        raise ToolError("Model URL invalid: local requires loopback; cloud requires HTTPS")
    key = os.environ.get("SPT_MODEL_API_KEY")
    headers = {"Authorization": "Bearer " + key} if key else {}
    return url, mode, os.environ.get("SPT_MODEL_NAME", "qwen2-1.5b"), headers


def model_json(method, url, headers, payload=None, timeout=35, limit=65536):
    try:
        with session() as client, client.request(method, url, json=payload, headers=headers, timeout=(3, timeout),
                                                   stream=True, allow_redirects=False) as response:
            if response.status_code == 503:
                raise ToolError("Model service returned HTTP 503 (loading or unavailable); check server logs")
            if response.status_code != 200:
                raise ToolError(f"Model endpoint returned HTTP {response.status_code}")
            deadline = time.monotonic() + timeout
            body = bytearray()
            for chunk in response.iter_content(2048):
                body.extend(chunk)
                if len(body) > limit or time.monotonic() > deadline:
                    raise ToolError("Model response exceeded its limit")
        return json.loads(body)
    except requests.Timeout:
        raise ToolError("Model request timed out; check inference server logs") from None
    except requests.RequestException:
        raise ToolError("Model request failed; check model URL and service") from None
    except ValueError:
        raise ToolError("Model endpoint did not return valid JSON") from None


def model_plan(text):
    url, mode, name, headers = model_settings()
    payload = {"model": name, "temperature": 0,
               "max_tokens": 160, "messages": [{"role": "system", "content": PROMPT},
                                                 {"role": "user", "content": text}]}
    if mode == "local":
        payload["response_format"] = {"type": "json_object", "schema": PLAN_SCHEMA}
        # Avoid plan changes due to reuse of a preceding request's prompt cache.
        payload["cache_prompt"] = False
    data = model_json("POST", url, headers, payload)
    try:
        content = data["choices"][0]["message"]["content"].strip()
        fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.S)
        return Plan.model_validate_json(fence[1] if fence else content)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise ToolError("Model output did not match the allowed tool schema; nothing was executed") from None


def check_model():
    start = time.monotonic()
    checks = []

    def record(check, ok, detail, began):
        checks.append({"check": check, "ok": ok, "detail": detail,
                       "elapsed_ms": round((time.monotonic() - began) * 1000)})

    try:
        url, mode, name, headers = model_settings()
        if mode == "local":
            endpoint = urlsplit(url)
            suffix = "/chat/completions"
            if not endpoint.path.endswith(suffix):
                raise ToolError("Local model URL must end in /chat/completions for discovery")
            models_url = endpoint._replace(path=endpoint.path[:-len(suffix)] + "/models", query="", fragment="").geturl()
            models = model_json("GET", models_url, headers, timeout=5, limit=262144)
            if not isinstance(models, dict) or not isinstance(models.get("data"), list):
                raise ToolError("Unexpected model list; check the local /v1/models endpoint")
            if not any(isinstance(entry, dict) and entry.get("id") == name for entry in models["data"]):
                raise ToolError("SPT_MODEL_NAME is not listed; compare it with /v1/models or set llama-server --alias")
            record("models", True, "Configured model ID is listed", start)
    except ToolError as exc:
        record("connection", False, str(exc), start)
        return {"ok": False, "checks": checks}

    for text, expected in MODEL_PROBES:
        began = time.monotonic()
        label = f"{expected.action}/{expected.profile}" if expected.profile else expected.action
        try:
            plan = model_plan(text)
            actual = f"{plan.action}/{plan.profile}" if plan.profile else plan.action
            record(label, plan == expected, f"Returned {actual}", began)
        except ToolError as exc:
            record(label, False, str(exc), began)
    return {"ok": all(item["ok"] for item in checks), "checks": checks}


@dataclass
class Pending:
    action: str
    profile: str | None
    snapshot: object
    source: str
    expires: float


class Agent:
    def __init__(self, root):
        self.root = root
        self.env = ProjectEnvironment(root)
        self.proxy = ManagedNginx(root)
        self.model_slots = threading.BoundedSemaphore(2)
        self.work = threading.Lock()
        self.audit_lock = threading.Lock()
        self.pending = {}
        self.fixture_enabled = os.environ.get("SPT_ENABLE_FIXTURE") == "1"

    def result(self, task_id, status, source, action, started, details, **fields):
        elapsed_ms = round((time.monotonic() - started) * 1000)
        risk = "confirm" if action in {"environment", "deploy", "repair", "fault"} else (
            "read_only" if action in {"resources", "diagnose"} else "blocked")
        event = {"time": datetime.now(timezone.utc).isoformat(), "id": task_id, "user": getpass.getuser(),
                 "status": status, "action": action, "risk": risk, "source": source, "elapsed_ms": elapsed_ms}
        with self.audit_lock, (self.root / "audit.jsonl").open("a", encoding="utf-8") as audit:
            audit.write(json.dumps(event, ensure_ascii=False) + "\n")
        return {"task_id": task_id, "status": status, "source": source, "action": action, "risk": risk,
                "elapsed_ms": elapsed_ms, "details": details, **fields}

    def run(self, req):
        start, ident = time.monotonic(), secrets.token_hex(16)
        source = "fixture" if req.fixture else "template" if req.demo else os.environ.get("SPT_MODEL_MODE", "local")
        action, profile = "unsupported", None
        try:
            text = req.user_input.strip()
            if req.fixture:
                if not self.fixture_enabled or req.demo or text:
                    raise ToolError("Operator fault fixture is disabled or incorrectly invoked")
                action = "fault"
            elif not text:
                raise ToolError("Enter one operation")
            elif BLOCKED.search(text):
                return self.result(ident, "blocked", source, action, start, ["High-risk operation refused"])
            else:
                if req.demo:
                    plan = DEMO_PLANS.get(text)
                    if plan is None:
                        raise ToolError("Template mode only supports the exact example phrases in the guide")
                else:
                    if not self.model_slots.acquire(blocking=False):
                        raise ToolError("Planner is busy; try again")
                    try:
                        plan = model_plan(text)
                    finally:
                        self.model_slots.release()
                action, profile = plan.action, plan.profile
                if action == "unsupported":
                    return self.result(ident, "blocked", source, action, start, ["No allowlisted operation matches the request"])
                if action != "resources" and not PROJECT.search(text):
                    return self.result(ident, "blocked", source, action, start,
                                       ["Project operations must explicitly name demo-web; no changes applied"])
            if not self.work.acquire(blocking=False):
                raise ToolError("Another operation is running; try again")
            try:
                if action in {"resources", "diagnose"}:
                    details = resources(self.root) if action == "resources" else self.proxy.inspect()[0]
                    return self.result(ident, "verified", source, action, start, details)
                if action == "environment":
                    snapshot = self.env.path(profile).exists()
                    details = self.env.preview(profile)
                else:
                    snapshot = self.proxy.snapshot()
                    details = self.proxy.preview(action)
                now = time.monotonic()
                self.pending = {key: value for key, value in self.pending.items() if value.expires > now}
                if len(self.pending) >= 32:
                    raise ToolError("Too many pending plans; wait for expiry")
                result = self.result(ident, "pending", source, action, start, details, expires_in=120)
                self.pending[ident] = Pending(action, profile, snapshot, source, now + 120)
                return result
            finally:
                self.work.release()
        except (ToolError, OSError, ValueError) as exc:
            return self.result(ident, "failed", source, action, start,
                               [str(exc) if isinstance(exc, ToolError) else "Local configuration or I/O error"])

    def decide(self, req):
        start, ident = time.monotonic(), req.plan_id
        if not self.work.acquire(blocking=False):
            return self.result(ident, "failed", "approval", "unsupported", start,
                               ["Another operation is running; no change made for this request"])
        try:
            plan = self.pending.pop(ident, None)
            if plan is None or plan.expires <= time.monotonic():
                return self.result(ident, "expired", "approval", "unsupported", start, ["Plan expired or already used"])
            if not req.approve:
                return self.result(ident, "cancelled", plan.source, plan.action, start, ["No changes applied"])
            self.result(ident, "approved", plan.source, plan.action, start, [])
            try:
                details = (self.env.apply(plan.profile, plan.snapshot) if plan.action == "environment"
                           else self.proxy.apply(plan.action, plan.snapshot))
                return self.result(ident, "verified", plan.source, plan.action, start, details)
            except (ToolError, OSError, ValueError) as exc:
                return self.result(ident, "failed", plan.source, plan.action, start,
                                   [str(exc) if isinstance(exc, ToolError) else "Local configuration or I/O error"])
        finally:
            self.work.release()


@asynccontextmanager
async def lifespan(app):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise RuntimeError("Run ShellPilot as a non-root user")
    root = state_dir()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt" and (root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077):
        raise RuntimeError(f"State directory must be owned by this user and private (0700): {root}")
    lock = FileLock(str(root / "agent.lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise RuntimeError("Another ShellPilot agent uses this state directory") from None
    agent = None
    try:
        token = root / "agent.token"
        if token.is_symlink():
            raise RuntimeError("State token must not be a symlink")
        if not token.exists():
            with os.fdopen(os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="ascii") as out:
                out.write(secrets.token_hex(32))
        if os.name != "nt" and (token.stat().st_uid != os.getuid() or token.stat().st_mode & 0o077):
            raise RuntimeError("State token must be private (0600)")
        app.state.token = token.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", app.state.token):
            raise RuntimeError("Invalid state token")
        app.state.agent = agent = Agent(root)
        yield
    finally:
        try:
            if agent:
                agent.proxy.close()
        finally:
            lock.release()


app = FastAPI(title="ShellPilot Agent API", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])


@app.middleware("http")
async def local_only(request: Request, call_next):
    if not request.client or request.client.host not in {"127.0.0.1", "::1"} or request.headers.get("origin"):
        return JSONResponse({"detail": "Local CLI requests only"}, status_code=403)
    if request.url.path.startswith("/agent/"):
        auth = request.headers.get("authorization", "").encode("utf-8")
        if not secrets.compare_digest(auth, ("Bearer " + app.state.token).encode("ascii")):
            return JSONResponse({"detail": "Local agent authentication failed"}, status_code=401)
        if request.method == "POST":
            length = request.headers.get("content-length", "")
            if not re.fullmatch(r"[0-9]{1,5}", length) or int(length) > 8192:
                return JSONResponse({"detail": "Request body too large or invalid"}, status_code=413)
    return await call_next(request)


@app.post("/agent/run")
def run_agent(req: TaskRequest):
    return app.state.agent.run(req)


@app.post("/agent/decision")
def decide_agent(req: Decision):
    return app.state.agent.decide(req)


@app.get("/agent/check-model")
def check_model_endpoint():
    slots = app.state.agent.model_slots
    if not slots.acquire(blocking=False):
        return JSONResponse({"detail": "Planner is busy; try again"}, status_code=503)
    try:
        return check_model()
    finally:
        slots.release()


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "ShellPilot Agent", "model_mode": os.environ.get("SPT_MODEL_MODE", "local")}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=port_setting("SPT_AGENT_PORT", 8000), proxy_headers=False,
                workers=1, timeout_graceful_shutdown=185, access_log=False)
