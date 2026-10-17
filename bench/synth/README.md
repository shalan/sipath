# Synthesis benchmark

Sixteen designs with their SDCs from [shalan/synth_flow](https://github.com/shalan/synth_flow)
(commit 8e6e5b7, Apache-2.0, Mohamed Shalan): twelve in-house designs under
`designs/` (copied with their constraints) and four external IPs fetched at
pinned commits by `fetch_external.sh` into `external/` (not committed).
`manifest.yaml` lists files, tops, clocks and periods; paths are relative to
this directory.

```bash
bench/synth/fetch_external.sh                    # once: zxip, zx16, ms_psram_ahb, uart_apb_master
python3 bench/synth_bench.py                     # every design, default synth parameters
python3 bench/synth_bench.py alu32 --set abc_sweep=delay --tag delayset
python3 bench/synth_bench.py --frontend-sweep --tag fe   # plus Booth / prefix-adder front ends
```

Results go to `runs/bench_synth/<tag>.csv` and `.md`, next to synth_flow's
best pre-repair result per design (`reference_synth_flow.csv`, from its
`baseline-noD` run: same slow-corner Liberty, OpenSTA with wire load, no ABC
delay target, no post-synthesis repairs).
