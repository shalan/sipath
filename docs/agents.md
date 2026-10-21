# Agents (MCP)

Part of the [SiPath documentation](../README.md#documentation).

`python3 -m sipath mcp` serves `open_run` (an example name or any design file;
`resume` picks up a workdir), `run_step`, `run_eco`, `job_status`,
`cancel_job`, `list_jobs`, `list_attempts`, `get_outputs`, `describe_step`,
`query_checkpoint`, `query_heatmap`, `adapt_step`, `draw_checkpoint` and `render_checkpoint` (plus
`list_designs`, `list_scores`).
- `run_step` / `run_eco` are jobs: in the foreground they send
  `notifications/progress` as attempts start and end (with a
  `progressToken`); with `background: true` they return a job id at once.
- `cancel_job`, or `notifications/cancelled`, kills the job's OpenROAD
  processes; its attempts end `cancelled` with no checkpoint.
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
  irdrop; see [heatmaps](flow.md#heatmaps)): unit, peak, p95, hot share, the
  hotspots with the instances or nets inside, and the bins (at most 2500;
  give a box for a region of a larger map).
- `adapt_step {"run_id": R, "step": "place", "rounds": 2, "options": {"max_density": 60}}`: adaptive rounds on
  a step that already ran (see [adaptive rounds](flow.md#adaptive-rounds)): the selected attempt's hotspots
  become new attempts of the step, which selects again; returns the rounds, why they stopped, and the
  attempts (`adapted_from`, `adapt_reason`).
