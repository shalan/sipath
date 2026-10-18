"""
CD+LNS Placer — Coordinate Descent with Large Neighborhood Search.

Algorithm:
  1. Start from initial.plc positions; legalize via greedy displacement.
  2. Build incremental HPWL surrogate (per-net bbox cache, O(degree*pins)
     update per macro move).
  3. Coordinate descent: visit each movable hard macro in random order,
     compute HPWL-optimal target (centroid of net-bbox midpoints minus pin
     offsets), snap to nearest legal position, accept if HPWL drops.
  4. SA-style perturbation phase: random shifts/swaps with cooling
     temperature on the HPWL surrogate.
  5. Soft-macro reoptimization via PlacementCost.optimize_stdcells (the
     same FD-with-repulsion the SA baseline uses).
"""

from __future__ import annotations

import math
import random
import time
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import torch

from macro_place.benchmark import Benchmark


def _load_plc_for(benchmark: Benchmark):
    """Reconstruct PlacementCost for a benchmark by name (the placer API
    only hands us the Benchmark; we need plc for soft-macro FD)."""
    from macro_place.loader import load_benchmark, load_benchmark_from_dir

    name = benchmark.name
    iccad = Path("external/MacroPlacement/Testcases/ICCAD04") / name
    if iccad.exists():
        _, plc = load_benchmark_from_dir(str(iccad))
        return plc
    ng45 = {
        "ariane133_ng45": "ariane133",
        "ariane136_ng45": "ariane136",
        "nvdla_ng45": "nvdla",
        "mempool_tile_ng45": "mempool_tile",
        "ariane133": "ariane133",
        "ariane136": "ariane136",
        "nvdla": "nvdla",
        "mempool_tile": "mempool_tile",
    }
    d = ng45.get(name)
    if d:
        base = Path("external/MacroPlacement/Flows/NanGate45") / d / "netlist" / "output_CT_Grouping"
        if (base / "netlist.pb.txt").exists():
            _, plc = load_benchmark(str(base / "netlist.pb.txt"), str(base / "initial.plc"))
            return plc
    return None


# ── Helpers ─────────────────────────────────────────────────────────────────


def _legalize_greedy(
    pos: np.ndarray,
    movable: np.ndarray,
    sizes: np.ndarray,
    hw: np.ndarray,
    hh: np.ndarray,
    cw: float,
    ch: float,
    nh: int,
    gap: float = 0.05,
) -> np.ndarray:
    """Largest-first legalization with spiral search to nearest legal slot."""
    pos = pos.copy()
    order = sorted(range(nh), key=lambda i: -float(sizes[i, 0] * sizes[i, 1]))
    placed = np.zeros(nh, dtype=bool)

    for idx in order:
        cx0 = float(np.clip(pos[idx, 0], hw[idx], cw - hw[idx]))
        cy0 = float(np.clip(pos[idx, 1], hh[idx], ch - hh[idx]))
        if not movable[idx]:
            pos[idx, 0] = cx0
            pos[idx, 1] = cy0
            placed[idx] = True
            continue

        def overlaps(x: float, y: float) -> bool:
            if not placed.any():
                return False
            dx = np.abs(x - pos[:nh, 0])
            dy = np.abs(y - pos[:nh, 1])
            sx = (sizes[idx, 0] + sizes[:nh, 0]) / 2.0 + gap
            sy = (sizes[idx, 1] + sizes[:nh, 1]) / 2.0 + gap
            ov = (dx < sx) & (dy < sy) & placed
            ov[idx] = False
            return bool(ov.any())

        if not overlaps(cx0, cy0):
            pos[idx, 0] = cx0
            pos[idx, 1] = cy0
            placed[idx] = True
            continue

        step = max(sizes[idx, 0], sizes[idx, 1]) * 0.25
        best = (cx0, cy0)
        best_d = float("inf")
        for r in range(1, 200):
            found = False
            for dxi in range(-r, r + 1):
                for dyi in range(-r, r + 1):
                    if abs(dxi) != r and abs(dyi) != r:
                        continue
                    cx = float(np.clip(cx0 + dxi * step, hw[idx], cw - hw[idx]))
                    cy = float(np.clip(cy0 + dyi * step, hh[idx], ch - hh[idx]))
                    if not overlaps(cx, cy):
                        d = (cx - cx0) ** 2 + (cy - cy0) ** 2
                        if d < best_d:
                            best_d = d
                            best = (cx, cy)
                            found = True
            if found:
                break
        pos[idx, 0] = best[0]
        pos[idx, 1] = best[1]
        placed[idx] = True

    return pos


# ── Placer ──────────────────────────────────────────────────────────────────


class CDLNSPlacer:
    """Coordinate Descent + LNS placer."""

    def __init__(
        self,
        seed: int = 42,
        max_seconds: float = 300.0,
        cd_passes: int = 60,
        sa_iters: int = 5_000_000,
        sa_initial_temp_frac: float = 0.15,
        sa_final_temp_frac: float = 0.0005,
        soft_opt_steps: Tuple[int, int, int] = (0, 0, 0),
        density_lambda: float = 30.0,
        congestion_lambda: float = 0.0,
        lns_iters: int = 600,
        lns_cluster_size: int = 5,
        true_proxy_check_every: float = 12.0,
        hill_climb_seconds: float = 0.0,
        soft_fd_steps: int = 0,  # opt-in; piles up density when on by default
        soft_fd_lr: float = 0.04,
        soft_fd_repel_k: float = 8.0,
        soft_fd_mode: str = "density",  # "density" (gradient) or "centroid" (attract+repel)
        soft_fd_density_lr: float = 0.05,
        soft_fd_attract_lr: float = 0.08,
        smooth_cong: bool = True,
        smooth_cong_kernel: int = 5,
        gap: float = 0.05,
        incremental_init: bool = False,
        incremental_batch: int = 10,
        penalty_ramp: bool = False,
        ramp_steps: int = 20,
        ramp_lam_o_lo: float = 0.01,
        ramp_lam_o_hi: float = 1000.0,
        initial_perturbation: float = 0.0,
        annealed_cd: bool = False,
        verbose: bool = True,
    ) -> None:
        self.seed = seed
        self.max_seconds = max_seconds
        self.cd_passes = cd_passes
        self.sa_iters = sa_iters
        self.sa_initial_temp_frac = sa_initial_temp_frac
        self.sa_final_temp_frac = sa_final_temp_frac
        self.soft_opt_steps = soft_opt_steps
        self.density_lambda = density_lambda
        self.congestion_lambda = congestion_lambda
        self.lns_iters = lns_iters
        self.lns_cluster_size = lns_cluster_size
        self.true_proxy_check_every = true_proxy_check_every
        self.hill_climb_seconds = hill_climb_seconds
        self.soft_fd_steps = soft_fd_steps
        self.soft_fd_lr = soft_fd_lr
        self.soft_fd_repel_k = soft_fd_repel_k
        self.soft_fd_mode = soft_fd_mode
        self.soft_fd_density_lr = soft_fd_density_lr
        self.soft_fd_attract_lr = soft_fd_attract_lr
        self.smooth_cong = smooth_cong
        self.smooth_cong_kernel = smooth_cong_kernel
        self.gap = gap
        self.incremental_init = incremental_init
        self.incremental_batch = incremental_batch
        self.penalty_ramp = penalty_ramp
        self.ramp_steps = ramp_steps
        self.ramp_lam_o_lo = ramp_lam_o_lo
        self.ramp_lam_o_hi = ramp_lam_o_hi
        self.initial_perturbation = initial_perturbation
        self.annealed_cd = annealed_cd
        self.verbose = verbose

    # ------------------------------------------------------------------ place
    def place(self, benchmark: Benchmark) -> torch.Tensor:
        random.seed(self.seed)
        np.random.seed(self.seed)
        torch.manual_seed(self.seed)

        t0 = time.time()
        n = benchmark.num_macros
        nh = benchmark.num_hard_macros
        cw = float(benchmark.canvas_width)
        ch = float(benchmark.canvas_height)

        pos = benchmark.macro_positions.numpy().astype(np.float64).copy()
        sizes = benchmark.macro_sizes.numpy().astype(np.float64)
        movable = benchmark.get_movable_mask().numpy()
        hw = sizes[:, 0] / 2.0
        hh = sizes[:, 1] / 2.0

        # ── Build per-net pin structure ──────────────────────────────────
        port_pos = (
            benchmark.port_positions.numpy().astype(np.float64)
            if benchmark.port_positions.shape[0] > 0
            else np.zeros((0, 2))
        )
        offsets_per_macro: List[np.ndarray] = []
        for i in range(nh):
            o = (
                benchmark.macro_pin_offsets[i].numpy().astype(np.float64)
                if i < len(benchmark.macro_pin_offsets)
                else np.zeros((0, 2))
            )
            offsets_per_macro.append(o)

        n_nets = len(benchmark.net_pin_nodes)
        net_owners: List[np.ndarray] = []
        net_ox: List[np.ndarray] = []
        net_oy: List[np.ndarray] = []
        net_is_port: List[np.ndarray] = []
        net_size = np.zeros(n_nets, dtype=np.int32)

        for nid in range(n_nets):
            entries = benchmark.net_pin_nodes[nid].numpy()
            owners = entries[:, 0].astype(np.int64)
            slots = entries[:, 1].astype(np.int64)
            k_pins = len(owners)
            ox = np.zeros(k_pins)
            oy = np.zeros(k_pins)
            is_port = np.zeros(k_pins, dtype=bool)
            for k in range(k_pins):
                o = int(owners[k])
                s = int(slots[k])
                if o < nh:
                    om = offsets_per_macro[o]
                    if om.shape[0] > s:
                        ox[k] = om[s, 0]
                        oy[k] = om[s, 1]
                elif o < n:
                    pass  # soft macro: pin at center
                else:
                    pidx = o - n
                    is_port[k] = True
                    ox[k] = port_pos[pidx, 0]
                    oy[k] = port_pos[pidx, 1]
            net_owners.append(owners)
            net_ox.append(ox)
            net_oy.append(oy)
            net_is_port.append(is_port)
            net_size[nid] = k_pins

        # macro -> [(net_idx, pin_idx_in_net)] (only hard macros get optimized)
        m_pins: List[List[Tuple[int, int]]] = [[] for _ in range(n)]
        for nid in range(n_nets):
            owners = net_owners[nid]
            isp = net_is_port[nid]
            for k in range(len(owners)):
                if not isp[k]:
                    o = int(owners[k])
                    if o < n:
                        m_pins[o].append((nid, k))

        # Initial pin positions and net bboxes
        pin_x: List[np.ndarray] = [None] * n_nets  # type: ignore[list-item]
        pin_y: List[np.ndarray] = [None] * n_nets  # type: ignore[list-item]
        net_minx = np.zeros(n_nets)
        net_maxx = np.zeros(n_nets)
        net_miny = np.zeros(n_nets)
        net_maxy = np.zeros(n_nets)

        def recompute_pins() -> None:
            for nid in range(n_nets):
                owners = net_owners[nid]
                ox_a = net_ox[nid]
                oy_a = net_oy[nid]
                isp = net_is_port[nid]
                k_pins = len(owners)
                if k_pins == 0:
                    pin_x[nid] = np.zeros(0)
                    pin_y[nid] = np.zeros(0)
                    continue
                px = np.empty(k_pins)
                py = np.empty(k_pins)
                for k in range(k_pins):
                    if isp[k]:
                        px[k] = ox_a[k]
                        py[k] = oy_a[k]
                    else:
                        o = int(owners[k])
                        px[k] = pos[o, 0] + ox_a[k]
                        py[k] = pos[o, 1] + oy_a[k]
                pin_x[nid] = px
                pin_y[nid] = py
                net_minx[nid] = px.min()
                net_maxx[nid] = px.max()
                net_miny[nid] = py.min()
                net_maxy[nid] = py.max()

        # ── Perturb initial placement (optional, for restart diversity) ─
        # Drops movable hard macros to perturbed positions (gaussian noise
        # scaled by initial_perturbation × max canvas dim). _legalize_greedy
        # then snaps each to its nearest legal slot — different perturbations
        # → different post-legalization topologies → different SA basins.
        if self.initial_perturbation > 0.0:
            sigma_p = float(self.initial_perturbation) * max(cw, ch)
            for i in range(nh):
                if not movable[i]:
                    continue
                pos[i, 0] = float(np.clip(pos[i, 0] + np.random.normal(0.0, sigma_p), hw[i], cw - hw[i]))
                pos[i, 1] = float(np.clip(pos[i, 1] + np.random.normal(0.0, sigma_p), hh[i], ch - hh[i]))

        # ── Legalize initial placement ───────────────────────────────────
        pos = _legalize_greedy(pos, movable, sizes, hw, hh, cw, ch, nh, self.gap)
        recompute_pins()

        def hpwl_total() -> float:
            return float((net_maxx - net_minx + net_maxy - net_miny).sum())

        # ── Density grid (incremental) ───────────────────────────────────
        # Grid uses the same row/col counts as the proxy density grid so the
        # surrogate matches the ground-truth metric. Cells store total macro
        # area inside (hard + soft); soft macros are static, hard get updated
        # on every move. Penalty = sum (cell_density)^2 over all cells, where
        # cell_density = grid_total / cell_area.
        gr = int(benchmark.grid_rows)
        gc = int(benchmark.grid_cols)
        cell_w_g = cw / gc
        cell_h_g = ch / gr
        cell_area = cell_w_g * cell_h_g
        grid = np.zeros((gr, gc), dtype=np.float64)

        def cells_for(m: int, x: float, y: float) -> List[Tuple[int, int, float]]:
            """List (row, col, overlap_area) for cells covered by macro m at (x, y)."""
            xmin = x - hw[m]
            xmax = x + hw[m]
            ymin = y - hh[m]
            ymax = y + hh[m]
            c0 = max(0, int(xmin / cell_w_g))
            c1 = min(gc - 1, int(xmax / cell_w_g))
            r0 = max(0, int(ymin / cell_h_g))
            r1 = min(gr - 1, int(ymax / cell_h_g))
            out: List[Tuple[int, int, float]] = []
            for r in range(r0, r1 + 1):
                cy0 = r * cell_h_g
                cy1 = (r + 1) * cell_h_g
                y_ov = min(ymax, cy1) - max(ymin, cy0)
                if y_ov <= 0:
                    continue
                for c in range(c0, c1 + 1):
                    cx0 = c * cell_w_g
                    cx1 = (c + 1) * cell_w_g
                    x_ov = min(xmax, cx1) - max(xmin, cx0)
                    if x_ov <= 0:
                        continue
                    out.append((r, c, x_ov * y_ov))
            return out

        # Initial grid: include both hard and soft macros (soft are static).
        for mi in range(n):
            for r, c, a in cells_for(mi, float(pos[mi, 0]), float(pos[mi, 1])):
                grid[r, c] += a

        # Density penalty value cache.
        density_pen = float(((grid / cell_area) ** 2).sum())
        inv_cell_area_sq = 1.0 / (cell_area * cell_area)

        def density_apply(m: int, new_x: float, new_y: float) -> Tuple[float, List[Tuple[int, int, float]]]:
            """Update grid for moving macro m to (new_x, new_y).
            Returns (delta_penalty, saved_cells) where saved_cells = [(r, c, original_grid_val)].
            """
            old_cells = cells_for(m, float(pos[m, 0]), float(pos[m, 1]))
            new_cells = cells_for(m, new_x, new_y)
            change: dict = {}
            for r, c, a in old_cells:
                key = (r, c)
                if key not in change:
                    change[key] = [0.0, 0.0]
                change[key][0] += a
            for r, c, a in new_cells:
                key = (r, c)
                if key not in change:
                    change[key] = [0.0, 0.0]
                change[key][1] += a
            delta = 0.0
            saved: List[Tuple[int, int, float]] = []
            for (r, c), (a_old, a_new) in change.items():
                g_orig = grid[r, c]
                g_new = g_orig - a_old + a_new
                delta += (g_new * g_new - g_orig * g_orig) * inv_cell_area_sq
                saved.append((r, c, g_orig))
                grid[r, c] = g_new
            return delta, saved

        def density_revert(saved: List[Tuple[int, int, float]]) -> None:
            for r, c, g_orig in saved:
                grid[r, c] = g_orig

        # ── Congestion proxy grid (incremental) ──────────────────────────
        # For each net, track its bbox cell range; the grid counts how many
        # net bboxes cover each cell. Penalty = sum(count^2) — a smooth proxy
        # for "many nets crowding the same routing channels".
        net_r0 = np.zeros(n_nets, dtype=np.int64)
        net_r1 = np.zeros(n_nets, dtype=np.int64)
        net_c0 = np.zeros(n_nets, dtype=np.int64)
        net_c1 = np.zeros(n_nets, dtype=np.int64)
        cong_grid = np.zeros((gr, gc), dtype=np.int64)

        def _net_cells(nid: int) -> Tuple[int, int, int, int]:
            if net_size[nid] == 0:
                return 0, -1, 0, -1
            r0 = max(0, int(net_miny[nid] / cell_h_g))
            r1 = min(gr - 1, int(net_maxy[nid] / cell_h_g))
            c0 = max(0, int(net_minx[nid] / cell_w_g))
            c1 = min(gc - 1, int(net_maxx[nid] / cell_w_g))
            return r0, r1, c0, c1

        for nid in range(n_nets):
            r0, r1, c0, c1 = _net_cells(nid)
            if r1 < r0 or c1 < c0:
                continue
            net_r0[nid] = r0
            net_r1[nid] = r1
            net_c0[nid] = c0
            net_c1[nid] = c1
            cong_grid[r0 : r1 + 1, c0 : c1 + 1] += 1

        cong_pen = float((cong_grid * cong_grid).sum())

        def cong_update_net(
            nid: int,
        ) -> Tuple[float, Optional[Tuple[int, int, int, int, int, List[Tuple[int, int, int]]]]]:
            """Update cong_grid for net nid based on its current bbox.
            Returns (delta_penalty, saved_state) where saved_state lets us revert.
            """
            new_r0, new_r1, new_c0, new_c1 = _net_cells(nid)
            old_r0 = int(net_r0[nid])
            old_r1 = int(net_r1[nid])
            old_c0 = int(net_c0[nid])
            old_c1 = int(net_c1[nid])
            if (
                new_r0 == old_r0
                and new_r1 == old_r1
                and new_c0 == old_c0
                and new_c1 == old_c1
            ):
                return 0.0, None
            saved_cells: List[Tuple[int, int, int]] = []
            delta = 0.0
            # Remove old (cells in old bbox but not new)
            for r in range(old_r0, old_r1 + 1):
                in_new_r = new_r0 <= r <= new_r1
                for c in range(old_c0, old_c1 + 1):
                    if in_new_r and new_c0 <= c <= new_c1:
                        continue
                    cnt_old = int(cong_grid[r, c])
                    cnt_new = cnt_old - 1
                    delta += cnt_new * cnt_new - cnt_old * cnt_old
                    saved_cells.append((r, c, cnt_old))
                    cong_grid[r, c] = cnt_new
            # Add new (cells in new bbox but not old)
            for r in range(new_r0, new_r1 + 1):
                in_old_r = old_r0 <= r <= old_r1
                for c in range(new_c0, new_c1 + 1):
                    if in_old_r and old_c0 <= c <= old_c1:
                        continue
                    cnt_old = int(cong_grid[r, c])
                    cnt_new = cnt_old + 1
                    delta += cnt_new * cnt_new - cnt_old * cnt_old
                    saved_cells.append((r, c, cnt_old))
                    cong_grid[r, c] = cnt_new
            net_r0[nid] = new_r0
            net_r1[nid] = new_r1
            net_c0[nid] = new_c0
            net_c1[nid] = new_c1
            return float(delta), (old_r0, old_r1, old_c0, old_c1, nid, saved_cells)

        def cong_revert(state: Tuple[int, int, int, int, int, List[Tuple[int, int, int]]]) -> None:
            old_r0, old_r1, old_c0, old_c1, nid, saved_cells = state
            net_r0[nid] = old_r0
            net_r1[nid] = old_r1
            net_c0[nid] = old_c0
            net_c1[nid] = old_c1
            for r, c, cnt_old in saved_cells:
                cong_grid[r, c] = cnt_old

        # ── Overlap check (vectorized over hard macros) ──────────────────
        sep_w = (sizes[:nh, 0:1] + sizes[:nh, 0:1].T) / 2.0
        sep_h = (sizes[:nh, 1:2] + sizes[:nh, 1:2].T) / 2.0

        def overlaps_with_others(m: int, cx: float, cy: float) -> bool:
            dx = np.abs(cx - pos[:nh, 0])
            dy = np.abs(cy - pos[:nh, 1])
            sx = sep_w[m] + self.gap
            sy = sep_h[m] + self.gap
            ov = (dx < sx) & (dy < sy)
            ov[m] = False
            return bool(ov.any())

        def calc_overlap_area(m: int, cx: float, cy: float) -> float:
            """Total pairwise overlap area between macro m at (cx,cy) and all other hard macros."""
            dx = np.abs(cx - pos[:nh, 0])
            dy = np.abs(cy - pos[:nh, 1])
            sx = sep_w[m] + self.gap
            sy = sep_h[m] + self.gap
            ov_x = np.maximum(0.0, sx - dx)
            ov_y = np.maximum(0.0, sy - dy)
            ov = ov_x * ov_y
            ov[m] = 0.0
            return float(np.sum(ov))

        # ── Per-macro HPWL-optimal target (centroid heuristic) ───────────
        def compute_target(m: int) -> Tuple[float, float]:
            sx = 0.0
            sy = 0.0
            cnt = 0
            for nid, k in m_pins[m]:
                if net_size[nid] <= 1:
                    continue
                cx = (net_minx[nid] + net_maxx[nid]) / 2.0
                cy = (net_miny[nid] + net_maxy[nid]) / 2.0
                sx += cx - net_ox[nid][k]
                sy += cy - net_oy[nid][k]
                cnt += 1
            if cnt == 0:
                return float(pos[m, 0]), float(pos[m, 1])
            return sx / cnt, sy / cnt

        # ── Apply move: returns (delta_wl, delta_density, delta_cong, state) ──
        track_cong = self.congestion_lambda > 0.0

        eps = 1e-9

        def apply_move(
            m: int, new_x: float, new_y: float
        ):
            old_x = float(pos[m, 0])
            old_y = float(pos[m, 1])
            # Density grid update FIRST (uses current pos[m]).
            d_den, saved_den = density_apply(m, new_x, new_y)
            # Net bbox updates + per-net congestion grid updates.
            saved_net: List[Tuple[float, float, float, float, float, float]] = []
            saved_cong: List[Tuple[int, int, int, int, int, List[Tuple[int, int, int]]]] = []
            d_wl = 0.0
            d_cong = 0.0
            for nid, k in m_pins[m]:
                old_minx = net_minx[nid]
                old_maxx = net_maxx[nid]
                old_miny = net_miny[nid]
                old_maxy = net_maxy[nid]
                old_w = (old_maxx - old_minx) + (old_maxy - old_miny)
                px_arr = pin_x[nid]
                py_arr = pin_y[nid]
                old_px = float(px_arr[k])
                old_py = float(py_arr[k])
                new_px = new_x + net_ox[nid][k]
                new_py = new_y + net_oy[nid][k]
                px_arr[k] = new_px
                py_arr[k] = new_py
                # Fast x bbox update — full recompute only if pin was at boundary.
                if old_px > old_minx + eps and old_px < old_maxx - eps:
                    if new_px < old_minx:
                        new_minx = new_px
                        new_maxx = old_maxx
                    elif new_px > old_maxx:
                        new_minx = old_minx
                        new_maxx = new_px
                    else:
                        new_minx = old_minx
                        new_maxx = old_maxx
                else:
                    new_minx = float(px_arr.min())
                    new_maxx = float(px_arr.max())
                # Fast y bbox update.
                if old_py > old_miny + eps and old_py < old_maxy - eps:
                    if new_py < old_miny:
                        new_miny = new_py
                        new_maxy = old_maxy
                    elif new_py > old_maxy:
                        new_miny = old_miny
                        new_maxy = new_py
                    else:
                        new_miny = old_miny
                        new_maxy = old_maxy
                else:
                    new_miny = float(py_arr.min())
                    new_maxy = float(py_arr.max())
                new_w = (new_maxx - new_minx) + (new_maxy - new_miny)
                d_wl += new_w - old_w
                saved_net.append((old_minx, old_maxx, old_miny, old_maxy, old_px, old_py))
                net_minx[nid] = new_minx
                net_maxx[nid] = new_maxx
                net_miny[nid] = new_miny
                net_maxy[nid] = new_maxy
                if track_cong:
                    d_c, st = cong_update_net(nid)
                    d_cong += d_c
                    if st is not None:
                        saved_cong.append(st)
            pos[m, 0] = new_x
            pos[m, 1] = new_y
            return d_wl, d_den, d_cong, (old_x, old_y, saved_net, saved_den, saved_cong)

        def revert(m: int, state) -> None:
            old_x, old_y, saved_net, saved_den, saved_cong = state
            pos[m, 0] = old_x
            pos[m, 1] = old_y
            for (nid, k), box in zip(m_pins[m], saved_net):
                ominx, omaxx, ominy, omaxy, opx, opy = box
                pin_x[nid][k] = opx
                pin_y[nid][k] = opy
                net_minx[nid] = ominx
                net_maxx[nid] = omaxx
                net_miny[nid] = ominy
                net_maxy[nid] = omaxy
            density_revert(saved_den)
            for st in reversed(saved_cong):
                cong_revert(st)

        # ── Find legal position closest to (tx, ty) ──────────────────────
        def find_legal_near(m: int, tx: float, ty: float, max_rings: int = 80) -> Optional[Tuple[float, float]]:
            tx = float(np.clip(tx, hw[m], cw - hw[m]))
            ty = float(np.clip(ty, hh[m], ch - hh[m]))
            if not overlaps_with_others(m, tx, ty):
                return tx, ty
            step = max(sizes[m, 0], sizes[m, 1]) * 0.25
            for r in range(1, max_rings + 1):
                best: Optional[Tuple[float, float]] = None
                best_d = float("inf")
                for dxi in range(-r, r + 1):
                    for dyi in range(-r, r + 1):
                        if abs(dxi) != r and abs(dyi) != r:
                            continue
                        cx = float(np.clip(tx + dxi * step, hw[m], cw - hw[m]))
                        cy = float(np.clip(ty + dyi * step, hh[m], ch - hh[m]))
                        if not overlaps_with_others(m, cx, cy):
                            d = (cx - tx) ** 2 + (cy - ty) ** 2
                            if d < best_d:
                                best_d = d
                                best = (cx, cy)
                if best is not None:
                    return best
            return None

        # ── Soft-macro FD updater ────────────────────────────────────────
        # Attract each soft macro toward the centroid of its connected nets;
        # repel from nearest neighbors so they don't pile up. Runs at the end
        # of each CD pass to keep soft macros tracking the hard layout.
        soft_movable_idx = np.array(
            [i for i in range(nh, n) if movable[i]], dtype=np.int64
        )
        avg_soft_size_sq = 0.0
        if benchmark.num_soft_macros > 0:
            sw_soft = sizes[nh:n, 0].mean() if n > nh else 1.0
            avg_soft_size_sq = float(sw_soft * sw_soft)
        repel_eps = max(avg_soft_size_sq, 0.04)  # avoid 1/d² blow-ups

        def _soft_fd_step(lr: float) -> int:
            """One FD step over soft macros. Returns number that actually moved."""
            if soft_movable_idx.size == 0:
                return 0
            # ── Attraction: target = centroid of net midpoints (HPWL-style) ──
            tx = np.zeros(soft_movable_idx.size)
            ty = np.zeros(soft_movable_idx.size)
            cnt = np.zeros(soft_movable_idx.size, dtype=np.int32)
            for k_local, sidx in enumerate(soft_movable_idx):
                for nid, k in m_pins[int(sidx)]:
                    if net_size[nid] <= 1:
                        continue
                    cx_n = (net_minx[nid] + net_maxx[nid]) / 2.0
                    cy_n = (net_miny[nid] + net_maxy[nid]) / 2.0
                    tx[k_local] += cx_n - net_ox[nid][k]
                    ty[k_local] += cy_n - net_oy[nid][k]
                    cnt[k_local] += 1
            mask = cnt > 0
            attract_x = np.zeros_like(tx)
            attract_y = np.zeros_like(ty)
            attract_x[mask] = tx[mask] / cnt[mask] - pos[soft_movable_idx[mask], 0]
            attract_y[mask] = ty[mask] / cnt[mask] - pos[soft_movable_idx[mask], 1]

            # ── Repulsion: O(N_soft × N_all) inverse-square with epsilon ──
            # All-pairs vectorized: (S, 2) - (1, N, 2) → (S, N, 2)
            soft_pos = pos[soft_movable_idx]  # (S, 2)
            all_pos = pos[:n]  # (N, 2)
            dxy = soft_pos[:, None, :] - all_pos[None, :, :]  # (S, N, 2)
            d2 = dxy[..., 0] ** 2 + dxy[..., 1] ** 2 + repel_eps  # (S, N)
            # Zero self-pair (diagonal in the soft slice).
            for k_local, sidx in enumerate(soft_movable_idx):
                d2[k_local, int(sidx)] = np.inf
            inv_d2 = 1.0 / d2  # (S, N)
            repel_force = (dxy * inv_d2[..., None]).sum(axis=1)  # (S, 2)
            scale = float(self.soft_fd_repel_k) * (cw * ch) / max(1, n)
            repel_x = scale * repel_force[:, 0]
            repel_y = scale * repel_force[:, 1]

            # ── Combined step with caps to keep moves stable ──
            step_x = lr * attract_x + repel_x
            step_y = lr * attract_y + repel_y
            cap = max(cw, ch) * 0.05
            np.clip(step_x, -cap, cap, out=step_x)
            np.clip(step_y, -cap, cap, out=step_y)

            # Apply via apply_move so net bboxes / density grid stay coherent.
            n_moved = 0
            for k_local, sidx in enumerate(soft_movable_idx):
                sidx_int = int(sidx)
                new_x = float(np.clip(pos[sidx_int, 0] + step_x[k_local], hw[sidx_int], cw - hw[sidx_int]))
                new_y = float(np.clip(pos[sidx_int, 1] + step_y[k_local], hh[sidx_int], ch - hh[sidx_int]))
                if abs(new_x - pos[sidx_int, 0]) < 1e-6 and abs(new_y - pos[sidx_int, 1]) < 1e-6:
                    continue
                # Soft macros may overlap; just apply (no legality check).
                apply_move(sidx_int, new_x, new_y)
                n_moved += 1
            return n_moved

        def _soft_fd_density_step() -> int:
            """One density-gradient step. For each soft macro, compute
            ∂(density²)/∂(sx, sy) from the density grid and step opposite
            the gradient. Also adds an attraction term toward HPWL net
            centroids so wirelength doesn't drift.
            """
            if soft_movable_idx.size == 0:
                return 0
            inv_ca2 = 1.0 / (cell_area * cell_area)
            n_moved = 0
            for sidx in soft_movable_idx:
                s = int(sidx)
                sx = float(pos[s, 0])
                sy = float(pos[s, 1])
                xmin = sx - hw[s]
                xmax = sx + hw[s]
                ymin = sy - hh[s]
                ymax = sy + hh[s]
                c0 = max(0, int(xmin / cell_w_g))
                c1 = min(gc - 1, int(xmax / cell_w_g))
                r0 = max(0, int(ymin / cell_h_g))
                r1 = min(gr - 1, int(ymax / cell_h_g))
                if c0 > c1 or r0 > r1:
                    continue
                # Compute overlap_y per row
                overlap_y_arr = np.zeros(r1 - r0 + 1)
                for ri, r in enumerate(range(r0, r1 + 1)):
                    cy0 = r * cell_h_g
                    cy1 = (r + 1) * cell_h_g
                    overlap_y_arr[ri] = max(0.0, min(ymax, cy1) - max(ymin, cy0))
                # Compute overlap_x per col
                overlap_x_arr = np.zeros(c1 - c0 + 1)
                for ci, c in enumerate(range(c0, c1 + 1)):
                    cx0 = c * cell_w_g
                    cx1 = (c + 1) * cell_w_g
                    overlap_x_arr[ci] = max(0.0, min(xmax, cx1) - max(xmin, cx0))
                # ∂(density²)/∂sx = sum_r 2*grid[r,c]/cell_area² * (∂overlap_x/∂sx) * overlap_y[r]
                # ∂overlap_x/∂sx is +1 only for the rightmost column the macro
                # PARTIALLY occupies (i.e., right edge inside it), -1 for the
                # leftmost column similarly, 0 elsewhere.
                grad_x = 0.0
                grad_y = 0.0
                # Detect which columns the macro's edges sit in
                left_col = c0  # left edge xmin is in column c0
                right_col = c1  # right edge xmax is in column c1
                # Skip if both edges in same column (macro fully inside cell)
                if left_col != right_col:
                    for ri, r in enumerate(range(r0, r1 + 1)):
                        oy = overlap_y_arr[ri]
                        if oy <= 0:
                            continue
                        grad_x += (
                            2.0 * inv_ca2 * oy
                            * (float(grid[r, right_col]) - float(grid[r, left_col]))
                        )
                # Same logic for y
                bottom_row = r0
                top_row = r1
                if bottom_row != top_row:
                    for ci, c in enumerate(range(c0, c1 + 1)):
                        ox = overlap_x_arr[ci]
                        if ox <= 0:
                            continue
                        grad_y += (
                            2.0 * inv_ca2 * ox
                            * (float(grid[top_row, c]) - float(grid[bottom_row, c]))
                        )
                # Step opposite the gradient (descend density penalty).
                step_x = -self.soft_fd_density_lr * grad_x
                step_y = -self.soft_fd_density_lr * grad_y
                # Add attraction toward HPWL net centroid.
                if self.soft_fd_attract_lr > 0:
                    sx_acc = 0.0
                    sy_acc = 0.0
                    cnt_a = 0
                    for nid, k in m_pins[s]:
                        if net_size[nid] <= 1:
                            continue
                        cx_n = (net_minx[nid] + net_maxx[nid]) / 2.0
                        cy_n = (net_miny[nid] + net_maxy[nid]) / 2.0
                        sx_acc += cx_n - net_ox[nid][k]
                        sy_acc += cy_n - net_oy[nid][k]
                        cnt_a += 1
                    if cnt_a > 0:
                        target_x = sx_acc / cnt_a
                        target_y = sy_acc / cnt_a
                        step_x += self.soft_fd_attract_lr * (target_x - sx)
                        step_y += self.soft_fd_attract_lr * (target_y - sy)
                # Cap step magnitude so we don't oscillate.
                cap = max(cw, ch) * 0.05
                if step_x > cap:
                    step_x = cap
                elif step_x < -cap:
                    step_x = -cap
                if step_y > cap:
                    step_y = cap
                elif step_y < -cap:
                    step_y = -cap
                nx = float(np.clip(sx + step_x, hw[s], cw - hw[s]))
                ny = float(np.clip(sy + step_y, hh[s], ch - hh[s]))
                if abs(nx - sx) < 1e-6 and abs(ny - sy) < 1e-6:
                    continue
                apply_move(s, nx, ny)
                n_moved += 1
            return n_moved

        def soft_fd_burst() -> int:
            total = 0
            if self.soft_fd_mode == "density":
                for _ in range(self.soft_fd_steps):
                    total += _soft_fd_density_step()
            else:
                for s_iter in range(self.soft_fd_steps):
                    lr = self.soft_fd_lr * (1.0 - 0.5 * s_iter / max(1, self.soft_fd_steps))
                    total += _soft_fd_step(lr)
            return total

        # ── Smoothed cong_grid cache (5x5 box filter) ───────────────────
        # Refreshed once per CD pass, not on every move (too expensive).
        cong_grid_smoothed = cong_grid.astype(np.float64).copy()
        cong_pen_smooth = float((cong_grid_smoothed * cong_grid_smoothed).sum())

        def refresh_smooth_cong() -> None:
            nonlocal cong_grid_smoothed, cong_pen_smooth
            if not self.smooth_cong:
                return
            try:
                from scipy.ndimage import uniform_filter

                cong_grid_smoothed = uniform_filter(
                    cong_grid.astype(np.float64),
                    size=self.smooth_cong_kernel,
                    mode="constant",
                    cval=0.0,
                )
            except Exception:
                # Fallback: simple manual box filter via numpy
                k = self.smooth_cong_kernel
                pad = k // 2
                padded = np.pad(cong_grid.astype(np.float64), pad, mode="constant")
                acc = np.zeros_like(cong_grid, dtype=np.float64)
                for di in range(-pad, pad + 1):
                    for dj in range(-pad, pad + 1):
                        acc += padded[
                            pad + di : pad + di + cong_grid.shape[0],
                            pad + dj : pad + dj + cong_grid.shape[1],
                        ]
                cong_grid_smoothed = acc / (k * k)
            cong_pen_smooth = float((cong_grid_smoothed * cong_grid_smoothed).sum())

        # ── Coordinate Descent ───────────────────────────────────────────
        movable_idx = [i for i in range(nh) if movable[i]]
        if not movable_idx:
            full = benchmark.macro_positions.clone()
            return full

        lam = float(self.density_lambda)
        lam_c = float(self.congestion_lambda)
        cur_wl = hpwl_total()
        cur_den = density_pen
        cur_cong = cong_pen
        cur_cost = cur_wl + lam * cur_den + lam_c * cur_cong
        best_cost = cur_cost
        best_pos = pos.copy()
        # Initialize smoothed cong_grid cache (used after smoothing kicks in).
        refresh_smooth_cong()
        if self.verbose:
            print(
                f"  [init] HPWL={cur_wl:.1f} DEN={cur_den:.2f} CONG={cur_cong:.0f} "
                f"COST={cur_cost:.1f}, n_movable={len(movable_idx)}, "
                f"n_soft_movable={int(soft_movable_idx.size)}, "
                f"n_nets={n_nets}, t={time.time()-t0:.1f}s"
            )

        # ── Penalty Ramp warm-start (RePlAce-style density scheduling) ─────
        # Phase A: overlap-tolerant moves with rising overlap penalty.
        # Phase B: explicit repair — force every overlapping macro to a legal
        #          slot via find_legal_near (unconditional move).
        # Phase C: resync surrogate state, then fall through to standard CD/SA/LNS.
        # The mid-point _legalize_greedy from the prior attempt left 44 overlaps
        # because the spiral search fell back to the original position when
        # max_rings ran out. Forcing each overlapping macro through
        # find_legal_near here is more reliable for legality.
        if self.penalty_ramp and len(movable_idx) > 0:
            try:
                from scipy.ndimage import gaussian_filter
            except ImportError:
                def gaussian_filter(x, sigma): return x

            for rpass in range(int(self.ramp_steps)):
                frac = rpass / max(1, int(self.ramp_steps) - 1)
                lam_o = float(self.ramp_lam_o_lo) * (float(self.ramp_lam_o_hi) / float(self.ramp_lam_o_lo)) ** frac
                lam_den_eff = lam * frac
                
                # --- Global Gaussian Density Field (HARD macros only) ---
                # Computing the gradient over hard+soft was the bug in v6: soft
                # macros are static and dominate the density mountains, so the
                # gradient pushed hard macros into soft-valleys regardless of
                # where their HPWL targets were. Restricting to nh gives a
                # gradient that reflects only the placeable population.
                temp_grid = np.zeros_like(grid)
                for mi in range(nh):
                    for r, c, a in cells_for(mi, float(pos[mi, 0]), float(pos[mi, 1])):
                        temp_grid[r, c] += a
                # Smear out from 30 down to 2 over the ramp
                sigma = 30.0 * (1.0 - frac) + 2.0
                global_den = gaussian_filter(temp_grid, sigma=sigma)
                # Compute spatial gradients of the global density field
                grad_y, grad_x = np.gradient(global_den)
                # The gradient force should push macros away from dense regions
                
                # Evolve the system by doing a few inner steps of gradient descent
                n_acc_ramp = 0
                for _inner in range(3):
                    random.shuffle(movable_idx)
                    for m in movable_idx:
                        tx, ty = compute_target(m)
                        
                        # 1. Wirelength attractive force (spring towards HPWL centroid)
                        fwx = tx - pos[m, 0]
                        fwy = ty - pos[m, 1]
                        
                        # 2. Density repulsive force (opposite to density gradient)
                        c = min(gc - 1, max(0, int(pos[m, 0] / cell_w_g)))
                        r = min(gr - 1, max(0, int(pos[m, 1] / cell_h_g)))
                        
                        fdx = -float(grad_x[r, c])
                        fdy = -float(grad_y[r, c])
                        
                        # Normalize repulsive gradient to avoid exploding forces on steep mountains
                        gnorm = np.sqrt(fdx**2 + fdy**2) + 1e-9
                        fdx /= gnorm
                        fdy /= gnorm
                        
                        # Weighting the repulsive force. Increases as we ramp up.
                        # Scale density force by canvas dimensions to make it comparable to HPWL distance
                        den_scale = max(cw, ch) * 0.15 * frac
                        
                        step_x = fwx + den_scale * fdx
                        step_y = fwy + den_scale * fdy
                        
                        # Cap step size to ensure smooth trajectory
                        cap = max(cw, ch) * 0.05
                        norm = np.sqrt(step_x**2 + step_y**2) + 1e-9
                        if norm > cap:
                            step_x = (step_x / norm) * cap
                            step_y = (step_y / norm) * cap
                            
                        nx = float(np.clip(pos[m, 0] + step_x, hw[m], cw - hw[m]))
                        ny = float(np.clip(pos[m, 1] + step_y, hh[m], ch - hh[m]))
                        
                        if abs(nx - pos[m, 0]) > 1e-6 or abs(ny - pos[m, 1]) > 1e-6:
                            apply_move(m, nx, ny)
                            n_acc_ramp += 1
                if self.verbose:
                    print(
                        f"  [ramp r{rpass+1:02d}] acc={n_acc_ramp} "
                        f"lam_o={lam_o:.1f} lam_den={lam_den_eff:.1f} sigma={sigma:.1f} "
                        f"t={time.time()-t0:.1f}s"
                    )

            # Phase B: explicit repair — force overlapping macros to legal slots.
            repair_order = sorted(movable_idx, key=lambda i: -float(sizes[i, 0] * sizes[i, 1]))
            n_repaired = 0
            n_unrepairable = 0
            for m in repair_order:
                if not overlaps_with_others(m, float(pos[m, 0]), float(pos[m, 1])):
                    continue
                legal = find_legal_near(m, float(pos[m, 0]), float(pos[m, 1]), max_rings=150)
                if legal is None:
                    n_unrepairable += 1
                    continue
                nx, ny = legal
                if abs(nx - pos[m, 0]) >= 1e-6 or abs(ny - pos[m, 1]) >= 1e-6:
                    apply_move(m, nx, ny)  # unconditional — repair
                    n_repaired += 1

            # Phase C: resync surrogate from authoritative state.
            cur_wl = hpwl_total()
            cur_den = float(((grid / cell_area) ** 2).sum())
            cur_cong = float((cong_grid * cong_grid).sum())
            cur_cost = cur_wl + lam * cur_den + lam_c * cur_cong
            best_cost = cur_cost
            best_pos = pos.copy()
            refresh_smooth_cong()
            if self.verbose:
                print(
                    f"  [post-ramp] repaired={n_repaired} unrepairable={n_unrepairable} "
                    f"HPWL={cur_wl:.1f} DEN={cur_den:.2f} CONG={cur_cong:.0f} "
                    f"COST={cur_cost:.1f} t={time.time()-t0:.1f}s"
                )

        # ── Incremental refinement: place macros one-at-a-time in size order ─
        # Each macro moves from its initial.plc slot to its HPWL-optimal legal
        # slot, computed against the current (partially-refined) bbox state.
        # Periodic mini-CD over already-touched set lets earlier placements
        # adjust as later ones land. This replaces the "blind global legalize
        # → optimize" pattern that creates irreversible traffic jams.
        if self.incremental_init and len(movable_idx) > 0:
            order = sorted(movable_idx, key=lambda i: -float(sizes[i, 0] * sizes[i, 1]))
            n_inc_moved = 0
            n_inc_skipped = 0
            n_inc_cd_acc = 0
            batch_k = max(1, int(self.incremental_batch))
            touched: List[int] = []
            for k_ins, m in enumerate(order):
                tx, ty = compute_target(m)
                legal = find_legal_near(m, tx, ty, max_rings=60)
                if legal is None:
                    n_inc_skipped += 1
                    touched.append(m)
                    continue
                nx, ny = legal
                if abs(nx - pos[m, 0]) >= 1e-6 or abs(ny - pos[m, 1]) >= 1e-6:
                    d_wl, d_den, d_cong, _state = apply_move(m, nx, ny)
                    d_m = d_wl + lam * d_den + lam_c * d_cong
                    if d_m < -1e-9:
                        cur_wl += d_wl
                        cur_den += d_den
                        cur_cong += d_cong
                        cur_cost += d_m
                        n_inc_moved += 1
                    else:
                        revert(m, _state)
                        n_inc_skipped += 1
                touched.append(m)
                # Periodic partial CD over the placed-so-far set.
                if (k_ins + 1) % batch_k == 0 and len(touched) > 1:
                    pass_n_acc = 0
                    for j in touched:
                        tx_j, ty_j = compute_target(j)
                        legal_j = find_legal_near(j, tx_j, ty_j, max_rings=30)
                        if legal_j is None:
                            continue
                        nxj, nyj = legal_j
                        if abs(nxj - pos[j, 0]) < 1e-6 and abs(nyj - pos[j, 1]) < 1e-6:
                            continue
                        d_wl_j, d_den_j, d_cong_j, st_j = apply_move(j, nxj, nyj)
                        d_j = d_wl_j + lam * d_den_j + lam_c * d_cong_j
                        if d_j < -1e-9:
                            cur_wl += d_wl_j
                            cur_den += d_den_j
                            cur_cong += d_cong_j
                            cur_cost += d_j
                            pass_n_acc += 1
                        else:
                            revert(j, st_j)
                    n_inc_cd_acc += pass_n_acc
            if cur_cost < best_cost:
                best_cost = cur_cost
                best_pos = pos.copy()
            if self.verbose:
                print(
                    f"  [inc-init] placed={n_inc_moved} skipped={n_inc_skipped} "
                    f"cd_acc={n_inc_cd_acc} HPWL={cur_wl:.1f} DEN={cur_den:.2f} "
                    f"CONG={cur_cong:.0f} COST={cur_cost:.1f} t={time.time()-t0:.1f}s"
                )

        for ppass in range(self.cd_passes):
            if time.time() - t0 > self.max_seconds * 0.55:
                break
            random.shuffle(movable_idx)
            n_acc = 0
            n_no_move = 0
            for m in movable_idx:
                tx, ty = compute_target(m)
                legal = find_legal_near(m, tx, ty)
                if legal is None:
                    continue
                cx, cy = legal
                if abs(cx - pos[m, 0]) < 1e-6 and abs(cy - pos[m, 1]) < 1e-6:
                    n_no_move += 1
                    continue
                d_wl, d_den, d_cong, state = apply_move(m, cx, cy)
                d = d_wl + lam * d_den + lam_c * d_cong
                # Acceptance: annealed (threshold ramps 5%*cur_cost → 0) if
                # annealed_cd, else strict greedy. Annealed lets locally-worse
                # moves through early so coordinated topology shifts can pass
                # the per-macro acceptance test that single-macro CD rejects.
                if self.annealed_cd:
                    frac = ppass / max(1, self.cd_passes - 1)
                    accept_thresh = 0.05 * cur_cost * (1.0 - frac)
                else:
                    accept_thresh = -1e-9
                if d < accept_thresh:
                    n_acc += 1
                    cur_wl += d_wl
                    cur_den += d_den
                    cur_cong += d_cong
                    cur_cost += d
                else:
                    revert(m, state)

            # ── Soft-macro FD burst at end of pass (drag soft to nets) ───
            soft_moved = soft_fd_burst() if self.soft_fd_steps > 0 else 0
            # Resync cur_cost components after soft moves (they touched bboxes
            # and the density grid via apply_move).
            cur_wl = hpwl_total()
            cur_den = float(((grid / cell_area) ** 2).sum())
            cur_cong = float((cong_grid * cong_grid).sum())
            cur_cost = cur_wl + lam * cur_den + lam_c * cur_cong
            # Refresh smoothed cong cache once per pass (cheap).
            refresh_smooth_cong()

            if cur_cost < best_cost:
                best_cost = cur_cost
                best_pos = pos.copy()

            if self.verbose:
                print(
                    f"  [cd p{ppass+1:02d}] HPWL={cur_wl:.1f} DEN={cur_den:.2f} "
                    f"CONG={cur_cong:.0f} CONGs={cong_pen_smooth:.0f} "
                    f"COST={cur_cost:.1f} (best {best_cost:.1f}) "
                    f"acc={n_acc} stuck={n_no_move} soft_mvd={soft_moved} "
                    f"t={time.time()-t0:.1f}s"
                )
            if n_acc == 0 and soft_moved == 0:
                break

        # ── SA perturbation phase on HPWL surrogate ──────────────────────
        # CD converges to a local minimum; SA escapes it.
        canvas_diag = math.hypot(cw, ch)
        T_hi = self.sa_initial_temp_frac * canvas_diag
        T_lo = self.sa_final_temp_frac * canvas_diag

        # Build per-macro neighbor list (macros sharing a net) for swaps.
        neighbor_set: List[set] = [set() for _ in range(nh)]
        for nid in range(n_nets):
            owners = net_owners[nid]
            isp = net_is_port[nid]
            hard_owners = [int(owners[k]) for k in range(len(owners)) if not isp[k] and int(owners[k]) < nh]
            for i in range(len(hard_owners)):
                for j in range(i + 1, len(hard_owners)):
                    a = hard_owners[i]
                    b = hard_owners[j]
                    if a != b:
                        neighbor_set[a].add(b)
                        neighbor_set[b].add(a)
        neighbors: List[List[int]] = [list(s) for s in neighbor_set]

        if self.sa_iters > 0 and len(movable_idx) > 0:
            t_remaining = self.max_seconds - (time.time() - t0)
            sa_budget = max(0.0, min(t_remaining * 0.85, self.max_seconds * 0.65))
            sa_start = time.time()
            iters_done = 0
            n_acc = 0
            n_imp = 0
            # True-proxy tracking: compute the actual proxy at intervals and
            # save the best by *true* proxy (the surrogate can disagree with it).
            best_true_proxy = float("inf")
            best_true_pos: Optional[np.ndarray] = None
            true_proxy_plc = None
            last_true_check = sa_start
            # Load plc only if SA tracking is enabled. HC's gate at the end
            # of place() additionally requires `true_proxy_check_every > 0`,
            # so loading the plc just because hill_climb_seconds>0 was wasted
            # work — multiple unused plc loads/restart caused memory pressure.
            if self.true_proxy_check_every > 0:
                from macro_place.objective import compute_proxy_cost as _compute_proxy
                true_proxy_plc = _load_plc_for(benchmark)

            def _maybe_check_true_proxy() -> None:
                nonlocal best_true_proxy, best_true_pos, last_true_check
                if true_proxy_plc is None or self.true_proxy_check_every <= 0:
                    return
                now = time.time()
                if now - last_true_check < self.true_proxy_check_every:
                    return
                last_true_check = now
                full_pl = benchmark.macro_positions.clone()
                full_pl[:nh] = torch.tensor(pos[:nh], dtype=torch.float32)
                try:
                    costs_eval = _compute_proxy(full_pl, benchmark, true_proxy_plc)
                    p_now = float(costs_eval["proxy_cost"])
                    if int(costs_eval["overlap_count"]) == 0 and p_now < best_true_proxy:
                        best_true_proxy = p_now
                        best_true_pos = pos.copy()
                except Exception:
                    pass
            while iters_done < self.sa_iters and (time.time() - sa_start) < sa_budget:
                # Slow cooling: target T_lo after 2x the budget so within the
                # actual budget we stay closer to T_hi (high-temp exploration
                # works well empirically for this problem).
                frac = min(1.0, 0.5 * (time.time() - sa_start) / max(1e-3, sa_budget))
                T = T_hi * (T_lo / T_hi) ** frac
                shift_scale = max(canvas_diag * 0.005, T * 0.5)

                roll = random.random()
                if roll < 0.1:
                    # Bias macro selection toward those in dense cells.
                    cand_set = random.sample(movable_idx, min(6, len(movable_idx)))
                    m = max(
                        cand_set,
                        key=lambda i: float(
                            grid[
                                min(gr - 1, max(0, int(pos[i, 1] / cell_h_g))),
                                min(gc - 1, max(0, int(pos[i, 0] / cell_w_g))),
                            ]
                        ),
                    )
                    # Low-density teleport: sample 12 random cells, pick the
                    # least-dense one, move macro there. Big jump out of
                    # over-clustered regions.
                    best_cell_density = float("inf")
                    best_cell: Optional[Tuple[int, int]] = None
                    for _ in range(12):
                        rr = random.randint(0, gr - 1)
                        cc = random.randint(0, gc - 1)
                        d_here = float(grid[rr, cc])
                        if d_here < best_cell_density:
                            best_cell_density = d_here
                            best_cell = (rr, cc)
                    if best_cell is None:
                        iters_done += 1
                        continue
                    rr, cc = best_cell
                    tx_t = (cc + 0.5) * cell_w_g + random.uniform(
                        -cell_w_g * 0.4, cell_w_g * 0.4
                    )
                    ty_t = (rr + 0.5) * cell_h_g + random.uniform(
                        -cell_h_g * 0.4, cell_h_g * 0.4
                    )
                    legal = find_legal_near(m, tx_t, ty_t, max_rings=30)
                    if legal is None:
                        iters_done += 1
                        continue
                    nx, ny = legal
                    if abs(nx - pos[m, 0]) < 1e-6 and abs(ny - pos[m, 1]) < 1e-6:
                        iters_done += 1
                        continue
                    d_wl, d_den, d_cong, state = apply_move(m, nx, ny)
                    d = d_wl + lam * d_den + lam_c * d_cong
                    if d < 0 or random.random() < math.exp(-d / max(T, 1e-9)):
                        cur_wl += d_wl
                        cur_den += d_den
                        cur_cong += d_cong
                        cur_cost += d
                        n_acc += 1
                        if cur_cost < best_cost:
                            best_cost = cur_cost
                            best_pos = pos.copy()
                            n_imp += 1
                    else:
                        revert(m, state)
                else:
                    m = random.choice(movable_idx)
                if roll < 0.1:
                    pass  # already handled above
                elif roll < 0.6:
                    # Gaussian shift toward HPWL target
                    tx_t, ty_t = compute_target(m)
                    alpha = random.uniform(0.1, 0.7)
                    nx = pos[m, 0] + alpha * (tx_t - pos[m, 0]) + random.gauss(0, shift_scale)
                    ny = pos[m, 1] + alpha * (ty_t - pos[m, 1]) + random.gauss(0, shift_scale)
                    nx = float(np.clip(nx, hw[m], cw - hw[m]))
                    ny = float(np.clip(ny, hh[m], ch - hh[m]))
                    if overlaps_with_others(m, nx, ny):
                        iters_done += 1
                        continue
                    d_wl, d_den, d_cong, state = apply_move(m, nx, ny)
                    d = d_wl + lam * d_den + lam_c * d_cong
                    if d < 0 or random.random() < math.exp(-d / max(T, 1e-9)):
                        cur_wl += d_wl
                        cur_den += d_den
                        cur_cong += d_cong
                        cur_cost += d
                        n_acc += 1
                        if cur_cost < best_cost:
                            best_cost = cur_cost
                            best_pos = pos.copy()
                            n_imp += 1
                    else:
                        revert(m, state)
                elif roll < 0.85:
                    # Swap two macros (prefer net-neighbors)
                    cands = neighbors[m] if neighbors[m] else movable_idx
                    j = random.choice(cands)
                    if j == m or not movable[j]:
                        iters_done += 1
                        continue
                    # Try swapping centers but clip to canvas; check overlaps
                    new_xi = float(np.clip(pos[j, 0], hw[m], cw - hw[m]))
                    new_yi = float(np.clip(pos[j, 1], hh[m], ch - hh[m]))
                    new_xj = float(np.clip(pos[m, 0], hw[j], cw - hw[j]))
                    new_yj = float(np.clip(pos[m, 1], hh[j], ch - hh[j]))

                    di_wl, di_den, di_cong, si = apply_move(m, new_xi, new_yi)
                    if overlaps_with_others(m, new_xi, new_yi):
                        revert(m, si)
                        iters_done += 1
                        continue
                    dj_wl, dj_den, dj_cong, sj = apply_move(j, new_xj, new_yj)
                    if overlaps_with_others(j, new_xj, new_yj):
                        revert(j, sj)
                        revert(m, si)
                        iters_done += 1
                        continue
                    d_wl = di_wl + dj_wl
                    d_den = di_den + dj_den
                    d_cong = di_cong + dj_cong
                    d = d_wl + lam * d_den + lam_c * d_cong
                    if d < 0 or random.random() < math.exp(-d / max(T, 1e-9)):
                        cur_wl += d_wl
                        cur_den += d_den
                        cur_cong += d_cong
                        cur_cost += d
                        n_acc += 1
                        if cur_cost < best_cost:
                            best_cost = cur_cost
                            best_pos = pos.copy()
                            n_imp += 1
                    else:
                        revert(j, sj)
                        revert(m, si)
                else:
                    # Direct CD step (move to target if legal)
                    tx_t, ty_t = compute_target(m)
                    legal = find_legal_near(m, tx_t, ty_t, max_rings=20)
                    if legal is None:
                        iters_done += 1
                        continue
                    nx, ny = legal
                    if abs(nx - pos[m, 0]) < 1e-6 and abs(ny - pos[m, 1]) < 1e-6:
                        iters_done += 1
                        continue
                    d_wl, d_den, d_cong, state = apply_move(m, nx, ny)
                    d = d_wl + lam * d_den + lam_c * d_cong
                    if d < 0:
                        cur_wl += d_wl
                        cur_den += d_den
                        cur_cong += d_cong
                        cur_cost += d
                        n_acc += 1
                        if cur_cost < best_cost:
                            best_cost = cur_cost
                            best_pos = pos.copy()
                            n_imp += 1
                    else:
                        revert(m, state)
                iters_done += 1
                _maybe_check_true_proxy()

            # Final true-proxy check at end of SA.
            if true_proxy_plc is not None:
                last_true_check = 0.0  # force a check
                _maybe_check_true_proxy()

            if self.verbose:
                tp_str = (
                    f" best_true={best_true_proxy:.4f}"
                    if best_true_pos is not None
                    else ""
                )
                print(
                    f"  [sa] iters={iters_done} acc={n_acc} imp={n_imp} "
                    f"HPWL={cur_wl:.1f} DEN={cur_den:.2f} CONG={cur_cong:.0f} "
                    f"COST={cur_cost:.1f} best={best_cost:.1f}{tp_str} "
                    f"t={time.time()-t0:.1f}s"
                )

        # ── LNS phase: targeted rip-up of macros in high-density cells ───
        # Repair by moving each chosen macro toward a low-density cell.
        lns_iter = 0
        if self.lns_iters > 0 and time.time() - t0 < self.max_seconds * 0.95:
            n_lns_acc = 0
            lns_start = time.time()
            lns_budget = max(
                0.0,
                min(self.max_seconds - (time.time() - t0) - 5.0, self.max_seconds * 0.25),
            )

            def _pick_high_density_macro() -> Optional[int]:
                """Return a movable hard macro inside the densest of N sampled cells."""
                best_d = -1.0
                best_rc: Optional[Tuple[int, int]] = None
                for _ in range(8):
                    rr = random.randint(0, gr - 1)
                    cc = random.randint(0, gc - 1)
                    d_here = float(grid[rr, cc])
                    if d_here > best_d:
                        best_d = d_here
                        best_rc = (rr, cc)
                if best_rc is None:
                    return None
                rr, cc = best_rc
                cx_c = (cc + 0.5) * cell_w_g
                cy_c = (rr + 0.5) * cell_h_g
                dx = cx_c - pos[:nh, 0]
                dy = cy_c - pos[:nh, 1]
                ds = dx * dx + dy * dy
                # Sort by distance and pick closest movable hard macro.
                order = np.argsort(ds)
                for idx in order[:5]:
                    if movable[idx]:
                        return int(idx)
                return None

            def _pick_low_density_target() -> Optional[Tuple[float, float]]:
                best_d = float("inf")
                best_rc: Optional[Tuple[int, int]] = None
                for _ in range(12):
                    rr = random.randint(0, gr - 1)
                    cc = random.randint(0, gc - 1)
                    d_here = float(grid[rr, cc])
                    if d_here < best_d:
                        best_d = d_here
                        best_rc = (rr, cc)
                if best_rc is None:
                    return None
                rr, cc = best_rc
                tx = (cc + 0.5) * cell_w_g + random.uniform(
                    -cell_w_g * 0.4, cell_w_g * 0.4
                )
                ty = (rr + 0.5) * cell_h_g + random.uniform(
                    -cell_h_g * 0.4, cell_h_g * 0.4
                )
                return tx, ty

            for lns_iter in range(self.lns_iters):
                if time.time() - lns_start > lns_budget:
                    break
                K = random.randint(2, max(2, self.lns_cluster_size))
                if K > len(movable_idx):
                    K = len(movable_idx)

                cost_before = cur_cost
                wl_before = cur_wl
                den_before = cur_den
                cong_before = cur_cong
                states_applied: List = []
                seen_set = set()

                for _slot in range(K):
                    # Target a macro from a high-density region.
                    if random.random() < 0.7:
                        m = _pick_high_density_macro()
                        if m is None or m in seen_set:
                            m = random.choice(movable_idx)
                    else:
                        m = random.choice(movable_idx)
                    if m in seen_set:
                        continue
                    seen_set.add(m)

                    # Repair: half toward HPWL target, half toward low-density cell.
                    if random.random() < 0.5:
                        tx_t, ty_t = compute_target(m)
                        tx_t += random.gauss(0, canvas_diag * 0.04)
                        ty_t += random.gauss(0, canvas_diag * 0.04)
                    else:
                        target_low = _pick_low_density_target()
                        if target_low is None:
                            continue
                        tx_t, ty_t = target_low
                    legal = find_legal_near(m, tx_t, ty_t, max_rings=40)
                    if legal is None:
                        continue
                    nx, ny = legal
                    if abs(nx - pos[m, 0]) < 1e-6 and abs(ny - pos[m, 1]) < 1e-6:
                        continue
                    d_wl, d_den, d_cong, state = apply_move(m, nx, ny)
                    states_applied.append((m, state))
                    cur_wl += d_wl
                    cur_den += d_den
                    cur_cong += d_cong
                    cur_cost += d_wl + lam * d_den + lam_c * d_cong

                if cur_cost < cost_before - 1e-6:
                    n_lns_acc += 1
                    if cur_cost < best_cost:
                        best_cost = cur_cost
                        best_pos = pos.copy()
                else:
                    for m, state in reversed(states_applied):
                        revert(m, state)
                    cur_cost = cost_before
                    cur_wl = wl_before
                    cur_den = den_before
                    cur_cong = cong_before
            if self.verbose:
                print(
                    f"  [lns] iters={lns_iter+1} acc={n_lns_acc} "
                    f"HPWL={cur_wl:.1f} DEN={cur_den:.2f} CONG={cur_cong:.0f} "
                    f"COST={cur_cost:.1f} best={best_cost:.1f} "
                    f"t={time.time()-t0:.1f}s"
                )
            # Final true-proxy check after LNS.
            if 'true_proxy_plc' in dir() and true_proxy_plc is not None:
                last_true_check = 0.0
                _maybe_check_true_proxy()

        # Choose final placement: prefer best_true_pos if it's actually
        # better than best_pos when both are evaluated against true proxy.
        if (
            "best_true_pos" in locals()
            and best_true_pos is not None
            and "true_proxy_plc" in locals()
            and true_proxy_plc is not None
        ):
            from macro_place.objective import compute_proxy_cost as _cp
            full_a = benchmark.macro_positions.clone()
            full_a[:nh] = torch.tensor(best_pos[:nh], dtype=torch.float32)
            try:
                a_costs = _cp(full_a, benchmark, true_proxy_plc)
                a_proxy = float(a_costs["proxy_cost"])
                a_overlap = int(a_costs["overlap_count"])
            except Exception:
                a_proxy, a_overlap = float("inf"), 1
            chosen_by_true = (
                a_overlap > 0 or a_proxy > best_true_proxy + 1e-9
            )
            if chosen_by_true:
                pos = best_true_pos.copy()
                if self.verbose:
                    print(
                        f"  [final-pick] best_true={best_true_proxy:.4f} "
                        f"vs surrogate-best_proxy={a_proxy:.4f} "
                        f"→ chose best_true"
                    )
            else:
                pos = best_pos.copy()
                if self.verbose:
                    print(
                        f"  [final-pick] surrogate-best_proxy={a_proxy:.4f} "
                        f"≤ best_true={best_true_proxy:.4f} "
                        f"→ chose best_surrogate"
                    )
        else:
            pos = best_pos.copy()
        recompute_pins()

        # ── Final true-proxy hill climb (opt-in) ────────────────────────
        # The surrogate doesn't perfectly track proxy (especially congestion
        # which uses TILOS routing model). Run a small greedy hill climb
        # using `compute_proxy_cost` directly — slow per step but exact.
        # Disabled by default: each plc.compute_proxy_cost call grows with
        # benchmark size, so HC can blow past the 1-hour-per-bench limit on
        # large benchmarks.
        if (
            self.hill_climb_seconds > 0
            and self.true_proxy_check_every > 0
            and "true_proxy_plc" in locals()
            and true_proxy_plc is not None
            and time.time() - t0 < self.max_seconds * 0.95
        ):
            from macro_place.objective import compute_proxy_cost as _cp_hc

            t_hc_start = time.time()
            hc_budget = max(
                0.0,
                min(
                    self.max_seconds - (time.time() - t0) - 3.0,
                    self.hill_climb_seconds,
                ),
            )
            full_pl = benchmark.macro_positions.clone()
            full_pl[:nh] = torch.tensor(pos[:nh], dtype=torch.float32)
            try:
                base_costs = _cp_hc(full_pl, benchmark, true_proxy_plc)
                cur_true = float(base_costs["proxy_cost"])
                if int(base_costs["overlap_count"]) > 0:
                    cur_true = float("inf")
            except Exception:
                cur_true = float("inf")

            n_hc_iter = 0
            n_hc_acc = 0
            best_hc_true = cur_true
            best_hc_pos = pos.copy()

            while time.time() - t_hc_start < hc_budget:
                m = random.choice(movable_idx)
                roll_hc = random.random()
                # Mix: HPWL target, low-density teleport, or random bigger shift.
                if roll_hc < 0.4:
                    tx_t, ty_t = compute_target(m)
                    tx_t += random.gauss(0, max(cw, ch) * 0.04)
                    ty_t += random.gauss(0, max(cw, ch) * 0.04)
                elif roll_hc < 0.7:
                    # Low-density teleport
                    best_d = float("inf")
                    best_rc = None
                    for _ in range(10):
                        rr = random.randint(0, gr - 1)
                        cc = random.randint(0, gc - 1)
                        d_here = float(grid[rr, cc])
                        if d_here < best_d:
                            best_d = d_here
                            best_rc = (rr, cc)
                    if best_rc is None:
                        n_hc_iter += 1
                        continue
                    rr, cc = best_rc
                    tx_t = (cc + 0.5) * cell_w_g + random.uniform(
                        -cell_w_g * 0.4, cell_w_g * 0.4
                    )
                    ty_t = (rr + 0.5) * cell_h_g + random.uniform(
                        -cell_h_g * 0.4, cell_h_g * 0.4
                    )
                else:
                    tx_t = pos[m, 0] + random.gauss(0, max(cw, ch) * 0.05)
                    ty_t = pos[m, 1] + random.gauss(0, max(cw, ch) * 0.05)
                legal = find_legal_near(m, tx_t, ty_t, max_rings=30)
                if legal is None:
                    n_hc_iter += 1
                    continue
                nx, ny = legal
                if abs(nx - pos[m, 0]) < 1e-6 and abs(ny - pos[m, 1]) < 1e-6:
                    n_hc_iter += 1
                    continue
                # Apply temporarily; evaluate true proxy.
                old_x, old_y = float(pos[m, 0]), float(pos[m, 1])
                pos[m, 0] = nx
                pos[m, 1] = ny
                full_pl[m, 0] = float(nx)
                full_pl[m, 1] = float(ny)
                try:
                    new_costs = _cp_hc(full_pl, benchmark, true_proxy_plc)
                    new_true = float(new_costs["proxy_cost"])
                    overlaps_n = int(new_costs["overlap_count"])
                except Exception:
                    new_true = float("inf")
                    overlaps_n = 1
                if overlaps_n == 0 and new_true < best_hc_true - 1e-9:
                    best_hc_true = new_true
                    best_hc_pos = pos.copy()
                    n_hc_acc += 1
                else:
                    pos[m, 0] = old_x
                    pos[m, 1] = old_y
                    full_pl[m, 0] = old_x
                    full_pl[m, 1] = old_y
                n_hc_iter += 1

            pos = best_hc_pos.copy()
            if self.verbose:
                print(
                    f"  [hc] iters={n_hc_iter} acc={n_hc_acc} "
                    f"best_true={best_hc_true:.4f} t={time.time()-t0:.1f}s"
                )

        # ── Soft-macro reoptimization via plc.optimize_stdcells ─────────
        # The simple FD-attract heuristic stacks soft macros on top of each
        # other (huge density spike); plc.optimize_stdcells uses both
        # attraction and repulsion (the same routine the SA baseline runs
        # between iterations).
        soft_pos_after: Optional[np.ndarray] = None
        if (
            benchmark.num_soft_macros > 0
            and time.time() - t0 < self.max_seconds * 0.95
            and any(s > 0 for s in self.soft_opt_steps)
        ):
            plc = _load_plc_for(benchmark)
            if plc is not None:
                # Push final hard-macro positions into plc; soft macros stay at
                # initial.plc (which is what plc currently has).
                for i, mod_idx in enumerate(benchmark.hard_macro_indices):
                    plc.modules_w_pins[mod_idx].set_pos(float(pos[i, 0]), float(pos[i, 1]))
                canvas_size = max(cw, ch)
                steps = list(self.soft_opt_steps)
                t_so_start = time.time()
                try:
                    plc.optimize_stdcells(
                        use_current_loc=False,
                        move_stdcells=True,
                        move_macros=False,
                        log_scale_conns=False,
                        use_sizes=False,
                        io_factor=1.0,
                        num_steps=steps,
                        max_move_distance=[canvas_size / 100.0] * 3,
                        attract_factor=[100.0, 1.0e-3, 1.0e-5],
                        repel_factor=[0.0, 1.0e6, 1.0e7],
                    )
                    # Read soft macro positions back.
                    soft_pos_after = np.zeros((benchmark.num_soft_macros, 2))
                    for j, mod_idx in enumerate(benchmark.soft_macro_indices):
                        sx, sy = plc.modules_w_pins[mod_idx].get_pos()
                        soft_pos_after[j, 0] = sx
                        soft_pos_after[j, 1] = sy
                    if self.verbose:
                        print(
                            f"  [soft-opt] steps={steps} took {time.time()-t_so_start:.1f}s"
                        )
                except Exception as e:  # noqa: BLE001
                    if self.verbose:
                        print(f"  [soft-opt] failed: {e}; keeping initial soft positions")

        # ── Build full placement ────────────────────────────────────────
        full = benchmark.macro_positions.clone()
        full[:nh] = torch.tensor(pos[:nh], dtype=torch.float32)
        if soft_pos_after is not None:
            full[nh:n] = torch.tensor(soft_pos_after, dtype=torch.float32)
        if self.verbose:
            print(f"  [done] COST={best_cost:.1f} t={time.time()-t0:.1f}s")
        return full
