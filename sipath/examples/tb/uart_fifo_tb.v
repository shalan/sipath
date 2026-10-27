`timescale 1ns/1ps
module tb;
    reg clk=0;
    always #5 clk=~clk;
    reg rst_n=0, rx_i=1, valid=0, write=0;
    reg [1:0] addr=0;
    reg [7:0] wdata=0;
    wire tx_o, ready;
    wire [7:0] rdata;
    uart_fifo dut(.*);
    localparam BIT_CYCLES=16;
    reg [7:0] received[0:15];
    integer tx_seen=0, i, k, j, stalls=0;
    reg monitor=1;
    reg [7:0] decoded, value;
    task reset;
        begin
            @(negedge clk); rst_n=0; valid=0; rx_i=1;
            repeat(4) @(negedge clk); rst_n=1;
            repeat(4) @(negedge clk);
        end
    endtask
    task bus(input reg wr, input [1:0] a, input [7:0] data, output [7:0] result);
        integer wait_cycles;
        begin
            @(negedge clk); valid=1; write=wr; addr=a; wdata=data;
            #1; wait_cycles=0;
            while (!ready && wait_cycles<1000) begin
                @(negedge clk); #1; wait_cycles=wait_cycles+1; stalls=stalls+1;
            end
            if (!ready) $fatal(1,"bus timeout addr=%0d",a);
            result=rdata;
            @(negedge clk); valid=0;
        end
    endtask
    task serial(input [7:0] data, input reg good_stop);
        integer b;
        begin
            @(negedge clk); rx_i=0;
            repeat(BIT_CYCLES) @(negedge clk);
            for(b=0;b<8;b=b+1) begin
                rx_i=data[b]; repeat(BIT_CYCLES) @(negedge clk);
            end
            rx_i=good_stop; repeat(BIT_CYCLES) @(negedge clk);
            rx_i=1;
        end
    endtask
    initial forever begin
        @(negedge tx_o);
        if (monitor && rst_n) begin
            #(BIT_CYCLES*5);
            if(tx_o!==0) $fatal(1,"TX start bit");
            for(k=0;k<8;k=k+1) begin #(BIT_CYCLES*10); decoded[k]=tx_o; end
            #(BIT_CYCLES*10);
            if(tx_o!==1) $fatal(1,"TX stop bit");
            received[tx_seen]=decoded; tx_seen=tx_seen+1;
        end
    end
    initial begin
        #1; rst_n=1; #1; rst_n=0;
        reset();
        bus(0,1,0,value); if(value!==8'h02) $fatal(1,"reset status %h",value);
        bus(0,2,0,value); if(value!==16) $fatal(1,"divisor readback");
        bus(0,3,0,value); if(value!==0) $fatal(1,"reserved read");
        // Empty reads stall without changing FIFO state.
        @(negedge clk); valid=1; write=0; addr=0;
        repeat(3) begin @(negedge clk); if(ready) $fatal(1,"empty RX accepted"); end
        valid=0;
        // More than FIFO capacity: writes must wait for the serial shifter.
        for(i=0;i<8;i=i+1) bus(1,0,8'(i*37),value);
        if(stalls==0) $fatal(1,"TX full backpressure was not exercised");
        wait(tx_seen==8);
        for(i=0;i<8;i=i+1)
            if(received[i]!==8'(i*37)) $fatal(1,"TX order/data at %0d",i);
        // Wrap FIFO pointers repeatedly; exercise RX independently of TX.
        for(i=0;i<8;i=i+1) begin
            serial(8'(255-i*29),1);
            bus(0,0,0,value);
            if(value!==8'(255-i*29)) $fatal(1,"RX data at %0d: %h",i,value);
        end
        // Full duplex: RX accumulates four bytes while TX sends its own stream.
        fork
            begin for(j=0;j<4;j=j+1) serial(8'(90+j),1); end
            begin for(i=0;i<4;i=i+1) bus(1,0,8'(150+i),value); end
        join
        wait(tx_seen==12);
        for(i=0;i<4;i=i+1) begin
            if(received[8+i]!==8'(150+i)) $fatal(1,"full-duplex TX");
            bus(0,0,0,value); if(value!==8'(90+i)) $fatal(1,"full-duplex RX");
        end
        // Fifth unread byte is dropped, with a sticky overrun indication.
        for(i=0;i<5;i=i+1) serial(8'(i+40),1);
        bus(0,1,0,value); if(!value[4]) $fatal(1,"missing overrun");
        for(i=0;i<4;i=i+1) begin
            bus(0,0,0,value); if(value!==8'(i+40)) $fatal(1,"RX overrun order");
        end
        bus(1,1,8'h10,value);
        bus(0,1,0,value); if(value[4] || value[0]) $fatal(1,"clear overrun/empty");
        // A short low pulse is a false start, not a byte.
        @(negedge clk); rx_i=0;
        repeat(3) @(negedge clk); rx_i=1;
        repeat(180) @(negedge clk);
        bus(0,1,0,value); if(value[0] || value[3]) $fatal(1,"false start accepted");
        serial(8'ha5,0);
        repeat(20) @(negedge clk);
        bus(0,1,0,value); if(!value[3] || value[0]) $fatal(1,"framing error handling");
        bus(1,1,8'h08,value);
        bus(0,1,0,value); if(value[3]) $fatal(1,"framing W1C");
        // Reset aborts an in-flight TX and partial RX and empties both FIFOs.
        monitor=0;
        bus(1,0,8'h55,value);
        @(negedge clk); rx_i=0;
        repeat(25) @(negedge clk);
        reset();
        if(tx_o!==1) $fatal(1,"reset TX idle");
        bus(0,1,0,value); if(value!==8'h02) $fatal(1,"reset recovery %h",value);
        serial(8'hc3,1); bus(0,0,0,value);
        if(value!==8'hc3) $fatal(1,"RX after reset");
        $display("PASS UART: serialization, FIFOs, backpressure, errors and reset");
        $finish;
    end
    initial begin #1000000; $fatal(1,"watchdog"); end
endmodule
