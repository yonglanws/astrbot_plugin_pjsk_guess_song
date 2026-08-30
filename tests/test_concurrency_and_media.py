import asyncio
import importlib
import sys
import types
from pathlib import Path


PLUGIN_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(PLUGIN_DIR / "services"))


def _install_astrbot_stub():
    logger = types.SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
    )
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logger
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api})


def _load_db_service():
    _install_astrbot_stub()
    sys.modules.pop("db_service", None)
    return importlib.import_module("db_service").DBService


def test_concurrent_stats_updates_keep_every_result(tmp_path):
    DBService = _load_db_service()
    db = DBService(str(tmp_path / "guess_song.db"))

    async def exercise():
        await db.init_db()
        await db.update_stats("group", "player", "Player", 1, True)
        await asyncio.gather(
            db.update_stats("group", "player", "Player", 4, True),
            db.update_stats("group", "player", "Player", 3, True),
        )
        return await db.get_user_local_global_stats("player")

    stats = asyncio.run(exercise())

    assert stats["score"] == 8
    assert stats["attempts"] == 3
    assert stats["correct"] == 3


def test_generated_output_names_are_unique():
    import uuid

    assert uuid.uuid4().hex != uuid.uuid4().hex
