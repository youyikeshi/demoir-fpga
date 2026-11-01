"""numpy 金标准：与 conv_ref_rt（C 参考实现）语义逐位一致的卷积。

语义：out = sat8( (sum + bias) >> shift )；relu=True 时先做 ReLU
（v2 新增 relu 开关：Ghost 残差投影等线性层传 False）。
本文件不带任何 PYNQ 依赖，可单独导入做本机校验（见 ../tools/check_numpy_ref.py）。

==== k=3 权重的"12 字节容器"（2026-10-23 第四轮）====
诊断：k=3 权重装载占 conv 的 44%（357 MB / 约 39 s），根因是每个 (oc,ic) 的
9 个抽头在 DDR 里连续但**9 字节步长不是 4 的倍数**，HLS 只能按 1 字节/笔记
访问（`ARLEN=1`，板端 16.3 拍/字节）。

解法：导出侧把每个 (oc,ic) 的 9 个抽头补 3 个 0，排成 **12 字节 = 3 个 32-bit
字**（第 q 个字的低 3 字节 = 抽头 3q..3q+2，高字节是 0）。于是
    offset(oc, ic, ky, kx) = (oc*cin + ic)*12 + ky*3 + kx
恒 4 字节对齐，装载循环一拍读一个字 ⇒ 事务数 ÷9、每笔 12 字节。

这只影响**容器**（`w_shape=[cout,cin,12]`、`w_len=12*cout*cin`），语义不变：
下面的 `conv_ref_numpy` 用 (cout,cin,3,3) 的旧形状照常算（`pad12` 里的 9 个
有效字节就是它），所以 ref_out.bin / 板上 md5 都是同一串答案。

==== k=5 权重的"28 字节容器"（2026-10-23 第五轮）====
同一个病、同一招：`encoder.conv_first` 是唯一的 5×5 层（5.40 s，占 conv 10%），
每个 (oc,ic) 25 字节，25 不是 4 的倍数 ⇒ 装载只能逐字节（`ARLEN=1`）。
现在把 25 个抽头紧排在前 25 字节、再补 3 个零，凑成 **28 字节 = 7 个 32-bit 字**：
    offset(oc, ic, ky, kx) = (oc*cin + ic)*28 + ky*5 + kx
代价只有体积 ×28/25（k=5 全网络只有 1 层，1.47 → 1.65 MB 量级）。
k=1 的容器**不变**（[oc][ic][1][1]）。
"""
import numpy as np

# 布局标签（index.json 的 layers[key]["w_layout"]）：
W_LAYOUT_OCIC = "oc_ic_ky_kx"    # 默认/旧容器 [oc][ic][ky][kx]
W_LAYOUT_K3_PAD12 = "k3_pad12"   # 仅 k=3：[oc][ic][12]（9 个抽头 + 3 个零）
W_LAYOUT_K5_PAD28 = "k5_pad28"   # 仅 k=5：[oc][ic][28]（25 个抽头 + 3 个零）
# 通用 TAP 容器（2026-10-30 时序优化后的 RTL 口径）：
#   [oc][ic][TAP]，前 k² 字节是真抽头（j = ky*k + kx），其余是补齐洞
#   TAP = 8·ceil(k²/8) ∈ {8,16,32}（新，默认）；k²+3 ∈ {4,12,28}（旧，兼容）
#   与上面两个 PAD 容器的区别只有"补齐洞多宽"：j < k² 的字节逐字节相同。
W_LAYOUT_TAP = "oc_ic_tap"

K3_PAD = 12                      # 每个 (oc,ic) 的容器字节数
K3_PAD_BYTES = 9                 # 其中有效的抽头字节数
K5_PAD = 28                      # 每个 (oc,ic) 的容器字节数
K5_PAD_BYTES = 25                # 其中有效的抽头字节数（5×5）


def layout_of(entry):
    """从 index.json 的一条 layer 记录里取布局标签（缺省 = 旧容器）。"""
    lay = entry.get("w_layout") if isinstance(entry, dict) else None
    return lay or W_LAYOUT_OCIC


def k3_to_pad12(w):
    """(cout,cin,3,3) int8 → (cout,cin,12) int8（C 连续，可直接写进 blob）。

    第 q 个字的低 3 字节 = 抽头 3q..3q+2，高字节恒 0。与 C 侧
    `widx() = (oc*cin+ic)*12 + ky*3 + kx`、引擎装载循环同一定义。
    """
    w = np.asarray(w, dtype=np.int8)
    cout, cin, k1, k2 = w.shape
    assert k1 == 3 and k2 == 3, "k3_to_pad12 只接受 (cout,cin,3,3)"
    out = np.zeros((cout, cin, K3_PAD), np.int8)
    out[:, :, :K3_PAD_BYTES] = w.reshape(cout, cin, 9)
    return np.ascontiguousarray(out)


def k3_from_pad12(w, cout=None, cin=None):
    """(cout,cin,12) → (cout,cin,3,3)（k3_to_pad12 的逆）。

    允许传扁平 buffer 的视图：给 cout/cin 时会先 reshape 成 (cout,cin,12)。
    顺带校验补零的 3 个字节**必须为 0**（抓"打包时忘了清零"的错）。
    """
    w = np.asarray(w, dtype=np.int8)
    if w.ndim == 1:
        if cout is None or cin is None:
            raise ValueError("k3_from_pad12: 扁平输入必须给 cout/cin")
        w = w.reshape(cout, cin, K3_PAD)
    assert w.shape[2] == K3_PAD, "k3_from_pad12 期望最后一维 %d，得到 %d" % (
        K3_PAD, w.shape[2])
    if np.any(w[:, :, K3_PAD_BYTES:]):
        raise ValueError("k=3 权重容器的补零字节非 0：权重包坏了或布局不匹配")
    return np.ascontiguousarray(w[:, :, :K3_PAD_BYTES]
                                .reshape(w.shape[0], w.shape[1], 3, 3))


def k5_to_pad28(w):
    """(cout,cin,5,5) int8 → (cout,cin,28) int8（C 连续，可直接写进 blob）。

    25 个抽头紧排在前 25 字节、第 25~27 字节补 0 = 7 个 32-bit 字。
    与 C 侧 `widx() = (oc*cin+ic)*28 + ky*5 + kx`、引擎装载循环同一定义。
    """
    w = np.asarray(w, dtype=np.int8)
    cout, cin, k1, k2 = w.shape
    assert k1 == 5 and k2 == 5, "k5_to_pad28 只接受 (cout,cin,5,5)"
    out = np.zeros((cout, cin, K5_PAD), np.int8)
    out[:, :, :K5_PAD_BYTES] = w.reshape(cout, cin, 25)
    return np.ascontiguousarray(out)


def k5_from_pad28(w, cout=None, cin=None):
    """(cout,cin,28) → (cout,cin,5,5)（k5_to_pad28 的逆）。

    允许传扁平 buffer 的视图；顺带校验补零的 3 个字节必须为 0。
    """
    w = np.asarray(w, dtype=np.int8)
    if w.ndim == 1:
        if cout is None or cin is None:
            raise ValueError("k5_from_pad28: 扁平输入必须给 cout/cin")
        w = w.reshape(cout, cin, K5_PAD)
    assert w.shape[2] == K5_PAD, "k5_from_pad28 期望最后一维 %d，得到 %d" % (
        K5_PAD, w.shape[2])
    if np.any(w[:, :, K5_PAD_BYTES:]):
        raise ValueError("k=5 权重容器的补零字节非 0：权重包坏了或布局不匹配")
    return np.ascontiguousarray(w[:, :, :K5_PAD_BYTES]
                                .reshape(w.shape[0], w.shape[1], 5, 5))


def tap_from_container(w, k=None, w_tap=None, cout=None, cin=None):
    """通用 TAP 容器 (cout,cin,TAP) → (cout,cin,k,k)。补齐洞必须全 0。

    `w` 可以是扁平 buffer 的视图（给 cout/cin）；`w_tap` 只在扁平输入时需要，
    reshape 之后以最后一维为准（并校验与 index.json 记录的 `tap` 一致）。
    """
    import tap_pack                      # 与导出侧共用同一份 TAP 表/校验
    w = np.asarray(w, dtype=np.int8)
    if w.ndim == 1:
        if cout is None or cin is None or w_tap is None:
            raise ValueError("tap_from_container: 扁平输入必须给 cout/cin/w_tap")
        w = w.reshape(int(cout), int(cin), int(w_tap))
    if w.ndim != 3:
        raise ValueError("tap_from_container 期望 (cout,cin,TAP)，收到 %r"
                         % (w.shape,))
    if w_tap is not None and int(w_tap) != int(w.shape[2]):
        raise ValueError("tap_from_container: index 记录 tap=%d 与 w_shape 末维 %d "
                         "不一致（权重包坏了）" % (int(w_tap), int(w.shape[2])))
    return tap_pack.unpack_layer(w, k=k, tap=int(w.shape[2]))


def wgt_as_ocic(w, w_layout=W_LAYOUT_OCIC, k=None, w_tap=None):
    """把容器里的权重统一成 (cout,cin,k,k)，供下面的数值计算使用。

    只处理三种合法输入：旧容器的 4 维数组、k3_pad12 的 (cout,cin,12)、
    k5_pad28 的 (cout,cin,28)。其它形状直接报错（宁可炸掉，也别猜错下标 ——
    猜错的后果是"两边一起错"）。
    """
    w = np.asarray(w)
    lay = w_layout or W_LAYOUT_OCIC
    if lay == W_LAYOUT_K3_PAD12:
        return k3_from_pad12(w)
    if lay == W_LAYOUT_K5_PAD28:
        return k5_from_pad28(w)
    if lay == W_LAYOUT_TAP:
        return tap_from_container(w, k=k, w_tap=w_tap)
    if w.ndim == 4:
        return w
    raise ValueError("wgt_as_ocic: 布局 %s 期望 4 维 (cout,cin,k,k)，收到 %d 维"
                     % (w_layout, w.ndim))


def conv_ref_numpy(inp, wgt, bias, k, s, d, pad, shift, relu=True,
                   w_layout=W_LAYOUT_OCIC, w_tap=None):
    """inp: (cin,H,W) int8; wgt: (cout,cin,k,k) int8; bias: (cout,) int32。
    返回 (cout,Hout,Wout) int8。

    w_layout = "k3_pad12" 时 wgt 是 (cout,cin,12) 的容器（导出侧新布局），
    内部会先取出前 9 个字节转成 (cout,cin,3,3)。**两条路径的数值完全一样**
    （只是容器补齐），所以任何布局错误都会在这里被 C 侧（conv_ref_rt）或
    总量对拍（ref_out.bin 必须逐字节不变）抓出来。
    """
    wgt = wgt_as_ocic(wgt, w_layout, k, w_tap=w_tap)
    # ---- C 加速（2026-10-28）----------------------------------------------
    #  k>=3 时先试 ARM 上的 C 内核（conv_ref_fast.c，逐字节与下面 numpy 版一致，
    #  x86 实测 4.7x、板上更明显——numpy 版对 k=5 的 conv_first 要 45 s，
    #  占整网 63%）。不可用（非 ARM / 编译失败 / 参数不支持）就静默退回 numpy。
    #  只对 k>=3 启用：k=1 时 numpy 的 tensordot 已经很快，C 反而略慢。
    if k >= 3 and s == 1 and d == 1:
        try:
            from conv_ref_fast import conv_chw_i8 as _cf
            _fast = _cf(inp, wgt, bias, k, s, d, pad, shift, relu)
            if _fast is not None:
                return _fast
        except Exception:                       # noqa: BLE001
            pass
    cin, H, W = inp.shape
    cout = wgt.shape[0]
    hout = (H - 1) // s + 1
    wout = (W - 1) // s + 1

    p = np.zeros((cin, H + 2 * pad, W + 2 * pad), np.int32)
    p[:, pad:pad + H, pad:pad + W] = inp.astype(np.int32)

    w = wgt.astype(np.int32)
    acc = np.zeros((cout, hout, wout), np.int32)
    for ky in range(k):
        r0 = ky * d
        rows = p[:, r0:r0 + (hout - 1) * s + 1:s, :]           # (cin,hout,W+2pad)
        for kx in range(k):
            c0 = kx * d
            sub = rows[:, :, c0:c0 + (wout - 1) * s + 1:s]     # (cin,hout,wout)
            acc += np.tensordot(w[:, :, ky, kx], sub, axes=([1], [0]))

    v = (acc + bias.reshape(-1, 1, 1).astype(np.int32)) >> shift   # 算术右移
    if relu:
        v = np.maximum(v, 0)                                       # ReLU（可旁路）
    v = np.clip(v, -128, 127).astype(np.int8)                      # 饱和
    return v
