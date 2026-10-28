"""WebUI 配置页的中文展示布局。

只重组 SDK 生成的 Schema（分块、中文标签、提示、页签），不改配置模型、
不改默认值、也不改 TOML 里的真实路径：每个展示块的 ``name`` 始终指向原有配置节。
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
    "target": _Section(
        source="target",
        title="触发目标",
        description="谁被 @ 时触发，以及目标账号本人在场时要不要让路。",
        fields={
            "user": "目标账号",
            "presence_policy": "目标账号在场时",
            "presence_window_seconds": "最近发言判定窗口（秒）",
        },
    ),
    "scope": _Section(
        source="scope",
        title="生效范围",
        description="插件只在哪些平台、哪些群里工作；群号留空则完全不生效。",
        fields={
            "platforms": "生效平台",
            "groups": "生效群号",
        },
    ),
    "decide": _Section(
        source="decide",
        title="接话预判",
        description="接不接交给便宜模型判断；它只判断，不写回复正文。",
        fields={
            "mode": "预判模式",
            "task_name": "预判模型任务",
            "model_name": "预判模型名（可选）",
            "temperature": "预判温度",
            "max_tokens": "预判输出上限",
            "confidence_threshold": "最低置信度",
            "rpc_timeout_ms": "预判超时（毫秒）",
        },
    ),
    "prompt": _Section(
        source="prompt",
        title="提示词",
        description="预判用的提示词模板；改坏了不会静默回退，加载和热更新时都会校验并报错。",
        fields={
            "template": "预判提示词模板",
        },
        collapsed=True,
    ),
    "reply": _Section(
        source="reply",
        title="触发节奏",
        description="冷却、额度与延迟，用来避免刷屏和重复触发。",
        fields={
            "cooldown_seconds": "同群冷却（秒）",
            "hourly_limit": "每小时上限",
            "delay_seconds": "命中后延迟（秒）",
            "retry_delay_seconds": "投递重试间隔（秒）",
            "max_text_chars": "任务书原文上限（字符）",
        },
    ),
    "plugin_info": _Section(
        source="plugin",
        title="插件信息",
        description="版本与总开关。",
        fields={
            "enabled": "启用插件",
            "config_version": "配置版本",
        },
        collapsed=True,
    ),
}

_TABS = {
    "basic": ("基础", ("target", "scope")),
    "behavior": ("预判与节奏", ("decide", "reply")),
    "advanced": ("高级", ("prompt", "plugin_info")),
}

_HINTS: Dict[str, str] = {
    "target.user": "被 @ 的目标账号，格式 platform:user_id，例如 qq:123456789；只支持填一个。",
    "target.presence_policy": "yield = 目标账号最近发过言就让路；engage = 他在线也照常交给预判。",
    "target.presence_window_seconds": "判断「最近发过言」的时间窗，0~3600 秒；0 表示不启用让路。",
    "scope.platforms": "只处理这些平台的消息，例如 qq；留空表示不限平台。",
    "scope.groups": "只在这些群生效，填群号；留空 = 插件完全不生效（默认如此）。",
    "decide.mode": "cheap_then_planner = 先让预判判断，通过才触发；planner_only = 跳过预判直接触发（更费 token）。",
    "decide.task_name": "预判使用的模型任务，对应 model_config.toml 的 model_task_config；utils 快而便宜。",
    "decide.model_name": "直接指定具体模型名；留空则沿用上面的任务。",
    "decide.temperature": "预判温度，0 最稳定。",
    "decide.max_tokens": "预判最大输出；思考模型的 reasoning 也占额度，太小会出现「思考吃满、正文为空」。",
    "decide.confidence_threshold": "预判置信度低于该值就不接，范围 0~1。",
    "decide.rpc_timeout_ms": "预判 RPC 超时；宿主默认 30 秒，思考型模型经常不够。",
    "prompt.template": (
        "支持 $bot_name、$bot_persona、$place、$group_name、$target_name、$sender_name、"
        "$text、$target_state、$recent；其中 $text 和 $recent 必填。"
        "要求模型只输出 JSON，不要写回复正文。"
    ),
    "reply.cooldown_seconds": "同一聊天流两次接话之间的最短间隔，0 表示不冷却。",
    "reply.hourly_limit": "每小时最多触发几次接话，全局统计。",
    "reply.delay_seconds": "命中规则后延迟多久再读上下文并触发，留给主链注册会话。",
    "reply.retry_delay_seconds": "会话还没注册、主动任务投递失败时的重试间隔。",
    "reply.max_text_chars": "拼进 Planner 任务书的原文最大长度。",
    "plugin.enabled": "总开关；宿主在加载阶段就读这个开关，关闭时不会导入插件代码（Hook 也收不到消息），改完需要重启。",
    "plugin.config_version": "由插件维护，不建议手动修改。",
}

_DISABLED_FIELDS = {"plugin.config_version"}


def _build_field(source: str, name: str, template: Dict[str, Any], label: str, order: int) -> Dict[str, Any]:
    """在 SDK 给的字段定义上补中文标签、提示与顺序。"""

    path = f"{source}.{name}"
    hint = _HINTS.get(path, template.get("description") or "")
    entry = dict(template)
    entry.update(
        label=label,
        hint=hint,
        description=hint,
        order=order,
    )
    if path in _DISABLED_FIELDS:
        entry["disabled"] = True
    if path == "prompt.template":
        entry["rows"] = 14
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
