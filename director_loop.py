from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = SCRIPT_DIR / "prompts"
SCHEMAS_DIR = SCRIPT_DIR / "schemas"


class OrchestratorError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Claude as director and Codex as worker in a review loop.",
    )
    parser.add_argument("task", help="Natural-language task for the director.")
    parser.add_argument(
        "--repo",
        default=".",
        help="Repository path. Defaults to the current working directory.",
    )
    parser.add_argument(
        "--test",
        dest="test_command",
        default=None,
        help="Verification command to run after each Codex round.",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=3,
        help="Maximum number of director/worker rounds.",
    )
    parser.add_argument(
        "--claude-model",
        default=None,
        help="Optional Claude model override.",
    )
    parser.add_argument(
        "--codex-model",
        default=None,
        help="Optional Codex model override.",
    )
    return parser.parse_args()


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def truncate_text(content: str, limit: int = 12000) -> str:
    if len(content) <= limit:
        return content
    head = content[: limit - 120]
    tail = content[-80:]
    return f"{head}\n\n...[truncated {len(content) - len(head) - len(tail)} chars]...\n\n{tail}"


def run_subprocess(
    args: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: int = 1200,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            check=False,
        )
    except FileNotFoundError as exc:
        raise OrchestratorError(f"Command not found: {args[0]}") from exc


def maybe_run_shell(command: str | None, cwd: Path) -> dict[str, Any]:
    if not command:
        return {
            "command": None,
            "returncode": None,
            "status": "skipped",
            "stdout": "",
            "stderr": "",
        }

    completed = subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
        shell=True,
        check=False,
    )
    status = "passed" if completed.returncode == 0 else "failed"
    return {
        "command": command,
        "returncode": completed.returncode,
        "status": status,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def detect_test_command(repo: Path) -> str | None:
    if (repo / "package.json").exists():
        if (repo / "pnpm-lock.yaml").exists():
            return "pnpm test"
        if (repo / "yarn.lock").exists():
            return "yarn test"
        return "npm test"
    if (repo / "pytest.ini").exists() or (repo / "conftest.py").exists():
        return "pytest -q"
    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        if "pytest" in text:
            return "pytest -q"
    if (repo / "Cargo.toml").exists():
        return "cargo test"
    if (repo / "go.mod").exists():
        return "go test ./..."
    if (repo / "pom.xml").exists():
        return "mvn test"
    if (repo / "build.gradle").exists() or (repo / "build.gradle.kts").exists():
        return "gradle test"
    return None


def home_cli(name: str) -> Path:
    override = os.environ.get(f"{name.upper()}_CLI")
    if override:
        return Path(override).expanduser().resolve()
    discovered = shutil.which(f"{name}.cmd") or shutil.which(name)
    if discovered:
        return Path(discovered).resolve()
    return (Path.home() / "AppData" / "Roaming" / "npm" / f"{name}.cmd").resolve()


def ensure_cli(path: Path) -> Path:
    if not path.exists():
        raise OrchestratorError(f"CLI not found: {path}")
    return path


def is_git_repo(repo: Path) -> bool:
    completed = run_subprocess(
        [
            "git",
            "-c",
            f"safe.directory={repo}",
            "-C",
            str(repo),
            "rev-parse",
            "--is-inside-work-tree",
        ]
    )
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def git_output(repo: Path, *args: str) -> str:
    completed = run_subprocess(
        [
            "git",
            "-c",
            f"safe.directory={repo}",
            "-C",
            str(repo),
            *args,
        ],
        timeout=120,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout


def collect_repo_state(repo: Path) -> dict[str, Any]:
    inside_git = is_git_repo(repo)
    state: dict[str, Any] = {
        "repo_path": str(repo),
        "is_git_repo": inside_git,
        "cwd": str(Path.cwd()),
    }
    if inside_git:
        state["branch"] = git_output(repo, "branch", "--show-current").strip()
        state["status_short"] = git_output(repo, "status", "--short")
        state["dirty"] = bool(state["status_short"].strip())
    else:
        state["branch"] = ""
        state["status_short"] = ""
        state["dirty"] = False
    return state


def collect_diff(repo: Path) -> dict[str, Any]:
    if not is_git_repo(repo):
        return {
            "changed_files": [],
            "diff_stat": "",
            "diff_patch": "",
        }
    changed_files = [
        line.strip()
        for line in git_output(repo, "diff", "--name-only").splitlines()
        if line.strip()
    ]
    return {
        "changed_files": changed_files,
        "diff_stat": git_output(repo, "diff", "--stat"),
        "diff_patch": truncate_text(
            git_output(repo, "diff", "--unified=1", "--no-ext-diff"),
            limit=6000,
        ),
    }


def print_banner(message: str) -> None:
    print(f"[director-codex] {message}", flush=True)


def claude_structured(
    *,
    claude_cli: Path,
    repo: Path,
    run_dir: Path,
    context_file: Path,
    schema_file: Path,
    system_prompt: str,
    user_prompt: str,
    model: str | None,
    output_file: Path,
) -> dict[str, Any]:
    schema = load_json(schema_file)
    context_payload = load_json(context_file)
    context_json = json.dumps(context_payload, ensure_ascii=False, separators=(",", ":"))
    compact_system_prompt = " ".join(system_prompt.split())
    prompt_text = user_prompt.format(
        context_path=str(context_file),
        context_json=context_json,
    )
    if compact_system_prompt:
        prompt_text = f"{compact_system_prompt} {prompt_text}"
    args = [
        str(claude_cli),
        "-p",
        "--tools",
        "",
        "--input-format",
        "text",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, ensure_ascii=False),
    ]
    if model:
        args.extend(["--model", model])

    completed = run_subprocess(args, cwd=repo, input_text=prompt_text, timeout=1800)
    write_text(output_file.with_suffix(".stdout.log"), completed.stdout)
    write_text(output_file.with_suffix(".stderr.log"), completed.stderr)
    if completed.returncode != 0:
        raise OrchestratorError(
            f"Claude failed with exit code {completed.returncode}. See {output_file.with_suffix('.stderr.log')}"
        )

    envelope = json.loads(completed.stdout)
    payload = envelope.get("structured_output", envelope)
    write_json(output_file, payload)
    return payload


def codex_structured(
    *,
    codex_cli: Path,
    repo: Path,
    schema_file: Path,
    prompt: str,
    model: str | None,
    output_file: Path,
) -> dict[str, Any]:
    args = [
        str(codex_cli),
        "-a",
        "never",
        "exec",
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        "--color",
        "never",
        "-C",
        str(repo),
        "--output-schema",
        str(schema_file),
        "-o",
        str(output_file),
        "-",
    ]
    if model:
        args[3:3] = ["-m", model]

    completed = run_subprocess(args, cwd=repo, input_text=prompt, timeout=3600)
    write_text(output_file.with_suffix(".stdout.log"), completed.stdout)
    write_text(output_file.with_suffix(".stderr.log"), completed.stderr)
    if completed.returncode != 0:
        raise OrchestratorError(
            f"Codex failed with exit code {completed.returncode}. See {output_file.with_suffix('.stderr.log')}"
        )

    return load_json(output_file)


def build_worker_prompt(
    *,
    repo: Path,
    task: str,
    director_plan: dict[str, Any],
    worker_role: str,
    test_command: str | None,
    round_index: int,
    max_rounds: int,
    review_feedback: dict[str, Any] | None,
    repo_state: dict[str, Any],
) -> str:
    feedback_section = json.dumps(review_feedback, ensure_ascii=False, indent=2) if review_feedback else "null"
    return textwrap.dedent(
        f"""
        {worker_role.strip()}

        Repository: {repo}
        Round: {round_index}/{max_rounds}
        User request:
        {task}

        Director plan JSON:
        {json.dumps(director_plan, ensure_ascii=False, indent=2)}

        Current repo state:
        {json.dumps(repo_state, ensure_ascii=False, indent=2)}

        Director review feedback from previous round:
        {feedback_section}

        External verification command that will be run after you finish:
        {test_command or "No external test command configured."}

        Work directly in the repository and implement the task now.
        Address every acceptance criterion and every director issue.
        If the repository already has unrelated dirty files, leave them alone.
        Your final response must be JSON only, matching the provided schema.
        """
    ).strip()


def build_plan_context(
    *,
    repo: Path,
    task: str,
    repo_state: dict[str, Any],
    test_command: str | None,
    max_rounds: int,
) -> dict[str, Any]:
    return {
        "task": task,
        "repo_path": str(repo),
        "repo_state": repo_state,
        "test_command": test_command,
        "max_rounds": max_rounds,
        "note": "Best results come from a clean git working tree. If the repo is dirty, make assumptions explicit and keep the change set minimal.",
    }


def build_review_context(
    *,
    task: str,
    repo: Path,
    repo_state: dict[str, Any],
    director_plan: dict[str, Any],
    worker_report: dict[str, Any],
    test_result: dict[str, Any],
    diff_payload: dict[str, Any],
    round_index: int,
    max_rounds: int,
) -> dict[str, Any]:
    return {
        "task": task,
        "repo_path": str(repo),
        "repo_state": repo_state,
        "director_plan": director_plan,
        "worker_report": worker_report,
        "test_result": {
            "command": test_result.get("command"),
            "status": test_result.get("status"),
            "returncode": test_result.get("returncode"),
            "stdout": truncate_text(test_result.get("stdout", ""), limit=4000),
            "stderr": truncate_text(test_result.get("stderr", ""), limit=4000),
        },
        "git_diff": diff_payload,
        "round": round_index,
        "max_rounds": max_rounds,
        "review_rule": "Approve only if the user request is satisfied, the acceptance criteria are met, and the available evidence supports it. If tests failed, reject unless the task explicitly says tests may remain failing.",
    }


def summarize_review(review: dict[str, Any]) -> str:
    lines = [f"Decision: {review['decision']}", f"Summary: {review['verdict_summary']}"]
    for issue in review.get("issues", []):
        lines.append(f"- [{issue['severity']}] {issue['title']}: {issue['action']}")
    return "\n".join(lines)


def run_loop(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    if not repo.exists():
        raise OrchestratorError(f"Repository path does not exist: {repo}")

    claude_cli = ensure_cli(home_cli("claude"))
    codex_cli = ensure_cli(home_cli("codex"))

    test_command = args.test_command or detect_test_command(repo)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = repo / ".director-codex" / "runs" / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    repo_state = collect_repo_state(repo)
    write_json(run_dir / "repo_state.initial.json", repo_state)

    if repo_state.get("dirty"):
        print_banner("warning: repository is already dirty; git diff review may include unrelated changes")
    if test_command:
        print_banner(f"using test command: {test_command}")
    else:
        print_banner("no test command configured; review will rely on diff and worker report")

    plan_context_path = run_dir / "director_plan.context.json"
    write_json(
        plan_context_path,
        build_plan_context(
            repo=repo,
            task=args.task,
            repo_state=repo_state,
            test_command=test_command,
            max_rounds=args.max_rounds,
        ),
    )

    print_banner("director is generating the task plan")
    director_plan = claude_structured(
        claude_cli=claude_cli,
        repo=repo,
        run_dir=run_dir,
        context_file=plan_context_path,
        schema_file=SCHEMAS_DIR / "director_plan.schema.json",
        system_prompt=load_text(PROMPTS_DIR / "director_system.md"),
        user_prompt=(
            "Use this JSON context: {context_json}. "
            "Produce a concrete implementation plan for Codex. "
            "Return only structured output that matches the schema."
        ),
        model=args.claude_model,
        output_file=run_dir / "director_plan.json",
    )
    print_banner(f"director plan ready: {director_plan['task_summary']}")

    worker_role = load_text(PROMPTS_DIR / "worker_role.md")
    review_feedback: dict[str, Any] | None = None

    for round_index in range(1, args.max_rounds + 1):
        print_banner(f"starting codex round {round_index}/{args.max_rounds}")
        worker_prompt = build_worker_prompt(
            repo=repo,
            task=args.task,
            director_plan=director_plan,
            worker_role=worker_role,
            test_command=test_command,
            round_index=round_index,
            max_rounds=args.max_rounds,
            review_feedback=review_feedback,
            repo_state=repo_state,
        )
        worker_report_path = run_dir / f"worker_report.round_{round_index}.json"
        worker_report = codex_structured(
            codex_cli=codex_cli,
            repo=repo,
            schema_file=SCHEMAS_DIR / "worker_report.schema.json",
            prompt=worker_prompt,
            model=args.codex_model,
            output_file=worker_report_path,
        )

        print_banner("running external verification")
        test_result = maybe_run_shell(test_command, repo)
        write_json(run_dir / f"test_result.round_{round_index}.json", test_result)

        diff_payload = collect_diff(repo)
        write_json(run_dir / f"git_diff.round_{round_index}.json", diff_payload)

        review_context_path = run_dir / f"director_review.context.round_{round_index}.json"
        write_json(
            review_context_path,
            build_review_context(
                task=args.task,
                repo=repo,
                repo_state=repo_state,
                director_plan=director_plan,
                worker_report=worker_report,
                test_result=test_result,
                diff_payload=diff_payload,
                round_index=round_index,
                max_rounds=args.max_rounds,
            ),
        )

        print_banner("director is reviewing the implementation")
        review_feedback = claude_structured(
            claude_cli=claude_cli,
            repo=repo,
            run_dir=run_dir,
            context_file=review_context_path,
            schema_file=SCHEMAS_DIR / "director_review.schema.json",
            system_prompt=load_text(PROMPTS_DIR / "director_system.md"),
            user_prompt=(
                "Use this JSON review context: {context_json}. "
                "Review the worker result and decide whether to approve or request changes. "
                "Return only structured output that matches the schema."
            ),
            model=args.claude_model,
            output_file=run_dir / f"director_review.round_{round_index}.json",
        )

        print_banner(summarize_review(review_feedback))
        if review_feedback["decision"] == "approve":
            final_payload = {
                "status": "approved",
                "round": round_index,
                "run_dir": str(run_dir),
                "director_plan": director_plan,
                "final_review": review_feedback,
                "worker_report": worker_report,
                "test_result": test_result,
                "git_diff": diff_payload,
            }
            write_json(run_dir / "result.json", final_payload)
            print_banner(f"approved in round {round_index}; artifacts: {run_dir}")
            return 0

    final_payload = {
        "status": "max_rounds_exceeded",
        "round": args.max_rounds,
        "run_dir": str(run_dir),
        "director_plan": director_plan,
        "final_review": review_feedback,
    }
    write_json(run_dir / "result.json", final_payload)
    print_banner(f"stopped after {args.max_rounds} rounds; artifacts: {run_dir}")
    return 2


def main() -> int:
    try:
        return run_loop(parse_args())
    except KeyboardInterrupt:
        print_banner("interrupted")
        return 130
    except OrchestratorError as exc:
        print_banner(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
