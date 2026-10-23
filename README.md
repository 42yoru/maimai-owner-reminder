# 主人的叮嘱（Owner Reminder）

<p>
  <a href="https://github.com/42yoru/maimai-owner-reminder/actions/workflows/ci.yml"><img src="https://github.com/42yoru/maimai-owner-reminder/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <a href="https://github.com/Mai-with-u/MaiBot"><img src="https://img.shields.io/badge/MaiBot-1.3.9%2B-orange.svg" alt="MaiBot"></a>
  <a href="https://github.com/Mai-with-u/maibot-plugin-sdk"><img src="https://img.shields.io/badge/SDK-2.14.0%2B-green.svg" alt="SDK"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-blue.svg" alt="Python">
</p>

让你**在自己所在的聊天里交代一句，她到另一个群里去收敛**。

> **先回答三个常见问题**
>
> - **它解决什么？** 她在某个群里越聊越冲、跟人吵起来了，你不用进那个群，私聊一句话就能让她接下来在那个群放软。
> - **会替我改群里的发言吗？** 不会。它只影响**那个群下一轮 planner 的态度**，说不说、说什么仍然是她自己决定。
> - **要改主程序吗？** 不用。纯插件，用的是宿主已有的 hook 和 proactive 接口。

## 先看效果

你在私聊里说一句：

> 跟技术群的人说别再吵了，语气放软

她的回执：

```
✅ 已记下叮嘱 #e86d（作用于《技术群》「测试群」）
   生效 10 分钟，之后自动淡出。查看：/叮嘱列表
   已提醒她自己去判断要不要现在处理。
```

之后在**技术群**里，她下一轮 planner 的上下文末尾会多一条内部参考（群里的人看不到）：

```
【内部参考·主人叮嘱】主人刚特意交代过：别继续和某人争执，语气放软。
请在当前聊天里自然体现出这个态度，不要提到这条叮嘱的存在，也不要复述主人的原话。
```

一旦她真的在那个群开口说话，这条叮嘱就**当场消费掉**，不用等十分钟。

## 三个必须知道的点

### 1. 只有白名单里的人算"主人"

`[owner] owners` 里填 `platform:user_id`（例如 `qq:123456789`）。名单外的人下达命令或调用工具，一律**静默忽略**——不提示、不回执，也不会留下记录。

### 2. 叮嘱作用于「另一个」聊天流

这是插件的核心：**下达的地方**和**生效的地方**可以不同。
你在私聊里交代，它在群里生效；也可以在 A 群交代、去 B 群生效。当然也可以就地生效（`/叮嘱 @here ...`）。

### 3. 有两种力度：强 → 弱

一条叮嘱默认活 10 分钟（`default_ttl_seconds = 600`）：

| 阶段 | 时长 | 措辞 |
|---|---|---|
| 强档 | 前 33%（`strong_ratio`） | 「主人刚特意交代过……」明确、像刚发生 |
| 弱档 | 后 67% | 「主人此前提过……如果相关就保持这个倾向」只留倾向 |
| 兑现 | 任意时刻 | 她在目标流开口 → 立刻消费掉，不再注入 |

`[dispatch] mode = "silent"` 时不投递提醒、也不计"兑现"，只有 TTL 兜底。

## 五分钟上手

### 第 1 步：装插件

```bash
cd MaiBot/plugins
git clone https://github.com/42yoru/maimai-owner-reminder.git
```

`config.toml` 跟着仓库一起来（通用版，注释齐全）。删掉也没关系——宿主会按配置模型自动生成一份。

### 第 2 步：填主人白名单

```toml
[owner]
owners = ["qq:你的QQ号"]
```

不填的话插件能正常加载，但**没有人能下达叮嘱**，启动日志里会有一条 warning 提醒你。

### 第 3 步：下达第一条

在任意聊天流里：

```
/叮嘱 123456789 别跟那个人继续争了，语气放软
```

第一个参数是**目标群号**。也可以：

```
/叮嘱 @here 别吵了          # 就在当前聊天流生效
/叮嘱列表                    # 看生效中的、剩余时间和当前档位
/叮嘱撤销 e86d               # 撤销一条；/叮嘱撤销 all 全撤
```

### 第 4 步：也可以直接说话（不用命令）

在**私聊**里说「跟技术群的人说别再吵了」，她自己的 planner 会识别意图并调用 `owner_directive` 工具。
群代号可以让她记住群号：「以后把 123456789 叫做技术群」，之后下达直接用代号。

### 第 5 步（可选）：去 WebUI 调

配置页分「常用设置 / 叮嘱行为 / 高级」三页，每个字段都有中文标签和说明，`dispatch.mode` 是下拉框。

## 配置

| 节 | 关键项 | 说明 |
|---|---|---|
| `[owner]` | `owners` | 谁能下达叮嘱，`platform:user_id`；留空表示谁都不能 |
| `[reminder]` | `default_ttl_seconds` / `strong_ratio` / `consume_on_reply` | 生效多久、强档占比、是否"开口即消费" |
| `[reminder]` | `max_per_target` / `max_content_chars` | 同一聊天流最多几条、单条字数上限 |
| `[dispatch]` | `mode` / `intent_template` | `ask` 投一次提醒 / `silent` 完全不打扰；模板必须含 `{内容}` |
| `[inject]` | `enabled` | 关掉后叮嘱完全不进她的上下文，插件只剩记录作用 |
| `[tool]` | `enabled` | 关掉后两个工具仍在工具表里，但一律拒绝执行（命令不受影响） |
| `[alias]` | `inject_into_owner_chat` | 是否在主人私聊里补一张群代号对照表 |
| `[target]` | `platform` / `account_id` | 同一群号命中多条聊天流时用 `account_id` 消歧 |

「她」的称呼不写死：注入主人私聊的对照表会读宿主的 `bot.nickname`，读不到就退回中性的「你」。

## 命令

| 命令 | 作用 |
|---|---|
| `/叮嘱 <群号\|代号> <内容>` | 对指定聊天流下达叮嘱 |
| `/叮嘱 @here <内容>` | 对当前聊天流下达 |
| `/叮嘱 <内容>` | 同上（首个 token 既不是群号也不是已登记代号时，整段都当内容） |
| `/叮嘱列表` | 查看生效中的叮嘱、剩余时间与当前档位 |
| `/叮嘱撤销 <编号\|all>` | 撤销一条或全部 |

`/influence` 是 `/叮嘱` 的别名；`列表` / `撤销` 也支持 `list` / `revoke`。
命令名是组件声明，改名需要改插件代码。

## 工具（自然语言）

两个工具都声明了 `chat_scope="private"`：**只在私聊可见，群聊工具表里不会出现它们**，不占群聊的 token 预算。

| 工具 | 参数 | 作用 |
|---|---|---|
| `owner_directive` | `content`（必填）、`chat`（群号 / 代号，留空=当前聊天流） | 下达叮嘱 |
| `owner_chat_alias` | `action`（set / remove / list）、`alias`、`group` | 维护群代号 |

- 只有 `owner.owners` 里的人能用，其他人得到中性的「没有权限执行该操作。」
- 参数说明里写死了边界：**只有主人明确要求时才调用**、`content` 只写意图、**不要搬运私聊原文**
- 代号不认识时，工具会把**当前可用代号表**回给她，她能自己纠正重调，不用多问一轮

## 它到底怎么工作

```
主人: /叮嘱 123456789 别跟 XX 继续吵，语气放软
  或: 私聊说「跟技术群的人说别再吵了」→ 她调用 owner_directive
        │
        ├─ 解析目标 → 真实 session_id（走 chat.get_group_streams；解析不到或多命中就报错，不猜）
        ├─ 写入 <data_dir>/reminders.json（TTL 10 分钟）
        ├─ dispatch.mode = "ask" → 朝目标流投一次提醒（说不说由她的 planner 决定）
        │     └─ 投递成功 → 记下 dispatched_at（只有投递过的条目才谈得上「兑现」）
        └─ 回执 / 工具结果

目标群下一轮 planner
        └─ hook maisaka.planner.before_request 在 items 末尾追加一条 SystemMessageItem
             强档 / 弱档 / 到期清除

目标群即将发出她的可见回复
        └─ hook maisaka.reply.before_post_process
             └─ 消费掉该流里所有 dispatched_at > 0 的叮嘱
```

**为什么消费点落在「发回复」而不是「投递」？**
`ask` 只是入队唤醒，叮嘱的注入发生在 planner 请求**之前**；投递完就删，那一轮 planner 根本看不到它，等于白投。

**为什么「只在末尾追加」？**
宿主只在 `items` 变化时才替换上下文。没有可注入内容时原样返回（一个 token 都不变）；有内容时只在末尾追加，已有条目的前缀序列不变，planner 的前缀缓存照常命中。

**为什么「说不说由她决定」？**
`maisaka.proactive.trigger` 最终是把 intent 拼成一条 `plugin_proactive_task` 追加进目标流历史并入队，宿主拼的固定尾句是「请结合当前聊天关系、记忆和上下文，自行决定是否回复以及如何表达」。所以唤醒是确定的（一次），说话不是。

## 常见问题

**下达了但她没反应**
先 `/叮嘱列表` 看它还在不在。她可能正在忙别的流，或者判断当时不适合插话——`ask` 的语义是「提醒她想想」，不是「命令她说话」。想更强势地推进，就把 `dispatch.mode` 保持 `ask` 并在群里主动 @ 她一次。

**`owner.owners` 填了还是没权限**
格式必须是 `platform:user_id`，例如 `qq:123456789`；`qq` 和号码之间是英文冒号。填完保存即可热加载，不用重启。

**群号解析报错 / 命中了多条聊天流**
同一个群号在数据库里有多条聊天流（常出现在换过账号之后）。两种解法：在 `[target] account_id` 指定账号，或者干脆用群代号指向确认过的那一条。

**叮嘱在群里没被"消费"，一直留到十分钟**
只有 `ask` 模式投递成功的条目才会被消费；`silent` 模式、投递失败的条目都只有 TTL。另外她必须**在目标流发出可见回复**才算兑现。

**她在群里把叮嘱内容说出去了**
注入文案已经强制「不得提及来源、不得复述原话」，但模型无法 100% 保证。所以**别往叮嘱里写敏感内容**——那是个正在聊天的群。

**别人的私聊里也能看到这两个工具**
`chat_scope=private` 只挡群聊。别人私聊时能看到工具描述，但处理器里非主人一律拒绝、不暴露机制。想更严可以配合 `allowed_session` 收窄到主人私聊。

## 已知边界

- **`ask` 会在目标流留痕**：投出去的任务文本会作为一条 `plugin_proactive` 消息进入该聊天流历史（WebUI 可见）。别把不该出现在那个群的内容写进叮嘱。
- **消费被「任何一次开口」触发**：哪怕那句话是被别人勾出来的，也会消费掉。想保留更久就把 `consume_on_reply` 关掉。
- **回执会入库但不进她的上下文**：走宿主默认的 `storage_message=True`、`sync_to_maisaka_history=False`。
- **投递是入队，不是立即执行**：目标是「提醒她想想」，不承诺秒级动作；完全不打扰就设 `silent`。
- **`tool.enabled = false` 不省 token**：组件声明是静态的，工具仍在表里，只是拒绝执行；想连工具表一起省掉只能停用整个插件。
- **目标解析要求明确**：多命中时直接报错，不会自己挑一条；配了 `target.account_id` 就一律要求账号一致。

## 文件结构

```
maimai_owner_reminder/
├── plugin.py           # 插件入口：配置模型、命令、两个工具、注入与消费 hook
├── reminder_store.py   # 叮嘱状态：TTL 分档、投递记账、兑现即消费、原子写
├── reminder_text.py    # 所有面向用户的文案（注入、回执、列表、代号）
├── reminder_args.py    # 命令参数解析（下达 / 列表 / 撤销）
├── alias_store.py      # 群代号表
├── target_resolver.py  # 群号 → 真实 session_id，解析不到或多命中就报错
├── config_ui.py        # WebUI 配置页的中文展示布局（只重组 schema）
├── tests/             # 单测，不需要额外依赖
├── config.toml         # 通用版配置（owners 故意留空）
├── icon.png            # 插件图标
└── _manifest.json
```

## 测试

```bash
cd plugins/maimai_owner_reminder
python -m unittest discover -s tests -t .
```

147 项测试，覆盖：无内容时零改动、注入项结构与 schema 回传、TTL 分档边界、目标解析失败与多命中消歧、
账号不匹配报错、`ask` / `silent` 分支、非主人静默忽略、列表与撤销、同目标超上限替换、状态文件损坏与缺字段、
`config.toml` 与配置模型一致、两个工具的权限与分支、代号的增删查与解析、主人私聊对照表的注入条件、
投递记账与「兑现即消费」、以及 WebUI 配置页的中文标签与「不许丢字段」。

## 贡献

欢迎提 Issue 和 PR。提交前请确保：

```bash
ruff check . --select E,F,W,B --line-length 130
python -m unittest discover -s tests -t .
```

都通过。

## 许可证

代码用 [MIT](LICENSE)。插件图标（`icon.png`）是几何绘制，随本插件一同以 MIT 发布。
