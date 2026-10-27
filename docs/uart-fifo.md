# UART with FIFOs: macro and OpenFrame echo

These examples implement a single-clock 8N1 UART: one start bit, eight data
bits (least significant first), and one stop bit. The clock divider is fixed
at elaboration (`CLKS_PER_BIT=16`, supported values 4 through 255); there is no
programmable baud register, parity, interrupt, flow control, or bus bridge.
At the example's 20 ns clock, the nominal baud rate is 3.125 Mbit/s. This is
an example timing target, not a maximum-frequency or board-level guarantee.

The RX input passes through two synchronizer flops and is sampled at bit
centres. The SDC provides a 2 ns input/output budget without cutting RX
paths. This does not constitute CDC/metastability or analog pad validation.
Reset is active-low asynchronous; external systems must release it according
to their reset synchronization policy. All protocol logic uses the same clock.

## Register interface

A request transfers at a rising edge with `valid && ready`. Hold `write`,
`addr`, and `wdata` stable until accepted; read data is sampled on that edge.
A request held valid over multiple ready edges transfers multiple times.

| Address | Read | Write |
|---|---|---|
| 0 | Pop RX FIFO; stalls while empty | Push TX FIFO; stalls while full |
| 1 | Status | Write one to bits 3/4 to clear their sticky errors |
| 2 | Clocks per bit (16) | Ignored |
| 3 | Zero | Ignored |

Status bits are: 0 RX data available; 1 TX space available; 2 TX shifter busy;
3 framing error; 4 RX overrun; 7:5 zero. Each FIFO holds four bytes; TX can
also hold one byte in its shifter. A full RX FIFO drops the arriving byte and
sets overrun, even if software pops a byte on that same edge. A bad stop bit
sets framing error and discards the frame. New errors take precedence over
simultaneous clearing. Reset clears queues/errors and aborts active frames;
TX returns high. Short false starts are discarded.

## Standalone macro

`uart_fifo.yaml` uses the reference flow through metal fill, extraction,
configured signoff, and `views`. `macro.yaml`, LEF, GDS, per-corner Liberty,
SPEF and netlists are exported by the views step. The example explicitly
configures clock clustering/buffer spacing to meet the PDK fanout limit;
the default CTS configuration for other designs is unchanged.

```bash
python3 -m sipath run sipath/examples/uart_fifo.yaml runs/uart --plan
python3 -m sipath run sipath/examples/uart_fifo.yaml runs/uart
python3 -m sipath report runs/uart
```

## OpenFrame echo

Install/configure the locked Magic and cf-precheck images first and check
`python3 -m sipath doctor`. `--plan` does not check image availability.
`openframe_uart.yaml` uses the pinned OpenFrame template and existing pad
loopback controls. GPIO0 is the clock, GPIO1 RX, GPIO2 TX, and GPIO3 toggles
when the echo controller writes a received byte into TX. Reset comes from
`resetb_l`. Other GPIO buffers are disabled. The wrapper internally consumes
the register interface; this example does not expose a processor bus on pads.

```bash
python3 -m sipath run sipath/examples/openframe_uart.yaml runs/openframe-uart --plan
python3 -m sipath run sipath/examples/openframe_uart.yaml runs/openframe-uart
```

Required wrapper checks precede mandatory cf-precheck and chip integration
with the seal ring. As in the GCD example, Netgen LVS in precheck replaces
KLayout LVS; the aggregate verdict still says `omitted`. Full-chip DRC and
optional Magic precheck DRC are not enabled. Assembly checks do not replace
those checks. Unused pads use drive mode `000` with both digital buffers
disabled. cf-precheck classifies these as `USER_STD_ANALOG` and can report
missing-analog-connection warnings for GPIO4–43; they have no analog load by
design. Its structured OEB result must still pass with zero errors.

Echo throughput can be limited by baud mismatch and finite FIFO
capacity; overrun recovery belongs in a system using the register interface.

## Acceptance and tests

`RouteSearchRtl` runs the shared RTL test hook (Icarus required):

```bash
python3 -m unittest -v sipath.tests.test_route_search.RouteSearchRtl
python3 -m sipath.tests.run_live -j 1 LiveUart --keep runs/live-uart
python3 -m sipath.tests.run_live -j 1 LiveOpenFrameUart --keep runs/live-uart
```

Simulation checks serialization and byte order, FIFO wrap/full/empty behavior,
backpressure, RX overrun, false starts, framing errors, W1C status, reset during
traffic and recovery, plus OpenFrame echo and all pad controls. Synthesis uses
the existing ten-cycle bounded equivalence check; it is not an unbounded proof
of UART operation. Full frames are exercised by simulation.

Acceptance requires all configured macro signoff checks and reusable views;
the wrapper additionally requires cf-precheck (including LVS) and chip checks.
Runtime figures are observations, not acceptance thresholds across machines.
The macro live test runs nightly in blocks; OpenFrame UART is dispatch-only
and shares image builds with other OpenFrame tests. Choose dispatch input
`openframe_extra: uart`, or `classes: LiveOpenFrameUart` for only that class.
OpenFrame classes run serially. Both UART live tests enforce the measured
2026-10-26 timing, DRC, wirelength and instance limits in `limits.yaml`, using
the standard `suggest` margins. These are regression guards from initial good
runs, not demonstrated variation bounds across machines; CI ignores runtime.
