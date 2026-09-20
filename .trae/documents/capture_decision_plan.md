# 统一 CaptureDecision 采集策略实施计划

## Repository Research（现状）

采集主循环在 [background.py](file:///Users/kkcarrot/swe-project/Memento_fork2/memento/background.py) 的 `Background.run()`，当前每 2 秒（FPS=0.5）：

1. `utils.get_active_window()`（[utils.py#L23-L36](file:///Users/kkcarrot/swe-project/Memento_fork2/memento/utils.py#L23-L36)）只返回 X11 `WM_CLASS`，**没有窗口标题、没有显示器信息**。
2. `mss` 抓 `monitors[1]` → 去 alpha → resize 到 1920x1080。
3. `asyncio.run(self.rec.new_im(im))`：像素**立即**进入 H264 编码（[utils.py#L83-L89](file:///Users/kkcarrot/swe-project/Memento_fork2/memento/utils.py#L83-L89)）。
4. **同一原始像素**被 `put` 进多进程 `images_queue`（跨进程排队发生在 OCR 之前），worker 做 imgdiff / OCR。
5. 元数据（`window_title`、`time`，随后还有 `bbs`、`text`）写入分段 JSON（[caching.py](file:///Users/kkcarrot/swe-project/Memento_fork2/memento/caching.py)）；OCR 文本入 SQLite FTS（[db.py](file:///Users/kkcarrot/swe-project/Memento_fork2/memento/db.py)）；段完成后合并文本 + metadata（含 `id`/`time`/`window_title`）入 ChromaDB（OpenAI embedding，云端出口）。
6. Timeline 侧：`frame_i` 全局连续编号，视频文件 `{frame_i // 5}.mp4`，Reader 按「帧序号 = frame_i - offset」直接下标取帧；`TimeBar` 按每帧 metadata 的 `window_title` 分段并在悬停时预览原始帧；搜索走 FTS；Chat 走 Chroma/OpenAI 并显示帧缩略图；`ctrl+c` 复制 OCR 文本；框选区域可即时 OCR。

关键约束：编号、视频分片、JSON 分段、Reader offset 全部依赖 frame_i 连续；drop 帧若不编码，必须引入缺口→实际编码位置的翻译层。

## 总体设计

新增两个纯逻辑模块（可离线单测），在抓取与 `Recorder.new_im` 之间插入唯一判定点；判定结果与该帧像素绑定为同一采集事件，先处置、再 fan-out（编码 / 排队 / OCR / 元数据 / 云端）。

- `allow`：现状路径。
- `drop`：**不编码、不入 images_queue、不 OCR、不入 DB/Chroma**；只写一条不含窗口标题的缺口元数据（时间 + 命中规则 id）。
- `redact`：在 `new_im`、`images_queue.put`、OCR 之前就地改像素（默认不透明黑块，保证 OCR 不可恢复），随后走正常编码/OCR 路径。
- 云端（embedding 入库 + Chat 发给 LLM 的 metadata）走独立 `EgressPolicy` 字段白名单（默认仅 `id`、`time`）。
- 旧录像：无治理标记 / metadata 无 `decision` 字段 → 判定恒为 allow，Timeline 维持现状；只有标记之后的新录制受约束。

## Files and Modules

- `memento/policy.py`（**新增**）：`CaptureDecision`、`CaptureContext`、`CapturePolicy`（规则加载、规范化身份/标题、显示器、时间窗、用户区域匹配）、像素 redaction、治理标记读写。
- `memento/egress.py`（**新增**）：`EgressPolicy`，metadata 字段白名单过滤。
- `memento/utils.py`：新增 `get_active_window_info()`（WM_CLASS + `_NET_WM_NAME`/`WM_NAME` 标题 + 经 `translate_coords` 计算窗口所在 display 序号）、`POLICY_PATH`/`GOVERNANCE_MARKER` 常量、`GAP_APP = "__memento_gap__"` 哨兵；`Recorder` 改为首个帧才建 stream，支持整段全 drop 的空文件。
- `memento/background.py`：抓取后、编码前调用策略；三分支处置；缺口元数据；Chroma 入库前过 egress 过滤。
- `memento/caching.py`：`Metadata`/`MetadataCache` 增加每段缺口排序列表（惰性、缓存）；`Reader` 按缺口计数把 frame_i 翻译成实际解码下标，空/缺失视频文件容错为空帧列表。
- `memento/timeline/frame_getter.py`：`is_gap(frame_i)`；缺口帧返回本地合成的占位卡片（深色底 + "Not recorded" + 规则 id，不含任何抓屏像素）；对缺口跳过 annotation。
- `memento/timeline/time_bar.py`：缺口感知分段（`window_title` 缺失按 `GAP_APP` 处理）、缺口段固定深色样式；悬停缺口不预览、时间提示改为显示命中规则原因。
- `memento/timeline/apps.py`：`GAP_APP` 固定颜色、无 icon 的兜底，扫描处兼容无 `window_title` 的帧。
- `memento/timeline/timeline.py`：缺口帧禁止框选 OCR、禁止 `ctrl+c` 复制，弹提示说明原因。
- `memento/timeline/search_bar.py`：FTS 结果中的缺口 id 防御性过滤（正常情况下 drop 从无 FTS 行）。
- `memento/timeline/chat.py`：query 子进程组装 md 后过 `EgressPolicy` 再发 LLM；回答中的缺口 frame_id 不出缩略图。

## 规则配置（用户定义）

路径：`$XDG_CONFIG_HOME/memento/policy.json`（无 XDG 时 `~/.config/memento/policy.json`），首次受治理启动时写默认模板（`default_action: allow`、空规则）。Schema：

```json
{
  "version": 1,
  "default_action": "allow",
  "redaction": "black",
  "regions": [[100, 100, 300, 40]],
  "rules": [
    {"id": "work-hours-block", "action": "drop",
     "apps": ["1password", "keepassxc"]},
    {"id": "incognito", "action": "redact",
     "title_contains": ["incognito", "private"]},
    {"id": "evening", "action": "drop",
     "time": {"weekdays": [0,1,2,3,4], "start": "19:00", "end": "09:00"}},
    {"id": "monitor2", "action": "drop", "displays": [2]},
    {"id": "slack-sidebar", "action": "redact", "apps": ["slack"],
     "regions": [[0, 0, 220, 1080]]}
  ],
  "egress": {"allowed_fields": ["id", "time"]}
}
```

- 匹配字段可任意组合（AND）；规则按序首条命中生效；顶层 `regions` 恒生效。
- 坐标空间统一为录制分辨率 1920x1080（resize 之后判定与 redact，坐标空间唯一）。
- `redact` 不带 `regions` → 整帧；带 `regions` → 仅遮罩指定矩形；`drop` 永不带像素输出。
- app 身份规范化：WM_CLASS 小写、去空白；标题匹配用小写包含/正则。

## 治理标记与旧录像兼容

- 全新启动（或 erase）时在 `CACHE_PATH` 写 `governance.json`（version + 配置快照）；continue 旧缓存且无标记 → legacy 模式：策略恒 allow、不写 decision 字段。
- Timeline 只读路径只依据 metadata 中 `decision == "drop"` 识别缺口；旧 JSON 无该字段，所有现有行为（预览/复制/搜索/聊天）不变。

## 元数据 schema（仅新录制）

- allow：现有字段 + `"decision": "allow"`
- drop：`{"time": ..., "decision": "drop", "rule_id": "..."}`（**无 window_title**）
- redact：现有字段 + `"decision": "redact", "rule_id": "...", "regions": [[x,y,w,h], ...]`

## Implementation Steps（依赖顺序）

1. `utils.py`：常量与哨兵；`get_active_window_info()`（app/title/display，保留旧 `get_active_window`）；`Recorder` 惰性建 stream + 空文件安全 close。
2. 新增 `policy.py`：数据类、规范化、规则匹配（apps/title/displays/time/regions）、`decide(context)`、`apply_redaction(im, regions)`、配置加载与治理标记。
3. 新增 `egress.py`：`EgressPolicy.filter_metadata()` / 批量过滤，白名单来自配置。
4. `background.py` 接线：resize 之后立即构造 context 并判定；drop 跳过 `new_im`/queue、写缺口元数据；redact 先改像素再编码与入队；Chroma `add_texts` 的 metadatas 过 egress。
5. `caching.py`：每段缺口 id 排序列表 + bisect 计数；`Reader` 用缺口计数翻译 frame_i→解码下标，容错空文件。
6. `frame_getter.py`：`is_gap`、合成缺口卡片（cv2 绘制，含规则 id）。
7. `time_bar.py` + `apps.py`：缺口段渲染、无预览、原因提示；兼容缺 `window_title` 的 metadata。
8. `timeline.py`：缺口帧禁框选 OCR / 禁复制并提示。
9. `search_bar.py`、`chat.py`：缺口过滤 + Chat egress 白名单 + 缺口不出缩略图。
10. 清理默认策略模板与文档字符串；不新增 README/文档文件。

## Dependencies and Considerations

- drop 不推进 `Recorder` PTS：编码视频时长短于墙钟时间，时间以 metadata `time` 为准；frame_i 与分片号仍按「每个采集周期」递增（现有分片轮转逻辑不变），Reader 靠缺口计数翻译下标。
- 全 drop 分片产生 0 帧 mp4：Reader 打开失败/空文件时回退为空列表，不影响 `nb_frames` 计算（仍按 mp4 个数 ×5）。
- redact 在主进程就地修改 ndarray，发生在 multiprocessing pickle 之前，worker 物理上拿不到原像素；`prev_im` 也沿用 redact 后图像。
- drop 原因只存用户自定义的规则 id，不存原始窗口标题，避免缺口本身泄露敏感信息。
- Chat 的 egress 过滤同时作用于旧库读出的 doc metadata（发送前过滤），不影响本地回放语义。
- 仅抓取 monitors[1] 是现状；display 规则针对活动窗口所在显示器编号（窗口几何与 mss monitor 矩形求交），多显示器扩展抓屏不在本次范围。
- 运行环境为 macOS，X11/mss/tesserocr 无法在此机实跑；采集链路以静态检查 + 纯逻辑测试验证。

## Validation

- `python3 -m py_compile` 覆盖所有改动文件。
- 用内联 Python（不落测试文件）验证纯逻辑：
  - 规则匹配：apps/标题包含/正则/显示器/跨午夜时间窗/首条命中/默认 allow；
  - redaction 后目标区域像素全黑且区域外不变；
  - EgressPolicy 只留白名单字段；
  - 含缺口分段的 frame_i→解码下标翻译正确（含空分片、连续多缺口）。
- `GetDiagnostics` 确认无静态错误。

## Risks

- **X11 标题/坐标 API 差异**：`_NET_WM_NAME` 缺失时回退 `WM_NAME`，取不到标题/几何时分别回退空串与 display=1，不阻塞采集。
- **空 mp4 分片 av 行为差异**：惰性建 stream + Reader 容错双重兜底。
- **规则误配导致大量 drop**：缺口在 Timeline 明确可见且带规则 id，用户可直接定位修正配置；默认模板为空规则（allow-all），升级即无行为变化。
