create_clock -name clk -period 20.0 [get_ports {gpio_in[0]}]
set_clock_uncertainty 0.2 [get_clocks clk]
set data_inputs {}
foreach port [get_ports {gpio_in[*] resetb_l}] {
    if {[get_name $port] ne {gpio_in[0]}} { lappend data_inputs $port }
}
set_input_delay 2.0 -clock clk $data_inputs
set_output_delay 2.0 -clock clk [get_ports {gpio_out[*]}]
