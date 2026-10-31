# 替主人回话（Target Replyer）

<p>
  <a href="https://github.com/42yoru/maimai-target-replyer/actions/workflows/ci.yml"><img src="https://github.com/42yoru/maimai-target-replyer/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <a href="https://github.com/Mai-with-u/MaiBot"><img src="https://img.shields.io/badge/MaiBot-1.3.9%2B-orange.svg" alt="MaiBot"></a>
  <a href="https://github.com/Mai-with-u/maibot-plugin-sdk"><img src="https://img.shields.io/badge/SDK-2.12%2B-green.svg" alt="SDK"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python">
</p>

> **一句话定位：** 群里有人 @ 指定目标账号时，先用规则和便宜模型判断是否值得接话，再由 bot 自己的 Planner 决定如何自然回复。

## 适用与不适用

**适用：**

- 目标账号被 @、但本人没有回应时，希望 bot 偶尔接住事实性问题或简单话题；
- 需要按群、平台、冷却时间和每小时额度限制触发；
- 希望回复继续使用宿主 bot 的昵称、人格、记忆和表达链路。

**不适用：**

- 不会以目标账号身份发言，也不会模仿目标账号；最终消息永远由 bot 自己发送；
- 只处理群聊中 @ 目标账号的文字消息，不处理私聊、命令、通知、纯 @ 或同时 @ bot 的消息；
- 只支持一个目标账号，不提供独立命令面板。

## 快速安装

```bash
cd MaiBot/plugins
git clone https://github.com/42yoru/maimai-target-replyer.git
```

在 WebUI 或宿主生成的 `config.toml` 中完成配置后，重启 MaiBot 或触发插件热重载。

## 最小配置

至少配置目标账号和生效群：

```toml
[plugin]
enabled = true

[target]
user = "qq:123456789"

[scope]
platforms = ["qq"]
groups = ["123456789"]
```

`scope.groups=[]` 时插件不会处理任何群消息，这是默认的安全范围。插件加载后日志应包含目标账号、平台和生效群。

## 工作流程

1. `chat.receive.after_process` observe hook 先做本地筛选：群聊、平台、群白名单、@ 目标、非命令/通知/自己人、去重、冷却和小时额度。
2. 命中后创建后台任务，默认延迟约 1.5 秒，等待宿主注册聊天流。
3. 读取最近消息，若目标账号或 bot 最近已经发言，则按 presence 策略让路。
4. `cheap_then_planner` 模式调用 `llm.generate` 做 L1 预判，只接受 `answer / ignore / defer`、置信度和回答要点；预判失败或置信度不足时不接话。`planner_only` 会跳过预判，直接触发主动任务。
5. 通过 `maisaka.proactive.trigger` 投递任务书和预判要点，由宿主 Planner 结合 bot 人格与上下文决定是否回复、怎么回复；Planner 也可以 `stop()`。

同一聊天流同时只保留一个待处理任务。插件还限制同一群冷却时间和全局每小时触发数，以减少刷屏和模型成本。

## 配置表

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `plugin.enabled` | `false` | 插件总开关 |
| `target.user` | 空 | 目标账号，格式 `platform:user_id` |
| `target.presence_policy` | `yield` | `yield`：目标最近发言就让路；`engage`：仍然判断 |
| `target.presence_window_seconds` | `180` | 目标最近发言的让路窗口 |
| `scope.platforms` | `["qq"]` | 生效平台 |
| `scope.groups` | `[]` | 生效群，空值表示不生效 |
| `decide.mode` | `cheap_then_planner` | 预判模式，或使用 `planner_only` |
| `decide.task_name` | `utils` | L1 预判模型任务 |
| `decide.max_tokens` | `2000` | 预判输出上限，需要给 reasoning 留余量 |
| `decide.confidence_threshold` | `0.6` | 放行所需的最低置信度 |
| `reply.cooldown_seconds` | `180` | 同一群两次接话的最短间隔 |
| `reply.hourly_limit` | `6` | 每小时最多触发次数 |
| `reply.delay_seconds` | `1.5` | 触发前等待时间 |
| `prompt.template` | 内置模板 | L1 预判模板，必须保留 `$text` 和 `$recent` |

### 提示词占位符

模板可在 WebUI“高级 → 提示词”中修改，使用 `$` 占位符：

| 占位符 | 内容 |
| --- | --- |
| `$bot_name` / `$bot_persona` | 宿主 `bot.nickname` / `personality.personality` |
| `$place` / `$group_name` | 群位置和群名 |
| `$target_name` / `$sender_name` | 目标账号显示名和发言者 |
| `$text` | 当前被 @ 的原消息（必填） |
| `$target_state` | 目标最近是否发言 |
| `$recent` | 最近聊天记录（必填） |

模板为空、缺少必填占位符或含未知占位符时，加载/热更新会记录错误，预判阶段按“不接”处理，不静默换回默认模板。

## 边界与隐私

- 预判会把当前消息和最近聊天记录（包含昵称、群名）发送给 `decide.task_name` 指定的模型。若该任务指向外部 API，相关群聊内容会交给对应服务商；请按隐私要求选择模型，并自行控制提示词中的数据范围。
- 预判只判断是否接话，不生成最终回复；真正出站消息仍由 bot 自己的 Planner 和账号发送。
- 结构化状态保存在宿主分配的插件数据目录，用于消息去重、每群冷却和小时额度；状态文件使用原子写入。
- `processed_plain_text` 和原始消息中的 at 段由规则层共同参与判断；消息过滤依赖宿主提供的标准化消息结构。
- 主动任务仍需运行一轮 Planner，单次放行成本约为一次预判模型调用加一整轮 Planner。

## 测试

在插件目录运行：

```bash
python -m pytest tests -q
```

测试覆盖目标解析、@ 段匹配、@全体、命令/通知/自己人排除、去重、冷却、小时额度、presence 策略、预判解析、LLM 失败、会话未就绪重试和状态持久化。

## 插件信息

| 项目 | 值 |
| --- | --- |
| 插件 ID | `github.42yoru.maimai-target-replyer` |
| 版本 | `0.3.10` |
| 仓库 | <https://github.com/42yoru/maimai-target-replyer> |
| 许可证 | MIT |
