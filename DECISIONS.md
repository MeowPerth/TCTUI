# 旧脚本里"互相冲突"的逻辑，这一版的处理方式

`ReDecodingToH265.sh` 里有几处逻辑在特定情况下会互相打架（或者会丢文件）。
下面每条都列出：**旧行为 → 这版默认 → 怎么改回去**。你可以逐条拍板。

## 1. 分集合并：先删分集，再转码（最危险）

**旧**：`concat -c copy` 生成 `正片名.ext` 后，立刻 `rm -v` 删掉所有 `xxx-1/2/3.ext`，
然后才转码。转码失败时，分集已经没了，只剩一个可能不完整的合并文件。

**新**：合并只写到临时文件 `.tctui-<id>.merged.mkv`，转码成功且通过压缩率/时长校验后
才删除分集（分集在 `series.delete_sources_after_success` 控制）。
合并失败、转码失败、压缩率过低、用户取消、断电——分集都还在。

**改回去**：`series.delete_sources_after_success` 的删除时机不再支持"转码前删"，
这是安全性设计，不建议改。

## 2. 分集合并会覆盖同名正片

**旧**：如果目录里已经有 `正片.mp4`，合并输出直接写同名文件，把它覆盖掉。

**新**：输出名 = 分集基准名 + 输出扩展名；若已存在则自动 `_1`、`_2` 后缀，
**任何情况下都不覆盖已有文件**。

**改回去**：没有开关，故意不做。

## 3. 压缩率低于阈值：保留源文件，但写进"已处理"日志

**旧**：低于 10% 就保留源文件，然后 `mark_file_processed`——以后永远不会再碰。
问题是：以后调低 CQ / 换更好的编码器时，这些文件也不会重试。

**新**：默认仍标记为 `kept`（避免每次扫描重复转码），但：
* UI 里能选到 `保留` 状态的任务，按 `r` 可单条重新入队；
* 想每次扫描都重试：`policy.mark_kept_as_processed=false`。

**改回去**：`policy.mark_kept_as_processed=true`（已经是默认）。

## 4. 已经是 HEVC 的文件还会被重压一遍

**旧**：只要不在 `video_transcode_log.txt` 里，H.265 也照压，白烧显卡。

**新**：默认跳过 `scan.skip_if_codec=["hevc"]`（同时看 comment 标记）。
如果你想把 10bit HDR 的 HEVC 压成 8bit SDR，把 `skip_if_codec` 改成 `[]`，
再配合 `encode.hdr="force-sdr"`。

**改回去**：`"skip_if_codec": []`。

## 5. 音频统一转 AAC

**旧**：`-c:a aac`，所有音轨有损重编码，5.1/Atmos 直接废掉，还慢。

**新**：默认 `encode.audio="copy"`，音轨原样保留（mkv 容器几乎什么都能装）。
要压音频：`"audio": "aac", "audio_bitrate": "192k"`。

**改回去**：`"audio": "aac"`。

## 6. 只保留一条视频+一条音频，字幕全丢

**旧**：`-filter_complex ... -map [out] -c:a aac -c:s copy`，没有 `-map 0:a?`/`-map 0:s?`，
默认流选择只取一条视频和一条音频，多音轨、字幕、字体附件都丢。

**新**：`-map 0:V:0`（正片，不含封面图）+ 所有音轨 + 所有字幕 + mkv 字体附件。

**改回去**：不建议；如只要主音轨可自行改 `worker.build_transcode_args`。

## 7. HDR / 10bit 会被压成 8bit

**旧**：`scale_cuda=iw:-2` 没指定格式，默认输出 8bit，HDR 片源转完发灰。

**新**：源是 10bit/HDR 时输出 `p010le` 并复制 `color_primaries/color_trc/colorspace`
（`encode.hdr="auto"`）。要转 SDR：`encode.hdr="force-sdr"`（CPU tonemap，慢但颜色正常）。
注意：HDR10 的 mastering display 静态元数据 NVENC 不能原样搬，这点与原脚本一致。

**改回去**：`"hdr": "ignore"`。

## 7.5 CUDA 滤镜链：`-noautoscale` + `scale_cuda`（修 -38 错误）

旧脚本本来就用 `-filter_complex ... scale_cuda`，但这一版早期为了支持多音轨/字幕改成了
`-vf`，在某些片源上会触发 ffmpeg 自动插入 `auto_scale`，报：

```
Impossible to convert between the formats supported by the filter 'Parsed_null_0'
and the filter 'auto_scale_0'（退出码 -38）
```

现在统一改为：

```
-noautoscale -filter_complex "[0:V:0]scale_cuda=iw:-2[out]" -map "[out]" ...
```

* 不缩放：`scale_cuda=iw:-2` 占位
* 10bit/HDR：`scale_cuda=iw:-2:format=p010le`
* 需要缩放：`scale_cuda=W:H:format=...`
* 软解回退时整体重建参数（不带 cuda 滤镜、不带 `-noautoscale`），不再做字符串替换

注意：不要在 `encode.extra_video_args` 里再写 `-vf`/`-filter_complex`，会和内置滤镜链冲突。

## 8. 硬件解码失败就整条任务失败

**旧**：`-hwaccel cuda` 解码失败（例如 10bit H.264、VC-1、坏流）直接当转码失败。

**新**：worker 自动降级为软件解码再试一次（`encode.fallback_software_decode`），
日志里能看到 `attempt 2 mode=gpu-swdec`。两次都失败才算失败。

**改回去**：`"fallback_software_decode": false`。

## 9. 多实例 / 并发写同一目录

**旧**：临时名带 `$$` 支持多实例，但两个实例扫到同一个文件会互相抢 rename，
日志文件也是每个目录一份，容易写乱。

**新**：单 daemon 统一调度；worker 只写 `.tctui-<jobid>.*`，不删源文件；
任务库是 SQLite（WAL），UI 只读。

**改回去**：不建议。要拆成多机跑，可给每台机器不同的 `state_dir` + 各自的媒体目录。

## 10. 中断恢复

**旧**：trap 处理 SIGINT/SIGTERM 会还原；但 kill -9、DSM 重启、OOM 会留下
`.transcode_<pid>.old.mp4` 和半截 tmp，需要手工收拾。

**新**：daemon 启动时扫描任务库，把"running 但 worker 已不在"的任务结算：
还原 `.old` → 源文件、删半截输出、标记 `interrupted`（可 `r` 重试）。
`./tctui doctor` 会列出残留 `.tctui-*` 文件，`./tctui clean --yes` 清理。

## 11. 单卡串行 → 多卡多路

**旧**：一个文件一个文件串行转，只用默认 GPU。

**新**：自动发现所有卡、实测会话上限与吞吐饱和点、按显存/负载分配；
`gpu.prefer` 可选 `auto / least-loaded / round-robin`；
某张卡报 NVENC 错误会自动换卡重试（最多 2 张）。

**改回去**：`gpu.max_parallel_per_gpu=1` + `gpu.exclude` 掉不用的卡。

## 12. 旧的处理日志

**旧**：每个目录一个 `video_transcode_log.txt`，记录相对路径。

**新**：迁移到 `state/state.db` 的 `processed` 表；首次扫描会把各 watch 根目录下
已有的 `video_transcode_log.txt` 导入（`scan.import_legacy_log`），不丢历史。

## 13. 磁盘空间

**旧**：先 rename 源文件，再写 tmp；空间不够就写满磁盘。

**新**：派发前检查 `剩余空间 ≥ 源大小×1.05 + 512MB + policy.min_free_disk_mb`，
不够就延后 5 分钟再试，并写事件日志。

## 14. ffmpeg 吞 stdin 的坑

**旧**：循环里调 ffmpeg 没加 `-nostdin`，如果脚本的 stdin 是文件/管道，ffmpeg 会把它
当成交互命令读走，导致后续循环"莫名其妙"不执行（我在测试旧逻辑时复现了）。

**新**：worker 固定 `-nostdin`，且 stdout 只给 `-progress pipe:1`，stderr 进任务日志。

## 14.5 低码率片源白烧显卡（新增开关）

实测发现：已经被压过的 720p（~1 Mbps）用 cq27 重压，多数只能省 0~10%，甚至变大。
新增 `scan.skip_if_source_bitrate_kbps`（默认 0=不启用，按 1080p 归一化），
设成 1500 就能跳过这类文件；也可以把 `encode.cq` 从 27 提高到 30–32。

## 15. 输出容器与命名

**旧**：`OUTPUT_EXT=mkv`，`mv tmp 原名.mkv`，同名冲突时加 `_1`。

**新**：默认 mkv（保留全部音轨/字幕）；`movie.mp4 → movie.mkv`；`movie.mkv` 原地替换
（临时文件 + 原子 rename）。目标已存在则 `_1`。想保留 mp4 容器：
`"out_ext": "mp4"`（会自动加 `hvc1` tag、faststart，兼容 Apple 设备）。

## 16. 新增的质量校验

**新**：转码后用 ffprobe 对比时长，输出 < 源 97% 视为失败（防止 ffmpeg 静默截断）。
`policy.verify_min_duration_ratio` 可调，设 0 关闭。

## 16.5 取消/中断的任务会再次入队

`canceled` / `interrupted` 不会写进 processed，所以下一次扫描会重新排队（方便修好后重跑）。
如果不想这样，把文件移出扫描路径，或把 `scan.min_size_mb` 调大临时避开。

## 16.8 驱动把 NVENC 会话上限做成了"全系统共享"

实测这台机器：单卡可以开 12 路，但三卡合计超过 12 路就会随机报
`OpenEncodeSessionEx failed: incompatible client key (21)`，跟用哪张卡无关。
所以旧版"三卡同时探测"会让两张同型号 2080 Ti 报出 6 和 8 两种上限——不是硬件差异。

新版：**不再探测**。并发完全手动：`gpu.max_parallel_per_gpu`（每张卡同一个值），
没有全局上限，总并发 = 启用卡数 × 它。调度时靠这个值和显存余量决定是否派发。

代价：不再自动避开整机 12 路会话上限，需要自己保证 `卡数 × 单卡并发 ≤ 12`
（本机三卡建议每卡 4）；超过会随机报 `incompatible client key (21)`。

## 17. CPU 回退

**旧**：没有，显卡不可用就报错。

**新**：默认关闭。`encode.fallback_to_cpu=true` 时，所有卡都不可用会用 libx265
串行兜底（`encode.cpu_parallel` 控制并行度）。NAS 的 CPU 压 4K 会很慢，慎开。
