module four_bit_counter (
    input wire clk,
    input wire reset,
    output reg [3:0] count
);

always @(posedge clk or posedge reset) begin
    if (reset) begin
        count <= 4'b0000;
    end else begin
        count <= count + 1;
    end
end

always @(posedge clk) begin
    if (count >= 16'h10) begin
        count <= 4'b0000;
    end
end

endmodule