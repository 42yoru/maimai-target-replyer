"""插件集成测试：规则命中后投递主动任务，以及各种让路条件。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import IsolatedAsyncioTestCase

import importlib.util
import json
import sys
import tempfile
import time

from maibot_sdk import PluginContext, PluginPaths

PLUGIN_DIR = Path(__file__).resolve().parents[1]
MODULE_NAME = PLUGIN_DIR.name
MANIFEST = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
BOT_ID = "100000003"
TARGET_ID = "100000001"
SENDER_ID = "200000002"
GROUP_ID = "123456789"
SESSION_ID = "session-1"


def _load_plugin_module() -> Any:
    """按 Runner 的加载方式导入插件模块。"""

    if MODULE_NAME in sys.modules:
        return sys.modules[MODULE_NAME]
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
    return module


PLUGIN_MODULE = _load_plugin_module()


def build_config(**sections: Any) -> Dict[str, Any]:
    """构造插件配置；只覆盖传入的配置节。"""

    config: Dict[str, Any] = {
        "plugin": {"enabled": True, "config_version": MANIFEST["version"]},
        "target": {"user": f"qq:{TARGET_ID}", "presence_policy": "yield", "presence_window_seconds": 180},
        "scope": {"platforms": ["qq"], "groups": [GROUP_ID]},
        "decide": {
            "mode": "cheap_then_planner",
            "task_name": "utils",
            "model_name": "",
            "temperature": 0.0,
            "max_tokens": 2000,
            "confidence_threshold": 0.6,
            "rpc_timeout_ms": 60000,
        },
        "reply": {
            "cooldown_seconds": 0,
            "hourly_limit": 6,
            "delay_seconds": 0.0,
            "retry_delay_seconds": 0.0,
            "max_text_chars": 500,
        },
    }
    config.update(sections)
    return config


def build_message(**overrides: Any) -> Dict[str, Any]:
    """构造一条入站群消息。"""

    message: Dict[str, Any] = {
        "message_id": "m1",
        "timestamp": str(time.time()),
        "platform": "qq",
        "account_id": "10001",
        "scope": "group",
        "message_info": {
            "user_info": {"user_id": SENDER_ID, "user_nickname": "群友", "user_cardname": "群友A"},
            "group_info": {"group_id": GROUP_ID, "group_name": "测试群"},
        },
        "raw_message": [
            {
                "type": "at",
                "data": {
                    "target_user_id": TARGET_ID,
                    "target_user_nickname": "目标用户",
                    "target_user_cardname": "目标用户",
                },
            },
            {"type": "text", "data": " 今晚维护吗？"},
        ],
        "is_notify": False,
        "session_id": SESSION_ID,
        "processed_plain_text": "@目标用户 今晚维护吗？",
    }
    message.update(overrides)
    return message


class FakeHost:
    """替身宿主：记录主动任务调用并返回可编排的结果。"""

    def __init__(self) -> None:
        self.bot_account = BOT_ID
        self.bot_name = "助手"
        self.bot_persona = "是一个测试助手"
        self.recent: List[Dict[str, Any]] = []
        self.proactive_calls: List[Dict[str, Any]] = []
        self.trigger_results: List[Dict[str, Any]] = [{"success": True, "task_id": "proactive:1"}]
        self.llm_calls: List[Dict[str, Any]] = []
        self.llm_timeouts: List[Optional[int]] = []
        self.llm_result: Dict[str, Any] = {
            "success": True,
            "response": json.dumps(
                {
                    "action": "answer",
                    "confidence": 0.9,
                    "reason": "最近消息里有明确答案",
                    "answer_points": "维护今晚 23:00 开始，预计 2 小时",
                },
                ensure_ascii=False,
            ),
        }

    async def rpc(
        self,
        method: str,
        plugin_id: str,
        payload: Dict[str, Any],
        timeout_ms: Optional[int] = None,
    ) -> Any:
        """接受 sdk 的 cap.call，并按能力名分发。"""

        del plugin_id
        if method != "cap.call":
            raise AssertionError(f"未预期的 host 方法: {method}")
        capability = str(payload.get("capability") or "")
        args = payload.get("args") or {}

        if capability == "config.get":
            key = args.get("key")
            if key == "bot.qq_account":
                return {"success": True, "value": self.bot_account}
            if key == "bot.nickname":
                return {"success": True, "value": self.bot_name}
            if key == "personality.personality":
                return {"success": True, "value": self.bot_persona}
            return {"success": True, "value": None}
        if capability == "message.get_recent":
            return {"success": True, "messages": list(self.recent)}
        if capability == "llm.generate":
            self.llm_calls.append(dict(args))
            self.llm_timeouts.append(timeout_ms)
            return self.llm_result
        if capability == "maisaka.proactive.trigger":
            self.proactive_calls.append(dict(args))
            if len(self.trigger_results) > 1:
                return self.trigger_results.pop(0)
            return self.trigger_results[0]
        raise AssertionError(f"未预期的能力调用: {capability}")


class PluginTestCase(IsolatedAsyncioTestCase):
    """带替身宿主与临时数据目录的测试基类。"""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.host = FakeHost()
        self.plugin = PLUGIN_MODULE.TargetReplyerPlugin()
        context = PluginContext(
            plugin_id=MANIFEST["id"],
            rpc_call=self.host.rpc,
            paths=PluginPaths(data_dir=Path(self._tmp.name), runtime_dir=Path(self._tmp.name)),
        )
        self.plugin._set_context(context)
        self.plugin.set_plugin_config(build_config())
        await self.plugin.on_load()

    async def asyncTearDown(self) -> None:
        await self.plugin.on_unload()

    async def deliver(self, message: Dict[str, Any]) -> None:
        """调用入站 Hook 并等待后台任务结束。"""

        await self.plugin.watch_inbound(message=message)
        await self.plugin.wait_pending()


class TriggerTest(PluginTestCase):
    """命中规则时的投递行为。"""

    async def test_triggers_once_with_natural_intent(self) -> None:
        await self.deliver(build_message())

        self.assertEqual(len(self.host.proactive_calls), 1)
        call = self.host.proactive_calls[0]
        self.assertEqual(call["stream_id"], SESSION_ID)
        self.assertIn("今晚维护吗？", call["intent"])
        self.assertIn("群友A", call["intent"])
        self.assertIn("目标用户", call["intent"])
        self.assertIn("自然", call["intent"])
        self.assertNotIn("不在", call["intent"])
        self.assertEqual(call["metadata"]["trigger_message_id"], "m1")
        self.assertEqual(call["metadata"]["trigger_user_id"], SENDER_ID)

    async def test_dedupe_same_message(self) -> None:
        message = build_message()
        await self.deliver(message)
        await self.deliver(dict(message))
        self.assertEqual(len(self.host.proactive_calls), 1)

    async def test_same_stream_only_one_pending(self) -> None:
        await self.plugin.watch_inbound(message=build_message(message_id="m1"))
        await self.plugin.watch_inbound(message=build_message(message_id="m2"))
        await self.plugin.wait_pending()
        self.assertEqual(len(self.host.proactive_calls), 1)

    async def test_non_retryable_error_gives_up_immediately(self) -> None:
        """配置错/未授权这类错误重试三次也是同一个结果，只该试一次。"""

        self.host.trigger_results = [{"success": False, "error": "插件未获授权：maisaka.proactive.trigger"}]

        await self.deliver(build_message())

        self.assertEqual(len(self.host.proactive_calls), 1)

    async def test_retry_when_session_not_ready(self) -> None:
        self.host.trigger_results = [
            {"success": False, "error": "未找到已存在的聊天流"},
            {"success": False, "error": "未找到已存在的聊天流"},
            {"success": True, "task_id": "proactive:2"},
        ]
        await self.deliver(build_message())
        self.assertEqual(len(self.host.proactive_calls), 3)


class SkipRulesTest(PluginTestCase):
    """不该触发的情况。"""

    async def test_not_a_group(self) -> None:
        message = build_message()
        message["message_info"] = {"user_info": {"user_id": SENDER_ID, "user_nickname": "群友"}}
        await self.deliver(message)
        self.assertEqual(self.host.proactive_calls, [])

    async def test_group_not_in_whitelist(self) -> None:
        await self.deliver(build_message(message_info={
            "user_info": {"user_id": SENDER_ID, "user_nickname": "群友"},
            "group_info": {"group_id": "123456", "group_name": "别的群"},
        }))
        self.assertEqual(self.host.proactive_calls, [])

    async def test_empty_group_whitelist(self) -> None:
        self.plugin.set_plugin_config(build_config(scope={"platforms": ["qq"], "groups": []}))
        await self.deliver(build_message())
        self.assertEqual(self.host.proactive_calls, [])

    async def test_target_not_mentioned(self) -> None:
        message = build_message(raw_message=[{"type": "text", "data": "随便聊聊"}], processed_plain_text="随便聊聊")
        await self.deliver(message)
        self.assertEqual(self.host.proactive_calls, [])

    async def test_sender_is_target(self) -> None:
        message = build_message()
        message["message_info"]["user_info"]["user_id"] = TARGET_ID
        await self.deliver(message)
        self.assertEqual(self.host.proactive_calls, [])

    async def test_sender_is_bot(self) -> None:
        message = build_message()
        message["message_info"]["user_info"]["user_id"] = BOT_ID
        await self.deliver(message)
        self.assertEqual(self.host.proactive_calls, [])

    async def test_also_at_bot(self) -> None:
        message = build_message()
        message["raw_message"].append({"type": "at", "data": {"target_user_id": BOT_ID}})
        await self.deliver(message)
        self.assertEqual(self.host.proactive_calls, [])

    async def test_bot_identity_works_without_configured_account(self) -> None:
        """bot.qq_account 为空时，仍靠消息自带的 account_id 认出 bot 自己。

        宿主里 bot.qq_account 只是「适配器没上报身份时的备用账号」、默认空串；一旦为空，
        原来三条让路规则会全部静默失效——而那是插件唯一的防重复回复机制。
        """

        self.host.bot_account = ""

        message = build_message()
        message["message_info"]["user_info"]["user_id"] = "10001"  # 等于消息自带的 account_id
        await self.deliver(message)

        self.assertEqual(self.host.proactive_calls, [])

    async def test_also_at_bot_without_configured_account(self) -> None:
        """同上：@ 了"消息归属的 bot 账号"也算 @ 了 bot 自己。"""

        self.host.bot_account = ""

        message = build_message()
        message["raw_message"].append({"type": "at", "data": {"target_user_id": "10001"}})
        await self.deliver(message)

        self.assertEqual(self.host.proactive_calls, [])

    async def test_command_text(self) -> None:
        message = build_message(processed_plain_text="/接话 状态")
        await self.deliver(message)
        self.assertEqual(self.host.proactive_calls, [])

    async def test_empty_text(self) -> None:
        message = build_message(processed_plain_text="", raw_message=[{"type": "at", "data": {"target_user_id": TARGET_ID}}])
        await self.deliver(message)
        self.assertEqual(self.host.proactive_calls, [])

    async def test_missing_session_id(self) -> None:
        await self.deliver(build_message(session_id=""))
        self.assertEqual(self.host.proactive_calls, [])

    async def test_notify_message(self) -> None:
        await self.deliver(build_message(is_notify=True))
        self.assertEqual(self.host.proactive_calls, [])


class CooldownTest(PluginTestCase):
    """冷却与小时额度。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.plugin.set_plugin_config(build_config(reply={
            "cooldown_seconds": 180,
            "hourly_limit": 6,
            "delay_seconds": 0.0,
            "retry_delay_seconds": 0.0,
            "max_text_chars": 500,
        }))
        await self.plugin.on_config_update("self", {}, MANIFEST["version"])

    async def test_cooldown_blocks_second_message(self) -> None:
        await self.deliver(build_message(message_id="m1"))
        await self.deliver(build_message(message_id="m2"))
        self.assertEqual(len(self.host.proactive_calls), 1)

    async def test_hourly_limit(self) -> None:
        self.plugin.set_plugin_config(build_config(reply={
            "cooldown_seconds": 0,
            "hourly_limit": 1,
            "delay_seconds": 0.0,
            "retry_delay_seconds": 0.0,
            "max_text_chars": 500,
        }))
        await self.plugin.on_config_update("self", {}, MANIFEST["version"])
        await self.deliver(build_message(message_id="m1"))
        await self.deliver(build_message(message_id="m2"))
        self.assertEqual(len(self.host.proactive_calls), 1)


class PresenceTest(PluginTestCase):
    """目标账号 / bot最近发言时的让路。"""

    def recent_from(self, user_id: str, seconds_ago: float) -> Dict[str, Any]:
        return {
            "message_id": f"recent-{user_id}",
            "timestamp": str(time.time() - seconds_ago),
            "platform": "qq",
            "message_info": {
                "user_info": {"user_id": user_id, "user_nickname": "某人"},
                "group_info": {"group_id": GROUP_ID, "group_name": "测试群"},
            },
            "raw_message": [{"type": "text", "data": "刚说了一句"}],
            "session_id": SESSION_ID,
        }

    async def test_yield_when_target_active(self) -> None:
        self.host.recent = [self.recent_from(TARGET_ID, 10)]
        await self.deliver(build_message())
        self.assertEqual(self.host.proactive_calls, [])

    async def test_engage_when_target_active(self) -> None:
        self.plugin.set_plugin_config(build_config(target={
            "user": f"qq:{TARGET_ID}",
            "presence_policy": "engage",
            "presence_window_seconds": 180,
        }))
        await self.plugin.on_config_update("self", {}, MANIFEST["version"])
        self.host.recent = [self.recent_from(TARGET_ID, 10)]
        await self.deliver(build_message())
        self.assertEqual(len(self.host.proactive_calls), 1)

    async def test_yield_when_target_active_long_ago(self) -> None:
        self.host.recent = [self.recent_from(TARGET_ID, 600)]
        await self.deliver(build_message())
        self.assertEqual(len(self.host.proactive_calls), 1)

    async def test_yield_when_bot_active(self) -> None:
        self.host.recent = [self.recent_from(BOT_ID, 10)]
        await self.deliver(build_message())
        self.assertEqual(self.host.proactive_calls, [])


class JudgeIntegrationTest(PluginTestCase):
    """L1 预判接入后的行为。"""

    async def test_judge_prompt_and_routing(self) -> None:
        await self.deliver(build_message())

        self.assertEqual(len(self.host.llm_calls), 1)
        call = self.host.llm_calls[0]
        self.assertEqual(call["task_name"], "utils")
        self.assertEqual(call["temperature"], 0.0)
        self.assertEqual(call["max_tokens"], 2000)
        self.assertEqual(self.host.llm_timeouts[0], 60000)
        prompt = call["prompt"]
        self.assertIn("今晚维护吗？", prompt)
        self.assertIn("测试群", prompt)
        self.assertIn("目标用户", prompt)
        self.assertIn("你的名字是助手", prompt)
        self.assertIn("是一个测试助手", prompt)

    async def test_judge_answer_points_go_into_intent(self) -> None:
        await self.deliver(build_message())
        intent = self.host.proactive_calls[0]["intent"]
        self.assertIn("维护今晚 23:00 开始", intent)
        self.assertIn("不是最终回复", intent)

    async def test_judge_ignore_skips_trigger(self) -> None:
        self.host.llm_result = {
            "success": True,
            "response": json.dumps(
                {"action": "ignore", "confidence": 0.9, "reason": "只是顺手 @", "answer_points": ""},
                ensure_ascii=False,
            ),
        }
        await self.deliver(build_message())
        self.assertEqual(self.host.proactive_calls, [])

    async def test_low_confidence_skips_trigger(self) -> None:
        self.host.llm_result = {
            "success": True,
            "response": json.dumps(
                {"action": "answer", "confidence": 0.3, "reason": "不太有把握", "answer_points": "随便答"},
                ensure_ascii=False,
            ),
        }
        await self.deliver(build_message())
        self.assertEqual(self.host.proactive_calls, [])

    async def test_invalid_json_skips_trigger(self) -> None:
        self.host.llm_result = {"success": True, "response": "我觉得可以接"}
        await self.deliver(build_message())
        self.assertEqual(self.host.proactive_calls, [])

    async def test_llm_failure_skips_trigger(self) -> None:
        self.host.llm_result = {"success": False, "error": "model timeout"}
        await self.deliver(build_message())
        self.assertEqual(self.host.proactive_calls, [])

    async def test_planner_only_skips_judge(self) -> None:
        self.plugin.set_plugin_config(
            build_config(
                decide={
                    "mode": "planner_only",
                    "task_name": "utils",
                    "model_name": "",
                    "temperature": 0.0,
                    "max_tokens": 2000,
                    "confidence_threshold": 0.6,
                    "rpc_timeout_ms": 60000,
                }
            )
        )
        await self.plugin.on_config_update("self", {}, MANIFEST["version"])
        await self.deliver(build_message())
        self.assertEqual(self.host.llm_calls, [])
        self.assertEqual(len(self.host.proactive_calls), 1)

    async def test_custom_prompt_from_config(self) -> None:
        self.plugin.set_plugin_config(
            build_config(prompt={"template": "自定义预判\n问题：$text\n记录：$recent\n群：$group_name"})
        )
        await self.plugin.on_config_update("self", {}, MANIFEST["version"])
        await self.deliver(build_message())
        self.assertEqual(len(self.host.llm_calls), 1)
        prompt = self.host.llm_calls[0]["prompt"]
        self.assertTrue(prompt.startswith("自定义预判"))
        self.assertIn("今晚维护吗？", prompt)

    async def test_invalid_prompt_template_skips_trigger(self) -> None:
        self.plugin.set_plugin_config(build_config(prompt={"template": "缺少必需占位符"}))
        await self.plugin.on_config_update("self", {}, MANIFEST["version"])
        await self.deliver(build_message())
        self.assertEqual(self.host.llm_calls, [])
        self.assertEqual(self.host.proactive_calls, [])
