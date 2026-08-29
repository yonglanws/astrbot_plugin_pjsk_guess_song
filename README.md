# astrbot_plugin_pjsk_guess_song

《初音未来 缤纷舞台》（Project SEKAI）**猜歌**娱乐插件。使用游戏音频资源制作的听歌猜曲游戏，提供普通猜歌、2倍速、倒放以及随机组合效果等多种模式。

> 原作者停止维护，这是后续维护分支。

## 特性

- 🎵 **丰富音频效果**：普通模式、2倍速、倒放、随机组合效果等多种模式，难度越高得分越多
- 🔄 **题库自动同步**：歌曲题库自 Haruki master（`musics.json` + `musicVocals.json`）+ 中文翻译每 24 小时自动同步，新歌随游戏版本更新自动入库，版本未变跳过大文件下载
- 🌐 **多服务器题库**：支持日服 / 国服题库自由切换，按群独立记忆
- 🏆 **精美数据面板**：内置积分排行榜（Pillow 本地渲染横向表格，与猜卡面同款视觉规范，支持自定义名称、未绑定 QQ 徽章）、个人战绩查询、每日次数限制与冷却
- 🤖 **QQ 官方机器人支持**：官机 markdown 渲染，开局附操作连接，结算附切换题库/绑定/查分/排行榜连接；快捷入口由 `quick_entries` 配置控制（默认关闭）；支持绑定普通 QQ 迁移分数
- ⚡ **双模式退出**：`仅退出本局` 与 `退出自动模式` 严格分离，自动模式精简无扰
- ⚙️ **分群独立配置**：支持通过 `group_settings.json` 设置群专属禁用时段、独立每日次数等高级策略

## 环境依赖

本插件的核心音频处理功能依赖于 `ffmpeg`，请确保系统环境已正确安装 `ffmpeg`。

- **Docker 用户**：若容器内缺失 ffmpeg，需手动进入容器安装：
  ```bash
  docker exec -it <容器名或ID> /bin/bash
  apt-get update && apt-get install -y ffmpeg
  ```

## 指令

### 游戏指令

| 指令 | 别名 | 说明 |
| --- | --- | --- |
| `pjsk猜歌` / `猜歌` | - | 普通模式猜歌（1分） |
| `pjsk猜歌1` / `猜歌1` | `2倍速猜歌`、`二倍速猜歌` | 2倍速模式（1分） |
| `pjsk猜歌2` / `猜歌2` | `倒放猜歌` | 倒放模式（1分） |
| `随机猜歌` | `rgs` | 核心玩法：随机组合多种效果，最高3分 |
| `自动猜歌` | `连续猜歌`、`autogs` | 自动模式：每局结束后自动开下一局（可加 `随机`/`1`/`2` 指定模式） |

### 题库与账号

| 指令 | 说明 |
| --- | --- |
| `猜歌切换日服题库` / `猜歌切换国服题库` | 切换本群题库服务器（下一局生效） |
| `猜歌绑定 QQ号` | 将 QQ 官方机器人账号绑定到普通 QQ 号，分数自动迁移（需发送"确认"） |

### 数据与帮助

| 指令 | 别名 | 说明 |
| --- | --- | --- |
| `群猜歌排行榜` | `gssrank`、`gstop` | 查看本群猜歌排行榜 |
| `猜歌排行榜` | `本地猜歌排行榜` | 查看本地数据库中的总排行榜 |
| `猜歌分数` | `gsscore`、`我的猜歌分数`、`猜歌个人分数` | 查看自己的积分、正确率和排名统计 |
| `查看统计` | `mode_stats`、`题型统计` | 查看各游戏模式的正确率统计图 |
| `猜歌帮助` | - | 显示帮助图 |

### 管理员指令

| 指令 | 别名 | 说明 |
| --- | --- | --- |
| `重置猜歌次数 [用户ID]` | `resetgs` | 重置指定用户的每日猜歌次数 |
| `重置题型统计` | `resetmodestats` | 清空本地所有题型的统计数据 |
| `同步分数` | `syncscore`、`migrategs` | 将本地总分强制同步至远程服务器 |

### 退出机制说明

- **`仅退出本局`**：仅在游玩中生效，立即结束当前对局并公布答案（自动模式继续开下一局）。
- **`退出自动模式`**（别名：`退出`）：任何时候可触发，停止自动续局（当前对局继续打完）。
- **自动模式精简**：自动模式期间全程不出现 markdown 连接按钮，结算只显示结果、歌名与下一局提示，退出自动模式后恢复完整结算面板。

## 配置说明

通过 AstrBot WebUI 界面进行配置：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `default_server` | string | `jp` | 默认题库服务器（`jp`=日服 / `sc`=国服） |
| `update_interval_hours` | int | `24` | master 题库自动更新间隔（小时） |
| `connect_link_template` | string | （官方标签） | QQ 官方机器人结算连接的 markdown 模板 |
| `quick_entries` | list | `[]` | 结算快捷入口列表（若为空则不显示快捷入口） |
| `answer_timeout` | int | `30` | 答题超时时间（秒） |
| `daily_play_limit` | int | `15` | 每日游戏次数上限 |
| `game_cooldown_seconds` | int | `60` | 游戏冷却时间（秒） |
| `max_guess_attempts` | int | `10` | 每轮最大尝试回答次数上限 |
| `clip_duration_seconds` | int | `10` | 播放的音频片段时长（秒） |
| `bonus_time_after_first_answer` | int | `5` | 首位答对后的奖励有效时间（秒，0 为禁用） |
| `end_game_after_bonus_time` | bool | `true` | 是否在奖励时间结束后立即结束游戏 |
| `ranking_row_limit` | int | `10` | 排行榜显示人数 |
| `jp_resource_url_base` | string | `https://storage.exmeaning.com/sekai-jp-assets` | 日服音频/封面资源根地址 |
| `sc_resource_url_base` | string | `https://storage.exmeaning.com/sekai-sc-assets` | 国服音频/封面资源根地址 |
| `remote_resource_url_base` | string | （兼容旧配置） | 旧版日服资源地址迁移兜底，不建议新配置使用 |
| `stats_server_api_key` | string | `""` | 统计服务器 API 密钥（留空禁用在线同步） |
| `debug_mode` | bool | `false` | 调试模式（立即显示答案） |
| `group_whitelist` | list | `[]` | 群聊白名单（为空则所有群可用） |
| `whitelist_reject_message`| string | （提示语） | 非白名单群聊提示语（留空不提示） |
| `super_users` | list | `[]` | 管理员用户 ID 列表 |

### 群聊特定配置 (group_settings.json)

在插件数据目录下的 `group_settings.json` 可为群聊设置独立规则（如独立每日次数、禁用时段）：

```json
{
  "12345678": {
    "daily_play_limit": 100,
    "independent_daily_limit": true,
    "disable_guess_song_periods": [
      { "start": "02:00", "end": "07:00", "message": "现在是深夜休息时间哦 (02:00-07:00)" }
    ]
  }
}
```

## 资源

- 日服master：[Team-Haruki/haruki-sekai-master](https://github.com/Team-Haruki/haruki-sekai-master)
- 国服master：[Team-Haruki/haruki-sekai-sc-master](https://github.com/Team-Haruki/haruki-sekai-sc-master)
- 中文译名：`translation.exmeaning.com`（Moesekai 翻译源）
- 日服音频与封面资源：`https://storage.exmeaning.com/sekai-jp-assets`
- 国服音频与封面资源：`https://storage.exmeaning.com/sekai-sc-assets`

优先走 GitHub Contents API，失败时回退 jsDelivr CDN。数据持久化于 `data/plugin_data/pjsk_guess_song/`。

## 依赖

`Pillow`、`pilmoji`、`pydub`、`aiohttp`、`aiosqlite`。图片全部使用 Pillow 本地渲染。

## 致谢

本项目二改自 [nichinichisou0609/astrbot_plugin_pjsk_guess_song](https://github.com/nichinichisou0609/astrbot_plugin_pjsk_guess_song)。在此致谢。

完整更新历史见 [CHANGELOG.md](CHANGELOG.md)。
