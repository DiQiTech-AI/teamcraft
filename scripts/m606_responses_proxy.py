"""Minimal /v1/responses -> oMLX /v1/chat/completions bridge (M6-06 Codex leg).

OpenCode/openai-compatible servers such as oMLX implement the Chat Completions
API reliably, but Codex 0.17x only speaks the Responses API (``wire_api =
"chat"`` was removed).  This proxy accepts the subset of ``POST /v1/responses``
that Codex uses and forwards it to oMLX's Chat Completions endpoint, translating
messages and function tools both ways.

It is a local, loopback-only test bridge; it reads no credentials and forwards
nothing off-host.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

OMLX_URL = "http://127.0.0.1:8000/v1/chat/completions"


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(item.get("text") or "") for item in content
                       if isinstance(item, dict))
    return "" if content is None else str(content)


def _messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        out.append({"role": "system", "content": instructions})
    raw = payload.get("input")
    if isinstance(raw, str):
        out.append({"role": "user", "content": raw})
        return out
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in (None, "message"):
            role = item.get("role") or "user"
            out.append({"role": role, "content": _text(item.get("content"))})
        elif kind == "function_call":
            call = {
                "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": item.get("name") or "",
                             "arguments": item.get("arguments") or "{}"},
            }
            out.append({"role": "assistant", "content": None, "tool_calls": [call]})
        elif kind == "function_call_output":
            out.append({"role": "tool",
                        "tool_call_id": item.get("call_id") or "",
                        "content": _text(item.get("output"))})
    return out


def _tools(tools: Any) -> list[dict[str, Any]] | None:
    if not isinstance(tools, list):
        return None
    out = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = fn.get("name")
        if not name:
            continue
        out.append({"type": "function",
                    "function": {"name": name,
                                 "description": fn.get("description") or "",
                                 "parameters": fn.get("parameters") or
                                 {"type": "object", "properties": {}}}})
    return out or None


def _call_omlx(body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        OMLX_URL, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _output_items(message: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        items.append({"type": "message", "role": "assistant",
                      "content": [{"type": "output_text", "text": text}]})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        items.append({"type": "function_call",
                      "call_id": call.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                      "name": fn.get("name") or "",
                      "arguments": fn.get("arguments") or "{}"})
    return items


def _sse(event: dict[str, Any]) -> bytes:
    return (f"event: {event['type']}\n"
            f"data: {json.dumps(event, ensure_ascii=False)}\n\n").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:  # keep stdout quiet
        return

    def _json(self, code: int, obj: dict[str, Any]) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            self._json(200, {"object": "list", "data": []})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/responses":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"})
            return
        messages = _messages(payload)
        chat = {"model": payload.get("model") or "Qwen3.8-Flash-Next-oQ4e-mtp",
                "messages": messages, "stream": False}
        tools = _tools(payload.get("tools"))
        if tools:
            chat["tools"] = tools
            chat["tool_choice"] = "auto"
        max_tokens = payload.get("max_output_tokens")
        if isinstance(max_tokens, int) and max_tokens > 0:
            chat["max_tokens"] = max_tokens
        try:
            result = _call_omlx(chat)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self._json(502, {"error": f"upstream: {exc}"})
            return
        message = ((result.get("choices") or [{}])[0].get("message") or {})
        output = _output_items(message)
        response = {
            "id": f"resp_{uuid.uuid4().hex}",
            "object": "response",
            "created_at": int(time.time()),
            "model": chat["model"],
            "status": "completed",
            "output": output,
            "usage": result.get("usage") or {},
        }
        if not payload.get("stream"):
            self._json(200, response)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        created = dict(response, status="in_progress", output=[])
        self.wfile.write(_sse({"type": "response.created", "response": created}))
        for item in output:
            self.wfile.write(_sse({"type": "response.output_item.added",
                                   "output_index": 0, "item": item}))
            if item["type"] == "message":
                text = _text(item.get("content"))
                if text:
                    self.wfile.write(_sse({
                        "type": "response.output_text.delta", "output_index": 0,
                        "content_index": 0, "delta": text}))
            elif item["type"] == "function_call":
                self.wfile.write(_sse({
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0, "delta": item.get("arguments") or "{}"}))
            self.wfile.write(_sse({"type": "response.output_item.done",
                                   "output_index": 0, "item": item}))
        self.wfile.write(_sse({"type": "response.completed", "response": response}))
        self.wfile.flush()
        self.close_connection = True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"m606 responses bridge on http://{args.host}:{args.port}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
