import os
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

from atproto import Client

# ============================================================
# EYESCANDY - REPOST ALWAYS
# ============================================================

USERNAME = "eyescandy.bsky.social"
PASSWORD = os.getenv("EYESCANDY_PASSWORD")

LIST_URL = (
    "https://bsky.app/profile/"
    "did:plc:sp54ouue6fp2dlvn2cux54ka/"
    "lists/3mncvshsuhi2d"
)

STATE_FILE = Path("eyescandy_state.json")

HOURS_BACK = 3
MAX_PER_USER = 3
MAX_PER_RUN = 100
POSTS_PER_ACCOUNT = 30
SLEEP_SECONDS = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("eyescandy")


# ============================================================
# DATUM EN TIJD
# ============================================================

def utcnow():
    return datetime.now(timezone.utc)


def parse_date(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except Exception:
        return None


# ============================================================
# STATE
# ============================================================

def load_state():
    if not STATE_FILE.exists():
        return {"normal_reposts": {}}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            state = json.load(f)

        if not isinstance(state, dict):
            state = {}

        state.setdefault("normal_reposts", {})
        return state

    except Exception as exc:
        log.error("State kon niet worden gelezen: %s", exc)
        return {"normal_reposts": {}}


def save_state(state):
    temp_file = STATE_FILE.with_suffix(".tmp")

    with temp_file.open("w", encoding="utf-8") as f:
        json.dump(
            state,
            f,
            indent=2,
            ensure_ascii=False
        )

    os.replace(temp_file, STATE_FILE)


# ============================================================
# BLUESKY-LIJST
# ============================================================

def list_uri_from_url(url):
    parts = url.rstrip("/").split("/")

    if len(parts) < 2:
        raise ValueError("Ongeldige Repost Always URL")

    return (
        f"at://{parts[-3]}/app.bsky.graph.list/{parts[-1]}"
    )


def fetch_list_members(client, list_uri):
    members = []
    cursor = None

    while True:
        params = {
            "list": list_uri,
            "limit": 100
        }

        if cursor:
            params["cursor"] = cursor

        try:
            response = client.app.bsky.graph.get_list(params)

        except Exception as exc:
            log.error("Lijst ophalen mislukt: %s", exc)
            break

        for item in getattr(response, "items", []) or []:
            subject = getattr(item, "subject", None)

            if not subject:
                continue

            did = getattr(subject, "did", None)

            if did:
                members.append(did)

        cursor = getattr(response, "cursor", None)

        if not cursor:
            break

    return list(dict.fromkeys(members))


# ============================================================
# MEDIA CONTROLE
# ============================================================

def has_media(post):
    embed = getattr(post, "embed", None)

    if not embed:
        return False

    if getattr(embed, "images", None):
        return True

    if getattr(embed, "video", None):
        return True

    media = getattr(embed, "media", None)

    if media:
        if getattr(media, "images", None):
            return True

        if getattr(media, "video", None):
            return True

    return False


def is_original_media_post(item, post):
    # Geen reposts van andere accounts
    if getattr(item, "reason", None) is not None:
        return False

    record = getattr(post, "record", None)

    if not record:
        return False

    # Geen antwoorden
    if getattr(record, "reply", None):
        return False

    # Alleen normale foto's en video's
    if not has_media(post):
        return False

    # Geen quote-posts
    embed = getattr(post, "embed", None)

    if embed:
        embed_type = getattr(embed, "$type", "") or ""

        if "record" in embed_type:
            return False

    return True


def get_post_date(post):
    record = getattr(post, "record", None)

    created = None

    if record:
        created = (
            getattr(record, "created_at", None)
            or getattr(record, "createdAt", None)
        )

    if not created:
        created = (
            getattr(post, "indexed_at", None)
            or getattr(post, "indexedAt", None)
        )

    return parse_date(created)


# ============================================================
# BERICHTEN VAN EEN ACCOUNT OPHALEN
# ============================================================

def fetch_author_posts(client, actor):
    try:
        response = client.app.bsky.feed.get_author_feed({
            "actor": actor,
            "limit": POSTS_PER_ACCOUNT
        })

        return getattr(response, "feed", []) or []

    except Exception as exc:
        # Zelfde eenvoudige foutafhandeling als DMPhotos:
        # dit account overslaan en doorgaan met het volgende.
        log.warning(
            "Account overgeslagen wegens ophaalfout: %s | %s",
            actor,
            exc
        )

        return []


# ============================================================
# REPOST ALWAYS VERWERKEN
# ============================================================

def process_repost_always(client, state):

    cutoff = utcnow() - timedelta(hours=HOURS_BACK)

    reposted = state["normal_reposts"]

    list_uri = list_uri_from_url(LIST_URL)

    log.info("Repost Always lijst ophalen")

    members = fetch_list_members(client, list_uri)

    log.info("Accounts in lijst: %s", len(members))

    candidates = []

    for actor in members:

        items = fetch_author_posts(client, actor)

        for item in items:

            post = getattr(item, "post", None)

            if not post:
                continue

            if not is_original_media_post(item, post):
                continue

            uri = getattr(post, "uri", None)
            cid = getattr(post, "cid", None)

            if not uri or not cid:
                continue

            # Staat het bericht al in state? Dan overslaan.
            if uri in reposted:
                continue

            created = get_post_date(post)

            if not created:
                continue

            # Alleen berichten van de afgelopen 3 uur.
            if created < cutoff:
                continue

            candidates.append({
                "uri": uri,
                "cid": cid,
                "created": created,
                "author": actor
            })

    # Oudste eerst
    candidates.sort(key=lambda x: x["created"])

    log.info("Geschikte nieuwe berichten: %s", len(candidates))

    author_counts = {}
    total = 0

    for candidate in candidates:

        if total >= MAX_PER_RUN:
            break

        actor = candidate["author"]

        author_counts.setdefault(actor, 0)

        if author_counts[actor] >= MAX_PER_USER:
            continue

        uri = candidate["uri"]
        cid = candidate["cid"]

        try:
            client.app.bsky.feed.repost.create(
                repo=client.me.did,
                record={
                    "subject": {
                        "uri": uri,
                        "cid": cid
                    },
                    "createdAt": utcnow().isoformat()
                }
            )

            # Alleen na een geslaagde repost opslaan.
            reposted[uri] = utcnow().isoformat()

            save_state(state)

            author_counts[actor] += 1
            total += 1

            log.info("REPOST GESLAAGD: %s", uri)

            time.sleep(SLEEP_SECONDS)

        except Exception as exc:

            message = str(exc).lower()

            # Een reeds bestaande repost niet als nieuwe
            # succesvolle actie registreren.
            log.error(
                "REPOST MISLUKT: %s | %s",
                uri,
                exc
            )

    log.info("Klaar. Nieuwe reposts: %s", total)


# ============================================================
# MAIN
# ============================================================

def main():

    log.info("=== EYESCANDY REPOST ALWAYS START ===")

    if not PASSWORD:
        log.error("EYESCANDY_PASSWORD ontbreekt")
        return

    state = load_state()

    client = Client()

    try:
        client.login(USERNAME, PASSWORD)

    except Exception as exc:
        log.error("Inloggen mislukt: %s", exc)
        return

    log.info("Ingelogd als %s", USERNAME)

    process_repost_always(client, state)

    log.info("=== EYESCANDY REPOST ALWAYS KLAAR ===")


if __name__ == "__main__":
    main()