`timescale 1ns/1ps
module tb;
    reg clk = 0;
    always #5 clk = ~clk;
    reg resetb_l = 0, start = 0;
    reg [3:0] a = 0, b = 0;
    wire [43:0] gpio_in = {34'b0, b, a, start, clk};
    wire [43:0] gpio_out, gpio_oeb, gpio_inp_dis, gpio_dm2, gpio_dm1, gpio_dm0;
    wire [43:0] gpio_ib_mode_sel, gpio_vtrip_sel, gpio_slow_sel, gpio_holdover;
    wire [43:0] gpio_analog_en, gpio_analog_sel, gpio_analog_pol;
    wire [43:0] analog_io, analog_noesd_io;
    wire porb_h = 1, porb_l = 1, por_l = 0, resetb_h = 1;
    wire [31:0] mask_rev = 0;
    wire [43:0] gpio_in_h = 0, gpio_loopback_one = {44{1'b1}}, gpio_loopback_zero = 0;
    openframe_project_wrapper dut (.*);
    integer x, y, cycles;
    function integer gcd(input integer p, input integer q);
        integer r;
        begin
            while (q != 0) begin r = p % q; p = q; q = r; end
            gcd = p;
        end
    endfunction
    initial begin
        #2; resetb_l = 1; #1; resetb_l = 0; #3; resetb_l = 1;
        for (x = 0; x < 16; x = x + 1)
            for (y = 0; y < 16; y = y + 1) begin
                @(negedge clk); a = x; b = y; start = 1;
                @(negedge clk); start = 0;
                cycles = 0;
                while (gpio_out[14] !== 1'b1 && cycles < 200) begin
                    @(negedge clk); cycles = cycles + 1;
                end
                if (cycles == 200 || gpio_out[13:10] !== 4'(gcd(x, y)))
                    $fatal(1, "GCD mismatch %0d %0d", x, y);
                if (gpio_oeb !== ~44'h7c00 || gpio_inp_dis !== ~44'h3ff ||
                    gpio_dm2 !== 44'h7c00 || gpio_dm1 !== 44'h7c00 || gpio_dm0 !== 44'h3ff)
                    $fatal(1, "pad direction/drive mode mismatch");
                if (gpio_ib_mode_sel !== 0 || gpio_vtrip_sel !== 0 || gpio_slow_sel !== 0 ||
                    gpio_holdover !== 0 || gpio_analog_en !== 0 || gpio_analog_sel !== 0 ||
                    gpio_analog_pol !== 0 || (gpio_out & ~44'h7c00) !== 0)
                    $fatal(1, "unused pad/control mismatch");
                @(negedge clk); // allow done to deassert before the next transaction
            end
        $display("PASS openframe: all 256 input pairs and template pad controls");
        $finish;
    end
    initial begin #1000000; $fatal(1, "timeout"); end
endmodule
