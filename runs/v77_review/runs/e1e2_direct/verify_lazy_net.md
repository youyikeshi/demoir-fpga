# 整网离线 A/B（FakePL = conv + E1E2）

```
== verify_lazy_net：FakePL（conv + E1E2）整网 A/B，hw=256 ==
A 基线（纯 PS 参考）      sha1 = ('392d1ae02761', '7f68fb00d2b5', 'aa03751a2d1c')  总 20.00 s
B E1E2 + LAZY_DEQ=1       sha1 = ('392d1ae02761', '7f68fb00d2b5', 'aa03751a2d1c')  总 20.79 s  prologue 0.16  epilogue 0.07
  惰性统计：{"pinned": 198, "released": 168, "starved": 30, "direct_layers": 200, "mixed_layers": 0, "ps_layers": 0, "deq_skipped": 198, "direct_bytes": 86231040, "f32_bytes": 42251264}
C E1E2 + LAZY_DEQ=0       sha1 = ('392d1ae02761', '7f68fb00d2b5', 'aa03751a2d1c')  总 26.95 s  prologue 1.22  epilogue 0.04
   冻结 256² 基线（主机 FakePL）('392d1ae02761', '7f68fb00d2b5', 'aa03751a2d1c') ⇒ 一致
== 总结：PASS（67.9 s）==
```
