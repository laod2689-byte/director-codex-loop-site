# Director-Codex Loop

本地双代理编排器：

- `Claude` 充当项目总监，负责拆解任务、定义验收标准、审查结果
- `Codex` 充当执行工程师，负责修改代码、运行实现轮次
- 控制器在每轮后执行测试命令，并把 `git diff`、测试结果、执行报告交给 Claude 做验收

## 目录

- `director_loop.py`: 主编排脚本
- `director-codex.cmd`: Windows 一键启动包装
- `prompts/`: Claude 和 Codex 的角色提示词
- `schemas/`: 结构化输出约束

## 前置条件

需要本机可调用以下命令之一：

- `claude.cmd` 或 `claude`
- `codex.cmd` 或 `codex`
- `python`

脚本查找顺序：

1. 环境变量 `CLAUDE_CLI` / `CODEX_CLI`
2. `PATH` 里的 `claude(.cmd)` / `codex(.cmd)`
3. 默认 NPM 全局路径 `%USERPROFILE%\\AppData\\Roaming\\npm\\*.cmd`

如果你的 CLI 不在默认位置，推荐显式设置：

```powershell
$env:CLAUDE_CLI="C:\path\to\claude.cmd"
$env:CODEX_CLI="C:\path\to\codex.cmd"
```

## 用法

在目标项目根目录执行：

```powershell
python C:\Users\ZhuanZ\director-codex-loop\director_loop.py "实现登录接口并补齐测试" --test "pytest -q"
```

或者：

```powershell
C:\Users\ZhuanZ\director-codex-loop\director-codex.cmd "实现登录接口并补齐测试" --test "pytest -q"
```

常用参数：

- `--repo <path>`: 指定目标仓库，默认当前目录
- `--test <cmd>`: 每轮结束后运行的验证命令
- `--max-rounds <n>`: 最大评审轮数，默认 `3`
- `--claude-model <name>`: 指定 Claude 模型
- `--codex-model <name>`: 指定 Codex 模型

## 运行流程

1. Claude 根据任务和仓库状态生成计划
2. Codex 根据计划直接修改仓库
3. 控制器运行测试命令
4. 控制器收集 `git diff`
5. Claude 基于差异和测试结果做验收
6. 若未通过，则把审查意见发给 Codex 进入下一轮

## 产物位置

每次运行会生成：

```text
<repo>\.director-codex\runs\<timestamp>\
```

其中包含：

- `director_plan.json`
- `worker_report.round_*.json`
- `test_result.round_*.json`
- `git_diff.round_*.json`
- `director_review.round_*.json`
- `result.json`

## 示例

```powershell
cd C:\path\to\your\repo
C:\Users\ZhuanZ\director-codex-loop\director-codex.cmd "修复注册接口的参数校验，并补充单元测试" --test "pytest -q" --max-rounds 4
```

## 建议

- 最好在相对干净的 `git` 工作区运行，否则 Claude 审查时会看到无关改动
- 明确传入 `--test`，不要完全依赖自动识别
- 先在小仓库或 smoke repo 试跑一轮，确认你的 `claude` 和 `codex` CLI 都能正常工作
