"""Turn an alert into a read-only investigation, without a human in the loop.

Operator 2026-10-09: *「一旦发生报警，这车就停下来，自动去调用大模型（用 Codex）
去检查这个问题…每次遇到问题就主动去调取，就不用等它定期了。」*

So this module does exactly one thing: when the state on disk says something is
wrong, it starts ``codex exec`` in a **read-only** sandbox with the evidence
attached, and files the answer next to the data. The stop itself is not done
here -- the runners already stop themselves and write the HALT -- and nothing
here can start them again.

Four rules, and each one is load-bearing:

* **Read-only, by construction.** ``-s read-only`` is passed on the command
  line, so the promise does not depend on the model reading the prompt. The
  report is written by the CLI (``-o``), not by the sandboxed agent.
* **One investigation per problem, not per round.** The fingerprint is
  ``(kind, symbol, family)``, and the cooldown/daily cap are checked before a
  process starts. A watcher that investigates its own alert every 30 seconds is
  a watcher that spends the operator's quota on one broken RPC.
* **Never in the hot path.** ``launch`` returns as soon as the process exists;
  the cycle that produced the alert is not waiting for a model.
* **A failed investigation is still reported.** No Codex binary, no network, a
  non-zero exit: all three land in the state file with the reason, because "the
  thing that was supposed to tell me what happened did not run" is itself
  something the operator has to know.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_DIR = ROOT / "data-v2"
DIR_NAME = "investigations"
STATE_NAME = "state.json"
INDEX_NAME = "index.jsonl"
#: Keep the finished list bounded: this file is read by the board every second.
KEEP_DONE = 20

DEFAULT_BINARY = os.environ.get("ARB_INVESTIGATE_BIN", "codex")
DEFAULT_MODEL = os.environ.get("ARB_INVESTIGATE_MODEL", "").strip()
DEFAULT_MAX_PER_DAY = 20
DEFAULT_COOLDOWN_S = 300.0
#: A problem that stays broken backs off instead of being re-investigated every
#: five minutes: 5 min, 10, 20, 40, 80, then the ceiling. Six tries in the first
#: day of one stuck HALT, not twenty -- the remaining budget belongs to the *new*
#: problems, which is what the operator asked to be told about.
DEFAULT_MAX_COOLDOWN_S = 21600.0


def settings(env: dict | None = None) -> dict:
    env = env if env is not None else os.environ
    return {
        "binary": env.get("ARB_INVESTIGATE_BIN") or DEFAULT_BINARY,
        "model": (env.get("ARB_INVESTIGATE_MODEL") or DEFAULT_MODEL).strip(),
        "sandbox": "read-only",
        "max_per_day": _int(env.get("ARB_INVESTIGATE_MAX_PER_DAY"),
                            DEFAULT_MAX_PER_DAY),
        "cooldown_s": _float(env.get("ARB_INVESTIGATE_COOLDOWN_S"),
                             DEFAULT_COOLDOWN_S),
        "max_cooldown_s": _float(env.get("ARB_INVESTIGATE_MAX_COOLDOWN_S"),
                                 DEFAULT_MAX_COOLDOWN_S),
        "timeout_s": _int(env.get("ARB_INVESTIGATE_TIMEOUT_S"), 900),
        "enabled": (env.get("ARB_INVESTIGATE_ENABLED", "1")
                    not in ("0", "false", "no", "")),
    }


def directory(data_dir: Path | str | None = None) -> Path:
    return Path(data_dir or DEFAULT_DATA_DIR) / DIR_NAME


def state_path(data_dir: Path | str | None = None) -> Path:
    return directory(data_dir) / STATE_NAME


def state_load(data_dir: Path | str | None = None) -> dict:
    try:
        state = json.loads(state_path(data_dir).read_text())
    except (OSError, ValueError):
        state = {}
    state.setdefault("day", "")
    state.setdefault("launched_today", 0)
    state.setdefault("last_launch_ms", 0)
    state.setdefault("seen", {})
    state.setdefault("running", [])
    state.setdefault("done", [])
    return state


def state_save(state: dict, data_dir: Path | str | None = None) -> Path:
    target = state_path(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n")
    return target


def decide(*, alert: dict, state: dict, now_ms: int,
           settings: dict) -> tuple[bool, str]:
    """``(may_launch, reason)``. Every refusal names itself."""
    if not settings.get("enabled", True):
        return False, "disabled"
    fingerprint = str(alert.get("fingerprint") or "")
    day = time.strftime("%Y-%m-%d", time.localtime(now_ms / 1000))
    if state.get("day") != day:
        state["day"] = day
        state["launched_today"] = 0
    if int(state.get("launched_today") or 0) >= int(settings["max_per_day"]):
        return False, "daily_cap"
    seen = (state.get("seen") or {}).get(fingerprint)
    if seen:
        age_s = now_ms / 1000 - float(seen.get("last_ms") or 0) / 1000
        wait_s = cooldown_for(seen, settings)
        if age_s < wait_s:
            return False, f"cooldown({age_s:.0f}s/{wait_s:.0f}s)"
    for item in state.get("running") or []:
        if item.get("fingerprint") == fingerprint:
            return False, "already_running"
    if not _binary_available(str(settings.get("binary") or "")):
        return False, "binary_missing"
    return True, "ok"


def cooldown_for(seen: dict, settings: dict) -> float:
    """How long to wait before looking at this same problem again.

    Doubles with every investigation of the same fingerprint. A state that heals
    itself (a transient RPC) is re-checked quickly; a state that is *meant* to
    stay broken until a human acts (a HALT with coins on the floor) stops
    consuming the daily budget after a handful of looks.
    """
    base = float(settings.get("cooldown_s") or DEFAULT_COOLDOWN_S)
    ceiling = float(settings.get("max_cooldown_s") or DEFAULT_MAX_COOLDOWN_S)
    count = max(1, int(seen.get("count") or 1))
    return min(base * (2 ** (count - 1)), max(base, ceiling))


def prompt(*, alert: dict, data_dir: Path | str | None = None,
           evidence: dict | None = None, now_ms: int | None = None) -> str:
    """The instruction. Short, evidence-first, and explicit about what not to do."""
    data_dir = Path(data_dir or DEFAULT_DATA_DIR)
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ms / 1000))
    if evidence is None:
        try:
            from arb_monitor.xstocks_v2 import incident

            evidence = incident.read(data_dir)
        except Exception:  # noqa: BLE001 - evidence is a bonus, not a gate
            evidence = {}
    lines = [
        "你是 arb 项目（/home/operator/Documents/arb）的**只读**故障排查助手。",
        "自动监控刚刚因为下面这条报警停机，需要你判断它是不是真问题。",
        "",
        f"时间：{when}",
        f"报警类型：{alert.get('kind')}（严重度 {alert.get('severity')}）",
        f"交易对：{alert.get('symbol') or '—'}",
        f"家族：{alert.get('family') or '—'}",
        f"报警内容：{alert.get('detail')}",
        f"相关文件：{alert.get('path') or '—'}",
        "",
        "本次尝试的动钱证据（data-v2/xstocks_v2_incident.json）：",
        json.dumps(evidence or {}, ensure_ascii=False)[:2000],
        "",
        "先读这几份再下结论：",
        "1. `AGENTS.md`（尤其「推进边界」的「仍然不碰」四条）；",
        "2. `HANDOFF.md` 顶部最新一轮追加；",
        "3. `docs/xstocks-v2-todo.md`。",
        f"4. 数据目录 `{data_dir}`：`xstocks_v2_cycles.jsonl` 最后 20 行、"
        "`xstocks_v2_live.json`、`xstocks_v2_incident.json`。",
        "",
        "按这个顺序回答，尽量短：",
        "1. **一句话结论**：这笔有没有真钱动过？(没有 / 有 / 未知) + 一句依据；",
        "2. **根因**：崩在哪个文件哪一行，为什么会崩；",
        "3. **证据**：`文件:行` 或你跑过的命令；",
        "4. **要人做什么**：删哪个停机文件 / 补什么 / 或什么都不用做；",
        "5. **建议的代码修复**（有就写，没有就写「无」）。",
        "",
        "硬规则：",
        "- 只读。不要创建、修改或删除任何文件；不要碰 KILL_SWITCH / AUTO_HALT / "
        "xstocks_v2_HALT / phase / mode / auto_orders / live_ack；不要下单；不要改代码。",
        "- 不要运行任何写操作或广播类命令。",
        "- 查不到就写「待确认」，不要编造数字或文件行号。",
    ]
    return "\n".join(lines)


def command(*, alert: dict, report: Path, repo_root: Path | str = ROOT,
            settings: dict | None = None, prompt_text: str | None = None,
            data_dir: Path | str | None = None) -> list[str]:
    """The exact argv. Separated out so a test can assert it without running it."""
    settings = settings or {}
    argv = [str(settings.get("binary") or DEFAULT_BINARY), "exec",
            "-s", str(settings.get("sandbox") or "read-only"),
            "--skip-git-repo-check",
            "-C", str(repo_root),
            "--color", "never",
            "-o", str(report)]
    model = str(settings.get("model") or "").strip()
    if model:
        argv += ["-m", model]
    argv.append(prompt_text if prompt_text is not None
                else prompt(alert=alert, data_dir=data_dir))
    return argv


def launch(*, alert: dict, state: dict, now_ms: int, settings: dict,
           data_dir: Path | str | None = None,
           repo_root: Path | str = ROOT,
           popen=subprocess.Popen) -> dict:
    """Start one investigation and return its record. Never waits for it.

    ``popen`` is injectable so the tests can prove the argv, the detached
    session and the bookkeeping without launching a model.
    """
    data_dir = Path(data_dir or DEFAULT_DATA_DIR)
    out_dir = directory(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now_ms / 1000))
    symbol = str(alert.get("symbol") or "all")
    ident = f"{stamp}-{alert.get('kind')}-{symbol}"
    report = out_dir / f"{ident}.md"
    log = out_dir / f"{ident}.log"
    argv = command(alert=alert, report=report, repo_root=repo_root,
                   settings=settings, data_dir=data_dir)
    record = {
        "id": ident, "kind": alert.get("kind"), "severity": alert.get("severity"),
        "symbol": alert.get("symbol") or "", "family": alert.get("family") or "",
        "fingerprint": alert.get("fingerprint") or "",
        "detail": alert.get("detail") or "",
        "report": str(report), "log": str(log),
        "started_ms": now_ms, "pid": 0, "argv": argv[:5],
    }
    try:
        with open(log, "ab") as handle:
            process = popen(argv, stdout=handle, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, cwd=str(repo_root),
                            start_new_session=True)
        record["pid"] = int(getattr(process, "pid", 0) or 0)
        record["ok"] = True
    except Exception as exc:  # noqa: BLE001 - a spawn that fails is a result too
        record["ok"] = False
        record["error"] = f"{type(exc).__name__}: {exc}"[:200]
        record["finished_ms"] = now_ms
    state.setdefault("running", [])
    state.setdefault("done", [])
    state.setdefault("seen", {})
    seen = state["seen"].setdefault(record["fingerprint"], {"count": 0})
    seen["count"] = int(seen.get("count") or 0) + 1
    seen["last_ms"] = now_ms
    seen["detail"] = record["detail"][:200]
    if record["ok"]:
        state["running"].append(record)
        state["launched_today"] = int(state.get("launched_today") or 0) + 1
        state["last_launch_ms"] = now_ms
    else:
        state["done"].append(record)
    _index(out_dir, record)
    return record


def reap(*, state: dict, now_ms: int, settings: dict | None = None,
         data_dir: Path | str | None = None) -> list[dict]:
    """Move finished investigations out of ``running``, with their answer.

    Returns the ones that finished *this* pass, so the watcher can tell the
    operator once -- not once per round forever.
    """
    settings = settings or {}
    timeout_s = float(settings.get("timeout_s") or 900)
    finished: list[dict] = []
    still_running: list[dict] = []
    for item in state.get("running") or []:
        pid = int(item.get("pid") or 0)
        spent_s = (now_ms - int(item.get("started_ms") or 0)) / 1000
        if _alive(pid) and spent_s <= timeout_s:
            still_running.append(item)
            continue
        item = dict(item)
        item["finished_ms"] = now_ms
        item["outcome"] = ("timeout" if _alive(pid) else "finished")
        item["summary"] = report_summary(Path(str(item.get("report") or "")))
        finished.append(item)
    state["running"] = still_running
    state["done"] = (finished + list(state.get("done") or []))[:KEEP_DONE]
    return finished


def report_summary(path: Path, *, lines: int = 6) -> str:
    """The first few non-empty lines of a report: the conclusion, in the open."""
    try:
        text = path.read_text()
    except OSError:
        return ""
    kept = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(kept[:lines])[:800]


def format_launched(record: dict) -> str:
    return ("【自动自检启动】" + str(record.get("kind")) + " "
            + str(record.get("symbol") or "—") + "\n"
            + str(record.get("detail") or "")[:200] + "\n"
            + "报告稍后送到同一处；过程只读，不会动开关或下单。\n"
            + str(record.get("report") or "")).strip()


def format_finished(record: dict) -> str:
    answer = str(record.get("summary") or "").strip() or "（报告为空，见 log）"
    return ("【自动自检完成】" + str(record.get("kind")) + " "
            + str(record.get("symbol") or "—")
            + "（" + str(record.get("outcome") or "") + "）\n"
            + answer + "\n" + str(record.get("report") or "")).strip()


def _index(out_dir: Path, record: dict) -> None:
    try:
        with open(out_dir / INDEX_NAME, "a") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _binary_available(binary: str) -> bool:
    if not binary:
        return False
    if "/" in binary:
        return Path(binary).exists()
    return shutil.which(binary) is not None


def _int(value, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _float(value, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default
