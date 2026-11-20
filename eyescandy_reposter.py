import os
import json
import time
import random
import logging
import tempfile
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

from atproto import Client, models

# ============================================================
# EYESCANDY - COMPLETE REPOSTER
# Based on the proven HotBleusky processing order.
# ============================================================

USERNAME = "eyescandy.bsky.social"
PASSWORD = os.getenv("EYESCANDY_PASSWORD")
STATE_FILE = Path("eyescandy_state.json")

MAX_ACTIONS = 100
MAX_PER_USER = 3
NORMAL_LOOKBACK_HOURS = 3
MAX_AUTHOR_FEED_PAGES = 2
REPOST_ALWAYS_REBOOST_HOURS = 8
PROMO_MAX_AGE_HOURS = 12
PROMO_RANDOM_POOL = 25
PROMO_RANDOM_REUSE_HOURS = 24
SLEEP_SECONDS = 2

REDFOX_FEED = "https://bsky.app/profile/did:plc:cxrt7ggxkamgzxa47cggtees/feed/aaaoirmgh53zw"
REPOST_ALWAYS_LIST = "https://bsky.app/profile/did:plc:sp54ouue6fp2dlvn2cux54ka/lists/3mncvshsuhi2d"
PROMO_LAST_LIST = "https://bsky.app/profile/did:plc:sp54ouue6fp2dlvn2cux54ka/lists/3mncvroe6aj24"
PROMO_RANDOM_LIST = "https://bsky.app/profile/did:plc:sp54ouue6fp2dlvn2cux54ka/lists/3mncvr7dr3x2d"
HASHTAG_BLACKLIST_LIST = "https://bsky.app/profile/did:plc:sp54ouue6fp2dlvn2cux54ka/lists/3mnianivya72q"
HASHTAG = "#eyescandy"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("eyescandy-reposter")


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


def post_age_hours(post):
    created = post_datetime(post)
    if not created:
        return None
    return (now_utc() - created).total_seconds() / 3600


def age_ok(post, hours):
    age = post_age_hours(post)
    return age is not None and age <= hours


def get_post_uri(post):
    return getattr(post, "uri", None)


def get_post_cid(post):
    return getattr(post, "cid", None)


def get_author_did(post):
    return getattr(getattr(post, "author", None), "did", None)


def get_author_handle(post):
    return getattr(getattr(post, "author", None), "handle", "unknown")


def record_text(post):
    return getattr(getattr(post, "record", None), "text", "") or ""


def url_to_at_uri(url, collection):
    parts = urlparse(url).path.strip("/").split("/")
    if len(parts) < 4:
        raise ValueError("Ongeldige Bluesky-link: " + url)
    return f"at://{parts[1]}/{collection}/{parts[3]}"


def feed_url_to_uri(url):
    return url_to_at_uri(url, "app.bsky.feed.generator")


def list_url_to_uri(url):
    return url_to_at_uri(url, "app.bsky.graph.list")


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


def suitable_post(post, allow_replies=False):
    return bool(
        post
        and has_media(post)
        and not is_quote(post)
        and (allow_replies or not is_reply(post))
    )


def default_state():
    return {
        "normal_reposts": {},
        "repost_always_reboosted": {},
        "promo_random_used": {},
        "promo_last_used": {},
        "own_reboosted": {},
        "likes": {},
        "last_run": None,
    }


def load_state():
    state = default_state()
    if not STATE_FILE.exists():
        return state
    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            saved = json.load(file)
        state.update(saved)
    except Exception:
        log.exception("Statebestand kon niet worden gelezen; standaardstate gebruikt")
    return state


def save_state(state):
    temp_path = None
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
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
            try:
                os.remove(temp_path)
            except OSError:
                pass


class Reposter:
    def __init__(self, client, state):
        self.client = client
        self.state = state
        self.actions = 0
        self.author_counts = defaultdict(int)

    def can_act(self):
        return self.actions < MAX_ACTIONS

    def author_allowed(self, post):
        did = get_author_did(post)
        return bool(did and self.author_counts[did] < MAX_PER_USER)

    def register_author(self, post):
        did = get_author_did(post)
        if did:
            self.author_counts[did] += 1

    def pause(self):
        time.sleep(SLEEP_SECONDS)

    def like_after_action(self, post, label):
        uri = get_post_uri(post)
        if not uri or uri in self.state["likes"]:
            return
        try:
            self.client.like(uri, get_post_cid(post))
            self.state["likes"][uri] = now_utc().isoformat()
            log.info("LIKE [%s] %s", label, uri)
            time.sleep(0.5)
        except Exception as exc:
            message = str(exc).lower()
            if "already" in message or "duplicate" in message:
                self.state["likes"][uri] = now_utc().isoformat()
            else:
                log.exception("LIKE ERROR [%s] %s: %s", label, uri, exc)

    def repost(self, post, label, do_like=True):
        """Normal new repost, following HotBleusky's main processing logic."""
        if not self.can_act():
            return False

        uri, cid = get_post_uri(post), get_post_cid(post)
        if not uri or not cid:
            return False
        if uri in self.state["normal_reposts"]:
            return False

        try:
            self.client.repost(uri, cid)
            self.actions += 1
            self.state["normal_reposts"][uri] = now_utc().isoformat()
            log.info("REPOST [%s] %s", label, uri)
            if do_like:
                self.like_after_action(post, label)
            self.pause()
            return True
        except Exception as exc:
            message = str(exc).lower()
            if "already" in message or "duplicate" in message:
                self.state["normal_reposts"][uri] = now_utc().isoformat()
            log.error("REPOST ERROR [%s] %s: %s", label, uri, exc)
            return False

    def reboost(self, post, label, do_like=False):
        """Remove the existing repost record, then repost the original post."""
        if not self.can_act():
            return False

        uri, cid = get_post_uri(post), get_post_cid(post)
        if not uri or not cid:
            return False

        viewer = getattr(post, "viewer", None)
        repost_record_uri = getattr(viewer, "repost", None) if viewer else None

        if repost_record_uri:
            try:
                # viewer.repost is the repost-record URI.
                self.client.delete_repost(repost_record_uri)
                log.info("UNREPOST [%s] %s", label, uri)
                self.pause()
            except Exception as exc:
                log.error(
                    "UNREPOST ERROR [%s] %s: %s; reboost overgeslagen",
                    label,
                    uri,
                    exc,
                )
                return False

        try:
            self.client.repost(uri, cid)
            self.actions += 1
            log.info("REBOOST [%s] %s", label, uri)
            if do_like:
                self.like_after_action(post, label)
            self.pause()
            return True
        except Exception as exc:
            log.error("REBOOST ERROR [%s] %s: %s", label, uri, exc)
            return False


def fetch_list_members(client, list_url, label="LIST"):
    list_uri = list_url_to_uri(list_url)
    members, cursor = [], None
    log.info("[%s] lijst ophalen: %s", label, list_uri)

    while True:
        try:
            params = {"list": list_uri, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            response = client.app.bsky.graph.get_list(params=params)

            for item in getattr(response, "items", []) or []:
                subject = getattr(item, "subject", None)
                did = getattr(subject, "did", None) if subject else None
                if did:
                    members.append({
                        "did": did,
                        "handle": getattr(subject, "handle", None),
                    })

            cursor = getattr(response, "cursor", None)
            if not cursor:
                break
        except Exception:
            log.exception("[%s] lijst ophalen mislukt", label)
            break

    unique = {}
    for member in members:
        unique.setdefault(member["did"], member)
    result = list(unique.values())
    log.info("[%s] %s account(s) gevonden", label, len(result))
    return result


def fetch_author_feed(client, actor, limit=100, label="AUTHOR"):
    """Fetch the newest author-feed page, like HotBleusky."""
    try:
        response = client.app.bsky.feed.get_author_feed({
            "actor": actor,
            "limit": min(100, limit),
        })
        posts = []
        for item in getattr(response, "feed", []) or []:
            if is_repost_item(item):
                continue
            post = getattr(item, "post", None)
            if post:
                posts.append(post)

        log.info(
            "[%s] %s: %s recente feed-item(s) bekeken (1 API page)",
            label,
            actor,
            len(posts),
        )
        return posts
    except Exception:
        log.exception("[%s] auteurfeed ophalen mislukt: %s", label, actor)
        return []


def fetch_author_media(client, actor, limit=100, label="AUTHOR"):
    """Fetch media for promo lists, without imposing the normal 3-hour cutoff."""
    results, cursor = [], None

    for _ in range(MAX_AUTHOR_FEED_PAGES):
        try:
            params = {"actor": actor, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            response = client.app.bsky.feed.get_author_feed(params=params)
            feed = getattr(response, "feed", []) or []
            if not feed:
                break

            for item in feed:
                if is_repost_item(item):
                    continue
                post = getattr(item, "post", None)
                if post and suitable_post(post):
                    results.append(post)
                    if len(results) >= limit:
                        break

            if len(results) >= limit:
                break
            cursor = getattr(response, "cursor", None)
            if not cursor:
                break
        except Exception:
            log.exception("[%s] auteurfeed ophalen mislukt: %s", label, actor)
            break

    log.info("[%s] %s: %s geschikte mediapost(s)", label, actor, len(results))
    return results


def fetch_redfox_posts(client):
    results = []
    try:
        response = client.app.bsky.feed.get_feed({
            "feed": feed_url_to_uri(REDFOX_FEED),
            "limit": 100,
        })
        for item in getattr(response, "feed", []) or []:
            if is_repost_item(item):
                continue
            post = getattr(item, "post", None)
            if suitable_post(post, allow_replies=True) and age_ok(post, NORMAL_LOOKBACK_HOURS):
                results.append(post)
        log.info("[REDFOX] geschikte recente media/replies: %s", len(results))
    except Exception:
        log.exception("[REDFOX] feed ophalen mislukt")
    return results


def collect_repost_always(client):
    """Collect candidates and cache feeds; process normal reposts before +8h reboosts."""
    candidates, cache = [], {}
    members = fetch_list_members(client, REPOST_ALWAYS_LIST, "REPOST ALWAYS")
    log.info("[REPOST ALWAYS] starten voor %s accounts", len(members))

    for index, member in enumerate(members, start=1):
        actor = member.get("handle") or member.get("did")
        if not actor:
            continue

        log.info(
            "[REPOST ALWAYS] account %s/%s: %s",
            index,
            len(members),
            actor,
        )
        posts = fetch_author_feed(client, actor, 100, "REPOST ALWAYS")
        cache[actor] = posts

        count = 0
        for post in posts:
            if not suitable_post(post) or not age_ok(post, NORMAL_LOOKBACK_HOURS):
                continue
            if count >= MAX_PER_USER:
                continue
            candidates.append(post)
            count += 1

    unique = {
        get_post_uri(post): post
        for post in candidates
        if get_post_uri(post)
    }
    result = list(unique.values())
    log.info("[REPOST ALWAYS] normale repost kandidaten=%s", len(result))
    return result, cache


def process_repost_always_reboost(reposter, cache):
    """Separate +8-hour pass, based on the HotBleusky method."""
    threshold = now_utc() - timedelta(hours=REPOST_ALWAYS_REBOOST_HOURS)
    log.info("[REPOST ALWAYS +8H] cache gebruiken voor %s account(s)", len(cache))

    for actor, posts in cache.items():
        for post in posts:
            if not reposter.can_act():
                return
            if not suitable_post(post):
                continue

            uri = get_post_uri(post)
            if not uri:
                continue

            first_repost = parse_datetime(
                reposter.state["normal_reposts"].get(uri)
            )
            last_reboost = parse_datetime(
                reposter.state["repost_always_reboosted"].get(uri)
            )

            if not first_repost or first_repost > threshold or last_reboost:
                continue

            if reposter.reboost(post, "Repost Always +8h", do_like=False):
                reposter.state["repost_always_reboosted"][uri] = now_utc().isoformat()


def fetch_blacklist_dids(client):
    members = fetch_list_members(
        client,
        HASHTAG_BLACKLIST_LIST,
        "HASHTAG BLACKLIST",
    )
    blacklist = {member["did"] for member in members if member.get("did")}
    log.info("[HASHTAG BLACKLIST] %s accounts geladen", len(blacklist))
    return blacklist


def fetch_hashtag_posts(client):
    candidates, cursor = [], None
    while True:
        try:
            params = {"q": HASHTAG, "limit": 100, "sort": "latest"}
            if cursor:
                params["cursor"] = cursor
            response = client.app.bsky.feed.search_posts(params=params)
            posts = getattr(response, "posts", []) or []
            if not posts:
                break

            for post in posts:
                if not suitable_post(post):
                    continue
                if HASHTAG.lower() not in record_text(post).lower():
                    continue
                if age_ok(post, NORMAL_LOOKBACK_HOURS):
                    candidates.append(post)

            cursor = getattr(response, "cursor", None)
            if not cursor:
                break
        except Exception:
            log.exception("[HASHTAG] zoeken mislukt")
            break

    unique = {
        get_post_uri(post): post
        for post in candidates
        if get_post_uri(post)
    }
    result = list(unique.values())
    log.info("[%s] geschikte recente posts: %s", HASHTAG, len(result))
    return result


def process_hashtag_posts(reposter, posts, blacklist):
    ordered = sorted(
        posts,
        key=lambda post: post_datetime(post) or now_utc(),
    )
    for post in ordered:
        if not reposter.can_act():
            break
        if get_author_did(post) in blacklist:
            log.info("[HASHTAG] blacklist: %s overgeslagen", get_author_handle(post))
            continue
        if not reposter.author_allowed(post):
            continue
        if reposter.repost(post, "Hashtag " + HASHTAG, do_like=True):
            reposter.register_author(post)


def process_promo_last(reposter):
    log.info("[PROMO LAST] starten")
    members = fetch_list_members(reposter.client, PROMO_LAST_LIST, "PROMO LAST")

    for member in members:
        if not reposter.can_act():
            break
        actor = member.get("handle") or member.get("did")
        if not actor:
            continue

        posts = fetch_author_media(
            reposter.client,
            actor,
            limit=100,
            label="PROMO LAST",
        )
        eligible = [
            post for post in posts
            if age_ok(post, PROMO_MAX_AGE_HOURS)
        ]
        eligible.sort(
            key=lambda post: post_datetime(post) or now_utc(),
            reverse=True,
        )

        if not eligible:
            log.info("[PROMO LAST] geen geschikte post: %s", actor)
            continue

        post = eligible[0]
        if not reposter.author_allowed(post):
            continue
        if reposter.reboost(post, "Promo Last", do_like=True):
            reposter.register_author(post)


def process_promo_random(reposter):
    log.info("[PROMO RANDOM] starten")
    members = fetch_list_members(
        reposter.client,
        PROMO_RANDOM_LIST,
        "PROMO RANDOM",
    )
    used = reposter.state["promo_random_used"]

    for member in members:
        if not reposter.can_act():
            break
        actor = member.get("handle") or member.get("did")
        if not actor:
            continue

        posts = fetch_author_media(
            reposter.client,
            actor,
            limit=100,
            label="PROMO RANDOM",
        )
        eligible = [
            post for post in posts
            if age_ok(post, PROMO_MAX_AGE_HOURS)
        ]
        eligible.sort(
            key=lambda post: post_datetime(post) or now_utc(),
            reverse=True,
        )
        candidates = eligible[:PROMO_RANDOM_POOL]
        if not candidates:
            log.info("[PROMO RANDOM] geen geschikte post: %s", actor)
            continue

        available = []
        for post in candidates:
            uri = get_post_uri(post)
            previous = parse_datetime(used.get(uri))
            if (
                not previous
                or (now_utc() - previous).total_seconds() / 3600
                >= PROMO_RANDOM_REUSE_HOURS
            ):
                available.append(post)

        if not available:
            log.info(
                "[PROMO RANDOM] geen post beschikbaar binnen %su-regel: %s",
                PROMO_RANDOM_REUSE_HOURS,
                actor,
            )
            continue

        random.shuffle(available)
        chosen = available[0]
        if not reposter.author_allowed(chosen):
            continue
        if reposter.reboost(chosen, "Promo Random", do_like=True):
            used[get_post_uri(chosen)] = now_utc().isoformat()
            reposter.register_author(chosen)


def process_own_posts(reposter):
    """Latest 3 own original media posts, no 3-hour cutoff, oldest to newest."""
    log.info("[OWN POSTS] eigen author feed ophalen")
    own_did = reposter.client.me.did

    try:
        response = reposter.client.app.bsky.feed.get_author_feed({
            "actor": own_did,
            "limit": 100,
        })
    except Exception:
        log.exception("[OWN POSTS] author feed ophalen mislukt")
        return

    own_posts = []
    for item in getattr(response, "feed", []) or []:
        if is_repost_item(item):
            continue

        post = getattr(item, "post", None)
        if not post or get_author_did(post) != own_did:
            continue

        record = getattr(post, "record", None)
        if not isinstance(record, models.AppBskyFeedPost.Record):
            continue
        if getattr(record, "reply", None) is not None:
            continue

        embed = getattr(record, "embed", None)
        if isinstance(
            embed,
            (
                models.AppBskyEmbedRecord.Main,
                models.AppBskyEmbedRecordWithMedia.Main,
            ),
        ):
            continue
        if not isinstance(
            embed,
            (
                models.AppBskyEmbedImages.Main,
                models.AppBskyEmbedVideo.Main,
            ),
        ):
            continue

        own_posts.append(post)
        if len(own_posts) >= 3:
            break

    log.info("[OWN POSTS] %s eigen originele mediapost(s) gevonden", len(own_posts))
    if not own_posts:
        log.info("[OWN POSTS] geen eigen originele mediapost gevonden")
        return

    own_posts.reverse()
    log.info("[OWN POSTS] laatste 3 verwerken: oudste -> nieuwste")
    for post in own_posts:
        if not reposter.can_act():
            break
        reposter.reboost(post, "Own Posts", do_like=False)


def main():
    if not PASSWORD:
        raise RuntimeError("EYESCANDY_PASSWORD ontbreekt in GitHub Secrets")

    state = load_state()
    client = Client()
    log.info("Inloggen als %s", USERNAME)
    client.login(USERNAME, PASSWORD)

    reposter = Reposter(client, state)

    log.info("=" * 60)
    log.info("EYESCANDY COMPLETE REPOSTER START")
    log.info("TIME: %s", now_utc().isoformat())
    log.info("=" * 60)

    # 1. Gather normal sources before processing, as in HotBleusky.
    normal_candidates = []

    try:
        redfox_posts = fetch_redfox_posts(client)
        normal_candidates.extend((post, "RedFoxOfficial") for post in redfox_posts)
    except Exception:
        log.exception("[REDFOX] onderdeel mislukt")

    repost_always_cache = {}
    try:
        ra_posts, repost_always_cache = collect_repost_always(client)
        normal_candidates.extend((post, "Repost Always") for post in ra_posts)
    except Exception:
        log.exception("[REPOST ALWAYS] verzamelen mislukt")

    try:
        blacklist = fetch_blacklist_dids(client)
        hashtag_posts = fetch_hashtag_posts(client)
        normal_candidates.extend(
            (post, "Hashtag " + HASHTAG)
            for post in hashtag_posts
            if get_author_did(post) not in blacklist
        )
    except Exception:
        log.exception("[HASHTAG] onderdeel mislukt")

    # Deduplicate normal sources and retain the first source label.
    unique = {}
    for post, label in normal_candidates:
        uri = get_post_uri(post)
        if uri:
            unique.setdefault(uri, (post, label))

    ordered = sorted(
        unique.values(),
        key=lambda item: post_datetime(item[0]) or now_utc(),
    )
    log.info("[NORMAL] %s unieke kandidaten uit alle normale bronnen", len(ordered))

    # Maximum 3 normal reposts per author across RedFox, Repost Always and hashtag.
    for post, label in ordered:
        if not reposter.can_act():
            break
        if not reposter.author_allowed(post):
            continue
        if reposter.repost(post, label, do_like=True):
            reposter.register_author(post)

    # 2. Repost Always +8h is a separate pass, after normal reposts.
    try:
        process_repost_always_reboost(reposter, repost_always_cache)
    except Exception:
        log.exception("[REPOST ALWAYS +8H] onderdeel mislukt")

    # 3. Promo modules.
    try:
        process_promo_last(reposter)
    except Exception:
        log.exception("[PROMO LAST] onderdeel mislukt")

    try:
        process_promo_random(reposter)
    except Exception:
        log.exception("[PROMO RANDOM] onderdeel mislukt")

    # 4. Own posts always last.
    try:
        process_own_posts(reposter)
    except Exception:
        log.exception("[OWN POSTS] onderdeel mislukt")

    state["last_run"] = now_utc().isoformat()
    save_state(state)

    log.info("=" * 60)
    log.info(
        "EYESCANDY RUN KLAAR | Repostacties: %s / %s",
        reposter.actions,
        MAX_ACTIONS,
    )
    log.info("=" * 60)


if __name__ == "__main__":
    main()
