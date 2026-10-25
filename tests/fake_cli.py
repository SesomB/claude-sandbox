"""A stand-in for the `claude` / `opencode` CLIs in tests.

It finds the harness's MCP bridge URL the way the real CLI would (Claude Code: the
--mcp-config argument; OpenCode: OPENCODE_CONFIG_CONTENT), speaks MCP to it over HTTP,
makes the tool calls scripted for this invocation, and prints output in that CLI's format.

FAKE_CLI_SCRIPT = path to JSON {"invocations": [[{"call": name, "args": {...}}, ...], ...]}.
Each run consumes the next invocation and appends its argv to FAKE_CLI_SCRIPT + ".log".
A step {"exit": n} exits with that code; {"sleep": s} sleeps.
"""

import json
import os
import sys
import time
import urllib.request


def rpc(url: str, method: str, params: dict, mid: int) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        url,
        body,
        {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def main() -> None:
    argv = sys.argv[1:]
    script_path = os.environ["FAKE_CLI_SCRIPT"]
    with open(script_path + ".log", "a") as log:
        log.write(
            json.dumps({"argv": argv, "stdin": "" if "run" in argv[:1] else sys.stdin.read()})
            + "\n"
        )
    with open(script_path + ".log") as log:
        n = len(log.readlines()) - 1
    with open(script_path) as f:
        steps = json.load(f)["invocations"][n]
    is_opencode = argv[:1] == ["run"]
    if is_opencode:
        cfg = json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])
        url = cfg["mcp"]["harness"]["url"]
    else:
        url = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["harness"]["url"]
    rpc(url, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}, 1)
    tools = [t["name"] for t in rpc(url, "tools/list", {}, 2)["result"]["tools"]]
    results = []
    for i, step in enumerate(steps, 3):
        if "exit" in step:
            print(step.get("stdout", ""))
            sys.exit(step["exit"])
        if "sleep" in step:
            time.sleep(step["sleep"])
            continue
        assert step["call"] in tools, f"{step['call']} not in {tools}"
        res = rpc(url, "tools/call", {"name": step["call"], "arguments": step.get("args", {})}, i)
        results.append(res["result"])
    with open(script_path + ".results", "a") as f:
        f.write(json.dumps(results) + "\n")
    if is_opencode:
        print(
            json.dumps({"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}})
        )
        print(
            json.dumps(
                {"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": "done"}}
            )
        )
        print(
            json.dumps(
                {
                    "type": "step_finish",
                    "sessionID": "ses_1",
                    "part": {
                        "type": "step-finish",
                        "cost": 0.002,
                        "tokens": {
                            "input": 10,
                            "output": 5,
                            "reasoning": 1,
                            "cache": {"read": 3, "write": 2},
                        },
                    },
                }
            )
        )
    else:
        print(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "done",
                    "session_id": "sess-1",
                    "total_cost_usd": 0.01,
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 50,
                        "cache_creation_input_tokens": 5,
                    },
                }
            )
        )


if __name__ == "__main__":
    main()
