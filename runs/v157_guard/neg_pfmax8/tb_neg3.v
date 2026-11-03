`timescale 1ns/1ps
module tb_neg3;
    logic s_axi_aclk=1'b0, s_axi_aresetn=1'b0;
    bin_unit_axi_wrap_v50 #(.PF_DEPTH(3)) dut (
        .s_axi_aclk(s_axi_aclk), .s_axi_aresetn(s_axi_aresetn));
    initial begin #20; $display("[TB-NEG] ❌ 检查②没拦住"); $finish; end
endmodule
