create_clock -name clk -period 10.0 [get_ports clk]
set_clock_uncertainty 0.1 [get_clocks clk]
set_input_delay 1.0 -clock clk [get_ports {rst_n start a0[*] b0[*] a1[*] b1[*]}]
set_output_delay 1.0 -clock clk [get_ports {result0[*] result1[*] sum[*] both_done}]
