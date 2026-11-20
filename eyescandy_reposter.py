import os
import json
import time
import logging
import tempfile
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse

from atproto import Client

# ============================================================
# EYESCANDY - REPOST ALWAYS ONLY
# ============================================================

USERNAME = "eyescandy.bsky.social"
PASSWORD = os.getenv("EYESCANDY_PASSWORD")
STATE_FILE = Path("eyescandy_state.json")

REPOST_ALWAYS_LIST = "https://bsky.app/profile/did:plc:sp54ouue6fp2dlvn2cux54ka/lists/3mncvshsuhi2d"

HOURS_BACK = 3
MAX_PER_USER = 3
MAX_PER_RUN = 100
AUTHOR_FEED_LIMIT = 100
SLEEP_SECONDS = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("eyescandy-repost-always")


def now_utc():
    return datetime.now(timezone.utc)


def parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def post_datetime(post):
    record = getattr(post, "record", None)
    created = (
        getattr(record, "created_at", None)
        or getattr(post, "indexed_at", None)
    )
    return parse_datetime(created)


def post_uri(post):
    return getattr(post, "uri", None)


def post_cid(post):
    return getattr(post, "cid", None)


def author_did(post):
    return getattr(getattr(post, "author", None), "did", None)


def has_media(post):
    embed = getattr(post, "embed", None)
    if not embed:
        return False

    if getattr(embed, "images", None) or getattr(embed, "video", None):
        return True

    media = getattr(embed, "media", None)
    if media and (
        getattr(media, "images", None)
        or getattr(media, "video", None)
    ):
        return True

    return False


def is_reply(post):
    return getattr(getattr(post, "record", None), "reply", None) is not None


def is_quote(post):
    embed = getattr(post, "embed", None)
    if not embed:
        return False

    embed_type = getattr(embed, "$type", "") or ""
    if "recordWithMedia" in embed_type:
        return getattr(embed, "record", None) is not None
    return "record" in embed_type


def is_repost_item(item):
    reason = getattr(item, "reason", None)
    return reason is not None and "repost" in reason.__class__.__name__.lower()


def suitable_post(post):
    return bool(
        post
        and has_media(post)
        and not is_reply(post)
        and not is_quote(post)
    )


def list_url_to_uri(url):
    parts = urlparse(url).path.strip("/").split("/")
    if len(parts) < 4:
        raise ValueError(f"Ongeldige Bluesky-lijstlink: {url}")
    return f"at://{parts[1]}/app.bsky.graph.list/{parts[3]}"


def load_state():
    # Keep compatibility with the previous Eyescandy state structure.
    state = {"normal_reposts": {}}

    if not STATE_FILE.exists():
        return state

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            saved = json.load(file)
        if isinstance(saved, dict):
            state.update(saved)
        if not isinstance(state.get("normal_reposts"), dict):
            state["normal_reposts"] = {}

        # Migrate entries made by the temporary simplified script.
        temporary_entries = saved.get("reposted", {}) if isinstance(saved, dict) else {}
        if isinstance(temporary_entries, dict):
            for uri, value in temporary_entries.items():
                state["normal_reposts"].setdefault(uri, value)
    except Exception:
        log.exception("Statebestand kon niet worden gelezen")
        raise

    return state


def save_state(state):
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(STATE_FILE.parent),
            delete=False,
        ) as temp:
            json.dump(state, temp, indent=2, ensure_ascii=False, sort_keys=True)
            temp_path = temp.name
        os.replace(temp_path, STATE_FILE)
    except Exception:
        log.exception("Statebestand opslaan mislukt")
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def fetch_list_members(client):
    list_uri = list_url_to_uri(REPOST_ALWAYS_LIST)
    members = []
    cursor = None

    while True:
        params = {"list": list_uri, "limit": 100}
        if cursor:
            params["cursor"] = cursor

        try:
            response = client.app.bsky.graph.get_list(params=params)
        except Exception:
            log.exception("Repost Always-lijst ophalen mislukt")
            raise

        for item in getattr(response, "items", []) or []:
            subject = getattr(item, "subject", None)
            did = getattr(subject, "did", None) if subject else None
            handle = getattr(subject, "handle", None) if subject else None
            if did:
                members.append({"did": did, "handle": handle})

        cursor = getattr(response, "cursor", None)
        if not cursor:
            break

    unique = {}
    for member in members:
        unique.setdefault(member["did"], member)

    return list(unique.values())


def fetch_author_posts(client, member):
    actor = member.get("handle") or member["did"]

    try:
        response = client.app.bsky.feed.get_author_feed({
            "actor": actor,
            "limit": AUTHOR_FEED_LIMIT,
            "filter": "posts_no_replies",
        })
        return [
            item.post
            for item in (getattr(response, "feed", []) or [])
            if not is_repost_item(item) and getattr(item, "post", None)
        ]
    except Exception:
        log.exception("Auteurfeed ophalen mislukt: %s", actor)
        return []


def collect_candidates(client, state):
    cutoff = now_utc().timestamp() - HOURS_BACK * 3600
    already_done = state["normal_reposts"]
    members = fetch_list_members(client)

    log.info("Repost Always: %s accounts in de lijst", len(members))

    candidates = {}

    for number, member in enumerate(members, start=1):
        actor = member.get("handle") or member["did"]
        log.info("Account %s/%s: @%s", number, len(members), actor)

        posts = fetch_author_posts(client, member)
        eligible_for_author = 0

        for post in posts:
            uri = post_uri(post)
            cid = post_cid(post)
            created = post_datetime(post)

            if not uri or not cid or not created:
                continue
            if created.timestamp() < cutoff:
                continue
            if uri in already_done:
                continue
            if not suitable_post(post):
                continue

            candidates[uri] = post
            eligible_for_author += 1
            if eligible_for_author >= MAX_PER_USER:
                break

        log.info(
            "@%s: %s recente geschikte post(s) gevonden",
            actor,
            eligible_for_author,
        )

    ordered = sorted(
        candidates.values(),
        key=lambda post: post_datetime(post) or datetime.min.replace(tzinfo=timezone.utc),
    )

    log.info("Totaal nieuwe kandidaten: %s", len(ordered))
    return ordered


def run_reposts(client, state, posts):
    counts = defaultdict(int)
    done = 0

    for post in posts:
        if done >= MAX_PER_RUN:
            break

        did = author_did(post)
        uri = post_uri(post)
        cid = post_cid(post)

        if not did or not uri or not cid:
            continue
        if counts[did] >= MAX_PER_USER:
            continue
        if uri in state["normal_reposts"]:
            continue

        handle = getattr(getattr(post, "author", None), "handle", did)

        try:
            client.repost(uri, cid)

            # Alleen opslaan nadat Bluesky de repost heeft bevestigd.
            state["normal_reposts"][uri] = now_utc().isoformat()
            save_state(state)

            counts[did] += 1
            done += 1
            log.info("REPOST GESLAAGD: @%s | %s", handle, uri)
            time.sleep(SLEEP_SECONDS)

        except Exception:
            log.exception("REPOST MISLUKT: @%s | %s", handle, uri)

    return done


def main():
    log.info("=== EYESCANDY REPOST ALWAYS START ===")

    if not PASSWORD:
        raise RuntimeError("EYESCANDY_PASSWORD ontbreekt in GitHub Secrets")

    state = load_state()
    client = Client()
    client.login(USERNAME, PASSWORD)
    log.info("Ingelogd als %s", USERNAME)

    candidates = collect_candidates(client, state)
    total = run_reposts(client, state, candidates)

    log.info("Repost Always klaar: %s nieuwe repost(s)", total)
    log.info("=== EYESCANDY REPOST ALWAYS KLAAR ===")


if __name__ == "__main__":
    main()