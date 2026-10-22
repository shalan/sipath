# Agents (MCP)

Part of the [SiPath documentation](../README.md#documentation).

## With Claude Code

The repository's `.mcp.json` registers the server as `sipath`. Open the
repository in Claude Code; it asks once whether to use the project's MCP
server (`/mcp` shows it). The tools then appear as `mcp__sipath__<tool>`;
Claude Code asks before each call unless you allow them, for example with
`"permissions": {"allow": ["mcp__sipath__*"]}` in your `.claude/settings.json`
or `.claude/settings.local.json`. Runs go under `runs/` of the repository.
Elsewhere (another project, Claude Desktop) register the same command:

```bash
claude mcp add --transport stdio --scope project sipath -- python3 -m sipath mcp
```

Then ask in plain words; the agent picks the tools. For example:

```text
Run gcd8 to signoff with sipath and summarise the signoff checks and the
slowest steps.

On gcd8, sweep placement density 0.45, 0.6 and 0.75 in parallel. Compare the
placement-density and RUDY heatmaps and the setup slack of the three, explain
which you keep, and continue the flow to signoff from it.

Open a run for sipath/examples/dp8.yaml and take it through routing. Query
the routing heatmap of the routed attempt: where is the worst hotspot, and
which nets cross it?

Make gcd8 crowded (core_utilization 0.65, placement density 0.8) and run it
to placement. If the placement heatmap has hotspots, run two adaptive rounds
on place and tell me what they changed and what it cost in slack.
```

Tried with Claude Code 2.1.301, non-interactively (`claude -p "..."
--allowedTools "mcp__sipath__*"`, the built-in tools disallowed): it loaded the
project server, listed the designs, ran gcd8's synth, floorplan, physical and
place each from the previous selected attempt, and reported the placement
heatmap and the setup slack: 10 turns, about a minute, $0.40.

Long steps (aes signoff takes most of an hour) are better run with
`background: true` and followed with `job_status`; Claude Code moves a tool
call still running after two minutes to the background on its own.

## From a script (Claude Agent SDK)

[`sipath/examples/agent_sdk.py`](../sipath/examples/agent_sdk.py) runs an agent
on a goal with the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview).
It gives the model SiPath's MCP tools and nothing else: no shell, no file
access, and none of your own Claude Code settings, servers or plugins. It
prints every tool call and answer.

```bash
python3 -m pip install claude-agent-sdk
export ANTHROPIC_API_KEY=...
python3 -m sipath.examples.agent_sdk
python3 -m sipath.examples.agent_sdk "Take dp8 through routing and explain the worst routing hotspot" --max-turns 40
```

The default goal takes gcd8 through placement, sweeps placement density 0.5,
0.65 and 0.8 in parallel, compares slack and the placement-density and RUDY
heatmaps, and says which attempt to keep. Tried with SDK 0.2.171 and
`claude-sonnet-5-5`: it ran synth, floorplan, physical and the three-way
place sweep, queried the six heatmaps and answered with a table (15 turns,
36 s, $0.09). It kept density 0.8 (setup slack 4.590 ns against 4.506 and
4.510; HPWL 2514 µm against 2775 and 2653; no hotspot in any map); every
number matched the run's `graph.json`. Told to stop when a tool fails, it
did: an earlier try hit a mount bug (a new `runs/mcp/` was not mounted into
the container, now fixed) and it reported the error instead of working around
it.

## The tools

`python3 -m sipath mcp` serves `open_run` (an example name or any design file;
`resume` picks up a workdir), `fork_run` (a new run from any finished attempt), `run_step`, `run_eco`, `job_status`,
`cancel_job`, `list_jobs`, `list_attempts`, `get_outputs`, `describe_step`,
`query_checkpoint`, `query_heatmap`, `adapt_step`, `draw_checkpoint` and `render_checkpoint` (plus
`list_designs`, `list_scores`).
- `run_step` / `run_eco` are jobs: in the foreground they send
  `notifications/progress` as attempts start and end (with a
  `progressToken`); with `background: true` they return a job id at once.
- `cancel_job`, or `notifications/cancelled`, kills the job's OpenROAD
  processes and its tool processes (Yosys, KLayout, Magic, the precheck, the
  macro placer), with their whole process trees and containers; its attempts
  end `cancelled` with no checkpoint.
- Arguments are checked before anything runs (step, score, parallel,
  parameter names per step, ECO ops): a bad one is a tool error.
- Resources: each run's `graph.json` and every attempt's DEF, SPEF (per
  corner), reports, debug checkpoint and pictures, as
  `flow://<run>/<attempt>/<name>`.
- `query_checkpoint` runs Tcl in a fresh process with `write_*`, `exec`,
  `file`, `source`, `socket` removed and `open` limited to the session's own
  result files: a guard against accidental writes, not a security sandbox.
- `query_heatmap {"run_id": R, "attempt": "route_digital.0", "map": "routing", "box": [x0, y0, x1, y1]}`:
  a heatmap the attempt collected (placement, rudy, routing, pin, power,
  irdrop; see [heatmaps](flow.md#heatmaps)): unit, peak, p95, hot share and
  the hotspots with the instances or nets inside; the bins only with a box
  (those overlapping it) or `bins: true` (at most 2500).
- `adapt_step {"run_id": R, "step": "place", "rounds": 2, "options": {"max_density": 60}}`: adaptive rounds on
  a step that already ran (see [adaptive rounds](flow.md#adaptive-rounds)): the selected attempt's hotspots
  become new attempts of the step, which selects again; returns the rounds, why they stopped, and the
  attempts (`adapted_from`, `adapt_reason`).
