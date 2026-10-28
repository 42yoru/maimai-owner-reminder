# 更新记录

## 0.4.7

**修复**

- **「兑现即消费」会被竞态绕过**：`mark_dispatched` 原来落在 `await proactive.trigger` 之后。
  `trigger` 只是入队并唤醒目标流，那一轮的 planner→reply 完全可能先跑完，此时
  `dispatched_at` 还是 0，`consume_dispatched` 认为"没投递过"而跳过，于是叮嘱一直留到 TTL，
  期间每轮 planner 继续被注入「主人刚特意交代过」——已经照做了却还在施压。
  现在先记账再投递，投递失败 / 抛错时用新增的 `clear_dispatched` 回滚回纯 TTL 语义
- **状态先改内存再写盘**：`create` / `purge_expired` / `mark_dispatched` / `consume_dispatched` /
  `revoke` / `revoke_all` 以及 `AliasStore.set/remove` 全部改成「先原子写盘、成功后才切内存」
  （`_commit`）。之前磁盘满或权限错误时，回执说「❌ 叮嘱没能保存」，但那条叮嘱已经在内存里，
  下一轮目标流 planner 照样被注入，且后续任意一次成功写盘还会把它持久化

**细节**

- 测试 155 → 158：新增「写盘失败时内存与磁盘都不变」「目标流在 trigger 返回前回复也要算兑现」两条

## 0.4.6

**主要功能**

- **身份改为反查，不再采信模型传入的 `user_id`**：宿主把工具参数交给插件时只在"模型没填"时才注入真实上下文，所以模型塞一个 `user_id` 就能冒充主人下达跨流叮嘱、维护群代号。现在私聊流按 `stream_id` 反查 `chat.get_private_streams` 拿到真实 `user_id`；群聊流退化为核对声明的账号确实在该流最近 50 条消息里说过话，核对不到一律拒绝。

**细节**

- 新增能力 `message.get_recent`（只用于身份核验，读 ID 字段，不送模型、不落盘）
- 删掉「调用一次工具就自学主人私聊会话」的旧测试：那个行为恰好依赖"信任无法核实的身份"，换成三条新用例（群里冒充被拒 / 主人最近发过言则放行 / 反查失败抛错）
- `chat.get_private_streams` 返回形状异常时直接抛错，不再降级成"查不到主人"
- manifest：`plugin_type` 由 `tool` 改为 `automation`（对齐 WebUI 类型词表）、补上 `changelog` 字段、`sdk` 区间改为 `>=2.12.0`（已核对 2.12.0 与 2.19.0 的 API 面一致）
- README 增加"身份是反查出来的"一节与两条已知边界；SDK 徽章改 2.12.0+

## 0.4.5

**修复**

- **白名单收紧没生效**：`_refresh_owner_sessions` 以前只往集合里加，把某个主人从 `owner.owners`
  删掉后，他的私聊仍会继续被注入「群代号对照 + owner_directive 提示」。现在每次刷新都先清空重建
- **`consume_dispatched` 不先清过期**：已经淡出的叮嘱会被当成「兑现」写进日志；
  现在先 purge 再消费，日志语义对得上

**细节**

- 加载和热更新时做一次配置体检：`owner.owners` 里全角冒号这类永远匹配不上的条目会被点名警告，
  `dispatch.intent_template` 少了 `{内容}` 也会提示（否则投递出去的指令里没有叮嘱本身）
- 同一平台只查一次私聊流，不再为一个 owner 列表反复调同一个能力
- 时长文案：90 秒不再显示成「2 分钟」，改成「1 分 30 秒」
- README：统一「命令静默忽略 / 工具中性拒绝」的口径，删掉并不存在的 `allowed_session` 说法，
  补上时间回拨与状态文件损坏的处理办法
- CI 矩阵加 3.13，SDK 依赖约束到 `>=2.14.0`，去掉没用的 httpx
- manifest 的 `sdk.max_version` 从 `2.99.99` 收到 `2.16.99`（plugin-repo 的校验脚本不接受前两位为 99）
- 测试 148 → 152 项

## 0.4.4

**修复**

- 补上 0.4.3 漏改的一处：`handle_owner_reminder` 里最后一条 `return … , True`
  现在也统一成 `1`，全仓库不再有布尔权重（宿主会 `bool()` 它，行为不变）

## 0.4.3

**修复**

- **`config.toml` 不再进仓库**：运行时配置由宿主按 `config_model` 生成，提交进仓库会让用户一改配置
  就和仓库版本打架。已加入 `.gitignore` 并从索引移除；原来读这个文件的两个用例改成验证配置模型的
  默认值，另加一条「config.toml 不许提交」的断言
- 命令处理函数返回三元组的第三项由 `True` / `False` 改成 `1` / `0`，与文档约定的 int 权重一致
  （宿主会 `bool()` 它，行为不变）

**细节**

- manifest 补 `plugin_type: "tool"`：主体是一条斜杠命令加两个模型可调用工具，比默认的 `extension` 更准
- 版本与 config_version 升到 0.4.3；测试 147 → 148 项

## 0.4.2

**细节**

- 插件 ID 改为 `github.42yoru.maimai-owner-reminder`：与插件中心推荐的
  `github.<用户名>.<插件名>` 一致，也和同作者的模拟盘插件统一
- 数据目录随 ID 变化（`data/plugins/<id>/`），老的 `maimai.owner-reminder` 目录里的
  `aliases.json` / `reminders.json` 需要手动搬到新目录；插件本身不自动迁移
- 版本与 config_version 升到 0.4.2，行为无变化

## 0.4.1

**细节**

- 换用插画版图标：橙色信使少女递信封 + 两个对话气泡与循环箭头，512×512、412 KB，
  圆角透明，深浅主题下都不露白角
- 图标由作者用 AI 图像模型生成

## 0.4.0

**主要功能**

- 加了插件图标：`display.icon` 指向仓库里的 `icon.png`（512×512，几何绘制，4 KB），
  加载失败时回退到 lucide 的 `bell-ring`，底色 `#FFD69A`
- WebUI 配置页汉化重排：分成「常用设置 / 叮嘱行为 / 高级」三个页签，
  每个字段有中文标签和「填什么、为什么」的说明
  - `dispatch.mode` 改成下拉框，投递文案用多行输入框，配置版本只读
  - 新增 `config_ui.py` 负责展示布局，配置模型与 TOML 路径完全不动
- README 重写：先看效果 → 三个必须知道的点 → 五分钟上手，技术细节挪到后面

**细节**

- 补 `LICENSE`（MIT）与 CI（ruff + unittest，`python -m unittest discover -s tests -t .`）
- 补 `tests/__init__.py`，让 CI 的 `-t .` 发现方式能跑起来
- 新增「不许丢字段」测试：每个真实配置节里的字段都必须出现在重排后的 schema 里
- 配置版本同步到 0.4.0；测试 143 → 147 项

## 0.3.1

- `owner.owners` 为空时给出明确 warning，而不是只在 info 日志里体现
- README 说明仓库里的是通用版配置

## 0.3.0

- 新增 `reminder.consume_on_reply`：投递过提醒的叮嘱在她于目标流开口后立即消费
- 插件 ID 改为 `maimai.owner-reminder`，owner 白名单默认留空，示例与测试数据去个人化
- bot 昵称改为读取宿主配置（`config.get` → `bot.nickname`）

## 0.2.0

- 二期：自然语言工具 `owner_directive` / `owner_chat_alias`，以及群代号
- 工具声明 `chat_scope=private`，只在私聊可见

## 0.1.0

- 一期：`/叮嘱` 命令、TTL 分档注入、目标流解析、投递与回执
