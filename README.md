# Absolutely autonomous memory

**绝对自主记忆**：一个给「她自己」用的长期记忆插件，不是系统拿来控制她的资料库。

> 系统只做两件事：**注意到**发生了什么，在她需要时**把材料递过去**。
> 不替她决定该想什么、该怎么感受、要不要提起。

AstrBot 插件，作者：婉清。MIT License。

---

## 它解决什么问题

大多数「长期记忆」是在替角色总结、替角色回忆，最后角色背下来的是一段第三人称的报告。

这个插件反过来：

- 原文只追加，不改写她看到过的对话
- 记忆有版本链，改口是追加新版本，旧版永远留着
- 她可以自己翻、自己钉原话、自己补一句
- 系统不替她判断「这件事该不该记得」「这个人该不该提」

---

## 功能

### 写入

| 能力 | 说明 |
|---|---|
| 原文归档 | 每段对话落一个 `episode`，`raw_message` 保留原话，只追加 |
| 异步提炼 | 空闲 10 分钟或攒够一批后提炼，不阻塞聊天 |
| 七类痕迹 | `fact` / `observation` / `speculation` / `self` / `unresolved` / `promise` / `agreement` |
| 版本链 | 同一件事的新说法追加版本，旧版不删 |
| 三块字段 | `text` 只写事实与动作；`felt` 当时的感觉；`reaction` 当时的反应；另有 `inference` + 置信度 |
| 原话 | 提炼时留一句 `anchor_quote`，完整对话在 `episode.transcript` |
| 去重 | 同类型近似重复按时间窗归并；不同天的发生不合并，检索按同系列折叠 |

### 她自己的东西

| 能力 | 说明 |
|---|---|
| `leave_my_words` | 在一条记忆后面留一句自己的话 |
| `keep_quote` | 自己钉一句真正的原话，覆盖提炼时那句的显示 |
| `read_memory(original=True)` | 按需查看当时那段对话原文 |


### 常驻区（她自己放的）

`resident_add` / `resident_retire` / `resident_list`：她自己决定放什么、撤什么；每轮原样注入，不筛选、不总结；最多 12 条 / 1200 字，满了拒绝，不自动清。

### 系列（她自己分的）

`series_create` / `series_rename` / `series_add` / `series_remove` / `series_retire` / `series_list` / `series_show`：把一类反复发生的事挂成系列；只加链接，不改正文、不删记忆；一条记忆可以同时属于多个系列；`series_show` 支持 `since` / `until` / `limit` / `query`，按对话时间排。

### 检索

- 关键词 FTS + 向量（默认 Ollama `bge-m3`，可配置）
- 关键词 / 向量 / 时间三路加权，固定公开的排序规则
- 支持按时间、场景、对象筛选：
  - `since` / `until`：`2026-11-10`、`昨天`、`最近3天`、`7天前`
  - `scope`：`私聊`、`群聊`、`群:123456`
  - `person`：按正文和主题词匹配名字
- 向量失败自动降级关键词，不阻塞聊天

### 遗忘与未了

```
0–7 天     正常挂着，可主动提
7–30 天    转 stale（久了没提），不再主动提
30 天以上  转 old（很久没提），几乎不浮现
```

- 计时只看 `last_mentioned_at`（真被提起），被检索/想起不算
- 到期未提醒的约定不提前沉默
- 重新提起会复活
- 3 天没动静问一句「还算数吗」；7 天没回应转 `dormant`（休眠，不删、不判作废）
- 不自动了结、不删除

### 走神（可选）

事件触发，不是纯定时：

```
重要新记忆      → 5 分钟后入队
旧事转沉默      → 1 小时后入队
隔 6 小时回来    → 立即入队（只私聊）
没事时          → 每 240 分钟心跳一次
```

每次触发：80% 用触发记忆做种子、从相关候选里随机挑；20% 完全随机；7 天内真想过的不再重复。

决策必须输出 JSON：

```json
{"decision":"speak 或 silent","source_trace":123,"reason":"...","message":"..."}
```

来源必须指回眼前这条，否则判幻觉丢弃；沉默也会落库。

护栏：每日最多说 2 条、最多想 6 次、静默 1–7 点、对方刚说话 30 分钟内不打扰。

> 走神只走私聊。群聊主动发话是另一个工具，见下。

### 群聊主动发话（可选）

工具 `send_group_message(text, group="")`：

- 她决定说，才说；不是系统定时在群里找话
- 群白名单（空 = 全部群）、每群每日上限（0 = 不限）、静默时段
- 不填群号就用当前群；在私聊里用需带群号
- 默认关闭，必须显式开启；白名单留空表示不限制群

### 核心层

`core_memory`：她自己写的长期自我认识，旧条不删，`read_core_memory` / `write_core_memory` 读写。

---

## 工具一览

| 工具 | 作用 |
|---|---|
| `recall_memory` | 翻记忆；支持 query / kind / limit / since / until / scope / person / sort |
| `sort_last_recall` | 把刚才那批按时间/相关度重排，不重新搜 |
| `read_memory` | 深读一条；`original=True` 看当时对话 |
| `recall_day` | 按天回想 |
| `memory_directory` | 记忆目录 |
| `list_unresolved` | 未了清单 |
| `review_loose_ends` | 翻沉下去的事 |
| `settle_memory` | 了结 / 作废 |
| `tag_memory` | 补主题词 |
| `avoid_memory` | 回避某条 |
| `keep_quote` | 自己钉一句原话 |
| `leave_my_words` | 留一句自己的话 |
| `read_core_memory` | 读核心层 |
| `write_core_memory` | 写核心层 |
| `send_group_message` | 在允许的群里主动说一句 |
| `resident_add` / `resident_retire` / `resident_list` | 她自己放的常驻区 |
| `series_*` | 她自己分的系列：建 / 改名 / 加 / 移出 / 撤 / 列 / 看 |

命令：`/mem ...`（管理员）

---

## 安装

要求 AstrBot `>=4.24.2,<5`。

1. 把整个目录放到 AstrBot 插件目录：

```
AstrBot/data/plugins/astrbot_plugin_absolutely_autonomous_memory
```

2. 安装可选依赖（面板 / 向量）：

```bash
pip install -r requirements.txt
```

3. 在 AstrBot 后台配置里填模型与向量服务，重载插件。

4. 确认日志出现：

```
Plugin astrbot_plugin_absolutely_autonomous_memory (vX) by 婉清
```

只有 `Failed to load ... 未通过 Star 注册` 表示没加载，不是成功。

### 向量（可选）

默认 `embedding_mode = ollama`，地址 `http://127.0.0.1:11434/api/embed`，模型 `bge-m3`。

没有向量服务也能用：关键词检索仍工作，向量会自动降级并在 5 分钟后重试。

### 最小配置

| 项 | 默认 | 说明 |
|---|---|---|
| `provider_id` | 当前会话模型 | 提炼用哪个模型 |
| `idle_minutes` | 10 | 空闲多久结一次 |
| `batch_size` | 4 | 攒多少行触发 |
| `stale_days` / `old_days` | 7 / 30 | 两段沉默 |
| `proactive_reminder_enabled` | true | 到点提醒 |
| `wander_enabled` | false | 走神，默认关 |
| `group_send_enabled` | false | 群聊主动发话，默认关 |
| `group_send_allow` | 空 | 空 = 全部群；要先开 group_send_enabled |
| `group_send_max_per_day` | 0 | 0 = 不限 |

完整项见 `_conf_schema.json`。

---

## 数据与隐私

- 数据默认在 AstrBot 的 `data/plugin_data/astrbot_plugin_absolutely_autonomous_memory/`
- SQLite 单文件，本地；插件不会把记忆上传到任何地方
- 数据库里是真实对话和记忆，**不要提交到仓库**
- `.gitignore` 已排除 `*.db`、备份、`panel-python.txt` 等本机文件

---

## 面板

```bash
python webui.py --port 21600
```

- 只监听 `127.0.0.1`
- 标签：最近 / 未了的事 / 按主题 / 系列 / 总览 / 统计 / 原话
- 详情显示版本链、她自己的话、她留的原话、提炼原话、感觉/反应/推断
- 可改重要度、加旁注、标作废或了结、改写（追加版本）

Windows 可用 `open-panel.ps1`；需要指定 Python 时设 `AAM_PYTHONW`。

---

## 测试

```bash
python smoke_test.py       # 建库 / 写入 / 向量 / 检索
python wander_test.py      # 走神
python experience_test.py  # 三块字段 / 原话 / 时效
python group_test.py       # 群聊主动发话 / 走神不进群
python eval_recall.py      # 检索评测
```

---

## 已知限制

- `person` 是正文/主题词子串匹配，不是身份 ID；只写「他」就筛不到
- 只给时间/场景筛选、不给 query 时，`recall_memory` 返回空；按天翻用 `recall_day`
- 老记忆没有 `felt` / `reaction` / `inference`，只有升级后新写的才有
- `her_words` / `quote` 旁注不绑定版本，只挂在 trace 上
- 跨人私聊没有硬隔离，靠 scope 和提取时写名字；介意的话自己加过滤
- 走神与群发的效果高度依赖模型与提示词，建议先 `wander_dry_run` 观察

---

## 设计约束

1. **对话历史永不修改**：更正、提醒只做临时系统文本注入。
2. **原文只追加**：`episode` / `trace_version` / `raw_message` 不改写。
3. **系统不替她判断**：可以检索、可以递材料，但想起什么、说不说、算不算数，由她决定。

---

## English

An AstrBot plugin for long-term memory that stores and delivers, but does not decide for the character.

- append-only episodes and versioned traces
- seven memory kinds, three-stage silence, dormant state
- hybrid keyword + vector retrieval with time / scope / person filters
- she can keep her own quote and add her own words
- optional event-triggered wandering and whitelisted group messages
- local SQLite, no upload

MIT License.

---

## License

MIT，见 [LICENSE](LICENSE)。
