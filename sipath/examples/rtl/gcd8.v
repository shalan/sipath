// gcd8: greatest common divisor of two 8-bit numbers (subtract and swap).
// start loads a and b; done rises with result when the loop ends.
// Written for flow's RTL example (synthesis -> signoff on sky130A).
`define GCD_W 8

module gcd8 (
  input  wire                clk,
  input  wire                rst_n,
  input  wire                start,
  input  wire [`GCD_W-1:0]   a,
  input  wire [`GCD_W-1:0]   b,
  output reg  [`GCD_W-1:0]   result,
  output reg                 done
);
  localparam IDLE = 2'd0, RUN = 2'd1, DONE = 2'd2;

  reg [1:0]          state;
  reg [`GCD_W-1:0]   x, y;

  wire x_lt_y = x < y;
  wire y_zero = (y == {`GCD_W{1'b0}});

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state  <= IDLE;
      x      <= {`GCD_W{1'b0}};
      y      <= {`GCD_W{1'b0}};
      result <= {`GCD_W{1'b0}};
      done   <= 1'b0;
    end else begin
      case (state)
        IDLE: begin
          done <= 1'b0;
          if (start) begin
            x     <= a;
            y     <= b;
            state <= RUN;
          end
        end
        RUN: begin
          if (y_zero) begin
            result <= x;
            state  <= DONE;
          end else if (x_lt_y) begin
            x <= y;          // swap so that x >= y
            y <= x;
          end else begin
            x <= x - y;
          end
        end
        default: begin       // DONE
          done  <= 1'b1;
          state <= IDLE;
        end
      endcase
    end
  end
endmodule
