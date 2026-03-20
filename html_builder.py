from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .constants import (
    RANKING_CARD_WIDTH,
    SIGN_CARD_HEIGHT,
    SIGN_CARD_WIDTH,
    SYSTEM_FONT_STACK,
)

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


@lru_cache(maxsize=16)
def _load_template(name: str) -> str:
    return (_TEMPLATE_DIR / name).read_text(encoding="utf-8")


def _render_template(name: str, replacements: dict[str, str]) -> str:
    content = _load_template(name)
    for key, value in replacements.items():
        content = content.replace(f"[[{key}]]", value)
    return content


def _build_crown_html(rank: int) -> str:
    if rank != 1:
        return ""
    return _load_template("crown_badge.html")


def build_avatar_html(
    *,
    css_class: str,
    avatar_url: str,
    fallback_text: str,
) -> str:
    if avatar_url:
        return _render_template(
            "avatar_image.html",
            {
                "CSS_CLASS": css_class,
                "AVATAR_URL": avatar_url,
            },
        )

    return _render_template(
        "avatar_fallback.html",
        {
            "CSS_CLASS": css_class,
            "FALLBACK_TEXT": fallback_text,
        },
    )


def build_sign_card_html(
    *,
    bg_url: str,
    avatar_html: str,
    safe_nickname: str,
    safe_hour_word: str,
    safe_coin: str,
    safe_level: str,
    safe_total: str,
    total_days: int,
    continuous_days: int,
    impression_text: str,
    target_score_text: str,
    progress_percent: float,
    safe_bonus: str,
    safe_date: str,
    safe_saying: str,
    safe_footer: str,
) -> str:
    return _render_template(
        "sign_card.html",
        {
            "SIGN_CARD_WIDTH": str(SIGN_CARD_WIDTH),
            "SIGN_CARD_HEIGHT": str(SIGN_CARD_HEIGHT),
            "SYSTEM_FONT_STACK": SYSTEM_FONT_STACK,
            "BG_URL": bg_url,
            "AVATAR_HTML": avatar_html,
            "NICKNAME": safe_nickname,
            "HOUR_WORD": safe_hour_word,
            "COIN_TEXT": safe_coin,
            "LEVEL_TEXT": safe_level,
            "TOTAL_TEXT": safe_total,
            "TOTAL_DAYS": str(total_days),
            "CONTINUOUS_DAYS": str(continuous_days),
            "IMPRESSION_TEXT": impression_text,
            "TARGET_SCORE_TEXT": target_score_text,
            "PROGRESS_PERCENT": f"{progress_percent:.2f}",
            "BONUS_TEXT": safe_bonus,
            "DATE_TEXT": safe_date,
            "SAYING_TEXT": safe_saying,
            "FOOTER_TEXT": safe_footer,
        },
    )


def build_ranking_card_html(
    *,
    canvas_height: int,
    safe_title: str,
    safe_updated: str,
    top_html: str,
    rows_html: str,
    safe_footer: str,
) -> str:
    return _render_template(
        "ranking_card.html",
        {
            "RANKING_CARD_WIDTH": str(RANKING_CARD_WIDTH),
            "CANVAS_HEIGHT": str(canvas_height),
            "SYSTEM_FONT_STACK": SYSTEM_FONT_STACK,
            "TITLE": safe_title,
            "UPDATED_TEXT": safe_updated,
            "TOP_ITEMS": top_html,
            "ROWS": rows_html,
            "FOOTER_TEXT": safe_footer,
        },
    )


def build_ranking_top_item_html(
    *,
    rank: int,
    avatar_html: str,
    safe_name: str,
    safe_attitude: str,
    safe_score: str,
) -> str:
    return _render_template(
        "ranking_top_item.html",
        {
            "RANK": str(rank),
            "AVATAR_HTML": avatar_html,
            "CROWN_HTML": _build_crown_html(rank),
            "NAME": safe_name,
            "ATTITUDE": safe_attitude,
            "SCORE": safe_score,
        },
    )


def build_ranking_row_html(
    *,
    rank: int,
    avatar_html: str,
    safe_name: str,
    safe_attitude: str,
    safe_progress: str,
    ratio_percent: float,
) -> str:
    return _render_template(
        "ranking_row.html",
        {
            "RANK": str(rank),
            "AVATAR_HTML": avatar_html,
            "NAME": safe_name,
            "ATTITUDE": safe_attitude,
            "PROGRESS_TEXT": safe_progress,
            "RATIO_PERCENT": f"{ratio_percent:.2f}",
        },
    )
