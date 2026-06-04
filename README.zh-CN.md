<p align="center">
  <img src="assets/thoth-banner.png" alt="Thoth" width="100%">
</p>

<h1 align="center">Thoth</h1>

<p align="center"><i>真正能记住事情的自进化 AI 代理。</i></p>

<p align="center">
  <a href="https://github.com/519lab/thoth-agent/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/PostgreSQL-17-336791?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL 17">
  <img src="https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11">
  <a href="README.md"><img src="https://img.shields.io/badge/Lang-English-lightgrey?style=for-the-badge" alt="English"></a>
</p>

**Thoth 是一个会学习的 AI 代理。** 它以神祇的书记官托特（Thoth）——知识、记录与裁断的守护者——命名，而它所做的也正如其名：记录所发生的一切，将其提炼为持久的记忆，并在使用中不断变得更好。它从经验中创建技能，在工作中改进技能，搜索自己过往的对话，并跨会话构建对你日益深入的理解——这一切都建立在一个由 PostgreSQL 支撑的认知基底（substrate）之上，这个基底是它所知一切的唯一真实来源。

可以在 $5 的 VPS 上运行，也可以在 GPU 集群上运行，或者使用空闲时几乎零成本的 Serverless 基础设施。它不绑定你的笔记本——你可以在 Telegram 上与它对话，而它在云端 VM 上工作。

支持任意模型——[OpenRouter](https://openrouter.ai)（200+ 模型）、[NovitaAI](https://novita.ai)、[NVIDIA NIM](https://build.nvidia.com)（Nemotron）、[小米 MiMo](https://platform.xiaomimimo.com)、[z.ai/GLM](https://z.ai)、[Kimi/Moonshot](https://platform.moonshot.ai)、[MiniMax](https://www.minimax.io)、[Hugging Face](https://huggingface.co)、[Nous Portal](https://portal.nousresearch.com)、OpenAI，或你自己的端点。使用 `thoth model` 即可切换——无需改代码，无锁定。

<table>
<tr><td><b>真正的终端界面</b></td><td>完整的 TUI，支持多行编辑、斜杠命令自动补全、对话历史、中断重定向和流式工具输出。</td></tr>
<tr><td><b>随你所在</b></td><td>Telegram、Discord、Slack、WhatsApp、Signal 和 CLI——全部从单个网关进程运行。语音备忘录转写、跨平台对话连续性。</td></tr>
<tr><td><b>闭环学习</b></td><td>代理管理记忆并定期自我提醒。复杂任务后自动创建技能。技能在使用中自我改进。全文会话搜索配合 LLM 摘要实现跨会话回溯。<a href="https://github.com/plastic-labs/honcho">Honcho</a> 辩证式用户建模。兼容 <a href="https://agentskills.io">agentskills.io</a> 开放标准。</td></tr>
<tr><td><b>认知基底</b></td><td>每一条消息、每一个动作、每一个事件都作为一个切片（slice）记录到 PostgreSQL 中，随时间衰减并被持续管理，并通过语义 + 关键词 + 显著性 + 时近性评分召回。这是一种会沉淀整合、而非一味堆积的记忆。</td></tr>
<tr><td><b>定时自动化</b></td><td>内置 cron 调度器，支持向任何平台投递。日报、夜间备份、周审计——全部用自然语言描述，无人值守运行。</td></tr>
<tr><td><b>委派与并行</b></td><td>生成隔离子代理处理并行工作流。编写 Python 脚本通过 RPC 调用工具，将多步管道压缩为零上下文开销的轮次。</td></tr>
<tr><td><b>随处运行，而不只在你的笔记本上</b></td><td>七种终端后端——本地、Docker、SSH、Singularity、Modal、Daytona 和 Vercel Sandbox。Daytona 和 Modal 提供 Serverless 持久化——代理环境空闲时休眠、按需唤醒，会话之间几乎零成本。</td></tr>
<tr><td><b>研究就绪</b></td><td>批量轨迹生成、轨迹压缩——用于训练下一代工具调用模型。</td></tr>
</table>

> **关于名字的说明：** 项目名为 **Thoth**，命令行工具现已改为 `thoth`——本文档中的每个示例都使用它。旧的 `hermes` 命令仍作为别名可用，安装时配置也仍保留在 `~/.hermes` 下（数据库同样仍命名为 `hermes`），因此升级时不会有任何破坏。剩余的可执行文件 / 命名空间重命名正在进行中。

---

## 快速安装

### Linux、macOS、WSL2、Termux

```bash
curl -fsSL https://raw.githubusercontent.com/519lab/thoth-agent/main/scripts/install.sh | bash
```

安装程序会为你准备好所需的一切，包括一个 `docker compose` 的 PostgreSQL 17 服务（端口 5432，数据库 `hermes`），并运行 schema 迁移。如果你希望从干净的数据库开始，可传入 `--reset-db`；若要指向你自己的 PostgreSQL，可传入 `--skip-postgres`。

### Windows（原生，PowerShell）— 早期 Beta

> **请注意：** 原生 Windows 支持目前处于**早期 Beta** 阶段。它能安装并运行，但尚未像 Linux/macOS/WSL2 路径那样经过广泛实战检验。遇到问题请[提交 issue](https://github.com/519lab/thoth-agent/issues)。如果想要当前最稳妥的 Windows 体验，请在 **WSL2** 中运行上面的 Linux/macOS 一行命令。

```powershell
iex (irm https://raw.githubusercontent.com/519lab/thoth-agent/main/scripts/install.ps1)
```

安装程序会处理一切：uv、Python 3.11、Node.js、ripgrep、ffmpeg，**以及一个便携版 Git Bash**（MinGit，解压到 `%LOCALAPPDATA%\hermes\git`——无需管理员权限，与任何系统 Git 安装完全隔离），用于运行 shell 命令。如果你已经安装了 Git，它会检测并改用你的安装。

> **Android / Termux：** Termux 会安装精选的 `.[termux]` 扩展，因为完整的 `.[all]` 扩展目前会拉取 Android 不兼容的语音依赖。
>
> **Windows 路径：** 原生 Windows 安装在 `%LOCALAPPDATA%\hermes` 下；WSL2 则与 Linux 一样安装在 `~/.hermes` 下。目前唯一需要专门用 WSL2 的功能是基于浏览器的仪表盘聊天面板（它使用 POSIX PTY——经典 CLI 和网关都可原生运行）。

安装后：

```bash
source ~/.bashrc    # 重新加载 shell（或: source ~/.zshrc）
thoth              # 开始对话！
```

---

## 数据库设置

Thoth 使用 **PostgreSQL 17**（启用 `vector` 和 `pg_trgm` 扩展）作为会话记录、看板状态和基底感知切片的唯一真实来源——任何地方都没有 SQLite。安装程序会自动启动一个 `docker compose` 的 PostgreSQL 服务；手动 / 生产环境路径如下。

**本地开发：**

```bash
docker compose up -d postgres
export THOTH_PG_DSN=postgresql://hermes:hermes@localhost:5432/hermes   # 旧的 HERMES_PG_DSN 仍通过环境变量桥接生效
uv run alembic -c migrations/alembic.ini upgrade head
```

**生产部署：** 将 `THOTH_PG_DSN`（旧的 `HERMES_PG_DSN` 仍通过环境变量桥接生效）指向任意安装了 `vector` 和 `pg_trgm` 扩展的 PostgreSQL 17+ 实例，并将 `alembic upgrade head` 作为部署流程的一部分运行。

**从旧的基于 SQLite 的安装迁移？** 我们提供了一个一次性导入器：

```bash
uv run thoth db migrate-from-sqlite --sqlite-path ~/.hermes/state.db   # 加 --dry-run 可预览
```

---

## 认知基底

基底（substrate）是让 Thoth 不止是一个无状态聊天循环的关键所在。它是一个由 PostgreSQL 支撑的**感知汇聚点与召回来源**：每一条用户消息、助手响应、工具调用 / 结果、子代理生成 / 返回、会话生命周期事件和 cron 派发，都会作为一个*切片（slice）*发射到一个命名的*流（stream）*上（如 `thoth.world.user_message.cli`、`thoth.self_action.assistant_response` 等）。切片存储在 `substrate_slices` 中（按摄入时间做月度 RANGE 分区），被持续管理，并按需召回。

它通过后台 worker 与代理并行运行，并被设计为安全的：基底故障是非致命的，而召回路径默认通过环境变量关闭。schema 迁移是永久性的，所以如果你在意这些数据，请在首次运行前备份数据库。

**感知汇聚。** 三张核心表（`substrate_streams`、`substrate_slices`、`substrate_decay_profiles`）、自动注册的流，以及月度分区切分。后台 worker 在启动时开始运转：**Sentinel**（切片分诊）、**force-reject**（丢弃超过其衰减配置 TTL 的待处理切片），以及 **partition-maintenance**（在 `now()` 之前维持一个滚动的月度分区窗口）。

**Curator（管理器）。** 一个持续的衰减 + 释放循环。切片按其衰减配置的半衰期逐渐淡化；当低于该配置的 `min_salience_to_retain` 阈值时，它们按墓碑策略（`thin` / `full` / `none`）释放。每一个决策本身都会作为一个 self-state 切片被记录下来，因此系统能够随时间推移对自身的记忆进行推理。

**召回 + 嵌入。** 一个只追加的 `substrate_recall_log` 审计每一次 `recall()` 调用，切片带有一个 pgvector 的 `embedding` 列。Curator 会异步回填语义嵌入；对尚未嵌入的切片进行召回时会回退到关键词 Jaccard。当通过 `HERMES_SUBSTRATE_RECALL=1` 启用时，每一轮的 `<memory-context>` 都会从基底切片中按复合评分（向量相似度 + 关键词 Jaccard + 显著性 + 时近性，在 token 预算内）组装，并且模型会获得一个 `substrate_recall_more` 工具用于显式的深入搜索。

### 查看基底状态

```bash
thoth substrate            # 默认摘要（流、切片数、待处理）
thoth substrate streams    # 每个流的切片数
thoth substrate slices --stream thoth.world.user_message.cli --limit 20
thoth substrate pending    # 当前待处理队列深度 + 最旧切片的年龄
thoth substrate profiles   # 已植入的衰减配置
thoth substrate curator    # Curator 衰减 / 释放活动
thoth substrate recall     # 召回覆盖率 + 最近的调用
```

如果 Thoth 启动时你的数据库处于较旧的 Alembic 修订版本，启动会抛出一个 `RuntimeError`，其中包含需要运行的升级命令；设置 `HERMES_AUTO_MIGRATE=1` 可在首次启动时自动升级。基底的操作员手册以内置技能形式随附——用 `/substrate` 加载。

---

## 快速入门

```bash
thoth              # 交互式 CLI — 开始对话
thoth model        # 选择 LLM 提供商和模型
thoth tools        # 配置启用的工具
thoth config set   # 设置单个配置项
thoth gateway      # 启动消息网关（Telegram、Discord 等）
thoth setup        # 运行完整设置向导（一次性配置所有内容）
thoth update       # 更新到最新版本
thoth doctor       # 诊断问题
```

### CLI 与消息平台 快速对照

Thoth 有两种入口：用 `thoth` 启动终端 UI，或运行网关从 Telegram、Discord、Slack、WhatsApp、Signal 或 Email 与之对话。进入对话后，许多斜杠命令在两种界面中通用。

| 操作 | CLI | 消息平台 |
|------|-----|----------|
| 开始对话 | `thoth` | 运行 `thoth gateway setup` + `thoth gateway start`，然后给机器人发消息 |
| 开始新对话 | `/new` 或 `/reset` | `/new` 或 `/reset` |
| 更换模型 | `/model [provider:model]` | `/model [provider:model]` |
| 设置人格 | `/personality [name]` | `/personality [name]` |
| 重试或撤销上一轮 | `/retry`、`/undo` | `/retry`、`/undo` |
| 压缩上下文 / 查看用量 | `/compress`、`/usage`、`/insights [--days N]` | `/compress`、`/usage`、`/insights [days]` |
| 浏览技能 | `/skills` 或 `/<skill-name>` | `/<skill-name>` |
| 中断当前工作 | `Ctrl+C` 或发送新消息 | `/stop` 或发送新消息 |
| 平台特定状态 | `/platforms` | `/status`、`/sethome` |

运行 `thoth --help`（或 `thoth <command> --help`）查看完整命令列表。

---

## 开发测试

测试套件使用 `pytest-postgresql` 对一个**独立的** PostgreSQL 容器运行，因此你在 5432 端口上的真实数据库永远不会被触及。

**一次性设置：**

```bash
docker compose --profile test up -d postgres-test    # 在 5433 端口上的专用 PG，独立卷
```

**本地运行测试：**

```bash
# 按文件隔离的运行器（与 CI 一致）。通过
# tests/conftest.py:_TEST_PG_PORT（默认 5433）获取测试数据库。
PYTEST_XDIST_WORKER=run_local uv run python scripts/run_tests_parallel.py tests/substrate/

# 或单个文件：
PYTEST_XDIST_WORKER=run_local uv run python -m pytest tests/substrate/test_commit.py \
    -o "addopts=" --timeout-method=thread --timeout=120
```

直接运行 pytest 时必须设置 `PYTEST_XDIST_WORKER`（并行运行器会为每个子进程设置它）。它的值只是一个唯一标签——`pytest-postgresql` 用它来派生每个 worker 的数据库名，从而让并发的子进程不会在共享的模板数据库上竞争。要指向不同的测试 PG，请在运行 pytest 前设置 `HERMES_TEST_POSTGRES_PORT`（或 `POSTGRES_PORT`）。

**在 Linux 容器中运行测试（与 CI 完全一致）：**

`test-runner` docker-compose 服务会在 Debian + Python 3.11 + `[all,dev]` 扩展中运行整个套件——与 CI 镜像一致——因此失败可在本地复现，且 Windows 主机噪声（POSIX 权限、`/mnt/c` 路径、波浪号展开）也会消失。

```bash
docker compose --profile test up -d postgres-test
docker compose --profile test build test-runner       # 首次，约 3 分钟
scripts/run_tests_docker.sh                            # 完整套件
scripts/run_tests_docker.sh tests/substrate/test_commit.py
scripts/run_tests_docker.sh tests/substrate/ -- -v -k 'reinforce'
```

源码以 bind-mount 方式挂载，因此测试的编辑不会触发镜像重建；venv 位于镜像内的 `/opt/venv`，以便在 bind mount 下保留。

---

## 从 OpenClaw 迁移

如果你来自 OpenClaw，Thoth 可以自动导入你的设置、记忆、技能和 API 密钥。安装向导（`thoth setup`）会检测 `~/.openclaw` 并在配置开始前提供迁移选项。安装后任意时间：

```bash
thoth claw migrate              # 交互式迁移（完整预设）
thoth claw migrate --dry-run    # 预览将要迁移的内容
thoth claw migrate --preset user-data   # 不含密钥地迁移
thoth claw migrate --overwrite  # 覆盖已有冲突
```

导入内容包括：人格文件（**SOUL.md**）、记忆（MEMORY.md / USER.md）、用户创建的技能、命令白名单、消息设置、白名单中的 API 密钥、TTS 资产，以及工作区指令（AGENTS.md）。所有选项请参阅 `thoth claw migrate --help`。

---

## 贡献

欢迎贡献。克隆即可开始：

```bash
git clone https://github.com/519lab/thoth-agent.git
cd thoth-agent
./setup-thoth.sh     # 安装 uv、创建 venv、安装 .[all]、创建符号链接 ~/.local/bin/thoth
./thoth              # 自动检测 venv，无需先 source
```

手动安装（等效于上述命令）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[all,dev]"
scripts/run_tests.sh
```

开发设置和 PR 流程请参阅 [`CONTRIBUTING.md`](CONTRIBUTING.md)，架构以及代理（与人类）应遵循的约定请参阅 [`AGENTS.md`](AGENTS.md)。

---

## 社区

- 📚 [技能中心](https://agentskills.io) — `agentskills.io` 开放技能标准
- 🐛 [问题反馈](https://github.com/519lab/thoth-agent/issues)
- 🔌 [computer-use-linux](https://github.com/avifenesh/computer-use-linux) — Linux 桌面控制 MCP 服务器，提供 AT-SPI 无障碍树、Wayland/X11 输入、截图，以及合成器窗口定位

---

## 致谢

Thoth 站在 **[Hermes](https://github.com/NousResearch/hermes-agent)** 的肩膀上——这是由 **[Nous Research](https://nousresearch.com)** 创建的开源代理。终端界面、消息网关、技能系统、工具框架、七种终端后端——那个根基是他们的工作成果，以宽松的许可证慷慨地发布出来，让其他人得以在其之上构建。Thoth 之所以存在，正是因为他们选择了开放地构建。

致 Nous Research 团队，以及在 Hermes 历次发布中塑造它的每一位贡献者：**谢谢你们。** 本项目带着深深的感激与敬意，将你们的工作向前延续。这次更名反映的是一个不同的方向，以及我们自己的记忆优先架构——它不是与你们所构建之物的分道扬镳，而是对它的延续。

---

## 许可证

基于 **MIT 许可证**发布——详见 [LICENSE](LICENSE)。

原始的 Hermes 版权（© Nous Research）保留在许可证文件中，这既是 MIT 的要求，也是我们乐于去做的。本项目中的新工作在同样的 MIT 条款下贡献。
