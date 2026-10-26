// OpenFrame UART echo: GPIO0 clock, GPIO1 RX, GPIO2 TX, GPIO3 activity.
`default_nettype none
`ifndef OPENFRAME_IO_PADS
`define OPENFRAME_IO_PADS 44
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
  // Echo each received byte through the register interface. GPIO3 toggles
  // when a byte is accepted by TX; only GPIO0/1 inputs and GPIO2/3 outputs.
  localparam IN_LO = 0, IN_HI = 1, OUT_LO = 2, OUT_HI = 3;
  wire tx, ready;
  wire [7:0] data;
  reg sending, activity;
  reg [7:0] byte_to_send;
  uart_fifo u_uart(.clk(gpio_in[0]), .rst_n(resetb_l), .rx_i(gpio_in[1]), .tx_o(tx),
      .valid(1'b1), .write(sending), .addr(2'b0), .wdata(byte_to_send),
      .ready(ready), .rdata(data));
  always @(posedge gpio_in[0] or negedge resetb_l) begin
      if (!resetb_l) begin sending <= 0; byte_to_send <= 0; activity <= 0; end
      else if (ready) begin
          sending <= !sending;
          if (!sending) byte_to_send <= data;
          else activity <= !activity;
      end
  end

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
      if (i == 2) begin : serial_tx
        assign gpio_out[i] = tx;
      end else if (i == 3) begin : accepted
        assign gpio_out[i] = activity;
      end else begin : off
        assign gpio_out[i] = gpio_loopback_zero[i];
      end
    end
  endgenerate
endmodule

`default_nettype wire
