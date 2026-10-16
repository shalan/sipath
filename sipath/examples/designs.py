"""Generators for the example inputs (real files OpenROAD reads).

``dp8``  : soft macro. Flat gate-level netlist with an 8-bit ripple adder,
           a 4x4 array multiplier, an XOR accumulator and two analog
           feed-through nets. Floorplan comes from a DEF written here.
``mcu8`` : MCU die. Pad ring of sky130_ef_io cells, one OpenRAM SRAM, an
           8-bit ADC and DAC hard macro (abstract LEFs written here) and a
           flat top with the soft logic as gates between the macros.

Run ``python3 -m sipath gen-inputs`` to (re)write everything under
``sipath/examples/inputs``.
"""
from __future__ import annotations

import os

from ..netlist import Module

HD = "sky130_fd_sc_hd__"
INPUTS = os.path.join(os.path.dirname(__file__), "inputs")


class changed_only:
    """``open(path, "w")`` that leaves a file with identical content alone:
    the generated inputs are committed, so an installed package is never
    written when they are current (P1.6)."""

    def __init__(self, path):
        import io
        self.path, self.buf = path, io.StringIO()

    def __enter__(self):
        return self.buf

    def __exit__(self, kind, *_):
        if kind is not None:
            return False
        new = self.buf.getvalue()
        try:
            with open(self.path) as f:
                if f.read() == new:
                    return False
        except OSError:
            pass
        with open(self.path, "w") as f:
            f.write(new)
        return False

# sky130 hd geometry (microns)
SITE_W, ROW_H = 0.46, 2.72
DBU = 1000


def _u(v: float) -> int:
    return int(round(v * DBU))


# -----------------------------------------------------------------------------
# shared logic builders
# -----------------------------------------------------------------------------
def ripple_adder(m: Module, pfx: str, a: list[str], b: list[str], tie_lo: str):
    """Ripple-carry adder of fa_1 cells. Returns (sum bits, carry out, insts)."""
    carry, sums, insts = tie_lo, [], []
    for i in range(len(a)):
        s = m.wire(f"{pfx}.s_{i}")
        co = m.wire(f"{pfx}.c_{i}")
        insts.append(m.inst(f"{pfx}.fa_{i}", HD + "fa_1", A=a[i], B=b[i], CIN=carry, COUT=co, SUM=s).name)
        sums.append(s)
        carry = co
    return sums, carry, insts


def array_multiplier(m: Module, pfx: str, x: list[str], y: list[str]):
    """Unsigned n x n array multiplier (and2 partial products, ha/fa rows).

    Returns (product bits, rows) where ``rows[r]`` is the list of instance
    names of physical row r, interleaved pp/adder left to right.
    """
    n = len(x)
    pp = [[None] * n for _ in range(n)]
    rows: list[list[str]] = [[] for _ in range(n)]
    for r in range(n):
        for c in range(n):
            pp[r][c] = m.wire(f"{pfx}.pp_{r}_{c}")
            m.inst(f"{pfx}.pp_{r}_{c}", HD + "and2_1", A=x[c], B=y[r], X=pp[r][c])
    out = [pp[0][0]]
    hi = pp[0][1:] + [None]
    rows[0] = [f"{pfx}.pp_0_{c}" for c in range(n)]
    for r in range(1, n):
        carry, new = None, []
        for c in range(n):
            a, b = hi[c], pp[r][c]
            name = f"{pfx}.add_{r}_{c}"
            rows[r].append(f"{pfx}.pp_{r}_{c}")
            if a is None and carry is None:
                new.append(b)
                continue
            s = m.wire(f"{name}.s")
            co = m.wire(f"{name}.co")
            if carry is None or a is None:
                other = a if a is not None else carry
                m.inst(name, HD + "ha_1", A=other, B=b, COUT=co, SUM=s)
            else:
                m.inst(name, HD + "fa_1", A=a, B=b, CIN=carry, COUT=co, SUM=s)
            rows[r].append(name)
            new.append(s)
            carry = co
        out.append(new[0])
        hi = new[1:] + [carry]
    out.extend(hi)
    return out, rows


def register(m: Module, name: str, clk: str, d: str, q: str):
    return m.inst(name, HD + "dfxtp_1", CLK=clk, D=d, Q=q)


# -----------------------------------------------------------------------------
# dp8 soft macro
# -----------------------------------------------------------------------------
def dp8_netlist() -> tuple[Module, dict]:
    m = Module("dp8")
    clk = m.port("clk", "input")[0]
    a = m.port("a", "input", 8)
    b = m.port("b", "input", 8)
    x = m.port("x", "input", 4)
    y = m.port("y", "input", 4)
    ana_in = m.port("ana_in", "inout", 2)
    ana_out = m.port("ana_out", "inout", 2)
    sum_ = m.port("sum", "output", 9)
    prod = m.port("prod", "output", 8)
    acc = m.port("acc", "output", 8)

    def regs(pfx, src, tag):
        q = []
        for i, d in enumerate(src):
            q.append(m.wire(f"u_in.{tag}_q_{i}"))
            register(m, f"u_in.r{tag}_{i}", clk, d, q[-1])
        return q

    aq, bq, xq, yq = regs("u_in", a, "a"), regs("u_in", b, "b"), regs("u_in", x, "x"), regs("u_in", y, "y")
    tie_lo = m.wire("u_add.tie_lo")
    m.inst("u_add.tie", HD + "conb_1", LO=tie_lo)
    sums, cout, adder = ripple_adder(m, "u_add", aq, bq, tie_lo)
    for i, s in enumerate(sums + [cout]):
        register(m, f"u_out.rs_{i}", clk, s, sum_[i])
    p, mul_rows = array_multiplier(m, "u_mul", xq, yq)
    for i, bit in enumerate(p):
        register(m, f"u_out.rp_{i}", clk, bit, prod[i])
    for i in range(8):
        nxt = m.wire(f"u_acc.n_{i}")
        q = m.wire(f"u_acc.q_{i}")
        m.inst(f"u_acc.x_{i}", HD + "xor2_1", A=q, B=sums[i], X=nxt)
        register(m, f"u_acc.r_{i}", clk, nxt, q)
        m.inst(f"u_acc.ob_{i}", HD + "buf_2", A=q, X=acc[i])
    # analog feed-throughs: one net each, no digital loads
    for i in range(2):
        m.assigns.append((ana_out[i], ana_in[i]))
    meta = {
        "adder_column": adder,
        # bit-slice view: bit i = [a register, b register, full adder]
        "adder_slices": [[f"u_in.ra_{i}", f"u_in.rb_{i}", adder[i]] for i in range(len(adder))],
        "multiplier_rows": mul_rows,
        "analog_nets": ana_in,
    }
    return m, meta


def floorplan_def(m: Module, die, site: str, site_w: float, row_h: float, margin_sites: int, margin_rows: int,
                  hpin: tuple, vpin: tuple, dbu: int = DBU) -> str:
    """A 'given' floorplan DEF for a soft macro: die, rows (N/FS alternating),
    pins. ``hpin`` / ``vpin``: (layer, track pitch, track offset, half width)
    of the pins on the left/right (horizontal layer) and top/bottom (vertical
    layer) edges. clk and ana_out on the bottom, ana_in on the top, other
    inputs left, outputs right."""
    u = lambda v: int(round(v * dbu))  # noqa: E731
    W, H = die
    x0 = margin_sites * site_w
    y0 = margin_rows * row_h
    nsites = int(round((W - 2 * x0) / site_w))
    nrows = int(round((H - 2 * y0) / row_h))
    lines = [
        "VERSION 5.8 ;",
        'DIVIDERCHAR "/" ;',
        'BUSBITCHARS "[]" ;',
        f"DESIGN {m.name} ;",
        f"UNITS DISTANCE MICRONS {dbu} ;",
        f"DIEAREA ( 0 0 ) ( {u(W)} {u(H)} ) ;",
    ]
    for r in range(nrows):
        orient = "N" if r % 2 == 0 else "FS"
        lines.append(f"ROW ROW_{r} {site} {u(x0)} {u(y0 + r * row_h)} {orient} DO {nsites} BY 1 STEP {u(site_w)} 0 ;")

    sides = {"left": [], "right": [], "top": [], "bottom": []}
    dirs = m.port_bits()
    for p in m.ports:
        for bit in p.bits():
            if p.name == "clk":
                sides["bottom"].append(bit)
            elif p.name == "ana_in":
                sides["top"].append(bit)
            elif p.name == "ana_out":
                sides["bottom"].append(bit)
            elif p.direction == "input":
                sides["left"].append(bit)
            else:
                sides["right"].append(bit)
    pins = []

    def spread(n, lo, hi, offset, pitch):
        tracks = int((hi - lo) / pitch)
        step = max(1, tracks // (n + 1))
        first = int((lo - offset) / pitch) + 1
        return [offset + (first + step * (k + 1)) * pitch for k in range(n)]

    for side, bits in sides.items():
        if side in ("left", "right"):
            layer, pitch, off, hw = hpin
            ys = spread(len(bits), y0, H - y0, off, pitch)
            for bit, yy in zip(bits, ys):
                xx = 0 if side == "left" else W
                rect = f"( 0 -{u(hw)} ) ( 1000 {u(hw)} )" if side == "left" else f"( -1000 -{u(hw)} ) ( 0 {u(hw)} )"
                pins.append((bit, layer, rect, xx, yy))
        else:
            layer, pitch, off, hw = vpin
            xs = spread(len(bits), x0, W - x0, off, pitch)
            for bit, xx in zip(bits, xs):
                yy = 0 if side == "bottom" else H
                rect = f"( -{u(hw)} 0 ) ( {u(hw)} 1000 )" if side == "bottom" else f"( -{u(hw)} -1000 ) ( {u(hw)} 0 )"
                pins.append((bit, layer, rect, xx, yy))
    lines.append(f"PINS {len(pins)} ;")
    for bit, layer, rect, xx, yy in pins:
        d = {"input": "INPUT", "output": "OUTPUT", "inout": "INOUT"}[dirs[bit]]
        use = "ANALOG" if bit.startswith("ana_") else ("CLOCK" if bit == "clk" else "SIGNAL")
        lines.append(f"- {bit} + NET {bit} + DIRECTION {d} + USE {use}")
        lines.append(f"  + PORT + LAYER {layer} {rect} + PLACED ( {u(xx)} {u(yy)} ) N ;")
    lines.append("END PINS")
    lines.append("END DESIGN")
    return "\n".join(lines) + "\n"


def dp8_floorplan_def(m: Module, die=(165.60, 163.20), margin_sites=22, margin_rows=4) -> str:
    """The 'given' floorplan DEF for the soft macro: die, rows, pins (sky130 hd:
    met3 pins left/right on the 0.68 um tracks, met2 top/bottom on 0.46 um)."""
    return floorplan_def(m, die, "unithd", SITE_W, ROW_H, margin_sites, margin_rows,
                         hpin=("met3", 0.68, 0.34, 0.15), vpin=("met2", 0.46, 0.23, 0.14))


DP8_SDC = """\
create_clock -name clk -period 10.0 [get_ports clk]
set_clock_uncertainty 0.1 [get_clocks clk]
set_input_delay 1.0 -clock clk [get_ports {a[*] b[*] x[*] y[*]}]
set_output_delay 1.0 -clock clk [get_ports {sum[*] prod[*] acc[*]}]
set_driving_cell -lib_cell sky130_fd_sc_hd__buf_2 [get_ports {a[*] b[*] x[*] y[*]}]
set_load 0.02 [get_ports {sum[*] prod[*] acc[*]}]
"""


# -----------------------------------------------------------------------------
# writer
# -----------------------------------------------------------------------------
def write_dp8(out_dir=INPUTS) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    m, meta = dp8_netlist()
    paths = {
        "netlist": os.path.join(out_dir, "dp8_flat.v"),
        "floorplan_def": os.path.join(out_dir, "dp8_floorplan.def"),
        "sdc": os.path.join(out_dir, "dp8.sdc"),
    }
    with changed_only(paths["netlist"]) as f:
        f.write("// generated by sipath.examples.designs -- flat gate-level netlist\n")
        f.write(m.to_verilog())
    with changed_only(paths["floorplan_def"]) as f:
        f.write(dp8_floorplan_def(m))
    with changed_only(paths["sdc"]) as f:
        f.write(DP8_SDC)
    return {**paths, **meta}


HS = "sky130_fd_sc_hs__"


def write_dp8_hs(out_dir=INPUTS) -> dict:
    """dp8 on the high-speed library: the same netlist with every cell taken
    from sky130_fd_sc_hs (identical names and functions), a floorplan on the hs
    rows (unit site, 0.48 x 3.33 um) with pins on the hs track grid."""
    os.makedirs(out_dir, exist_ok=True)
    m, meta = dp8_netlist()
    m.name = "dp8_hs"
    for i in m.instances:
        i.cell = i.cell.replace(HD, HS)
    paths = {"netlist": os.path.join(out_dir, "dp8_hs.v"), "floorplan_def": os.path.join(out_dir, "dp8_hs_floorplan.def"),
             "sdc": os.path.join(out_dir, "dp8_hs.sdc")}
    with changed_only(paths["netlist"]) as f:
        f.write("// generated by sipath.examples.designs -- dp8 on sky130_fd_sc_hs\n" + m.to_verilog())
    with changed_only(paths["floorplan_def"]) as f:
        # hs tracks.info: met3 Y 0.37/0.74, met2 X 0.24/0.48
        f.write(floorplan_def(m, (168.0, 166.5), "unit", 0.48, 3.33, margin_sites=21, margin_rows=3,
                              hpin=("met3", 0.74, 0.37, 0.15), vpin=("met2", 0.48, 0.24, 0.14)))
    with changed_only(paths["sdc"]) as f:
        f.write(DP8_SDC.replace(HD, HS))
    return {**paths, **meta}


# -----------------------------------------------------------------------------
# mcu8 die (ChipFoundry hard macros)
# -----------------------------------------------------------------------------
IO = "sky130_ef_io__"
CF = os.path.join(INPUTS, "chipfoundry")
SRAM = "CF_SRAM_1024x32"
ADC = "EF_ADCS1008NC"
DAC = "sky130_ef_ip__rdac3v_8bit"
CF_VIEWS = {
    SRAM: {"lef": os.path.join(CF, SRAM, "CF_SRAM_1024x32.lef"),
           "lib": os.path.join(CF, SRAM, "CF_SRAM_1024x32_tt_180V_25C.lib"),
           "lib_ss": os.path.join(CF, SRAM, "CF_SRAM_1024x32_ss_160V_100C.lib"),
           "lib_ff": os.path.join(CF, SRAM, "CF_SRAM_1024x32_ff_195V_n40C.lib")},
    ADC: {"lef_vendor": os.path.join(CF, ADC, "EF_ADCS1008NC.lef"),
          "lef": os.path.join(INPUTS, "EF_ADCS1008NC.annotated.lef")},
    DAC: {"lef": os.path.join(CF, DAC, "sky130_ef_ip__rdac3v_8bit.lef")},
}

MCU8_PADS = {  # edge -> ordered list of (port, pad master); passes sipath.padplan rules
    # core supply pairs on west and south; a VDDIO/VSSIO pair on every edge;
    # analog pads grouped on east with VDDA/VSSA, away from the outputs (north)
    "west": [("clk", "gpiov2_pad_wrapped"), ("rst_n", "gpiov2_pad_wrapped"), ("en", "gpiov2_pad_wrapped"),
             ("vccd_w", "vccd_lvc_clamped_pad"), ("vssd_w", "vssd_lvc_clamped_pad")] +
            [(f"gain[{i}]", "gpiov2_pad_wrapped") for i in range(4)] +
            [("vddio_w", "vddio_hvc_clamped_pad"), ("vssio_w", "vssio_hvc_clamped_pad")],
    "north": [(f"dout[{i}]", "gpiov2_pad_wrapped") for i in range(4)] +
             [("vddio_n", "vddio_hvc_clamped_pad"), ("vssio_n", "vssio_hvc_clamped_pad")] +
             [(f"dout[{i}]", "gpiov2_pad_wrapped") for i in range(4, 8)],
    "east": [("vddio_e", "vddio_hvc_clamped_pad"), ("vssio_e", "vssio_hvc_clamped_pad"), ("vssa", "vssa_hvc_clamped_pad"),
             ("vdda", "vdda_hvc_clamped_pad"), ("ana_in", "gpiov2_pad_wrapped"), ("vref_hi", "gpiov2_pad_wrapped"),
             ("vref_lo", "gpiov2_pad_wrapped"), ("dac_out", "gpiov2_pad_wrapped")],
    "south": [(f"cfg[{i}]", "gpiov2_pad_wrapped") for i in range(3)] +
             [("vccd_s", "vccd_lvc_clamped_pad"), ("vssd_s", "vssd_lvc_clamped_pad")] +
             [(f"ch[{i}]", "gpiov2_pad_wrapped") for i in range(3)] +
             [("vddio_s", "vddio_hvc_clamped_pad"), ("vssio_s", "vssio_hvc_clamped_pad")],
}

# Static configuration of every sky130_fd_io gpiov2 control pin (no pin left
# floating). Core-domain pins come from tie cells; *_H pins are in the VDDIO
# domain and are tied with the pad's own TIE_HI_ESD/TIE_LO_ESD outputs.
# Values follow the gpiov2 mode table (DM 001 input, 110 strong push-pull
# output, 000 disabled) -- check them against the IO datasheet for a tapeout.
GPIO_COMMON = {"ANALOG_EN": 0, "ANALOG_POL": 0, "ANALOG_SEL": 0, "HLD_OVR": 0, "IB_MODE_SEL": 0, "SLOW": 0,
               "VTRIP_SEL": 0, "ENABLE_VDDIO": 1}
GPIO_HV = {"ENABLE_H": "hi", "HLD_H_N": "hi", "ENABLE_VDDA_H": "hi", "ENABLE_INP_H": "lo", "ENABLE_VSWITCH_H": "lo"}
GPIO_MODE = {"input": {"DM": 0b001, "INP_DIS": 0, "OE_N": 1, "OUT": 0},
             "output": {"DM": 0b110, "INP_DIS": 1, "OE_N": 0},
             "analog": {"DM": 0b000, "INP_DIS": 1, "OE_N": 1, "OUT": 0}}
# Power pads are the *_clamped variants (as in Caravel's padframe): their ESD
# clamps are wired inside the cell. The plain *_hvc pads leave DRN_HVC and
# SRC_BDY_HVC as core-side tabs that must be routed (LVS showed them open).
PAD_SUPPLIES = {"^VCCD$": "VDD", "^VSSD$": "VSS", "^VDDA$": "VDDA", "^VSSA$": "VSSA", "^VDDIO$": "VDDIO",
                "^VSSIO$": "VSSIO"}

ANALOG_PORTS = ("ana_in", "dac_out", "vref_hi", "vref_lo")


def _pad_inst(port: str) -> str:
    return "pad_" + port.replace("[", "_").replace("]", "")


def sar_controller(m: Module, pfx: str, clk: str, rst_n: str, cmp_: str, bits: int = 10):
    """Gate-level SAR controller for a no-controller SAR front end.

    One-hot pointer p[bits..0] (p[bits] = sample phase, reset state). Each
    cycle the bit under the pointer resolves to CMP and the next bit is
    tried at 1; the result register loads when the pointer reaches bit 0.
    Returns (hold, data bits, result bits)."""
    p = [m.wire(f"{pfx}.p_{j}") for j in range(bits + 1)]
    for j in range(bits + 1):  # ring: p[j] <= p[j+1], p[bits] <= p[0]
        src = p[(j + 1) % (bits + 1)]
        if j == bits:
            m.inst(f"{pfx}.rp_{j}", HD + "dfstp_1", CLK=clk, D=src, SET_B=rst_n, Q=p[j])
        else:
            m.inst(f"{pfx}.rp_{j}", HD + "dfrtp_1", CLK=clk, D=src, RESET_B=rst_n, Q=p[j])
    d = [m.wire(f"{pfx}.d_{j}") for j in range(bits)]
    for j in range(bits):
        t = m.wire(f"{pfx}.t_{j}")
        m.inst(f"{pfx}.or_{j}", HD + "or2_1", A=p[j + 1], B=d[j], X=t)  # tried at 1 when pointer arrives
        if j < bits - 1:
            u = m.wire(f"{pfx}.u_{j}")
            m.inst(f"{pfx}.clr_{j}", HD + "and2b_1", A_N=p[bits], B=t, X=u)  # cleared in sample phase
        else:
            u = t
        n = m.wire(f"{pfx}.n_{j}")
        m.inst(f"{pfx}.mx_{j}", HD + "mux2_1", A0=u, A1=cmp_, S=p[j], X=n)  # resolve to CMP
        m.inst(f"{pfx}.rd_{j}", HD + "dfrtp_1", CLK=clk, D=n, RESET_B=rst_n, Q=d[j])
    res = [m.wire(f"{pfx}.res_{j}") for j in range(bits)]
    for j in range(bits):
        nxt = m.wire(f"{pfx}.rn_{j}")
        new = cmp_ if j == 0 else d[j]
        m.inst(f"{pfx}.rm_{j}", HD + "mux2_1", A0=res[j], A1=new, S=p[0], X=nxt)
        register(m, f"{pfx}.rr_{j}", clk, nxt, res[j])
    return p[bits], d, res


def mcu8_netlist() -> tuple[Module, dict]:
    m = Module("mcu8")
    for name, w in (("clk", 0), ("rst_n", 0), ("en", 0), ("gain", 4), ("dout", 8), ("ana_in", 0), ("dac_out", 0),
                    ("vref_hi", 0), ("vref_lo", 0), ("cfg", 3), ("ch", 3)):
        m.port(name, "inout", w)
    tie = m.wire("u_tie.lo")
    tie_hi = m.wire("u_tie.hi")
    m.inst("u_tie.c0", HD + "conb_1", LO=tie, HI=tie_hi)
    core_in, pads = {}, []
    kinds = {}
    for edge, lst in MCU8_PADS.items():
        for order, (port, master) in enumerate(lst):
            inst = _pad_inst(port)
            pads.append({"inst": inst, "edge": edge, "order": order, "master": IO + master, "port": port})
            if master != "gpiov2_pad_wrapped":
                m.inst(inst, IO + master)  # supply pad: pins join the ring/core nets via PAD_SUPPLIES
                continue
            kind = "analog" if port in ANALOG_PORTS else "output" if port.startswith("dout") else "input"
            kinds[inst] = kind
            conns = {"PAD": port}
            if kind == "analog":
                conns["PAD_A_ESD_0_H"] = m.wire(f"ana.{port}")
            elif kind == "output":
                conns["OUT"] = m.wire(f"core.{inst}_o")
            else:
                core_in[port] = conns["IN"] = m.wire(f"core.{inst}_i")
            hv = {"hi": m.wire(f"padtie.{inst}_hi"), "lo": m.wire(f"padtie.{inst}_lo")}
            conns["TIE_HI_ESD"], conns["TIE_LO_ESD"] = hv["hi"], hv["lo"]
            for pin, v in GPIO_HV.items():
                conns[pin] = hv[v]
            for pin, v in {**GPIO_COMMON, **GPIO_MODE[kind]}.items():
                if pin == "DM":
                    for b in range(3):
                        conns[f"DM[{b}]"] = tie_hi if (v >> b) & 1 else tie
                else:
                    conns[pin] = tie_hi if v else tie
            m.inst(inst, IO + master, **conns)
    clk, rst_n = core_in["clk"], core_in["rst_n"]

    # ADC front end + SAR controller (soft logic)
    cmp_ = m.wire("u_adc.cmp")
    hold, sar_d, sar_res = sar_controller(m, "u_sar", clk, rst_n, cmp_)
    adc_rst = m.wire("u_sar.adc_rst")
    m.inst("u_sar.rst_inv", HD + "inv_2", A=rst_n, Y=adc_rst)
    adc = {"HOLD": hold, "RST": adc_rst, "EN": core_in["en"], "CMP": cmp_, "VIN[0]": "ana.ana_in",
           "VH": "ana.vref_hi", "VL": "ana.vref_lo"}
    for i in range(1, 8):  # unused mux inputs: explicit no-connect wires
        adc[f"VIN[{i}]"] = m.wire(f"u_adc.vin_nc_{i}")
    for i in range(3):
        adc[f"B[{i}]"] = core_in[f"ch[{i}]"]
    for i in range(10):
        adc[f"DATA[{i}]"] = sar_d[i]
    m.inst("u_adc", ADC, **adc)

    # scale: sample (top 8 result bits) * gain + sample
    sq = sar_res[2:]
    gq = []
    for i in range(4):
        gq.append(m.wire(f"u_core.g_q_{i}"))
        register(m, f"u_core.rg_{i}", clk, core_in[f"gain[{i}]"], gq[-1])
    prod, mul_rows = array_multiplier(m, "u_mul", sq[:4], gq)
    sums, cout, adder = ripple_adder(m, "u_add", prod, sq, tie)
    wq = []
    for i in range(8):
        wq.append(m.wire(f"u_core.w_q_{i}"))
        register(m, f"u_core.rw_{i}", clk, sums[i], wq[-1])
    # 10-bit address counter
    aq, carry = [], core_in["cfg[0]"]
    for i in range(10):
        q = m.wire(f"u_cnt.q_{i}")
        s_ = m.wire(f"u_cnt.s_{i}")
        c = m.wire(f"u_cnt.c_{i}")
        m.inst(f"u_cnt.ha_{i}", HD + "ha_1", A=q, B=carry, SUM=s_, COUT=c)
        register(m, f"u_cnt.r_{i}", clk, s_, q)
        aq.append(q)
        carry = c

    # SRAM: tie-offs per the ChipFoundry pin description (normal operation)
    sram = {"CLKin": clk, "EN": core_in["cfg[1]"], "R_WB": core_in["cfg[2]"]}
    for pin in ("WLBI", "WLOFF", "TM", "SM", "ScanInCC", "ScanInDL", "ScanInDR", "vpwrpc", "vpwrac"):
        sram[pin] = tie
    for i in range(10):
        sram[f"AD[{i}]"] = aq[i]
    for i in range(32):
        sram[f"DI[{i}]"] = wq[i] if i < 8 else tie
        sram[f"BEN[{i}]"] = tie_hi
    rd = [m.wire(f"u_sram.do_{i}") for i in range(8)]
    for i in range(8):
        sram[f"DO[{i}]"] = rd[i]
    m.inst("u_sram", SRAM, **sram)

    oq = []
    for i in range(8):
        oq.append(m.wire(f"u_core.o_q_{i}"))
        register(m, f"u_core.ro_{i}", clk, rd[i], oq[-1])
        m.inst(f"u_core.ob_{i}", HD + "buf_4", A=oq[-1], X=f"core.{_pad_inst(f'dout[{i}]')}_o")
    m.inst("u_dac", DAC, ena=tie_hi, out="ana.dac_out", Vhigh="ana.vref_hi", Vlow="ana.vref_lo",
           **{f"b[{i}]": oq[i] for i in range(8)})
    meta = {"pads": pads, "adder_column": adder, "multiplier_rows": mul_rows,
            "analog_nets": [f"ana.{p}" for p in ANALOG_PORTS], "pad_kinds": kinds}
    return m, meta


# pin annotations for the ADC LEF (the vendor LEF has no DIRECTION/USE);
# taken from the vendor behavioral model EF_ADCS1008NC.v
ADC_PIN_ANNOTATION = {
    "VDD": ("INOUT", "POWER"), "VSS": ("INOUT", "GROUND"), "DVDD": ("INOUT", "POWER"), "DVSS": ("INOUT", "GROUND"),
    "VH": ("INOUT", "ANALOG"), "VL": ("INOUT", "ANALOG"), "HOLD": ("INPUT", "SIGNAL"), "RST": ("INPUT", "SIGNAL"),
    "EN": ("INPUT", "SIGNAL"), "CMP": ("OUTPUT", "SIGNAL"),
    **{f"VIN[{i}]": ("INOUT", "ANALOG") for i in range(8)},
    **{f"B[{i}]": ("INPUT", "SIGNAL") for i in range(3)},
    **{f"DATA[{i}]": ("INPUT", "SIGNAL") for i in range(10)},
}


def annotate_lef(src: str, annotations: dict[str, tuple[str, str]]) -> str:
    """Insert DIRECTION/USE into PIN blocks that lack them. Geometry untouched."""
    out, missing = [], set(annotations)
    for line in src.splitlines():
        out.append(line)
        tok = line.split()
        if len(tok) == 2 and tok[0] == "PIN" and tok[1] in annotations:
            d, u = annotations[tok[1]]
            indent = line[:len(line) - len(line.lstrip())] + "  "
            out += [f"{indent}DIRECTION {d} ;", f"{indent}USE {u} ;"]
            missing.discard(tok[1])
    if missing:
        raise ValueError(f"pins not in LEF: {sorted(missing)}")
    return "\n".join(out) + "\n"


def blackbox_liberty() -> str:
    """Pin directions and loads for the analog macros (no vendor Liberty) and
    the wrapped GPIO pad, so STA and the resizer see drivers and loads. No
    timing arcs are invented except a fixed pad buffer delay."""
    def pin(name, d, cap=0.005, extra=""):
        return f'    pin ("{name}") {{ direction : {d}; capacitance : {cap}; {extra} }}\n'

    def bits(base, n, d, cap=0.005):
        return f"    bus ({base}) {{ bus_type : bus{n}; direction : {d}; capacitance : {cap}; }}\n"

    arc = ('timing () { related_pin : "%s"; timing_sense : positive_unate; '
           'cell_rise (scalar) { values ("0.5"); } cell_fall (scalar) { values ("0.5"); } '
           'rise_transition (scalar) { values ("0.2"); } fall_transition (scalar) { values ("0.2"); } }')
    out = ['library (mcu8_blackboxes) {', '  delay_model : table_lookup;', '  time_unit : "1ns";',
           '  voltage_unit : "1V";', '  current_unit : "1mA";', '  capacitive_load_unit (1, pf);',
           '  pulling_resistance_unit : "1kohm";', '  nom_voltage : 1.8; nom_temperature : 25; nom_process : 1;',
           '  default_max_transition : 1.5;',
           *[f'  type (bus{n}) {{ base_type : array; data_type : bit; bit_width : {n}; bit_from : {n - 1}; '
             f'bit_to : 0; downto : true; }}' for n in (3, 8, 10)],
           f'  cell ("{ADC}") {{', '    area : 180576;', '    dont_touch : true; dont_use : true;',
           pin("HOLD", "input"), pin("RST", "input"), pin("EN", "input"), pin("CMP", "output"),
           bits("B", 3, "input"), bits("DATA", 10, "input"), bits("VIN", 8, "inout", 0.1),
           pin("VH", "inout", 0.1), pin("VL", "inout", 0.1), '  }',
           f'  cell ("{DAC}") {{', '    area : 13221;', '    dont_touch : true; dont_use : true;',
           pin("ena", "input"), bits("b", 8, "input"), pin("out", "inout", 0.1),
           pin("Vhigh", "inout", 0.1), pin("Vlow", "inout", 0.1), '  }',
           '  cell ("sky130_ef_io__gpiov2_pad_wrapped") {', '    area : 16877;', '    dont_touch : true; dont_use : true;',
           pin("PAD", "inout", 5.0, arc % "OUT"), pin("IN", "output", 0.005, arc % "PAD"), pin("OUT", "input"),
           pin("OE_N", "input"), pin("PAD_A_ESD_0_H", "inout", 0.5),
           pin("TIE_HI_ESD", "output"), pin("TIE_LO_ESD", "output"), bits("DM", 3, "input"),
           *[pin(p, "input") for p in sorted(set(GPIO_COMMON) | set(GPIO_HV) | {"INP_DIS"})], '  }', '}']
    return "\n".join(out) + "\n"


MCU8_SDC = """\
create_clock -name clk -period 20.0 [get_pins pad_clk/IN]
set_clock_uncertainty 0.2 [get_clocks clk]
set_input_delay 2.0 -clock clk [get_ports {rst_n en gain[*] cfg[*] ch[*]}]
set_output_delay 2.0 -clock clk [get_ports {dout[*]}]
"""


def write_mcu8(out_dir=INPUTS) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    for views in CF_VIEWS.values():
        for k, f in views.items():
            if k != "lef" or "annotated" not in f:
                if not os.path.isfile(f):
                    raise FileNotFoundError(f"ChipFoundry view missing: {f} (see inputs/chipfoundry/SOURCES.txt)")
    m, meta = mcu8_netlist()
    paths = {
        "netlist": os.path.join(out_dir, "mcu8_flat.v"),
        "sdc": os.path.join(out_dir, "mcu8.sdc"),
        "blackbox_lib": os.path.join(out_dir, "mcu8_blackboxes.lib"),
        "adc_lef": CF_VIEWS[ADC]["lef"], "dac_lef": CF_VIEWS[DAC]["lef"],
        "sram_lef": CF_VIEWS[SRAM]["lef"], "sram_lib": CF_VIEWS[SRAM]["lib"],
        "sram_libs": {"ss": CF_VIEWS[SRAM]["lib_ss"], "tt": CF_VIEWS[SRAM]["lib"], "ff": CF_VIEWS[SRAM]["lib_ff"]},
    }
    with changed_only(paths["netlist"]) as f:
        f.write("// generated by sipath.examples.designs -- flat gate-level netlist, pads and ChipFoundry macros\n")
        f.write(m.to_verilog())
    with changed_only(paths["sdc"]) as f:
        f.write(MCU8_SDC)
    with open(CF_VIEWS[ADC]["lef_vendor"]) as f:
        annotated = annotate_lef(f.read(), ADC_PIN_ANNOTATION)
    with changed_only(paths["adc_lef"]) as f:
        f.write(annotated)
    with changed_only(paths["blackbox_lib"]) as f:
        f.write(blackbox_liberty())
    for stale in ("adc8.lef", "dac8.lef"):
        if os.path.exists(os.path.join(out_dir, stale)):
            os.remove(os.path.join(out_dir, stale))
    return {**paths, **meta}
