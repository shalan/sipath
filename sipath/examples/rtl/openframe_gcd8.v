// openframe_gcd8: gcd8 in the ChipFoundry OpenFrame wrapper.
// With W = GCD_W, the pads from 0: clk, start, a (W), b (W) as inputs; result
// (W), done as outputs; the rest unused (buffers off). With GCD_W = 4 that is
// pads 0-14, the frame's east edge. Reset from the frame's resetb_l. Every pad
// control is looped back from the frame's per-pad constants
// (gpio_loopback_one/zero), as OpenFrame recommends, so the wrapper needs no
// tie cells. Ports follow sipath/harnesses/sources/openframe/__openframe_project_wrapper.v.
`default_nettype none
`ifndef OPENFRAME_IO_PADS
`define OPENFRAME_IO_PADS 44
`endif
`ifndef GCD_W
`define GCD_W 8
`endif

module openframe_project_wrapper (
    input         porb_h,
    input         porb_l,
    input         por_l,
    input         resetb_h,
    input         resetb_l,
    input  [31:0] mask_rev,
    input  [`OPENFRAME_IO_PADS-1:0] gpio_in,
    input  [`OPENFRAME_IO_PADS-1:0] gpio_in_h,
    output [`OPENFRAME_IO_PADS-1:0] gpio_out,
    output [`OPENFRAME_IO_PADS-1:0] gpio_oeb,
    output [`OPENFRAME_IO_PADS-1:0] gpio_inp_dis,
    output [`OPENFRAME_IO_PADS-1:0] gpio_ib_mode_sel,
    output [`OPENFRAME_IO_PADS-1:0] gpio_vtrip_sel,
    output [`OPENFRAME_IO_PADS-1:0] gpio_slow_sel,
    output [`OPENFRAME_IO_PADS-1:0] gpio_holdover,
    output [`OPENFRAME_IO_PADS-1:0] gpio_analog_en,
    output [`OPENFRAME_IO_PADS-1:0] gpio_analog_sel,
    output [`OPENFRAME_IO_PADS-1:0] gpio_analog_pol,
    output [`OPENFRAME_IO_PADS-1:0] gpio_dm2,
    output [`OPENFRAME_IO_PADS-1:0] gpio_dm1,
    output [`OPENFRAME_IO_PADS-1:0] gpio_dm0,
    inout  [`OPENFRAME_IO_PADS-1:0] analog_io,
    inout  [`OPENFRAME_IO_PADS-1:0] analog_noesd_io,
    input  [`OPENFRAME_IO_PADS-1:0] gpio_loopback_one,
    input  [`OPENFRAME_IO_PADS-1:0] gpio_loopback_zero
);
  localparam W = `GCD_W;
  localparam A_LO = 2, B_LO = A_LO + W, R_LO = B_LO + W, DONE = R_LO + W;
  localparam IN_LO = 0, IN_HI = R_LO - 1, OUT_LO = R_LO, OUT_HI = DONE;

  wire [W-1:0] result;
  wire         done;
  gcd8 u_gcd (.clk(gpio_in[0]), .rst_n(resetb_l), .start(gpio_in[1]),
              .a(gpio_in[B_LO-1:A_LO]), .b(gpio_in[R_LO-1:B_LO]), .result(result), .done(done));

  genvar i;
  generate
    for (i = 0; i < `OPENFRAME_IO_PADS; i = i + 1) begin : pad
      localparam IS_IN = (i >= IN_LO && i <= IN_HI);
      localparam IS_OUT = (i >= OUT_LO && i <= OUT_HI);
      // output enable (active low): only the outputs drive
      assign gpio_oeb[i] = IS_OUT ? gpio_loopback_zero[i] : gpio_loopback_one[i];
      // input buffer disabled except on the inputs
      assign gpio_inp_dis[i] = IS_IN ? gpio_loopback_zero[i] : gpio_loopback_one[i];
      // drive mode: 001 input, 110 strong push-pull output, 000 off
      assign gpio_dm2[i] = IS_OUT ? gpio_loopback_one[i] : gpio_loopback_zero[i];
      assign gpio_dm1[i] = IS_OUT ? gpio_loopback_one[i] : gpio_loopback_zero[i];
      assign gpio_dm0[i] = IS_IN ? gpio_loopback_one[i] : gpio_loopback_zero[i];
      assign gpio_ib_mode_sel[i] = gpio_loopback_zero[i];
      assign gpio_vtrip_sel[i] = gpio_loopback_zero[i];
      assign gpio_slow_sel[i] = gpio_loopback_zero[i];
      assign gpio_holdover[i] = gpio_loopback_zero[i];
      assign gpio_analog_en[i] = gpio_loopback_zero[i];
      assign gpio_analog_sel[i] = gpio_loopback_zero[i];
      assign gpio_analog_pol[i] = gpio_loopback_zero[i];
      if (i >= R_LO && i < DONE) begin : res
        assign gpio_out[i] = result[i - R_LO];
      end else if (i == DONE) begin : dn
        assign gpio_out[i] = done;
      end else begin : off
        assign gpio_out[i] = gpio_loopback_zero[i];
      end
    end
  endgenerate
endmodule
