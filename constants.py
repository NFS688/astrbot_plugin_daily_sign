from __future__ import annotations

from pathlib import Path

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

# 业务常量：签到逻辑与好感度流程。
PLUGIN_NAME = "astrbot_plugin_daily_sign"
PLUGIN_VERSION = "0.0.1"
LOG_PREFIX = "[astrbot_plugin_daily_sign]"

DEFAULT_NICKNAME = "聊天用户"
DEFAULT_NEXT_SCORE = 25
DEFAULT_RANKING_LIMIT = 10
DEFAULT_ENABLE_CACHE_CLEANUP = False
DEFAULT_CACHE_CLEANUP_INTERVAL_DAYS = 7
DEFAULT_CACHE_CLEANUP_TIME = "03:00"
DEFAULT_CACHE_RETENTION_DAYS = 7

# 渲染常量：网页截图与卡片尺寸。
SIGN_CARD_WIDTH = 1280
SIGN_CARD_HEIGHT = 760
RANKING_CARD_WIDTH = 1080
RANKING_MIN_CARD_HEIGHT = 820
RANKING_BASE_CARD_HEIGHT = 520
RANKING_ROW_HEIGHT = 94

BG_URL = "https://v2.xxapi.cn/api/random4kPic?type=acg"
SYSTEM_FONT_STACK = (
    '"Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", '
    '"PingFang SC", "WenQuanYi Micro Hei", "Arial Unicode MS", sans-serif'
)
RENDER_DEVICE_SCALE_FACTOR = 1.5

# 路径常量。
PLUGIN_DIR = Path(__file__).resolve().parent
IMAGE_DIR = PLUGIN_DIR / "resources" / "images"
LOCAL_BG_DIR = PLUGIN_DIR / "resources" / "custombg"

PLUGIN_DATA_DIR = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
SIGN_DB_PATH = PLUGIN_DATA_DIR / "sign.db"
CACHE_CLEANUP_STATE_PATH = PLUGIN_DATA_DIR / "cache_cleanup_state.json"
