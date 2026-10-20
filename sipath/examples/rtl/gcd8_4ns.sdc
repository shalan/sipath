create_clock -name clk -period 4.0 [get_ports clk]
set_clock_uncertainty 0.1 [get_clocks clk]
set_input_delay 0.4 -clock clk [get_ports {rst_n start a[*] b[*]}]
set_output_delay 0.4 -clock clk [get_ports {result[*] done}]
