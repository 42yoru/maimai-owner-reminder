"""WebUI 配置页的中文展示布局。

只重组 SDK 生成的 Schema（分块、标签、提示、页签），**不改配置模型、
不改默认值、也不改 TOML 里的真实路径**：每个展示块的 ``name`` 始终指向原有的配置节。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class _Section:
    """一个虚拟展示块。"""

    source: str
    title: str
    description: str
    fields: Dict[str, str]
    collapsed: bool = False


_SECTIONS: Dict[str, _Section] = {
    "owner": _Section(
        source="owner",
        title="谁能下达叮嘱",
        description="只有名单里的账号能下达，其他人在任何聊天流里下达都会被静默忽略。",
        fields={"owners": "主人账号（platform:user_id）"},
    ),
    "dispatch": _Section(
        source="dispatch",
        title="下达时怎么提醒她",
        description="叮嘱本身会立刻生效；这里决定要不要顺手朝目标流投一次提醒。",
        fields={"mode": "提醒投递方式"},
    ),
    "inject": _Section(
        source="inject",
        title="注入开关",
        description="关掉后叮嘱完全不进她的上下文，插件只剩「记一笔」的作用。",
        fields={"enabled": "注入到目标流上下文"},
    ),
    "reminder": _Section(
        source="reminder",
        title="叮嘱的时效",
        description="一条叮嘱在目标流里存在多久、以几档强度出现、什么时候被消费掉。",
        fields={
            "default_ttl_seconds": "默认生效时长（秒）",
            "strong_ratio": "强档占比",
            "consume_on_reply": "发言后立即消费",
            "max_per_target": "同一聊天流最多几条",
            "max_content_chars": "单条内容上限（字符）",
        },
    ),
    "dispatch_adv": _Section(
        source="dispatch",
        title="投递文案",
        description="投给目标流的那句话长什么样。{内容} 会被替换成叮嘱本身。",
        fields={"intent_template": "投递文案模板"},
        collapsed=True,
    ),
    "target": _Section(
        source="target",
        title="目标解析",
        description="同一群号命中多条聊天流时才需要在这里指定账号。",
        fields={"platform": "默认平台", "account_id": "限定账号 ID"},
        collapsed=True,
    ),
    "tool": _Section(
        source="tool",
        title="自然语言工具",
        description="关掉后工具仍会出现在私聊的工具表里，但一律拒绝执行。",
        fields={"enabled": "允许工具执行"},
        collapsed=True,
    ),
    "alias": _Section(
        source="alias",
        title="群代号",
        description="代号表存在插件数据目录；这里只管要不要在她私聊里补一张对照表。",
        fields={"inject_into_owner_chat": "在主人私聊注入代号表"},
        collapsed=True,
    ),
    "plugin_info": _Section(
        source="plugin",
        title="插件信息",
        description="用于版本识别，保持现状即可。",
        fields={"enabled": "启用插件", "config_version": "配置版本"},
        collapsed=True,
    ),
}

_TABS = {
    "basic": ("常用设置", ("owner", "dispatch", "inject")),
    "behavior": ("叮嘱行为", ("reminder",)),
    "advanced": ("高级", ("dispatch_adv", "target", "tool", "alias", "plugin_info")),
}

_HINTS: Dict[str, str] = {
    "owner.owners": "格式 platform:user_id，例如 qq:123456789；留空表示谁都下不了叮嘱（启动时会有 warning 提示）。",
    "dispatch.mode": "ask = 下达时朝目标流投一次「你自己判断要不要回应」的提醒；silent = 完全不打扰，等它自然触发时再注入。",
    "inject.enabled": "关掉后叮嘱不会出现在目标流 planner 的上下文里，只保留记录。",
    "reminder.default_ttl_seconds": "一条叮嘱最长活多久；没被「发言后立即消费」收掉的话，到期自动淡出。范围 60~3600 秒。",
    "reminder.strong_ratio": "前百分之多少的时间用强档措辞，之后降为弱档。0.33 表示前三分之一强、后三分之二弱。",
    "reminder.consume_on_reply": "开启后，投递过提醒的叮嘱只要她在目标流真的开口，就立刻消费掉，不用等 TTL。",
    "reminder.max_per_target": "同一个聊天流最多同时生效几条；超出时按最旧的替换。",
    "reminder.max_content_chars": "单条叮嘱的字符上限，超出截断并在回执里提示。",
    "dispatch.intent_template": "必须包含 {内容} 占位符。默认文案会要求不要暴露提醒来源、也不要复述原话。",
    "target.platform": "命令上下文取不到平台时用的默认值，qq 就够。",
    "target.account_id": "一个群号命中多条聊天流（比如退役账号留下的旧流）时用它消歧；一旦填写就一律要求账号一致。",
    "tool.enabled": "关掉后 owner_directive / owner_chat_alias 仍在工具表里，但一律拒绝执行，命令不受影响。",
    "alias.inject_into_owner_chat": "只在已经存在代号、且当前是主人私聊时才注入，没有代号时一个 token 都不多花。",
    "plugin_info.config_version": "由插件自己维护，不要手改。",
}

_DISABLED_FIELDS = {"plugin.config_version"}


def _build_field(source: str, name: str, template: Dict[str, Any], label: str, order: int) -> Dict[str, Any]:
    """在 SDK 给的字段定义上补中文标签、提示与顺序。"""

    path = f"{source}.{name}"
    entry = dict(template)
    entry.update(
        label=label,
        hint=_HINTS.get(path, template.get("description") or ""),
        description=_HINTS.get(path, template.get("description") or ""),
        order=order,
    )
    if path in _DISABLED_FIELDS:
        entry["disabled"] = True
    if path == "dispatch.mode":
        entry["choices"] = ["ask", "silent"]
        entry["widget"] = "select"
    if path == "dispatch.intent_template":
        entry["rows"] = 3
        entry["ui_type"] = "textarea"
    return entry


def build_config_ui_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """重组 SDK Schema：分出页签、中文标签、常用与高级分离。

    Args:
        schema: SDK ``build_config_schema()`` 生成的原始 Schema。

    Returns:
        Dict[str, Any]: 重组后的 Schema；字段默认值与类型保持原样。
    """

    source_sections: Dict[str, Any] = schema.get("sections") or {}
    sections: Dict[str, Any] = {}
    tabs: list[Dict[str, Any]] = []

    for tab_id, (tab_title, section_ids) in _TABS.items():
        tabs.append({"id": tab_id, "title": tab_title, "sections": list(section_ids)})
        for section_id in section_ids:
            spec = _SECTIONS[section_id]
            template_section = source_sections.get(spec.source)
            if template_section is None:
                continue
            template_fields = template_section.get("fields") or {}
            fields: Dict[str, Any] = {}
            for order, (field_name, label) in enumerate(spec.fields.items()):
                template = template_fields.get(field_name)
                if template is None:
                    continue
                fields[field_name] = _build_field(spec.source, field_name, template, label, order)
            if not fields:
                continue
            # 字典键只标识展示分块，name 才是 WebUI 读写配置的真实节名
            sections[section_id] = {
                **template_section,
                "name": spec.source,
                "title": spec.title,
                "description": spec.description,
                "collapsed": spec.collapsed,
                "order": len(sections),
                "fields": fields,
            }

    return {**schema, "sections": sections, "layout": {"type": "tabs", "tabs": tabs}}


__all__ = ["build_config_ui_schema"]
