`timescale 1ns/1ps
module tb;
    reg clk = 0;
    always #5 clk = ~clk;
    reg rst_n = 0, start = 0;
    reg [7:0] a = 0, b = 0;
    wire [7:0] result;
    wire done;
    gcd8 dut (.*);
    integer i, cycles, seed = 12345;
    function integer gcd(input integer p, input integer q);
        integer r;
        begin
            while (q != 0) begin r = p % q; p = q; q = r; end
            gcd = p;
        end
    endfunction
    task check(input integer x, input integer y);
        begin
            @(negedge clk); a = x; b = y; start = 1;
            @(negedge clk); start = 0;
            cycles = 0;
            while (done !== 1'b1 && cycles < 600) begin
                @(negedge clk); cycles = cycles + 1;
            end
            if (cycles == 600 || result !== 8'(gcd(x, y)))
                $fatal(1, "gcd(%0d,%0d) = %0d", x, y, result);
            @(negedge clk);
        end
    endtask
    initial begin
        #2; rst_n = 1; #1; rst_n = 0; #3; rst_n = 1;
        check(0, 0); check(255, 0); check(0, 255);
        check(255, 1); check(1, 255); check(255, 255);
        check(252, 168); check(251, 253);
        for (i = 0; i < 256; i = i + 1)
            check($random(seed) & 255, $random(seed) & 255);
        // Reset while a long subtraction sequence is active.
        @(negedge clk); a = 255; b = 1; start = 1;
        @(negedge clk); start = 0;
        @(negedge clk); rst_n = 0; #1;
        if (done !== 0 || result !== 0) $fatal(1, "reset during operation");
        rst_n = 1;
        check(84, 30);
        $display("PASS gcd8: boundaries, random operands and reset during operation");
        $finish;
    end
    initial begin #2000000; $fatal(1, "timeout"); end
endmodule
