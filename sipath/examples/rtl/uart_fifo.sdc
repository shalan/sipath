create_clock -name clk -period 20.0 [get_ports clk]
set_clock_uncertainty 0.2 [get_clocks clk]
set_input_delay 2.0 -clock clk [get_ports {rst_n rx_i valid write addr[*] wdata[*]}]
set_output_delay 2.0 -clock clk [get_ports {tx_o ready rdata[*]}]
