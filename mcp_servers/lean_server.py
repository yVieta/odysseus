#!/usr/bin/env python3
"""
lean_server.py

MCP server (stdio transport) exposing Lean 4 / Lake as tools for
formal verification, theorem proving, and code generation.

Tools:
- lean_run: Execute Lean 4 code (file or snippet) and return output/errors
- lake_build: Build a Lake project
- lake_test: Run Lake tests
- lean_check: Type-check a Lean file without running
- lean_search: Search Mathlib/Lean docs (via local cache or web)

This enables agents to use Lean 4 as a formal verification harness
for testing and specification.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

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

server = Server("lean4")

# --- Configuration -----------------------------------------------------------

LEAN_EXEC = os.environ.get("LEAN_EXEC", "lean")
LAKE_EXEC = os.environ.get("LAKE_EXEC", "lake")
WORKSPACE_ROOT = os.environ.get("LEAN_WORKSPACE", "/workspace/lean")
DEFAULT_TIMEOUT = float(os.environ.get("LEAN_TIMEOUT", "120.0"))

# Ensure workspace exists
Path(WORKSPACE_ROOT).mkdir(parents=True, exist_ok=True)


# --- Tool definitions --------------------------------------------------------

TOOLS = [
    Tool(
        name="lean_run",
        description=(
            "Execute Lean 4 code and return the output. Provide either a `file` "
            "path (relative to workspace) or inline `code`. Useful for running "
            "proofs, checking definitions, or evaluating expressions."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Lean 4 code snippet to execute (wrapped in a minimal file)",
                },
                "file": {
                    "type": "string",
                    "description": "Path to a .lean file in the workspace to run",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Additional command-line arguments for lean",
                    "default": [],
                },
            },
        },
    ),
    Tool(
        name="lean_check",
        description=(
            "Type-check a Lean 4 file without executing. Returns errors/warnings. "
            "Useful for validating proofs and definitions."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "Path to a .lean file in the workspace to check",
                },
                "code": {
                    "type": "string",
                    "description": "Lean 4 code snippet to check (wrapped in a minimal file)",
                },
            },
        },
    ),
    Tool(
        name="lake_build",
        description=(
            "Build the Lake project in the workspace. Runs `lake build` and "
            "returns the output. Useful for compiling Lean projects and checking "
            "for build errors."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Specific target to build (optional)",
                    "default": "",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Additional arguments for lake build",
                    "default": [],
                },
            },
        },
    ),
    Tool(
        name="lake_test",
        description=(
            "Run the Lake project tests. Runs `lake test` and returns output. "
            "Useful for running Lean test suites as a verification harness."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Additional arguments for lake test",
                    "default": [],
                },
            },
        },
    ),
    Tool(
        name="lake_init",
        description=(
            "Initialize a new Lake project in a subdirectory of the workspace. "
            "Creates a basic `lakefile.lean` and `Lean4` package structure."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Project name (directory will be created under workspace)",
                },
                "template": {
                    "type": "string",
                    "enum": ["default", "mathlib", "lean4"],
                    "description": "Project template",
                    "default": "default",
                },
            },
            "required": ["name"],
        },
    ),
]

RESOURCES = [
    Resource(
        uri="lean://workspace",
        name="Lean Workspace",
        description="Root of the Lean workspace directory",
        mimeType="text/plain",
    ),
    Resource(
        uri="lean://lakefile",
        name="Lake Configuration",
        description="The lakefile.lean for the current project",
        mimeType="text/plain",
    ),
]


# --- Helper functions --------------------------------------------------------

async def _run_cmd(cmd: list[str], cwd: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"Command timed out after {timeout}s"


def _wrap_code(code: str) -> str:
    """Wrap a code snippet in a minimal Lean file with IO.main."""
    # If code already has a main or is a full file, use as-is
    if "def main" in code or code.strip().startswith("#"):
        return code
    # Otherwise wrap in a minimal executable
    return f"""def main : IO Unit := do
  {textwrap.indent(code, "  ")}
  pure ()
"""


async def _write_temp_file(code: str, suffix: str = ".lean") -> tuple[str, str]:
    """Write code to a temp file in workspace, return (filename, full_path)."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, dir=WORKSPACE_ROOT, delete=False) as f:
        f.write(code)
        return os.path.basename(f.name), f.name


# --- MCP tool handlers -------------------------------------------------------

@server.list_tools()
async def list_tools() -> ListToolsResult:
    return ListToolsResult(tools=TOOLS)


@server.list_resources()
async def list_resources() -> ListResourcesResult:
    return ListResourcesResult(resources=RESOURCES)


@server.read_resource()
async def read_resource(uri: str) -> ReadResourceResult:
    if uri == "lean://workspace":
        files = []
        for p in Path(WORKSPACE_ROOT).rglob("*.lean"):
            try:
                rel = p.relative_to(WORKSPACE_ROOT)
                content = p.read_text(encoding="utf-8")
                files.append(f"=== {rel} ===\n{content}\n")
            except Exception:
                pass
        return ReadResourceResult(contents=[TextContent(type="text", text="\n".join(files) or "(empty)", uri=uri)])
    elif uri == "lean://lakefile":
        lakefile = Path(WORKSPACE_ROOT) / "lakefile.lean"
        if lakefile.exists():
            content = lakefile.read_text(encoding="utf-8")
        else:
            content = "(no lakefile.lean found)"
        return ReadResourceResult(contents=[TextContent(type="text", text=content, uri=uri)])
    else:
        return ReadResourceResult(contents=[TextContent(type="text", text=f"Unknown resource: {uri}", uri=uri)])


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    try:
        if name == "lean_run":
            return await _handle_lean_run(arguments)
        elif name == "lean_check":
            return await _handle_lean_check(arguments)
        elif name == "lake_build":
            return await _handle_lake_build(arguments)
        elif name == "lake_test":
            return await _handle_lake_test(arguments)
        elif name == "lake_init":
            return await _handle_lake_init(arguments)
        else:
            return CallToolResult(
                content=[TextContent(type="text", text=f"Unknown tool: {name}")],
                isError=True,
            )
    except Exception as e:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Error: {type(e).__name__}: {e}")],
            isError=True,
        )


async def _handle_lean_run(args: dict) -> CallToolResult:
    code = args.get("code", "")
    file_path = args.get("file", "")
    extra_args = args.get("args", [])

    if file_path:
        full_path = Path(WORKSPACE_ROOT) / file_path
        if not full_path.exists():
            return CallToolResult(content=[TextContent(type="text", text=f"File not found: {file_path}")], isError=True)
        cmd = [LEAN_EXEC] + extra_args + [str(full_path)]
        rc, out, err = await _run_cmd(cmd, WORKSPACE_ROOT)
    elif code:
        # Write to temp file and run
        filename, full_path = await _write_temp_file(_wrap_code(code))
        cmd = [LEAN_EXEC] + extra_args + [full_path]
        rc, out, err = await _run_cmd(cmd, WORKSPACE_ROOT)
        # Clean up temp file
        try:
            os.unlink(full_path)
        except Exception:
            pass
    else:
        return CallToolResult(content=[TextContent(type="text", text="Provide either 'code' or 'file'")], isError=True)

    output = f"Exit code: {rc}\n"
    if out:
        output += f"STDOUT:\n{out}\n"
    if err:
        output += f"STDERR:\n{err}\n"
    return CallToolResult(content=[TextContent(type="text", text=output)], isError=rc != 0)


async def _handle_lean_check(args: dict) -> CallToolResult:
    file_path = args.get("file", "")
    code = args.get("code", "")

    if file_path:
        full_path = Path(WORKSPACE_ROOT) / file_path
        if not full_path.exists():
            return CallToolResult(content=[TextContent(type="text", text=f"File not found: {file_path}")], isError=True)
        cmd = [LEAN_EXEC, "--check", str(full_path)]
        rc, out, err = await _run_cmd(cmd, WORKSPACE_ROOT)
    elif code:
        filename, full_path = await _write_temp_file(code)
        cmd = [LEAN_EXEC, "--check", full_path]
        rc, out, err = await _run_cmd(cmd, WORKSPACE_ROOT)
        try:
            os.unlink(full_path)
        except Exception:
            pass
    else:
        return CallToolResult(content=[TextContent(type="text", text="Provide either 'code' or 'file'")], isError=True)

    output = f"Type-check exit code: {rc}\n"
    if out:
        output += f"STDOUT:\n{out}\n"
    if err:
        output += f"STDERR:\n{err}\n"
    return CallToolResult(content=[TextContent(type="text", text=output)], isError=rc != 0)


async def _handle_lake_build(args: dict) -> CallToolResult:
    target = args.get("target", "")
    extra_args = args.get("args", [])

    cmd = [LAKE_EXEC, "build"] + extra_args
    if target:
        cmd.append(target)

    rc, out, err = await _run_cmd(cmd, WORKSPACE_ROOT)

    output = f"lake build exit code: {rc}\n"
    if out:
        output += f"STDOUT:\n{out}\n"
    if err:
        output += f"STDERR:\n{err}\n"
    return CallToolResult(content=[TextContent(type="text", text=output)], isError=rc != 0)


async def _handle_lake_test(args: dict) -> CallToolResult:
    extra_args = args.get("args", [])

    cmd = [LAKE_EXEC, "test"] + extra_args
    rc, out, err = await _run_cmd(cmd, WORKSPACE_ROOT)

    output = f"lake test exit code: {rc}\n"
    if out:
        output += f"STDOUT:\n{out}\n"
    if err:
        output += f"STDERR:\n{err}\n"
    return CallToolResult(content=[TextContent(type="text", text=output)], isError=rc != 0)


async def _handle_lake_init(args: dict) -> CallToolResult:
    name = args.get("name", "")
    template = args.get("template", "default")

    if not name:
        return CallToolResult(content=[TextContent(type="text", text="Project name required")], isError=True)

    project_dir = Path(WORKSPACE_ROOT) / name
    if project_dir.exists():
        return CallToolResult(content=[TextContent(type="text", text=f"Directory already exists: {project_dir}")], isError=True)

    # Run lake init
    cmd = [LAKE_EXEC, "init", name]
    rc, out, err = await _run_cmd(cmd, WORKSPACE_ROOT)

    output = f"lake init exit code: {rc}\n"
    if out:
        output += f"STDOUT:\n{out}\n"
    if err:
        output += f"STDERR:\n{err}\n"

    if rc == 0:
        # Show the created structure
        files = []
        for p in project_dir.rglob("*"):
            if p.is_file():
                rel = p.relative_to(WORKSPACE_ROOT)
                files.append(f"  {rel}")
        output += "\nCreated files:\n" + "\n".join(files)

    return CallToolResult(content=[TextContent(type="text", text=output)], isError=rc != 0)


async def main() -> int:
    """Run the server over stdio."""
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    except Exception as e:
        print(f"lean_server error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))