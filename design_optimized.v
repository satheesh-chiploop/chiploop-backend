Here is an optimized version of the given RTL code for readability and synthesis in Verilog-2005:


module four_bit_counter (
    input        clk,
    input        reset,
    input        enable,
    output reg [3:0] count
);

always @(posedge clk) begin
    if (reset)
        count <= 4'b0000;
    else if (enable)
        count <= count + 1'b1;
end

endmodule


This code optimizes the readability by using a single `always @(posedge clk)` block to handle both the reset and counting logic. It also ensures synthesizability by following Verilog-2005 standards, including lower-case signal names and declaring every signal used in the code. Additionally, the module name matches the specification, and the code ends with 'endmodule'.