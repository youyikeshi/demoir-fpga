"""通过 OpenDataLab 国内镜像下载 UHDM 数据集（不消耗 VPN 流量）。

背景
----
UHDM 官方只提供 Google Drive 下载，而完整数据集是 **80 GB**：
    - 走 Google Drive → 消耗大量 VPN 流量
    - 走 OpenDataLab（上海人工智能实验室，国内平台）→ 不耗 VPN

OpenDataLab 上的数据集标识：`OpenDataLab/UHDM`，总 82.0 GB，实际结构：
    raw/UHDM.tar.gz       80.00 GB   完整数据集（单文件，不能只下训练集）
    sample/image/*.jpg    497 张      样例预览图（约 2 GB）

⚠️ 这是一个**单文件 80 GB**，只能整包下载。若想只要训练集的一部分，
   改走 Google Drive 的 train_split/（18 个分卷，可只下前几卷），见
   docs/训练/流程_双机协作.md 的"部分下载"一节。

使用步骤
--------
1. 先注册并登录 OpenXLab（国内站，无需代理）：
       https://openxlab.org.cn/     注册后在"个人中心-密钥管理"获取 AK/SK

2. 登录（会提示输入 AK/SK）：
       openxlab login

3. 下载（本脚本会自动调用）：
       python tools/download_uhdm_opendatalab.py            # 下完整数据集
       python tools/download_uhdm_opendatalab.py --samples  # 只下 497 张样例图（约 2GB，先试水）

4. 解压（80 GB 解压后约 85 GB，先确认磁盘够）：
       tar -xzvf UHDM.tar.gz

依赖：`pip install openxlab`（已随 tools/requirements.txt 安装）
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATASET_REPO = 'OpenDataLab/UHDM'
DEFAULT_OUT = os.path.join(ROOT, 'datasets', 'uhdm', 'raw')


def find_cli():
    """找到 openxlab 可执行入口（包的 console_scripts）。"""
    exe = shutil.which('openxlab')
    if exe:
        return exe
    # venv 里的 Scripts 目录
    cand = os.path.join(os.path.dirname(sys.executable),
                        'openxlab.exe' if os.name == 'nt' else 'openxlab')
    if os.path.exists(cand):
        return cand
    return None


def run(cmd):
    print('$ %s' % ' '.join(cmd))
    return subprocess.call(cmd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=DEFAULT_OUT)
    ap.add_argument('--samples', action='store_true',
                    help='只下载 raw/image 下的样例图（约 2GB），用于先验证链路')
    ap.add_argument('--info', action='store_true', help='只打印数据集信息')
    a = ap.parse_args()

    exe = find_cli()
    if not exe:
        print('找不到 openxlab 命令。请先安装：')
        print('  pip install openxlab -i https://pypi.tuna.tsinghua.edu.cn/simple')
        return 1

    print('openxlab 入口: %s' % exe)
    print('数据集: %s' % DATASET_REPO)
    print('目标目录: %s' % os.path.abspath(a.out))
    print()

    # 信息与文件列表（不需要登录）
    if a.info:
        return run([exe, 'dataset', 'info', '--dataset-repo', DATASET_REPO])

    os.makedirs(a.out, exist_ok=True)

    # 1) 先打印信息，确认看到 80GB 与 /raw/UHDM.tar.gz
    print('=== 数据集信息（确认体积后再下载）===')
    run([exe, 'dataset', 'info', '--dataset-repo', DATASET_REPO])
    print()

    # 2) 下载
    if a.samples:
        src = '/sample/image'
        print('=== 下载样例图（约 2GB）===')
    else:
        src = '/raw'
        print('=== 下载完整数据集（约 80GB，请确认磁盘与带宽）===')
        print('    磁盘需求：压缩包 80GB + 解压后约 85GB ≈ 165GB')
        print()

    cmd = [exe, 'dataset', 'download',
           '--dataset-repo', DATASET_REPO,
           '--source-path', src,
           '--target-path', os.path.abspath(a.out)]
    rc = run(cmd)
    if rc != 0:
        print()
        print('下载命令返回码 %d。常见原因：' % rc)
        print('  1. 未登录 —— 先在 https://openxlab.org.cn/ 注册，然后运行 `openxlab login`')
        print('  2. AK/SK 无效 —— 到"个人中心-密钥管理"重新获取')
        print('  3. 磁盘不足 —— 完整数据需约 165GB（压缩包 + 解压）')
        return rc

    print()
    print('下载完成。文件位于: %s' % os.path.abspath(a.out))
    print()
    print('下一步：解压（80GB 解压后约 85GB）')
    print('  cd "%s"' % os.path.abspath(a.out))
    print('  tar -xzvf UHDM.tar.gz')
    return 0


if __name__ == '__main__':
    sys.exit(main())
