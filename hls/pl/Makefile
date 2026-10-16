# =============================================================================
# pl/Makefile —— 算子库的两条编译路径
# =============================================================================
#   make csim     普通 g++ 编译 + x86 对拍（开发机，秒级）
#   make csynth   Vitis HLS 综合出 IP（需要 source Vitis 的 settings64.sh，分钟级）
#
# 两条路径共用同一份 src/*.cpp —— 这是刻意的：如果为了综合另写一份实现，
# 等价性证明就作废了。csim 用 g++ 时 `#pragma HLS` 被忽略（-Wno-unknown-pragmas）。
# =============================================================================
PL_DIR   := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
BUILD    := $(PL_DIR)build
INCLUDE  := $(PL_DIR)include
SRCS     := $(wildcard $(PL_DIR)src/*.cpp)
CXX      ?= g++
CXXFLAGS ?= -O2 -std=c++11 -Wall -Wextra -Wno-unknown-pragmas -I$(INCLUDE)

VITIS_SETTINGS ?= /home/zheyang/AMD/2025.2/Vitis/settings64.sh
HLS_PART       ?= xc7z015-clg485-1
HLS_TOP        ?= demoir_conv2d_top
HLS_CLOCK      ?= 10

.PHONY: all csim lib csynth clean help

all: csim

# --- 测试台：读清单、跑算子、写 .npy -----------------------------------------
csim: $(BUILD)/op_runner

$(BUILD)/op_runner: $(SRCS) $(PL_DIR)tb/op_runner.cpp $(INCLUDE)/demoir_ops.h $(INCLUDE)/npy_io.h
	@mkdir -p $(BUILD)
	$(CXX) $(CXXFLAGS) $(SRCS) $(PL_DIR)tb/op_runner.cpp -o $@
	@echo "  建好 $@"

# --- 只编译库（给别的 host 程序链）-------------------------------------------
lib: $(SRCS)
	@mkdir -p $(BUILD)
	$(CXX) $(CXXFLAGS) -c $(SRCS) -o /dev/null 2>/dev/null || true
	$(CXX) $(CXXFLAGS) -fsyntax-only $(SRCS)
	@echo "  语法检查通过"

# --- HLS 综合 -----------------------------------------------------------------
# vitis_hls 在 2025.2 已经并进统一 Vitis，只能用 vitis-run --mode hls 调。
csynth:
	@mkdir -p $(BUILD)/hls
	@bash -c 'source $(VITIS_SETTINGS) >/dev/null 2>&1; \
	  export HLS_PART=$(HLS_PART) HLS_TOP=$(HLS_TOP) HLS_CLOCK=$(HLS_CLOCK); \
	  cd $(BUILD)/hls && vitis-run --mode hls --tcl $(PL_DIR)run_hls.tcl \
	    --work_dir $(BUILD)/hls/work 2>&1 | tail -40'
	@echo "  报告：$(BUILD)/hls/$(HLS_TOP)/solution1/syn/report/"

clean:
	rm -rf $(BUILD)/op_runner $(BUILD)/hls

help:
	@echo "make csim      # g++ 建 op_runner（x86 对拍用）"
	@echo "make lib       # 只做语法检查"
	@echo "make csynth    # Vitis HLS 综合（先 source $(VITIS_SETTINGS)）"
