# aes for the strategy benchmark: ORFS's constraint.sdc (designs/sky130hd/aes) with a period SiPath's
# nine-corner signoff can meet; everything else as ORFS has it.
current_design aes_cipher_top

set clk_name clk
set clk_port_name clk
# ORFS benchmark: 3.6 ns, met at tt only; 6.5 ns closes the slow corner (ss_max needed ~6.0)
set clk_period 6.5
set clk_io_pct 0.2

set clk_port [get_ports $clk_port_name]

create_clock -name $clk_name -period $clk_period $clk_port
set clk_io_name vclk_$clk_name
create_clock -name $clk_io_name -period $clk_period
set_clock_latency 0.710 [get_clocks $clk_name]
set_clock_latency 0.710 [get_clocks $clk_io_name]

set non_clock_inputs [all_inputs -no_clocks]

set_input_delay [expr $clk_period * $clk_io_pct] -clock $clk_io_name $non_clock_inputs
set_output_delay [expr $clk_period * $clk_io_pct] -clock $clk_io_name [all_outputs]
