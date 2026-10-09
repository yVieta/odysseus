#!/usr/bin/env python3
"""
aigents_bus_server.py

MCP server (stdio transport) that wraps the aigents testing MCP bus
(http://127.0.0.1:8765/mcp). This lets the Odysseus chat/Test Manager
use the bus tools: assign_test_case, get_findings, get_agent_status,
start_agent, stop_agent, mail_report, publish_note, publish_process, etc.

Run via stdio from Odysseus' builtin MCP registration.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx

# Allow importing from the Odysseus root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
    CallToolResult,
    ListToolsResult,
    ListResourcesResult,
    ReadResourceResult,
    Resource,
)

# Bus endpoint (can be overridden via env)
BUS_URL = os.environ.get("AIGENTS_BUS_URL", "http://127.0.0.1:8765/mcp")
REQUEST_TIMEOUT = float(os.environ.get("AIGENTS_BUS_TIMEOUT", "60.0"))

server = Server("aigents-bus")

# --- JSON-RPC helpers --------------------------------------------------------

async def _rpc(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    """Send one JSON-RPC request to the aigents bus and return the result."""
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(BUS_URL, json=payload, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Bus error: {data['error']}")
    return data.get("result", {})


# --- MCP tool definitions (mirror the bus's TOOLS) ---------------------------

TOOLS = [
    Tool(
        name="assign_test_case",
        description=(
            "Assign a test case to the e2e tester and/or the pentester "
            "for the set SUT (OWASP Juice Shop), optionally starting "
            "the agents. This is the test manager's control function."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "enum": ["e2e", "pentester", "manager", "both", "all"],
                    "description": "which agent(s) should run the test case",
                },
                "test_case": {
                    "type": "string",
                    "description": "the test case / scenario to execute",
                },
                "instruction": {
                    "type": "string",
                    "description": "extra role-specific direction",
                    "default": "",
                },
                "start": {
                    "type": "boolean",
                    "default": True,
                    "description": "start the agent services after queuing",
                },
            },
            "required": ["role", "test_case"],
        },
    ),
    Tool(
        name="list_tasks",
        description="List assigned test-case tasks (optionally filtered).",
        inputSchema={
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "running", "done", "failed", ""]},
                "limit": {"type": "integer", "default": 20},
            },
        },
    ),
    Tool(
        name="get_next_task",
        description=("Claim the oldest pending task for a role (marks it "
                     "running). Used by the workers."),
        inputSchema={
            "type": "object",
            "properties": {"role": {"type": "string", "enum": ["e2e", "pentester", "manager"]}},
            "required": ["role"],
        },
    ),
    Tool(
        name="has_open_task",
        description=("Tell whether a role already has an open (pending or "
                     "running) task — optionally filtered to a specific test "
                     "case. Lets the test manager avoid dispatching duplicate "
                     "work to the team."),
        inputSchema={
            "type": "object",
            "properties": {
                "role": {"type": "string", "enum": ["e2e", "pentester", "manager"]},
                "test_case": {"type": "string", "default": ""},
            },
            "required": ["role"],
        },
    ),
    Tool(
        name="submit_findings",
        description=("Share a role's findings/knowledge on the bus and close "
                     "the task (done on success, failed on a broken run). "
                     "This is how the agents exchange knowledge."),
        inputSchema={
            "type": "object",
            "properties": {
                "role": {"type": "string", "enum": ["e2e", "pentester", "manager"]},
                "task_id": {"type": "integer"},
                "findings": {"type": "string"},
                "kind": {"type": "string", "default": "findings"},
                "success": {"type": "boolean", "default": True},
            },
            "required": ["role", "findings"],
        },
    ),
    Tool(
        name="get_findings",
        description="Read the knowledge other roles have shared (newest first).",
        inputSchema={
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
        },
    ),
    Tool(
        name="get_agent_status",
        description="Show the systemd state of the agent services.",
        inputSchema={
            "type": "object",
            "properties": {"role": {"type": "string"}},
        },
    ),
    Tool(
        name="start_agent",
        description="Start the e2e/pentester/manager agent services.",
        inputSchema={
            "type": "object",
            "properties": {"role": {"type": "string", "default": "all"}},
        },
    ),
    Tool(
        name="stop_agent",
        description="Stop the agent services.",
        inputSchema={
            "type": "object",
            "properties": {"role": {"type": "string", "default": "all"}},
        },
    ),
    Tool(
        name="publish_process",
        description=("Compose the live testing-process markdown and publish it "
                     "to the Odysseus document library."),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_process",
        description="Return the live testing-process markdown.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="publish_note",
        description=("Publish (or overwrite) the test results as a note in the "
                     "Odysseus Notes panel (web UI). Same title + label updates "
                     "the existing note so each role keeps its latest results."),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "note title (stable titles update in place)"},
                "content": {"type": "string", "description": "markdown body of the note"},
                "label": {"type": "string", "default": "test-results"},
            },
            "required": ["title", "content"],
        },
    ),
    Tool(
        name="list_notes",
        description=("List the notes on the Odysseus Notes panel — the latest "
                     "test results each role published (optionally one label)."),
        inputSchema={
            "type": "object",
            "properties": {
                "label": {"type": "string", "default": ""},
                "limit": {"type": "integer", "default": 50},
            },
        },
    ),
    Tool(
        name="mail_report",
        description=("Send the final test report as mail through the Odysseus "
                     "mail function (/api/email/send). The report body is "
                     "markdown; the recipient defaults to REPORT_MAIL_TO "
                     "(env or odysseus credentials.env). An SMTP-capable "
                     "Email Account must be configured in Odysseus."),
        inputSchema={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "recipient address (defaults to REPORT_MAIL_TO)"},
                "subject": {"type": "string", "description": "mail subject (defaults to a SUT test report title)"},
                "body": {"type": "string", "description": "markdown report to deliver as mail"},
            },
            "required": ["body"],
        },
    ),
]

RESOURCES = [
    Resource(
        uri="testing://process",
        name="Testing process",
        description="Live markdown of tasks, agent state and findings.",
        mimeType="text/markdown",
    ),
    Resource(
        uri="testing://tasks",
        name="Test-case tasks",
        description="Assigned test cases per role.",
        mimeType="text/markdown",
    ),
    Resource(
        uri="testing://findings/e2e",
        name="e2e findings",
        mimeType="text/markdown",
    ),
    Resource(
        uri="testing://findings/pentester",
        name="pentester findings",
        mimeType="text/markdown",
    ),
    Resource(
        uri="testing://findings/manager",
        name="manager findings",
        mimeType="text/markdown",
    ),
]


# --- MCP server handlers -----------------------------------------------------

@server.list_tools()
async def list_tools() -> ListToolsResult:
    return ListToolsResult(tools=TOOLS)


@server.list_resources()
async def list_resources() -> ListResourcesResult:
    return ListResourcesResult(resources=RESOURCES)


@server.read_resource()
async def read_resource(uri: str) -> ReadResourceResult:
    result = await _rpc("resources/read", {"uri": uri})
    content = result.get("content", "")
    return ReadResourceResult(contents=[TextContent(type="text", text=content, uri=uri)])


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    result = await _rpc("tools/call", {"name": name, "arguments": arguments})
    # The bus returns {"content": [{"type": "text", "text": "..."}], "isError": bool}
    content = result.get("content", [])
    text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
    is_error = result.get("isError", False)
    return CallToolResult(
        content=[TextContent(type="text", text="\n".join(text_parts))],
        isError=is_error,
    )


async def main() -> int:
    """Run the server over stdio."""
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    except Exception as e:
        print(f"aigents_bus_server error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))