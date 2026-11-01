# 整网离线 A/B（FakePL = conv + E1E2）

```
== verify_lazy_net：FakePL（conv + E1E2）整网 A/B，hw=64 ==
A 基线（纯 PS 参考）      sha1 = ('24222554c880', '2fd745d93159', '8ab595fcf7cd')  总 1.20 s
B E1E2 + LAZY_DEQ=1       sha1 = ('24222554c880', '2fd745d93159', '8ab595fcf7cd')  总 1.42 s  prologue 0.05  epilogue 0.01
  惰性统计：{"pinned": 198, "released": 168, "starved": 30, "direct_layers": 200, "mixed_layers": 0, "ps_layers": 0, "deq_skipped": 198, "direct_bytes": 5389440, "f32_bytes": 2640704}
C E1E2 + LAZY_DEQ=0       sha1 = ('24222554c880', '2fd745d93159', '8ab595fcf7cd')  总 1.37 s  prologue 0.15  epilogue 0.01
   冻结 64² 基线（主机 FakePL）('104710f69c4d', '2ac6efec2325', '8ab595fcf7cd') ⇒ 不同
== 总结：FAIL（4.0 s）==
```
