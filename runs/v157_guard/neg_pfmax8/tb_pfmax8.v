// T-157：反向验证 —— 主线（已合入闸门）在 PF_MAX=8 时必须 $fatal，不许静默
`timescale 1ns/1ps
module tb_pfmax8;
    logic s_axi_aclk = 1'b0;
    logic s_axi_aresetn = 1'b0;
    bin_unit_axi_wrap_v50 #(.PF_MAX(8)) dut (
        .s_axi_aclk(s_axi_aclk), .s_axi_aresetn(s_axi_aresetn));
    initial begin
        #20;
        $display("[TB-NEG] ❌ 没有拦住：PF_MAX=8 竟然跑起来了");
        $finish;
    end
endmodule
