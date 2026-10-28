# 替主人回话（Target Replyer）

<p align="center">
  <img src="icon.png" width="180" alt="插件图标">
</p>

<p>
  <a href="https://github.com/42yoru/maimai-target-replyer/actions/workflows/ci.yml"><img src="https://github.com/42yoru/maimai-target-replyer/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <a href="https://github.com/Mai-with-u/MaiBot"><img src="https://img.shields.io/badge/MaiBot-1.3.9%2B-orange.svg" alt="MaiBot"></a>
  <a href="https://github.com/Mai-with-u/maibot-plugin-sdk"><img src="https://img.shields.io/badge/SDK-2.16.0%2B-green.svg" alt="SDK"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python">
</p>

群里有人 **@ 目标账号** 时，让 bot 自己判断「这话要不要由自己自然接一句」，该接就接，不该接就安静。

> **先回答三个常见问题**
>
> - **它解决什么？** 目标账号（比如主人）被 @、但他本人没回应时，bot 可以自然地接住问题，而不是让消息一直空着。
> - **会冒充目标账号吗？** 不会。输出永远以 bot 自己的身份发出，不模仿目标账号，也不说「他不在，我替他答」。
> - **要改主程序吗？** 不用。纯插件，用的是宿主已有的入站 Hook、`llm.generate` 和 `maisaka.proactive.trigger`。

## 目录

- [先看效果](#先看效果) · [五分钟上手](#五分钟上手)
- [配置](#配置) · [她怎么工作](#她怎么工作) · [什么时候会让路](#什么时候会让路)
- [已知边界](#已知边界) · [文件结构](#文件结构) · [测试](#测试)

## 先看效果

群里有人问：

```
群友：@目标账号 那 PayPal 的验证只是用来提升收款付款额度的吗
```

预判先判断「这是事实性问题，最近聊天里有相关上下文，可以接」，然后 bot 回：

```
bot：有一说一，PayPal 验证可不只是提额啦，也是确认你身份、银行卡和地址，
     不验证的话提现收款都可能受限，账户安全和合规都靠它。
```

语气来自你自己配置的 bot 人设；插件只负责「接不接」，不负责「怎么说」。

## 五分钟上手

### 第 1 步：放进插件目录

把整个目录放到 MaiBot 的 `plugins/` 下，保持这样：

```
plugins/maimai_target_replyer/
  _manifest.json
  plugin.py
  judge.py
  rules.py
  state.py
  config.toml          # 首次加载后由宿主生成
```

### 第 2 步：改两个值

在 WebUI 的插件配置页改，或者编辑宿主生成的 `config.toml`：

```toml
[target]
user = "qq:123456789"        # 被 @ 的目标账号

[scope]
groups = ["123456789"]       # 只在这个群生效
```

> `groups` 留空 = 插件完全不生效。这是故意的：避免刚装上就在所有群说话。
> 填好之后记得把 `plugin.enabled` 打开。

### 第 3 步：重载或重启

宿主支持插件源码热重载，保存文件会自动重新加载；也可以在插件管理里手动重载。加载后日志里应该能看到：

```
接话插件已加载: enabled=True target=qq:123456789 bot=... groups=['123456789'] presence=yield
```

之后在群里 @ 一次目标账号，日志会依次出现预判和投递结果：

```
预判判断可以接: confidence=0.88 reason=...
已投递接话任务: stream_id=... message_id=... task_id=...
```

## 配置

所有字段的详细注释都在 `config.toml` 里；WebUI 的插件配置页已经把分节和字段名都汉化了，分成「基础 / 预判与节奏 / 高级」三页签，`enabled` 显示为「启用插件」，鼠标移上去还有一句中文提示。这里只列最常改的：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `plugin.enabled` | `false` | 总开关；关闭时宿主不会加载本插件，填好目标账号和群之后再打开，改完需要重启 |
| `target.user` | 空 | 被 @ 的目标账号，`platform:user_id`，只支持一个 |
| `target.presence_policy` | `yield` | `yield`=目标最近发言就让路；`engage`=他在线也照常判断 |
| `target.presence_window_seconds` | `180` | 判断「最近发言」的时间窗 |
| `scope.platforms` | `["qq"]` | 生效平台 |
| `scope.groups` | `[]` | 生效群；空 = 不生效 |
| `decide.mode` | `cheap_then_planner` | 预判模式；`planner_only` 可回到「直接触发」 |
| `decide.task_name` | `utils` | 预判模型任务，便宜的小模型 |
| `decide.max_tokens` | `2000` | 要留出思考模型的 reasoning 余量 |
| `decide.confidence_threshold` | `0.6` | 低于该值不接 |
| `prompt.template` | 内置模板 | 预判提示词；占位符见下，`$text` 和 `$recent` 必填 |
| `reply.cooldown_seconds` | `180` | 同一群两次接话的最短间隔 |
| `reply.hourly_limit` | `6` | 每小时最多触发几次 |

### 提示词

默认模板里的 bot 名字和人格不是写死的：插件会读取宿主的 `bot.nickname` 和
`personality.personality`，替换模板里的 `$bot_name` / `$bot_persona`。

提示词在 WebUI 的「高级 → 提示词」里改，也可以直接改 `config.toml` 的 `[prompt] template`。
模板用 `$` 占位符，JSON 示例里的 `{` `}` 不需要转义：

| 占位符 | 内容 |
| --- | --- |
| `$bot_name` | bot 昵称（宿主 `bot.nickname`） |
| `$bot_persona` | bot 人格（宿主 `personality.personality`） |
| `$place` | `群「群名」` 或 `群里` |
| `$group_name` | 群名，取不到时为「未知」 |
| `$target_name` | 被 @ 的目标账号显示名 |
| `$sender_name` | 发消息的人 |
| `$text` | 被 @ 的原消息（必填） |
| `$target_state` | 目标账号最近有没有发言 |
| `$recent` | 最近 20 条聊天记录（必填） |

模板为空、缺少 `$text` / `$recent`、或写了未知占位符时，加载和热更新会直接报错，预判阶段按「不接」处理，不会静默换回默认模板。

> 预判会把当前消息和最近 20 条聊天记录（含昵称、群名）发给 `decide.task_name` 指定的模型。
> 默认的 `utils` 是你自己配置的模型；如果把 `decide.task_name` 指到外部 API，等于把这些群聊内容交给对应服务商，请按自己的隐私要求选择。

## 她怎么工作

```
群消息入站
 └─ chat.receive.after_process（observe Hook，只做本地规则，立刻返回）
     │  群聊 / 群白名单 / at 段命中 target / 非 @全体 / 非 @bot 自己
     │  非命令 / 非自己人 / 消息去重 / 每群冷却 / 每小时额度
     ▼
  后台任务（延迟 1.5 秒，等主链注册会话）
     │  读最近 30 条：目标账号最近发言（yield 模式）或 bot 刚回过 → 让路
     ├─ L1 预判（小模型，一次 JSON）
     │    answer / ignore / defer + confidence + reason + answer_points
     └─ maisaka.proactive.trigger（任务书 + 预判要点）
          由 Planner 带着人格、记忆和上下文决定怎么接；它也可以 stop()
```

几个刻意的设计：

- **预判只判断，不写回复正文。** 真正怎么说交给 Planner，人格、表达习惯、引用、表情都不会丢。
- **预判失败 = 不接。** LLM 超时、返回空、JSON 不合法都按「不接」处理并记日志，不猜、不硬答。
- **失败不静默。** Hook 异常默认会被宿主吞掉，所以规则层自己 try/except 并写日志；预判失败会打印 `completion_tokens` 和 `reasoning_chars`，方便判断是不是被 `max_tokens` 截断。
- **同一聊天流串行。** 宿主 runtime 的主动任务只有单槽，连发会互相覆盖；插件用 `_pending_streams` 保证同一流同时只有一个待处理任务。

## 什么时候会让路

| 情况 | 结果 |
| --- | --- |
| 目标账号最近 180 秒内发过言 | `yield` 模式下不接，让他自己答 |
| bot 最近 40 秒内在本流发过言 | 不接，避免连着刷屏 |
| 同一条消息同时 @ 了 bot 自己 | 不接，交给主程序处理 |
| 发言的是目标账号本人或 bot 自己 | 不接 |
| 消息是命令、通知、纯 @ 没有文字 | 不接 |
| 目标账号不在 `scope.groups` 白名单的群里 | 不处理 |
| 预判返回 `ignore` / `defer` / 置信度不足 | 不接 |

## 已知边界

- **只支持一个目标账号。** 替多个账号回话的语义不清晰，v1 不做。
- **只处理群聊。** 私聊里每句话本来就是对 bot 说的，不需要代答。
- **不会以目标账号的身份发言。** 出站消息永远是 bot 账号，插件能做的是「bot 自然接话」，不是「冒充某个人」。
- **不做命令和面板。** 插件依赖「有人 @ 目标账号」这一个入口；开关走 `plugin.enabled`（关闭＝宿主不加载本插件），范围走 `scope.groups`。
- **主动任务仍然要跑一轮 Planner。** 预判只能挡掉不值得的触发，不能替代表达链路；单轮成本约 1.5k 输入（预判）+ 一整轮 Planner（仅放行时）。

## 文件结构

```
maimai_target_replyer/
  plugin.py          # 插件类、Hook、配置模型、后台任务
  rules.py           # L0 纯函数：target 解析、at 段、文本规则、intent 拼装
  judge.py           # L1 预判：prompt、最近消息格式化、严格 JSON 解析
  state.py           # 去重 / 冷却 / 小时额度，JSON 原子写
  config.toml        # 运行时配置（宿主生成，不进仓库）
  config_ui.py       # WebUI 配置页的中文标签与页签布局
  _manifest.json     # 插件清单 v2
  tests/             # 单测与集成测试
```

## 测试

```bash
cd <MaiBot 根目录>
.venv/Scripts/python.exe -m pytest plugins/maimai_target_replyer/tests -q
```

覆盖：target 解析、at 段匹配、@全体、命令 / 通知 / 自己人排除、去重、冷却、小时额度、
两种 presence 模式、预判放行 / 不接 / 低置信度 / 非法 JSON / LLM 失败、会话未就绪重试、状态持久化。

## 许可

MIT
