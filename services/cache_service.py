import asyncio
import os
import io
import json
import time
from pathlib import Path
from typing import List, Dict, Optional
from PIL import Image
import aiohttp
from astrbot.api import logger
from astrbot.api import AstrBotConfig


class CacheService:
    def __init__(self, resources_dir: Path, output_dir: Path, config: AstrBotConfig):
        self.resources_dir = resources_dir
        self.output_dir = output_dir
        self.config = config

        os.makedirs(self.output_dir, exist_ok=True)

        self.remote_resource_url_base = self.config.get("remote_resource_url_base", "https://storage.exmeaning.com/sekai-jp-assets").strip('/')

        self.song_data: List[Dict] = []

    async def load_resources_and_manifest(self):
        if not self._load_song_data():
            logger.error("核心数据文件加载失败，插件将无法正常工作。")

    def _load_song_data(self) -> bool:
        try:
            songs_file = self.resources_dir / "guess_song.json"
            with open(songs_file, "r", encoding="utf-8") as f:
                self.song_data = json.load(f)
            return True
        except FileNotFoundError as e:
            logger.error(f"加载歌曲数据失败: {e}. 请确保 'guess_song.json' 在 'resources' 目录中。")
            return False
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"加载或解析歌曲数据失败: {e}")
            return False

    async def periodic_cleanup_task(self):
        cleanup_interval_seconds = 3600
        while True:
            await asyncio.sleep(cleanup_interval_seconds)
            logger.info("开始周期性清理 output 目录...")
            self.cleanup_output_dir()

    def cleanup_output_dir(self, max_age_seconds: int = 3600):
        if not self.output_dir.exists(): return
        now = time.time()
        for filename in os.listdir(self.output_dir):
            file_path = self.output_dir / filename
            if file_path.is_file() and (file_path.suffix in ['.png', '.wav', '.mp3']):
                if (now - file_path.stat().st_mtime) > max_age_seconds:
                    os.remove(file_path)
                    logger.info(f"已清理旧的输出文件: {filename}")

    def get_resource_url(self, relative_path: str) -> Optional[str]:
        if not self.remote_resource_url_base:
            logger.error("远程资源服务器地址未设置 (remote_resource_url_base)。")
            return None
        return f"{self.remote_resource_url_base}/{'/'.join(Path(relative_path).parts)}"

    async def open_image(self, relative_path: str) -> Optional[Image.Image]:
        url = self.get_resource_url(relative_path)
        if not url: return None

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    response.raise_for_status()
                    image_data = await response.read()
                    return Image.open(io.BytesIO(image_data))
        except Exception as e:
            logger.error(f"无法从远程获取图片资源 {url}: {e}", exc_info=True)
            return None

    def find_song_by_query(self, query: str) -> Optional[Dict]:
        if query.isdigit():
            return next((s for s in self.song_data if s['id'] == int(query)), None)
        else:
            query_lower = query.lower()
            found_songs = [s for s in self.song_data if query_lower in s['title'].lower()]
            if not found_songs: return None

            exact_match = next((s for s in found_songs if s['title'].lower() == query_lower), None)
            return exact_match or min(found_songs, key=lambda s: len(s['title']))

    async def terminate(self):
        pass
