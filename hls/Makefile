# =============================================================================
# hls/Makefile —— 这条线的唯一入口
# =============================================================================
# 这条线做的是"把去摩尔纹网络切分成 PS 控制 + PL 算子，并把算子写成可综合的
# C/C++"。三段证据链的跑法都在这里，不用记零散命令：
#
#   make venv       建本地 python 环境（只有导出权重/对拍需要 torch）
#   make weights    .tar -> weights.npz（PS 侧不装 torch 也能拿权重）
#   make pl         用 g++ 建 C++ 算子测试台
#   make plan       生成 PS 侧静态执行计划（缓冲分配 + 任务表；不需要硬件）
#   make check      跑完三段等价性 + 重新生成算子清单   ← 平时就这一条
#   make check-quick  跳过 torch 那两段（没有 torch 的机器上也能跑）
#   make csynth     Vitis HLS 综合（分钟级；先 source Vitis 的 settings64.sh）
#   make clean
#
# 为什么 check 要跑三段：见 docs/流程_等价性验证.md。一句话 —— 整网对得上
# 不代表某个算子对得上，某个算子对得上也不代表整网拼起来对得上。
# =============================================================================
HLS_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
REPO    := $(HLS_DIR)..
# venv 放在仓库根，跟仓库既有的 .venv-pdnet 同一层；.gitignore 的 .venv*/ 已覆盖
VENV    := $(REPO)/.venv-hls
PY      := $(VENV)/bin/python
PYTHON  ?= python3
SIZE    ?= 256

.PHONY: all venv weights pl plan check check-quick check-torch check-hls inventory csynth clean help

all: check

# --- 本地环境（只有开发机需要；板子上不需要 torch）---------------------------
venv:
	@test -x $(PY) || $(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -q --upgrade pip
	$(VENV)/bin/pip install torch==2.7.0 --index-url https://download.pytorch.org/whl/cpu
	$(VENV)/bin/pip install torchvision==0.22.0 --index-url https://download.pytorch.org/whl/cpu
	$(VENV)/bin/pip install pillow opencv-python-headless pyyaml -i https://pypi.tuna.tsinghua.edu.cn/simple

# --- 权重导出 ----------------------------------------------------------------
weights:
	@test -x $(PY) || { echo "先跑 make venv"; exit 1; }
	$(PY) $(HLS_DIR)tools/export_weights.py

# --- PL 侧 C++ 测试台 --------------------------------------------------------
pl:
	$(MAKE) -C $(HLS_DIR)pl csim

# --- PS 侧静态执行计划（不需要硬件，不需要 torch）----------------------------
plan: weights
	$(PYTHON) $(HLS_DIR)ps/schedule.py --size 512

# --- 三段证据链 --------------------------------------------------------------
check: check-torch check-hls inventory plan
	@echo
	@echo "三段等价性 + 算子清单 + 执行计划：全部完成"

check-quick: pl check-hls
	@echo
	@echo "（跳过了 torch 两段；完整验证请跑 make check）"

check-torch:
	@test -x $(PY) || { echo "没有 $(PY)：先跑 make venv，或用 make check-quick"; exit 1; }
	$(PY) $(HLS_DIR)tests/test_ops_numpy_vs_torch.py
	$(PY) $(HLS_DIR)tests/test_graph_numpy_vs_torch.py --size $(SIZE)

check-hls: pl weights
	$(PY) $(HLS_DIR)tests/run_equiv.py --source graph --size $(SIZE)
	$(PY) $(HLS_DIR)tests/run_equiv.py --source random --size $(SIZE)

inventory:
	$(PY) $(HLS_DIR)tools/op_inventory.py --size 512

# --- HLS 综合（需要 Vitis；不要用它代替 csim）--------------------------------
csynth:
	$(MAKE) -C $(HLS_DIR)pl csynth

clean:
	rm -rf $(HLS_DIR)build $(HLS_DIR)pl/build
	$(MAKE) -C $(HLS_DIR)pl clean 2>/dev/null || true

help:
	@sed -n '2,19p' $(HLS_DIR)Makefile | sed 's/^# \{0,1\}//'
