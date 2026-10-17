// gcd8_sv: gcd8 written in SystemVerilog (package, enum state, packed struct,
// a function, always_ff/always_comb, unique case), for SiPath's slang front
// end (synth frontend auto/slang). start loads a and b; done rises with result.
module gcd8_sv
  import gcd_sv_pkg::*;
(
  input  logic       clk,
  input  logic       rst_n,
  input  logic       start,
  input  logic [7:0] a,
  input  logic [7:0] b,
  output logic [7:0] result,
  output logic       done
);
  state_e state, state_d;
  pair_t  p, p_d;

  always_comb begin
    state_d = state;
    p_d     = p;
    unique case (state)
      IDLE:   if (start) begin p_d = '{x: a, y: b}; state_d = RUN; end
      RUN:    if (p.y == '0) state_d = FINISH; else p_d = step(p);
      FINISH: state_d = IDLE;
      default: state_d = IDLE;
    endcase
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state  <= IDLE;
      p      <= '0;
      result <= '0;
      done   <= 1'b0;
    end else begin
      state <= state_d;
      p     <= p_d;
      done  <= (state == RUN) && (p.y == '0);
      if ((state == RUN) && (p.y == '0)) result <= p.x;
    end
  end
endmodule
