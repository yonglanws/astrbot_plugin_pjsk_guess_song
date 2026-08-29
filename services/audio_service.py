import asyncio
import io
import random
import subprocess
import time
import itertools
import aiohttp
from typing import List, Dict, Optional, Tuple, Union
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from collections import defaultdict
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime

try:
    from pydub import AudioSegment
    PYDUB_AVAILABLE = True
except ImportError:
    AudioSegment = None
    PYDUB_AVAILABLE = False

try:
    from PIL.Image import Resampling
    LANCZOS = Resampling.LANCZOS
except ImportError:
    LANCZOS = 1

from pilmoji import Pilmoji

from astrbot.api import logger
from astrbot.api import AstrBotConfig
from .cache_service import CacheService

class AudioService:
    def __init__(self, cache_service: CacheService, resources_dir: Path, output_dir: Path, config: AstrBotConfig, plugin_version: str):
        self.cache_service = cache_service
        self.resources_dir = resources_dir
        self.output_dir = output_dir
        self.config = config
        self.plugin_version = plugin_version
        self.executor = ThreadPoolExecutor(max_workers=5)
        self._session: Optional[aiohttp.ClientSession] = None

        self.game_effects = {
            'speed_2x': {'name': '2倍速', 'score': 1, 'kwargs': {'speed_multiplier': 2.0}},
            'reverse': {'name': '倒放', 'score': 1, 'kwargs': {'reverse_audio': True}},
        }
        self.game_modes = {
            'normal': {'name': '普通', 'kwargs': {}, 'score': 1},
            '1': {'name': '2倍速', 'kwargs': {'speed_multiplier': 2.0}, 'score': 1},
            '2': {'name': '倒放', 'kwargs': {'reverse_audio': True}, 'score': 1},
        }
        self.mode_name_map = {}
        for key, value in self.game_modes.items():
            self.mode_name_map[key] = key
            self.mode_name_map[value['name'].lower()] = key
        for key, value in self.game_effects.items():
            self.mode_name_map[key] = key
            self.mode_name_map[value['name'].lower()] = key

        self.random_mode_decay_factor = self.config.get("random_mode_decay_factor", 0.75)
        self.base_effects = [
            {'name': '2倍速', 'kwargs': {'speed_multiplier': 2.0}, 'group': 'speed', 'score': 1},
            {'name': '倒放', 'kwargs': {'reverse_audio': True}, 'group': 'direction', 'score': 1},
        ]
        self.source_effects = [
            {'name': '普通', 'kwargs': {}, 'group': 'source', 'score': 1},
        ]

    async def _get_session(self) -> Optional[aiohttp.ClientSession]:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get_game_clip(self, **kwargs) -> Optional[Dict]:
        if not self.cache_service.song_data or not PYDUB_AVAILABLE:
            logger.error("无法开始游戏: 歌曲数据未加载或pydub未安装。")
            return None

        loop = asyncio.get_running_loop()

        song = kwargs.get("force_song_object")
        audio_source = None

        if not song:
            song = random.choice(self.cache_service.song_data)

        if not song:
            logger.error("未能确定歌曲。")
            return None

        sekai_ver = next((v for v in song.get('vocals', []) if v.get('musicVocalType') == 'sekai'), None)
        vocal_version = sekai_ver if sekai_ver else (random.choice(song.get("vocals", [])) if song.get("vocals") else None)

        if vocal_version:
            bundle_name = vocal_version["vocalAssetbundleName"]
            audio_source = self.cache_service.get_resource_url(f"music/long/{bundle_name}/{bundle_name}.mp3")

        if not audio_source:
            logger.error(f"歌曲 '{song.get('title')}' 没有有效的音频源文件。")
            return None

        has_speed_change = kwargs.get("speed_multiplier", 1.0) != 1.0
        has_reverse = kwargs.get("reverse_audio", False)
        use_slow_path = has_speed_change or has_reverse

        if not use_slow_path:
            try:
                total_duration_ms = await loop.run_in_executor(self.executor, self._get_duration_ms_ffprobe_sync, audio_source)
                if total_duration_ms is None: raise ValueError("ffprobe failed or not found.")
                target_duration_ms = int(self.config.get("clip_duration_seconds", 10) * 1000)
                start_range_min = int(song.get("fillerSec", 0) * 1000)
                start_range_max = int(total_duration_ms - target_duration_ms)
                start_ms = random.randint(start_range_min, start_range_max) if start_range_min < start_range_max else start_range_min
                clip_path_obj = self.output_dir / f"clip_{int(time.time())}.mp3"
                command = [
                    'ffmpeg', '-ss', str(start_ms / 1000.0), '-i', str(audio_source),
                    '-t', str(target_duration_ms / 1000.0), '-c', 'copy', '-y', str(clip_path_obj)
                ]
                run_subprocess = partial(subprocess.run, command, capture_output=True, text=True, check=True, encoding='utf-8')
                await loop.run_in_executor(self.executor, run_subprocess)
                mode_key = kwargs.get("random_mode_name") or "normal"
                return {"song": song, "clip_path": str(clip_path_obj), "score": kwargs.get("score", 1), "mode": mode_key, "game_type": kwargs.get('game_type')}
            except Exception as e:
                logger.warning(f"快速路径处理失败: {e}. 将回退到 pydub 慢速路径。")

        try:
            session = await self._get_session()
            if not session:
                logger.error("无法获取 aiohttp session")
                return None
            async with session.get(audio_source) as response:
                response.raise_for_status()
                audio_data = io.BytesIO(await response.read())

            pydub_kwargs = {
                "target_duration_seconds": self.config.get("clip_duration_seconds", 10),
                "speed_multiplier": kwargs.get("speed_multiplier", 1.0),
                "reverse_audio": kwargs.get("reverse_audio", False),
                "song_filler_sec": song.get("fillerSec", 0),
            }

            clip = await loop.run_in_executor(self.executor, self._process_audio_with_pydub, audio_data, "mp3", pydub_kwargs)
            if clip is None: raise RuntimeError("pydub audio processing failed.")
            mode = kwargs.get("random_mode_name") or "normal"
            clip_path = self.output_dir / f"clip_{int(time.time())}.mp3"
            clip.export(clip_path, format="mp3", bitrate="128k")
            return {"song": song, "clip_path": str(clip_path), "score": kwargs.get("score", 1), "mode": mode, "game_type": kwargs.get('game_type')}
        except Exception as e:
            logger.error(f"慢速路径 (pydub) 处理音频文件 {audio_source} 时失败: {e}", exc_info=True)
            return None

    def _get_duration_ms_ffprobe_sync(self, file_path: Union[Path, str]) -> Optional[float]:
        command = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(file_path)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True, encoding='utf-8')
            return float(result.stdout.strip()) * 1000
        except (FileNotFoundError, subprocess.CalledProcessError, ValueError) as e:
            logger.error(f"使用 ffprobe 获取时长失败 ({type(e).__name__}): {e}")
            return None

    def _process_audio_with_pydub(self, audio_data: Union[str, Path, io.BytesIO], audio_format: str, options: dict) -> Optional['AudioSegment']:
        try:
            audio = AudioSegment.from_file(audio_data, format=audio_format)
            target_duration_ms = int(options.get("target_duration_seconds", 10) * 1000)
            speed_multiplier = options.get("speed_multiplier", 1.0)
            source_duration_ms = int(target_duration_ms * speed_multiplier)
            total_duration_ms = len(audio)

            if source_duration_ms >= total_duration_ms:
                clip_segment = audio
            else:
                start_range_min = int(options.get("song_filler_sec", 0) * 1000)
                start_range_max = total_duration_ms - source_duration_ms
                start_ms = random.randint(start_range_min, start_range_max) if start_range_min < start_range_max else start_range_min
                end_ms = start_ms + source_duration_ms
                clip_segment = audio[start_ms:end_ms]

            clip = clip_segment
            if speed_multiplier != 1.0:
                clip = clip._spawn(clip.raw_data, overrides={'frame_rate': int(clip.frame_rate * speed_multiplier)})
            if options.get("reverse_audio", False):
                clip = clip.reverse()
            return clip
        except Exception as e:
            logger.error(f"Pydub processing in executor failed: {e}", exc_info=True)
            return None

    async def create_options_image(self, options: List[Dict]) -> Optional[str]:
        if not options or len(options) != 12: return None
        tasks = [self.cache_service.open_image(f"music/jacket/{opt['jacketAssetbundleName']}/{opt['jacketAssetbundleName']}.png") for opt in options]
        jacket_images = await asyncio.gather(*tasks)
        loop = asyncio.get_running_loop()
        try:
            img_path = await loop.run_in_executor(self.executor, self._draw_options_image_sync, options, jacket_images)
            return img_path
        except Exception as e:
            logger.error(f"在executor中创建选项图片失败: {e}", exc_info=True)
            return None

    def _draw_options_image_sync(self, options: List[Dict], jacket_images: List[Optional[Image.Image]]) -> Optional[str]:
        jacket_w, jacket_h = 128, 128
        padding = 15
        text_h = 50
        cols, rows = 3, 4
        img_w = cols * jacket_w + (cols + 1) * padding
        img_h = rows * (jacket_h + text_h) + (rows + 1) * padding
        img = Image.new('RGBA', (img_w, img_h), (245, 245, 245, 255))
        try:
            font_path = str(self.resources_dir / "font.ttf")
            title_font = ImageFont.truetype(font_path, 16)
            num_font = ImageFont.truetype(font_path, 22)
        except IOError:
            title_font = ImageFont.load_default()
            num_font = title_font
        draw = ImageDraw.Draw(img)
        for i, option in enumerate(options):
            jacket_img = jacket_images[i]
            if not jacket_img: continue
            row_idx, col_idx = i // cols, i % cols
            x = padding + col_idx * (jacket_w + padding)
            y = padding + row_idx * (jacket_h + text_h + padding)
            try:
                jacket = jacket_img.convert("RGBA").resize((jacket_w, jacket_h), LANCZOS)
                img.paste(jacket, (x, y), jacket)
                num_text = f"{i + 1}"
                circle_radius = 16
                circle_center = (x + circle_radius, y + circle_radius)
                draw.ellipse((circle_center[0] - circle_radius, circle_center[1] - circle_radius,
                                circle_center[0] + circle_radius, circle_center[1] + circle_radius),
                                fill=(0, 0, 0, 180))
                with Pilmoji(img) as pilmoji_drawer:
                    pilmoji_drawer.text(circle_center, num_text, font=num_font, fill=(255, 255, 255), anchor="mm")
                title = option.get('cn', option['title'])
                if title_font.getbbox(title)[2] > jacket_w:
                    while title_font.getbbox(title + "...")[2] > jacket_w and len(title) > 1:
                        title = title[:-1]
                    title += "..."
                title_bbox = draw.textbbox((0, 0), title, font=title_font)
                title_w = title_bbox[2] - title_bbox[0]
                text_x = x + (jacket_w - title_w) / 2
                text_y = y + jacket_h + 8
                draw.text((text_x, text_y), title, font=title_font, fill=(30, 30, 50))
            except Exception as e:
                logger.error(f"处理歌曲封面失败: {option.get('title')}, 错误: {e}")
                continue
        img_path = self.output_dir / f"song_options_{int(time.time())}.png"
        img.save(img_path)
        return str(img_path)

    async def draw_ranking_image(self, rows, title_text="猜歌排行榜", max_rows: int = 10, date_range_str: Optional[str] = None) -> Optional[str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, self._draw_ranking_image_sync, rows, title_text, max_rows, date_range_str)

    def _draw_ranking_image_sync(self, rows, title_text="猜歌排行榜", max_rows: int = 10, date_range_str: Optional[str] = None) -> Optional[str]:
        """渲染与猜卡面排行榜一致的横向表格图片"""
        try:
            rows = rows[:max_rows]
            width = 850
            base_height = 250
            item_height = 70
            height = base_height + len(rows) * item_height

            bg_color_start = (230, 240, 255)
            bg_color_end = (200, 210, 240)
            img = Image.new("RGB", (width, height), bg_color_start)
            draw_bg = ImageDraw.Draw(img)
            for y in range(height):
                r = int(bg_color_start[0] + (bg_color_end[0] - bg_color_start[0]) * y / height)
                g = int(bg_color_start[1] + (bg_color_end[1] - bg_color_start[1]) * y / height)
                b = int(bg_color_start[2] + (bg_color_end[2] - bg_color_start[2]) * y / height)
                draw_bg.line([(0, y), (width, y)], fill=(r, g, b))

            if img.mode != 'RGBA':
                img = img.convert('RGBA')

            white_overlay = Image.new("RGBA", img.size, (255, 255, 255, 100))
            img = Image.alpha_composite(img, white_overlay)

            title_text = title_text or "猜歌排行榜"
            font_color = (30, 30, 50)
            shadow_color = (180, 180, 190, 128)
            header_color = (80, 90, 120)
            score_color = (235, 120, 20)
            accuracy_color = (0, 128, 128)
            try:
                font_path = self.resources_dir / "font.ttf"
                title_font = ImageFont.truetype(str(font_path), 48)
                header_font = ImageFont.truetype(str(font_path), 28)
                body_font = ImageFont.truetype(str(font_path), 26)
                id_font = ImageFont.truetype(str(font_path), 16)
                medal_font = ImageFont.truetype(str(font_path), 36)
            except IOError:
                title_font, header_font, body_font, id_font = [ImageFont.load_default()] * 4
                medal_font = body_font

            with Pilmoji(img) as pilmoji:
                center_x, title_y = int(width / 2), 80
                pilmoji.text((center_x + 2, title_y + 2), title_text, font=title_font, fill=shadow_color, anchor="mm", emoji_position_offset=(0, 6))
                pilmoji.text((center_x, title_y), title_text, font=title_font, fill=font_color, anchor="mm", emoji_position_offset=(0, 6))

                headers = ["排名", "玩家", "总分", "正确率", "总次数"]
                col_positions_header = [40, 150, 500, 610, 720]
                current_y = title_y + int(pilmoji.getsize(title_text, font=title_font)[1] / 2) + 45
                for header in headers:
                    pilmoji.text((col_positions_header.pop(0), current_y), header, font=header_font, fill=header_color)

                current_y += 55
                rank_icons = ["🥇", "🥈", "🥉"]

                for i, row in enumerate(rows):
                    user_id, user_name, score, attempts, correct_attempts = str(row[0]), row[1], str(row[2]), str(row[3]), row[4]
                    is_unbound_official = bool(row[5]) if len(row) > 5 else False
                    display_name = user_name if user_name else "未知用户"
                    if attempts == "-1":
                        accuracy = "—"
                        attempts_str = "—"
                    else:
                        attempts_str = attempts
                        accuracy = f"{(correct_attempts * 100 / int(attempts) if int(attempts) > 0 else 0):.1f}%"

                    rank = i + 1
                    col_positions = [40, 150, 500, 610, 720]
                    rank_num_align_x = 130
                    pilmoji.text((rank_num_align_x, current_y), str(rank), font=body_font, fill=font_color, anchor="ra")

                    if i < 3:
                        pilmoji.text((col_positions[0], current_y - 30), rank_icons[i], font=medal_font, fill=font_color)

                    name_x = col_positions[1]
                    if is_unbound_official:
                        badge_text = "未绑定QQ"
                        badge_text_width = pilmoji.getsize(badge_text, font=id_font)[0]
                        badge_width = badge_text_width + 16
                        badge_height = 26
                        badge_y = current_y + 3
                        draw = ImageDraw.Draw(img)
                        draw.rounded_rectangle(
                            [name_x, badge_y, name_x + badge_width, badge_y + badge_height],
                            radius=8,
                            fill=(115, 125, 150, 230),
                        )
                        pilmoji.text(
                            (name_x + 8, badge_y + 4),
                            badge_text,
                            font=id_font,
                            fill=(255, 255, 255, 255),
                        )
                        name_x += badge_width + 10

                    max_name_width = col_positions[2] - name_x - 20
                    if body_font.getbbox(display_name)[2] > max_name_width:
                        while body_font.getbbox(display_name + "...")[2] > max_name_width and len(display_name) > 0:
                            display_name = display_name[:-1]
                        display_name += "..."

                    pilmoji.text((name_x, current_y), display_name, font=body_font, fill=font_color)
                    id_text = f"{user_name} ID: {user_id}"
                    max_id_width = col_positions[2] - col_positions[1] - 20
                    if id_font.getbbox(id_text)[2] > max_id_width:
                        while id_font.getbbox(id_text + "...")[2] > max_id_width and len(id_text) > 0:
                            id_text = id_text[:-1]
                        id_text += "..."
                    # ID 小字固定从玩家列左侧开始，不能跟随未绑定徽章后的名称位置右移。
                    pilmoji.text((col_positions[1], current_y + 32), id_text, font=id_font, fill=header_color)
                    pilmoji.text((col_positions[2], current_y), score, font=body_font, fill=score_color)
                    pilmoji.text((col_positions[3], current_y), accuracy, font=body_font, fill=accuracy_color)
                    pilmoji.text((col_positions[4], current_y), attempts_str, font=body_font, fill=font_color)

                    separator_y = current_y + 60
                    if i < len(rows) - 1:
                        draw = ImageDraw.Draw(img)
                        draw.line([(30, separator_y), (width - 30, separator_y)], fill=(200, 200, 210, 128), width=1)

                    current_y += 70

                footer_text = f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                footer_y = height - 25
                pilmoji.text((center_x, footer_y), footer_text, font=id_font, fill=header_color, anchor="ms")

            img_path = self.output_dir / f"song_ranking_{int(time.time())}.png"
            img.save(img_path)
            return str(img_path)
        except Exception as e:
            logger.error(f"生成猜歌排行榜图片时出错: {e}", exc_info=True)
            return None

    async def draw_help_image(self) -> Optional[str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, self._draw_help_image_sync)

    def _draw_help_image_sync(self) -> Optional[str]:
        try:
            width, height = 800, 900
            bg_color_start, bg_color_end = (230, 240, 255), (200, 210, 240)
            img = Image.new("RGB", (width, height), bg_color_start)
            draw_bg = ImageDraw.Draw(img)
            for y in range(height):
                r = int(bg_color_start[0] + (bg_color_end[0] - bg_color_start[0]) * y / height)
                g = int(bg_color_start[1] + (bg_color_end[1] - bg_color_start[1]) * y / height)
                b = int(bg_color_start[2] + (bg_color_end[2] - bg_color_start[2]) * y / height)
                draw_bg.line([(0, y), (width, y)], fill=(r, g, b))
            if img.mode != 'RGBA': img = img.convert('RGBA')
            white_overlay = Image.new("RGBA", img.size, (255, 255, 255, 100))
            img = Image.alpha_composite(img, white_overlay)
            font_color, shadow_color = (30, 30, 50), (180, 180, 190, 128)
            header_color = (80, 90, 120)
            try:
                font_path = str(self.resources_dir / "font.ttf")
                title_font = ImageFont.truetype(font_path, 48)
                section_font = ImageFont.truetype(font_path, 32)
                body_font = ImageFont.truetype(font_path, 24)
                id_font = ImageFont.truetype(font_path, 16)
            except IOError:
                title_font = ImageFont.load_default(size=48)
                section_font = ImageFont.load_default(size=32)
                body_font = ImageFont.load_default(size=24)
                id_font = ImageFont.load_default(size=16)

            help_text = (
                "--- PJSK猜歌帮助 ---\n\n"
                "🎵 基础指令\n"
                f"  `pjsk猜歌` / `猜歌` - {self.game_modes['normal']['name']} ({self.game_modes['normal']['score']}分)\n"
                f"  `pjsk猜歌 1` / `2倍速猜歌` - {self.game_modes['1']['name']} ({self.game_modes['1']['score']}分)\n"
                f"  `pjsk猜歌 2` / `倒放猜歌` - {self.game_modes['2']['name']} ({self.game_modes['2']['score']}分)\n\n"
                "🎲 高级指令\n"
                "  `随机猜歌` - 随机组合效果 (最高3分)\n\n"
                "📊 数据统计\n"
                "  `猜歌分数` - 查看自己的猜歌积分和排名\n"
                "  `猜歌绑定 QQ号` - QQ官方机器人绑定到你的QQ号，需发送“确认”\n"
                "  `群猜歌排行榜` - 查看本群猜歌排行榜\n"
                "  `本地猜歌排行榜` - 查看插件本地存储的猜歌排行榜\n"
                "  `查看统计` - 查看个人各题型正确率统计\n"
            )
            with Pilmoji(img) as pilmoji:
                center_x, current_y = width // 2, 80
                x_margin = 60
                line_height_body = 40
                line_height_section = 55
                lines = help_text.split('\n')
                title_text = lines[0].replace("---", "").strip()
                pilmoji.text((int(center_x) + 2, int(current_y) + 2), title_text, font=title_font, fill=shadow_color, anchor="mm")
                pilmoji.text((int(center_x), int(current_y)), title_text, font=title_font, fill=font_color, anchor="mm")
                current_y += 100
                for line in lines[2:]:
                    if not line.strip():
                        current_y += line_height_body // 2
                        continue

                    if line.startswith("🎵") or line.startswith("🎲") or line.startswith("📊"):
                        font = section_font
                        y_increment = line_height_section
                        text_to_draw = line.strip()
                    else:
                        font = body_font
                        y_increment = line_height_body
                        text_to_draw = line

                    pilmoji.text((x_margin, int(current_y)), text_to_draw, font=font, fill=font_color)
                    current_y += y_increment
                footer_text = f"GuessSong v{self.plugin_version}"
                pilmoji.text((int(center_x), height - 40), footer_text, font=id_font, fill=header_color, anchor="ms")
            img_path = self.output_dir / f"guess_song_help_{int(time.time())}.png"
            img.save(img_path)
            return str(img_path)
        except Exception as e:
            logger.error(f"生成帮助图片时出错: {e}", exc_info=True)
            return None

    def get_random_mode_config(self) -> Tuple[Dict, int, str, str]:
        combinations_by_score = self._precompute_random_combinations()
        if not combinations_by_score: return {}, 0, "", ""

        target_distribution = self._get_random_target_distribution(combinations_by_score)
        scores = list(target_distribution.keys())
        probabilities = list(target_distribution.values())
        target_score = random.choices(scores, weights=probabilities, k=1)[0]

        valid_combinations = combinations_by_score[target_score]
        chosen_processed_combo = random.choice(valid_combinations)

        combined_kwargs = chosen_processed_combo['final_kwargs']
        total_score = chosen_processed_combo['final_score']

        effect_names = [eff['name'] for eff in chosen_processed_combo['effects_list']]
        effect_names_display = sorted(list(set(effect_names)))
        speed_mult = combined_kwargs.get('speed_multiplier')
        has_reverse = 'reverse_audio' in combined_kwargs

        if speed_mult and has_reverse:
            effect_names_display = [n for n in effect_names_display if n not in ['倒放', '2倍速', '1.5倍速']]
            effect_names_display.append(f"倒放+{speed_mult}倍速组合(+1分)")

        mode_name_str = '+'.join(sorted([name.replace(' ver.', '') for name in effect_names if name != 'Off']))
        return combined_kwargs, total_score, "、".join(effect_names_display), mode_name_str

    def _precompute_random_combinations(self) -> Dict[int, List[Dict]]:
        combinations_by_score = defaultdict(list)
        playable_source_effects = []
        for effect in self.source_effects:
            playable_source_effects.append(effect)

        independent_options = []
        for effect in self.base_effects:
            independent_options.append([effect, {'name': 'Off', 'score': 0, 'kwargs': {}}])

        if not playable_source_effects:
            return {}

        for source_effect in playable_source_effects:
            for independent_choices in itertools.product(*independent_options):
                raw_combination = [source_effect] + [choice for choice in independent_choices if choice['score'] > 0]

                final_effects_list = []
                final_kwargs = {}
                base_score = 0

                for effect_template in raw_combination:
                    effect = {k: (v.copy() if isinstance(v, dict) else v) for k, v in effect_template.items()}

                    final_effects_list.append(effect)
                    final_kwargs.update(effect.get('kwargs', {}))
                    base_score += effect.get('score', 0)

                final_score = base_score

                processed_combo = {
                    'effects_list': final_effects_list,
                    'final_kwargs': final_kwargs,
                    'final_score': final_score,
                }
                combinations_by_score[final_score].append(processed_combo)
        return dict(combinations_by_score)

    def _get_random_target_distribution(self, combinations_by_score: Dict[int, list]) -> Dict[int, float]:
        if not combinations_by_score: return {}
        scores = sorted(combinations_by_score.keys())
        decay_factor = self.random_mode_decay_factor
        weights = [decay_factor ** score for score in scores]
        total_weight = sum(weights)
        if total_weight == 0:
            return {score: 1.0 / len(scores) for score in scores}
        probabilities = [w / total_weight for w in weights]
        return dict(zip(scores, probabilities))

    def _mode_display_name(self, mode_key: str) -> str:
        default_map = {"normal": "普通"}
        if mode_key in default_map: return default_map[mode_key]
        if mode_key.startswith("random_"):
            ids = mode_key.replace("random_", "").split('+')
            names = [self.game_effects.get(i, {}).get('name', i) for i in ids]
            return "随机-" + "+".join(names)
        return self.game_effects.get(mode_key, {}).get('name', mode_key)

    async def draw_personal_stats_image(
        self,
        user_name: str,
        server_stats: Optional[Dict],
        core_mode_stats: Dict,
        detailed_stats: List[Tuple[str, int, int, float]],
    ) -> Optional[str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self.executor,
            self._draw_personal_stats_image_sync,
            user_name,
            server_stats,
            core_mode_stats,
            detailed_stats
        )

    def _draw_personal_stats_image_sync(
        self,
        user_name: str,
        server_stats: Optional[Dict],
        core_mode_stats: Dict,
        detailed_stats: List[Tuple[str, int, int, float]],
    ) -> Optional[str]:
        try:
            width, height = 1000, 1200

            img = Image.new("RGBA", (width, height), (230, 240, 255)).convert("RGBA")

            white_overlay = Image.new("RGBA", img.size, (255, 255, 255, 185))
            img = Image.alpha_composite(img, white_overlay)

            font_path = self.resources_dir / "font.ttf"
            font_title = ImageFont.truetype(str(font_path), 38)
            font_subtitle = ImageFont.truetype(str(font_path), 24)
            font_header = ImageFont.truetype(str(font_path), 28)
            font_body_bold = ImageFont.truetype(str(font_path), 26)
            font_body = ImageFont.truetype(str(font_path), 24)
            font_footer = ImageFont.truetype(str(font_path), 16)

            c_title = (40, 45, 60)
            c_text = (50, 55, 70)
            c_highlight = (10, 130, 140)
            c_dim = (140, 145, 160)
            c_line = (200, 205, 215, 150)

            with Pilmoji(img) as pilmoji:
                draw = pilmoji.draw

                center_x, margin, y = width // 2, 100, 100

                pilmoji.text((center_x, y), f"{user_name} 的模式统计报告", font=font_title, fill=c_title, anchor="ms")
                y += 50

                if server_stats and server_stats.get('rank') is not None:
                    server_text = f"全服总排名: {server_stats.get('rank', 'N/A')}   |   总分数: {server_stats.get('total_score', 0)}"
                else:
                    server_text = "全服总排名: 暂无数据"
                pilmoji.text((center_x, y), server_text, font=font_subtitle, fill=c_text, anchor="ms")
                y += 50
                draw.line([(margin, y), (width - margin, y)], fill=c_line, width=2)
                y += 35

                x_name, x_rank, x_percent, x_counts = margin, 500, 750, width - margin

                pilmoji.text((x_name, y), "核心模式表现", font=font_header, fill=c_title, anchor="ls")
                pilmoji.text((x_rank, y), "全服排名", font=font_header, fill=c_title, anchor="ms")
                y += 55

                for name, data in core_mode_stats.items():
                    total, correct, rank = data.get('total', 0), data.get('correct', 0), data.get('rank')
                    accuracy = (correct * 100 / total) if total > 0 else 0

                    pilmoji.text((x_name, y), name, font=font_body_bold, fill=c_text, anchor="ls")
                    if rank:
                        pilmoji.text((x_rank, y), f"#{rank}", font=font_body_bold, fill=c_text, anchor="ms")
                    else:
                        pilmoji.text((x_rank, y), "未上榜", font=font_body, fill=c_dim, anchor="ms")

                    pilmoji.text((x_counts, y), f"({correct}/{total})", font=font_body, fill=c_dim, anchor="rs")
                    pilmoji.text((x_percent, y), f"{accuracy:.1f}%", font=font_body_bold, fill=c_highlight, anchor="rs")
                    y += 48

                y += 20
                draw.line([(margin, y), (width - margin, y)], fill=c_line, width=2)
                y += 35

                pilmoji.text((x_name, y), "详细模式统计 (按正确率排序)", font=font_header, fill=c_title, anchor="ls")
                y += 50
                x_detail_name = x_name + 20

                for name, total, correct, accuracy in detailed_stats[:15]:
                    if y > height - 90: break

                    max_width = x_percent - x_detail_name - 30
                    display_name = name
                    if pilmoji.getsize(display_name, font=font_body)[0] > max_width:
                        while pilmoji.getsize(display_name + "...", font=font_body)[0] > max_width and len(display_name) > 1:
                            display_name = display_name[:-1]
                        display_name += "..."

                    pilmoji.text((x_detail_name, y), f"· {display_name}", font=font_body, fill=c_text, anchor="ls")
                    pilmoji.text((x_counts, y), f"({correct}/{total})", font=font_body, fill=c_dim, anchor="rs")
                    pilmoji.text((x_percent, y), f"{accuracy:.1f}%", font=font_body_bold, fill=c_highlight, anchor="rs")
                    y += 42

                footer_text = f"GuessSong v{self.plugin_version} |"
                pilmoji.text((center_x, height - 65), footer_text, font=font_footer, fill=c_dim, anchor="ms")

            output_path = self.output_dir / f"personal_stats_{user_name}_{int(time.time())}.png"
            img.save(output_path)
            return str(output_path)

        except Exception as e:
            logger.error(f"生成个人统计图片时出错: {e}", exc_info=True)
            return None

    async def terminate(self):
        self.executor.shutdown(wait=False)
        if self._session and not self._session.closed:
            await self._session.close()
