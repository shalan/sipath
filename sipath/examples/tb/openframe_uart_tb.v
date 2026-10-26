`timescale 1ns/1ps
module tb;
    reg clk = 0;
    always #5 clk = ~clk;
    reg resetb_l = 0, rx = 1;
    wire [43:0] gpio_in = {42'b0, rx, clk};
    wire [43:0] gpio_out, gpio_oeb, gpio_inp_dis, gpio_dm2, gpio_dm1, gpio_dm0;
    wire [43:0] gpio_ib_mode_sel, gpio_vtrip_sel, gpio_slow_sel, gpio_holdover;
    wire [43:0] gpio_analog_en, gpio_analog_sel, gpio_analog_pol;
    wire [43:0] analog_io, analog_noesd_io;
    wire porb_h = 1, porb_l = 1, por_l = 0, resetb_h = 1;
    wire [31:0] mask_rev = 0;
    wire [43:0] gpio_in_h = 0, gpio_loopback_one = {44{1'b1}}, gpio_loopback_zero = 0;
    openframe_project_wrapper dut (.*);
    integer i,b;
    reg [7:0] decoded;
    task send(input [7:0] byte_in);
        integer n;
        begin
            @(negedge clk); rx=0; repeat(16) @(negedge clk);
            for(n=0;n<8;n=n+1) begin rx=byte_in[n]; repeat(16) @(negedge clk); end
            rx=1; repeat(16) @(negedge clk);
        end
    endtask
    task receive(input [7:0] expected);
        integer n;
        begin
            @(negedge gpio_out[2]); #80;
            if(gpio_out[2]!==0) $fatal(1,"echo start");
            for(n=0;n<8;n=n+1) begin #160; decoded[n]=gpio_out[2]; end
            #160;
            if(gpio_out[2]!==1 || decoded!==expected) $fatal(1,"echo byte %h expected %h",decoded,expected);
        end
    endtask
    initial begin
        #2; resetb_l=1; #1; resetb_l=0; #3; resetb_l=1;
        for(i=0;i<8;i=i+1) begin
            fork send(8'(i*37)); receive(8'(i*37)); join
            if(gpio_oeb!==~44'hc || gpio_inp_dis!==~44'h3 ||
               gpio_dm2!==44'hc || gpio_dm1!==44'hc || gpio_dm0!==44'h3)
                $fatal(1,"pad directions");
            if(gpio_analog_en!==0 || gpio_analog_sel!==0 || gpio_analog_pol!==0 ||
               gpio_ib_mode_sel!==0 || gpio_vtrip_sel!==0 || gpio_slow_sel!==0 ||
               gpio_holdover!==0 || (gpio_out & ~44'hc)!==0)
                $fatal(1,"pad controls");
            if(gpio_out[3]!==1'((i+1)%2)) $fatal(1,"activity toggle");
            repeat(32) @(negedge clk);
        end
        $display("PASS OpenFrame UART: serial echo and pad controls"); $finish;
    end
    initial begin #1000000; $fatal(1,"watchdog"); end
endmodule
