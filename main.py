import asyncio
import json
import random
import time
import re
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime
from urllib.parse import quote

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
import astrbot.api.message_components as Comp
from astrbot.core.utils.session_waiter import session_waiter, SessionController, SessionFilter
from astrbot.api import AstrBotConfig

# 导入重构后的服务
try:
    from .services.db_service import DBService
    from .services.audio_service import AudioService
    from .services.stats_service import StatsService
    from .services.cache_service import CacheService
    from .services.master_data_service import SERVER_JP, SERVER_SC, MasterDataService
except ImportError:  # 直接以脚本方式加载（单测）时使用绝对导入
    from services.db_service import DBService
    from services.audio_service import AudioService
    from services.stats_service import StatsService
    from services.cache_service import CacheService
    from services.master_data_service import SERVER_JP, SERVER_SC, MasterDataService


def _get_normalized_session_id(event: AstrMessageEvent) -> str:
    """
    [Standalone] 标准化 session_id，以处理 unified_msg_origin 中可能存在的 user_id 前缀问题。
    - 标准格式: 'platform:type:group_id' (e.g., 'aiocqhttp:GroupMessage:2342')
    - 异常格式: 'platform:type:user_id_group_id' (e.g., 'aiocqhttp:GroupMessage:12345_2342')
    此函数确保无论输入哪种格式，始终返回基于群组的标准格式。
    """
    # 优先使用 get_group_id()，因为它更直接可靠
    group_id = event.get_group_id()
    if group_id:
        # 从原始ID中提取平台和类型部分，与可靠的group_id组合
        original_id = event.unified_msg_origin
        parts = original_id.split(':', 2)
        if len(parts) == 3:
            return f"{parts[0]}:{parts[1]}:{group_id}"

    # 如果 get_group_id() 不可用（例如私聊），则回退到解析 unified_msg_origin
    original_id = event.unified_msg_origin
    parts = original_id.split(':', 2)
    if len(parts) == 3:
        session_part = parts[2]
        if '_' in session_part:
            core_session_id = session_part.rsplit('_', 1)[-1]
            return f"{parts[0]}:{parts[1]}:{core_session_id}"
            
    return original_id


class CustomSessionFilter(SessionFilter):
    """
    自定义会话过滤器，使用标准化的 session_id 来支持群聊。
    """
    def filter(self, event: AstrMessageEvent) -> str:
        return _get_normalized_session_id(event)


# --- 插件元数据 ---
PLUGIN_NAME = "pjsk_guess_song"
PLUGIN_AUTHOR = "nichinichisou"
PLUGIN_DESCRIPTION = "PJSK猜歌插件"
PLUGIN_VERSION = "1.2.1"
PLUGIN_REPO_URL = "https://github.com/nichinichisou0609/astrbot_plugin_pjsk_guess_song"
DEFAULT_PLATFORM_NAME = "aiocqhttp"
OFFICIAL_PLATFORM_NAME = "qq_official"
OFFICIAL_QID_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")

# --- 题库服务器与官机 markdown 机制（与 PJSK Wordle 保持一致） ---
SERVER_JP = "jp"
SERVER_SC = "sc"
SERVER_LABELS = {SERVER_JP: "日服", SERVER_SC: "国服"}
SERVER_BADGES = {SERVER_JP: "日服题库", SERVER_SC: "国服题库"}
# 结算尾部显示的切换指令（当前日服 → 提示切换国服）
SWITCH_COMMANDS = {SERVER_JP: "猜歌切换国服题库", SERVER_SC: "猜歌切换日服题库"}
# 结算连接入口使用的指令名（与注册指令别名一一对应）
CONNECT_SWITCH_COMMANDS = {SERVER_JP: "猜歌切换国服题库", SERVER_SC: "猜歌切换日服题库"}

# 指令连接的默认 markdown 模板：QQ 官方机器人 markdown 消息的参数指令标签
# （见 bot.q.qq.com/wiki 的 markdown / text-chain 文档）。
# text 只放指令本身：QQ 客户端在群聊发送时会自动 @ 官方机器人，
# 拼进 "@id" 反而会出现双重 @。
DEFAULT_CONNECT_TEMPLATE = '<qqbot-cmd-input text="{encoded_command}" show="{encoded_name}" />'
# 旧版本默认模板特征：命中即视为未自定义，自动升级到新默认模板
_LEGACY_TEMPLATE_MARKERS = ("{encoded_at_text}", "mqqapi://")

# 快捷入口：所有 PJSK 娱乐插件的触发指令，Wordle 固定排最后


class BindingSessionFilter(SessionFilter):
    """只接收发起绑定的同一用户在同一会话中的确认消息。"""

    def __init__(self, session_id: str, user_id: str):
        self.session_id = str(session_id)
        self.user_id = str(user_id)

    def filter(self, event: AstrMessageEvent) -> str:
        if (
            str(event.unified_msg_origin) != self.session_id
            or str(event.get_sender_id()) != self.user_id
        ):
            return ""
        return f"{event.unified_msg_origin}:{event.get_sender_id()}"


@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESCRIPTION, PLUGIN_VERSION, PLUGIN_REPO_URL)
class GuessSongPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.plugin_dir = Path(__file__).parent
        self.resources_dir = self.plugin_dir / "resources"
        self.output_dir = self.plugin_dir / "output"
        
        # 服务层初始化
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "guess_song_data.db"
        self.group_settings_path = data_dir / "group_settings.json"
        self.group_settings = self._load_group_settings()
        self.db_service = DBService(str(db_path))
        self.stats_service = StatsService(config)
        self.cache_service = CacheService(self.resources_dir, self.output_dir, config)
        self.audio_service = AudioService(self.cache_service, self.resources_dir, self.output_dir, config, PLUGIN_VERSION)

        # 游戏状态管理
        self.context.game_session_locks = getattr(self.context, "game_session_locks", {})
        self.context.active_game_sessions = getattr(self.context, "active_game_sessions", set())
        self.last_game_end_time = {}
        self.auto_game_sessions = {}  # session_id -> mode_key
        self.consecutive_no_answer = {}  # session_id -> int (连续无人回答的局数)
        self._background_tasks: set[asyncio.Task] = set()
        self._stopping = False

        # 内置静态曲池（master 题库未就绪时的回退数据源，_async_init 中加载）
        self.song_data: List[Dict] = []

        # --- 题库服务器偏好与 master 数据自动同步 ---
        self.server_prefs_path = data_dir / "session_servers.json"
        self.server_prefs: Dict = self._load_server_prefs()
        self.master_data = MasterDataService(
            data_dir,
            update_interval_hours=int(config.get("update_interval_hours", 24)),
        )
        self.master_data.on_songs_updated = self._on_master_updated

        self.game_effects = self.audio_service.game_effects
        self.game_modes = self.audio_service.game_modes
        self.mode_name_map = self.audio_service.mode_name_map

        # 异步初始化任务
        self._init_task = self._track_task(asyncio.create_task(self._async_init()))
        self._master_task = self._track_task(asyncio.create_task(self.master_data.start()))
        self._cleanup_task = self._track_task(asyncio.create_task(self.cache_service.periodic_cleanup_task()))

    def _track_task(self, task: asyncio.Task) -> asyncio.Task:
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    # --- 题库服务器与 master 数据同步 ---

    def _load_server_prefs(self) -> Dict:
        try:
            if self.server_prefs_path.exists():
                return json.loads(self.server_prefs_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"加载题库服务器偏好失败: {e}")
        return {}

    def _save_server_prefs(self):
        try:
            self.server_prefs_path.write_text(
                json.dumps(self.server_prefs, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"保存题库服务器偏好失败: {e}")

    def _server_for_session(self, session_id: str) -> str:
        saved = self.server_prefs.get(session_id)
        if saved in (SERVER_JP, SERVER_SC):
            return saved
        default = str(self.config.get("default_server", SERVER_JP)).lower()
        return SERVER_SC if default == SERVER_SC else SERVER_JP

    def _on_master_updated(self):
        """题库更新后的回调（曲池在开局时按会话实时读取，无需额外重建）。"""
        logger.info("[猜歌插件] 题库已随 master 数据更新。")

    def _get_song_pool(self, session_id: str) -> List[Dict]:
        """获取会话当前服务器的曲池，避免用日服回退数据请求国服资源。"""
        server = self._server_for_session(session_id)
        master_songs = self.master_data.get_songs(server)
        if master_songs:
            return master_songs
        if server == SERVER_JP:
            return self.song_data or []
        return []

    # --- 官机 markdown 机制（与 PJSK Wordle 一致） ---

    def _build_connect_link(self, command: str, self_id: str, show: Optional[str] = None) -> str:
        """按配置模板生成 markdown 格式的指令连接。

        默认使用 QQ 官方机器人 markdown 消息的参数指令标签
        <qqbot-cmd-input>：点击后在聊天框填入指令，QQ 客户端发送时会自动 @ 官方机器人。
        show 为展示名（默认与指令一致）。可通过 connect_link_template 配置项适配环境，
        模板显式置空则退回纯文本"（连接：@官机 指令）"。
        """
        template = self.config.get("connect_link_template")
        if template is None or any(marker in str(template) for marker in _LEGACY_TEMPLATE_MARKERS):
            template = DEFAULT_CONNECT_TEMPLATE
        template = str(template).strip()
        if not template:
            return f"（连接：@{self_id} {command}）"
        display = show or command
        at_text = f"@{self_id} {command}"
        return template.format(
            name=command,
            command=command,
            self_id=self_id,
            at_text=at_text,
            encoded_command=quote(command, safe=""),
            encoded_name=quote(display, safe=""),
            encoded_at_text=quote(at_text, safe=""),
        )

    def _get_official_connect_id(self, event: AstrMessageEvent) -> str:
        """返回官机连接模板的兼容 ID；默认 qqbot-cmd-input 不依赖该值。"""
        return self._get_official_self_id(event) or "qq_official"

    def _get_official_self_id(self, event: AstrMessageEvent) -> str:
        return str(getattr(event.message_obj, "self_id", "") or "").strip()

    async def _send_markdown_text(self, event: AstrMessageEvent, text: str):
        """以 QQ 官方机器人 markdown 消息发送纯文本（含连接标签）。"""
        result = event.make_result()
        result.chain = [Comp.Plain(text)]
        result.use_markdown(True)
        await event.send(result)

    def _get_quick_entries(self) -> list[str]:
        """读取快捷入口配置；若列表为空则不显示快捷入口。"""
        entries = self.config.get("quick_entries")
        if not entries:
            return []
        cleaned = [str(x).strip() for x in entries if str(x).strip()]
        wordle = "Wordle"
        if wordle in cleaned:
            cleaned = [x for x in cleaned if x != wordle] + [wordle]
        return cleaned

    def _build_server_footer(self, event: AstrMessageEvent, server: str) -> List[str]:
        """构建结算消息的题库服务器尾部：官机附 markdown 连接入口与快捷入口，普通 QQ 仅提示指令。"""
        other = SERVER_SC if server == SERVER_JP else SERVER_JP
        switch_cmd = SWITCH_COMMANDS[server]
        connect_switch_cmd = CONNECT_SWITCH_COMMANDS[server]
        lines = [f"本局题库服务器：{SERVER_LABELS[server]}"]

        if self._get_event_platform_name(event) == OFFICIAL_PLATFORM_NAME:
            self_id = self._get_official_connect_id(event)
            if self_id:
                lines.append(self._build_connect_link(connect_switch_cmd, self_id))
                account_links = ["猜歌绑定QQ", "猜歌个人分数", "猜歌排行榜"]
                lines.append(
                    "  ".join(self._build_connect_link(name, self_id) for name in account_links)
                )
                entries = self._get_quick_entries()
                if entries:
                    lines.append("快捷入口：")
                    lines.append(
                        "  ".join(self._build_connect_link(name, self_id) for name in entries)
                    )
                return lines
        lines.append(f"你可以使用{switch_cmd}指令切换{SERVER_LABELS[other]}题库。")
        return lines

    async def _switch_server(self, event: AstrMessageEvent, server: str):
        """切换当前会话的题库服务器。"""
        session_id = _get_normalized_session_id(event)
        if session_id in self.context.active_game_sessions:
            await event.send(event.plain_result("本局游戏还在进行中，结束后再切换题库服务器吧。"))
            return
        current = self._server_for_session(session_id)
        if current == server:
            await event.send(event.plain_result(f"当前题库已经是{SERVER_LABELS[server]}题库了。"))
            return
        self.server_prefs[session_id] = server
        self._save_server_prefs()
        count = self.master_data.get_song_count(server)
        version = self.master_data.get_version(server)
        await event.send(
            event.plain_result(
                f"已切换为{SERVER_BADGES[server]}（共 {count} 首，版本 {version}），下一局生效。"
            )
        )

    @staticmethod
    def _get_event_platform_name(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_platform_name", None)
        try:
            platform_name = getter() if callable(getter) else DEFAULT_PLATFORM_NAME
        except Exception:
            platform_name = DEFAULT_PLATFORM_NAME
        return str(platform_name or DEFAULT_PLATFORM_NAME).strip().lower()

    def _is_qq_official_event(self, event: AstrMessageEvent) -> bool:
        return self._get_event_platform_name(event) == OFFICIAL_PLATFORM_NAME

    async def _get_account_identity(self, event: AstrMessageEvent) -> Tuple[str, str]:
        platform_name = self._get_event_platform_name(event)
        raw_user_id = str(event.get_sender_id())
        resolved_user_id = await self.db_service.resolve_user_id(platform_name, raw_user_id)
        if str(resolved_user_id) != raw_user_id:
            return str(resolved_user_id), DEFAULT_PLATFORM_NAME
        return raw_user_id, platform_name

    @staticmethod
    def _build_binding_confirmation_message(qq_user_id: str) -> str:
        return (
            f"你确认将账号绑定至  {qq_user_id} ？官方机作答的分数将迁移至该账号。\n"
            "发送“确认”将开始绑定。发送“取消”将取消绑定。"
        )

    def _load_group_settings(self) -> Dict:
        """从 group_settings.json 加载群聊特定设置。"""
        if not self.group_settings_path.exists():
            # 文件不存在是正常情况，无需日志
            return {}
        try:
            with open(self.group_settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
                logger.info(f"成功加载 {len(settings)} 个群聊的特定设置。")
                return settings
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"加载或解析 group_settings.json 文件失败: {e}")
            return {}

    def _get_setting_for_group(self, event: AstrMessageEvent, key: str, default: any) -> any:
        """为当前群聊获取一个分层设置。优先群聊特定设置，然后是全局设置，最后是代码默认值。"""
        group_id = event.get_group_id()
        # 1. 尝试从群聊特定设置中获取 (from group_settings.json)
        if group_id:
            group_config = self.group_settings.get(str(group_id), {})
            if key in group_config:
                return group_config[key]
        
        # 2. 如果没有找到，则回退到全局设置 (from main config file)
        return self.config.get(key, default)

    async def _async_init(self):
        """异步初始化所有服务和数据"""
        await self.db_service.init_db()
        await self.cache_service.load_resources_and_manifest()
        
        self.song_data = self.cache_service.song_data

    async def _check_game_start_conditions(self, event: AstrMessageEvent) -> Tuple[bool, Optional[str]]:
        """检查是否可以开始新游戏，返回(布尔值, 提示信息)"""
        if not await self._is_group_allowed(event):
            return False, None

        # --- 新增：检查游戏是否在禁用时段 ---
        now_time = datetime.now().time()
        disable_periods = self._get_setting_for_group(event, "disable_guess_song_periods", [])
        if isinstance(disable_periods, list):
            for period in disable_periods:
                try:
                    start_time = datetime.strptime(period["start"], "%H:%M").time()
                    end_time = datetime.strptime(period["end"], "%H:%M").time()
                    in_period = (
                        start_time <= now_time < end_time
                        if start_time <= end_time
                        else now_time >= start_time or now_time < end_time
                    )
                    if in_period:
                        default_msg = f"当前时段 ({period['start']} - {period['end']}) 猜歌功能已禁用。"
                        return False, period.get("message", default_msg)
                except (KeyError, ValueError) as e:
                    logger.warning(f"跳过格式错误的禁用时段配置: {period}, 错误: {e}")
                    continue
        
        session_id = _get_normalized_session_id(event)
        cooldown = self._get_setting_for_group(event, "game_cooldown_seconds", 30)
        limit = self._get_setting_for_group(event, "daily_play_limit", 15)
        debug_mode = self.config.get("debug_mode", False)
        is_independent_limit = self._get_setting_for_group(event, "independent_daily_limit", False)

        if not debug_mode and time.time() - self.last_game_end_time.get(session_id, 0) < cooldown:
            remaining_time = cooldown - (time.time() - self.last_game_end_time.get(session_id, 0))
            time_display = f"{remaining_time:.3f}" if remaining_time < 1 else str(int(remaining_time))
            return False, f"嗯...休息 {time_display} 秒再玩吧......"

        if session_id in self.context.active_game_sessions:
            return False, "嗯...有一个正在进行的游戏了呢。"

        user_id, platform_name = await self._get_account_identity(event)
        can_play = await self.db_service.can_play(
            user_id,
            limit,
            session_id,
            is_independent_limit,
            platform_name,
        )
        if not debug_mode and not can_play:
            limit_type = "本群" if is_independent_limit else "你"
            return False, f"......{limit_type}今天的游戏次数已达上限（{limit}次），请明天再来吧......"

        return True, None
    
    async def _consume_daily_attempt(
        self,
        event: AstrMessageEvent,
        session_id: str,
        user_id: str,
        user_name: str,
        platform_name: str,
    ) -> bool:
        is_independent = self._get_setting_for_group(event, "independent_daily_limit", False)
        limit = self._get_setting_for_group(event, "daily_play_limit", 15)
        consumed = await self.db_service.consume_daily_play_attempt(
            user_id,
            user_name,
            session_id,
            is_independent,
            platform_name,
            daily_limit=limit,
        )
        if not consumed:
            limit_type = "本群" if is_independent else "你"
            await event.send(
                event.plain_result(f"......{limit_type}今天的游戏次数已达上限（{limit}次）。")
            )
        return consumed

    async def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        """检查群组是否在白名单中, 如果不在则发送提示消息"""
        # 标准化白名单为字符串集合
        whitelist = {str(x) for x in self.config.get("group_whitelist", [])}
        # 如果白名单为空，则允许所有群聊
        if not whitelist:
            return True

        group_id = event.get_group_id()
        is_in_whitelist = bool(group_id and str(group_id) in whitelist)

        # 如果是群聊、不在白名单中，并且配置了提示消息，则发送提示
        if group_id and not is_in_whitelist:
            reject_msg = self._get_whitelist_reject_message()
            if reject_msg:
                try:
                    await event.send(event.plain_result(reject_msg))
                except Exception as e:
                    logger.error(f"发送非白名单群聊提示消息失败: {e}")

        return is_in_whitelist

    def _get_whitelist_reject_message(self) -> Optional[str]:
        """获取白名单拒绝提示信息"""
        msg = self.config.get("whitelist_reject_message", "")
        if msg and msg.strip():
            return msg.strip()
        return None

    @filter.command(
        "pjsk猜歌",
        alias={
            "猜歌",
            "pjsk猜歌1", "pjsk猜歌2",
            "猜歌1", "猜歌2",
            "2倍速猜歌", "倒放猜歌"
        }
    )
    async def start_guess_song_unified(self, event: AstrMessageEvent):
        """统一处理所有固定模式的猜歌指令"""
        session_id = _get_normalized_session_id(event)
        if session_id not in self.context.game_session_locks:
            self.context.game_session_locks[session_id] = asyncio.Lock()
        lock = self.context.game_session_locks[session_id]

        if "2倍速" in event.message_str or "二倍速" in event.message_str:
            mode_key = '1'
        elif "倒放" in event.message_str:
            mode_key = '2'
        else:
            match = re.search(r'(\d+)', event.message_str)
            mode_key = match.group(1) if match else 'normal'

        async with lock:
            can_start, message = await self._check_game_start_conditions(event)
            if not can_start:
                if message:
                    await event.send(event.plain_result(message))
                return
            self.context.active_game_sessions.add(session_id)

        try:
            initiator_id, initiator_platform = await self._get_account_identity(event)
            initiator_name = event.get_sender_name()
            await self.stats_service.api_ping("guess_song")
            
            mode_config = self.game_modes.get(mode_key)
            if not mode_config:
                await event.send(event.plain_result(f"......未知的猜歌模式 '{mode_key}'。"))
                return
            
            game_kwargs = mode_config['kwargs'].copy()
            game_kwargs['score'] = mode_config.get('score', 1)

            if 'reverse_audio' in game_kwargs:
                game_type_suffix = 'reverse'
            elif 'speed_multiplier' in game_kwargs:
                game_type_suffix = 'speed_2x'
            else:
                game_type_suffix = 'normal'
            game_kwargs['mode_key'] = mode_key
            game_kwargs['game_type'] = f"guess_song_{game_type_suffix}"
            
            song_pool = self._get_song_pool(session_id)
            if not song_pool:
                await event.send(event.plain_result("......歌曲数据未加载，无法开始游戏。"))
                return
            game_kwargs['force_song_object'] = random.choice(song_pool)
            game_kwargs['server'] = self._server_for_session(session_id)
            game_data = await self.audio_service.get_game_clip(**game_kwargs)
            if not game_data:
                await event.send(event.plain_result("......开始游戏失败，可能是缺少资源文件或配置错误。"))
                return
            if not await self._consume_daily_attempt(
                event,
                session_id,
                initiator_id,
                initiator_name,
                initiator_platform,
            ):
                return
            await self._execute_game_round(event, session_id, game_data)
        except Exception as e:
            logger.error(f"游戏启动过程中发生未处理的异常: {e}", exc_info=True)
            await event.send(event.plain_result("......开始游戏时发生内部错误，已中断。"))
        finally:
            if session_id in self.context.active_game_sessions:
                self.context.active_game_sessions.remove(session_id)
            self.last_game_end_time[session_id] = time.time()
            if session_id in self.auto_game_sessions:
                self._track_task(asyncio.create_task(self._auto_next_round(event, session_id, mode_key)))

    @filter.command("随机猜歌", alias={"rgs"})
    async def start_random_guess_song(self, event: AstrMessageEvent):
        """开始一轮随机特殊模式的猜歌"""
        session_id = _get_normalized_session_id(event)
        if session_id not in self.context.game_session_locks:
            self.context.game_session_locks[session_id] = asyncio.Lock()
        lock = self.context.game_session_locks[session_id]
        
        async with lock:
            can_start, message = await self._check_game_start_conditions(event)
            if not can_start:
                if message:
                    await event.send(event.plain_result(message))
                return
            self.context.active_game_sessions.add(session_id)

        try:
            initiator_id, initiator_platform = await self._get_account_identity(event)
            initiator_name = event.get_sender_name()
            await self.stats_service.api_ping("guess_song_random")

            combined_kwargs, total_score, effect_names_display, mode_name_str = self.audio_service.get_random_mode_config()
            if not combined_kwargs:
                await event.send(event.plain_result("......随机模式启动失败，没有可用的效果组合。请检查资源文件。"))
                return

            await event.send(event.plain_result(f"好哒！本轮应用效果：【{effect_names_display}】(总计{total_score}分)"))
            combined_kwargs['random_mode_name'] = f"random_{mode_name_str}"
            combined_kwargs['score'] = total_score
            combined_kwargs['game_type'] = 'guess_song_random'
            
            song_pool = self._get_song_pool(session_id)
            if not song_pool:
                await event.send(event.plain_result("......歌曲数据未加载，无法开始游戏。"))
                return
            combined_kwargs['force_song_object'] = random.choice(song_pool)
            combined_kwargs['server'] = self._server_for_session(session_id)
            game_data = await self.audio_service.get_game_clip(**combined_kwargs)
            if not game_data:
                await event.send(event.plain_result("......开始游戏失败，可能是缺少资源文件或配置错误。"))
                return
            if not await self._consume_daily_attempt(
                event,
                session_id,
                initiator_id,
                initiator_name,
                initiator_platform,
            ):
                return
            await self._execute_game_round(event, session_id, game_data)
        except Exception as e:
            logger.error(f"游戏启动过程中发生未处理的异常: {e}", exc_info=True)
            await event.send(event.plain_result("......开始游戏时发生内部错误，已中断。"))
        finally:
            if session_id in self.context.active_game_sessions:
                self.context.active_game_sessions.remove(session_id)
            self.last_game_end_time[session_id] = time.time()
            if session_id in self.auto_game_sessions:
                self._track_task(asyncio.create_task(self._auto_next_round(event, session_id, 'random')))

    @filter.command("自动猜歌", alias={"连续猜歌", "autogs"})
    async def start_auto_guess_song(self, event: AstrMessageEvent):
        """进入自动猜歌模式，结束后自动开启下一局"""
        session_id = _get_normalized_session_id(event)
        if session_id not in self.context.game_session_locks:
            self.context.game_session_locks[session_id] = asyncio.Lock()
        lock = self.context.game_session_locks[session_id]

        msg = event.message_str.strip().lower()
        if "随机" in msg or "random" in msg:
            mode_key = 'random'
        elif "2倍速" in msg or "二倍速" in msg or " 1" in msg:
            mode_key = '1'
        elif "倒放" in msg or " 2" in msg:
            mode_key = '2'
        else:
            mode_key = 'normal'
        yield event.plain_result("已开启自动猜歌模式！本局结束后将自动开始下一局。发送“退出自动模式”可关闭自动模式。")
        async with lock:
            can_start, message = await self._check_game_start_conditions(event)
            if not can_start:
                if message:
                    await event.send(event.plain_result(message))
                return
            self.context.active_game_sessions.add(session_id)
            self.auto_game_sessions[session_id] = mode_key
            self.consecutive_no_answer.pop(session_id, None)  # 进入自动模式时重置计数器

        try:
            initiator_id, initiator_platform = await self._get_account_identity(event)
            initiator_name = event.get_sender_name()

            if mode_key == 'random':
                await self.stats_service.api_ping("guess_song_random")
                combined_kwargs, total_score, effect_names_display, mode_name_str = self.audio_service.get_random_mode_config()
                if not combined_kwargs:
                    await event.send(event.plain_result("......随机模式启动失败，没有可用的效果组合。"))
                    return
                await event.send(event.plain_result(f"好哒！本轮应用效果：【{effect_names_display}】(总计{total_score}分)"))
                combined_kwargs['random_mode_name'] = f"random_{mode_name_str}"
                combined_kwargs['score'] = total_score
                combined_kwargs['game_type'] = 'guess_song_random'
                song_pool = self._get_song_pool(session_id)
                if not song_pool:
                    await event.send(event.plain_result("......歌曲数据未加载，无法开始游戏。"))
                    return
                combined_kwargs['force_song_object'] = random.choice(song_pool)
                combined_kwargs['server'] = self._server_for_session(session_id)
                game_data = await self.audio_service.get_game_clip(**combined_kwargs)
            else:
                await self.stats_service.api_ping("guess_song")
                mode_config = self.game_modes.get(mode_key)
                if not mode_config:
                    await event.send(event.plain_result(f"......未知的猜歌模式 '{mode_key}'。"))
                    return
                game_kwargs = mode_config['kwargs'].copy()
                game_kwargs['score'] = mode_config.get('score', 1)
                if 'reverse_audio' in game_kwargs:
                    game_type_suffix = 'reverse'
                elif 'speed_multiplier' in game_kwargs:
                    game_type_suffix = 'speed_2x'
                else:
                    game_type_suffix = 'normal'
                game_kwargs['mode_key'] = mode_key
                game_kwargs['game_type'] = f"guess_song_{game_type_suffix}"
                song_pool = self._get_song_pool(session_id)
                if not song_pool:
                    await event.send(event.plain_result("......歌曲数据未加载，无法开始游戏。"))
                    return
                game_kwargs['force_song_object'] = random.choice(song_pool)
                game_kwargs['server'] = self._server_for_session(session_id)
                game_data = await self.audio_service.get_game_clip(**game_kwargs)

            if not game_data:
                await event.send(event.plain_result("......开始游戏失败，可能是缺少资源文件或配置错误。"))
                self.auto_game_sessions.pop(session_id, None)
                return
            if not await self._consume_daily_attempt(
                event,
                session_id,
                initiator_id,
                initiator_name,
                initiator_platform,
            ):
                self.auto_game_sessions.pop(session_id, None)
                return

            success = await self._execute_game_round(event, session_id, game_data)
            if not success:
                self.auto_game_sessions.pop(session_id, None)
        except Exception as e:
            logger.error(f"自动猜歌启动异常: {e}", exc_info=True)
            await event.send(event.plain_result("......开始游戏时发生内部错误，已中断。"))
        finally:
            if session_id in self.context.active_game_sessions:
                self.context.active_game_sessions.remove(session_id)
            self.last_game_end_time[session_id] = time.time()
            if session_id in self.auto_game_sessions:
                self._track_task(asyncio.create_task(self._auto_next_round(event, session_id, mode_key)))

    async def _run_game_session(
        self,
        event: AstrMessageEvent,
        game_data: Dict,
        intro_text: str,
        intro_image_path: Optional[str],
        answer_reveal_messages: List,
        is_official_round: bool,
    ) -> Tuple[List[Dict], List[Dict]]:
        """统一的游戏会话执行器，包含简化的统计逻辑。"""
        session_id = _get_normalized_session_id(event)
        debug_mode = self.config.get("debug_mode", False)
        timeout_seconds = self._get_setting_for_group(event, "answer_timeout", 30)
        correct_players = {}
        first_correct_answer_time = 0
        game_ended_by_attempts = False
        quit_ended_round = False
        user_guess_counts = {}
        guess_attempts_count = 0
        max_guess_attempts = self._get_setting_for_group(event, "max_guess_attempts", 10)
        max_guesses_per_user = 2
        game_results_to_log = []
        score_updates_to_log = []

        in_auto_mode = session_id in self.auto_game_sessions
        official_self_id = self._get_official_connect_id(event) if is_official_round else ""
        try:
            await event.send(event.chain_result([Comp.Record(file=game_data["clip_path"])]))
            if is_official_round:
                # 官方机器人以 markdown 发送开局消息
                if in_auto_mode:
                    intro_md = intro_text + (
                        "\n"
                        + self._build_connect_link("退出本局", official_self_id)
                        + "  "
                        + self._build_connect_link("退出自动模式", official_self_id)
                    )
                else:
                    intro_md = intro_text + (
                        "\n"
                        + self._build_connect_link("退出本局", official_self_id)
                    )
                await self._send_markdown_text(event, intro_md)
            elif in_auto_mode:
                # 自动模式（非官机）：文字提示退出方式
                auto_intro = (
                    intro_text
                    + "\n发送「退出」可结束自动模式，发送「退出本局」可提前结束这一局。"
                )
                await event.send(event.chain_result([Comp.Plain(auto_intro)]))
            else:
                # 普通模式（非官机）：提示「退出本局」指令可提前结束这一局
                plain_intro = intro_text + "\n发送「退出本局」可提前结束这一局。"
                await event.send(event.chain_result([Comp.Plain(plain_intro)]))
            if intro_image_path:
                await event.send(event.chain_result([Comp.Image(file=intro_image_path)]))

            if debug_mode:
                logger.info("[猜歌插件] 调试模式已启用，立即显示答案")
                await event.send(event.chain_result(answer_reveal_messages))
                return [], [] # 调试模式下不发送统计数据
        except Exception as e:
            logger.error(f"发送消息失败: {e}. 游戏中断。", exc_info=True)
            if session_id in self.context.active_game_sessions:
                self.context.active_game_sessions.remove(session_id)
            self.last_game_end_time[session_id] = time.time()
            return [], [] # 发送失败时返回空列表
        finally:
            if debug_mode:
                if session_id in self.context.active_game_sessions:
                    self.context.active_game_sessions.remove(session_id)
                self.last_game_end_time[session_id] = time.time()

        @session_waiter(timeout=timeout_seconds)
        async def unified_waiter(controller: SessionController, answer_event: AstrMessageEvent):
            nonlocal guess_attempts_count, correct_players, game_ended_by_attempts, first_correct_answer_time, user_guess_counts, quit_ended_round

            user_id, platform_name = await self._get_account_identity(answer_event)
            user_name = answer_event.get_sender_name()
            answer_text = answer_event.message_str.strip()

            # 仅退出本局：支持「仅退出本局」、「退出本局」；若非自动模式，发送「退出」也视为退出本局
            if answer_text in ["仅退出本局", "退出本局"] or (answer_text == "退出" and session_id not in self.auto_game_sessions):
                quit_ended_round = True
                controller.stop()
                return

            # 退出自动模式：任何时候可触发，本局继续、自动模式停止
            if answer_text in ["退出自动模式", "退出"]:
                if self.auto_game_sessions.pop(session_id, None) is not None:
                    await answer_event.send(answer_event.plain_result("已退出自动模式，本局结束后将不再自动开始下一局。"))
                return

            if not answer_text.isdigit():
                return
            
            answer_key = (user_id, platform_name)
            if user_guess_counts.get(answer_key, 0) >= max_guesses_per_user:
                return
            user_guess_counts[answer_key] = user_guess_counts.get(answer_key, 0) + 1
            
            is_correct = False
            try:
                answer_num = int(answer_text)
                if 1 <= answer_num <= game_data.get("num_options", 12):
                    if answer_num == game_data['correct_answer_num']:
                        is_correct = True
            except ValueError:
                pass
            
            if not is_correct:
                guess_attempts_count += 1

            score_to_add = 0
            can_score = False
            if is_correct:
                bonus_time = self._get_setting_for_group(event, "bonus_time_after_first_answer", 5)
                is_first_correct_answer = (first_correct_answer_time == 0)
                can_score = is_first_correct_answer or (bonus_time > 0 and (time.time() - first_correct_answer_time) <= bonus_time)
                if can_score:
                    score_to_add = game_data.get("score", 1)

            if game_data.get('game_type', '').startswith('guess_song'):
                await self.db_service.update_stats(
                    session_id,
                    user_id,
                    user_name,
                    score_to_add,
                    is_correct,
                    platform_name,
                )
                if score_to_add > 0:
                    score_updates_to_log.append({
                        "user_id": user_id,
                        "user_name": user_name,
                        "score_change": score_to_add
                    })

                await self.db_service.update_mode_stats(game_data['mode'], is_correct)
                
                game_results_to_log.append({
                    "game_type": game_data.get('game_type', 'guess_song'),
                    "game_mode": game_data['mode'],
                    "user_id": user_id,
                    "user_name": user_name,
                    "is_correct": is_correct,
                    "score_awarded": score_to_add,
                    "session_id": session_id
                })

            if is_correct and can_score:
                if answer_key not in correct_players:
                    correct_players[answer_key] = {'name': user_name}
                    if first_correct_answer_time == 0:
                        first_correct_answer_time = time.time()
                        end_game_early = self._get_setting_for_group(event, "end_game_after_bonus_time", True)
                        if end_game_early and bonus_time > 0:
                            asyncio.create_task(
                                asyncio.sleep(bonus_time),
                                name=f"end_game_task_{session_id}"
                            ).add_done_callback(
                                lambda _: not game_ended_by_attempts and controller.stop()
                            )

            if max_guess_attempts > 0 and guess_attempts_count >= max_guess_attempts:
                game_ended_by_attempts = True
                controller.stop()

        try:
            await unified_waiter(event, session_filter=CustomSessionFilter())
        except TimeoutError:
            pass
        finally:
            self.last_game_end_time[session_id] = time.time()
            if session_id in self.context.active_game_sessions:
                self.context.active_game_sessions.remove(session_id)
            # 追踪连续无人回答的局数：只要有人作答（无论对错）就不算无人回答
            if session_id in self.auto_game_sessions:
                if correct_players or user_guess_counts:
                    self.consecutive_no_answer.pop(session_id, None)
                else:
                    self.consecutive_no_answer[session_id] = self.consecutive_no_answer.get(session_id, 0) + 1
        
        round_server = self._server_for_session(session_id)
        if quit_ended_round:
            summary_prefix = "本局已结束（仅退出本局）"
        elif game_ended_by_attempts:
            summary_prefix = f"本轮猜测已达上限({max_guess_attempts}次)！"
        else:
            summary_prefix = "时间到！"
        if correct_players:
            winner_names = "、".join(player['name'] for player in correct_players.values())
            summary_text = f"{summary_prefix}\n本轮答对的玩家有：\n{winner_names}"
        else:
            summary_text = f"{summary_prefix} 啊...好像没有人答对呢......"

        if session_id in self.auto_game_sessions:
            # 自动模式：只显示结果与歌名，随后自动开始下一局，不出现 markdown 按钮
            await event.send(event.plain_result(summary_text))
        else:
            summary_text += "\n" + "\n".join(self._build_server_footer(event, round_server))
            if is_official_round:
                # 官方机器人以 markdown 渲染结算消息，附切换/绑定/查分/排行榜连接与快捷入口
                await self._send_markdown_text(event, summary_text)
            else:
                await event.send(event.plain_result(summary_text))

        await event.send(event.chain_result(answer_reveal_messages))
        return game_results_to_log, score_updates_to_log

    async def _execute_game_round(self, event: AstrMessageEvent, session_id: str, game_data: Dict) -> bool:
        """执行一轮猜歌游戏的核心流程（生成选项、发送音频、等待回答、公布答案）。"""
        correct_song = game_data['song']
        pool = self._get_song_pool(session_id)
        if len(pool) < 12:
            await event.send(event.plain_result("......题库歌曲数量不足，无法生成选项。"))
            return False

        other_songs = random.sample([s for s in pool if s['id'] != correct_song['id']], 11)
        options = [correct_song] + other_songs
        random.shuffle(options)

        game_data['options'] = options
        game_data['correct_answer_num'] = options.index(correct_song) + 1

        logger.info(f"[猜歌插件] 新游戏开始. 答案: {correct_song['title']} (选项 {game_data['correct_answer_num']})")

        server = self._server_for_session(session_id)
        game_data["server"] = server
        options_img_path = await self.audio_service.create_options_image(options, server)

        answer_timeout = self._get_setting_for_group(event, "answer_timeout", 30)
        intro_text = f"嗯...\n这首歌是什么呢？请在{answer_timeout}秒内发送编号回答哦～\n每个玩家有2次作答机会"

        jacket_source = self.cache_service.get_resource_url(f"music/jacket/{correct_song['jacketAssetbundleName']}/{correct_song['jacketAssetbundleName']}.png", server)
        answer_reveal_messages = [
            Comp.Plain(f"正确答案是:\n{game_data['correct_answer_num']}. {correct_song.get('cn', correct_song['title'])}\n"),
        ]
        if jacket_source:
            answer_reveal_messages.append(Comp.Image(file=str(jacket_source)))

        is_official_round = self._get_event_platform_name(event) == OFFICIAL_PLATFORM_NAME
        game_logs, score_updates = await self._run_game_session(
            event, game_data, intro_text, options_img_path, answer_reveal_messages, is_official_round
        )
        if game_logs or score_updates:
            self._track_task(asyncio.create_task(self._robust_send_stats(game_logs, score_updates)))
        return True

    async def _auto_next_round(self, event: AstrMessageEvent, session_id: str, mode_key: str):
        """自动猜歌：延迟后启动下一局。期间监听「退出自动模式」消息。"""
        from astrbot.core.utils.session_waiter import session_waiter, SessionController, SessionFilter

        class AutoSessionFilter(SessionFilter):
            def filter(self, ev) -> str:
                return _get_normalized_session_id(ev)

        @session_waiter(timeout=5, record_history_chains=False)
        async def quit_waiter(controller: SessionController, waiter_event: AstrMessageEvent):
            if waiter_event.message_str.strip() in ["退出自动模式", "退出"]:
                if self.auto_game_sessions.pop(session_id, None) is not None:
                    await waiter_event.send(waiter_event.plain_result("已退出自动模式。"))
                controller.stop()

        try:
            await quit_waiter(event, session_filter=AutoSessionFilter())
            if session_id not in self.auto_game_sessions:
                return
        except TimeoutError:
            pass

        # 连续3局无人回答，自动停止
        if self.consecutive_no_answer.get(session_id, 0) >= 3:
            self.auto_game_sessions.pop(session_id, None)
            self.consecutive_no_answer.pop(session_id, None)
            await event.send(event.plain_result("连续3局无人回答，自动猜歌已停止。"))
            return

        if session_id not in self.auto_game_sessions:
            return
        await event.send(event.plain_result("下一局即将开始…发送「退出自动模式」可停止"))
        await asyncio.sleep(2)
        if session_id not in self.auto_game_sessions:
            return

        if session_id not in self.context.game_session_locks:
            self.context.game_session_locks[session_id] = asyncio.Lock()
        lock = self.context.game_session_locks[session_id]

        async with lock:
            if not await self._is_group_allowed(event):
                self.auto_game_sessions.pop(session_id, None)
                return
            if session_id in self.context.active_game_sessions:
                return
            self.context.active_game_sessions.add(session_id)

        try:
            initiator_id, initiator_platform = await self._get_account_identity(event)
            initiator_name = event.get_sender_name()

            if mode_key == 'random':
                await self.stats_service.api_ping("guess_song_random")
                combined_kwargs, total_score, effect_names_display, mode_name_str = self.audio_service.get_random_mode_config()
                if not combined_kwargs:
                    await event.send(event.plain_result("......随机模式启动失败，没有可用的效果组合。"))
                    return
                await event.send(event.plain_result(f"好哒！本轮应用效果：【{effect_names_display}】(总计{total_score}分)"))
                combined_kwargs['random_mode_name'] = f"random_{mode_name_str}"
                combined_kwargs['score'] = total_score
                combined_kwargs['game_type'] = 'guess_song_random'
                song_pool = self._get_song_pool(session_id)
                if not song_pool:
                    await event.send(event.plain_result("......歌曲数据未加载，无法开始游戏。"))
                    return
                combined_kwargs['force_song_object'] = random.choice(song_pool)
                combined_kwargs['server'] = self._server_for_session(session_id)
                game_data = await self.audio_service.get_game_clip(**combined_kwargs)
            else:
                await self.stats_service.api_ping("guess_song")
                mode_config = self.game_modes.get(mode_key)
                if not mode_config:
                    return
                game_kwargs = mode_config['kwargs'].copy()
                game_kwargs['score'] = mode_config.get('score', 1)
                if 'reverse_audio' in game_kwargs:
                    game_type_suffix = 'reverse'
                elif 'speed_multiplier' in game_kwargs:
                    game_type_suffix = 'speed_2x'
                else:
                    game_type_suffix = 'normal'
                game_kwargs['mode_key'] = mode_key
                game_kwargs['game_type'] = f"guess_song_{game_type_suffix}"
                song_pool = self._get_song_pool(session_id)
                if not song_pool:
                    await event.send(event.plain_result("......歌曲数据未加载，无法开始游戏。"))
                    return
                game_kwargs['force_song_object'] = random.choice(song_pool)
                game_kwargs['server'] = self._server_for_session(session_id)
                game_data = await self.audio_service.get_game_clip(**game_kwargs)

            if not game_data:
                await event.send(event.plain_result("......开始游戏失败，可能是缺少资源文件或配置错误。"))
                self.auto_game_sessions.pop(session_id, None)
                return
            if not await self._consume_daily_attempt(
                event,
                session_id,
                initiator_id,
                initiator_name,
                initiator_platform,
            ):
                self.auto_game_sessions.pop(session_id, None)
                return

            success = await self._execute_game_round(event, session_id, game_data)
            if not success:
                self.auto_game_sessions.pop(session_id, None)
        except Exception as e:
            logger.error(f"自动猜歌下一局异常: {e}", exc_info=True)
            await event.send(event.plain_result("......自动猜歌发生错误，已停止。"))
            self.auto_game_sessions.pop(session_id, None)
        finally:
            if session_id in self.context.active_game_sessions:
                self.context.active_game_sessions.remove(session_id)
            self.last_game_end_time[session_id] = time.time()
            if session_id in self.auto_game_sessions:
                self._track_task(asyncio.create_task(self._auto_next_round(event, session_id, mode_key)))

    @filter.command("猜歌帮助")
    async def show_guess_song_help(self, event: AstrMessageEvent):
        """以图片形式显示猜歌插件帮助。"""
        if not await self._is_group_allowed(event):
            return

        img_path = await self.audio_service.draw_help_image()
        if img_path:
            await event.send(event.image_result(img_path))
        else:
            await event.send(event.plain_result("生成帮助图片时出错。"))

    @filter.command("猜歌切换国服题库", alias={"猜歌切换国服"})
    async def switch_to_sc(self, event: AstrMessageEvent):
        """切换为国服题库。"""
        await self._switch_server(event, SERVER_SC)

    @filter.command("猜歌切换日服题库", alias={"猜歌切换日服"})
    async def switch_to_jp(self, event: AstrMessageEvent):
        """切换为日服题库。"""
        await self._switch_server(event, SERVER_JP)

    @filter.command("猜歌绑定", alias={"pjsk猜歌绑定", "猜歌绑定QQ"})
    async def bind_song_account(self, event: AstrMessageEvent):
        """QQ 官方机器人账号绑定到普通 QQ 账号。"""
        if not self._is_qq_official_event(event):
            await event.send(event.plain_result("此绑定功能仅支持 QQ 官方机器人使用。"))
            return

        parts = event.message_str.strip().split(maxsplit=1)
        qq_user_id = parts[1].strip() if len(parts) > 1 else ""
        if not qq_user_id.isdigit() or not 5 <= len(qq_user_id) <= 12:
            await event.send(event.plain_result("请按“猜歌绑定 QQ号”的格式输入，例如：猜歌绑定 21555762216。"))
            return

        official_user_id = str(event.get_sender_id())
        current_user_id = await self.db_service.resolve_user_id(
            OFFICIAL_PLATFORM_NAME,
            official_user_id,
        )
        if str(current_user_id) != official_user_id:
            await event.send(event.plain_result(f"当前官方机器人账号已经绑定至 QQ号 {current_user_id}。"))
            return

        await event.send(event.plain_result(self._build_binding_confirmation_message(qq_user_id)))
        decision = None

        @session_waiter(timeout=60)
        async def binding_waiter(controller: SessionController, answer_event: AstrMessageEvent):
            nonlocal decision
            answer_text = answer_event.message_str.strip()
            if answer_text == "确认":
                decision = "confirm"
                controller.stop()
            elif answer_text == "取消":
                decision = "cancel"
                controller.stop()

        try:
            await binding_waiter(
                event,
                session_filter=BindingSessionFilter(event.unified_msg_origin, official_user_id),
            )
        except TimeoutError:
            await event.send(event.plain_result("绑定确认已超时，绑定操作已取消。"))
            return

        if decision == "cancel":
            await event.send(event.plain_result("已取消绑定。"))
            return
        if decision != "confirm":
            await event.send(event.plain_result("未收到有效的绑定确认，绑定操作已取消。"))
            return

        bound = await self.db_service.bind_official_account(official_user_id, qq_user_id)
        if bound:
            await event.send(event.plain_result(f"绑定成功！官方机的历史分数已迁移至 QQ号 {qq_user_id}。"))
        else:
            await event.send(event.plain_result("绑定失败：该官方账号可能已绑定，请稍后重试。"))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message_detect_quit(self, event: AstrMessageEvent):
        """监听所有消息：发送"退出自动模式"停止自动猜歌（本局进行中时由对局等待器处理）"""
        if str(event.message_str or "").strip() in ["退出自动模式", "退出"]:
            session_id = _get_normalized_session_id(event)
            if session_id in self.context.active_game_sessions:
                return
            if self.auto_game_sessions.pop(session_id, None) is not None:
                await event.send(event.plain_result("已退出自动模式。"))

    @filter.command("群猜歌排行榜", alias={"gssrank", "gstop"})
    async def show_ranking(self, event: AstrMessageEvent):
        """显示当前群聊的猜歌排行榜"""
        if not await self._is_group_allowed(event): return

        session_id = _get_normalized_session_id(event)
        rows = await self.db_service.get_group_ranking(session_id)

        if not rows:
            await event.send(event.plain_result("......本群目前还没有人参与过猜歌游戏"))
            return

        row_limit = self._get_setting_for_group(event, "ranking_row_limit", 10)
        img_path = await self.audio_service.draw_ranking_image(rows, "本群猜歌排行榜", max_rows=row_limit)
        if img_path:
            await event.send(event.image_result(img_path))
        else:
            await event.send(event.plain_result("生成排行榜图片时出错。"))

   
    @filter.command("猜歌排行榜", alias={"本地猜歌排行榜"})
    async def show_local_global_ranking(self, event: AstrMessageEvent):
        """显示本地存储的全局猜歌排行榜"""
        if not await self._is_group_allowed(event): return

        rows = await self.db_service.get_global_ranking_data()
        if not rows:
            await event.send(event.plain_result("......目前还没有人参与过猜歌游戏"))
            return
            
        row_limit = self._get_setting_for_group(event, "ranking_row_limit", 10)
        img_path = await self.audio_service.draw_ranking_image(rows, "本地猜歌排行榜", max_rows=row_limit)
        if img_path:
            await event.send(event.image_result(img_path))
        else:
            await event.send(event.plain_result("生成排行榜图片时出错。"))

   
    @filter.command("猜歌分数", alias={"gsscore", "我的猜歌分数", "猜歌个人分数"})
    async def show_user_score(self, event: AstrMessageEvent):
        """显示用户在本群、服务器和本地的总分数统计。"""
        raw_user_id = str(event.get_sender_id())
        user_id, platform_name = await self._get_account_identity(event)
        user_name = event.get_sender_name()
        session_id = _get_normalized_session_id(event)
        raw_platform_name = self._get_event_platform_name(event)
        platform_display_name = {
            OFFICIAL_PLATFORM_NAME: "QQ官方机器人",
            DEFAULT_PLATFORM_NAME: "普通QQ",
        }.get(raw_platform_name, raw_platform_name)
        identity_lines = [
            f"👤 用户ID: {raw_user_id}",
            f"🌐 平台: {platform_display_name}（{raw_platform_name}）",
        ]
        if raw_platform_name == OFFICIAL_PLATFORM_NAME and user_id == raw_user_id:
            identity_lines.extend([
                "当前官方机器人账号尚未绑定QQ号。",
                "如需将官方机分数迁移到普通QQ账号，请发送：猜歌绑定 QQ号",
            ])
        elif user_id != raw_user_id:
            identity_lines.append(f"🔗 统计账号: {user_id}")
        
        server_stats_task = asyncio.create_task(self.stats_service.api_get_user_global_stats(user_id))
        
        group_stats_task = self.db_service.get_user_stats_in_group(user_id, session_id, platform_name)
        local_global_stats_task = self.db_service.get_user_local_global_stats(user_id, platform_name)
        
        server_stats, group_stats, local_global_stats = await asyncio.gather(
            server_stats_task, group_stats_task, local_global_stats_task
        )
        
        result_parts = ["\n".join(identity_lines), f"📊 {user_name} 的猜歌报告"]
        
        if group_stats:
            group_score = group_stats.get('score', 0)
            group_attempts = group_stats.get('attempts', -1)
            group_correct = group_stats.get('correct_attempts', -1)
            
            rank_str = f"(排名: {group_stats['rank']})" if group_stats.get('rank') is not None else "(排名: N/A)"
            
            if group_attempts >= 0:
                accuracy_str = f"{(group_correct * 100 / group_attempts if group_attempts > 0 else 0):.1f}% ({group_correct}/{group_attempts})"
            else:
                accuracy_str = "N/A"

            result_parts.append(
                f"⚜️ 本群战绩 {rank_str}\n"
                f"  - 分数: {group_score}\n"
                f"  - 正确率: {accuracy_str}"
            )
        else:
            result_parts.append(
                "⚜️ 本群战绩\n"
                "  - 暂无记录"
            )

        if server_stats:
            server_score = server_stats.get('total_score', 0)
            server_rank = server_stats.get('rank', 'N/A')
            server_attempts = server_stats.get('total_attempts', 0)
            server_correct = server_stats.get('correct_attempts', 0)
            accuracy = f"{(server_correct * 100 / server_attempts if server_attempts > 0 else 0):.1f}%"
            
            result_parts.append(
                f"🌐 总计战绩 (服务器, 排名: {server_rank})\n"
                f"  - 分数: {server_score}\n"
                f"  - 正确率: {accuracy} ({server_correct}/{server_attempts})"
            )
        elif local_global_stats:
            local_score = local_global_stats.get('score', 0)
            local_rank = local_global_stats.get('rank', 'N/A')
            local_attempts = local_global_stats.get('attempts', 0)
            local_correct = local_global_stats.get('correct', 0)
            accuracy = f"{(local_correct * 100 / local_attempts if local_attempts > 0 else 0):.1f}%"
            
            result_parts.append(
                f"🌐 总计战绩 (仅本地, 排名: {local_rank})\n"
                f"  - 分数: {local_score}\n"
                f"  - 正确率: {accuracy} ({local_correct}/{local_attempts})"
            )
        else:
             result_parts.append(
                "🌐 总计战绩\n"
                "  - 暂无记录"
            )

        await event.send(event.plain_result("\n\n".join(result_parts)))
    
    @filter.command("重置猜歌次数", alias={"resetgs"})
    async def reset_guess_limit(self, event: AstrMessageEvent):
        """重置用户猜歌次数（仅限管理员）"""
        if not event.is_admin:
            return
            
        parts = event.message_str.strip().split()
        if len(parts) > 1 and parts[1].isdigit():
            target_id = parts[1]
            success = await self.db_service.reset_guess_limit(target_id)
            if success:
                await event.send(event.plain_result(f"......用户 {target_id} 的猜歌次数已重置。"))
            else:
                await event.send(event.plain_result(f"......未找到用户 {target_id} 的游戏记录。"))
        else:
            await event.send(event.plain_result("请提供要重置的用户ID。"))

    @filter.command("重置题型统计", alias={"resetmodestats"})
    async def reset_mode_stats(self, event: AstrMessageEvent):
        """清空所有题型统计数据（仅限管理员）"""
        if str(event.get_sender_id()) not in self.config.get("super_users", []):
            return
        
        await self.db_service.reset_mode_stats()
        await event.send(event.plain_result("......所有题型统计数据已被清空。"))

    @filter.command("查看统计", alias={"mode_stats", "题型统计"})
    async def show_mode_stats(self, event: AstrMessageEvent):
        """以图片形式显示个人的各题型正确率统计"""
        if not await self._is_group_allowed(event):
            return

        user_id, _ = await self._get_account_identity(event)
        user_name = event.get_sender_name()

        # 直接在代码中定义最低次数门槛
        ranking_min_attempts = 35

        # 并行获取所有需要的数据
        server_stats_task = asyncio.create_task(self.stats_service.api_get_user_global_stats(user_id))
        user_mode_stats_task = asyncio.create_task(self.stats_service.api_get_user_mode_stats(user_id))
        user_mode_ranks_task = asyncio.create_task(self.stats_service.api_get_user_mode_ranks(user_id, ranking_min_attempts))
        
        server_stats, user_mode_stats, user_mode_ranks = await asyncio.gather(
            server_stats_task, user_mode_stats_task, user_mode_ranks_task
        )

        if user_mode_stats is None:
            await event.send(event.plain_result(f"......无法从服务器获取 {user_name} 的统计数据。请稍后再试。"))
            return

        # --- 数据处理和分类 ---
        # 聚合关键字 -> 显示名称
        CORE_AGGREGATION_MAP = {
            "普通": "普通", "倍速": "倍速", "倒放": "倒放"
        }

        core_mode_stats = {v: {"total": 0, "correct": 0} for v in CORE_AGGREGATION_MAP.values()}
        detailed_stats = []

        # 聚合用户个人数据
        for stat in user_mode_stats:
            mode_name, total, correct = stat['mode'], stat['total_attempts'], stat['correct_attempts']
            for keyword, display_name in CORE_AGGREGATION_MAP.items():
                if keyword in mode_name:
                    core_mode_stats[display_name]["total"] += total
                    core_mode_stats[display_name]["correct"] += correct
            
            accuracy = (correct * 100 / total) if total > 0 else 0
            detailed_stats.append((mode_name, total, correct, accuracy))
        
        # 直接使用新 API 返回的排名
        if user_mode_ranks:
            for display_name, data in core_mode_stats.items():
                rank = user_mode_ranks.get(display_name)
                if rank:
                    data['rank'] = rank

        detailed_stats.sort(key=lambda x: x[3], reverse=True)

        # --- 调用绘图服务 ---
        img_path = await self.audio_service.draw_personal_stats_image(
            user_name,
            server_stats,
            core_mode_stats,
            detailed_stats
        )
        
        if img_path:
            await event.send(event.image_result(img_path))
        else:
            await event.send(event.plain_result("生成个人统计图片时出错。"))

    #@filter.command("测试猜歌", alias={"test_song", "调试猜歌"})
    async def test_guess_song(self, event: AstrMessageEvent):
        """(管理员) 生成一个用于测试的猜歌游戏，可指定歌曲和多种模式。"""
        if str(event.get_sender_id()) not in self.config.get("super_users", []):
            return

        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts) < 2:
            await event.send(event.plain_result("用法: /测试猜歌 [模式,...] <歌曲名或ID>\n例如: /测试猜歌 bass,reverse Tell Your World"))
            return

        args_str = parts[1]
        arg_parts = args_str.split()
        
        potential_modes_str = arg_parts[0]
        temp_modes = re.split(r'[,，]', potential_modes_str)
        
        parsed_mode_keys = []
        is_first_arg_modes = True
        for mode_str in temp_modes:
            mode_key = self.mode_name_map.get(mode_str.lower())
            if mode_key:
                parsed_mode_keys.append(mode_key)
            else:
                is_first_arg_modes = False
                break
        
        if is_first_arg_modes and parsed_mode_keys:
            mode_keys_input = list(dict.fromkeys(parsed_mode_keys))
            song_query = " ".join(arg_parts[1:])
        else:
            mode_keys_input = []
            song_query = args_str

        if not song_query:
            await event.send(event.plain_result("请输入要测试的歌曲名称或ID。"))
            return

        final_kwargs = {}
        effect_names = []
        total_score = 0

        if not mode_keys_input:
            mode_keys_input.append('normal')

        for mode_key in mode_keys_input:
            if mode_key in self.game_modes:
                mode_data = self.game_modes[mode_key]
                final_kwargs.update(mode_data.get('kwargs', {}))
                effect_names.append(mode_data['name'])
                total_score += mode_data.get('score', 0)
            elif mode_key in self.game_effects:
                effect_data = self.game_effects[mode_key]
                final_kwargs.update(effect_data.get('kwargs', {}))
                effect_names.append(effect_data['name'])
                total_score += effect_data.get('score', 0)
        
        target_song = self.cache_service.find_song_by_query(song_query)
        
        if not target_song:
            await event.send(event.plain_result(f'未在数据库中找到与 "{song_query}" 匹配的歌曲。'))
            return

        final_kwargs['force_song_object'] = target_song

        game_data = await self.audio_service.get_game_clip(**final_kwargs)
        if not game_data:
            await event.send(event.plain_result("......生成测试游戏失败，请检查日志。"))
            return

        correct_song = game_data['song']
        other_songs = random.sample([s for s in self.song_data if s['id'] != correct_song['id']], 11)
        options = [correct_song] + other_songs
        random.shuffle(options)
        correct_answer_num = options.index(correct_song) + 1
        options_img_path = await self.audio_service.create_options_image(options)
        
        applied_effects = "、".join(effect_names)
        intro_text = f"--- 调试模式 ---\n歌曲: {correct_song.get('cn', correct_song['title'])}\n效果: {applied_effects}\n答案: {correct_answer_num}"
        
        msg_chain = [Comp.Plain(intro_text)]
        if options_img_path:
            msg_chain.append(Comp.Image(file=options_img_path))
        
        await event.send(event.chain_result(msg_chain))
        await event.send(event.chain_result([Comp.Record(file=game_data["clip_path"])]))

        jacket_source = self.cache_service.get_resource_url(f"music/jacket/{correct_song['jacketAssetbundleName']}/{correct_song['jacketAssetbundleName']}.png")
        answer_msg = [Comp.Plain(f"[测试模式] 正确答案是: {correct_answer_num}. {correct_song['title']}\n")]
        if jacket_source:
            answer_msg.append(Comp.Image(file=str(jacket_source)))
        await event.send(event.chain_result(answer_msg))
    
    @filter.command("同步分数", alias={"syncscore", "migrategs"})
    async def sync_scores_to_server(self, event: AstrMessageEvent):
        """（管理员）将所有用户的本地总分同步到服务器。"""
        if str(event.get_sender_id()) not in self.config.get("super_users", []):
            yield event.plain_result("......权限不足，只有管理员才能执行此操作。")
            return

        if not self.stats_service.api_key:
            yield event.plain_result("......未配置服务器排行榜功能，无法同步。请先在配置文件中设置API密钥。")
            return

        if not self.stats_service.stats_server_url:
            yield event.plain_result("......服务器地址配置不正确，无法同步。")
            return
        
        yield event.plain_result("......正在准备同步所有本地玩家分数至服务器排行榜...")

        all_local_users = await self.db_service.get_all_user_stats()
        
        if not all_local_users:
            yield event.plain_result("......本地没有任何玩家数据，无需同步。")
            return
        
        payload = [
            {"user_id": str(user[0]), "user_name": user[1], "score": user[2]}
            for user in all_local_users
        ]
        
        yield event.plain_result(f"......正在将 {len(payload)} 条玩家数据同步至服务器...")
        await self.stats_service.migrate_scores(payload)
        yield event.plain_result("✅ 分数同步任务已完成。")

    async def _robust_send_stats(self, game_logs: List[Dict], score_updates: List[Dict]):
        """
        一个健壮的后台任务，用于带重试机制地发送统计数据。
        它被设计为通过 asyncio.create_task 来启动，不会阻塞主流程。
        """
        if not self.stats_service.api_key or (not game_logs and not score_updates):
            return

        # 短暂延迟，避免与游戏结束消息的发送抢占资源
        await asyncio.sleep(2)
        logger.debug(f"后台任务：开始发送 {len(game_logs)} 条游戏日志和 {len(score_updates)} 条分数更新。")

        MAX_RETRIES = 3
        RETRY_DELAY = 5  # seconds

        log_tasks = []
        for log in game_logs:
            async def send_log_with_retry(log_data):
                for attempt in range(MAX_RETRIES):
                    if await self.stats_service.api_log_game(log_data):
                        return
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                logger.error(f"发送游戏日志失败，已达最大重试次数: {log_data}")
            log_tasks.append(send_log_with_retry(log))

        score_tasks = []
        for update in score_updates:
            async def send_score_with_retry(score_data):
                for attempt in range(MAX_RETRIES):
                    if await self.stats_service.api_update_score(
                        user_id=score_data['user_id'],
                        user_name=score_data['user_name'],
                        score_delta=score_data['score_change']
                    ):
                        return
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                logger.error(f"发送分数更新失败，已达最大重试次数: {score_data}")
            score_tasks.append(send_score_with_retry(update))

        await asyncio.gather(*(log_tasks + score_tasks))
        logger.debug("后台统计数据发送任务完成。")

    async def terminate(self):
        """取消后台任务并关闭服务资源。"""
        self._stopping = True
        tasks = [task for task in self._background_tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
        self.auto_game_sessions.clear()

        await self.audio_service.terminate()
        await self.stats_service.terminate()
        try:
            await self.master_data.terminate()
        except Exception as e:
            logger.warning(f"停止题库同步服务时出错: {e}")
        logger.info("猜歌插件已终止。")
