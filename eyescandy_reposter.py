import os
import json
import time
import random
import logging
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse

from atproto import Client

# ============================================================
# EYESCANDY - COMPLETE REPOSTER
# ============================================================
USERNAME = "eyescandy.bsky.social"
PASSWORD = os.getenv("EYESCANDY_PASSWORD")
STATE_FILE = Path("eyescandy_state.json")

MAX_ACTIONS = 100
MAX_PER_USER = 3
NORMAL_LOOKBACK_HOURS = 12
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("eyescandy-reposter")


def default_state():
    return {
        "normal_reposts": {},
        "repost_always_reboosted": {},
        "promo_random_used": {},
        "promo_last_used": {},
        "own_reboosted": {},
        "last_run": None,
    }


def load_state():
    if not STATE_FILE.exists():
        return default_state()
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        state = default_state()
        state.update(data)
        return state
    except Exception as exc:
        log.error("Statebestand kon niet worden gelezen: %s", exc)
        return default_state()


def save_state(state):
    temp_path = None
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(STATE_FILE.parent), delete=False) as temp:
            json.dump(state, temp, indent=2, ensure_ascii=False)
            temp_path = temp.name
        os.replace(temp_path, STATE_FILE)
    except Exception:
        log.exception("Statebestand opslaan mislukt")
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def now_utc():
    return datetime.now(timezone.utc)


def parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def post_datetime(post):
    record = getattr(post, "record", None)
    created = getattr(record, "created_at", None) or getattr(post, "indexed_at", None)
    return parse_datetime(created)


def post_age_hours(post):
    created = post_datetime(post)
    return None if not created else (now_utc() - created).total_seconds() / 3600


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


def embed_has_media(embed):
    if not embed:
        return False
    if getattr(embed, "images", None) or getattr(embed, "video", None):
        return True
    media = getattr(embed, "media", None)
    return embed_has_media(media) if media else False


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
    return embed_has_media(media) if media else False


def is_reply(post):
    return bool(getattr(getattr(post, "record", None), "reply", None))


def is_quote(post):
    embed = getattr(post, "embed", None)
    if not embed:
        return False
    embed_type = getattr(embed, "$type", "") or ""
    if "recordWithMedia" in embed_type:
        return bool(getattr(embed, "record", None))
    return "record" in embed_type


def is_repost_item(item):
    return getattr(item, "reason", None) is not None


def suitable_post(post, allow_replies=False):
    return bool(post and has_media(post) and not is_quote(post) and (allow_replies or not is_reply(post)))


class Reposter:
    def __init__(self, client, state):
        self.client = client
        self.state = state
        self.actions = 0
        self.author_counts = {}

    def can_act(self):
        return self.actions < MAX_ACTIONS

    def author_allowed(self, post):
        did = get_author_did(post)
        return bool(did and self.author_counts.get(did, 0) < MAX_PER_USER)

    def register_author(self, post):
        did = get_author_did(post)
        if did:
            self.author_counts[did] = self.author_counts.get(did, 0) + 1

    def pause(self):
        time.sleep(SLEEP_SECONDS)

    def like_if_needed(self, post):
        viewer = getattr(post, "viewer", None)
        if getattr(viewer, "like", None):
            log.info("Post is al geliket: %s", get_post_uri(post))
            return
        try:
            self.client.like(uri=get_post_uri(post), cid=get_post_cid(post))
            log.info("Like geplaatst: %s", get_post_uri(post))
            self.pause()
        except Exception:
            log.exception("Liken mislukt")

    def repost(self, post, like=True):
        if not self.can_act():
            log.warning("MAX_ACTIONS bereikt")
            return False
        uri, cid = get_post_uri(post), get_post_cid(post)
        if not uri or not cid:
            return False
        viewer = getattr(post, "viewer", None)
        if getattr(viewer, "repost", None):
            log.info("Post is al gerepost: %s", uri)
            if like:
                self.like_if_needed(post)
            return False
        try:
            self.client.repost(uri=uri, cid=cid)
            self.actions += 1
            self.state["normal_reposts"][uri] = now_utc().isoformat()
            log.info("REPOST %s | %s", get_author_handle(post), uri)
            if like:
                self.pause()
                self.like_if_needed(post)
            self.pause()
            return True
        except Exception:
            log.exception("Repost mislukt: %s", uri)
            return False

    def reboost(self, post, like=True):
        if not self.can_act():
            return False
        uri, cid = get_post_uri(post), get_post_cid(post)
        if not uri or not cid:
            return False
        try:
            viewer = getattr(post, "viewer", None)
            if getattr(viewer, "repost", None):
                try:
                    self.client.delete_repost(uri)
                    log.info("UNREPOST: %s", uri)
                    self.pause()
                except Exception:
                    log.exception("Unrepost mislukt: %s", uri)
            self.client.repost(uri=uri, cid=cid)
            self.actions += 1
            log.info("REBOOST %s | %s", get_author_handle(post), uri)
            if like:
                self.pause()
                self.like_if_needed(post)
            self.pause()
            return True
        except Exception:
            log.exception("Reboost mislukt: %s", uri)
            return False


def fetch_list_members(client, list_url):
    list_uri = list_url_to_uri(list_url)
    members, cursor = [], None
    while True:
        try:
            params = {"list": list_uri, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            response = client.app.bsky.graph.get_list(params=params)
            for item in getattr(response, "items", []) or []:
                subject = getattr(item, "subject", None)
                if not subject:
                    continue
                did = getattr(subject, "did", None)
                if did:
                    members.append({"did": did, "handle": getattr(subject, "handle", None)})
            cursor = getattr(response, "cursor", None)
            if not cursor:
                break
        except Exception:
            log.exception("Fout bij ophalen lijst: %s", list_url)
            break
    return members


def fetch_author_media(client, actor, limit=100):
    results, cursor = [], None
    while len(results) < limit:
        try:
            response = client.get_author_feed(actor=actor, limit=min(100, limit - len(results)), cursor=cursor)
            feed = getattr(response, "feed", []) or []
            if not feed:
                break
            for item in feed:
                if is_repost_item(item):
                    continue
                post = getattr(item, "post", None)
                if suitable_post(post, allow_replies=False):
                    results.append(post)
                if len(results) >= limit:
                    break
            cursor = getattr(response, "cursor", None)
            if not cursor:
                break
        except Exception:
            log.exception("Auteurfeed ophalen mislukt: %s", actor)
            break
    return results


def fetch_redfox_posts(client):
    feed_uri = feed_url_to_uri(REDFOX_FEED)
    results, cursor = [], None
    while True:
        try:
            params = {"feed": feed_uri, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            response = client.app.bsky.feed.get_feed(params=params)
            items = getattr(response, "feed", []) or []
            if not items:
                break
            for item in items:
                if is_repost_item(item):
                    continue
                post = getattr(item, "post", None)
                if not suitable_post(post, allow_replies=True):
                    continue
                age = post_age_hours(post)
                if age is not None and age <= NORMAL_LOOKBACK_HOURS:
                    results.append(post)
            cursor = getattr(response, "cursor", None)
            if not cursor:
                break
        except Exception:
            log.exception("RedFox-feed ophalen mislukt")
            break
    return results


def fetch_repost_always_posts(client):
    members = fetch_list_members(client, REPOST_ALWAYS_LIST)
    candidates = []
    log.info("Repost Always: %s accounts", len(members))
    for member in members:
        if not member.get("handle"):
            continue
        for post in fetch_author_media(client, member["handle"], limit=100):
            age = post_age_hours(post)
            if age is not None and age <= NORMAL_LOOKBACK_HOURS:
                candidates.append(post)
    return candidates


def process_repost_always(reposter, posts):
    log.info("Repost Always verwerken")
    unique = {get_post_uri(post): post for post in posts if get_post_uri(post)}
    ordered = sorted(unique.values(), key=lambda p: post_datetime(p) or now_utc())
    for post in ordered:
        if not reposter.can_act():
            break
        if not reposter.author_allowed(post):
            continue
        uri = get_post_uri(post)
        last_boost = parse_datetime(reposter.state["repost_always_reboosted"].get(uri))
        if last_boost and (now_utc() - last_boost).total_seconds() / 3600 < REPOST_ALWAYS_REBOOST_HOURS:
            continue
        if reposter.reboost(post, like=True):
            reposter.state["repost_always_reboosted"][uri] = now_utc().isoformat()
            reposter.register_author(post)


def fetch_blacklist_dids(client):
    members = fetch_list_members(client, HASHTAG_BLACKLIST_LIST)
    blacklist = {member["did"] for member in members if member.get("did")}
    log.info("Hashtag blacklist geladen: %s accounts", len(blacklist))
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
                if not suitable_post(post, allow_replies=False):
                    continue
                if HASHTAG.lower() not in record_text(post).lower():
                    continue
                age = post_age_hours(post)
                if age is not None and age <= NORMAL_LOOKBACK_HOURS:
                    candidates.append(post)
            cursor = getattr(response, "cursor", None)
            if not cursor:
                break
        except Exception:
            log.exception("Hashtag zoeken mislukt")
            break
    return candidates


def process_hashtag_posts(reposter, posts, blacklist):
    log.info("Hashtag %s verwerken", HASHTAG)
    unique = {get_post_uri(post): post for post in posts if get_post_uri(post)}
    ordered = sorted(unique.values(), key=lambda p: post_datetime(p) or now_utc())
    for post in ordered:
        if not reposter.can_act():
            break
        if get_author_did(post) in blacklist:
            log.info("Blacklist: account overgeslagen: %s", get_author_handle(post))
            continue
        if not reposter.author_allowed(post):
            continue
        if reposter.repost(post, like=True):
            reposter.register_author(post)


def process_promo_last(reposter):
    log.info("Promo Last verwerken")
    members = fetch_list_members(reposter.client, PROMO_LAST_LIST)
    for member in members:
        if not reposter.can_act():
            break
        handle = member.get("handle")
        if not handle:
            continue
        try:
            posts = fetch_author_media(reposter.client, handle, limit=100)
            if not posts:
                log.info("Geen geschikte Promo Last post: %s", handle)
                continue
            posts.sort(key=lambda p: post_datetime(p) or now_utc(), reverse=True)
            post = posts[0]
            age = post_age_hours(post)
            if age is None or age > PROMO_MAX_AGE_HOURS or not reposter.author_allowed(post):
                continue
            if reposter.reboost(post, like=True):
                reposter.register_author(post)
        except Exception:
            log.exception("Promo Last fout bij %s", handle)
            continue


def process_promo_random(reposter):
    log.info("Promo Random 25 verwerken")
    members = fetch_list_members(reposter.client, PROMO_RANDOM_LIST)
    used = reposter.state["promo_random_used"]
    for member in members:
        if not reposter.can_act():
            break
        handle = member.get("handle")
        if not handle:
            continue
        try:
            posts = fetch_author_media(reposter.client, handle, limit=100)
            if not posts:
                log.info("Geen geschikte Random-post: %s", handle)
                continue
            posts.sort(key=lambda p: post_datetime(p) or now_utc(), reverse=True)
            candidates = []
            for post in posts:
                age = post_age_hours(post)
                if age is None or age > PROMO_MAX_AGE_HOURS:
                    continue
                candidates.append(post)
                if len(candidates) >= PROMO_RANDOM_POOL:
                    break
            if not candidates:
                continue
            eligible = []
            for post in candidates:
                uri = get_post_uri(post)
                previous = parse_datetime(used.get(uri))
                if not previous or (now_utc() - previous).total_seconds() / 3600 >= PROMO_RANDOM_REUSE_HOURS:
                    eligible.append(post)
            if not eligible:
                log.info("Geen Random-post beschikbaar binnen cooldown: %s", handle)
                continue
            random.shuffle(eligible)
            post = eligible[0]
            if not reposter.author_allowed(post):
                continue
            if reposter.reboost(post, like=True):
                used[get_post_uri(post)] = now_utc().isoformat()
                reposter.register_author(post)
        except Exception:
            log.exception("Promo Random fout bij %s", handle)
            continue


def process_own_posts(reposter):
    log.info("Eigen laatste 3 posts verwerken")
    try:
        posts = fetch_author_media(reposter.client, USERNAME, limit=100)
        if not posts:
            log.info("Geen eigen mediaposts gevonden")
            return
        posts.sort(key=lambda p: post_datetime(p) or now_utc(), reverse=True)
        latest_three = posts[:3]
        latest_three.reverse()  # oudste eerst, nieuwste als laatste
        for post in latest_three:
            if not reposter.can_act():
                break
            reposter.reboost(post, like=False)
    except Exception:
        log.exception("Eigen posts verwerken mislukt")


def main():
    if not PASSWORD:
        raise RuntimeError("EYESCANDY_PASSWORD ontbreekt in GitHub Secrets")

    state = load_state()
    client = Client()
    log.info("Inloggen als %s", USERNAME)
    try:
        client.login(USERNAME, PASSWORD)
    except Exception:
        log.exception("Bluesky login mislukt")
        raise

    reposter = Reposter(client, state)
    log.info("=" * 55)
    log.info("EYESCANDY COMPLETE REPOSTER START")
    log.info("=" * 55)

    # 1. RedFox: mediaposts en media-replies
    try:
        posts = fetch_redfox_posts(client)
        log.info("RedFox geschikte posts: %s", len(posts))
        unique = {get_post_uri(post): post for post in posts if get_post_uri(post)}
        ordered = sorted(unique.values(), key=lambda p: post_datetime(p) or now_utc())
        for post in ordered:
            if not reposter.can_act():
                break
            if not reposter.author_allowed(post):
                continue
            if reposter.repost(post, like=True):
                reposter.register_author(post)
    except Exception:
        log.exception("RedFox onderdeel mislukt")

    # 2. Repost Always
    try:
        posts = fetch_repost_always_posts(client)
        log.info("Repost Always geschikte posts: %s", len(posts))
        process_repost_always(reposter, posts)
    except Exception:
        log.exception("Repost Always onderdeel mislukt")

    # 3. Hashtag blacklist + #eyescandy
    try:
        blacklist = fetch_blacklist_dids(client)
        posts = fetch_hashtag_posts(client)
        log.info("#eyescandy geschikte posts: %s", len(posts))
        process_hashtag_posts(reposter, posts, blacklist)
    except Exception:
        log.exception("Hashtag onderdeel mislukt")

    # 4. Promo Last
    try:
        process_promo_last(reposter)
    except Exception:
        log.exception("Promo Last onderdeel mislukt")

    # 5. Promo Random 25
    try:
        process_promo_random(reposter)
    except Exception:
        log.exception("Promo Random onderdeel mislukt")

    # 6. Eigen laatste 3 posts - altijd als laatste
    try:
        process_own_posts(reposter)
    except Exception:
        log.exception("Eigen posts onderdeel mislukt")

    state["last_run"] = now_utc().isoformat()
    save_state(state)
    log.info("=" * 55)
    log.info("EYESCANDY RUN KLAAR | Repostacties: %s / %s", reposter.actions, MAX_ACTIONS)
    log.info("=" * 55)


if __name__ == "__main__":
    main()
