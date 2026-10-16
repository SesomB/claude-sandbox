#!/usr/bin/env python3
"""Minimal OpenAI-compatible LLM stub for offline Hermes smoke tests.

It answers just enough of the API for Hermes to run one agent turn without a
real model:

  GET  /v1/models            -> one model with a 128K context
  POST /v1/chat/completions  -> streaming (SSE) or plain JSON

Behaviour of /v1/chat/completions:
  * No tool result in the conversation yet and a terminal-like tool is offered
    -> reply with a single tool call that runs MOCK_COMMAND.
  * Otherwise -> reply with plain text that echoes the last tool output, so
    the smoke test can see the command really ran on the server.

Standard library only, so it runs on the air-gapped server with any Python 3.

  python3 mock_llm.py --host 127.0.0.1 --port 8000
  MOCK_COMMAND='touch ~/work/hello.txt && uname -a' python3 mock_llm.py
"""

import argparse
import json
import os
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = os.environ.get("MOCK_MODEL", "mock-hermes")
CONTEXT = int(os.environ.get("MOCK_CONTEXT", "131072"))
COMMAND = os.environ.get(
    "MOCK_COMMAND", "echo hermes-airgap-ok > hello.txt && uname -a && pwd"
)
# Tool names Hermes (or a fork) may use for shell execution, most likely first.
TERMINAL_TOOL_NAMES = ("terminal", "shell", "bash", "run_command", "execute_command")


def pick_terminal_tool(tools):
    """Return (name, arguments) for the first shell-like tool offered."""
    by_name = {}
    for tool in tools or []:
        fn = tool.get("function", tool)
        if fn.get("name"):
            by_name[fn["name"]] = fn
    for name in TERMINAL_TOOL_NAMES:
        fn = by_name.get(name)
        if fn is None:
            continue
        params = fn.get("parameters") or {}
        props = params.get("properties") or {}
        # Prefer a property literally called "command"; else the first
        # required string property.
        if "command" in props:
            arg = "command"
        else:
            required = params.get("required") or list(props)
            arg = next(
                (p for p in required if props.get(p, {}).get("type") == "string"),
                "command",
            )
        return name, {arg: COMMAND}
    # No shell tool (e.g. preflight's probe): call the first tool with
    # placeholder arguments derived from its JSON schema.
    for fn in by_name.values():
        props = (fn.get("parameters") or {}).get("properties") or {}
        sample = {"integer": 2, "number": 2, "boolean": True, "array": [], "object": {}}
        return fn["name"], {k: sample.get(v.get("type"), "test") for k, v in props.items()}
    return None


def last_tool_output(messages):
    for msg in reversed(messages or []):
        if msg.get("role") == "tool":
            content = msg.get("content")
            if isinstance(content, list):
                content = " ".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            return str(content)
    return None


def plan_reply(body):
    """Decide the assistant message: ('tool', name, args) or ('text', str)."""
    messages = body.get("messages", [])
    output = last_tool_output(messages)
    if output is None:
        tool = pick_terminal_tool(body.get("tools"))
        if tool:
            return ("tool",) + tool
        return ("text", "mock-llm: no terminal tool was offered.")
    return ("text", "mock-llm: command finished. Output:\n" + output[:2000])


class Handler(BaseHTTPRequestHandler):
    server_version = "mock-llm/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("mock-llm: " + (fmt % args) + "\n")

    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("/v1/models", "/models"):
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": MODEL,
                            "object": "model",
                            "owned_by": "mock",
                            "created": 0,
                            # vLLM and llama.cpp style context hints.
                            "max_model_len": CONTEXT,
                            "context_length": CONTEXT,
                        }
                    ],
                },
            )
        elif path in ("", "/health", "/v1/health"):
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": {"message": "not found: " + self.path}})

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._json(404, {"error": {"message": "not found: " + self.path}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": {"message": "invalid JSON"}})
            return

        reply = plan_reply(body)
        cid = "chatcmpl-" + uuid.uuid4().hex[:12]
        created = int(time.time())
        if reply[0] == "tool":
            _, name, args = reply
            call = {
                "id": "call_" + uuid.uuid4().hex[:12],
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            message = {"role": "assistant", "content": None, "tool_calls": [call]}
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": reply[1]}
            finish = "stop"
        usage = {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}

        if not body.get("stream"):
            self._json(
                200,
                {
                    "id": cid,
                    "object": "chat.completion",
                    "created": created,
                    "model": body.get("model", MODEL),
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": usage,
                },
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def chunk(delta, finish_reason=None, extra=None):
            payload = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": body.get("model", MODEL),
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            if extra:
                payload.update(extra)
            self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
            self.wfile.flush()

        if finish == "tool_calls":
            call = message["tool_calls"][0]
            chunk({"role": "assistant", "content": None,
                   "tool_calls": [dict(call, index=0)]})
        else:
            chunk({"role": "assistant", "content": message["content"]})
        chunk({}, finish, {"usage": usage})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    opts = parser.parse_args()
    httpd = ThreadingHTTPServer((opts.host, opts.port), Handler)
    sys.stderr.write(f"mock-llm: serving {MODEL} on http://{opts.host}:{opts.port}/v1\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
