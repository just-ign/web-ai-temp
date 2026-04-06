#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import datetime
import subprocess
from openpyxl import Workbook
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter

try:
    import requests
except ImportError:
    print("Failed to import requests module. Please install it first.")
    raise


# -----------------------------------------------------------------------------
# LOAD CONFIG
# -----------------------------------------------------------------------------
def load_config(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        print("Failed to load config: {0}".format(e))
        exit(1)


# -----------------------------------------------------------------------------
# GUARD RAILS — read-only by default
# -----------------------------------------------------------------------------
# Remote starts, kills, and other mutations require explicit opt-in.
def write_commands_allowed():
    v = os.getenv("WEB_AI_ALLOW_WRITE_COMMANDS", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def validate_safe_shell_fragment(s):
    """No shell metacharacters in strings we embed in remote commands."""
    if s is None or not isinstance(s, str):
        return False
    if not s:
        return False
    banned = '\n\r\x00;|&$`"\'<>()'
    if any(c in s for c in banned):
        return False
    return True


def validate_absolute_read_path(path):
    if not path or not isinstance(path, str):
        return False
    if not path.startswith("/"):
        return False
    return validate_safe_shell_fragment(path)


def validate_non_interactive_ssh_command(cmd):
    """Block chaining, redirection, subshells on any remote command string."""
    if not cmd or not isinstance(cmd, str):
        return False
    banned = '\n\r\x00;|&$`<>'
    if any(c in cmd for c in banned):
        return False
    if "(" in cmd or ")" in cmd:
        return False
    return True


def validate_startserver_command(cmd, jvm_name):
    """Expected shape: .../startServer.sh <jvm> with no shell injection."""
    if not validate_non_interactive_ssh_command(cmd):
        return False
    if "startServer.sh" not in cmd:
        return False
    parts = cmd.split()
    if len(parts) < 2:
        return False
    if parts[-1] != jvm_name:
        return False
    return True


def _ssh_blocked_read_only():
    return {
        "success": False,
        "stdout": "",
        "stderr": (
            "Blocked: read-only mode (mutating commands disabled). "
            "Set WEB_AI_ALLOW_WRITE_COMMANDS=1 to allow start/kill."
        ),
        "returncode": 1,
    }


# -----------------------------------------------------------------------------
# SAFE SSH EXECUTION
# -----------------------------------------------------------------------------
def ssh_exec(host, user, key_path, command, write_operation=False):
    """
    Run one remote command. By default only read-oriented use is expected;
    set write_operation=True for startServer/kill (requires
    WEB_AI_ALLOW_WRITE_COMMANDS=1).
    """
    if write_operation and not write_commands_allowed():
        return _ssh_blocked_read_only()

    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10"
    ]

    if key_path:
        cmd += ["-i", os.path.expanduser(key_path)]

    cmd.append("{0}@{1}".format(user, host))
    cmd.append(command)

    try:
        res = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = res.communicate()

        if not isinstance(stdout, str):
            stdout = stdout.decode("utf-8", "ignore")
        if not isinstance(stderr, str):
            stderr = stderr.decode("utf-8", "ignore")

        return {
            "success": res.returncode == 0,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
            "returncode": res.returncode
        }
    except Exception as e:
        return {
            "success": False,
            "stdout": "",
            "stderr": str(e),
            "returncode": 1
        }


# -----------------------------------------------------------------------------
# AI CALL
# -----------------------------------------------------------------------------
def _ai_model_name():
    return os.getenv("AI_MODEL", "gpt-4")


def call_ai(prompt, max_tokens=700):
    api_url = os.getenv("API_URL")
    api_key = os.getenv("API_KEY")

    if not api_url or not api_key:
        return "AI API not configured."

    headers = {
        "Authorization": "Bearer {0}".format(api_key),
        "Content-Type": "application/json"
    }

    payload = {
        "model": _ai_model_name(),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": max_tokens
    }

    try:
        r = requests.post(api_url, json=payload, headers=headers, timeout=60)
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        return msg.get("content") or ""
    except Exception as e:
        return "AI call failed: {0}".format(str(e))


def _ai_post_chat(payload):
    """
    POST chat completions. Returns (response_json, None) or (None, error_string).
    """
    api_url = os.getenv("API_URL")
    api_key = os.getenv("API_KEY")
    if not api_url or not api_key:
        return None, "AI API not configured."
    headers = {
        "Authorization": "Bearer {0}".format(api_key),
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(api_url, json=payload, headers=headers, timeout=120)
        if not r.ok:
            return None, "AI HTTP {0}: {1}".format(r.status_code, r.text[:500])
        return r.json(), None
    except Exception as e:
        return None, "AI call failed: {0}".format(str(e))


def _truncate_tool_output(text, limit=32000):
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n... [truncated]"


def run_ai_tool_conversation(
    messages,
    tools,
    dispatch_tool,
    max_tokens=1200,
    max_rounds=24,
):
    """
    Multi-turn chat with OpenAI-style tool_calls. dispatch_tool(name, args) -> str.
    Returns (error_string_or_None, last_assistant_text, messages).
    """
    base = {
        "model": _ai_model_name(),
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    for _ in range(max_rounds):
        payload = dict(base)
        payload["messages"] = messages
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data, err = _ai_post_chat(payload)
        if err:
            return err, "", messages
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            return "AI response missing choices/message", "", messages

        tcalls = msg.get("tool_calls")
        if tcalls:
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "tool_calls": tcalls,
                }
            )
            for i, tc in enumerate(tcalls):
                tid = tc.get("id") or "tool_call_{0}".format(i)
                fn = (tc.get("function") or {}).get("name") or ""
                raw_args = (tc.get("function") or {}).get("arguments") or "{}"
                try:
                    args = json.loads(raw_args)
                except ValueError:
                    args = {}
                    out = "Invalid tool arguments JSON: {0}".format(raw_args[:300])
                else:
                    out = dispatch_tool(fn, args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tid,
                        "content": _truncate_tool_output(out),
                    }
                )
            continue

        return None, msg.get("content") or "", messages

    return "AI tool loop exceeded max_rounds", "", messages


# -----------------------------------------------------------------------------
# PARSE STATUS
# -----------------------------------------------------------------------------
def parse_websphere_status(output):
    if not output:
        return [], [], []

    configured = re.findall(r'Server name:\s+(\S+)', output)
    started = re.findall(r'"([^"]+)" is STARTED', output)
    down = [srv for srv in configured if srv not in started]

    return configured, started, down


# -----------------------------------------------------------------------------
# GET SHUTDOWN CONTEXT
# -----------------------------------------------------------------------------
def get_latest_shutdown_context(host, profile, jvm, log_path):
    if not validate_safe_shell_fragment(jvm) or not validate_absolute_read_path(log_path):
        return None, None

    find_cmd = "grep -n 'WSVR0023I: Server {0} is stopping' {1}".format(jvm, log_path)

    if not validate_non_interactive_ssh_command(find_cmd):
        return None, None

    result = ssh_exec(host, profile["ssh_user"], profile.get("ssh_key_path"), find_cmd)

    if not result["success"] or not result["stdout"]:
        return None, None

    grep_lines = [ln for ln in result["stdout"].splitlines() if ln.strip()]
    if not grep_lines:
        return None, None
    last_hit = grep_lines[-1]
    try:
        line_number = int(last_hit.split(":", 1)[0])
    except Exception:
        return None, None

    ts_cmd = "sed -n '{0}p' {1}".format(line_number, log_path)
    if not validate_non_interactive_ssh_command(ts_cmd):
        return "UNKNOWN", None
    ts_res = ssh_exec(host, profile["ssh_user"], profile.get("ssh_key_path"), ts_cmd)

    timestamp_line = ts_res["stdout"] if ts_res["success"] else "UNKNOWN"

    start_line = max(1, line_number - 200)
    end_line = line_number + 50

    ctx_cmd = "sed -n '{0},{1}p' {2}".format(start_line, end_line, log_path)
    if not validate_non_interactive_ssh_command(ctx_cmd):
        return timestamp_line, None
    ctx_res = ssh_exec(host, profile["ssh_user"], profile.get("ssh_key_path"), ctx_cmd)

    if not ctx_res["success"]:
        return timestamp_line, None

    return timestamp_line, ctx_res["stdout"]


# -----------------------------------------------------------------------------
# GET STARTUP FAILURE LOGS
# -----------------------------------------------------------------------------
def get_startup_failure_context(host, profile, system_out_log_path):
    """
    Last 300 lines of SystemOut.log and native_stderr.log (same server log dir).
    Returns None only if both tail commands fail.
    """
    user = profile["ssh_user"]
    key = profile.get("ssh_key_path")
    native_stderr_log_path = os.path.join(
        os.path.dirname(system_out_log_path),
        "native_stderr.log",
    )

    if not validate_absolute_read_path(system_out_log_path):
        return None
    if not validate_absolute_read_path(native_stderr_log_path):
        return None

    results = []
    for title, path in (
        ("SystemOut.log", system_out_log_path),
        ("native_stderr.log", native_stderr_log_path),
    ):
        tail_cmd = "tail -n 300 {0}".format(path)
        if not validate_non_interactive_ssh_command(tail_cmd):
            results.append((title, {"success": False, "stdout": "", "stderr": "invalid tail command"}))
            continue
        res = ssh_exec(host, user, key, tail_cmd)
        results.append((title, res))

    if not any(r["success"] for _, r in results):
        return None

    parts = []
    for title, res in results:
        if res["success"]:
            block = res["stdout"].rstrip()
            parts.append(
                "=== {0} (last 300 lines) ===\n{1}".format(
                    title, block if block else "(empty)"
                )
            )
        else:
            hint = (res.get("stderr") or "").strip() or "tail failed"
            parts.append(
                "=== {0} (last 300 lines) ===\n[!] {1}".format(title, hint)
            )

    return "\n\n".join(parts)


# -----------------------------------------------------------------------------
# L1 STARTUP TROUBLESHOOTING (AI-planned shell steps, no log-regex inference)
# -----------------------------------------------------------------------------
_L1_DIAG_ALLOWED_FIRST = frozenset({
    "ss", "lsof", "fuser", "ls", "df", "stat", "ps", "cat", "file", "id",
    "whoami", "pwd", "netstat", "readlink", "uname", "hostname", "getconf",
    "test", "[", "head", "tail",
})

# Any token in the command line matching these (after light stripping) is rejected.
_L1_DIAG_FORBIDDEN_TOKENS = frozenset({
    "rm", "rmdir", "mv", "cp", "install", "mkdir", "touch", "chmod", "chown",
    "chgrp", "kill", "killall", "pkill", "xargs", "sudo", "su", "doas",
    "curl", "wget", "nc", "netcat", "ssh", "scp", "sftp", "rsync", "ftp",
    "tee", "dd", "mkfs", "mount", "umount", "shutdown", "reboot", "halt",
    "poweroff", "init", "telinit", "systemctl", "service", "nohup", "at",
    "crontab", "python", "python3", "perl", "ruby", "node", "php",     "exec",
    "eval", "source", "bash", "sh", "zsh", "ksh", "csh", "tcsh",
    "awk", "sed", "find", "tar", "gzip", "gunzip", "bzip2", "xz", "zip",
    "unzip", "docker", "podman", "kubectl", "nmap",
})


def _l1_strip_optional_stderr_redirect(cmd):
    c = cmd.strip()
    if c.endswith(" 2>&1"):
        return c[:-5].rstrip()
    return c


def validate_l1_diagnostic_command(cmd):
    """
    Single read-only remote command; no chaining, subshells, or redirection
    except optional trailing '2>&1'.
    """
    if not cmd or not isinstance(cmd, str):
        return False
    core = _l1_strip_optional_stderr_redirect(cmd)
    if not core:
        return False
    banned = ("\n", ";", "|", "&", "`", "$", "(", ")", ">", "<", "\x00")
    if any(b in core for b in banned):
        return False
    parts = core.split()
    if not parts:
        return False
    if parts[0] not in _L1_DIAG_ALLOWED_FIRST:
        return False
    for raw_t in parts[1:]:
        t = raw_t.strip("\",'").lower()
        if t in _L1_DIAG_FORBIDDEN_TOKENS:
            return False
    low = core.lower()
    for needle in ("${", "$(", "${{", "&&", "||", ">>", " 2>", ">&"):
        if needle in low:
            return False
    return True


def parse_kill_command(cmd):
    """Accept only 'kill -TERM|-15|-9 <pid>' with integer pid."""
    if not cmd or not isinstance(cmd, str):
        return None
    parts = cmd.strip().split()
    if len(parts) != 3 or parts[0] != "kill":
        return None
    if parts[1] not in ("-TERM", "-15", "-9"):
        return None
    if not parts[2].isdigit():
        return None
    pid = int(parts[2])
    if pid <= 0:
        return None
    return pid


def _pid_appears_as_token(pid, blob):
    """True if pid is present as a non-substring-of-longer-digit token in blob."""
    if blob is None:
        return False
    s = str(pid)
    if not s:
        return False
    i = 0
    n = len(blob)
    ls = len(s)
    while True:
        j = blob.find(s, i)
        if j < 0:
            return False
        left_ok = j == 0 or not blob[j - 1].isdigit()
        right_ok = j + ls >= n or not blob[j + ls].isdigit()
        if left_ok and right_ok:
            return True
        i = j + 1


def _l1_kill_flag_from_signal(signal):
    u = (signal or "TERM").strip().upper()
    if u in ("KILL", "SIGKILL"):
        return "-9"
    if u in ("INT",):
        return "-15"
    return "-TERM"


_L1_TOOLS_DIAGNOSTIC = [
    {
        "type": "function",
        "function": {
            "name": "run_diagnostic_command",
            "description": (
                "Run one read-only shell command on the target host via SSH. "
                "Use for L1 checks (ports, files, disk, processes). "
                "At most 8 calls; then call complete_diagnostic_phase."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "purpose": {
                        "type": "string",
                        "description": "Short label for the transcript",
                    },
                    "shell": {
                        "type": "string",
                        "description": (
                            "Single command: no ; | & $ ` ( ). "
                            "Optional suffix ' 2>&1' only. "
                            "Starters: ss, lsof, fuser, ls, df, stat, ps, cat, file, "
                            "id, whoami, pwd, netstat, readlink, uname, hostname, "
                            "getconf, test, [, head, tail."
                        ),
                    },
                },
                "required": ["purpose", "shell"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_diagnostic_phase",
            "description": (
                "Call when you have enough diagnostic output. "
                "Required to finish the diagnostic phase."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_hypothesis": {
                        "type": "string",
                        "description": "One sentence: what you think failed",
                    },
                },
                "required": ["issue_hypothesis"],
            },
        },
    },
]

_L1_TOOL_ESCALATE = {
    "type": "function",
    "function": {
        "name": "escalate_to_admin",
        "description": (
            "Stop automated remediation. Use when the SSH user cannot fix the "
            "issue (permissions, root-owned process, policy). Do not call kill "
            "or retry after this."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "What the admin must do (e.g. free port, kill PID)",
                },
            },
            "required": ["message"],
        },
    },
}

_L1_TOOL_RECORD_ACTIONS = {
    "type": "function",
    "function": {
        "name": "record_recommended_actions",
        "description": (
            "Record shell commands or steps for a human to run locally. "
            "Nothing is executed on the host — transcript only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "string",
                    "description": (
                        "Concrete commands (e.g. kill PID, startServer.sh) for operators"
                    ),
                },
            },
            "required": ["actions"],
        },
    },
}

_L1_TOOL_KILL = {
    "type": "function",
    "function": {
        "name": "kill_process",
        "description": (
            "ONLY when WEB_AI_ALLOW_WRITE_COMMANDS=1: send kill to a PID from "
            "diagnostics. Stale JVM processes only — never shared infrastructure. "
            "Max 3 calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "signal": {
                    "type": "string",
                    "enum": ["TERM", "KILL", "INT"],
                    "description": "TERM default; KILL for -9; INT maps to -15",
                },
            },
            "required": ["pid"],
        },
    },
}

_L1_TOOL_RETRY_START = {
    "type": "function",
    "function": {
        "name": "retry_start_server",
        "description": (
            "ONLY when WEB_AI_ALLOW_WRITE_COMMANDS=1: run startServer.sh, wait, "
            "recheck status. Not if escalated or a kill failed."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

_L1_TOOL_COMPLETE = {
    "type": "function",
    "function": {
        "name": "complete_remediation",
        "description": "Finish remediation with an operator-facing summary.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary_for_operator": {
                    "type": "string",
                    "description": "Short paragraph for the runbook/report",
                },
            },
            "required": ["summary_for_operator"],
        },
    },
}


def _l1_remediation_tools():
    """Mutating tools only if WEB_AI_ALLOW_WRITE_COMMANDS is enabled."""
    if write_commands_allowed():
        return [
            _L1_TOOL_ESCALATE,
            _L1_TOOL_KILL,
            _L1_TOOL_RETRY_START,
            _L1_TOOL_COMPLETE,
        ]
    return [
        _L1_TOOL_ESCALATE,
        _L1_TOOL_RECORD_ACTIONS,
        _L1_TOOL_COMPLETE,
    ]


def run_l1_startup_troubleshooting(
    host,
    profile,
    jvm,
    was_home,
    start_command,
    status_command,
    startup_logs,
    wait_time_seconds,
):
    """
    L1 startup troubleshooting via OpenAI-style chat tool_calls (diagnostic phase,
    then remediation). Diagnostic SSH is read-only. kill_process and retry_start_server
    run only if WEB_AI_ALLOW_WRITE_COMMANDS=1; otherwise use record_recommended_actions.
    """
    notes = []
    lines = []
    user = profile["ssh_user"]
    key = profile.get("ssh_key_path")

    diag_blob = ""
    state1 = {
        "n": 0,
        "hypothesis": "",
        "closed": False,
    }

    def dispatch_diagnostic(name, args):
        nonlocal diag_blob
        if name == "complete_diagnostic_phase":
            state1["closed"] = True
            state1["hypothesis"] = (args or {}).get("issue_hypothesis") or ""
            lines.append(
                "=== L1 issue hypothesis (tools) ===\n{0}".format(state1["hypothesis"])
            )
            return "Diagnostic phase recorded. Do not call run_diagnostic_command again."

        if name == "run_diagnostic_command":
            if state1["closed"]:
                return "Diagnostic phase already closed."
            if state1["n"] >= 8:
                return (
                    "Maximum 8 diagnostic commands used. "
                    "Call complete_diagnostic_phase with your hypothesis."
                )
            purpose = (args or {}).get("purpose") or "diagnostic"
            shell = (args or {}).get("shell") or ""
            if not validate_l1_diagnostic_command(shell):
                return (
                    "Command rejected: must be a single read-only command with an "
                    "allowed first token (see system instructions)."
                )
            lines.append("=== L1 run: {0} ===\n$ {1}".format(purpose, shell))
            res = ssh_exec(host, user, key, shell)
            out = (res.get("stdout") or "").strip()
            err = (res.get("stderr") or "").strip()
            block = out
            if err:
                block = (block + "\n" if block else "") + "[stderr] " + err
            lines.append(block or "(no output)")
            state1["n"] += 1
            diag_blob += "\n" + (block or "")
            return block or "(no output)"

        return "Unknown tool: {0}".format(name)

    sys_diag = (
        "You are L1 operations for IBM WebSphere on UNIX/Linux. "
        "A JVM failed to start after automated retries.\n"
        "Use run_diagnostic_command to gather evidence (max 8 calls), then "
        "complete_diagnostic_phase with issue_hypothesis.\n"
        "Do not use startServer/stopServer, kill, rm, sudo, or chained commands."
    )
    user_diag = (
        "JVM: {0}\nWAS_HOME (context only): {1}\n\n"
        "Startup logs (SystemOut / native_stderr tails):\n{2}"
    ).format(jvm, was_home, startup_logs[-10000:])

    msgs1 = [
        {"role": "system", "content": sys_diag},
        {"role": "user", "content": user_diag},
    ]
    err1, _txt1, _ = run_ai_tool_conversation(
        msgs1,
        _L1_TOOLS_DIAGNOSTIC,
        dispatch_diagnostic,
        max_tokens=1200,
        max_rounds=24,
    )
    if err1:
        lines.append("=== L1 diagnostic phase (API error) ===\n{0}".format(err1))
        notes.append("L1: diagnostic tool phase failed ({0})".format(err1))
        return "\n\n".join(lines), False, notes

    if not state1["closed"]:
        notes.append(
            "L1 tools: model did not call complete_diagnostic_phase; proceeding anyway"
        )
        lines.append(
            "=== L1 issue hypothesis (tools) ===\n(incomplete — model did not close phase)"
        )

    state2 = {
        "escalated": False,
        "kills": 0,
        "kills_attempted": False,
        "kills_all_ok": True,
        "recovered": False,
        "summary": "",
    }

    def dispatch_remediate(name, args):
        if name == "escalate_to_admin":
            state2["escalated"] = True
            msg = (args or {}).get("message") or "Escalate to admin."
            lines.append("=== L1 escalation (tools) ===\n{0}".format(msg))
            notes.append("L1 escalate: {0}".format(msg))
            return (
                "Escalation recorded. Do not call kill_process or retry_start_server."
            )

        if name == "record_recommended_actions":
            actions = ((args or {}).get("actions") or "").strip()
            if not actions:
                return "Provide non-empty actions text."
            lines.append(
                "=== L1 recommended actions (not executed — read-only) ===\n{0}".format(
                    actions[:8000]
                )
            )
            return "Recorded for operator; nothing was run on the host."

        if name == "kill_process":
            if not write_commands_allowed():
                return (
                    "kill_process is disabled (read-only mode). "
                    "Use record_recommended_actions or complete_remediation."
                )
            if state2["escalated"]:
                return "Escalation already set; do not kill."
            if state2["kills"] >= 3:
                return "Maximum 3 kill_process calls reached."
            pid = (args or {}).get("pid")
            if isinstance(pid, float) and pid == int(pid):
                pid = int(pid)
            if pid is None or not isinstance(pid, int):
                return "Invalid pid."
            if not _pid_appears_as_token(pid, diag_blob):
                return (
                    "Refused: PID {0} does not appear in diagnostic output.".format(pid)
                )
            sig = _l1_kill_flag_from_signal((args or {}).get("signal"))
            cmd = "kill {0} {1}".format(sig, pid)
            if parse_kill_command(cmd) is None:
                cmd = "kill -TERM {0}".format(pid)
            lines.append("=== L1 kill (tools) ===\n$ {0}".format(cmd))
            kres = ssh_exec(host, user, key, cmd, write_operation=True)
            state2["kills"] += 1
            state2["kills_attempted"] = True
            kout = (kres.get("stdout") or "").strip()
            kerr = (kres.get("stderr") or "").strip()
            kb = (kout + "\n" + kerr).strip()
            if kb:
                lines.append(kb)
            if not kres.get("success"):
                state2["kills_all_ok"] = False
                notes.append(
                    "L1: kill failed for pid {0}; reach out to admin".format(pid)
                )
                lines.append(
                    "=== L1 kill failed (permission or process) — contact admin ==="
                )
                return "Kill failed (permissions or process). Do not retry_start_server."
            return "Kill sent. Observe before retry_start_server if appropriate."

        if name == "retry_start_server":
            if not write_commands_allowed():
                return (
                    "retry_start_server is disabled (read-only mode). "
                    "Use record_recommended_actions with the start command text."
                )
            if state2["escalated"]:
                return "Cannot retry: escalation set."
            if state2["kills_attempted"] and not state2["kills_all_ok"]:
                return "Cannot retry: a kill failed."
            if not validate_startserver_command(start_command, jvm):
                return "start command failed safety validation; do not retry."
            lines.append("=== L1 retry start (tools) ===\n$ {0}".format(start_command))
            ssh_exec(host, user, key, start_command, write_operation=True)
            time.sleep(wait_time_seconds)
            st = ssh_exec(host, user, key, status_command)
            _, started_now, _ = parse_websphere_status(st.get("stdout") or "")
            if jvm in started_now:
                state2["recovered"] = True
                lines.append("=== L1 retry result: JVM STARTED ===")
                notes.append("Recovered after L1 remediation restart")
                return "JVM is STARTED."
            lines.append("=== L1 retry result: still down ===")
            return "Still down after retry."

        if name == "complete_remediation":
            state2["summary"] = (args or {}).get("summary_for_operator") or ""
            if state2["summary"]:
                lines.append(
                    "=== L1 operator summary (tools) ===\n{0}".format(state2["summary"])
                )
            return "Remediation complete."

        return "Unknown tool: {0}".format(name)

    if write_commands_allowed():
        sys_rem = (
            "You are L1 WebSphere operations. Mutating tools are ENABLED.\n"
            "Choose: escalate_to_admin, or kill_process only for clearly stale PIDs "
            "from diagnostics, optionally retry_start_server, then complete_remediation.\n"
            "If you escalate, do not kill or retry."
        )
    else:
        sys_rem = (
            "You are L1 WebSphere operations in READ-ONLY mode: the host will not "
            "be started or killed by this script.\n"
            "Use record_recommended_actions to write exact commands for a human to run. "
            "Use escalate_to_admin when the operator cannot fix the issue alone. "
            "Always end with complete_remediation."
        )
    user_rem = (
        "JVM: {0}\nWAS_HOME: {1}\n\nLogs (excerpt):\n{2}\n\n"
        "Diagnostic output:\n{3}\n\nHypothesis: {4}"
    ).format(
        jvm,
        was_home,
        startup_logs[-6000:],
        diag_blob[-8000:] if diag_blob else "(none)",
        state1["hypothesis"] or "(none)",
    )
    msgs2 = [
        {"role": "system", "content": sys_rem},
        {"role": "user", "content": user_rem},
    ]
    err2, _txt2, _ = run_ai_tool_conversation(
        msgs2,
        _l1_remediation_tools(),
        dispatch_remediate,
        max_tokens=1200,
        max_rounds=24,
    )
    if err2:
        notes.append("L1 tools: remediation phase API error ({0})".format(err2))
        return "\n\n".join(lines), False, notes

    return "\n\n".join(lines), state2["recovered"], notes


# -----------------------------------------------------------------------------
# EXCEL REPORT
# -----------------------------------------------------------------------------
REPORT_COLUMNS = ["Patched server", "status", "cause", "ai action"]


def _new_jvm_report():
    return {
        "shutdown_ai": None,
        "startup_ai": None,
        "l1_troubleshooting": None,
        "notes": [],
        "restart_attempted": False,
        "recovered": False,
        "recovered_via_l1": False,
    }


def _report_row(host, jvm, status, cause, ai_action):
    return {
        "Patched server": "{0} / {1}".format(host, jvm),
        "status": status,
        "cause": cause,
        "ai action": ai_action,
    }


def _jvm_ai_text(info):
    if not info:
        return ""
    parts = []
    if info.get("shutdown_ai"):
        parts.append("Shutdown analysis:\n{0}".format(info["shutdown_ai"]))
    if info.get("l1_troubleshooting"):
        parts.append(
            "L1 troubleshooting transcript:\n{0}".format(info["l1_troubleshooting"])
        )
    if info.get("startup_ai"):
        parts.append("Startup failure analysis:\n{0}".format(info["startup_ai"]))
    return "\n\n".join(parts)


def _jvm_cause_text(info, running):
    info = info or {}
    if running:
        if info.get("recovered"):
            if info.get("recovered_via_l1"):
                return "Recovered after L1 remediation"
            return "Recovered after automated restart"
        return ""
    parts = []
    if info.get("restart_attempted") and not info.get("recovered"):
        parts.append("Still down after restart attempt(s)")
    parts.extend(info.get("notes") or [])
    return "; ".join(parts) if parts else "Down"


def write_excel_report(rows, output_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Servers"
    ws.append(REPORT_COLUMNS)
    for row in rows:
        ws.append([row.get(col, "") for col in REPORT_COLUMNS])

    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, min_col=1, max_col=4):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    for idx in range(1, len(REPORT_COLUMNS) + 1):
        letter = get_column_letter(idx)
        col_cells = [ws.cell(row=r, column=idx) for r in range(1, ws.max_row + 1)]
        maxlen = min(max(len(str(c.value or "")) for c in col_cells), 60)
        ws.column_dimensions[letter].width = max(12, maxlen + 2)

    wb.save(output_path)
    return True


# -----------------------------------------------------------------------------
# HANDLE SERVER
# -----------------------------------------------------------------------------
def handle_server(group_name, host, profile, global_settings):
    print("\n{0} :: {1}".format(group_name, host))

    per_jvm_info = {}

    def ensure_jvm(j):
        if j not in per_jvm_info:
            per_jvm_info[j] = _new_jvm_report()

    status_cmd = (profile.get("commands") or {}).get("status")
    if not status_cmd or not validate_non_interactive_ssh_command(status_cmd):
        return [
            {
                "Patched server": "{0} | {1}".format(host, group_name),
                "status": "Config error",
                "cause": "commands.status fails safety check (disallowed shell metacharacters)",
                "ai action": "",
            }
        ]

    status_result = ssh_exec(
        host,
        profile["ssh_user"],
        profile.get("ssh_key_path"),
        status_cmd
    )

    if not status_result["success"]:
        print("SSH/status failed")
        print(status_result["stderr"])
        cause = (status_result.get("stderr") or status_result.get("stdout") or "").strip() or "unknown"
        return [
            {
                "Patched server": "{0} | {1}".format(host, group_name),
                "status": "Status check failed",
                "cause": cause,
                "ai action": "",
            }
        ]

    configured, started, down = parse_websphere_status(status_result["stdout"])
    initial_configured = list(configured)

    print("Configured JVMs: {0}".format(", ".join(configured) if configured else "None"))
    print("Running JVMs   : {0}".format(", ".join(started) if started else "None"))
    print("Stopped JVMs   : {0}".format(", ".join(down) if down else "None"))

    if not down:
        print("All JVMs are running")
        return [
            _report_row(host, j, "Running", "", "") for j in configured
        ]

    for jvm in down:
        print("\nJVM DOWN: {0}".format(jvm))
        ensure_jvm(jvm)

        match = re.search(r'_(wsvmt\d+)_', jvm)
        if not match:
            print("Cannot determine profile")
            per_jvm_info[jvm]["notes"].append("Cannot determine profile (expected _wsvmt#_ in server name)")
            continue

        profile_suffix = match.group(1)
        profile_dir = "ICI{0}".format(profile_suffix)
        was_home = "/was/wasprofile855/{0}".format(profile_dir)
        log_path = "{0}/logs/{1}/SystemOut.log".format(was_home, jvm)

        if not validate_absolute_read_path(was_home):
            print("WAS_HOME path failed safety check")
            per_jvm_info[jvm]["notes"].append("WAS_HOME path failed safety validation")
            continue

        if not re.match(r"^[\w.-]+$", jvm):
            print("JVM name failed safety check")
            per_jvm_info[jvm]["notes"].append(
                "JVM name contains disallowed characters for remote commands"
            )
            continue
        if not validate_absolute_read_path(log_path):
            print("Log path failed safety check")
            per_jvm_info[jvm]["notes"].append("Derived log path failed safety validation")
            continue

        timestamp, logs = get_latest_shutdown_context(host, profile, jvm, log_path)

        if logs:
            print("Shutdown at: {0}".format(timestamp))

            ai_response = call_ai("""
You are a senior WebSphere engineer.

Analyze JVM shutdown.

JVM: {0}

Tasks:
- Exact timestamp
- Trigger (manual / crash / memory / deployment)
- Root cause
- Evidence (log lines)

Logs:
{1}
""".format(jvm, logs[-8000:]))

            per_jvm_info[jvm]["shutdown_ai"] = ai_response
            print("\nShutdown Analysis:")
            print(ai_response)
        else:
            print("No shutdown logs found")
            per_jvm_info[jvm]["notes"].append("No shutdown logs found")

        start_script = "{0}/bin/startServer.sh".format(was_home)
        start_command = "{0} {1}".format(start_script, jvm)

        retry_count = global_settings.get("retry_count", 1)
        wait_time_seconds = global_settings.get("wait_time_seconds", 60)
        started_successfully = False
        restart_tried = False

        if write_commands_allowed():
            if not validate_startserver_command(start_command, jvm):
                print("Start command failed safety validation; skipping start attempts")
                per_jvm_info[jvm]["notes"].append(
                    "startServer command failed safety validation; not executed"
                )
            else:
                restart_tried = True
                for attempt in range(retry_count):
                    print("\nAttempt {0}".format(attempt + 1))

                    ssh_exec(
                        host,
                        profile["ssh_user"],
                        profile.get("ssh_key_path"),
                        start_command,
                        write_operation=True,
                    )

                    time.sleep(wait_time_seconds)

                    status_check = ssh_exec(
                        host,
                        profile["ssh_user"],
                        profile.get("ssh_key_path"),
                        status_cmd,
                    )

                    _, started_now, _ = parse_websphere_status(status_check["stdout"])

                    if jvm in started_now:
                        print("Started successfully")
                        started_successfully = True
                        per_jvm_info[jvm]["recovered"] = True
                        break
        else:
            print(
                "Read-only mode: skipping startServer.sh "
                "(set WEB_AI_ALLOW_WRITE_COMMANDS=1 to enable)"
            )
            per_jvm_info[jvm]["notes"].append(
                "Read-only mode: startServer.sh not executed"
            )

        if not started_successfully:
            if restart_tried:
                print("Failed to start")
                per_jvm_info[jvm]["restart_attempted"] = True
            else:
                print("JVM still down (no automated start in read-only mode or invalid start command)")

            startup_logs = get_startup_failure_context(host, profile, log_path)

            if startup_logs:
                l1_text, l1_recovered, l1_notes = run_l1_startup_troubleshooting(
                    host,
                    profile,
                    jvm,
                    was_home,
                    start_command,
                    status_cmd,
                    startup_logs,
                    wait_time_seconds,
                )
                per_jvm_info[jvm]["l1_troubleshooting"] = l1_text
                for note in l1_notes:
                    per_jvm_info[jvm]["notes"].append(note)
                if l1_recovered:
                    per_jvm_info[jvm]["recovered"] = True
                    per_jvm_info[jvm]["recovered_via_l1"] = True
                    started_successfully = True

                if l1_text:
                    print("\nL1 troubleshooting:")
                    print(l1_text)

                combined_for_ai = startup_logs
                if l1_text:
                    combined_for_ai = "{0}\n\n{1}".format(startup_logs, l1_text)

                ai_start_response = call_ai(
                    """
You are a senior WebSphere engineer.

JVM is down after monitoring (retries may have been skipped in read-only mode).

JVM: {0}

Tasks:
- Exact error and root cause from logs
- Evidence (log lines)
- If an L1 troubleshooting transcript is included, summarize what was run, whether anything was killed, escalations, and retry outcome.

Logs plus optional L1 transcript:
{1}
""".format(
                        jvm,
                        combined_for_ai[-12000:],
                    ),
                    max_tokens=1000,
                )

                per_jvm_info[jvm]["startup_ai"] = ai_start_response
                print("\nStartup Failure Analysis:")
                print(ai_start_response)
            else:
                per_jvm_info[jvm]["notes"].append(
                    "Failed to start; startup logs unavailable for AI analysis"
                )

    final_check = ssh_exec(
        host,
        profile["ssh_user"],
        profile.get("ssh_key_path"),
        status_cmd,
    )

    if not final_check["success"]:
        print("\nFinal status recheck failed for host: {0}".format(host))
        rows = []
        for jvm in initial_configured:
            info = per_jvm_info.get(jvm)
            rows.append(
                _report_row(
                    host,
                    jvm,
                    "Unknown",
                    "Final status recheck failed (SSH or empty output)",
                    _jvm_ai_text(info),
                )
            )
        return rows

    final_configured, final_started, final_down = parse_websphere_status(final_check["stdout"])

    print("\nFinal node summary for host: {0}".format(host))
    print("Configured JVMs: {0}".format(", ".join(final_configured) if final_configured else "None"))
    print("Started JVMs   : {0}".format(", ".join(final_started) if final_started else "None"))
    print("Down JVMs      : {0}".format(", ".join(final_down) if final_down else "None"))

    if not final_down:
        print("All JVMs are UP")
    else:
        print("Some JVMs are still DOWN")

    rows = []
    for jvm in final_configured:
        running = jvm in final_started
        info = per_jvm_info.get(jvm)
        rows.append(
            _report_row(
                host,
                jvm,
                "Running" if running else "Down",
                _jvm_cause_text(info, running),
                _jvm_ai_text(info)
                if (
                    not running
                    or (
                        info
                        and (
                            info.get("shutdown_ai")
                            or info.get("startup_ai")
                            or info.get("l1_troubleshooting")
                        )
                    )
                )
                else "",
            )
        )
    return rows


# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------
def main():
    config = load_config("server_config_websphere.json")

    global_settings = config.get("global_settings", {})
    profiles = config.get("middleware_profiles", {})
    server_groups = config.get("server_groups", {})

    all_rows = []

    for group_name, group in server_groups.items():
        profile_name = group.get("profile")
        profile = profiles.get(profile_name)

        if not profile:
            print("Profile not found: {0}".format(profile_name))
            continue

        for host in group.get("hosts", []):
            rows = handle_server(group_name, host, profile, global_settings)
            if rows:
                all_rows.extend(rows)

    report_path = os.getenv(
        "WEB_AI_REPORT",
        "websphere_monitor_report_{0}.xlsx".format(
            datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        ),
    )
    if write_excel_report(all_rows, report_path):
        print("\nExcel report written: {0}".format(os.path.abspath(report_path)))


# -----------------------------------------------------------------------------
if __name__ == "__main__":
    main()