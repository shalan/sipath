// sram8: eight sky130 OpenRAM 1 KB macros (32x256, 1rw1r) and the logic
// between them, SiPath's many-macro example (macro placement methods).
// Writes go to the bank waddr[10:8] selects; every cycle all eight banks are
// read at raddr on port 1 and combined pairwise (0+1, 2+3, 4+5, 6+7) then
// XORed (q), and port 0 of the last written bank is read back registered (r).
`default_nettype none
module sram8 (
  input  wire        clk,
  input  wire        rst_n,
  input  wire        en,
  input  wire        we,
  input  wire [10:0] waddr,
  input  wire [31:0] wdata,
  input  wire [7:0]  raddr,
  output reg  [31:0] q,
  output reg  [31:0] r
);
  wire [7:0]  sel = en ? (8'b1 << waddr[10:8]) : 8'b0;
  wire [31:0] d0 [0:7];
  wire [31:0] d1 [0:7];
  genvar i;
  generate
    for (i = 0; i < 8; i = i + 1) begin : bank
      sky130_sram_1kbyte_1rw1r_32x256_8 u_ram (
        .clk0(clk), .csb0(~sel[i]), .web0(~we), .wmask0(4'hf), .addr0(waddr[7:0]),
        .din0(wdata ^ {4{i[7:0]}}), .dout0(d0[i]),
        .clk1(clk), .csb1(~en), .addr1(raddr), .dout1(d1[i]));
    end
  endgenerate
  reg [2:0] bank_q;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      q <= 32'd0;
      r <= 32'd0;
      bank_q <= 3'd0;
    end else begin
      q <= (d1[0] + d1[1]) ^ (d1[2] + d1[3]) ^ (d1[4] + d1[5]) ^ (d1[6] + d1[7]);
      bank_q <= waddr[10:8];
      r <= d0[bank_q];
    end
  end
endmodule
