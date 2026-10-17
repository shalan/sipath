# ABC recipes

Scripts for the synth step's ABC mapping (`abc:` parameter). `{D}` stands for
the delay target: it becomes `-D <ps>` with `abc_target: period` (or a number of
ps) and is removed with `abc_target: none`, the default.

Eighteen recipes come from [shalan/synth_flow](https://github.com/shalan/synth_flow)
(commit 8e6e5b7, Apache-2.0, Mohamed Shalan), unchanged, with their comments:
pruned and extended there against a 16-design STA benchmark (OpenSTA at the
slow corner with the Liberty wire-load model). `area_map` and `speed_sizing`
are SiPath's.

| objective | recipes (synth_flow's top 5 per objective) |
|---|---|
| delay | delay_map_resyn, delay_map, orfs_area, delay_choice_deep_v3, delay_syn2 |
| area | delay_aggressive, area_lut6, area_max, yosys_default, area_classic |
| balanced | balanced_resyn, balanced_resyn2x, delay_triple, delay_iter_heavy, delay_map_resyn |

A new recipe is a `<name>.abc` file here: ABC commands separated by `;`, `#`
comment lines.
