# cat 消除 A/B（hw=256）

| 检查 | 结果 |
|---|---|
| A. cat part 的 C%8==0 | PASS（0 例外）|
| B. 嵌套 cat 结合律 | PASS（84 处）|
| C. cat→conv 打包等价 | PASS（110 处）|
| C2. 逐 227 层 | PASS（227 层）|
| C3. 边界数据 | PASS（3 层）|

主机累计：cat+pack 0.1275 s → pack_parts 0.1326 s

| conv（消费者） | parts | 展开 cat 数 | 输入 MB | sha1 |
|---|---:|---:|---:|---|
| `decoder.decoder_1.sam_blocks.0.b0.conv.conv` | 48+48+48+48+48+48+48+48+48+48+64 | 5 | 35.65 | `dbef38a930e97cc3` |
| `decoder.decoder_1.sam_blocks.0.b0.ghost_layers.4.primary_conv.conv` | 48+48+48+48+48+48+48+48+64 | 4 | 29.36 | `fba71bddf0b345fe` |
| `encoder.encoder_1.sam_blocks.0.b0.conv.conv` | 32+32+32+32+32+32+32+32+32+32+48 | 5 | 24.12 | `e93bd89846f28916` |
| `decoder.decoder_1.rdb.conv_post.conv` | 96+96+96+64 | 3 | 23.07 | `7d24370aeaf70a72` |
| `decoder.decoder_1.sam_blocks.0.b0.ghost_layers.3.primary_conv.conv` | 48+48+48+48+48+48+64 | 3 | 23.07 | `ab0c5ddbeaacb0cc` |
| `encoder.encoder_1.sam_blocks.0.b0.ghost_layers.4.primary_conv.conv` | 32+32+32+32+32+32+32+32+48 | 4 | 19.92 | `314d1b6efb0e9114` |
| `decoder.decoder_1.rdb.conv_layers.2.conv` | 96+96+64 | 2 | 16.78 | `79be66499004f0bc` |
| `decoder.decoder_1.sam_blocks.0.b0.ghost_layers.2.primary_conv.conv` | 48+48+48+48+64 | 2 | 16.78 | `e0a267386a13dbb0` |
| `encoder.encoder_1.rdb.conv_post.conv` | 64+64+64+48 | 3 | 15.73 | `134a3e6c2f4ba55a` |
| `encoder.encoder_1.sam_blocks.0.b0.ghost_layers.3.primary_conv.conv` | 32+32+32+32+32+32+48 | 3 | 15.73 | `4a3dbd19c9040f58` |
| `encoder.encoder_1.rdb.conv_layers.2.conv` | 64+64+48 | 2 | 11.53 | `ab3212c8b7ac79ba` |
| `encoder.encoder_1.sam_blocks.0.b0.ghost_layers.2.primary_conv.conv` | 32+32+32+32+48 | 2 | 11.53 | `b335bd894c401e17` |
| `decoder.decoder_1.rdb.conv_layers.1.conv` | 96+64 | 1 | 10.49 | `7e77b1e2aea81505` |
| `decoder.decoder_1.sam_blocks.0.b0.ghost_layers.1.primary_conv.conv` | 48+48+64 | 1 | 10.49 | `0172cefb3d770eba` |
| `decoder.decoder_2.sam_blocks.0.b0.conv.conv` | 48+48+48+48+48+48+48+48+48+48+64 | 5 | 8.91 | `e746789d0d8bfa65` |
| `decoder.decoder_1.sam_blocks.0.b2.conv.conv` | 48+48+48+48+48+48+48+48+48+48+64 | 5 | 8.91 | `26a80ad478d8c83c` |
| `encoder.encoder_1.rdb.conv_layers.1.conv` | 64+48 | 1 | 7.34 | `a94efe96aeb0371b` |
| `encoder.encoder_1.sam_blocks.0.b0.ghost_layers.1.primary_conv.conv` | 32+32+48 | 1 | 7.34 | `3c619e68648e66b4` |
| `decoder.decoder_2.sam_blocks.0.b0.ghost_layers.4.primary_conv.conv` | 48+48+48+48+48+48+48+48+64 | 4 | 7.34 | `4167f1f85d6034b8` |
| `decoder.preconv_1.conv` | 48+64 | 1 | 7.34 | `dba424f3c4b6b6a5` |
| `decoder.decoder_1.sam_blocks.0.b2.ghost_layers.4.primary_conv.conv` | 48+48+48+48+48+48+48+48+64 | 4 | 7.34 | `14802c27731576c2` |
| `encoder.encoder_2.sam_blocks.0.b0.conv.conv` | 32+32+32+32+32+32+32+32+32+32+96 | 5 | 6.82 | `4e46ade76bdf5fa2` |
| `encoder.encoder_1.sam_blocks.0.b2.conv.conv` | 32+32+32+32+32+32+32+32+32+32+48 | 5 | 6.03 | `ad4a5087def672a1` |
| `encoder.encoder_2.sam_blocks.0.b0.ghost_layers.4.primary_conv.conv` | 32+32+32+32+32+32+32+32+96 | 4 | 5.77 | `0d4f1f809604f1cb` |
| `decoder.decoder_2.rdb.conv_post.conv` | 96+96+96+64 | 3 | 5.77 | `aef552601a3a55bc` |
| `decoder.decoder_2.sam_blocks.0.b0.ghost_layers.3.primary_conv.conv` | 48+48+48+48+48+48+64 | 3 | 5.77 | `539983685323de01` |
| `decoder.decoder_1.sam_blocks.0.b2.ghost_layers.3.primary_conv.conv` | 48+48+48+48+48+48+64 | 3 | 5.77 | `a8f0cfc7c68bbfa4` |
| `encoder.encoder_1.sam_blocks.0.b2.ghost_layers.4.primary_conv.conv` | 32+32+32+32+32+32+32+32+48 | 4 | 4.98 | `e9624a8c23f32af5` |
| `encoder.encoder_2.rdb.conv_post.conv` | 64+64+64+96 | 3 | 4.72 | `4034765077925716` |
| `encoder.encoder_2.sam_blocks.0.b0.ghost_layers.3.primary_conv.conv` | 32+32+32+32+32+32+96 | 3 | 4.72 | `5608b8e6d5d9f2c8` |
| `decoder.decoder_2.rdb.conv_layers.2.conv` | 96+96+64 | 2 | 4.19 | `948221af3c3b8304` |
| `decoder.decoder_2.sam_blocks.0.b0.ghost_layers.2.primary_conv.conv` | 48+48+48+48+64 | 2 | 4.19 | `d8ce9fcc8e0d22fa` |
| `decoder.decoder_1.sam_blocks.0.b2.ghost_layers.2.primary_conv.conv` | 48+48+48+48+64 | 2 | 4.19 | `f38861238920922f` |
| `encoder.encoder_1.sam_blocks.0.b2.ghost_layers.3.primary_conv.conv` | 32+32+32+32+32+32+48 | 3 | 3.93 | `51ad85c4d26bcf58` |
| `encoder.encoder_2.rdb.conv_layers.2.conv` | 64+64+96 | 2 | 3.67 | `5e923daf772db605` |
| `encoder.encoder_2.sam_blocks.0.b0.ghost_layers.2.primary_conv.conv` | 32+32+32+32+96 | 2 | 3.67 | `8f1f3d1dfa0385d6` |
| `encoder.encoder_1.sam_blocks.0.b2.ghost_layers.2.primary_conv.conv` | 32+32+32+32+48 | 2 | 2.88 | `62322a934b2a04da` |
| `encoder.encoder_2.rdb.conv_layers.1.conv` | 64+96 | 1 | 2.62 | `1adc087622d97fd2` |
| `encoder.encoder_2.sam_blocks.0.b0.ghost_layers.1.primary_conv.conv` | 32+32+96 | 1 | 2.62 | `9dbaf23dd96d27bc` |
| `decoder.preconv_2.conv` | 96+64 | 1 | 2.62 | `401d6c84c92475da` |
