import argparse
import sys

import requests

from shellpilot_tools import port_setting, session, state_dir


STATUS = {"pending": "等待确认", "verified": "验证通过", "failed": "未完成", "blocked": "已拦截",
          "cancelled": "已取消", "expired": "计划已失效"}
ACTION = {"environment": "环境配置", "resources": "资源巡检", "deploy": "服务部署", "diagnose": "只读诊断",
          "repair": "故障修复", "fault": "演示故障注入", "unsupported": "不支持"}
SOURCE = {"local": "本地模型", "cloud": "云端模型", "template": "离线演示模板（未调用模型）",
          "fixture": "录制用故障注入", "approval": "确认"}
RISK = {"read_only": "只读", "confirm": "写操作，需确认", "blocked": "不执行"}


def printable(value):
    return "".join(char if char == "\n" or char.isprintable() else "?" for char in str(value))


def show(data):
    status, action, source, risk = (data[key] for key in ("status", "action", "source", "risk"))
    print(f"[{STATUS[status]}] {ACTION[action]} | {SOURCE.get(source, '配置异常')} | {RISK[risk]}")
    for index, line in enumerate(data["details"], 1):
        print(f"  {index}. {printable(line)}")
    print(f"  任务 {data['task_id'][:8]} | {data['elapsed_ms']} ms")


def ask():
    if not sys.stdin.isatty():
        print("未检测到交互终端，按拒绝处理。")
        return False
    try:
        return input("两分钟内确认是否执行？[y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def run_task(text="", demo=False, fixture=False):
    try:
        port = port_setting("SPT_AGENT_PORT", 8000)
        token = (state_dir() / "agent.token").read_text(encoding="ascii").strip()
        with session() as client:
            client.headers["Authorization"] = "Bearer " + token
            url = f"http://127.0.0.1:{port}/agent/"
            response = client.post(url + "run", json={"user_input": text, "demo": demo, "fixture": fixture},
                                   timeout=(3, 90), allow_redirects=False)
            response.raise_for_status()
            data = response.json()
            show(data)
            if data["status"] == "pending":
                approve = ask()
                response = client.post(url + "decision", json={"plan_id": data["task_id"], "approve": approve},
                                       timeout=(3, 185), allow_redirects=False)
                response.raise_for_status()
                data = response.json()
                show(data)
            return {"verified": 0, "cancelled": 3, "blocked": 4}.get(data["status"], 1)
    except (OSError, requests.RequestException, ValueError, KeyError):
        print("无法取得 Agent 结果：确认 Agent 已启动且当前终端的状态目录和端口一致。若已经确认，先查审计再重试。",
              file=sys.stderr)
        return 1


def run_model_check():
    try:
        port = port_setting("SPT_AGENT_PORT", 8000)
        token = (state_dir() / "agent.token").read_text(encoding="ascii").strip()
        print("推理自检：正在请求实际模型，不执行运维工具…", flush=True)
        url = f"http://127.0.0.1:{port}/agent/check-model"
        with session() as client:
            response = client.get(url, headers={"Authorization": "Bearer " + token},
                                  timeout=(3, 185), allow_redirects=False)
            if response.status_code == 503:
                print("Agent 规划器忙，请稍后重试。", file=sys.stderr)
                return 1
            response.raise_for_status()
        data = response.json()
        for item in data["checks"]:
            status = "PASS" if item["ok"] else "FAIL"
            print(f"[{status}] {item['check']} | {printable(item['detail'])} | {item['elapsed_ms']} ms")
        return 0 if data["ok"] else 1
    except (OSError, requests.RequestException, ValueError, KeyError):
        print("无法取得推理自检结果：检查 Agent 是否在线，以及端口和状态目录是否一致。", file=sys.stderr)
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="spt", description="在普通 Shell 中提交一句中文运维任务")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--demo", action="store_true", help="只用说明书里的离线模板，不调用模型")
    modes.add_argument("--demo-fault", action="store_true", help="只对托管演示服务注入可逆故障；仍需确认")
    modes.add_argument("--interactive", action="store_true", help="保留原有的连续交互模式")
    modes.add_argument("--check-model", action="store_true", help="实测模型规划四类任务，不执行运维工具")
    parser.add_argument("task", nargs="*", help="任务，例如 '检查当前 CPU、内存、磁盘和 GPU 使用情况'")
    args = parser.parse_args(argv)
    if args.check_model:
        if args.task:
            parser.error("--check-model does not take a task")
        return run_model_check()
    if args.interactive:
        if args.task:
            parser.error("--interactive does not take a task")
        while True:
            try:
                text = input("ShellPilot > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if text.lower() in {"exit", "quit", "退出"}:
                return 0
            if text:
                run_task(text)
    if args.demo_fault:
        if args.task:
            parser.error("--demo-fault does not take a task")
        return run_task(fixture=True)
    if not args.task:
        parser.print_help()
        return 2
    return run_task(" ".join(args.task), demo=args.demo)


if __name__ == "__main__":
    sys.exit(main())
