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
# SAFE SSH EXECUTION
# -----------------------------------------------------------------------------
def ssh_exec(host, user, key_path, command):
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
def call_ai(prompt):
    api_url = os.getenv("API_URL")
    api_key = os.getenv("API_KEY")

    if not api_url or not api_key:
        return "AI API not configured."

    headers = {
        "Authorization": "Bearer {0}".format(api_key),
        "Content-Type": "application/json"
    }

    payload = {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 700
    }

    try:
        r = requests.post(api_url, json=payload, headers=headers, timeout=60)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return "AI call failed: {0}".format(str(e))


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
    find_cmd = "grep -n 'WSVR0023I: Server {0} is stopping' {1} | tail -1".format(jvm, log_path)

    result = ssh_exec(host, profile["ssh_user"], profile.get("ssh_key_path"), find_cmd)

    if not result["success"] or not result["stdout"]:
        return None, None

    try:
        line_number = int(result["stdout"].split(":")[0])
    except Exception:
        return None, None

    ts_cmd = "sed -n '{0}p' {1}".format(line_number, log_path)
    ts_res = ssh_exec(host, profile["ssh_user"], profile.get("ssh_key_path"), ts_cmd)

    timestamp_line = ts_res["stdout"] if ts_res["success"] else "UNKNOWN"

    start_line = max(1, line_number - 200)
    end_line = line_number + 50

    ctx_cmd = "sed -n '{0},{1}p' {2}".format(start_line, end_line, log_path)
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

    results = []
    for title, path in (
        ("SystemOut.log", system_out_log_path),
        ("native_stderr.log", native_stderr_log_path),
    ):
        res = ssh_exec(host, user, key, "tail -n 300 {0}".format(path))
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
# EXCEL REPORT
# -----------------------------------------------------------------------------
REPORT_COLUMNS = ["Patched server", "status", "cause", "ai action"]


def _new_jvm_report():
    return {
        "shutdown_ai": None,
        "startup_ai": None,
        "notes": [],
        "restart_attempted": False,
        "recovered": False,
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
    if info.get("startup_ai"):
        parts.append("Startup failure analysis:\n{0}".format(info["startup_ai"]))
    return "\n\n".join(parts)


def _jvm_cause_text(info, running):
    info = info or {}
    if running:
        if info.get("recovered"):
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

    status_result = ssh_exec(
        host,
        profile["ssh_user"],
        profile.get("ssh_key_path"),
        profile["commands"]["status"]
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

        for attempt in range(retry_count):
            print("\nAttempt {0}".format(attempt + 1))

            ssh_exec(host, profile["ssh_user"], profile.get("ssh_key_path"), start_command)

            time.sleep(wait_time_seconds)

            status_check = ssh_exec(
                host,
                profile["ssh_user"],
                profile.get("ssh_key_path"),
                profile["commands"]["status"]
            )

            _, started_now, _ = parse_websphere_status(status_check["stdout"])

            if jvm in started_now:
                print("Started successfully")
                started_successfully = True
                per_jvm_info[jvm]["recovered"] = True
                break

        if not started_successfully:
            print("Failed to start")
            per_jvm_info[jvm]["restart_attempted"] = True

            startup_logs = get_startup_failure_context(host, profile, log_path)

            if startup_logs:
                ai_start_response = call_ai("""
You are a senior WebSphere engineer.

JVM failed to start.

JVM: {0}

Tasks:
- Exact error
- Root cause
- Evidence

Logs (SystemOut.log and native_stderr.log, last 300 lines each):
{1}
""".format(jvm, startup_logs[-8000:]))

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
        profile["commands"]["status"]
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
                _jvm_ai_text(info) if (not running or (info and (info.get("shutdown_ai") or info.get("startup_ai")))) else "",
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