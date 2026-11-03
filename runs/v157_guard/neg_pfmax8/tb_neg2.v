`timescale 1ns/1ps
module tb_neg2;
    logic s_axi_aclk=1'b0, s_axi_aresetn=1'b0;
    bin_unit_axi_wrap_v50 #(.NSLOT(8), .AR_SLOTS(4), .PF_MAX(4), .PF_EN(1)) dut (
        .s_axi_aclk(s_axi_aclk), .s_axi_aresetn(s_axi_aresetn));
    initial begin #20; $display("[TB-NEG] ❌ 检查③没拦住"); $finish; end
endmodule
