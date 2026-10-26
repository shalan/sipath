// Single-clock, fixed-baud 8N1 UART with four-byte TX/RX FIFOs.
// A transfer occurs on valid && ready. Hold request fields until accepted.
// addr 0: write TX / read RX (backpressured); addr 1: status, W1C errors;
// addr 2: clocks per bit (read only); addr 3: reserved (reads zero).
`default_nettype none
module uart_fifo #(parameter integer CLKS_PER_BIT = 16) (
    input wire clk, rst_n, rx_i,
    output wire tx_o,
    input wire valid, write,
    input wire [1:0] addr,
    input wire [7:0] wdata,
    output wire ready,
    output wire [7:0] rdata
);
    localparam integer CW = $clog2(CLKS_PER_BIT);
    wire [7:0] tx_head, rx_head;
    wire tx_full, tx_empty, rx_full, rx_empty;
    reg tx_busy;
    reg [9:0] tx_shift;
    reg [3:0] tx_bit;
    reg [CW-1:0] tx_count;
    wire tx_take = !tx_busy && !tx_empty;
    wire access_data = addr == 0;
    assign ready = valid && (!access_data || (write ? !tx_full : !rx_empty));
    wire transfer = valid && ready;
    reg frame_error, overrun;
    assign rdata = addr == 0 ? (rx_empty ? 8'b0 : rx_head) :
                   addr == 1 ? {3'b0, overrun, frame_error, tx_busy, !tx_full, !rx_empty} :
                   addr == 2 ? 8'(CLKS_PER_BIT) : 8'b0;
    uart_fifo4 txq(.clk(clk), .rst_n(rst_n), .push(transfer && write && access_data),
                  .pop(tx_take), .din(wdata), .dout(tx_head), .full(tx_full), .empty(tx_empty));
    reg rx_emit;
    reg [7:0] rx_byte;
    uart_fifo4 rxq(.clk(clk), .rst_n(rst_n), .push(rx_emit),
                  .pop(transfer && !write && access_data), .din(rx_byte),
                  .dout(rx_head), .full(rx_full), .empty(rx_empty));
    assign tx_o = tx_busy ? tx_shift[0] : 1'b1;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            tx_busy <= 0; tx_shift <= 10'h3ff; tx_bit <= 0; tx_count <= 0;
        end else if (tx_take) begin
            tx_busy <= 1; tx_shift <= {1'b1, tx_head, 1'b0};
            tx_bit <= 0; tx_count <= CW'(CLKS_PER_BIT-1);
        end else if (tx_busy) begin
            if (tx_count != 0) tx_count <= tx_count - 1'b1;
            else begin
                tx_count <= CW'(CLKS_PER_BIT-1);
                tx_shift <= {1'b1, tx_shift[9:1]};
                if (tx_bit == 9) tx_busy <= 0;
                else tx_bit <= tx_bit + 1'b1;
            end
        end
    end
    // Two-stage input synchronizer; all protocol state remains on clk.
    (* async_reg = "true" *) reg rx_meta, rx_sync;
    reg [1:0] rx_state;
    reg [CW-1:0] rx_count;
    reg [2:0] rx_bit;
    reg [7:0] rx_shift;
    reg bad_stop;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin rx_meta <= 1; rx_sync <= 1; end
        else begin rx_meta <= rx_i; rx_sync <= rx_meta; end
    end
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rx_state <= 0; rx_count <= 0; rx_bit <= 0;
            rx_shift <= 0; rx_byte <= 0; rx_emit <= 0; bad_stop <= 0;
        end else begin
            rx_emit <= 0; bad_stop <= 0;
            case (rx_state)
                0: if (!rx_sync) begin rx_state <= 1; rx_count <= CW'(CLKS_PER_BIT/2-1); end
                1: if (rx_count != 0) rx_count <= rx_count - 1'b1;
                   else if (rx_sync) rx_state <= 0; // false start
                   else begin rx_state <= 2; rx_bit <= 0; rx_count <= CW'(CLKS_PER_BIT-1); end
                2: if (rx_count != 0) rx_count <= rx_count - 1'b1;
                   else begin
                       rx_shift[rx_bit] <= rx_sync; rx_count <= CW'(CLKS_PER_BIT-1);
                       if (rx_bit == 7) rx_state <= 3;
                       else rx_bit <= rx_bit + 1'b1;
                   end
                3: if (rx_count != 0) rx_count <= rx_count - 1'b1;
                   else begin
                       rx_state <= 0;
                       if (rx_sync) begin rx_byte <= rx_shift; rx_emit <= 1; end
                       else bad_stop <= 1;
                   end
            endcase
        end
    end
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin frame_error <= 0; overrun <= 0; end
        else begin
            if (transfer && write && addr == 1) begin
                if (wdata[3]) frame_error <= 0;
                if (wdata[4]) overrun <= 0;
            end
            if (bad_stop) frame_error <= 1; // a new event wins over clearing
            if (rx_emit && rx_full) overrun <= 1;
        end
    end
endmodule

module uart_fifo4 (
    input wire clk, rst_n, push, pop,
    input wire [7:0] din,
    output wire [7:0] dout,
    output wire full, empty
);
    reg [7:0] mem [0:3];
    reg [1:0] rd, wr;
    reg [2:0] count;
    wire put = push && !full;
    wire take = pop && !empty;
    assign full = count == 4;
    assign empty = count == 0;
    assign dout = mem[rd];
    integer i;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rd <= 0; wr <= 0; count <= 0;
            for (i=0; i<4; i=i+1) mem[i] <= 0;
        end else begin
            if (put) begin mem[wr] <= din; wr <= wr + 1'b1; end
            if (take) rd <= rd + 1'b1;
            case ({put,take})
                2'b10: count <= count + 1'b1;
                2'b01: count <= count - 1'b1;
                default: count <= count;
            endcase
        end
    end
endmodule
`default_nettype wire
