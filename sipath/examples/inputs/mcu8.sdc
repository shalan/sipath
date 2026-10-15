create_clock -name clk -period 20.0 [get_pins pad_clk/IN]
set_clock_uncertainty 0.2 [get_clocks clk]
set_input_delay 2.0 -clock clk [get_ports {rst_n en gain[*] cfg[*] ch[*]}]
set_output_delay 2.0 -clock clk [get_ports {dout[*]}]
