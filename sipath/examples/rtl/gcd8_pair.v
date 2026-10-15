// gcd8_pair: two hardened gcd8 macros (python3 -m sipath harden gcd8 ...) and a
// little glue logic around them: the sum of the two results and a flag when
// both are done. gcd8 is a black box here: its Liberty (from the hardened
// views) gives synthesis the ports and timing.
`default_nettype none

(* blackbox *)
module gcd8 (
  input  wire       clk,
  input  wire       rst_n,
  input  wire       start,
  input  wire [7:0] a,
  input  wire [7:0] b,
  output wire [7:0] result,
  output wire       done
);
endmodule

module gcd8_pair (
  input  wire       clk,
  input  wire       rst_n,
  input  wire       start,
  input  wire [7:0] a0,
  input  wire [7:0] b0,
  input  wire [7:0] a1,
  input  wire [7:0] b1,
  output wire [7:0] result0,
  output wire [7:0] result1,
  output reg  [8:0] sum,
  output reg        both_done
);
  wire done0, done1;
  gcd8 u_gcd0 (.clk(clk), .rst_n(rst_n), .start(start), .a(a0), .b(b0), .result(result0), .done(done0));
  gcd8 u_gcd1 (.clk(clk), .rst_n(rst_n), .start(start), .a(a1), .b(b1), .result(result1), .done(done1));

  always @(posedge clk or negedge rst_n)
    if (!rst_n) begin
      sum <= 9'd0;
      both_done <= 1'b0;
    end else begin
      both_done <= done0 & done1;
      if (done0 & done1) sum <= result0 + result1;
    end
endmodule
