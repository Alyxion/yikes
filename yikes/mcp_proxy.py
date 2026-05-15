from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from typing import Any

from .mcp import McpServerConfig, filter_tools_list, is_tool_allowed


logger = logging.getLogger(__name__)


class McpSseProxy:
    """Bridge one stdio MCP server to MCP SSE transport."""

    def __init__(self, name: str, config: McpServerConfig, port: int = 0) -> None:
        self.name = name
        self.config = config
        self.port = port
        self._actual_port = 0
        self._process: asyncio.subprocess.Process | None = None
        self._server: asyncio.Server | None = None
        self._sse_queues: dict[str, asyncio.Queue[dict[str, Any] | None]] = {}
        self._pending_requests: dict[int | str, str] = {}
        self._stdout_task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        return f"http://localhost:{self._actual_port}/sse"

    @property
    def host_url(self) -> str:
        return f"http://host.docker.internal:{self._actual_port}/sse"

    async def start(self) -> None:
        env = {**os.environ, **self.config.env}
        self._process = await asyncio.create_subprocess_exec(
            self.config.command,
            *self.config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        self._stdout_task = asyncio.create_task(self._read_mcp_stdout())
        self._server = await asyncio.start_server(self._handle_connection, "0.0.0.0", self.port)
        socket = self._server.sockets[0]
        self._actual_port = int(socket.getsockname()[1])

    async def stop(self) -> None:
        for queue in list(self._sse_queues.values()):
            await queue.put(None)
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._process:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
        if self._stdout_task and not self._stdout_task.done():
            self._stdout_task.cancel()
            try:
                await self._stdout_task
            except asyncio.CancelledError:
                pass

    async def _read_stdio_message(self) -> dict[str, Any] | None:
        assert self._process and self._process.stdout
        reader = self._process.stdout
        content_length: int | None = None
        while True:
            line = await reader.readline()
            if not line:
                return None
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                if content_length is not None:
                    break
                continue
            if text.lower().startswith("content-length:"):
                content_length = int(text.split(":", 1)[1].strip())
        if content_length is None:
            return None
        body = await reader.readexactly(content_length)
        return json.loads(body)

    def _write_stdio_message(self, msg: dict[str, Any]) -> None:
        assert self._process and self._process.stdin
        body = json.dumps(msg).encode("utf-8")
        self._process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8") + body)

    async def _read_mcp_stdout(self) -> None:
        try:
            while True:
                msg = await self._read_stdio_message()
                if msg is None:
                    break
                msg = self._filter_response(msg)
                request_id = msg.get("id")
                session_id = self._pending_requests.pop(request_id, None) if request_id is not None else None
                if session_id and session_id in self._sse_queues:
                    await self._sse_queues[session_id].put(msg)
                else:
                    for queue in list(self._sse_queues.values()):
                        await queue.put(msg)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("MCP stdout reader failed for %s", self.name)
        finally:
            for queue in list(self._sse_queues.values()):
                await queue.put(None)

    def _filter_response(self, msg: dict[str, Any]) -> dict[str, Any]:
        tool_filter = self.config.tool_filter
        if not tool_filter:
            return msg
        result = msg.get("result")
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            return {
                **msg,
                "result": {
                    **result,
                    "tools": filter_tools_list(result["tools"], tool_filter),
                },
            }
        return msg

    def _check_request(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        tool_filter = self.config.tool_filter
        if not tool_filter or msg.get("method") != "tools/call":
            return None
        tool_name = msg.get("params", {}).get("name", "")
        if is_tool_allowed(str(tool_name), tool_filter):
            return None
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "error": {
                "code": -32601,
                "message": f"Tool '{tool_name}' is not allowed",
            },
        }

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=30)
            if not request_line:
                writer.close()
                return
            parts = request_line.decode("utf-8", errors="replace").strip().split(" ", 2)
            if len(parts) < 2:
                writer.close()
                return
            method, path = parts[0], parts[1]
            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                if line in (b"\r\n", b"\n", b""):
                    break
                decoded = line.decode("utf-8", errors="replace").strip()
                if ": " in decoded:
                    key, value = decoded.split(": ", 1)
                    headers[key.lower()] = value
            host = headers.get("host", f"localhost:{self._actual_port}")
            if method == "GET" and "/sse" in path:
                await self._handle_sse(writer, host)
                return
            if method == "POST" and "/message" in path:
                content_length = int(headers.get("content-length", "0"))
                body = await reader.readexactly(content_length) if content_length > 0 else b""
                await self._handle_message(writer, _session_id(path), body)
                return
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        except Exception:
            logger.exception("MCP HTTP handler failed for %s", self.name)
            writer.close()

    async def _handle_sse(self, writer: asyncio.StreamWriter, host: str) -> None:
        session_id = str(uuid.uuid4())
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._sse_queues[session_id] = queue
        endpoint = f"http://{host}/message?sessionId={session_id}"
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Connection: keep-alive\r\n"
            b"Access-Control-Allow-Origin: *\r\n\r\n"
        )
        writer.write(f"event: endpoint\ndata: {endpoint}\n\n".encode("utf-8"))
        await writer.drain()
        try:
            while True:
                msg = await queue.get()
                if msg is None:
                    break
                writer.write(f"event: message\ndata: {json.dumps(msg)}\n\n".encode("utf-8"))
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._sse_queues.pop(session_id, None)
            writer.close()

    async def _handle_message(self, writer: asyncio.StreamWriter, session_id: str, body: bytes) -> None:
        try:
            msg: dict[str, Any] = json.loads(body) if body else {}
        except json.JSONDecodeError:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        error = self._check_request(msg)
        if error:
            queue = self._sse_queues.get(session_id)
            if queue:
                await queue.put(error)
            writer.write(b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        if "id" in msg:
            self._pending_requests[msg["id"]] = session_id
        self._write_stdio_message(msg)
        assert self._process and self._process.stdin
        await self._process.stdin.drain()
        writer.write(b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()


class ProxyManager:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._proxies: list[McpSseProxy] = []

    def start(self, servers: dict[str, McpServerConfig], *, container_mode: bool) -> dict[str, str]:
        if not servers:
            return {}
        self._loop = asyncio.new_event_loop()
        urls: dict[str, str] = {}

        async def setup() -> None:
            for name, config in servers.items():
                proxy = McpSseProxy(name, config)
                await proxy.start()
                self._proxies.append(proxy)
                urls[name] = proxy.host_url if container_mode else proxy.url

        self._loop.run_until_complete(setup())
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        return urls

    def stop(self) -> None:
        if not self._loop or not self._thread:
            return

        async def teardown() -> None:
            for proxy in self._proxies:
                await proxy.stop()

        future = asyncio.run_coroutine_threadsafe(teardown(), self._loop)
        try:
            future.result(timeout=10)
        except Exception:
            logger.exception("MCP proxy teardown failed")
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._proxies.clear()


def _session_id(path: str) -> str:
    if "?" not in path:
        return ""
    for param in path.split("?", 1)[1].split("&"):
        if param.startswith("sessionId="):
            return param.split("=", 1)[1]
    return ""
