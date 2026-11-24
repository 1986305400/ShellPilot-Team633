# ShellPilot 智枢

UPC Team 633 · 在香橙派 6 Plus 上运行的端侧终端运维助手。

保留普通 Bash 提示符，用 `spt "中文任务"` 完成一条 `demo-web` 上线流程：**资源巡检、创建独立环境、Nginx 发布、故障诊断与修复**。本地模型只输出结构化意图，不生成或执行任意 Shell 命令；Agent 调用固定的工具，只读操作直接运行，写操作先列计划、确认后执行，再验证结果并写入审计日志。

```text
CLI (spt) -> Agent (8000) -> llama-server (8081)
                   |
                   +-> fixed tools: resources, venv, managed Nginx

curl -> Nginx (18080) -> demo-web (18081)
```

## 1. 环境依赖

在香橙派 6 Plus 的 **Debian 12 / ARM64** 板端以普通用户执行。部署以 CPU 为基线，不要求 GPU、CUDA、Conda 或云端密钥。首次部署需要访问 apt 软件源、PyPI、GitHub 和 Hugging Face；GGUF 约 0.99 GB，构建另需磁盘空间。建议预留至少 6 GB 空闲磁盘。

```bash
sudo apt update
sudo apt install -y git build-essential cmake libssl-dev python3 python3-venv nginx curl ca-certificates
```

- Python 3.11 或更高版本；Python 依赖的版本见 `requirements-shellpilot.lock`。
- Nginx 需支持 `-e stderr`（推荐 1.22 或更高版本）。安装软件包可能启动发行版默认 Nginx，但 ShellPilot 不使用它，不改 `/etc/nginx`。
- 以下均使用 `127.0.0.1`：模型 `8081`、Agent `8000`、演示入口 `18080`、后端 `18081`。`18082` 保持空闲，仅用于模拟错误的上游地址。

## 2. 一键部署

从本仓库下载或克隆代码，在含 `deploy.sh` 的**仓库根目录**执行：

```bash
bash deploy.sh
```

脚本会建立 `.spt-venv`、安装锁定的依赖、拉取并编译固定版本的 llama.cpp、下载并校验模型文件、启动模型和 Agent。首次编译、下载可能较久，后续复用本地构建产物和模型。看到 **`ShellPilot ready`** 后保持该窗口打开。结束时先在后端窗口按 `Ctrl+C`，再在部署窗口按 `Ctrl+C`，后者会停止脚本启动的 Agent 和模型；Agent 正常退出时也会清理自己启动的 Nginx。

固定版本：[llama.cpp b12007](https://github.com/ggml-org/llama.cpp/releases/tag/b12007)（`fb5997274ec0d0b4d8fe067eb55f87c0ad405478`）和 [Qwen2-1.5B-Instruct GGUF](https://huggingface.co/Qwen/Qwen2-1.5B-Instruct-GGUF/blob/main/qwen2-1_5b-instruct-q4_k_m.gguf)（`qwen2-1_5b-instruct-q4_k_m.gguf`）。本仓库不打包上游构建产物或模型权重，第三方组件按各自许可使用。

**已有板端模型和推理程序时**，先正常停止原来的模型、Agent 和演示后端，让出上面的端口。可在新仓库目录直接复用原文件，跳过重新编译和下载：

```bash
SPT_LLAMA_SERVER="$HOME/llama.cpp/build/bin/llama-server" \
SPT_MODEL_FILE="$HOME/llama.cpp/models/qwen2-1_5b-instruct-q4_k_m.gguf" \
bash deploy.sh
```

不需要这个提交包替换板上已经跑通的录屏版本。

## 3. 评委验收：一次完整的服务上线

另开一个 **操作终端**，进入同一仓库根目录，注册当前 Bash 窗口的短命令，然后先做模型自检：

```bash
if ! type spt >/dev/null 2>&1; then alias spt="$(printf '%q' "$PWD/spt")"; fi
type spt
spt --check-model
```

确认 `type spt` 指向本仓库；有其他同名入口时，不覆盖它，把下文的 `spt` 换成 `./spt`。模型自检预期 **5 项 `PASS`**：模型列表及四类意图识别，不执行运维工具。正式验收不加 `--demo`；该选项走离线模板，不调用模型。下面出现 `[y/N]` 时，核对计划后输入 `y` 并回车。

### 1. 资源巡检

```bash
spt "检查当前 CPU、内存、磁盘和 GPU 使用情况"
```

预期显示 `[验证通过]`，有实际 CPU、内存、磁盘使用情况。如没有 `nvidia-smi`，只标记 NVIDIA GPU 利用率未上报，不表示板子没有 GPU。

### 2. 创建独立运行环境

```bash
spt "为 demo-web 配置 Python 虚拟环境"
```

预期看到 `[验证通过]` 和 `Verified prefix`。**另开后端终端**，进入同一仓库目录，用刚创建的解释器手动启动服务，并保持窗口打开：

```bash
state="${SPT_STATE_DIR:-$HOME/.local/state/shellpilot}"
"$state/projects/demo-web/.venv/bin/python" shellpilot_demo_web.py
```

回到操作终端验收，预期 `HTTP 200` 和 `shellpilot-demo-web: ok`：

```bash
curl -i http://127.0.0.1:18081/health
```

### 3. 通过 Nginx 发布

```bash
spt "用 Nginx 发布 demo-web 服务"
curl -i http://127.0.0.1:18080/health
```

预期同样 `HTTP 200` 和 `shellpilot-demo-web: ok`，这次是从入口 `18080` 经 Nginx 转发到后端 `18081`。这是本机演示，不对公网开放。

### 4. 模拟故障、诊断修复并复验

```bash
spt --demo-fault
curl -sS -o /dev/null -w '后端 18081：HTTP %{http_code}\n' http://127.0.0.1:18081/health
curl -sS -o /dev/null -w '入口 18080：HTTP %{http_code}\n' http://127.0.0.1:18080/health
```

预期后端仍 **200**，入口变为 **502**。演示开关经批准后只把托管 Nginx 的上游临时改成未监听的 `18082`，不关闭后端。

```bash
spt "诊断并修复 demo-web 的 Nginx 502 故障"
curl -i http://127.0.0.1:18080/health
```

预期指出后端健康、上游端口错误，确认后重载托管 Nginx，最终入口 **200** 且响应正文匹配。地址是 `/health`，不是 `/healthy`。

## 4. 排查和复验

| 现象 | 检查 |
| --- | --- |
| 部署提示端口已占用 | 从旧实例的终端正常停止它，不要盲目终止其他服务。 |
| 未出现 `ShellPilot ready` | 查看 `.spt-runtime/model.log` 和 `.spt-runtime/agent.log`（对应进程启动后生成）。 |
| 模型下不来 | 从官方模型页取得同名 GGUF，用 `SPT_MODEL_FILE=/绝对路径/文件.gguf bash deploy.sh` 指定本地文件；仍会校验内容。 |
| 环境步骤提示已存在 | 默认状态目录是 `~/.local/state/shellpilot`，已有托管环境会被复验，不覆盖。要从空状态重录，在三个终端都先设置相同的 `SPT_STATE_DIR`，例如 `export SPT_STATE_DIR="$PWD/.spt-state"`，且应在仓库根目录执行。 |
| 查执行结果 | 状态目录中的 `audit.jsonl` 记录任务动作和结果。不要分享同目录下的 `agent.token`。 |

运行单元测试（不需要加载 GGUF；设置实际 Nginx 路径会运行真实代理用例）：

```bash
.spt-venv/bin/python -m pip install httpx==0.28.1
SPT_NGINX_EXE=/usr/sbin/nginx .spt-venv/bin/python -m unittest -v test_shellpilot
```

## 5. 代码结构

| 文件 | 用途 |
| --- | --- |
| `spt`, `shellpilot_cli.py` | Bash 入口、计划展示与交互确认 |
| `agent_server.py` | 本地 API、推理对接、计划校验、确认和审计 |
| `shellpilot_tools.py` | 资源采样、环境验证、托管 Nginx 与复验 |
| `shellpilot_demo_web.py` | 提供 `/health` 的 Python 演示后端 |
| `deploy.sh`, `requirements-shellpilot.lock` | 可复现部署入口、Python 依赖版本 |
| `test_shellpilot.py` | 自检、工具与代理回归测试 |

本项目不是通用 Shell 执行器或多租户沙箱，不管理系统 Nginx，也不声称提供板载 GPU/NPU 推理加速。本仓库仅发布 ShellPilot 代码，不修改 llama.cpp 的 C++ 推理代码。
