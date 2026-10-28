# 更新日志

## 0.3.3

### 修复

- **bot 身份取不到时所有「让路」保护静默失效**：`bot.qq_account` 在宿主里只是「适配器没上报身份时的
  备用账号」、默认空串。一旦为空，「同时 @ 了 bot」「bot 自己发的消息」「bot 最近 40 秒说过话就让路」
  三条规则会被 `if self._bot_account and ...` 全部跳过，而这是插件唯一的防重复回复/防刷屏机制。
  现在把每条消息自带的 `account_id` 也算作 bot 身份（两个来源取并集），并在配置为空时
  `logger.error` 明确指出「让路规则将不生效」而不是无声降级

### 细节

- 测试 74 → 76：新增「`bot.qq_account` 为空时靠 `account_id` 认出 bot 自己 / 认出 @ 了 bot」两条
- 顺带把识别 bot 的逻辑收进 `_bot_accounts_for(message)`，`_screen` 与 `_presence_allows` 共用

## 0.3.2

### 细节

- **`plugin.enabled` 的说明与宿主行为对齐**：关闭时宿主在加载阶段就跳过本插件（`plugin_enabled.py`），
  根本不会导入插件代码、Hook 也收不到消息。原来 README 与配置提示写的是「关掉后 Hook 仍会收到消息，
  但一律不处理」，与实际不符，现在统一成「关闭＝宿主不加载，改完需要重启」
- manifest：`sdk` 区间由 `2.16.0–2.16.99` 改为 `2.12.0–2.99.99`——`ctx.config.get`、
  `ctx.message.get_recent`、`ctx.maisaka.proactive.trigger`、`call_capability` 在 2.12.0 与 2.19.0 上签名一致

## 0.3.1

### 细节

- 兼容毫秒 / 微秒级消息时间戳，避免「目标账号最近发言」的让路规则因量级差 1000 倍而静默失效。
- README 与配置注释补充说明：预判会把当前消息和最近聊天记录发送给 `decide.task_name` 指定的模型。

## 0.3.0

### 主要功能

- 新增轻量预判：先用便宜小模型判断要不要自然接话，通过后才触发 Planner，减少无意义触发。
- 预判提示词可在 WebUI「高级 → 提示词」中修改，支持 `$bot_name`、`$bot_persona`、`$text`、`$recent` 等占位符。
- WebUI 配置页汉化，分成「基础 / 预判与节奏 / 高级」三个页签，字段名和提示均为中文。

### 细节

- 去个性化：插件不写死 bot 名称与人格，默认提示词从宿主 `bot.nickname` / `personality.personality` 读取。
- 新增插件图标；补充 GitHub Actions CI（Python 3.11 / 3.12 / 3.13）。
- 状态文件损坏或时间戳非法时明确报错，不再让插件加载失败。
- 提示词模板在加载/热更新时校验未知占位符、缺失必填项和非法 `$` 写法。
- `config.toml` 由宿主生成，不进仓库。
