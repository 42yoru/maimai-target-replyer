"""「替主人回话」插件入口。

设计上只认「被 @ 的目标账号」：配置一个 `platform:user_id`，群里有人 @ 它时，
插件先做零成本规则筛选，命中后用便宜模型预判要不要自然接一句；预判通过再投
一个 Maisaka 主动任务，由宿主自己的 Planner 决定怎么表达。

插件不写死任何具体 bot 人设：bot 的名字与人格从宿主配置读取，注入提示词。
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Set

import asyncio
import time

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .config_ui import build_config_ui_schema
from .judge import (
    DEFAULT_PROMPT_TEMPLATE,
    JudgeDecision,
    JudgeError,
    PromptTemplateError,
    build_prompt,
    parse_decision,
    validate_prompt_template,
)
from .rules import (
    Target,
    at_display_name,
    at_target_ids,
    build_intent,
    group_id,
    group_name,
    has_at,
    is_command_text,
    is_group_message,
    message_epoch,
    message_text,
    parse_target_spec,
    platform,
    sender_id,
    sender_nickname,
)
from .state import StateError, StateStore

WATCH_HOOK_TIMEOUT_MS = 2000
"""入站 Hook 的处理器超时；真正的等待都放在后台任务里。"""

BOT_RECENT_WINDOW_SECONDS = 40
"""bot 刚刚在本流发过言时让路的时间窗。"""

TRIGGER_MAX_ATTEMPTS = 3
"""主动任务投递的最大尝试次数（会话刚注册时可能查不到）。"""

_TRANSIENT_TRIGGER_ERRORS = ("未找到已存在的聊天流", "not found", "timeout", "超时")


def _is_transient_trigger_error(error_text: str) -> bool:
    """投递失败是否值得重试。

    只有「会话刚注册、宿主还没登记这条流」和网络类抖动才重试；配置错、未授权这类
    重试三次也是同一个结果，只会把这条消息的处理时间拖长。
    """

    return any(marker in error_text for marker in _TRANSIENT_TRIGGER_ERRORS)


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=False,
        description="是否启用插件；宿主在加载阶段读取，关闭时不会导入插件代码，改完需要重启",
    )
    config_version: str = Field(default="0.3.5", description="配置版本")


class TargetConfig(PluginConfigBase):
    """触发目标配置。"""

    __ui_label__ = "触发目标"
    __ui_icon__ = "at-sign"
    __ui_order__ = 1

    user: str = Field(
        default="",
        description="被 @ 时触发接话的账号，格式 platform:user_id，例如 qq:123456789；只支持一个",
    )
    presence_policy: Literal["yield", "engage"] = Field(
        default="yield",
        description="yield=目标账号最近发过言就让路；engage=他在线也照常接",
    )
    presence_window_seconds: int = Field(
        default=180,
        ge=0,
        le=3600,
        description="判断「目标账号最近发过言」的时间窗，单位秒",
    )


class ScopeConfig(PluginConfigBase):
    """生效范围配置。"""

    __ui_label__ = "生效范围"
    __ui_icon__ = "scope"
    __ui_order__ = 2

    platforms: List[str] = Field(default_factory=lambda: ["qq"], description="只处理这些平台的消息")
    groups: List[str] = Field(default_factory=list, description="只处理这些群；留空表示不生效")


class DecideConfig(PluginConfigBase):
    """接话预判配置。"""

    __ui_label__ = "接话预判"
    __ui_icon__ = "scale"
    __ui_order__ = 3

    mode: Literal["cheap_then_planner", "planner_only"] = Field(
        default="cheap_then_planner",
        description="cheap_then_planner=先用小模型判断再决定要不要触发；planner_only=跳过预判直接触发",
    )
    task_name: str = Field(default="utils", description="预判使用的模型任务名，默认 utils（快而便宜）")
    model_name: str = Field(default="", description="可直选具体模型；留空则沿用 task_name")
    temperature: float = Field(default=0.0, ge=0.0, le=1.0, description="预判温度")
    max_tokens: int = Field(default=2000, ge=50, le=4000, description="预判最大输出 token（要留出思考模型的 reasoning 余量）")
    confidence_threshold: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="confidence 低于该值就不触发",
    )
    rpc_timeout_ms: int = Field(default=60000, ge=1000, le=300000, description="预判 RPC 超时，默认 60 秒")


class PromptConfig(PluginConfigBase):
    """提示词配置。"""

    __ui_label__ = "提示词"
    __ui_icon__ = "message-square-text"
    __ui_order__ = 4

    template: str = Field(
        default=DEFAULT_PROMPT_TEMPLATE,
        description=(
            "接话预判的提示词模板；支持 $place、$group_name、$target_name、$sender_name、"
            "$text、$target_state、$recent 占位符，其中 $text 和 $recent 必填"
        ),
    )


class ReplyConfig(PluginConfigBase):
    """触发节奏配置。"""

    __ui_label__ = "触发节奏"
    __ui_icon__ = "timer"
    __ui_order__ = 5

    cooldown_seconds: int = Field(default=180, ge=0, le=3600, description="同一聊天流两次接话之间的最短间隔")
    hourly_limit: int = Field(default=6, ge=1, le=60, description="每小时最多触发几次接话")
    delay_seconds: float = Field(default=1.5, ge=0.0, le=10.0, description="命中后延迟多久再读上下文并触发")
    retry_delay_seconds: float = Field(default=1.5, ge=0.0, le=10.0, description="会话还没注册时的重试间隔")
    max_text_chars: int = Field(default=500, ge=50, le=2000, description="拼进 intent 的原文最大长度")


class TargetReplyerConfig(PluginConfigBase):
    """插件配置模型。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    target: TargetConfig = Field(default_factory=TargetConfig)
    scope: ScopeConfig = Field(default_factory=ScopeConfig)
    decide: DecideConfig = Field(default_factory=DecideConfig)
    prompt: PromptConfig = Field(default_factory=PromptConfig)
    reply: ReplyConfig = Field(default_factory=ReplyConfig)


class TargetReplyerPlugin(MaiBotPlugin):
    """被 @ 目标账号时触发自然接话的插件。"""

    config_model = TargetReplyerConfig

    @classmethod
    def build_config_schema(cls, **plugin_info: Any) -> Dict[str, Any]:
        """重组 WebUI 展示结构；配置模型与 TOML 路径保持不变。"""

        return build_config_ui_schema(super().build_config_schema(**plugin_info))

    def __init__(self) -> None:
        """初始化运行时状态。"""

        super().__init__()
        self._target: Optional[Target] = None
        self._bot_account = ""
        self._bot_name = ""
        self._bot_persona = ""
        self._prompt_error: Optional[str] = None
        self._state: Optional[StateStore] = None
        self._state_lock = asyncio.Lock()
        self._pending_streams: Set[str] = set()
        self._tasks: Set[asyncio.Task[None]] = set()

    # ──── 生命周期 ────

    async def on_load(self) -> None:
        """加载插件：准备状态存储、解析目标账号、读取 bot 账号。"""

        self._state = StateStore(self.ctx.paths.data_dir)
        try:
            self._state.load()
        except StateError as exc:
            self.ctx.logger.error("状态文件读取失败，本次按空状态运行: %s", exc)
            self._state = StateStore(self.ctx.paths.data_dir)

        self._refresh_target()
        self._refresh_prompt_template()
        self._bot_account = await self._read_bot_account()
        await self._read_bot_profile()
        self._warn_if_misconfigured()
        self.ctx.logger.info(
            "接话插件已加载: enabled=%s target=%s bot=%s(%s) groups=%s presence=%s",
            self.config.plugin.enabled,
            self._target.spec if self._target else "(未配置)",
            self._bot_account or "(未知)",
            self._bot_name or "(未命名)",
            self.config.scope.groups,
            self.config.target.presence_policy,
        )

    async def on_unload(self) -> None:
        """卸载插件：取消后台任务并落盘状态。"""

        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        self._save_state()

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """处理配置热更新。"""

        del config_data, version
        if scope == "self":
            self._refresh_target()
            self._refresh_prompt_template()
            self._warn_if_misconfigured()
            self.ctx.logger.info("接话插件配置已更新: target=%s", self._target.spec if self._target else "(未配置)")
        elif scope == "bot":
            self._bot_account = await self._read_bot_account()
            await self._read_bot_profile()
            self.ctx.logger.info(
                "接话插件已刷新 bot 信息: account=%s name=%s",
                self._bot_account or "(未知)",
                self._bot_name or "(未命名)",
            )

    # ──── Hook ────

    @HookHandler(
        "chat.receive.after_process",
        name="target_replyer_watch",
        description="检测消息是否 @ 了配置的目标账号，命中则安排一次接话判断",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        timeout_ms=WATCH_HOOK_TIMEOUT_MS,
        error_policy=ErrorPolicy.SKIP,
    )
    async def watch_inbound(self, **kwargs: Any) -> None:
        """入站消息旁路观察：只做本地规则，命中后丢给后台任务。"""

        message = kwargs.get("message")
        if not isinstance(message, dict):
            return
        skip_reason = self._screen(message)
        if skip_reason:
            self.ctx.logger.debug("跳过消息 %s: %s", message.get("message_id"), skip_reason)
            return

        message_id = str(message.get("message_id") or "")
        task = asyncio.create_task(self._handle_candidate(dict(message)), name=f"target_replyer:{message_id}")
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    # ──── 规则预筛 ────

    def _screen(self, message: Dict[str, Any]) -> str:
        """返回跳过原因；返回空字符串表示可以进入后台判断。"""

        if not self.config.plugin.enabled:
            return "插件未启用"
        if self._target is None:
            return "target.user 未配置或格式不合法"
        if self._bot_account and self._target.user_id == self._bot_account:
            return "target.user 指向 bot 自己"
        if not is_group_message(message):
            return "不是群聊消息"

        message_platform = platform(message)
        # target.user 写的是 platform:user_id：平台必须对上，否则同号不同平台会被误触发
        if self._target.platform and message_platform != self._target.platform:
            return f"消息平台 {message_platform or '(空)'} 与目标账号平台 {self._target.platform} 不一致"
        platforms = [item.strip() for item in self.config.scope.platforms if item.strip()]
        if platforms and message_platform not in platforms:
            return f"平台 {message_platform or '(空)'} 不在生效范围"

        groups = [item.strip() for item in self.config.scope.groups if item.strip()]
        if not groups:
            return "scope.groups 为空，插件不生效"
        current_group = group_id(message)
        if current_group not in groups:
            return f"群 {current_group or '(空)'} 不在白名单"

        if bool(message.get("is_notify")):
            return "通知类消息"

        sender = sender_id(message)
        if sender == self._target.user_id:
            return "目标账号自己发的消息"
        bot_accounts = self._bot_accounts_for(message)
        if sender and sender in bot_accounts:
            return "bot 自己发的消息"

        if self._target.user_id not in at_target_ids(message):
            return "没有 @ 目标账号"
        for account in bot_accounts:
            if has_at(message, account):
                return "同时 @ 了 bot 自己，让主程序处理"

        text = message_text(message)
        if is_command_text(text):
            return "命令消息"
        if not text:
            return "没有文字内容"
        return ""

    # ──── 后台处理 ────

    async def _handle_candidate(self, message: Dict[str, Any]) -> None:
        """后台任务：检查去重 / 冷却 / 额度，确认目标不在场后投递主动任务。"""

        message_id = str(message.get("message_id") or "")
        stream_id = str(message.get("session_id") or "")
        if not message_id or not stream_id:
            self.ctx.logger.error("候选消息缺少 message_id 或 session_id，跳过: %r", message)
            return

        now = time.time()
        async with self._state_lock:
            if self._state is None:
                self.ctx.logger.error("状态存储尚未初始化，跳过消息: %s", message_id)
                return
            if stream_id in self._pending_streams:
                self.ctx.logger.info("同一聊天流已有待处理的接话任务，跳过 message_id=%s", message_id)
                return
            if self._state.is_processed(message_id, now):
                self.ctx.logger.debug("消息已处理过，跳过: %s", message_id)
                return
            self._state.mark_processed(message_id, now)
            remaining = self._state.cooldown_remaining(stream_id, now, self.config.reply.cooldown_seconds)
            if remaining > 0:
                self.ctx.logger.info("聊天流冷却中（还剩 %.0f 秒），跳过 message_id=%s", remaining, message_id)
                self._save_state()
                return
            hourly_count = self._state.hourly_count(now)
            if hourly_count >= self.config.reply.hourly_limit:
                self.ctx.logger.info(
                    "本小时已触发 %s 次，达到上限 %s，跳过 message_id=%s",
                    hourly_count,
                    self.config.reply.hourly_limit,
                    message_id,
                )
                self._save_state()
                return
            self._pending_streams.add(stream_id)
            self._save_state()

        try:
            await asyncio.sleep(self.config.reply.delay_seconds)
            recent = await self._load_recent(stream_id)
            if recent is None:
                return
            if not self._presence_allows(recent):
                return
            decision = await self._decide(message, recent)
            if decision is None:
                return
            await self._trigger_proactive(
                stream_id,
                message,
                answer_points=decision.answer_points,
                judge_reason=decision.reason,
            )
        finally:
            self._pending_streams.discard(stream_id)

    async def _load_recent(self, stream_id: str) -> Optional[List[Dict[str, Any]]]:
        """读取最近消息；失败时返回 None 并记录错误。"""

        recent = await self.ctx.message.get_recent(stream_id, limit=30)
        if not isinstance(recent, list):
            self.ctx.logger.error("读取最近消息失败，放弃本次接话: %r", recent)
            return None
        return [item for item in recent if isinstance(item, dict)]

    def _bot_accounts_for(self, message: Dict[str, Any]) -> Set[str]:
        """「这条消息的 bot 自己」可能的账号集合。

        两个来源都要算：消息自带的 `account_id`（宿主一定会带，但可能只是内部账号标识，
        不等于 QQ 号）与配置里的 `bot.qq_account`（适配器没上报身份时的备用值、默认空串）。
        只认其中一个会让「@bot 让路」「bot 说过话就让路」在另一种取值下静默失效。
        """

        accounts = {str(message.get("account_id") or "").strip(), self._bot_account}
        accounts.discard("")
        return accounts

    def _presence_allows(self, recent: List[Dict[str, Any]]) -> bool:
        """判断目标账号或 bot 自己是不是刚说过话。"""

        now = time.time()
        bot_recent = False
        target_recent = False
        for item in recent:
            user_id = sender_id(item)
            occurred_at = message_epoch(item)
            if occurred_at is None:
                continue
            # 每条消息各自带 account_id，用它判断"这条是不是 bot 说的"，比只看全局配置更准
            if user_id and user_id in self._bot_accounts_for(item) and now - occurred_at <= BOT_RECENT_WINDOW_SECONDS:
                bot_recent = True
                break
            if (
                self.config.target.presence_policy == "yield"
                and self._target is not None
                and user_id == self._target.user_id
                and now - occurred_at <= self.config.target.presence_window_seconds
            ):
                target_recent = True
                break

        if bot_recent:
            self.ctx.logger.info("bot 最近 %s 秒内已在本流发言，让路", BOT_RECENT_WINDOW_SECONDS)
            return False
        if target_recent:
            self.ctx.logger.info(
                "目标账号最近 %s 秒内已在本流发言，让路",
                self.config.target.presence_window_seconds,
            )
            return False
        return True

    def _target_spoke_seconds_ago(self, recent: List[Dict[str, Any]]) -> Optional[float]:
        """返回目标账号最近一次发言距现在多少秒；没说过返回 None。"""

        if self._target is None:
            return None
        now = time.time()
        seconds: Optional[float] = None
        for item in recent:
            if sender_id(item) != self._target.user_id:
                continue
            occurred_at = message_epoch(item)
            if occurred_at is None:
                continue
            delta = max(0.0, now - occurred_at)
            seconds = delta if seconds is None else min(seconds, delta)
        return seconds

    async def _decide(self, message: Dict[str, Any], recent: List[Dict[str, Any]]) -> Optional[JudgeDecision]:
        """跑 L1 接话预判；返回 None 表示这次不接。"""

        if self.config.decide.mode == "planner_only":
            return JudgeDecision(action="answer", confidence=1.0, reason="planner_only", answer_points="")

        try:
            decision = await self._call_judge(message, recent)
        except PromptTemplateError as exc:
            self.ctx.logger.error("提示词模板错误，放弃本次接话: %s", exc)
            return None
        except JudgeError as exc:
            self.ctx.logger.error("预判失败，放弃本次接话: %s", exc)
            return None
        except Exception as exc:
            self.ctx.logger.error("预判调用异常，放弃本次接话: %s", exc)
            return None

        if decision.action != "answer":
            self.ctx.logger.info(
                "预判判断不接: action=%s confidence=%.2f reason=%s",
                decision.action,
                decision.confidence,
                decision.reason,
            )
            return None
        if decision.confidence < self.config.decide.confidence_threshold:
            self.ctx.logger.info(
                "预判置信度不足: %.2f < %.2f reason=%s",
                decision.confidence,
                self.config.decide.confidence_threshold,
                decision.reason,
            )
            return None

        self.ctx.logger.info("预判判断可以接: confidence=%.2f reason=%s", decision.confidence, decision.reason)
        return decision

    async def _call_judge(self, message: Dict[str, Any], recent: List[Dict[str, Any]]) -> JudgeDecision:
        """调用便宜模型并解析结论。"""

        if self._prompt_error:
            raise PromptTemplateError(self._prompt_error)
        target = self._target
        if target is None:
            raise JudgeError("目标账号配置已失效")

        prompt = build_prompt(
            group_name=group_name(message),
            target_name=at_display_name(message, target.user_id),
            sender_name=sender_nickname(message) or sender_id(message),
            text=message_text(message)[: self.config.reply.max_text_chars],
            recent_messages=recent,
            current_message_id=str(message.get("message_id") or ""),
            target_spoke_seconds_ago=self._target_spoke_seconds_ago(recent),
            bot_name=self._bot_name,
            bot_persona=self._bot_persona,
            template=self.config.prompt.template,
        )
        result = await self.ctx.call_capability(
            "llm.generate",
            timeout_ms=self.config.decide.rpc_timeout_ms,
            prompt=prompt,
            task_name=self.config.decide.task_name,
            model_name=self.config.decide.model_name,
            temperature=self.config.decide.temperature,
            max_tokens=self.config.decide.max_tokens,
        )
        if not isinstance(result, dict) or not result.get("success"):
            raise JudgeError(f"LLM 调用失败: {result!r}")
        response = result.get("response")
        if not isinstance(response, str) or not response.strip():
            completion_tokens = result.get("completion_tokens")
            reasoning = result.get("reasoning")
            reasoning_chars = len(reasoning) if isinstance(reasoning, str) else 0
            raise JudgeError(
                "LLM 返回空 response"
                f"（completion_tokens={completion_tokens}，reasoning_chars={reasoning_chars}；"
                "检查 decide.max_tokens 是否被思考模型的 reasoning 吃光）"
            )
        return parse_decision(response)

    async def _trigger_proactive(
        self,
        stream_id: str,
        message: Dict[str, Any],
        *,
        answer_points: str = "",
        judge_reason: str = "",
    ) -> None:
        """投递 Maisaka 主动任务；会话尚未注册时按配置重试。"""

        target = self._target
        if target is None:
            self.ctx.logger.error("目标账号配置已失效，放弃投递")
            return

        text = message_text(message)[: self.config.reply.max_text_chars]
        intent = build_intent(
            sender_name=sender_nickname(message) or sender_id(message),
            target_name=at_display_name(message, target.user_id),
            group_name=group_name(message),
            text=text,
            bot_name=self._bot_name or "助手",
            answer_points=answer_points[: self.config.reply.max_text_chars],
        )
        metadata = {
            "trigger_message_id": str(message.get("message_id") or ""),
            "trigger_user_id": sender_id(message),
            "group_id": group_id(message),
            "judge_reason": judge_reason,
        }

        # 入场时的额度检查是 check-then-act：多个聊天流并发时都会通过，真正记账却在投递成功之后，
        # 上限会被打穿。投递前在锁里复核一次，超限就不投了。
        async with self._state_lock:
            if self._state is not None:
                used = self._state.hourly_count(time.time())
                if used >= self.config.reply.hourly_limit:
                    self.ctx.logger.info(
                        "投递前复核额度：本小时已触发 %s 次，达到上限 %s，放弃本次接话",
                        used,
                        self.config.reply.hourly_limit,
                    )
                    return

        for attempt in range(1, TRIGGER_MAX_ATTEMPTS + 1):
            try:
                result = await self.ctx.maisaka.proactive.trigger(
                    stream_id=stream_id,
                    intent=intent,
                    reason="群消息 @ 了目标账号",
                    metadata=metadata,
                )
            except Exception as exc:  # 单次 RPC 失败（超时/连接抖动）值得重试
                self.ctx.logger.error(
                    "主动任务投递异常（第 %s/%s 次）: %s", attempt, TRIGGER_MAX_ATTEMPTS, exc, exc_info=True
                )
                result = None

            if isinstance(result, dict) and result.get("success"):
                self.ctx.logger.info(
                    "已投递接话任务: stream_id=%s message_id=%s task_id=%s",
                    stream_id,
                    metadata["trigger_message_id"],
                    result.get("task_id"),
                )
                async with self._state_lock:
                    if self._state is not None:
                        self._state.record_trigger(stream_id, time.time())
                        self._save_state()
                return

            error_text = str(result.get("error") or "") if isinstance(result, dict) else repr(result)
            self.ctx.logger.error(
                "主动任务投递失败（第 %s/%s 次）: %s",
                attempt,
                TRIGGER_MAX_ATTEMPTS,
                error_text or repr(result),
            )
            if result is not None and not _is_transient_trigger_error(error_text):
                # 配置错/未授权这类问题重试三次也还是同一个结果，只会拖长这条消息的处理时间
                self.ctx.logger.error("这个错误不可重试，放弃本次接话: %s", error_text)
                return
            if attempt < TRIGGER_MAX_ATTEMPTS:
                await asyncio.sleep(self.config.reply.retry_delay_seconds)

    # ──── 内部工具 ────

    def _refresh_target(self) -> None:
        """按当前配置重新解析目标账号。"""

        spec = self.config.target.user.strip()
        target = parse_target_spec(spec)
        if spec and target is None:
            self.ctx.logger.error("target.user 格式不合法，应为 platform:user_id，例如 qq:123456789；当前值=%r", spec)
        self._target = target

    def _refresh_prompt_template(self) -> None:
        """校验提示词模板；配置改坏时保留错误，预判阶段直接失败而不是静默换默认模板。"""

        self._prompt_error = None
        try:
            validate_prompt_template(self.config.prompt.template)
        except PromptTemplateError as exc:
            self._prompt_error = str(exc)
            self.ctx.logger.error("提示词模板配置错误，预判将一直失败: %s", exc)

    async def _read_bot_account(self) -> str:
        """读取宿主配置里的 bot QQ 号，作为「消息自带 account_id」取不到时的兜底。

        空值不能静默：`bot.qq_account` 在宿主里只是「适配器没上报身份时的备用账号」、默认空串，
        一旦为空，「同时 @ 了 bot」「bot 自己发的消息」「bot 最近说过话就让路」三条规则会全部失效，
        而这正是插件唯一的防重复回复机制。
        """

        try:
            value = await self.ctx.config.get("bot.qq_account", "")
        except Exception as exc:
            self.ctx.logger.error("读取 bot.qq_account 失败，bot 身份只能依赖消息自带的 account_id: %s", exc)
            return ""
        if not isinstance(value, str):
            self.ctx.logger.error("bot.qq_account 不是字符串，bot 身份只能依赖消息自带的 account_id: %r", value)
            return ""
        account = value.strip()
        if not account:
            self.ctx.logger.error(
                "未能从配置确定 bot 账号（bot.qq_account 为空）：bot 身份将改由每条消息自带的 account_id 提供；"
                "如果你的适配器不上报 account_id，@bot 让路与「bot 刚说过话」让路都不会生效，"
                "请在 bot_config.toml 的 bot.qq_account 里填上 bot 的账号"
            )
        return account

    async def _read_bot_profile(self) -> None:
        """读取宿主配置里的 bot 昵称与人格，供提示词注入；不写死具体人设。"""

        try:
            nickname = await self.ctx.config.get("bot.nickname", "")
        except Exception as exc:
            self.ctx.logger.warning("读取 bot.nickname 失败: %s", exc)
            nickname = ""
        try:
            persona = await self.ctx.config.get("personality.personality", "")
        except Exception as exc:
            self.ctx.logger.warning("读取 personality.personality 失败: %s", exc)
            persona = ""

        self._bot_name = nickname.strip() if isinstance(nickname, str) else ""
        self._bot_persona = persona.strip() if isinstance(persona, str) else ""
        if not self._bot_name:
            self.ctx.logger.warning("bot.nickname 为空，预判提示词会退回中性称呼「助手」")

    def _warn_if_misconfigured(self) -> None:
        """配置不完整时给出明确提示，避免误以为插件已经生效。"""

        if self._target is None:
            self.ctx.logger.warning("target.user 未配置，插件不会触发")
            return
        if self._bot_account and self._target.user_id == self._bot_account:
            self.ctx.logger.error("target.user 指向 bot 自己，插件不会触发")
            return
        if not [item.strip() for item in self.config.scope.groups if item.strip()]:
            self.ctx.logger.warning("scope.groups 为空，插件不会生效")
            return
        if not self.config.plugin.enabled:
            self.ctx.logger.info("插件当前未启用（plugin.enabled=false）")

    def _save_state(self) -> None:
        """保存状态；失败只记录，不打断已经发生的投递。"""

        if self._state is None:
            return
        try:
            self._state.save()
        except StateError as exc:
            self.ctx.logger.error("状态保存失败: %s", exc)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        """后台任务结束回调：移出集合并记录未处理异常。"""

        self._tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            self.ctx.logger.error("后台接话任务异常: %s", exception)

    async def wait_pending(self) -> None:
        """等待所有后台任务结束；测试和优雅停机时使用。"""

        while self._tasks:
            await asyncio.gather(*tuple(self._tasks))


def create_plugin() -> TargetReplyerPlugin:
    """创建插件实例。"""

    return TargetReplyerPlugin()
