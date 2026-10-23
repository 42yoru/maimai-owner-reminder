# 更新记录

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
