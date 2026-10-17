// ==============================================================
// Vitis HLS - High-Level Synthesis from C, C++ and OpenCL v2025.2 (64-bit)
// Tool Version Limit: 2025.11
// Copyright 1986-2022 Xilinx, Inc. All Rights Reserved.
// Copyright 2022-2025 Advanced Micro Devices, Inc. All Rights Reserved.
// 
// ==============================================================
// control
// 0x00 : reserved
// 0x04 : reserved
// 0x08 : reserved
// 0x0c : reserved
// 0x10 : Data signal of in_fm
//        bit 31~0 - in_fm[31:0] (Read/Write)
// 0x14 : Data signal of in_fm
//        bit 31~0 - in_fm[63:32] (Read/Write)
// 0x18 : reserved
// 0x1c : Data signal of wgt
//        bit 31~0 - wgt[31:0] (Read/Write)
// 0x20 : Data signal of wgt
//        bit 31~0 - wgt[63:32] (Read/Write)
// 0x24 : reserved
// 0x28 : Data signal of bias
//        bit 31~0 - bias[31:0] (Read/Write)
// 0x2c : Data signal of bias
//        bit 31~0 - bias[63:32] (Read/Write)
// 0x30 : reserved
// 0x34 : Data signal of out_fm
//        bit 31~0 - out_fm[31:0] (Read/Write)
// 0x38 : Data signal of out_fm
//        bit 31~0 - out_fm[63:32] (Read/Write)
// 0x3c : reserved
// (SC = Self Clear, COR = Clear on Read, TOW = Toggle on Write, COH = Clear on Handshake)

#define XCONV2D_ENGINE_CONTROL_ADDR_IN_FM_DATA  0x10
#define XCONV2D_ENGINE_CONTROL_BITS_IN_FM_DATA  64
#define XCONV2D_ENGINE_CONTROL_ADDR_WGT_DATA    0x1c
#define XCONV2D_ENGINE_CONTROL_BITS_WGT_DATA    64
#define XCONV2D_ENGINE_CONTROL_ADDR_BIAS_DATA   0x28
#define XCONV2D_ENGINE_CONTROL_BITS_BIAS_DATA   64
#define XCONV2D_ENGINE_CONTROL_ADDR_OUT_FM_DATA 0x34
#define XCONV2D_ENGINE_CONTROL_BITS_OUT_FM_DATA 64

// ctrl
// 0x00 : Control signals
//        bit 0  - ap_start (Read/Write/COH)
//        bit 1  - ap_done (Read/COR)
//        bit 2  - ap_idle (Read)
//        bit 3  - ap_ready (Read/COR)
//        bit 7  - auto_restart (Read/Write)
//        bit 9  - interrupt (Read)
//        others - reserved
// 0x04 : Global Interrupt Enable Register
//        bit 0  - Global Interrupt Enable (Read/Write)
//        others - reserved
// 0x08 : IP Interrupt Enable Register (Read/Write)
//        bit 0 - enable ap_done interrupt (Read/Write)
//        bit 1 - enable ap_ready interrupt (Read/Write)
//        others - reserved
// 0x0c : IP Interrupt Status Register (Read/TOW)
//        bit 0 - ap_done (Read/TOW)
//        bit 1 - ap_ready (Read/TOW)
//        others - reserved
// 0x10 : Data signal of ap_return
//        bit 31~0 - ap_return[31:0] (Read)
// 0x18 : Data signal of H
//        bit 31~0 - H[31:0] (Read/Write)
// 0x1c : reserved
// 0x20 : Data signal of W
//        bit 31~0 - W[31:0] (Read/Write)
// 0x24 : reserved
// 0x28 : Data signal of Hout
//        bit 31~0 - Hout[31:0] (Read/Write)
// 0x2c : reserved
// 0x30 : Data signal of Wout
//        bit 31~0 - Wout[31:0] (Read/Write)
// 0x34 : reserved
// 0x38 : Data signal of cin
//        bit 31~0 - cin[31:0] (Read/Write)
// 0x3c : reserved
// 0x40 : Data signal of cout
//        bit 31~0 - cout[31:0] (Read/Write)
// 0x44 : reserved
// 0x48 : Data signal of ksize
//        bit 31~0 - ksize[31:0] (Read/Write)
// 0x4c : reserved
// 0x50 : Data signal of stride
//        bit 31~0 - stride[31:0] (Read/Write)
// 0x54 : reserved
// 0x58 : Data signal of dil
//        bit 31~0 - dil[31:0] (Read/Write)
// 0x5c : reserved
// 0x60 : Data signal of pad
//        bit 31~0 - pad[31:0] (Read/Write)
// 0x64 : reserved
// 0x68 : Data signal of shift
//        bit 31~0 - shift[31:0] (Read/Write)
// 0x6c : reserved
// 0x70 : Data signal of relu_en
//        bit 31~0 - relu_en[31:0] (Read/Write)
// 0x74 : reserved
// (SC = Self Clear, COR = Clear on Read, TOW = Toggle on Write, COH = Clear on Handshake)

#define XCONV2D_ENGINE_CTRL_ADDR_AP_CTRL      0x00
#define XCONV2D_ENGINE_CTRL_ADDR_GIE          0x04
#define XCONV2D_ENGINE_CTRL_ADDR_IER          0x08
#define XCONV2D_ENGINE_CTRL_ADDR_ISR          0x0c
#define XCONV2D_ENGINE_CTRL_ADDR_AP_RETURN    0x10
#define XCONV2D_ENGINE_CTRL_BITS_AP_RETURN    32
#define XCONV2D_ENGINE_CTRL_ADDR_H_DATA       0x18
#define XCONV2D_ENGINE_CTRL_BITS_H_DATA       32
#define XCONV2D_ENGINE_CTRL_ADDR_W_DATA       0x20
#define XCONV2D_ENGINE_CTRL_BITS_W_DATA       32
#define XCONV2D_ENGINE_CTRL_ADDR_HOUT_DATA    0x28
#define XCONV2D_ENGINE_CTRL_BITS_HOUT_DATA    32
#define XCONV2D_ENGINE_CTRL_ADDR_WOUT_DATA    0x30
#define XCONV2D_ENGINE_CTRL_BITS_WOUT_DATA    32
#define XCONV2D_ENGINE_CTRL_ADDR_CIN_DATA     0x38
#define XCONV2D_ENGINE_CTRL_BITS_CIN_DATA     32
#define XCONV2D_ENGINE_CTRL_ADDR_COUT_DATA    0x40
#define XCONV2D_ENGINE_CTRL_BITS_COUT_DATA    32
#define XCONV2D_ENGINE_CTRL_ADDR_KSIZE_DATA   0x48
#define XCONV2D_ENGINE_CTRL_BITS_KSIZE_DATA   32
#define XCONV2D_ENGINE_CTRL_ADDR_STRIDE_DATA  0x50
#define XCONV2D_ENGINE_CTRL_BITS_STRIDE_DATA  32
#define XCONV2D_ENGINE_CTRL_ADDR_DIL_DATA     0x58
#define XCONV2D_ENGINE_CTRL_BITS_DIL_DATA     32
#define XCONV2D_ENGINE_CTRL_ADDR_PAD_DATA     0x60
#define XCONV2D_ENGINE_CTRL_BITS_PAD_DATA     32
#define XCONV2D_ENGINE_CTRL_ADDR_SHIFT_DATA   0x68
#define XCONV2D_ENGINE_CTRL_BITS_SHIFT_DATA   32
#define XCONV2D_ENGINE_CTRL_ADDR_RELU_EN_DATA 0x70
#define XCONV2D_ENGINE_CTRL_BITS_RELU_EN_DATA 32

