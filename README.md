# 主人的叮嘱（owner-reminder）

让**主人**可以在任意聊天流下达一条"叮嘱"，**作用于另一个指定的聊天流**：
这条叮嘱会在目标流 planner 的上下文里以「强 → 弱」两档出现，
**投递过提醒的叮嘱一旦真的兑现（她在那个流开口了）就立刻消费掉**，
没兑现的则由 TTL（默认 10 分钟）兜底淡出。纯插件实现，**不改主程序**。

定位是**短期态度影响**，不是实时制止 —— 因为麦麦的架构是「上下文驱动 + planner 轮次 + 频率门控」，
"她正在吵 → 我私聊一句 → 她立刻闭嘴"不现实；"我交代一句 → 她接下来在那个群收敛"才是这个插件做的事。

有两条下达通道，**走同一套落盘 / 投递 / 注入链路**：

| 通道 | 怎么用 | 特点 |
|---|---|---|
| 命令 | `/叮嘱 943355126 别吵了` | 确定性强，可兜底 |
| 自然语言（工具） | 私聊里直接说「跟技术群的人说别再吵了」 | 由**她自己的 planner** 识别意图后调用 `owner_directive` |

## 命令

| 命令 | 作用 |
|---|---|
| `/叮嘱 <群号\|代号> <内容>` | 对指定聊天流下达叮嘱 |
| `/叮嘱 @here <内容>` | 对当前聊天流下达叮嘱 |
| `/叮嘱 <内容>` | 同上（首个 token 既不是群号也不是已登记代号时，整段都当作内容） |
| `/叮嘱列表` | 查看生效中的叮嘱、剩余时间与当前档位 |
| `/叮嘱撤销 <编号\|all>` | 撤销一条或全部 |

`/influence` 是 `/叮嘱` 的别名；`列表/撤销` 也支持 `list` / `revoke` 写法。
只有配置里 `owner.owners` 白名单内的账号可以下达，其他人**静默忽略**（不提示、不回执）。

实现上这些是**一个命令组件** `owner_reminder`（正则把命令词之后的内容整段交给插件解析，
再分派到下达 / 查看 / 撤销），所以命令名写在代码里，改名需要改插件代码。

## 自然语言通道与群代号

两个工具，都声明 `chat_scope="private"` —— **只在私聊可见，群聊的工具表里不会出现它们**（不占群聊的 token 预算）：

| 工具 | 参数 | 作用 |
|---|---|---|
| `owner_directive` | `content`（必填）、`chat`（群号 / 代号，留空=当前聊天流） | 下达叮嘱 |
| `owner_chat_alias` | `action`（set / remove / list）、`alias`、`group` | 维护群代号 |

> 实现注意：SDK 的 `collect_components` 只对 ReplyExtension 把 `chat_scope` 提到组件声明的**顶层**，
> 而 Runner 读的正是顶层（`runner_main.py:1420`）。所以本插件覆写了 `get_components()` 做这次提升——
> 只写进 `@Tool(..., chat_scope="private")` 的 metadata 是**不会生效**的（会被当成 `all`）。
> 已用宿主真实的 `ComponentRegistry` 验证：群聊工具表 `[]`，私聊工具表含这两个工具。

- 只有主人（`owner.owners`）能用；非主人调用返回中性的「没有权限执行该操作。」
- `owner_directive` 的参数说明里写死了边界：**只有主人明确要求时才调用**、**`content` 只写意图、不要搬运私聊原文**；
- 用 `chat_scope=private` + 权限校验双保险，避免"她自作主张影响别的群"。

**群代号**：主人说「以后把 943355126 叫做技术群」，她调用 `owner_chat_alias` 记下来（存在插件数据目录的 `aliases.json`）。
之后下达可以直接用「技术群」指代。代号不认识时，工具会**把当前可用代号表回给她**，她能自己纠正重调，不用多问一轮。

**让她"知道"代号**：hook 在**主人的私聊**里补一条对照表（仅当已存在代号、且开关打开）：

```
【主人私聊·跨聊天流影响】主人要让你影响别的聊天流时，用 owner_directive 工具，chat 填群号或下面这些代号（留空表示当前聊天流）。
群代号：技术群=943355126（测试群）
```

这里的"你"在运行时会被替换成**宿主配置里的 bot 昵称**（`config.get` 读 `bot.nickname`，`capabilities` 里已声明）：
``` 
【主人私聊·跨聊天流影响】主人要让<bot 昵称>影响别的聊天流时，…
```
读不到昵称就退回中性的「你」，插件里不写死任何具体的名字。

"主人的私聊"是加载时用 `chat.get_private_streams` 按 `user_id == 主人` 解析出来并缓存的；之后每次工具/命令被调用
都会用当次 `stream_id` 自我补全，所以 **hook 每轮零 RPC**；**没有代号时零改动**，且绝不下发到群聊。

## 机制

```
主人: /叮嘱 943355126 别跟 XX 继续吵，语气放软
  或: 私聊说「跟技术群的人说别再吵了」→ 她调用 owner_directive
        │
        ├─ 解析目标 → 真实 session_id（走 chat.get_group_streams；解析不到或多命中就报错，不猜）
        ├─ 写入 <data_dir>/reminders.json（TTL 10 分钟）
        ├─ dispatch.mode = "ask" → 朝目标流投一次提醒（说不说由她的 planner 决定）
        │     └─ 投递成功 → 记下 dispatched_at（只有投递过的条目才谈得上"兑现"）
        └─ 回执 / 工具结果

目标群下一轮 planner
        └─ hook maisaka.planner.before_request 在 items 末尾追加一条 SystemMessageItem
            强档（前 1/3）：主人刚特意交代过……
            弱档（后 2/3）：主人此前提过……如果相关就保持这个倾向
            到期：不注入，并从状态里清除

目标群即将发出她的可见回复
        └─ hook maisaka.reply.before_post_process
             └─ 消费掉该流里所有 dispatched_at > 0 的叮嘱（记一条 info 日志）
```

### 为什么消费点落在"发回复"而不是"投递"

`ask` 只是 `enqueue_proactive_task` 入队唤醒，叮嘱的注入发生在 **planner 请求之前**。
如果投递完就把叮嘱删掉，那一轮 planner 根本看不到它——等于白投。
所以消费点必须落在「可见回复即将发出」这一刻，也就是 `maisaka.reply.before_post_process`
（这个 hook 带 `session_id`，正好能定位是哪个流兑现了叮嘱）。

`silent` 模式没有投递 → `dispatched_at` 保持 0 → **永远不会被消费**，照旧由 TTL 决定寿命；
投递失败的条目同理。想完全回到"纯 TTL"行为，把 `reminder.consume_on_reply` 关掉即可。

已知取舍：如果她是被群里**别人**的消息勾出一句话，也会把这条叮嘱消费掉。
要判断"这轮回复是否来自主动唤醒"，得让宿主在 hook 里补来源标记（改主程序），
代价大、收益小：她既然已经在那个流开口了，这条叮嘱的作用窗口本来就该结束。

### 为什么"只在末尾追加"

宿主只在 `items` 发生变化时才替换上下文（`chat_loop_service.py:1250`）：

- **没有可注入内容** → 原样返回，**prompt 一个 token 都不变**；
- **有内容** → 只在末尾追加，已有条目的前缀 token 序列不变，planner 的前缀缓存照常命中，
  代价只有新增的那几十 token。

### 为什么"说不说由她决定"

`maisaka.proactive.trigger` 最终调用 `runtime.enqueue_proactive_task()`，它把 intent 拼成一条
`<plugin_proactive_task>` 文本追加进目标流历史、布下强制轮次并入队，而宿主拼进去的固定尾句是：
**「请结合当前聊天关系、记忆和上下文，自行决定是否回复以及如何表达。」**

所以 `ask` 模式的语义就是"提醒她想想"：**唤醒动作是确定的（一次），说不说、说什么由她的 planner 决定**（她也可以选择 `wait` 或什么都不做）。

## 配置（`config.toml`）

```toml
[plugin]
enabled = true
config_version = "0.3.0"

[owner]
owners = ["qq:123456789"]      # platform:user_id，换成你自己的账号；留空表示谁都不能下达

[reminder]
default_ttl_seconds = 600      # 10 分钟（60~3600）
strong_ratio = 0.33            # 强档占 TTL 的比例
max_per_target = 3             # 同一聊天流最多同时几条
max_content_chars = 200        # 单条内容上限
consume_on_reply = true        # 投递过的叮嘱在她开口后立即消费；关掉回到纯 TTL

[target]
platform = "qq"
account_id = ""                # 配置后只承认该账号下的聊天流

[inject]
enabled = true                 # 关掉即完全不注入

[dispatch]
mode = "ask"                   # ask / silent
intent_template = "……{内容}……"

[tool]
enabled = true                 # 关掉后两个工具仍会在工具表里，但一律拒绝执行

[alias]
inject_into_owner_chat = true  # 是否在主人私聊里注入代号对照表
```

## 已知副作用与边界

- **`ask` 会在目标流留痕**：投出去的任务文本会作为一条 `plugin_proactive` 消息进入该聊天流历史
  （WebUI 聊天记录可见）。所以别把不该出现在那个群的内容写进叮嘱。
- **消费是被"任何一次开口"触发的**：只要她在目标流发出可见回复，投递过的叮嘱就被消费，
  哪怕这句话是被别人的消息勾出来的。想保留更长时间就用 `consume_on_reply = false`。
- **回执会入库但不进她的上下文**：回执按宿主默认走 `storage_message=True`、
  `sync_to_maisaka_history=False`，所以它在聊天记录里可见，但不会进她的 planner 上下文。
- **`priority` 只是文本**：宿主把它原样写进任务说明，没有队列排序逻辑。
- **投递是入队，不是立即执行**：目标是"提醒她想想"，不承诺秒级动作。想完全不打扰就设 `silent`。
- **模型可能"嘴漏"**：注入文案已强制"内部参考 + 不得提及来源 + 不得复述原话"，
  但模型仍可能以自己的口吻透露倾向，无法 100% 保证 —— 所以别写敏感内容。
- **目标解析要求明确**：同一群号命中多条聊天流（例如退役账号留下的重复聊天流）时，
  要么在 `target.account_id` 指定账号，要么直接报错，不会随便挑一条；
  而一旦配了 `target.account_id`，就**一律**要求账号一致——哪怕只命中一条，
  避免悄悄打到退役账号的旧聊天流上。
- **工具在别人的私聊里也可见**：`chat_scope=private` 只挡住群聊。别人私聊时能看到这两个工具的描述
  （也就知道"这 bot 的主人能影响别的聊天流"），但处理器里非主人一律拒绝、不暴露机制。
  想更严可以后续用 `allowed_session` 再收窄到主人私聊。
- **`tool.enabled = false` 不省 token**：组件声明是静态的，工具仍在工具表里，只是拒绝执行；
  想连工具表一起省掉，只能关掉整个插件（那样命令也没了）或移除插件目录。
- **命令名是组件声明**：`/叮嘱`（别名 `/influence`）写在代码的 `@Command` 里，改名需要改插件代码。

## 测试

单元测试用标准库 `unittest`，不需要额外依赖（跑在 MaiBot 实例环境的 python 上即可）：

```bash
python -m unittest discover -s tests -v
```

141 项测试，覆盖：无内容时"零改动"、注入项结构与 schema 回传、TTL 分档边界、
目标解析失败与多命中消歧、账号不匹配报错、`ask` / `silent` 分支、非主人静默忽略、
列表/撤销、同目标超上限替换、状态文件损坏与缺字段时报错、`config.toml` 与配置模型一致性、
两个工具的权限与分支、代号的增删查与合法性校验、代号在命令与工具两侧的解析、
主人私聊对照表的注入条件（有代号 / 无代号 / 群聊 / 别人私聊 / 开关关闭 / 自我补全）、
投递记账与"兑现即消费"（只消费目标流 + 只消费投递过的 / silent 与投递失败不被消费 / 开关关闭）。

## 验证状态

**单元测试**：`python -m unittest discover -s tests` → 141 项全绿（stdlib `unittest`，无额外依赖）。

**真机验证（MaiBot 1.3.9 / 1.3.12）**

| 项 | 结果 | 证据 |
|---|---|---|
| manifest 合法性 | ✅ | 宿主真实 `ManifestValidator` 校验通过 |
| 注入项结构 | ✅ | 宿主真实 `deserialize_prompt_items()` 反序列化通过，`validate_context_items()` 无告警 |
| 尾部 system item 是否被 provider 接受 | ✅ | 日志 **16 次** `主人叮嘱已注入`，**0 次** `Hook maisaka.planner.before_request 返回的 items 无效` |
| 前缀缓存是否受影响 | ✅ 未见退化 | 注入窗口内命中率中位数 **96.0%**，窗口前 **94.7%**；两段各有一个 0.00% 冷启动样本拉低了均值 |
| 命令 → 落盘链路 | ✅ | 生产 `data/plugins/maimai.owner-reminder/reminders.json` 里留下真实叮嘱，目标群名、真实 session_id、下达者都正确 |
| 主人的私聊识别 | ✅ | 加载日志里解析出 `主人的私聊=[<session_id>]` |
| 代码热加载 | ✅ | 日志 `插件 maimai.owner-reminder v0.3.0 加载成功`（工具与代号表随之生效） |
| 工具会话作用域 | ✅ | 用宿主真实 `ComponentRegistry` 注册本插件声明后查询：群聊工具表 `[]`、私聊工具表含两个工具 |
| 主观效果 | ✅ | 主人确认"效果很好" |
| 自然语言通道 | ⏳ 待你实测 | 单元测试已覆盖，但真机上还没实际说过一句自然语言下达 |

**仍需留意**

- 缓存统计样本量偏小（注入窗口内 7 次 planner 调用），结论是"没看到退化"，不是严格的性能回归测试。
- 目标解析目前是唯一命中。若以后又出现重复聊天流，插件会**直接报错并提示配 `target.account_id`**，
  不会自己挑一条；一旦配了账号，则一律要求账号一致。
- 复现"注入项结构"那项校验的方式：把 `MAIBOT_INSTANCE_DIR` 指向实例目录后
  `from src.plugin_runtime.hook_payloads import deserialize_prompt_items`，
  用 `plugin._build_system_item(...)` 的返回值调一次即可
  （注意：这条会连带启动 MaiBot 的配置与数据库初始化，所以没有放进单元测试）。
