"""
استخراج توییت‌های جدید از اکانت‌های توییتر/X
با استفاده از GraphQL API توییتر و Guest Token

این روش رایگان و بدون نیاز به کلید API است.
بر اساس همان مکانیزمی که خود توییتر/X در مرورگر استفاده می‌کند.

قابلیت‌ها:
- چندین بار retry با تأخیر تصاعدی
- User-Agent واقعی مرورگر
- User-Agent چرخشی برای کاهش ریسک مسدودی
- کش user_id برای کاهش درخواست
"""

import re
import json
import random
import logging
import asyncio
import urllib.parse
from datetime import datetime

import aiohttp

logger = logging.getLogger(__name__)

# Bearer Token عمومی توییتر (در کلاینت وب استفاده می‌شود)
BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# چند User-Agent مختلف برای چرخش
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]


def _make_headers(guest_token: str = "") -> dict:
    """ساخت هدرهای واقعی مرورگر."""
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Authorization": f"Bearer {BEARER_TOKEN}",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "referer": "https://x.com/",
        "origin": "https://x.com",
    }
    if guest_token:
        headers["x-guest-token"] = guest_token
    return headers


# Cache برای user_id
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
    headers = _make_headers()
    async with session.post(
        "https://api.twitter.com/1.1/guest/activate.json",
        headers=headers,
    ) as resp:
        data = await resp.json()
        return data["guest_token"]


async def _graphql_get(session: aiohttp.ClientSession, url: str, guest_token: str) -> tuple[dict, str]:
    """
    انجام درخواست GraphQL با مدیریت خطای guest token.
    در صورت rate-limit، تا ۵ بار با تأخیر تصاعدی امتحان می‌کند.
    """
    max_retries = 5
    delay = 8

    for attempt in range(max_retries):
        headers = _make_headers(guest_token)
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json(), guest_token

            if resp.status in (403, 429) and attempt < max_retries - 1:
                logger.debug(
                    f"Twitter GraphQL {resp.status} "
                    f"(تلاش {attempt+1}/{max_retries}) — صبر {delay}s"
                )
                await asyncio.sleep(delay + random.uniform(0, 3))  # تأخیر تصادفی
                guest_token = await _get_guest_token(session)
                delay = min(delay * 2, 60)  # حداکثر ۶۰ ثانیه
                continue

            body = await resp.text()
            raise Exception(f"HTTP {resp.status}: {body[:80]}")

    raise Exception(f"بعد از {max_retries} تلاش ناموفق بود")


async def fetch_tweets(handle: str, rsshub_url: str = "", timeout: int = 45) -> list[dict]:
    """
    دریافت آخرین توییت‌های یک اکانت توییتر/X.

    خروجی: [{id, text, link, timestamp}]
    """
    timeout_obj = aiohttp.ClientTimeout(total=timeout)

    async with aiohttp.ClientSession(timeout=timeout_obj) as session:
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
            await asyncio.sleep(2)
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
            if text.startswith("RT @"):
                continue

            text = _clean_tweet_text(text)

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
