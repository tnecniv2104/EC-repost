import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from atproto import Client

# ============================================================
# EYESCANDY - REPOST ALWAYS
# ============================================================

USERNAME = "eyescandy.bsky.social"
PASSWORD = os.getenv("EYESCANDY_PASSWORD")
STATE_FILE = Path("eyescandy_state.json")

REPOST_ALWAYS_LIST = (
    "https://bsky.app/profile/did:plc:sp54ouue6fp2dlvn2cux54ka/"
    "lists/3mncvshsuhi2d"
)

LOOKBACK_HOURS = 3
MAX_PER_AUTHOR = 3
PAGE_SIZE = 100
SLEEP_SECONDS = 2

# Alleen waarschuwingen en fouten tonen.
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def now_utc():
    return datetime.now(timezone.utc)


def parse_datetime(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def post_datetime(post):
    record = getattr(post, "record", None)
    value = (
        getattr(record, "created_at", None)
        or getattr(post, "indexed_at", None)
    )
    return parse_datetime(value)


def post_uri(post):
    return getattr(post, "uri", None)


def is_repost_item(item):
    reason = getattr(item, "reason", None)
    return (
        reason is not None
        and "repost" in reason.__class__.__name__.lower()
    )


def is_reply(post):
    return (
        getattr(getattr(post, "record", None), "reply", None)
        is not None
    )


def is_quote(post):
    embed = getattr(post, "embed", None)
    if not embed:
        return False

    embed_type = getattr(embed, "$type", "") or ""

    if "recordWithMedia" in embed_type:
        return getattr(embed, "record", None) is not None

    return "record" in embed_type


def has_media(post):
    embed = getattr(post, "embed", None)
    if not embed:
        return False

    embed_type = getattr(embed, "$type", "") or ""

    if "record" in embed_type and "recordWithMedia" not in embed_type:
        return False

    if getattr(embed, "images", None) or getattr(embed, "video", None):
        return True

    media = getattr(embed, "media", None)

    return bool(
        media
        and (
            getattr(media, "images", None)
            or getattr(media, "video", None)
        )
    )


def is_normal_media_post(post):
    return bool(
        post
        and has_media(post)
        and not is_reply(post)
        and not is_quote(post)
    )


def list_url_to_uri(url):
    parts = urlparse(url).path.strip("/").split("/")

    if len(parts) < 4:
        raise ValueError("Ongeldige Repost Always-lijstlink")

    return f"at://{parts[1]}/app.bsky.graph.list/{parts[3]}"


def load_state():
    if not STATE_FILE.exists():
        return {"normal_reposts": {}}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as handle:
            state = json.load(handle)

        if not isinstance(state, dict):
            raise ValueError("Statebestand bevat geen JSON-object")

        if not isinstance(state.get("normal_reposts"), dict):
            state["normal_reposts"] = {}

        return state

    except Exception as exc:
        raise RuntimeError(
            f"Statebestand niet leesbaar: {exc}"
        ) from exc


def save_state(state):
    temporary = STATE_FILE.with_suffix(".json.tmp")

    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(
            state,
            handle,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )

    temporary.replace(STATE_FILE)


def fetch_list_members(client):
    list_uri = list_url_to_uri(REPOST_ALWAYS_LIST)

    members = {}
    cursor = None

    while True:
        params = {
            "list": list_uri,
            "limit": PAGE_SIZE,
        }

        if cursor:
            params["cursor"] = cursor

        response = client.app.bsky.graph.get_list(params=params)

        for item in getattr(response, "items", []) or []:
            subject = getattr(item, "subject", None)

            did = getattr(subject, "did", None) if subject else None
            handle = getattr(subject, "handle", None) if subject else None

            if did:
                members.setdefault(did, handle or did)

        cursor = getattr(response, "cursor", None)

        if not cursor:
            break

    return list(members.items())


def fetch_recent_media(client, actor, cutoff):
    selected = {}
    cursor = None

    while True:
        params = {
            "actor": actor,
            "limit": PAGE_SIZE,
        }

        if cursor:
            params["cursor"] = cursor

        response = client.app.bsky.feed.get_author_feed(
            params=params
        )

        feed = getattr(response, "feed", []) or []

        if not feed:
            break

        reached_cutoff = False

        for item in feed:
            if is_repost_item(item):
                continue

            post = getattr(item, "post", None)

            if not post:
                continue

            created = post_datetime(post)

            if created is None:
                continue

            if created < cutoff:
                reached_cutoff = True
                continue

            if not is_normal_media_post(post):
                continue

            uri = post_uri(post)

            if uri:
                selected[uri] = post

        # De feed is nieuwste eerst.
        # Zodra we ouder dan 3 uur zijn, stoppen we.
        if reached_cutoff:
            break

        cursor = getattr(response, "cursor", None)

        if not cursor:
            break

    return list(selected.values())


def main():
    if not PASSWORD:
        raise RuntimeError(
            "EYESCANDY_PASSWORD ontbreekt in GitHub Secrets"
        )

    state = load_state()

    client = Client()
    client.login(USERNAME, PASSWORD)

    cutoff = now_utc() - timedelta(hours=LOOKBACK_HOURS)

    members = fetch_list_members(client)

    candidates = {}
    author_counts = defaultdict(int)

    for did, handle in members:
        posts = fetch_recent_media(
            client,
            handle,
            cutoff,
        )

        # Maximaal drie nieuwste geschikte posts per auteur.
        posts.sort(
            key=lambda post: post_datetime(post) or cutoff,
            reverse=True,
        )

        for post in posts:
            if author_counts[did] >= MAX_PER_AUTHOR:
                break

            uri = post_uri(post)

            if not uri:
                continue

            if uri in state["normal_reposts"]:
                continue

            candidates[uri] = post
            author_counts[did] += 1

    # Oudste eerst verwerken.
    ordered = sorted(
        candidates.values(),
        key=lambda post: post_datetime(post) or now_utc(),
    )

    reposted = 0
    skipped_state = 0
    errors = 0

    for post in ordered:
        uri = post_uri(post)
        cid = getattr(post, "cid", None)

        # State opnieuw controleren voor de repost.
        if uri in state["normal_reposts"]:
            skipped_state += 1
            continue

        if not uri or not cid:
            errors += 1
            logging.error("Post mist URI of CID; overgeslagen")
            continue

        try:
            client.repost(uri, cid)

            # Alleen na succesvolle repost in state opslaan.
            state["normal_reposts"][uri] = now_utc().isoformat()
            save_state(state)

            reposted += 1
            time.sleep(SLEEP_SECONDS)

        except Exception as exc:
            errors += 1
            logging.exception(
                "Repost mislukt voor %s: %s",
                uri,
                exc,
            )

    print(
        "REPOST ALWAYS | "
        f"lijstaccounts: {len(members)} | "
        f"nieuwe kandidaten: {len(ordered)} | "
        f"reposts: {reposted} | "
        f"al in state: {skipped_state} | "
        f"fouten: {errors}"
    )


if __name__ == "__main__":
    main()