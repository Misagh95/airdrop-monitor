"""
استخراج توییت‌های جدید از اکانت‌های توییتر/X
با استفاده از GraphQL API توییتر و Guest Token

این روش رایگان و بدون نیاز به کلید API است.
بر اساس همان مکانیزمی که خود توییتر/X در مرورگر استفاده می‌کند.
"""
import re
import json
import logging
import asyncio
import urllib.parse
from datetime import datetime

import aiohttp

logger = logging.getLogger(__name__)

# این Bearer Token عمومی توییتر است (در کلاینت وب استفاده می‌شود)
BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Authorization": f"Bearer {BEARER_TOKEN}",
    "x-twitter-active-user": "yes",
    "x-twitter-client-language": "en",
}

# Cache برای user_id (نام اکانت → ID)
_user_id_cache: dict[str, str] = {}


GRAPHQL_FEATURES = {
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}


def _clean_tweet_text(text: str) -> str:
    """پاکسازی متن توییت."""
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def _get_guest_token(session: aiohttp.ClientSession) -> str:
    """دریافت Guest Token تازه از توییتر."""
    async with session.post(
        "https://api.twitter.com/1.1/guest/activate.json"
    ) as resp:
        data = await resp.json()
        return data["guest_token"]


async def _graphql_get(session: aiohttp.ClientSession, url: str, guest_token: str) -> tuple[dict, str]:
    """
    انجام درخواست GraphQL با مدیریت خطای guest token.
    خروجی: (data, valid_guest_token)
    در صورت rate-limit، تا ۴ بار با تأخیر تصاعدی امتحان می‌کند.
    """
    max_retries = 4
    delay = 5

    for attempt in range(max_retries):
        headers = {**HEADERS, "x-guest-token": guest_token}
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json(), guest_token

            if resp.status in (403, 429) and attempt < max_retries - 1:
                logger.debug(f"GraphQL {resp.status} (تلاش {attempt+1}/{max_retries}) — صبر {delay}s")
                await asyncio.sleep(delay)
                guest_token = await _get_guest_token(session)
                delay *= 2
                continue

            body = await resp.text()
            raise Exception(f"HTTP {resp.status}: {body[:100]}")

    raise Exception(f"بعد از {max_retries} تلاش ناموفق بود")


async def fetch_tweets(handle: str, rsshub_url: str = "", timeout: int = 20) -> list[dict]:
    """
    دریافت آخرین توییت‌های یک اکانت توییتر/X.

    از GraphQL API توییتر با Guest Token استفاده می‌کند (رایگان، بدون کلید).

    خروجی: [{id, text, link, timestamp}]
    """
    timeout_obj = aiohttp.ClientTimeout(total=timeout)

    async with aiohttp.ClientSession(timeout=timeout_obj, headers=HEADERS) as session:
        # 1. Guest token تازه
        guest_token = await _get_guest_token(session)

        # 2. User ID (با cache)
        if handle not in _user_id_cache:
            variables = json.dumps({
                "screen_name": handle,
                "withSafetyModeUserFields": True,
                "withSuperFollowsUserFields": True,
            })
            features = json.dumps({
                "responsive_web_twitter_blue_verified_badge_is_enabled": True,
            })
            url = (
                "https://api.twitter.com/graphql/xc8f1g7BYqr6VTzTbvNlGw/UserByScreenName?"
                f"variables={urllib.parse.quote(variables)}"
                f"&features={urllib.parse.quote(features)}"
            )
            data, guest_token = await _graphql_get(session, url, guest_token)

            user_result = data.get("data", {}).get("user", {}).get("result", {})
            user_id = user_result.get("rest_id", "")
            if not user_id:
                raise Exception(f"اکانت @{handle} پیدا نشد")
            _user_id_cache[handle] = user_id
            # بعد از UserByScreenName، guest token ممکن است نامعتبر شود
            await asyncio.sleep(1)
            guest_token = await _get_guest_token(session)

        user_id = _user_id_cache[handle]

        # 3. UserTweets
        variables = json.dumps({
            "userId": user_id,
            "count": 10,
            "includePromotedContent": False,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True,
            "withV2Timeline": True,
        })
        features = json.dumps(GRAPHQL_FEATURES)

        url = (
            "https://api.twitter.com/graphql/V7H0Ap3_Hh2FyS75OCDO3Q/UserTweets?"
            f"variables={urllib.parse.quote(variables)}"
            f"&features={urllib.parse.quote(features)}"
        )
        data, _ = await _graphql_get(session, url, guest_token)

    # 4. استخراج توییت‌ها
    instructions = (
        data.get("data", {})
        .get("user", {})
        .get("result", {})
        .get("timeline_v2", {})
        .get("timeline", {})
        .get("instructions", [])
    )

    tweets = []
    for inst in instructions:
        for entry in inst.get("entries", []):
            content = entry.get("content", {})
            if content.get("entryType") != "TimelineTimelineItem":
                continue

            tweet_result = (
                content.get("itemContent", {})
                .get("tweet_results", {})
                .get("result", {})
            )
            legacy = tweet_result.get("legacy", {})
            text = legacy.get("full_text", "")
            tid = legacy.get("id_str", "")

            if not text or not tid:
                continue
            if text.startswith("RT @"):  # رد کردن retweet
                continue

            text = _clean_tweet_text(text)

            # timestamp
            created_at = legacy.get("created_at", "")
            timestamp = ""
            if created_at:
                try:
                    dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
                    timestamp = dt.isoformat()
                except Exception:
                    pass

            tweets.append({
                "id": tid,
                "text": text,
                "link": f"https://x.com/{handle}/status/{tid}",
                "timestamp": timestamp,
            })

    return tweets
