#!/usr/bin/env python3
"""
WOPR Oracle — NIP-89 announcement publisher.
Creates and broadcasts the DVM service announcement event so clients can discover us.

NIP-89 format: kind 1 with kind-specific content as JSON in content field:
{
  "name": "WOPR Oracle",
  "about": "Nostr DVM Oracle — text generation, summarization, translation, polls, and prediction markets",
  "picture": "https://...",
  "lightning_address": "cheshireremnant@lnaddress.com",
  "lightning_node": "<pubkey>",
  "price_sats": 50,
  "service_kinds": [5000, 5001, 5002, 5300, 6969],
  "result_kinds": [6000, 6001, 6002, 6300, 6970],
}
"""

import asyncio
import json
from pathlib import Path

from nostr_sdk import Client, Keys, EventBuilder, Tag, Kind

ORACLE_DIR = Path(__file__).parent
NSEC_FILE = Path.home() / ".openclaw" / "secrets" / "oracle-nsec"
ANNOUNCED_FILE = ORACLE_DIR / ".announced"

RELAYS_TO_ANNOUNCE = [
    "wss://relay.nostr.net",
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.primal.net",
    "wss://relay.nostrdvm.com",
]


def load_keys():
    nsec = NSEC_FILE.read_text().strip()
    return Keys.parse(nsec)


def build_announcement(keys):
    """Build the NIP-89 announcement event content."""
    pubkey_hex = keys.public_key().to_hex()
    pubkey_npub = keys.public_key().to_bech32()

    content = {
        "name": "WOPR Oracle",
        "about": (
            "Decentralized Oracle DVM on Nostr. "
            "Supports text generation, summarization, translation, "
            "prediction markets, and polls. Powered by Ollama AI. "
            f"Oracle pubkey: {pubkey_npub}"
        ),
        "picture": "https://placekitten.com/300/300",  # placeholder icon
        "lightning_address": "cheshireremnant@lnaddress.com",
        "lightning_node": pubkey_hex,
        "price_sats": 50,
        "currency": "Sats",
        "service_kinds": {
            "5000": "Text Generation",
            "5001": "Summarization",
            "5002": "Translation",
            "5300": "Content Discovery / Prediction Markets",
            "6969": "Polls (NIP-96B)",
        },
        "job_kinds": [5000, 5001, 5002, 5300, 6969],
        "result_kinds": [6000, 6001, 6002, 6300, 6970],
        "max_bid_mSat": 50000,
        "expires_at": 0,  # never expires
    }

    return json.dumps(content, indent=2)


def build_event(keys, content):
    """Build and sign the NIP-89 announcement event (kind 1)."""
    tags = [
        Tag.parse("d:wopr-oracle-v1"),
        Tag.parse("k:5000"),
        Tag.parse("k:5001"),
        Tag.parse("k:5002"),
        Tag.parse("k:5300"),
        Tag.parse("k:6969"),
    ]
    evt = (
        EventBuilder.text_note(content)
        .tags(tags)
        .sign_with_keys(keys)
    )
    return evt


async def announce():
    nsec_file = NSEC_FILE
    if not nsec_file.exists():
        print("ERROR: oracle nsec not found. Run oracle.py first to generate identity.")
        return

    keys = load_keys()
    content = build_announcement(keys)
    evt = build_event(keys, content)

    print(f"Oracle pubkey: {keys.public_key().to_bech32()}")
    print(f"Event id: {evt.id().to_hex()[:32]}...")
    print(f"Content:\n{content}\n")

    # Check if already announced
    if ANNOUNCED_FILE.exists():
        print(f"Already announced (see {ANNOUNCED_FILE}).")
        resp = input("Re-announce? [y/N]: ")
        if resp.lower() != "y":
            print("Aborted.")
            return

    # Publish to relays
    client = Client()
    for relay in RELAYS_TO_ANNOUNCE:
        try:
            await client.add_relay(relay)
            print(f"  + {relay}")
        except Exception as e:
            print(f"  ! {relay}: {e}")

    try:
        await client.connect()
        await client.send_event(evt)
        await client.disconnect()
    except Exception as e:
        print(f"Failed to announce: {e}")
        return

    # Record announcement
    ANNOUNCED_FILE.write_text(json.dumps({
        "event_id": evt.id().to_hex(),
        "pubkey": keys.public_key().to_hex(),
        "announced_at": int(__import__("time").time()),
    }))
    print(f"\nAnnounced! Event: {evt.id().to_hex()}")
    print(f"Saved to {ANNOUNCED_FILE}")


if __name__ == "__main__":
    asyncio.run(announce())
