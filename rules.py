"""「替主人回话」的 L0 规则。

这里只有纯函数：解析配置、读入站消息、拼 intent。不访问宿主、不读写文件，
方便在单测里直接跑。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

ALL_TARGET_ID = "all"
"""@全体成员 在 at 段里使用的伪用户号。"""

COMMAND_PREFIXES = ("/", "／")
"""命令消息的前缀；宿主真正的命令匹配在更晚的链路，这里只能按前缀近似排除。"""

_EPOCH_MS_THRESHOLD = 1e11
"""超过这个量级就按毫秒时间戳处理（秒级时间戳要到公元 5138 年才会达到）。"""

_EPOCH_US_THRESHOLD = 1e14
"""超过这个量级就按微秒时间戳处理。"""


@dataclass(frozen=True)
class Target:
    """被 @ 时触发接话的账号。"""

    platform: str
    user_id: str

    @property
    def spec(self) -> str:
        """返回 `platform:user_id` 形式的配置写法。"""

        return f"{self.platform}:{self.user_id}"


def parse_target_spec(spec: str) -> Optional[Target]:
    """把 `platform:user_id` 拆成 Target；格式不合法时返回 None。

    只认英文冒号：全角冒号 `qq：123` 是常见误填，必须明确拒绝，不能悄悄当成合法配置。
    """

    platform, separator, user_id = str(spec or "").strip().partition(":")
    platform = platform.strip()
    user_id = user_id.strip()
    if not separator or not platform or not user_id or user_id == ALL_TARGET_ID:
        return None
    return Target(platform=platform, user_id=user_id)


def message_info(message: Dict[str, Any]) -> Dict[str, Any]:
    """返回消息里的 message_info 字典。"""

    info = message.get("message_info")
    return info if isinstance(info, dict) else {}


def group_info(message: Dict[str, Any]) -> Dict[str, Any]:
    """返回消息里的群信息字典。"""

    info = message_info(message).get("group_info")
    return info if isinstance(info, dict) else {}


def group_id(message: Dict[str, Any]) -> str:
    """返回群号；私聊返回空字符串。"""

    return str(group_info(message).get("group_id") or "").strip()


def group_name(message: Dict[str, Any]) -> str:
    """返回群名，取不到时为空字符串。"""

    return str(group_info(message).get("group_name") or "").strip()


def platform(message: Dict[str, Any]) -> str:
    """返回消息所属平台。"""

    return str(message.get("platform") or "").strip()


def is_group_message(message: Dict[str, Any]) -> bool:
    """判断是不是群聊消息。"""

    return bool(group_id(message))


def sender_id(message: Dict[str, Any]) -> str:
    """返回发送者用户号。"""

    user_info = message_info(message).get("user_info")
    if not isinstance(user_info, dict):
        return ""
    return str(user_info.get("user_id") or "").strip()


def sender_nickname(message: Dict[str, Any]) -> str:
    """返回发送者昵称，取不到时回落到用户号。"""

    user_info = message_info(message).get("user_info")
    if not isinstance(user_info, dict):
        return ""
    nickname = str(user_info.get("user_cardname") or user_info.get("user_nickname") or "").strip()
    return nickname or str(user_info.get("user_id") or "").strip()


def at_items(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    """返回消息里所有 at 段的 data 字典。"""

    components = message.get("raw_message")
    if not isinstance(components, list):
        return []
    items: List[Dict[str, Any]] = []
    for component in components:
        if not isinstance(component, dict) or component.get("type") != "at":
            continue
        data = component.get("data")
        if isinstance(data, dict):
            items.append(data)
    return items


def at_target_ids(message: Dict[str, Any]) -> List[str]:
    """返回消息里所有被 @ 的用户号。"""

    ids: List[str] = []
    for item in at_items(message):
        target_user_id = str(item.get("target_user_id") or "").strip()
        if target_user_id:
            ids.append(target_user_id)
    return ids


def has_at(message: Dict[str, Any], user_id: str) -> bool:
    """判断消息是否 @ 了指定用户。"""

    if not user_id:
        return False
    return user_id in at_target_ids(message)


def at_display_name(message: Dict[str, Any], user_id: str) -> str:
    """取 at 段里带的群名片 / 昵称，取不到时回落到用户号。"""

    for item in at_items(message):
        if str(item.get("target_user_id") or "").strip() != user_id:
            continue
        name = str(item.get("target_user_cardname") or item.get("target_user_nickname") or "").strip()
        return name or user_id
    return user_id


def message_text(message: Dict[str, Any]) -> str:
    """取消息纯文本。

    `chat.receive.after_process` 之后 `processed_plain_text` 一定存在；这里仍允许从
    text 段拼一次，是为了让规则函数在没有宿主预处理的单测里也能用。
    """

    processed = message.get("processed_plain_text")
    if isinstance(processed, str) and processed.strip():
        return processed.strip()
    components = message.get("raw_message")
    if not isinstance(components, list):
        return ""
    parts: List[str] = []
    for component in components:
        if not isinstance(component, dict) or component.get("type") != "text":
            continue
        data = component.get("data")
        if isinstance(data, str):
            parts.append(data)
    return "".join(parts).strip()


def is_command_text(text: str) -> bool:
    """按前缀粗略判断是不是命令消息。"""

    return text.strip().startswith(COMMAND_PREFIXES)


def message_epoch(message: Dict[str, Any]) -> Optional[float]:
    """把消息里的时间戳统一转成秒级浮点数；格式不对返回 None。

    不同适配器可能给秒、毫秒或微秒，量级差 1000 倍会让「最近发言」的窗口直接失效，
    所以这里按数值量级归一化，而不是假定只有秒。
    """

    raw = message.get("timestamp")
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return None

    magnitude = abs(value)
    if magnitude >= _EPOCH_US_THRESHOLD:
        return value / 1_000_000
    if magnitude >= _EPOCH_MS_THRESHOLD:
        return value / 1_000
    return value


def build_intent(
    *,
    sender_name: str,
    target_name: str,
    group_name: str,
    text: str,
    bot_name: str,
    answer_points: str = "",
) -> str:
    """拼交给 Maisaka 的主动任务说明。

    口径是「自然接话」：不提「目标账号不在 / 替他答」，也不要求模仿任何人。
    `bot_name` 来自宿主配置；`answer_points` 是 L1 预判给出的回答要点，
    仅在预判判断可以接时传入。
    """

    place = f"群「{group_name}」" if group_name else "群里"
    lines = [
        f"{sender_name} 在{place} @ 了 {target_name}：{text}",
        f"这是发给 {target_name} 的消息，他本人没有回应。",
        f"请判断要不要以{bot_name}自己的身份自然地接一句：能答就直接答，不要因为「这是在问他」就让开。",
        "不模仿对方，也不要声明自己在代替谁回答；只说有依据的内容。",
        "只有涉及隐私、承诺、明显是玩笑，或者你确实答不上来时，才 stop()。",
    ]
    if answer_points:
        lines.insert(3, f"可参考的要点（不是最终回复，按你自己的语气说）：{answer_points}")
    return "\n".join(lines)
