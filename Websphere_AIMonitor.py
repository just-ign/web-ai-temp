#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import datetime
import subprocess

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
# SAFE SSH EXECUTION (UPDATED)
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
            stderr=subprocess.STDOUT
        )
        stdout, _ = res.communicate()

        if not isinstance(stdout, str):
            stdout = stdout.decode("utf-8", "ignore")

        filtered_lines = []
        skip_block = False

        for line in stdout.splitlines():
            if "BROADRIDGE ELECTRONIC COMMUNICATION SYSTEM" in line:
                skip_block = True
                continue
            if skip_block:
                if "*****" in line:
                    skip_block = False
                continue

            filtered_lines.append(line)

        clean_output = "\n".join(filtered_lines).strip()

        return {
            "success": res.returncode == 0,
            "stdout": clean_output,
            "stderr": "",
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
# GET LOGS AFTER ATTEMPT TIME
# -----------------------------------------------------------------------------
def get_logs_after_attempt(host, profile, log_path, attempt_epoch):
    cmd = "test -f {0} && stat -c %Y {0}".format(log_path)
    stat_res = ssh_exec(host, profile["ssh_user"], profile.get("ssh_key_path"), cmd)

    if not stat_res["success"] or not stat_res["stdout"]:
        return None, "Log file not found or cannot read log timestamp."

    try:
        log_mtime = int(stat_res["stdout"].strip())
    except Exception:
        return None, "Unable to determine log modification time."

    if log_mtime < int(attempt_epoch):
        return None, "No new log entries found after restart attempt."

    tail_cmd = "tail -300 {0}".format(log_path)
    tail_res = ssh_exec(host, profile["ssh_user"], profile.get("ssh_key_path"), tail_cmd)

    if not tail_res["success"]:
        return None, "Failed to read latest logs."

    if not tail_res["stdout"].strip():
        return None, "No log entries found after restart attempt."

    return tail_res["stdout"], None


# -----------------------------------------------------------------------------
# HANDLE SERVER
# -----------------------------------------------------------------------------
def handle_server(group_name, host, profile, global_settings):
    print("\n{0} :: {1}".format(group_name, host))

    status_result = ssh_exec(
        host,
        profile["ssh_user"],
        profile.get("ssh_key_path"),
        profile["commands"]["status"]
    )

    if not status_result["success"]:
        print("SSH/status failed")
        print(status_result["stderr"])
        return

    configured, started, down = parse_websphere_status(status_result["stdout"])

    print("Configured JVMs: {0}".format(", ".join(configured) if configured else "None"))
    print("Running JVMs   : {0}".format(", ".join(started) if started else "None"))
    print("Stopped JVMs   : {0}".format(", ".join(down) if down else "None"))

    if not down:
        print("All JVMs are running")
        return

    for jvm in down:
        print("\n==================================================")
        print("JVM DOWN: {0}".format(jvm))
        print("==================================================")

        match = re.search(r'_(wsvmt\d+)_', jvm)
        if not match:
            print("Cannot determine profile for JVM: {0}".format(jvm))
            continue

        profile_suffix = match.group(1)
        profile_dir = "ICI{0}".format(profile_suffix)
        was_home = "/was/wasprofile855/{0}".format(profile_dir)
        log_path = "{0}/logs/{1}/SystemOut.log".format(was_home, jvm)

        start_script = "{0}/bin/startServer.sh".format(was_home)
        start_command = "{0} {1}".format(start_script, jvm)

        retry_count = 2
        wait_time_seconds = global_settings.get("wait_time_seconds", 60)
        started_successfully = False

        for attempt in range(1, retry_count + 1):
            attempt_time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            print("\nAttempt {0} of {1} at {2}".format(attempt, retry_count, attempt_time_str))
            print("Executing command: {0}".format(start_command))

            start_result = ssh_exec(
                host,
                profile["ssh_user"],
                profile.get("ssh_key_path"),
                start_command
            )

            print("Start command return code: {0}".format(start_result["returncode"]))
            if start_result["stdout"]:
                print("Start command output:")
                print(start_result["stdout"])

            time.sleep(wait_time_seconds)

            status_check = ssh_exec(
                host,
                profile["ssh_user"],
                profile.get("ssh_key_path"),
                profile["commands"]["status"]
            )

            _, started_now, _ = parse_websphere_status(status_check["stdout"])

            if jvm in started_now:
                print("Attempt {0} status: SUCCESS".format(attempt))
                print("JVM {0} is now STARTED".format(jvm))
                started_successfully = True
                break

            print("Attempt {0} status: FAILED".format(attempt))
            print("JVM {0} is still DOWN".format(jvm))

        if not started_successfully:
            print("\nFinal status for JVM {0}: FAILED AFTER 2 ATTEMPTS".format(jvm))

    # -------------------------------------------------------------------------
    # FINAL STATUS RECHECK FOR ALL JVMs ON HOST
    # -------------------------------------------------------------------------
    final_check = ssh_exec(
        host,
        profile["ssh_user"],
        profile.get("ssh_key_path"),
        profile["commands"]["status"]
    )

    if not final_check["success"]:
        print("\nFinal status recheck failed for host: {0}".format(host))
        return

    final_configured, final_started, final_down = parse_websphere_status(final_check["stdout"])

    print("\nFinal node summary for host: {0}".format(host))
    print("Configured JVMs: {0}".format(", ".join(final_configured) if final_configured else "None"))
    print("Started JVMs   : {0}".format(", ".join(final_started) if final_started else "None"))
    print("Down JVMs      : {0}".format(", ".join(final_down) if final_down else "None"))

    if not final_down:
        print("All JVMs are UP")
    else:
        print("Some JVMs are still DOWN")


# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------
def main():
    config = load_config("server_config_websphere.json")

    global_settings = config.get("global_settings", {})
    profiles = config.get("middleware_profiles", {})
    server_groups = config.get("server_groups", {})

    for group_name, group in server_groups.items():
        profile_name = group.get("profile")
        profile = profiles.get(profile_name)

        if not profile:
            print("Profile not found: {0}".format(profile_name))
            continue

        for host in group.get("hosts", []):
            handle_server(group_name, host, profile, global_settings)


# -----------------------------------------------------------------------------
if __name__ == "__main__":
    main()