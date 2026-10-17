# 设计空间探索：UNROLL_K 取 1/5/25，PIPELINE_II 取 1/2/4，共 9 组。
# 每组建一个独立工程，各跑一次 csynth，报告留在各自工程目录下。
# 汇总交给 dse_summary.py，Tcl 这边只负责跑。
#
# 只跑 csynth 不跑 csim：这 9 组只改 pragma 与展开因子，算法完全一致，
# 且默认配置与 K=5/II=2 两组已在本机用 g++ 验证过行为一致。
#
# 用法（在 fpga/pl/scripts 目录下）：vitis-run --mode hls --tcl dse_scan.tcl

set here    [pwd]
set src_dir [file normalize "$here/../src"]
set tb_dir  [file normalize "$here/../tb"]
set base    [file normalize "$here/../proj/dse"]

foreach uk {1 5 25} {
    foreach ii {1 2 4} {
        set name   "k${uk}_ii${ii}"
        set proj   "$base/$name"
        set cflags "-DUSE_HLS_TYPES -DUNROLL_K=$uk -DPIPELINE_II=$ii -I$src_dir"

        puts "==== DSE 配置 $name ===="
        open_project -reset $proj
        add_files $src_dir/conv2d_accel.cpp -cflags $cflags
        add_files $src_dir/conv_ref.cpp    -cflags $cflags
        add_files -tb $tb_dir/conv2d_accel_tb.cpp -cflags $cflags
        set_top conv2d_accel
        open_solution -reset solution1
        set_part xc7z015clg485-2
        create_clock -period 6.667 -name default
        csynth_design
        close_project
        puts "==== 完成 $name ===="
    }
}

puts "==== DSE 扫描结束，共 9 组 ===="
exit
