# PJSK猜歌插件使用说明

原作者停止维护，这是由本人维护的猜歌插件后续分支

由于原作者关停了服务器，如果你是从原插件迁移而来并保留了配置，请将 **远程资源服务器的URL** 替换成 **https://storage.exmeaning.com/sekai-jp-assets** ，否则将无法获取资源文件

## 1. 插件功能简介

本插件是使用《プロジェクトセカイ カラフルステージ！ feat. 初音ミク》/《世界计划 多彩舞台》/《初音未来 缤纷舞台》a.k.a. PJSK的音频资源制作的听歌猜曲游戏。提供普通猜歌、2倍速、倒放以及随机组合效果等多种模式。

插件内置了积分排行榜、每日游戏次数限制、游戏冷却等功能。可通过API与远程服务器同步玩家分数和游戏数据。

## 2. 环境依赖

本插件的核心音频处理功能（如裁剪、变速、倒放等）依赖于 `ffmpeg`。请确保您的系统环境已正确安装 `ffmpeg`。

- **对于 Docker 用户**：官方提供的 `AstrBot` Docker 镜像**不包含或无法使用** `ffmpeg`。您需要手动进入正在运行的容器并执行以下指令进行安装：
  ```bash
  # 进入容器
  docker exec -it <你的容器名或ID> /bin/bash
  # 创建镜像源文件
  在系统根目录下创建debian.sources文件，推荐使用清华镜像源（启用源码源）
  # APT换源
  docker cp /debian.sources <你的容器名或ID>:/etc/apt/sources.list.d/
  # 在容器内安装 ffmpeg (以Debian/Ubuntu为例)
  apt-get update && apt-get install -y ffmpeg
  ```

## 3. 指令列表

### 游戏指令

- `pjsk猜歌` / `猜歌`: 开始一轮普通模式的猜歌游戏 (1分)。
- `pjsk猜歌1` / `猜歌1` / `2倍速猜歌` / `二倍速猜歌`: **2倍速**模式 (1分)。
- `pjsk猜歌2` / `猜歌2` / `倒放猜歌`: **倒放**模式 (1分)。
- `随机猜歌` / `rgs`: **核心玩法**。随机组合多种效果，最高3分。

### 数据与帮助

- `猜歌帮助`: 显示帮助信息。
- `猜歌分数` / `gsscore`: 查看自己的积分、正确率和排名统计。
- `群猜歌排行榜` / `gssrank` / `gstop`: 查看**本群**的猜歌排行榜。
- `猜歌排行榜` / `本地猜歌排行榜`: 查看**本地数据库**中的总排行榜。
- `查看统计` / `mode_stats` / `题型统计`: 查看各游戏模式的正确率统计。

### 管理员指令

- `重置猜歌次数 [用户ID]` / `resetgs`: 重置指定用户的每日猜歌次数。
- `重置题型统计` / `resetmodestats`: 清空本地所有题型的统计数据。
- `同步分数` / `syncscore` / `migrategs`: 将本地分数强制同步至远程服务器。

## 4. 插件配置说明

插件的配置由机器人管理员通过 AstrBot 框架提供的 **WebUI 界面**进行修改。以下是可配置的选项说明：

```json
{
  "group_whitelist": [],
  "super_users": [],
  "answer_timeout": 30,
  "game_cooldown_seconds": 60,
  "daily_play_limit": 15,
  "max_guess_attempts": 10,
  "clip_duration_seconds": 10,
  "bonus_time_after_first_answer": 5,
  "end_game_after_bonus_time": true,
  "debug_mode": false,
  "ranking_row_limit": 10,
  "remote_resource_url_base": "https://storage.exmeaning.com/sekai-jp-assets",
  "stats_server_api_key": ""
}
```

- `group_whitelist` (列表): **群聊白名单**。只有在此列表中的群号才能使用本插件。若列表为空 `[]`，则对所有群聊生效。
- `super_users` (列表): **管理员QQ号列表**。
- `answer_timeout` (整数): 游戏回答的**超时时间**（秒）。
- `game_cooldown_seconds` (整数): 游戏结束后的**冷却时间**（秒）。
- `daily_play_limit` (整数): 每个用户每天可发起游戏的最大次数。
- `max_guess_attempts` (整数): 每轮游戏中，所有玩家总共可以**尝试回答**的次数。
- `clip_duration_seconds` (整数): 播放的音频**片段时长**（秒）。
- `bonus_time_after_first_answer` (整数): 首位答对者出现后，其他玩家可继续得分的**奖励时间**（秒）。
- `end_game_after_bonus_time` (布尔值): 是否在奖励时间结束后**立即结束游戏**。
- `debug_mode` (布尔值): **调试模式**。启用后，游戏会立即显示答案，适合测试。
- `ranking_row_limit` (整数): **排行榜**最多显示的行数，默认10行。
- `remote_resource_url_base` (字符串): **远程资源URL**，插件从此地址获取音频和封面资源。
- `stats_server_api_key` (字符串): **统计服务器API密钥**。用于连接后端服务器同步分数和排行榜。留空则禁用所有在线功能。

## 4.1 群聊特定配置

除了通过WebUI进行全局配置外，本插件还支持为特定的群聊设置独立的配置。通过在插件根目录（`data/plugins/pjsk_guess_song/`）下创建一个名为 `group_settings.json` 的文件来实现。

如果该文件不存在，插件会使用全局配置。如果文件存在，插件会加载它，并对文件中指定的群聊应用特定设置。

**文件格式示例:**

```json
{
    "123123": {
      "daily_play_limit": 50,
      "game_cooldown_seconds": 5
    },
    "12312342": {
      "daily_play_limit": 50,
      "game_cooldown_seconds": 100
    }
}
```

**可单独配置的选项:**

- `daily_play_limit`: 每日游戏次数。
- `game_cooldown_seconds`: 游戏冷却时间（秒）。
- `answer_timeout`: 游戏回答的超时时间（秒）。
- `max_guess_attempts`: 每轮游戏总计可尝试回答的次数。
- `bonus_time_after_first_answer`: 首位答对后，他人可继续得分的奖励时间（秒）。
- `end_game_after_bonus_time`: 是否在奖励时间结束后立即结束游戏。
- `ranking_row_limit`: 排行榜显示行数。
- `independent_daily_limit` (布尔值): **独立每日次数限制**。
  - `true`: 本群的每日游戏次数将**独立计算**，不消耗用户的全局次数。
  - `false` (默认): 本群使用用户的全局游戏次数。
- `disable_guess_song_periods` (列表): **禁用猜歌时段**。
  - 在此列表中定义的时段内，猜歌类指令将被禁用。
  - 列表中的每一项都是一个对象，包含 `start` 和 `end` 时间 (24小时制, `HH:MM`格式)，以及一个可选的自定义提示 `message`。
  - **注意**: 跨天的时段（如 23:00 - 07:00）需要拆分为两个时段配置。

**高级配置示例:**

```json
{
    "12345678": {
      "comment": "高强度游戏群，有独立的每日次数，且深夜禁用游戏",
      "daily_play_limit": 100,
      "independent_daily_limit": true,
      "disable_guess_song_periods": [
        { "start": "02:00", "end": "07:00", "message": "现在是深夜休息时间哦 (02:00-07:00)" }
      ]
    },
    "87654321": {
      "comment": "休闲群，使用全局次数，但有午休时段",
      "game_cooldown_seconds": 120,
      "disable_guess_song_periods": [
        { "start": "12:00", "end": "13:30" }
      ]
    }
}
```

