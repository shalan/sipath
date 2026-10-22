"""An agent that drives SiPath through its MCP server (Claude Agent SDK).

    python3 -m pip install claude-agent-sdk
    export ANTHROPIC_API_KEY=...
    python3 -m sipath.examples.agent_sdk                       # the default goal on gcd8
    python3 -m sipath.examples.agent_sdk "your goal" --model claude-opus-5-5 --max-turns 40

The agent gets SiPath's MCP tools and nothing else (no shell, no file access):
it opens a run, runs steps, reads attempts, heatmaps and slacks, and decides
the next step itself. Every tool call and its answer are printed; the run stays
under runs/ like any other (graph.json, report).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GOAL = ("Open a run for the gcd8 example and take it through placement. Sweep placement density 0.5, 0.65 "
        "and 0.8 in parallel. For each attempt compare the setup slack and the placement-density and RUDY "
        "heatmaps (peak, hot share, worst hotspot). Say which attempt you would keep and why, in a short "
        "table and three sentences. Do not go past placement.")

SYSTEM = ("You drive the SiPath physical-design flow through its MCP tools only. Steps run in order "
          "(synth, floorplan, physical, place, ...), each from the previous step's selected attempt. Use "
          "describe_step to learn a step's parameters before sweeping them. Base every claim on numbers the "
          "tools returned; if a tool fails, say so and stop rather than guess.")

# the agent may only use SiPath's tools: the SDK's own tools are removed
BUILTIN = ["Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "WebFetch", "WebSearch",
           "NotebookEdit", "Task", "TodoWrite"]


def _short(value, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + f" ... ({len(text)} chars)"


async def run(goal: str, model: str, max_turns: int) -> int:
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage, SystemMessage, TextBlock,
                                  ToolResultBlock, ToolUseBlock, UserMessage, query)
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM,
        model=model,
        mcp_servers={"sipath": {"type": "stdio", "command": sys.executable, "args": ["-m", "sipath", "mcp"],
                                "env": {"PYTHONPATH": ROOT}}},
        strict_mcp_config=True,  # only the server above: none from your Claude Code settings or plugins
        setting_sources=[],  # nor their permissions, hooks or CLAUDE.md
        allowed_tools=["mcp__sipath__*"],
        disallowed_tools=BUILTIN,
        permission_mode="dontAsk",  # anything not allowed above is refused, never asked
        max_turns=max_turns,
        cwd=ROOT,
        env={"MCP_TIMEOUT": "60000"},
    )
    rc = 1
    async for m in query(prompt=goal, options=options):
        if isinstance(m, SystemMessage) and m.subtype == "init":
            servers = m.data.get("mcp_servers", [])
            print("[mcp] " + ", ".join(f"{s.get('name')}: {s.get('status')}" for s in servers))
            if not any(s.get("name") == "sipath" and s.get("status") == "connected" for s in servers):
                print("the sipath MCP server did not start", file=sys.stderr)
                return 1
        elif isinstance(m, AssistantMessage):
            for b in m.content:
                if isinstance(b, TextBlock):
                    print(b.text)
                elif isinstance(b, ToolUseBlock):
                    print(f"[tool] {b.name.removeprefix('mcp__sipath__')} {_short(b.input, 300)}")
        elif isinstance(m, UserMessage) and isinstance(m.content, list):
            for b in m.content:
                if isinstance(b, ToolResultBlock):
                    body = b.content if isinstance(b.content, str) else \
                        " ".join(x.get("text", "") for x in (b.content or []) if isinstance(x, dict))
                    print(f"[{'error' if b.is_error else 'result'}] {_short(body)}")
        elif isinstance(m, ResultMessage):
            cost = f", ${m.total_cost_usd:.2f}" if m.total_cost_usd is not None else ""
            print(f"[done] {m.subtype}: {m.num_turns} turns, {m.duration_ms / 1000:.0f} s{cost}")
            rc = 0 if m.subtype == "success" and not m.is_error else 1
    return rc


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("goal", nargs="?", default=GOAL)
    p.add_argument("--model", default="claude-sonnet-5-5")
    p.add_argument("--max-turns", type=int, default=30)
    a = p.parse_args(argv)
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        print("needs the Claude Agent SDK: python3 -m pip install claude-agent-sdk", file=sys.stderr)
        return 2
    return asyncio.run(run(a.goal, a.model, a.max_turns))


if __name__ == "__main__":
    sys.exit(main())
