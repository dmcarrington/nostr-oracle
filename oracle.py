#!/usr/bin/env python3
"""
Nostr Oracle DVM — listens for NIP-90 job requests, resolves them, posts results.
Supports: kind 5000/5001/5002 (text), kind 5300 (discovery), kind 6969 (polls).

Usage:
    python3 oracle.py              # run normally
    python3 oracle.py --test        # process due jobs once, print results
    python3 oracle.py --listen     # listen for new jobs for 60s, print them
"""

import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import websockets

# Project paths
ORACLE_DIR = Path(__file__).parent
DB_PATH = ORACLE_DIR / "data" / "oracle.db"
RELAYS = [
    "wss://relay.damus.io",
    "wss://relay.nostr.net",
    "wss://nos.lol",
    "wss://relay.primal.net",
]

# Your Oracle identity (nsec stored in secrets)
NSEC_FILE = Path.home() / ".openclaw" / "secrets" / "oracle-nsec"
PUBKEY_FILE = Path.home() / ".openclaw" / "secrets" / "oracle-npub"

# Job kinds to listen for
JOB_KINDS = list(range(5000, 5003)) + [5300, 6969]

# ─── Database ──────────────────────────────────────────────────────────────────

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id          TEXT PRIMARY KEY,
            pubkey      TEXT NOT NULL,
            kind        INTEGER NOT NULL,
            prompt      TEXT,
            tags        TEXT,
            amount_sats INTEGER DEFAULT 0,
            bolt11      TEXT,
            closed_at   INTEGER,
            relays      TEXT,
            status      TEXT DEFAULT 'pending',
            result      TEXT,
            error       TEXT,
            created_at  INTEGER NOT NULL,
            resolved_at INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status_closed ON jobs(status, closed_at)")
    conn.commit()
    return conn


# ─── Event parsing ─────────────────────────────────────────────────────────────

def extract_job(event):
    """Parse a NIP-90 job event, extract all relevant fields."""
    kind = event["kind"]
    tags = event.get("tags", [])
    content = event.get("content", "") or ""

    job = {
        "id": event["id"],
        "pubkey": event["pubkey"],
        "kind": kind,
        "prompt": "",
        "amount_sats": 0,
        "bolt11": None,
        "closed_at": None,
        "relays": [],
        "input_tags": {},
        "raw_tags": tags,
    }

    # Extract standard NIP-90 tags
    for tag in tags:
        if len(tag) < 2:
            continue
        key, val = tag[0], tag[1]
        job["input_tags"][key] = val

        if key == "i":
            job["prompt"] = val  # inline text input
        elif key == "bid":
            job["amount_sats"] = int(val) if val.isdigit() else 0
        elif key == "bolt11":
            job["bolt11"] = val
        elif key == "relays":
            job["relays"] = tag[1:]
        elif key == "closed_at":
            job["closed_at"] = int(val) if str(val).isdigit() else None
        elif key == "poll_option":
            pass  # handled separately for polls

    # For 6969 polls: content is the question
    if kind == 6969:
        job["prompt"] = content

    # For 5300 discovery: content may be empty, look for i tag
    if kind == 5300 and not job["prompt"]:
        # Try to find i tag value
        for tag in tags:
            if len(tag) >= 2 and tag[0] == "i":
                job["prompt"] = tag[1]
                break

    return job


def queue_job(conn, job):
    """Add a job to the pending queue if not already present."""
    existing = conn.execute(
        "SELECT status FROM jobs WHERE id = ?", (job["id"],)
    ).fetchone()
    if existing:
        return False  # already queued

    conn.execute("""
        INSERT INTO jobs (id, pubkey, kind, prompt, tags, amount_sats, bolt11,
                          closed_at, relays, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
    """, (
        job["id"],
        job["pubkey"],
        job["kind"],
        job["prompt"],
        json.dumps(job["raw_tags"]),
        job["amount_sats"],
        job["bolt11"],
        job["closed_at"],
        json.dumps(job["relays"]),
        int(time.time()),
    ))
    conn.commit()
    return True


# ─── Job listener ──────────────────────────────────────────────────────────────

async def listen_for_jobs(timeout_sec=60, max_jobs=10):
    """Subscribe to relays for NIP-90 job events, print them."""
    conn = init_db()
    collected = []
    stop = asyncio.Event()

    async def from_relay(relay):
        try:
            async with websockets.connect(relay, ping_interval=None) as ws:
                req = ["REQ", "oracle-listen",
                       {"kinds": JOB_KINDS, "limit": max_jobs}]
                await ws.send(json.dumps(req))

                while not stop.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
                        msg = json.loads(msg)
                        if msg[0] == "EVENT":
                            evt = msg[2]
                            job = extract_job(evt)
                            queued = queue_job(conn, job)
                            collected.append((relay, job, queued))
                            print(f"\n[JOB] kind={job['kind']} id={job['id'][:16]}... "
                                  f"prompt={job['prompt'][:60]!r} "
                                  f"bid={job['amount_sats']}msats "
                                  f"queued={queued}")
                    except asyncio.TimeoutError:
                        continue
        except Exception as e:
            print(f"  [!] {relay}: {e}", file=sys.stderr)

    tasks = [asyncio.create_task(from_relay(r)) for r in RELAYS]
    print(f"Listening on {len(RELAYS)} relays for {timeout_sec}s...")
    print(f"Kinds: {JOB_KINDS}\n")

    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        stop.set()

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    conn.close()

    print(f"\nDone. Collected {len(collected)} jobs "
          f"({sum(1 for _, _, q in collected if q)} new, "
          f"{sum(1 for _, _, q in collected if not q)} duplicates)")


# ─── Resolution engine ─────────────────────────────────────────────────────────

def resolve_job(job):
    """
    Resolve a job based on its kind and prompt.
    Returns a dict with 'answer', 'sources', 'confidence'.
    """
    kind = job["kind"]
    prompt = job["prompt"]
    tags = job["raw_tags"]

    if kind in (5000, 5001, 5002):
        # Text generation / summarization / translation
        # These are AI tasks — delegate to local LLM or external API
        return resolve_text_job(job)

    elif kind == 5300:
        # Content discovery / prediction market
        return resolve_discovery_job(job)

    elif kind == 6969:
        # Poll — check if closed and resolve
        return resolve_poll_job(job, tags)

    else:
        return {
            "answer": "unsupported_kind",
            "sources": [],
            "confidence": 1.0,
            "error": f"Kind {kind} not supported",
        }


def resolve_text_job(job):
    """Delegate text jobs to Ollama LLM."""
    import os, subprocess

    prompt = job["prompt"]
    kind = job["kind"]

    model = "minimax-m2.7:cloud"  # your configured Ollama cloud model

    # Build a concise prompt based on job type
    if kind == 5001:  # summarize
        instruction = "Summarize the following text concisely:\n\n"
    elif kind == 5002:  # translate
        instruction = "Translate the following to English:\n\n"
    else:
        instruction = ""

    full_prompt = instruction + prompt

    try:
        result = subprocess.run(
            ["curl", "-s", "http://127.0.0.1:11434/api/generate",
             "-d", json.dumps({"model": model, "prompt": full_prompt, "stream": False})],
            capture_output=True, text=True, timeout=30
        )
        data = json.loads(result.stdout)
        text = data.get("response", "").strip()
        return {"answer": text, "sources": ["ollama/minimax"], "confidence": 0.9}
    except Exception as e:
        return {"answer": "", "sources": [], "confidence": 0, "error": str(e)}


def resolve_discovery_job(job):
    """
    Prediction market / content discovery.
    Extract keywords and return a structured 'oracle pending' response.
    Real implementation: check external APIs for resolution.
    """
    prompt = job["prompt"].lower()
    closed_at = job.get("closed_at") or (int(time.time()) + 86400)

    # Categorize
    categories = []
    keywords = {
        "Crypto/Bitcoin": ["bitcoin", "btc", "nostr", "maxi", "mining"],
        "Markets": ["gold", "oil", "barrel", "crude", "market", "price"],
        "Geopolitics": ["trump", "iran", "china", "russia", "sanctions", "war"],
        "Sports": ["f1", "gp", "tournament", "bracket", "nba", "texas"],
        "AI/Tech": ["openai", "gpt", "model", "release", "github"],
    }
    for cat, kws in keywords.items():
        if any(kw in prompt for kw in kws):
            categories.append(cat)

    answer = {
        "type": "prediction_pending",
        "question": job["prompt"],
        "resolution_criteria": "Check external data source at or after closed_at",
        "closed_at": closed_at,
        "categories": categories,
        "oracle": "WOPR Oracle",
    }

    return {
        "answer": json.dumps(answer),
        "sources": [],
        "confidence": 0.8,
        "pending": True,
    }


def resolve_poll_job(job, tags):
    """Resolve a poll based on closed_at time and votes."""
    prompt = job["prompt"]
    closed_at = job.get("closed_at")

    # Extract poll options
    options = {}
    for tag in tags:
        if len(tag) >= 3 and tag[0] == "poll_option":
            _, idx, label = tag[0], tag[1], tag[2]
            options[idx] = label

    if closed_at and int(time.time()) < closed_at:
        # Not yet closed
        return {
            "answer": json.dumps({"status": "open", "options": options, "closed_at": closed_at}),
            "sources": [],
            "confidence": 1.0,
            "pending": True,
        }

    # Poll closed — return partial results (real impl would fetch from relay)
    return {
        "answer": json.dumps({
            "status": "resolved",
            "prompt": prompt,
            "options": options,
            "resolution_note": "Poll resolved — vote tally pending oracle fetch",
        }),
        "sources": [],
        "confidence": 0.5,
        "pending": True,
    }


# ─── Result poster ─────────────────────────────────────────────────────────────

async def post_result(job, resolution, relay_list=None):
    """Post a NIP-90 result event to the specified relays."""
    # Import nostr-sdk here to avoid top-level import issues
    try:
        from nostr_sdk import Client, Keys, NostrDatabase, EventBuilder, Tag, Kind
    except ImportError:
        print("nostr-sdk not installed, skipping result post")
        return

    nsec_file = NSEC_FILE
    if not nsec_file.exists():
        print("Oracle nsec not found, skipping result post")
        return

    keys = Keys.parse(nsec_file.read_text().strip())

    kind_map = {
        5000: 6000,
        5001: 6001,
        5002: 6002,
        5300: 6300,
        6969: 6970,
    }
    result_kind = kind_map.get(job["kind"], 6000)

    # Build tags from original job
    tags = [
        Tag.parse(f"job:{job['id']}"),
        Tag.parse(f"p:{job['pubkey']}"),
    ]
    if resolution.get("sources"):
        for src in resolution["sources"]:
            tags.append(Tag.parse(f"source:{src}"))

    content = resolution.get("answer", "")

    evt = EventBuilder(result_kind, content, tags).to_event(keys)

    relays_to_use = relay_list or job.get("relays", RELAYS)

    client = Client()
    for relay in relays_to_use[:4]:  # limit to 4 relays
        try:
            await client.add_relay(relay)
        except Exception:
            pass

    try:
        await client.connect()
        await client.send_event(evt)
        await client.disconnect()
        print(f"  [POSTED] kind={result_kind} → {relays_to_use[:2]}")
    except Exception as e:
        print(f"  [ERROR] posting: {e}")


# ─── Scheduler ─────────────────────────────────────────────────────────────────

def get_due_jobs(conn, limit=10):
    """Get pending jobs that are due for resolution."""
    now = int(time.time())
    return conn.execute("""
        SELECT id, pubkey, kind, prompt, tags, amount_sats, bolt11,
               closed_at, relays, created_at
        FROM jobs
        WHERE status = 'pending'
          AND (closed_at IS NULL OR closed_at <= ?)
        ORDER BY closed_at ASC
        LIMIT ?
    """, (now, limit)).fetchall()


def job_from_row(row):
    """Convert a DB row to a job dict."""
    return {
        "id": row[0],
        "pubkey": row[1],
        "kind": row[2],
        "prompt": row[3],
        "raw_tags": json.loads(row[4]) if row[4] else [],
        "amount_sats": row[5],
        "bolt11": row[6],
        "closed_at": row[7],
        "relays": json.loads(row[8]) if row[8] else [],
        "created_at": row[9],
    }


async def process_due_jobs():
    """Check for due jobs, resolve them, post results."""
    conn = init_db()
    jobs = get_due_jobs(conn)
    print(f"\nScheduler: {len(jobs)} jobs due for resolution")

    for row in jobs:
        job = job_from_row(row)
        print(f"\n[RESOLVING] kind={job['kind']} id={job['id'][:16]}... prompt={job['prompt'][:50]!r}")

        resolution = resolve_job(job)

        if resolution.get("pending"):
            # Not ready yet — update closed_at and skip
            print(f"  [PENDING] not yet resolvable: {resolution.get('answer', '')[:100]}")
            continue

        # Store result
        conn.execute(
            "UPDATE jobs SET status='resolved', result=?, resolved_at=? WHERE id=?",
            (json.dumps(resolution), int(time.time()), job["id"])
        )
        conn.commit()

        # Post result to relays
        await post_result(job, resolution)

    conn.close()


# ─── CLI ───────────────────────────────────────────────────────────────────────

async def main():
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "--listen":
            await listen_for_jobs(timeout_sec=60)
        elif cmd == "--test":
            await process_due_jobs()
        else:
            print(f"Usage: python3 {sys.argv[0]} [--listen|--test]")
    else:
        # Run as daemon: listen briefly then process due jobs, repeat
        print("WOPR Oracle starting...")
        print(f"DB: {DB_PATH}")
        print(f"NSEC: {NSEC_FILE}")
        print(f"Relays: {RELAYS}")

        # Initial listen for new jobs
        print("\n--- Job discovery phase (30s) ---")
        await listen_for_jobs(timeout_sec=30, max_jobs=20)

        # Process due jobs
        print("\n--- Resolution phase ---")
        await process_due_jobs()

        print("\nOracle cycle complete.")


if __name__ == "__main__":
    asyncio.run(main())
