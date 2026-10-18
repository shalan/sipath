create_clock -name clk -period 20.0 [get_ports clk]
set_clock_uncertainty 0.2 [get_clocks clk]
set_input_delay 4.0 -clock clk [get_ports {rst_n en we waddr[*] wdata[*] raddr[*]}]
set_output_delay 4.0 -clock clk [get_ports {q[*] r[*]}]
