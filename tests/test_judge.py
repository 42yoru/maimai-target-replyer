"""L1 预判的 prompt 拼装与结果解析测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
from unittest import TestCase

import importlib.util
import sys
import time

PLUGIN_DIR = Path(__file__).resolve().parents[1]
MODULE_NAME = PLUGIN_DIR.name


def _load_judge_module() -> Any:
    """按 Runner 的加载方式导入插件的 judge 模块。"""

    if f"{MODULE_NAME}.judge" in sys.modules:
        return sys.modules[f"{MODULE_NAME}.judge"]
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME,
        str(PLUGIN_DIR / "plugin.py"),
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法为插件创建 import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return sys.modules[f"{MODULE_NAME}.judge"]


JUDGE = _load_judge_module()


def recent_message(message_id: str, user: str, text: str, seconds_ago: float) -> Dict[str, Any]:
    """构造一条最近消息。"""

    return {
        "message_id": message_id,
        "timestamp": str(time.time() - seconds_ago),
        "platform": "qq",
        "message_info": {
            "user_info": {"user_id": user, "user_nickname": "群友", "user_cardname": user},
            "group_info": {"group_id": "123456789", "group_name": "测试群"},
        },
        "raw_message": [{"type": "text", "data": text}],
        "processed_plain_text": text,
    }


class FormatRecentTest(TestCase):
    """最近消息格式化。"""

    def test_format_includes_name_and_text(self) -> None:
        text = JUDGE.format_recent_messages([recent_message("m2", "群友A", "在的", 60)])
        self.assertIn("群友A", text)
        self.assertIn("在的", text)
        self.assertRegex(text, r"\[\d{2}:\d{2}\]")

    def test_exclude_current_message(self) -> None:
        text = JUDGE.format_recent_messages(
            [recent_message("m1", "群友A", "当前消息", 1), recent_message("m2", "群友B", "别的消息", 60)],
            exclude_message_id="m1",
        )
        self.assertNotIn("当前消息", text)
        self.assertIn("别的消息", text)

    def test_empty(self) -> None:
        self.assertEqual(JUDGE.format_recent_messages([]), "（没有可用的最近消息）")


class BuildPromptTest(TestCase):
    """prompt 必须覆盖上下文、口径和 JSON 约束。"""

    def test_prompt_sections(self) -> None:
        prompt = JUDGE.build_prompt(
            group_name="测试群",
            target_name="目标用户",
            sender_name="群友A",
            text="今晚维护吗？",
            recent_messages=[recent_message("m2", "群友B", "维护改到 23 点了", 60)],
            current_message_id="m1",
            target_spoke_seconds_ago=120,
            bot_name="助手",
            bot_persona="是一个测试助手",
        )
        self.assertIn("测试群", prompt)
        self.assertIn("今晚维护吗？", prompt)
        self.assertIn("维护改到 23 点了", prompt)
        self.assertIn("最近 120 秒内发过言", prompt)
        self.assertIn("不模仿他", prompt)
        self.assertIn("不声明自己在替谁回答", prompt)
        self.assertIn('"action"', prompt)
        self.assertIn("你的名字是助手", prompt)
        self.assertIn("是一个测试助手", prompt)

    def test_default_template_has_no_hardcoded_persona(self) -> None:
        self.assertNotIn("夜夜", JUDGE.DEFAULT_PROMPT_TEMPLATE)
        self.assertNotIn("麦麦", JUDGE.DEFAULT_PROMPT_TEMPLATE)
        prompt = JUDGE.build_prompt(
            group_name="测试群",
            target_name="目标用户",
            sender_name="群友A",
            text="今晚维护吗？",
            recent_messages=[],
            bot_name="小明",
            bot_persona="是一个测试助手",
        )
        self.assertTrue(prompt.startswith("你的名字是小明"))
        self.assertIn("是一个测试助手", prompt)


class PromptTemplateTest(TestCase):
    """可配置提示词模板的校验与替换。"""

    def test_default_template_is_valid(self) -> None:
        identifiers = JUDGE.validate_prompt_template(JUDGE.DEFAULT_PROMPT_TEMPLATE)
        self.assertIn("text", identifiers)
        self.assertIn("recent", identifiers)

    def test_unknown_placeholder_rejected(self) -> None:
        with self.assertRaises(JUDGE.PromptTemplateError):
            JUDGE.validate_prompt_template("$text $recent $nope")

    def test_missing_required_placeholder_rejected(self) -> None:
        with self.assertRaises(JUDGE.PromptTemplateError):
            JUDGE.validate_prompt_template("只有 $text")

    def test_empty_template_rejected(self) -> None:
        with self.assertRaises(JUDGE.PromptTemplateError):
            JUDGE.validate_prompt_template("   ")

    def test_invalid_dollar_syntax_rejected(self) -> None:
        with self.assertRaises(JUDGE.PromptTemplateError):
            JUDGE.validate_prompt_template("$text $recent 价格$100")

    def test_custom_template(self) -> None:
        prompt = JUDGE.build_prompt(
            group_name="测试群",
            target_name="目标用户",
            sender_name="群友A",
            text="今晚维护吗？",
            recent_messages=[recent_message("m2", "群友B", "维护改到 23 点了", 60)],
            current_message_id="m1",
            target_spoke_seconds_ago=120,
            template="群：$group_name\n问题：$text\n目标：$target_state\n记录：\n$recent",
        )
        self.assertIn("群：测试群", prompt)
        self.assertIn("问题：今晚维护吗？", prompt)
        self.assertIn("最近 120 秒内发过言", prompt)
        self.assertIn("维护改到 23 点了", prompt)


class ParseDecisionTest(TestCase):
    """预判返回解析。"""

    def test_valid(self) -> None:
        decision = JUDGE.parse_decision(
            '{"action":"answer","confidence":0.82,"reason":"有答案","answer_points":"23 点维护"}'
        )
        self.assertEqual(decision.action, "answer")
        self.assertAlmostEqual(decision.confidence, 0.82)
        self.assertEqual(decision.answer_points, "23 点维护")

    def test_markdown_fence(self) -> None:
        decision = JUDGE.parse_decision(
            '```json\n{"action":"ignore","confidence":0.7,"reason":"玩笑","answer_points":""}\n```'
        )
        self.assertEqual(decision.action, "ignore")

    def test_unknown_action(self) -> None:
        with self.assertRaises(JUDGE.JudgeError):
            JUDGE.parse_decision('{"action":"reply","confidence":0.8,"reason":"x","answer_points":""}')

    def test_confidence_out_of_range(self) -> None:
        with self.assertRaises(JUDGE.JudgeError):
            JUDGE.parse_decision('{"action":"answer","confidence":1.5,"reason":"x","answer_points":""}')

    def test_not_json(self) -> None:
        with self.assertRaises(JUDGE.JudgeError):
            JUDGE.parse_decision("我觉得可以接")

    def test_empty(self) -> None:
        with self.assertRaises(JUDGE.JudgeError):
            JUDGE.parse_decision("")
