# 主人的叮嘱（owner-reminder）

让**主人**可以在任意聊天流下达一条"叮嘱"，**作用于另一个指定的聊天流**：
这条叮嘱会在接下来的 10 分钟里以「强 → 弱」两档出现在目标流 planner 的上下文中，
然后自动淡出。纯插件实现，**不改主程序**。

定位是**短期态度影响**，不是实时制止 —— 因为麦麦的架构是「上下文驱动 + planner 轮次 + 频率门控」，
"她正在吵 → 我私聊一句 → 她立刻闭嘴"不现实；"我交代一句 → 她接下来在那个群收敛"才是这个插件做的事。

## 命令

| 命令 | 作用 |
|---|---|
| `/叮嘱 <群号> <内容>` | 对指定群下达叮嘱 |
| `/叮嘱 @here <内容>` | 对当前聊天流下达叮嘱 |
| `/叮嘱 <内容>` | 同上（首个 token 不是纯数字群号时，整段都当作内容） |
| `/叮嘱列表` | 查看生效中的叮嘱、剩余时间与当前档位 |
| `/叮嘱撤销 <编号\|all>` | 撤销一条或全部 |

`/influence` 是 `/叮嘱` 的别名；`列表/撤销` 也支持 `list` / `revoke` 写法。
只有配置里 `owner.owners` 白名单内的账号可以下达，其他人**静默忽略**（不提示、不回执）。

实现上这些是**一个命令组件** `owner_reminder`（正则把命令词之后的内容整段交给插件解析，
再分派到下达 / 查看 / 撤销），所以命令名写在代码里，改名需要改插件代码。

## 机制

```
主人: /叮嘱 943355126 别跟 XX 继续吵，语气放软
        │
        ├─ 解析目标 → 真实 session_id（走 chat.get_group_streams；解析不到或多命中就报错，不猜）
        ├─ 写入 plugins/<data_dir>/reminders.json（TTL 10 分钟）
        ├─ dispatch.mode = "ask" → 朝目标流投一次提醒（说不说由她的 planner 决定）
        └─ 给主人回执

目标群下一轮 planner
        └─ hook maisaka.planner.before_request 在 items 末尾追加一条 SystemMessageItem
            强档（前 1/3）：主人刚特意交代过……
            弱档（后 2/3）：主人此前提过……如果相关就保持这个倾向
            到期：不注入，并从状态里清除
```

### 为什么"只在末尾追加"

宿主只在 `items` 发生变化时才替换上下文（`chat_loop_service.py:1250`）：

- **没有生效叮嘱** → 原样返回，**prompt 一个 token 都不变**；
- **有叮嘱** → 只在末尾追加，已有条目的前缀 token 序列不变，planner 的前缀缓存照常命中，
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
config_version = "0.1.0"

[owner]
owners = ["qq:1215349250"]     # platform:user_id

[reminder]
default_ttl_seconds = 600      # 10 分钟（60~3600）
strong_ratio = 0.33            # 强档占 TTL 的比例
max_per_target = 3             # 同一聊天流最多同时几条
max_content_chars = 200        # 单条内容上限

[target]
platform = "qq"
account_id = ""                # 同一群号命中多条聊天流时用于消歧

[inject]
enabled = true                 # 关掉即完全不注入

[dispatch]
mode = "ask"                   # ask / silent
intent_template = "……{内容}……"
```

## 已知副作用与边界

- **`ask` 会在目标流留痕**：投出去的任务文本会作为一条 `plugin_proactive` 消息进入该聊天流历史
  （WebUI 聊天记录可见）。所以别把不该出现在那个群的内容写进叮嘱。
- **回执会入库但不进她的上下文**：`/叮嘱` 的回执按宿主默认走 `storage_message=True`、
  `sync_to_maisaka_history=False`，所以它在聊天记录里可见，但不会进她的 planner 上下文。
- **`priority` 只是文本**：宿主把它原样写进任务说明，没有队列排序逻辑。
- **投递是入队，不是立即执行**：目标是"提醒她想想"，不承诺秒级动作。想完全不打扰就设 `silent`。
- **模型可能"嘴漏"**：注入文案已强制"内部参考 + 不得提及来源 + 不得复述原话"，
  但模型仍可能以自己的口吻透露倾向，无法 100% 保证 —— 所以别写敏感内容。
- **目标解析要求明确**：同一群号命中多条聊天流（例如退役账号留下的重复聊天流）时，
  要么在 `target.account_id` 指定账号，要么直接报错，不会随便挑一条；
  而一旦配了 `target.account_id`，就**一律**要求账号一致——哪怕只命中一条，
  避免悄悄打到退役账号的旧聊天流上。
- **命令名是组件声明**：`/叮嘱`（别名 `/influence`）写在代码的 `@Command` 里，改名需要改插件代码。

## 测试

单元测试用标准库 `unittest`，不需要额外依赖（跑在 MaiBot 实例环境的 python 上即可）：

```bash
python -m unittest discover -s tests -v
```

61 项测试，覆盖：无叮嘱时"零改动"、注入项结构与 schema 回传、TTL 分档边界、
目标解析失败与多命中消歧、账号不匹配报错、`ask` / `silent` 分支、非主人静默忽略、
列表/撤销、同目标超上限替换、状态文件损坏与缺字段时报错、`config.toml` 与配置模型一致性。

## 已验证 / 尚未验证

**已验证（在本机 1.3.9 上）**

- `_manifest.json` 通过宿主真实的 `ManifestValidator`（id、能力声明、版本区间都合法）；
- 注入的尾部 `SystemMessageItem` 交给宿主真实的 `deserialize_prompt_items()` 能正常反序列化并通过
  `validate_context_items()` —— 该校验对 system item 的位置**没有**约束，只查 item_id 与工具调用配对。
  复现方式：把 `MAIBOT_INSTANCE_DIR` 指向实例目录后 `from src.plugin_runtime.hook_payloads import
  deserialize_prompt_items`，用 `plugin._build_system_item(...)` 的返回值调一次即可
  （注意：这条会连带启动 MaiBot 的配置与数据库初始化，所以没有放进单元测试）。

**尚未验证（需要真机跑一轮）**

- 两个 provider（`deepseek-flash` / `rinko-deepseek-v4.1-flash`）是否都接受**尾部追加的 system item**。
  若日志出现 `Hook maisaka.planner.before_request 返回的 items 无效`，就把注入位置退化为
  "插到最后一条 user 消息之前"（前缀缓存收益略降）。
- planner 前缀缓存命中率是否维持原有水平。
- 插件本身尚未在这台实例上加载过（`plugins/yeye_owner_reminder/` 会在下次 MaiBot 重启时被发现）。
