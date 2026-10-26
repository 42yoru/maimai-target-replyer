"""L1 接话预判：用便宜模型判断「要不要自然接一句」。

只做判断，不生成回复正文；真正怎么说仍然交给 Planner。输出严格 JSON，
解析失败一律当成「不接」并把错误暴露给调用方。

提示词模板可由配置覆盖，支持这些 ``$`` 占位符：

``$place`` ``$group_name`` ``$target_name`` ``$sender_name``
``$text`` ``$target_state`` ``$recent``
"""

from __future__ import annotations

from dataclasses import dataclass
from string import Template
from typing import Any, Dict, List, Optional, Sequence

import json
import time

from .rules import message_epoch, message_text, sender_nickname

JUDGE_ACTIONS = ("answer", "ignore", "defer")
"""预判允许返回的动作。"""

RECENT_MESSAGE_LIMIT = 20
"""拼进 prompt 的最近消息条数上限。"""

RECENT_TEXT_MAX_CHARS = 200
"""单条最近消息的最大字符数。"""

PROMPT_PLACEHOLDERS = frozenset(
    {
        "place",
        "group_name",
        "target_name",
        "sender_name",
        "text",
        "target_state",
        "recent",
        "bot_name",
        "bot_persona",
    }
)
"""提示词模板允许使用的占位符。"""

REQUIRED_PLACEHOLDERS = frozenset({"text", "recent"})
"""提示词模板必须包含的占位符。"""

DEFAULT_PROMPT_TEMPLATE = """你的名字是$bot_name。以下是你的人设：
$bot_persona

现在$place有人 @ 了「$target_name」，这条消息是发给他的，但他本人没有回应。

你的任务只有一个：判断这条消息要不要由你自己自然接一句。不要写回复正文。

【群】$group_name
【发消息的人】$sender_name
【被 @ 的消息】$text
【目标账号】$target_state

【最近聊天记录】
$recent

判断标准：
- 可以接：事实、进度、安排、物品/人在不在、最近聊天里明确出现过的信息、轻量寒暄。
- 不接：隐私（住址/收入/账号/私聊内容）、金钱与承诺、情感与站队、需要他的最新状态、明显是吐槽或玩笑、只是顺手 @。
- 答案必须在最近聊天记录或常识里有依据；没有依据就不要接，不许编。
- 你以自己的身份说话，不模仿他，也不声明自己在替谁回答。

只输出一个 JSON 对象，不要 markdown、不要解释：
{"action":"answer|ignore|defer","confidence":0到1的小数,
 "reason":"一句话原因","answer_points":"可以回答的要点；ignore/defer 时留空"}"""
"""默认提示词模板；配置里的 ``prompt.template`` 会覆盖它。

``$bot_name`` / ``$bot_persona`` 由宿主配置注入，不在插件里写死具体人设。
"""


class JudgeError(RuntimeError):
    """预判调用或结果解析失败。"""


class PromptTemplateError(ValueError):
    """提示词模板配置错误。"""


@dataclass(frozen=True)
class JudgeDecision:
    """预判结论。"""

    action: str
    confidence: float
    reason: str
    answer_points: str


def validate_prompt_template(template: str) -> List[str]:
    """校验提示词模板，返回实际用到的占位符。

    Args:
        template: 配置里的提示词模板。

    Returns:
        List[str]: 模板里出现的占位符名称（已排序）。

    Raises:
        PromptTemplateError: 模板为空、缺少必需占位符或出现未知占位符时抛出。
    """

    text = str(template or "")
    if not text.strip():
        raise PromptTemplateError("提示词模板为空")
    identifiers = set(Template(text).get_identifiers())
    unknown = sorted(identifiers - PROMPT_PLACEHOLDERS)
    if unknown:
        raise PromptTemplateError(
            "模板里有未知占位符：" + "、".join(f"${name}" for name in unknown)
        )
    missing = sorted(REQUIRED_PLACEHOLDERS - identifiers)
    if missing:
        raise PromptTemplateError(
            "模板缺少必需占位符：" + "、".join(f"${name}" for name in missing)
        )
    return sorted(identifiers)


def format_recent_messages(
    messages: Sequence[Dict[str, Any]],
    *,
    exclude_message_id: str = "",
    limit: int = RECENT_MESSAGE_LIMIT,
) -> str:
    """把最近消息格式化成给预判看的纯文本记录。"""

    lines: List[str] = []
    for message in list(messages)[-limit:]:
        if not isinstance(message, dict):
            continue
        if exclude_message_id and str(message.get("message_id") or "") == exclude_message_id:
            continue
        text = message_text(message)
        if not text:
            text = "[非文本消息]"
        text = text[:RECENT_TEXT_MAX_CHARS]
        occurred_at = message_epoch(message)
        clock = time.strftime("%H:%M", time.localtime(occurred_at)) if occurred_at is not None else "--:--"
        name = sender_nickname(message) or "某人"
        lines.append(f"[{clock}] {name}: {text}")
    return "\n".join(lines) if lines else "（没有可用的最近消息）"


def build_prompt(
    *,
    group_name: str,
    target_name: str,
    sender_name: str,
    text: str,
    recent_messages: Sequence[Dict[str, Any]],
    current_message_id: str = "",
    target_spoke_seconds_ago: Optional[float] = None,
    bot_name: str = "",
    bot_persona: str = "",
    template: str = DEFAULT_PROMPT_TEMPLATE,
) -> str:
    """按模板拼预判 prompt。

    Raises:
        PromptTemplateError: 模板里出现未知占位符或无法替换时抛出。
    """

    place = f"群「{group_name}」" if group_name else "群里"
    if target_spoke_seconds_ago is None:
        target_state = "最近没有发言"
    else:
        target_state = f"最近 {int(target_spoke_seconds_ago)} 秒内发过言"

    values = {
        "place": place,
        "group_name": group_name or "未知",
        "target_name": target_name,
        "sender_name": sender_name,
        "text": text,
        "target_state": target_state,
        "recent": format_recent_messages(recent_messages, exclude_message_id=current_message_id),
        "bot_name": bot_name or "助手",
        "bot_persona": bot_persona,
    }
    validate_prompt_template(template)
    try:
        return Template(template).substitute(values)
    except (KeyError, ValueError) as exc:
        raise PromptTemplateError(f"提示词模板替换失败: {exc}") from exc


def parse_decision(raw: str) -> JudgeDecision:
    """解析预判输出；结构不合法时抛 JudgeError。"""

    text = str(raw or "").strip()
    if not text:
        raise JudgeError("预判返回为空")
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise JudgeError(f"预判返回里找不到 JSON: {text[:200]!r}")

    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise JudgeError(f"预判返回不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise JudgeError("预判返回的 JSON 顶层必须是对象")

    action = str(payload.get("action") or "").strip()
    if action not in JUDGE_ACTIONS:
        raise JudgeError(f"预判返回了未知 action: {action!r}")

    try:
        confidence = float(payload.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise JudgeError("预判返回的 confidence 不是数字") from exc
    if not 0.0 <= confidence <= 1.0:
        raise JudgeError(f"预判返回的 confidence 越界: {confidence}")

    reason = str(payload.get("reason") or "").strip()
    answer_points = str(payload.get("answer_points") or "").strip()
    return JudgeDecision(
        action=action,
        confidence=confidence,
        reason=reason,
        answer_points=answer_points,
    )
