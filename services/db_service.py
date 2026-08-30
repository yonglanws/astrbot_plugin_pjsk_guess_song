import asyncio
import aiosqlite
import json
from typing import List, Dict, Optional, Tuple
from datetime import datetime
from astrbot.api import logger

DEFAULT_PLATFORM_NAME = "aiocqhttp"
OFFICIAL_PLATFORM_NAME = "qq_official"

class DBService:
    DEFAULT_PLATFORM_NAME = DEFAULT_PLATFORM_NAME
    OFFICIAL_PLATFORM_NAME = OFFICIAL_PLATFORM_NAME

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._write_lock = asyncio.Lock()

    def _get_conn(self) -> aiosqlite.Connection:
        """
        [正确模式] 返回一个 aiosqlite 连接对象（Awaitable），
        由调用方的 `async with` 来管理其生命周期。
        """
        return aiosqlite.connect(self.db_path)

    async def _ensure_user_exists(
        self,
        cursor: aiosqlite.Cursor,
        user_id: str,
        user_name: str,
        platform_name: str = DEFAULT_PLATFORM_NAME,
    ):
        """确保用户在数据库中存在，如果不存在则创建。"""
        await cursor.execute(
            "SELECT 1 FROM user_stats WHERE user_id = ? AND platform_name = ?",
            (user_id, platform_name),
        )
        if await cursor.fetchone() is None:
            today = datetime.now().strftime("%Y-%m-%d")
            columns = [
                "user_id", "user_name", "platform_name", "score", "attempts", "correct_attempts",
                "daily_games_played", "last_played_date", "daily_listen_songs",
                "last_listen_date", "correct_streak", "max_correct_streak", "group_scores"
            ]
            default_values = (user_id, user_name, platform_name, 0, 0, 0, 0, today, 0, today, 0, 0, '{}')
            placeholders = ','.join(['?'] * len(columns))
            await cursor.execute(f"INSERT INTO user_stats ({', '.join(columns)}) VALUES ({placeholders})", default_values)

    async def init_db(self):
        """初始化数据库，创建并迁移表结构。"""
        async with self._get_conn() as conn:
            # 在user_stats表中将user_id设为主键，以保证数据的唯一性。
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_stats (
                    user_id TEXT PRIMARY KEY, user_name TEXT,
                    platform_name TEXT NOT NULL DEFAULT 'aiocqhttp', score INTEGER DEFAULT 0,
                    attempts INTEGER DEFAULT 0, correct_attempts INTEGER DEFAULT 0,
                    last_played_date TEXT, daily_games_played INTEGER DEFAULT 0,
                    last_listen_date TEXT, daily_listen_songs INTEGER DEFAULT 0,
                    group_scores TEXT DEFAULT '{}', correct_streak INTEGER DEFAULT 0,
                    max_correct_streak INTEGER DEFAULT 0
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS mode_stats (
                    mode TEXT PRIMARY KEY, total_attempts INTEGER DEFAULT 0, correct_attempts INTEGER DEFAULT 0
                )
            """)

            # --- 安全地添加新列以实现平滑升级 ---
            try:
                await conn.execute("ALTER TABLE user_stats ADD COLUMN group_daily_plays TEXT DEFAULT '{}'")
                logger.info("数据库迁移：成功为 'user_stats' 表添加 'group_daily_plays' 列。")
            except aiosqlite.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise

            try:
                await conn.execute(
                    "ALTER TABLE user_stats ADD COLUMN platform_name TEXT NOT NULL DEFAULT 'aiocqhttp'"
                )
            except aiosqlite.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS account_bindings (
                    official_platform TEXT NOT NULL,
                    official_user_id TEXT NOT NULL,
                    qq_user_id TEXT NOT NULL,
                    bound_at TEXT NOT NULL,
                    PRIMARY KEY (official_platform, official_user_id)
                )
            """)
            async with conn.execute(
                "SELECT user_id FROM user_stats WHERE platform_name = ?",
                (self.DEFAULT_PLATFORM_NAME,),
            ) as legacy_cursor:
                for (legacy_user_id,) in await legacy_cursor.fetchall():
                    if len(str(legacy_user_id)) == 32 and all(
                        char in "0123456789abcdefABCDEF" for char in str(legacy_user_id)
                    ):
                        await conn.execute(
                            "UPDATE user_stats SET platform_name = ? WHERE user_id = ?",
                            (self.OFFICIAL_PLATFORM_NAME, legacy_user_id),
                        )

            await conn.commit()

    async def resolve_user_id(self, platform_name: str, user_id: str) -> str:
        """将已绑定的官方机器人 QID 解析为普通 QQ 号。"""
        if str(platform_name or self.DEFAULT_PLATFORM_NAME).strip().lower() != self.OFFICIAL_PLATFORM_NAME:
            return str(user_id)
        async with self._get_conn() as conn:
            async with conn.execute(
                "SELECT qq_user_id FROM account_bindings "
                "WHERE official_platform = ? AND official_user_id = ?",
                (self.OFFICIAL_PLATFORM_NAME, str(user_id)),
            ) as cursor:
                row = await cursor.fetchone()
        return str(row[0]) if row else str(user_id)

    @staticmethod
    def _merge_group_scores(target_value: str, source_value: str) -> str:
        try:
            target = json.loads(target_value or "{}")
        except (TypeError, json.JSONDecodeError):
            target = {}
        try:
            source = json.loads(source_value or "{}")
        except (TypeError, json.JSONDecodeError):
            source = {}
        if not isinstance(target, dict):
            target = {}
        if not isinstance(source, dict):
            source = {}
        for session_id, source_stat in source.items():
            if isinstance(source_stat, int):
                source_stat = {"score": source_stat, "attempts": 0, "correct_attempts": 0}
            if not isinstance(source_stat, dict):
                continue
            target_stat = target.get(session_id, {})
            if isinstance(target_stat, int):
                target_stat = {"score": target_stat, "attempts": 0, "correct_attempts": 0}
            if not isinstance(target_stat, dict):
                target_stat = {}
            target[session_id] = {
                "score": int(target_stat.get("score", 0) or 0) + int(source_stat.get("score", 0) or 0),
                "attempts": int(target_stat.get("attempts", 0) or 0) + int(source_stat.get("attempts", 0) or 0),
                "correct_attempts": int(target_stat.get("correct_attempts", 0) or 0) + int(source_stat.get("correct_attempts", 0) or 0),
            }
        return json.dumps(target, ensure_ascii=False)

    @staticmethod
    def _merge_group_daily_plays(target_value: str, source_value: str) -> str:
        try:
            target = json.loads(target_value or "{}")
        except (TypeError, json.JSONDecodeError):
            target = {}
        try:
            source = json.loads(source_value or "{}")
        except (TypeError, json.JSONDecodeError):
            source = {}
        if not isinstance(target, dict):
            target = {}
        if not isinstance(source, dict):
            source = {}
        for session_id, source_stat in source.items():
            target_stat = target.get(session_id, {})
            if not isinstance(target_stat, dict):
                target_stat = {}
            if not isinstance(source_stat, dict):
                continue
            if target_stat.get("date") == source_stat.get("date"):
                target[session_id] = {
                    "date": source_stat.get("date"),
                    "count": int(target_stat.get("count", 0) or 0) + int(source_stat.get("count", 0) or 0),
                }
            elif str(source_stat.get("date", "")) > str(target_stat.get("date", "")):
                target[session_id] = source_stat
        return json.dumps(target, ensure_ascii=False)

    async def bind_official_account(self, official_user_id: str, qq_user_id: str) -> bool:
        """合并官方机器人账号在本插件内的历史数据并记录绑定关系。"""
        source_id = str(official_user_id).strip()
        target_id = str(qq_user_id).strip()
        if not source_id or not target_id.isdigit() or not 5 <= len(target_id) <= 12 or source_id == target_id:
            return False

        async with self._get_conn() as conn:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                async with conn.execute(
                    "SELECT 1 FROM account_bindings WHERE official_platform = ? AND official_user_id = ?",
                    (self.OFFICIAL_PLATFORM_NAME, source_id),
                ) as cursor:
                    if await cursor.fetchone():
                        await conn.rollback()
                        return False

                columns = "user_name, score, attempts, correct_attempts, last_played_date, " \
                          "daily_games_played, group_scores, group_daily_plays"
                async with conn.execute(
                    f"SELECT {columns} FROM user_stats WHERE user_id = ?",
                    (source_id,),
                ) as cursor:
                    source_row = await cursor.fetchone()
                async with conn.execute(
                    f"SELECT {columns} FROM user_stats WHERE user_id = ?",
                    (target_id,),
                ) as cursor:
                    target_row = await cursor.fetchone()

                if source_row:
                    if target_row:
                        target_name, target_score, target_attempts, target_correct, target_date, target_daily, target_groups, target_group_daily = target_row
                        source_name, source_score, source_attempts, source_correct, source_date, source_daily, source_groups, source_group_daily = source_row
                        if str(source_date or "") > str(target_date or ""):
                            merged_date, merged_daily = source_date, source_daily
                        elif str(source_date or "") == str(target_date or ""):
                            merged_date, merged_daily = target_date, int(target_daily or 0) + int(source_daily or 0)
                        else:
                            merged_date, merged_daily = target_date, target_daily
                        await conn.execute(
                            "UPDATE user_stats SET user_name = ?, score = ?, attempts = ?, correct_attempts = ?, "
                            "last_played_date = ?, daily_games_played = ?, group_scores = ?, group_daily_plays = ? "
                            "WHERE user_id = ?",
                            (
                                target_name or source_name,
                                int(target_score or 0) + int(source_score or 0),
                                int(target_attempts or 0) + int(source_attempts or 0),
                                int(target_correct or 0) + int(source_correct or 0),
                                merged_date,
                                merged_daily,
                                self._merge_group_scores(target_groups, source_groups),
                                self._merge_group_daily_plays(target_group_daily, source_group_daily),
                                target_id,
                            ),
                        )
                    else:
                        await conn.execute(
                            "UPDATE user_stats SET user_id = ?, platform_name = ? WHERE user_id = ?",
                            (target_id, self.DEFAULT_PLATFORM_NAME, source_id),
                        )

                await conn.execute(
                    "INSERT INTO account_bindings (official_platform, official_user_id, qq_user_id, bound_at) VALUES (?, ?, ?, ?)",
                    (self.OFFICIAL_PLATFORM_NAME, source_id, target_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
                if source_row and target_row:
                    await conn.execute("DELETE FROM user_stats WHERE user_id = ?", (source_id,))
                await conn.commit()
                return True
            except Exception as exc:
                await conn.rollback()
                logger.error(f"绑定猜歌官方机器人账号失败: {exc}", exc_info=True)
                return False

    async def update_stats(
        self,
        session_id: str,
        user_id: str,
        user_name: str,
        score: int,
        correct: bool,
        platform_name: str = DEFAULT_PLATFORM_NAME,
    ):
        """串行更新用户和群统计，避免跨会话读改写丢失结果。"""
        async with self._write_lock:
            async with self._get_conn() as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.cursor() as cursor:
                    await self._ensure_user_exists(cursor, user_id, user_name, platform_name)
                    await cursor.execute(
                        "SELECT * FROM user_stats WHERE user_id = ? AND platform_name = ?",
                        (user_id, platform_name),
                    )
                    user_stats = await cursor.fetchone()

                    def safe_int(key: str) -> int:
                        try:
                            return int(user_stats[key] or 0)
                        except (KeyError, TypeError, ValueError):
                            logger.warning(
                                f"Corrupted data for user {user_id}, key '{key}'; treating as 0."
                            )
                            return 0

                    attempts = safe_int("attempts") + 1
                    correct_attempts = safe_int("correct_attempts") + (1 if correct else 0)
                    current_score = safe_int("score") + (int(score) if correct else 0)
                    correct_streak = safe_int("correct_streak") + 1 if correct else 0
                    max_correct_streak = max(safe_int("max_correct_streak"), correct_streak)

                    try:
                        group_scores = json.loads(user_stats["group_scores"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        group_scores = {}
                    group_stat = group_scores.get(
                        session_id, {"score": 0, "attempts": 0, "correct_attempts": 0}
                    )
                    if isinstance(group_stat, int):
                        group_stat = {"score": group_stat, "attempts": 0, "correct_attempts": 0}
                    group_stat["score"] = int(group_stat.get("score", 0) or 0) + (int(score) if correct else 0)
                    group_stat["attempts"] = int(group_stat.get("attempts", 0) or 0) + 1
                    group_stat["correct_attempts"] = (
                        int(group_stat.get("correct_attempts", 0) or 0) + (1 if correct else 0)
                    )
                    group_scores[session_id] = group_stat

                    await cursor.execute(
                        """
                        UPDATE user_stats SET user_name = ?, platform_name = ?, score = ?, attempts = ?,
                                              correct_attempts = ?, correct_streak = ?,
                                              max_correct_streak = ?, group_scores = ?
                        WHERE user_id = ? AND platform_name = ?
                        """,
                        (
                            user_name,
                            platform_name,
                            current_score,
                            attempts,
                            correct_attempts,
                            correct_streak,
                            max_correct_streak,
                            json.dumps(group_scores),
                            user_id,
                            platform_name,
                        ),
                    )
                await conn.commit()

    async def consume_daily_play_attempt(
        self,
        user_id: str,
        user_name: str,
        session_id: str,
        is_independent: bool,
        platform_name: str = DEFAULT_PLATFORM_NAME,
        daily_limit: int | None = None,
    ) -> bool:
        """原子检查并消耗每日次数，返回是否成功取得一次游戏资格。"""
        async with self._write_lock:
            async with self._get_conn() as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.cursor() as cursor:
                    await self._ensure_user_exists(cursor, user_id, user_name, platform_name)
                    today = datetime.now().strftime("%Y-%m-%d")
                    await cursor.execute(
                        "SELECT daily_games_played, last_played_date, group_daily_plays "
                        "FROM user_stats WHERE user_id = ? AND platform_name = ?",
                        (user_id, platform_name),
                    )
                    row = await cursor.fetchone()

                    if is_independent:
                        try:
                            group_plays = json.loads(row["group_daily_plays"] or "{}")
                        except (TypeError, json.JSONDecodeError):
                            group_plays = {}
                        group_stat = group_plays.get(session_id, {})
                        current_count = (
                            int(group_stat.get("count", 0) or 0)
                            if group_stat.get("date") == today
                            else 0
                        )
                        if daily_limit is not None and daily_limit >= 0 and current_count >= daily_limit:
                            return False
                        group_plays[session_id] = {"count": current_count + 1, "date": today}
                        await cursor.execute(
                            "UPDATE user_stats SET group_daily_plays = ?, user_name = ? "
                            "WHERE user_id = ? AND platform_name = ?",
                            (json.dumps(group_plays), user_name, user_id, platform_name),
                        )
                    else:
                        current_count = (
                            int(row["daily_games_played"] or 0)
                            if row["last_played_date"] == today
                            else 0
                        )
                        if daily_limit is not None and daily_limit >= 0 and current_count >= daily_limit:
                            return False
                        await cursor.execute(
                            "UPDATE user_stats SET daily_games_played = ?, last_played_date = ?, user_name = ? "
                            "WHERE user_id = ? AND platform_name = ?",
                            (current_count + 1, today, user_name, user_id, platform_name),
                        )
                await conn.commit()
                return True

    async def can_play(
        self,
        user_id: str,
        daily_limit: int,
        session_id: str,
        is_independent: bool,
        platform_name: str = DEFAULT_PLATFORM_NAME,
    ) -> bool:
        """根据是否为独立限制模式，检查用户是否可以开始游戏。"""
        async with self._get_conn() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.cursor() as cursor:
                today = datetime.now().strftime("%Y-%m-%d")
                
                if is_independent:
                    await cursor.execute(
                        "SELECT group_daily_plays FROM user_stats WHERE user_id = ? AND platform_name = ?",
                        (user_id, platform_name),
                    )
                    row = await cursor.fetchone()
                    if not row or not row['group_daily_plays']:
                        return True
                    
                    group_plays = json.loads(row['group_daily_plays'])
                    group_stat = group_plays.get(session_id, {})
                    if group_stat.get('date') != today:
                        return True
                    return group_stat.get('count', 0) < daily_limit
                else:
                    await cursor.execute(
                        "SELECT daily_games_played, last_played_date FROM user_stats "
                        "WHERE user_id = ? AND platform_name = ?",
                        (user_id, platform_name),
                    )
                    row = await cursor.fetchone()
                    if not row or row['last_played_date'] != today:
                        return True
                    return (row['daily_games_played'] or 0) < daily_limit

    async def get_games_played_today(
        self,
        user_id: str,
        session_id: str,
        is_independent: bool,
        platform_name: str = DEFAULT_PLATFORM_NAME,
    ) -> int:
        """获取用户今天已玩的游戏次数，能自动处理独立模式和全局模式。"""
        async with self._get_conn() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.cursor() as cursor:
                today = datetime.now().strftime("%Y-%m-%d")
                if is_independent:
                    await cursor.execute(
                        "SELECT group_daily_plays FROM user_stats WHERE user_id = ? AND platform_name = ?",
                        (user_id, platform_name),
                    )
                    row = await cursor.fetchone()
                    if not row or not row['group_daily_plays']: return 0
                    
                    group_plays = json.loads(row['group_daily_plays'])
                    group_stat = group_plays.get(session_id, {})
                    return group_stat.get('count', 0) if group_stat.get('date') == today else 0
                else:
                    await cursor.execute(
                        "SELECT daily_games_played, last_played_date FROM user_stats "
                        "WHERE user_id = ? AND platform_name = ?",
                        (user_id, platform_name),
                    )
                    row = await cursor.fetchone()
                    if not row or row['last_played_date'] != today: return 0
                    return row['daily_games_played'] or 0

    async def get_user_local_global_stats(
        self,
        user_id: str,
        platform_name: str = DEFAULT_PLATFORM_NAME,
    ) -> Optional[Dict]:
        async with self._get_conn() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT * FROM user_stats WHERE user_id = ? AND platform_name = ?",
                    (user_id, platform_name),
                )
                row = await cursor.fetchone()
                if not row: return None

                score = row['score'] or 0
                # 排名是本插件内的统一排行榜；平台字段只负责定位当前用户记录。
                await cursor.execute(
                    "SELECT COUNT(1) + 1 FROM user_stats WHERE score > ?",
                    (score,),
                )
                rank_row = await cursor.fetchone()
                
                return {
                    'score': score, 'attempts': row['attempts'], 'correct': row['correct_attempts'],
                    'daily_plays': row['daily_games_played'] if row and row['last_played_date'] == datetime.now().strftime("%Y-%m-%d") else 0,
                    'last_play_date': row['last_played_date'], 'rank': rank_row[0] if rank_row else 1
                }
    
    async def reset_guess_limit(
        self,
        target_id: str,
        platform_name: str = DEFAULT_PLATFORM_NAME,
    ) -> bool:
        async with self._get_conn() as conn:
            res = await conn.execute(
                "UPDATE user_stats SET daily_games_played = 0 WHERE user_id = ? AND platform_name = ?",
                (target_id, platform_name),
            )
            await conn.commit()
            return res.rowcount > 0

    async def get_all_user_stats(self) -> List[Tuple]:
        async with self._get_conn() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT user_id, user_name, score FROM user_stats WHERE score > 0")
                return await cursor.fetchall()
    
    async def get_group_ranking(self, session_id: str) -> List[Tuple]:
        async with self._get_conn() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT user_id, user_name, platform_name, group_scores FROM user_stats")
                all_users_data = await cursor.fetchall()
            
            group_ranking = []
            for row in all_users_data:
                group_scores_str = row['group_scores'] if 'group_scores' in row.keys() else None
                if not group_scores_str: continue
                try:
                    group_scores = json.loads(group_scores_str)
                    group_stat_raw = group_scores.get(session_id)
                    if not group_stat_raw: continue
                    
                    if isinstance(group_stat_raw, int):
                        score, attempts, correct_attempts = group_stat_raw, -1, -1
                    else:
                        score = group_stat_raw.get("score", 0)
                        attempts = group_stat_raw.get("attempts", 0)
                        correct_attempts = group_stat_raw.get("correct_attempts", 0)
                    
                    if score > 0:
                        is_unbound_official = (
                            row['platform_name'] == self.OFFICIAL_PLATFORM_NAME
                            and not await self._is_bound_official_account(row['user_id'])
                        )
                        group_ranking.append((row['user_id'], row['user_name'], score, attempts, correct_attempts, is_unbound_official))
                except json.JSONDecodeError:
                    continue
            
            group_ranking.sort(key=lambda x: x[2], reverse=True)
            return group_ranking
            
    async def get_global_ranking_data(self) -> List[Tuple]:
        async with self._get_conn() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    SELECT user_id, user_name, score as total_score, attempts as total_attempts,
                           correct_attempts as total_correct, platform_name
                    FROM user_stats ORDER BY total_score DESC LIMIT 20
                """)
                rows = await cursor.fetchall()
        result = []
        for row in rows:
            is_unbound_official = (
                row[5] == self.OFFICIAL_PLATFORM_NAME
                and not await self._is_bound_official_account(row[0])
            )
            result.append((row[0], row[1], row[2], row[3], row[4], is_unbound_official))
        return result

    async def _is_bound_official_account(self, user_id: str) -> bool:
        async with self._get_conn() as conn:
            async with conn.execute(
                "SELECT 1 FROM account_bindings WHERE official_platform = ? AND official_user_id = ?",
                (self.OFFICIAL_PLATFORM_NAME, str(user_id)),
            ) as cursor:
                return await cursor.fetchone() is not None

    async def get_user_stats_in_group(
        self,
        user_id_to_find: str,
        session_id: str,
        platform_name: str = DEFAULT_PLATFORM_NAME,
    ) -> Optional[Dict]:
        full_ranking = await self.get_group_ranking(session_id)
        for i, (user_id, _, score, attempts, correct_attempts, *_) in enumerate(full_ranking):
            if user_id == user_id_to_find:
                return {"score": score, "rank": i + 1, "attempts": attempts, "correct_attempts": correct_attempts}
        
        async with self._get_conn() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT group_scores FROM user_stats WHERE user_id = ? AND platform_name = ?",
                    (user_id_to_find, platform_name),
                )
                row = await cursor.fetchone()
            if row and 'group_scores' in row.keys() and row['group_scores']:
                try:
                    group_scores = json.loads(row['group_scores'])
                    stat = group_scores.get(session_id)
                    if isinstance(stat, dict):
                        return {"score": stat.get("score", 0), "rank": None, "attempts": stat.get("attempts", 0), "correct_attempts": stat.get("correct_attempts", 0)}
                    elif isinstance(stat, int):
                         return {"score": stat, "rank": None, "attempts": -1, "correct_attempts": -1}
                except json.JSONDecodeError:
                    pass
        return {"score": 0, "rank": None, "attempts": 0, "correct_attempts": 0}

    async def update_mode_stats(self, mode: str, correct: bool):
        async with self._get_conn() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT total_attempts, correct_attempts FROM mode_stats WHERE mode = ?", (mode,))
                row = await cursor.fetchone()
                if row:
                    await cursor.execute("UPDATE mode_stats SET total_attempts = ?, correct_attempts = ? WHERE mode = ?", 
                                         ((row['total_attempts'] or 0) + 1, (row['correct_attempts'] or 0) + (1 if correct else 0), mode))
                else:
                    await cursor.execute("INSERT INTO mode_stats (mode, total_attempts, correct_attempts) VALUES (?, ?, ?)",
                                         (mode, 1, 1 if correct else 0))
                await conn.commit()
    
    async def get_mode_stats(self) -> List[Tuple]:
        async with self._get_conn() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT mode, total_attempts, correct_attempts FROM mode_stats")
                return await cursor.fetchall()
    
    async def reset_mode_stats(self):
        async with self._get_conn() as conn:
            await conn.execute("DELETE FROM mode_stats")
            await conn.commit()
