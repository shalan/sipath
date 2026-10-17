// gcd_sv_pkg: types for gcd8_sv (SiPath's SystemVerilog example, read with slang).
package gcd_sv_pkg;
  typedef enum logic [1:0] {IDLE = 2'd0, RUN = 2'd1, FINISH = 2'd2} state_e;

  typedef struct packed {
    logic [7:0] x;
    logic [7:0] y;
  } pair_t;

  // one subtract-and-swap step of Euclid's algorithm
  function automatic pair_t step(pair_t p);
    if (p.x < p.y) return '{x: p.y, y: p.x};
    return '{x: p.x - p.y, y: p.y};
  endfunction
endpackage
