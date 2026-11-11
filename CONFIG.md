# 配置在哪里改

三个地方，效果一样，按你顺手的来。

## 1. 直接编辑文件（最全，推荐先看一遍）

```
/volume1/docker/ffmpeg-tui/config.json
```

* 只写**你要改的项**，没写的自动用默认值（默认值见 `./tctui config --example` 导出的 `config.example.json`）
* 必须是有合法 JSON（**不能写 `//` 注释**，注释写在本文档里）
* 保存后 **daemon 2 秒内自动热加载**，不用重启、不影响正在跑的任务
* 显卡并发（`gpu.max_parallel_per_gpu`）改完 2 秒热加载，不需要重启

## 2. 命令行改（不用开编辑器）

```bash
cd /volume1/docker/ffmpeg-tui

./tctui config --where                     # 看配置/状态/日志到底在哪
./tctui config                             # 打印合并后的完整配置
./tctui config --keys                      # 列出所有可设置项及当前值
./tctui config --get encode.cq --get gpu.max_parallel_per_gpu
./tctui config --set encode.cq=25
./tctui config --set scan.skip_if_codec=[]
./tctui config --set series.delete_sources_after_success=false
./tctui config --set encode.audio=aac --set encode.audio_bitrate=192k
```

* 值按 JSON 解析：`25` 是数字、`true/false` 是布尔、`[]` 是空数组、`"aac"` 也可直接写 `aac`
* 写错 key 会直接报错并提示相近的名字（不会静默写进无效配置）
* 改完会打印每一项的 `旧值 -> 新值`

## 3. 界面里改（鼠标点，或键盘 m）

按 `m`（或点标题栏 `⚙ 设置`、底栏 `m 设置`）打开设置面板：

| 操作 | 效果 |
|---|---|
| 点 `◀` / `▶` | 减 / 加（CQ、阈值、并发等） |
| 点 `[ 开 ]` / `[ 关 ]` | 切换开关（分集删除、保留策略、跳过 HEVC、显卡启用…） |
| 右键点数值 | 反向调整（减） |
| 点面板外 / `Esc` / `m` / `q` | 关闭面板 |
| 滚轮 / `↑↓` | 在面板里滚动选择 |
| `←` `→` / `Enter` | 键盘等价操作 |

面板里能改：视频编码（H.265/H.264/AV1，只列出显卡实测支持的）、CQ、预设、
输出容器（mkv/mp4/mov）、最大宽度、音频/字幕、HDR、压缩率阈值、保留策略、
时长校验、分集合并与删源、跳过 HEVC、低码率跳过、最小文件、**扫描哪些扩展名**
（点开后是勾选面板，含全选/仅常用/反选）、单卡最大并发、显卡启用开关、
扫描/暂停按钮。

每一项都**立即写入 `config.json`** 并显示"已保存，2 秒内生效"。
另外按数字键 `1`–`9` 也能直接开关显卡。

其余（数组型参数如 `extra_args`、`gpu.vram_per_job_mb` 等）仍然在文件里改。

---

# 你需要拍板的 6 件事 → 对应配置

| # | 决策 | 现在的默认 | 想改成另一种 | 怎么改 |
|---|------|-----------|-------------|--------|
| 1 | 分集何时删除 | 转码成功且通过校验后才删 | 保留分集（不删） | `./tctui config --set series.delete_sources_after_success=false` |
| 2 | 压缩率不达标 | 记为"保留"，不再自动重跑；界面可按 `r` 单条重试 | 每次扫描都重试 | `./tctui config --set policy.mark_kept_as_processed=false` |
| 3 | 已是 HEVC | 跳过，不重压 | 也重压（比如 10bit→8bit SDR） | `./tctui config --set scan.skip_if_codec=[]` 再配合 `encode.hdr=force-sdr` |
| 4 | 音频 | 原样复制（无损、快） | 统一转 AAC 192k | `./tctui config --set encode.audio=aac` |
| 5 | 单卡并发 | 4 路（每张卡） | 改成别的值 | `./tctui config --set gpu.max_parallel_per_gpu=3` |
| 6 | 低码率老片 | 不跳过（`0`=关） | 跳过 1080p 归一化后 <1500kbps 的 | `./tctui config --set scan.skip_if_source_bitrate_kbps=1500` |

> 第 5 项：`gpu.max_parallel_per_gpu` 是**唯一的并发设置**，三张卡都用这个值；
> 没有全局并发上限，总并发 = 卡数 × 它。整机 NVENC 会话上限是 12，别超过；
> 设 4 之后仍然会受"实测会话上限"和显存约束，不会把卡跑崩。

---

# 全部配置项

## 路径 / 目录

| key | 默认 | 说明 |
|---|---|---|
| `paths.ffmpeg` | 你的 7.1.3 静态版 | ffmpeg 绝对路径 |
| `paths.ffprobe` | 同上目录 | ffprobe 绝对路径 |
| `paths.nvidia_smi` | `nvidia-smi` | 可用绝对路径 |
| `path` | `""`（第一次安装为空） | **唯一的扫描目录**。按 `a` 或设置面板设置后会一直用它；再设置会替换。daemon 启动/按 `s` 扫这个目录 |
| `state_dir` | 空 = `<安装目录>/state` | 任务库/显卡缓存位置，一般不用改 |

## 扫描 `scan.*`

| key | 默认 | 说明 |
|---|---|---|
| `exts` | mp4,mkv,avi,… | 参与扫描的扩展名 |
| `exclude_dirs` | `@eaDir`, `#recycle`, … | 整目录跳过（DSM 的缩略图目录必须排除） |
| `min_size_mb` | `20` | 小于这个大小的文件不处理 |
| `skip_if_codec` | `["hevc"]` | 这些编码的源直接跳过 |
| `skip_if_tagged` | `Transcoded By HServer` | 有同值 comment 的视为已转码 |
| `skip_if_source_bitrate_kbps` | `0` | 0=关。按 1080p 归一化后低于此码率则跳过 |
| `skip_if_pix_fmt_10bit` | `false` | true 时 10bit 源一律跳过 |
| `follow_symlinks` | `false` | 是否跟随软链接 |
| `max_files_per_scan` | `20000` | 单次扫描上限，防手滑扫到根目录 |
| `on_start` | `true` | daemon 启动且队列为空、且已设置 `scan.path` 时自动扫描它 |
| `import_legacy_log` | `true` | 首次扫描导入旧脚本的 `video_transcode_log.txt` |
| `legacy_log_name` | `video_transcode_log.txt` | 旧日志文件名 |

## 分集 `series.*`

| key | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否自动合并分集 |
| `pattern` | `^(?P<base>.+?)[._\- ]?(?P<num>\d{1,4})$` | 末尾数字识别；`base`/`num` 是命名分组，改名规则就改这里 |
| `min_files` | `2` | 至少几个才算一组 |
| `delete_sources_after_success` | `true` | **成功后才删**分集（失败/保留时一定不动） |
| `require_same_codec` | `true` | 编码不同就不合并（concat copy 会坏），改为逐集处理 |
| `require_same_resolution` | `true` | 分辨率不同也不合并 |

## 编码 `encode.*`

| key | 默认 | 说明 |
|---|---|---|
| `video_codec` | `hevc_nvenc` | 也可 `h264_nvenc` / `av1_nvenc`（卡支持才行） |
| `preset` | `p7` | NVENC 预设 p1(最快)…p7(最慢最好) |
| `cq` | `27` | 质量/体积旋钮，越小越清晰越大。想更省体积试 30–32 |
| `out_ext` | `mkv` | 输出容器；`mp4` 会自动加 `hvc1`+faststart |
| `audio` | `copy` | `copy`/`aac`/`opus`/`flac`/`none` |
| `audio_bitrate` | `192k` | 仅重编码音频时生效 |
| `subs` | `copy` | `copy`/`none` |
| `max_width` | `0` | 0=保持原分辨率；如 `1920` 则超宽会缩小 |
| `hdr` | `auto` | `auto` 保留 10bit+色彩标记；`force-sdr` 转 SDR；`ignore` 不特殊处理 |
| `tune` | 空 | 透传给 `-tune` |
| `tag` | `Transcoded By HServer` | 写入 comment，用于"已转码"识别 |
| `extra_video_args` | `[]` | 追加到 `-c:v` 附近的参数，如 `["-rc","vbr","-multipass","qres"]`。**不要写 `-vf`/`-filter_complex`**（会与内置 cuda 滤镜链冲突） |
| `extra_args` | `[]` | 追加到输出前的参数 |
| `fallback_software_decode` | `true` | 硬解失败自动软解重试一次 |
| `fallback_to_cpu` | `false` | 全部卡不可用时用 libx265 兜底 |
| `cpu_codec` / `cpu_preset` / `cpu_parallel` | libx265 / medium / 1 | CPU 兜底参数 |
| `max_muxing_queue_size` | 4096 | 异常流时加大 |

## 策略 `policy.*`

| key | 默认 | 说明 |
|---|---|---|
| `keep_original_min_savings_pct` | `10` | 压缩率低于此值：删输出、留源文件、记"保留"。**输出不小于源文件时永远保留源**；`0` = 只要变小就替换 |
| `mark_kept_as_processed` | `true` | "保留"是否记为已处理（关掉就会每次扫描重试） |
| `verify_min_duration_ratio` | `0.97` | 输出时长/源时长短于此比例视为失败（防静默截断），0=关 |
| `delete_temp_on_failure` | `true` | 失败时删除半截输出 |
| `min_free_disk_mb` | `1024` | 转码前磁盘最少剩余；不够就延后 5 分钟 |

## 显卡 `gpu.*`

| key | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 关掉就只走 CPU 兜底 |
| `exclude` | `[]` | 永不用这些卡（索引或 UUID），界面 `1-9` 改的就是它 |
| `reserve_vram_mb` | `1024` | 每卡预留显存 |
| `min_free_vram_mb` | `512` | 派发后仍要剩下的显存 |
| `vram_per_job_mb` | `{720:320,1080:640,1440:1000,2160:1800}` | 单任务显存预估（按帧高查表） |
| `max_parallel_per_gpu` | `4` | 单卡最大并发（所有卡共用这个值）；总并发 = 卡数 × 它，别超过整机会话上限 12 |
| `prefer` | `auto` | `auto` / `least-loaded` / `round-robin` |

## 守护进程 / 界面

| key | 默认 | 说明 |
|---|---|---|
| `daemon.poll_seconds` | `0.5` | 主循环间隔 |
| `daemon.sample_seconds` | `2.0` | 写显卡采样（画走势图）的间隔 |
| `daemon.history_hours` | `2` | 采样保留时长 |
| `daemon.log_level` | `info` | 预留 |
| `ui.host` | 空=主机名 | 标题栏显示的名字 |
| `ui.mouse` | `true` | 鼠标支持；想用终端原生选择/复制文字时设 false（重启界面生效） |
| `ui.refresh_seconds` | `1.0` | 界面刷新 |
| `ui.log_tail_lines` | `8` | 详情页日志行数 |

---

# 三个可直接粘贴的预设

### A. 稳健默认（推荐先用这套）
```json
{
  "scan": {"path": "/volume2/video"},
  "policy": {"keep_original_min_savings_pct": 10},
  "encode": {"preset": "p7", "cq": 27, "out_ext": "mkv", "audio": "copy"}
}
```

### B. 想明显省空间（画质略降）
```json
{
  "encode": {"cq": 30, "preset": "p6", "audio": "aac"},
  "policy": {"keep_original_min_savings_pct": 15},
  "scan": {"skip_if_source_bitrate_kbps": 1500}
}
```

### C. 4K HDR 想转成 SDR 8bit（CPU 会忙）
```json
{
  "scan": {"skip_if_codec": ["hevc"], "skip_if_pix_fmt_10bit": false},
  "encode": {"hdr": "force-sdr", "cq": 26}
}
```
> 只有源是 HDR 且你确实想转 SDR 时才用；`hdr=auto` 会原样保留 10bit HDR 标记。

---

# 生效规则

| 改了什么 | 怎么生效 |
|---|---|
| `scan.path` / `scan.*` / `series.*` / `encode.*` / `policy.*` | 保存即生效（daemon 2 秒内热加载）；排队中的任务在下次派发时用新参数，正在跑的任务不受影响 |
| `gpu.exclude` / `gpu.max_parallel_per_gpu` | 保存后 2 秒热加载 |
| `paths.*` / `state_dir` | 需要 `./tctui daemon --stop && ./tctui daemon --start` |
| 界面里的 `1-9` | 立即生效，并写回 `config.json` |

不需要重启界面；界面每秒读一次状态库和配置。
