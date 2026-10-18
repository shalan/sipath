# Third-party code

Run by the `macros` step's `cdlns` method in the macro-placer Python (with
PyTorch; `python3 -m sipath setup-placer`), never imported by SiPath itself.

| directory | source | licence |
|---|---|---|
| `macropolo/cdlns_placer.py` | [ronitamitra/mpc26-team-marcopolo](https://github.com/ronitamitra/mpc26-team-marcopolo) commit 72070c2, `submissions/cd_lns/placer.py`, unchanged: Team Macro Polo's coordinate-descent + LNS + annealing macro placer, 8th (avg proxy 0.9965, verified) in the Partcl/HRT Macro Placement Challenge 2026, the best-ranked submission that runs on a CPU | Apache-2.0 (`macropolo/LICENSE`) |
| `macro_place/benchmark.py` | [partcleda/macro-place-challenge-2026](https://github.com/partcleda/macro-place-challenge-2026) commit 996193c, unchanged: the challenge's `Benchmark` (the placer's input) | Apache-2.0 (`macro_place/LICENSE.md`) |

`run_cdlns.py` (SiPath's) builds a `Benchmark` from the floorplan SiPath
exports and writes the placer's macro centres back.
