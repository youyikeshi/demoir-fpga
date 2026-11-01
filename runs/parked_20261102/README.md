# 停放区：2026-11-02 的两份未验证改动

## 为什么在这里
两个子代理（q8 缓存加固 T-08 / 描述符缓存 T-05）**同时在共享的 `ps/pynq/` 里改文件**，
导致"活的树不能当基线"（复核者早已点过）。按技能 §2.3 的要求，它们的活必须落在**自己的副本**里。

## 内容
- `parked_ps_work.patch`：当时工作树相对 HEAD 的全部 PS 改动（+947/−123，6 个文件）
  - `bin_engine.py +391`（描述符缓存 + acq/descviews 计数）
  - `bin_net.py +189`（acq 计数器 + runpoint + opt-in）
  - `qcache.py +181`（q8 加固）
  - `board_v39_verify.py +275`（runpoint/一次性成本/seed）
  - `bin_ref_numpy.py +11`（默认关注释）
- 语法检查：**7 个文件 `py_compile` 全过**（不是语法错，是**逻辑未验证**）

## 怎么用
- 各自的新代理在自己的副本里 `git apply` 这份 patch（或只取自己那部分）后继续
- 副本路径：`worktrees/T-08-q8/`、`worktrees/T-05-psdesc/`
- **不许再直接写 `ps/pynq/`**：那棵树只接受"已过门禁、准备合入"的改动

## 基线是什么（截至此刻）
`git HEAD 70dcfa1` 的 PS 代码（只有 docs/skill 提交，**PS 代码零改动**）：
`bin_net 3f154b75 / bin_engine ddfe6b6f / bin_ref_numpy 5a3bbd8b / bilinear_c 48134b7b`
