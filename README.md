# 主人的叮嘱（Owner Reminder）

<p>
  <a href="https://github.com/42yoru/maimai-owner-reminder/actions/workflows/ci.yml"><img src="https://github.com/42yoru/maimai-owner-reminder/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <a href="https://github.com/Mai-with-u/MaiBot"><img src="https://img.shields.io/badge/MaiBot-1.3.9%2B-orange.svg" alt="MaiBot"></a>
  <a href="https://github.com/Mai-with-u/maibot-plugin-sdk"><img src="https://img.shields.io/badge/SDK-2.12.0%2B-green.svg" alt="SDK"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-blue.svg" alt="Python">
</p>

> **一句话定位：** 在当前聊天流下达一条短期叮嘱，让另一个群或私聊流的 planner 在接下来一段时间里参考它。

它不会直接改写目标流的发言：说不说、说什么仍由 planner 自己决定。插件使用宿主已有的 hook 与 proactive 接口，不改主程序。

## 重要提醒

### 身份与权限边界

只有 `[owner] owners` 中的 `platform:user_id` 才算主人，例如 `qq:123456789`。英文冒号必须正确；全角冒号会匹配失败。名单为空时没有人能下达叮嘱。

命令通道由宿主从真实消息注入身份。自然语言工具则有一个宿主边界：宿主把工具参数交给插件时，模型填写的非空上下文字段可能不会被覆盖，因此插件**不能把模型自报的 `stream_id` / `user_id` 当成绝对可信凭据**。当前插件会反查私聊流；群聊工具只能核对声明账号是否在该流最近 50 条消息中出现过，核对不到就拒绝。要彻底消除这类冒名空间，需要宿主无条件覆盖身份上下文；本插件不修改宿主。

两个自然语言工具声明 `chat_scope="private"`，只在私聊可见；其他私聊调用者仍会看到工具描述，但处理器会拒绝非主人调用。

### 叮嘱的作用与留痕

- 下达流和目标流可以不同，也可以用 `/叮嘱 @here` 作用于当前流。
- 默认 TTL 为 10 分钟：前 33% 为强档，后 67% 为弱档。
- `dispatch.mode = "ask"` 会向目标流投递一次提醒；这会把 intent 留在目标流历史并唤醒 planner。
- `dispatch.mode = "silent"` 不投递提醒，只有 TTL 兜底。
- `inject.enabled = false` 时不向目标流投递提醒，叮嘱仅按 TTL 保存。
- 投递成功后，叮嘱会在目标流出现可见回复时消费；`silent` 模式、投递失败或没有可见回复时只按 TTL 保留。
- 不要把敏感内容写进叮嘱：目标群的 planner 可能看到注入内容，`ask` 的投递文本也会进入目标流历史。

## 快速安装

```bash
cd MaiBot/plugins
git clone https://github.com/42yoru/maimai-owner-reminder.git
```

仓库不包含 `config.toml`，宿主首次启动时会按配置模型生成；也可以在 WebUI 配置页修改。

## 最小配置

```toml
[owner]
owners = ["qq:你的QQ号"]
```

不填写主人白名单时，插件仍可加载，但没有人能下达叮嘱，日志会提示。

## 常用用法

### 命令

```text
/叮嘱 123456789 别跟那个人继续争了，语气放软
/叮嘱 @here 别吵了
/叮嘱列表
/叮嘱撤销 e86d
/叮嘱撤销 all
```

第一个参数可以是群号或已登记的群代号；当首个 token 既不是群号也不是已登记代号时，整段会作为内容。`/influence` 是 `/叮嘱` 的别名；`列表` / `撤销` 也支持 `list` / `revoke`。

### 自然语言工具

在私聊里说“跟技术群的人说别再吵了”，planner 可调用 `owner_directive`。`owner_chat_alias` 可维护群代号：

| 工具 | 参数 | 作用 |
|---|---|---|
| `owner_directive` | `content`（必填）、`chat`（群号/代号，留空为当前流） | 下达叮嘱 |
| `owner_chat_alias` | `action`（set/remove/list）、`alias`、`group` | 维护群代号 |

工具参数说明要求只有主人明确要求时才调用；`content` 应写意图，不要搬运私聊原文。

## 工作流程

```text
命令或私聊工具
  → 校验主人身份并解析目标聊天流
  → 写入 <data_dir>/reminders.json
  → ask 模式：向目标流投递一次提醒；silent 模式：不投递
  → planner hook 在目标流上下文末尾追加强档/弱档叮嘱
  → 目标流发出可见回复后，消费已投递的叮嘱
  → TTL 到期后清理
```

目标解析走 `chat.get_group_streams` / `chat.get_private_streams`；无法解析或多条命中时不会猜选。状态文件使用原子写入；`reminders.json` 与 `aliases.json` 损坏、版本不符或缺字段时会明确报错并阻止加载。

## 配置表

| 节 | 关键项 | 说明 |
|---|---|---|
| `[owner]` | `owners` | 主人白名单，格式 `platform:user_id`；留空表示无人可下达 |
| `[reminder]` | `default_ttl_seconds` / `strong_ratio` / `consume_on_reply` | TTL、强档占比、是否在可见回复后消费 |
| `[reminder]` | `max_per_target` / `max_content_chars` | 同一目标最多同时生效的条数、单条字数上限 |
| `[dispatch]` | `mode` / `intent_template` | `ask` 投递一次提醒；`silent` 完全不投递；模板必须含 `{内容}` |
| `[inject]` | `enabled` | 关闭后不注入、不投递，叮嘱仅保留记录和 TTL |
| `[tool]` | `enabled` | 关闭后两个工具仍在工具表中，但一律拒绝执行；命令不受影响 |
| `[alias]` | `inject_into_owner_chat` | 是否在主人私聊注入群代号对照表 |
| `[target]` | `platform` / `account_id` | 多账号或同群多流时用于消歧 |

WebUI 分为“常用设置 / 叮嘱行为 / 高级”三页。插件不写死 bot 称呼；群代号对照表会读取宿主 `bot.nickname`，读取不到时使用中性称呼。

## 边界 / FAQ

**下达了但没有反应？** `/叮嘱列表` 查看是否仍生效。`ask` 只是提醒 planner 思考，不保证立即回复；目标流可能正在忙，或 planner 判断不适合插话。

**`owner.owners` 填了仍无权限？** 检查是否为 `platform:user_id` 和英文冒号；保存后可热加载。

**群号解析报错或多命中？** 在 `[target] account_id` 指定账号，或使用确认过的群代号。

**为什么没有被消费？** 只有 `ask` 投递成功、且目标流发出可见回复后才消费；`silent`、投递失败和没有可见回复的情况都按 TTL 保留。

**`tool.enabled = false` 会省 token 吗？** 不会。工具声明是静态的，仍会出现在工具表中，只是执行时拒绝；想完全移除工具表只能停用插件。

**`ask` 会不会把内容留在目标群？** 会。proactive intent 会进入目标聊天流历史；不要写不该出现在目标流的内容。

**工具身份校验安全吗？** 私聊工具会反查私聊流；群聊工具只能通过最近 50 条消息核对声明账号，不能证明某次工具调用确实来自该用户。真正可靠的身份覆盖需要宿主支持。

**状态文件坏了怎么办？** 日志会明确指出 JSON、版本或字段错误。叮嘱有时效性，确认无须保留后可删除对应状态文件。

## 测试

```bash
cd plugins/maimai_owner_reminder
python -m unittest discover -s tests -t .
```

测试覆盖注入结构、TTL、目标解析、`ask`/`silent`、权限、列表/撤销、状态损坏、工具分支、群代号、投递记账与兑现消费，以及 WebUI 配置 schema。

```bash
ruff check . --select E,F,W,B --line-length 130
```

## 文件结构

```text
maimai_owner_reminder/
├── plugin.py           # 配置、命令、工具、注入与消费 hook
├── reminder_store.py   # TTL、投递记账、消费与原子写
├── reminder_text.py    # 用户文案
├── reminder_args.py    # 命令解析
├── alias_store.py      # 群代号表
├── target_resolver.py  # 群号到真实 session_id 的解析
├── config_ui.py        # WebUI schema 布局
├── tests/              # 单测
├── icon.png
└── _manifest.json
```

## 贡献与许可证

欢迎提 Issue 和 PR。代码使用 [MIT](LICENSE)；插件图标由作者使用 AI 图像模型生成，随插件一同以 MIT 发布。
