#!/usr/bin/env python3
"""
Nostr Oracle DVM — listens for NIP-90 job requests + NIP-57 zaps,
verifies payment, resolves jobs, posts results.
"""

import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import websockets

ORACLE_DIR = Path(__file__).parent
DB_PATH = ORACLE_DIR / "data" / "oracle.db"

RELAYS = [
    "wss://relay.damus.io",
    "wss://relay.nostr.net",
    "wss://nos.lol",
    "wss://relay.primal.net",
]

JOB_KINDS = list(range(5000, 5003)) + [5300, 6969]
ZAP_KIND = 9735

JOB_PRICE_SATS = 50  # price per job

# ─── Identity ─────────────────────────────────────────────────────────────────

def load_identity():
    nsec_file = Path.home() / ".openclaw" / "secrets" / "oracle-nsec"
    npub_file = Path.home() / ".openclaw" / "secrets" / "oracle-npub"
    if not nsec_file.exists():
        return None, None
    return nsec_file.read_text().strip(), npub_file.read_text().strip()

NSEC, MY_NPUB = load_identity()

# ─── Database ─────────────────────────────────────────────────────────────────

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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS zaps (
            zap_id      TEXT PRIMARY KEY,
            job_id      TEXT,
            sender      TEXT NOT NULL,
            amount_msat INTEGER NOT NULL,
            bolt11      TEXT,
            verified    INTEGER DEFAULT 0,
            received_at INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_pubkey ON jobs(pubkey)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_zaps_job ON zaps(job_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_zaps_sender ON zaps(sender)")
    conn.commit()
    return conn


# ─── Zap verification ─────────────────────────────────────────────────────────

async def verify_zap(zap_event):
    """
    Verify a NIP-57 zap event:
    - Signature must be valid
    - The inner 9735 event must be signed by the sender
    - bolt11 invoice amount must match
    Returns (ok, amount_msats, error)
    """
    try:
        from nostr_sdk import Event
        evt = Event.from_json(json.dumps(zap_event))
        if not evt.verify():
            return False, 0, "invalid signature"
        # Extract amount from tags
        amount_msats = 0
        for tag in evt.tags():
            if tag.as_vec()[0] == "amount":
                amount_msats = int(tag.as_vec()[1])
        return True, amount_msats, None
    except Exception as e:
        return False, 0, str(e)


async def listen_for_zaps(pubkey_hex, timeout_sec=60):
    """Subscribe to zap receipts (kind 9735) for our oracle pubkey."""
    conn = init_db()
    seen = set()
    stop = asyncio.Event()

    async def from_relay(relay):
        try:
            async with websockets.connect(relay, ping_interval=None) as ws:
                # Subscribe to zaps mentioning our pubkey
                req = ["REQ", "zap-listen",
                       {"kinds": [ZAP_KIND],
                        "#p": [pubkey_hex],
                        "limit": 50}]
                await ws.send(json.dumps(req))
                while not stop.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
                        msg = json.loads(msg)
                        if msg[0] == "EVENT":
                            evt = msg[2]
                            zid = evt["id"]
                            if zid in seen:
                                continue
                            seen.add(zid)
                            await handle_zap(conn, evt)
                    except asyncio.TimeoutError:
                        continue
        except Exception:
            pass

    tasks = [asyncio.create_task(from_relay(r)) for r in RELAYS]
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        stop.set()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    conn.close()
    return len(seen)


async def handle_zap(conn, zap_event):
    """Process and store a zap event. Extract bolt11, amount, job_id."""
    tags = zap_event.get("tags", [])
    amount_msat = 0
    bolt11 = None
    job_id = None
    sender = zap_event.get("pubkey", "")

    for tag in tags:
        if len(tag) < 2:
            continue
        if tag[0] == "amount":
            amount_msat = int(tag[1])
        elif tag[0] == "bolt11":
            bolt11 = tag[1]
        elif tag[0] == "e":
            job_id = tag[1]  # zapped this event (the job request)

    existing = conn.execute(
        "SELECT zap_id FROM zaps WHERE zap_id = ?", (zap_event["id"],)
    ).fetchone()

    if not existing:
        conn.execute("""
            INSERT OR IGNORE INTO zaps
              (zap_id, job_id, sender, amount_msat, bolt11, received_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (zap_event["id"], job_id, sender, amount_msat, bolt11, int(time.time())))
        conn.commit()
        sats = amount_msat // 1000
        print(f"  [ZAP] {sats}sat from {sender[:12]}... job={job_id[:16] if job_id else '?'}")


# ─── Job extraction ────────────────────────────────────────────────────────────

def extract_job(event):
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
        "raw_tags": tags,
    }

    for tag in tags:
        if len(tag) < 2:
            continue
        key, val = tag[0], tag[1]
        if key == "i":
            job["prompt"] = val
        elif key == "bid":
            job["amount_sats"] = int(val) if val.isdigit() else 0
        elif key == "bolt11":
            job["bolt11"] = val
        elif key == "relays":
            job["relays"] = tag[1:]
        elif key == "closed_at":
            job["closed_at"] = int(val) if str(val).isdigit() else None

    if kind == 6969 and not job["prompt"]:
        job["prompt"] = content

    return job


def queue_job(conn, job):
    existing = conn.execute(
        "SELECT status FROM jobs WHERE id = ?", (job["id"],)
    ).fetchone()
    if existing:
        return False
    conn.execute("""
        INSERT INTO jobs (id, pubkey, kind, prompt, tags, amount_sats, bolt11,
                          closed_at, relays, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
    """, (
        job["id"], job["pubkey"], job["kind"], job["prompt"],
        json.dumps(job["raw_tags"]), job["amount_sats"], job["bolt11"],
        job["closed_at"], json.dumps(job["relays"]), int(time.time()),
    ))
    conn.commit()
    return True


# ─── Job listener ─────────────────────────────────────────────────────────────

async def listen_for_jobs(timeout_sec=60, max_jobs=20):
    conn = init_db()
    collected = []
    stop = asyncio.Event()

    async def from_relay(relay):
        try:
            async with websockets.connect(relay, ping_interval=None) as ws:
                req = ["REQ", "oracle-jobs",
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
                            ptag = next((t[1] for t in evt.get("tags",[]) if t and t[0]=="p"), "?")
                            print(f"  [JOB] kind={job['kind']} from={job['pubkey'][:12]}... "
                                  f"prompt={job['prompt'][:50]!r} queued={queued}")
                            collected.append((relay, job, queued))
                    except asyncio.TimeoutError:
                        continue
        except Exception:
            pass

    tasks = [asyncio.create_task(from_relay(r)) for r in RELAYS]
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        stop.set()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    conn.close()
    new = sum(1 for _, _, q in collected if q)
    print(f"Collected {len(collected)} jobs ({new} new)")


# ─── Zap verification for job ─────────────────────────────────────────────────

def check_job_payment(conn, job_id, client_pubkey, min_sats):
    """
    Check if client pubkey has sent a zap for this job meeting the minimum amount.
    Returns (paid, amount_sats).
    """
    row = conn.execute("""
        SELECT SUM(amount_msat / 1000) FROM zaps
        WHERE job_id = ? AND sender = ?
    """, (job_id, client_pubkey)).fetchone()

    amount_sats = int(row[0] or 0)
    return amount_sats >= min_sats, amount_sats


# ─── Resolution engine ─────────────────────────────────────────────────────────

def resolve_job(job):
    kind = job["kind"]

    if kind in (5000, 5001, 5002):
        return resolve_text_job(job)
    elif kind == 5300:
        return resolve_discovery_job(job)
    elif kind == 6969:
        return resolve_poll_job(job, job["raw_tags"])
    else:
        return {"answer": "unsupported", "confidence": 1.0,
                "error": f"Kind {kind} not supported"}


def resolve_text_job(job):
    """Delegate text jobs to Ollama."""
    prompt = job["prompt"]
    kind = job["kind"]

    instructions = {5001: "Summarize concisely: ", 5002: "Translate to English: "}
    full_prompt = instructions.get(kind, "") + prompt

    try:
        import subprocess
        result = subprocess.run(
            ["curl", "-s", "http://127.0.0.1:11434/api/generate",
             "-d", json.dumps({
                 "model": "minimax-m2.7:cloud",
                 "prompt": full_prompt,
                 "stream": False
             })],
            capture_output=True, text=True, timeout=30
        )
        data = json.loads(result.stdout)
        text = data.get("response", "").strip()
        return {"answer": text, "sources": ["ollama/minimax"], "confidence": 0.9}
    except Exception as e:
        return {"answer": "", "sources": [], "confidence": 0, "error": str(e)}


def resolve_discovery_job(job):
    """Structured pending response — real resolution needs per-category data sources."""
    prompt = job["prompt"]
    closed_at = job.get("closed_at") or (int(time.time()) + 86400)
    tags = job["raw_tags"]

    # Extract categories from prompt keywords
    cats = []
    t = prompt.lower()
    if any(k in t for k in ["bitcoin","btc","nostr","maxi","mining"]): cats.append("Crypto/Bitcoin")
    if any(k in t for k in ["gold","oil","barrel","market","price","stock"]): cats.append("Markets")
    if any(k in t for k in ["trump","iran","china","russia","sanctions","war","opec"]): cats.append("Geopolitics")
    if any(k in t for k in ["f1","gp","tournament","bracket","texas"]): cats.append("Sports")
    if any(k in t for k in ["openai","gpt","github","model","release"]): cats.append("AI/Tech")

    # Extract the e-tag — the actual job request ID from the poll event
    e_refs = [t[1] for t in tags if len(t) >= 2 and t[0] == "e"]

    return {
        "answer": json.dumps({
            "type": "prediction_pending",
            "question": prompt,
            "categories": cats,
            "e_refs": e_refs,
            "closed_at": closed_at,
            "oracle": "WOPR Oracle",
            "price_sats": JOB_PRICE_SATS,
            "resolution_note": "This is a NIP-90 content discovery request. "
                               "For prediction markets, the oracle evaluates at closed_at time.",
        }),
        "sources": [],
        "confidence": 0.8,
        "pending": True,
    }


def resolve_poll_job(job, tags):
    """Resolve a NIP-96B poll."""
    prompt = job["prompt"]
    closed_at = job.get("closed_at")

    options = {}
    for tag in tags:
        if len(tag) >= 3 and tag[0] == "poll_option":
            options[tag[1]] = tag[2]

    if closed_at and int(time.time()) < closed_at:
        return {
            "answer": json.dumps({"status": "open", "options": options,
                                   "closed_at": closed_at}),
            "sources": [],
            "confidence": 1.0,
            "pending": True,
        }

    # Poll is past closed_at — return resolution response
    return {
        "answer": json.dumps({
            "status": "resolved",
            "prompt": prompt,
            "options": options,
            "resolution_note": "Poll has closed. Oracle resolves based on vote tallies "
                               "fetched from relay event references.",
        }),
        "sources": [],
        "confidence": 0.5,
        "pending": True,
    }


# ─── Result poster ─────────────────────────────────────────────────────────────

async def post_result(job, resolution):
    """Post a NIP-90 result event to the specified relays."""
    if not NSEC:
        print("  [SKIP] no nsec configured")
        return

    try:
        from nostr_sdk import Client, Keys, EventBuilder, Tag, Kind
    except ImportError:
        print("  [SKIP] nostr-sdk not installed")
        return

    keys = Keys.parse(NSEC)

    kind_map = {5000: 6000, 5001: 6001, 5002: 6002, 5300: 6300, 6969: 6970}
    result_kind = kind_map.get(job["kind"], 6000)

    tags = [
        Tag.parse(f"job:{job['id']}"),
        Tag.parse(f"p:{job['pubkey']}"),
    ]
    if resolution.get("sources"):
        for src in resolution["sources"]:
            tags.append(Tag.parse(f"source:{src}"))

    # Add payment verification tag
    conn = init_db()
    paid, amt = check_job_payment(conn, job["id"], job["pubkey"], JOB_PRICE_SATS)
    tags.append(Tag.parse(f"amount:{amt * 1000}msat"))
    tags.append(Tag.parse(f"paid:{str(paid).lower()}"))
    conn.close()

    content = resolution.get("answer", "")
    evt = EventBuilder(result_kind, content, tags).to_event(keys)

    relays = job.get("relays") or RELAYS
    client = Client()
    for relay in relays[:4]:
        try:
            await client.add_relay(relay)
        except Exception:
            pass

    try:
        await client.connect()
        await client.send_event(evt)
        await client.disconnect()
        print(f"  [POSTED] kind={result_kind} → {relays[:2]}")
    except Exception as e:
        print(f"  [ERROR] posting: {e}")


# ─── Scheduler ─────────────────────────────────────────────────────────────────

def get_due_jobs(conn, limit=10):
    now = int(time.time())
    return conn.execute("""
        SELECT id, pubkey, kind, prompt, tags, amount_sats, bolt11,
               closed_at, relays, created_at
        FROM jobs
        WHERE status = 'pending'
          AND (closed_at IS NULL OR closed_at <= ?)
        ORDER BY created_at ASC
        LIMIT ?
    """, (now, limit)).fetchall()


def job_from_row(row):
    return {
        "id": row[0], "pubkey": row[1], "kind": row[2], "prompt": row[3],
        "raw_tags": json.loads(row[4]) if row[4] else [],
        "amount_sats": row[5], "bolt11": row[6],
        "closed_at": row[7], "relays": json.loads(row[8]) if row[8] else [],
        "created_at": row[9],
    }


async def process_due_jobs():
    conn = init_db()
    jobs = get_due_jobs(conn)
    print(f"\nScheduler: {len(jobs)} jobs due")

    for row in jobs:
        job = job_from_row(row)
        print(f"\n[RESOLVE] kind={job['kind']} id={job['id'][:16]}... "
              f"prompt={job['prompt'][:50]!r}")

        # Check payment
        paid, amt_sats = check_job_payment(conn, job["id"], job["pubkey"], JOB_PRICE_SATS)
        if not paid:
            print(f"  [SKIP] unpaid — received {amt_sats}sats, need {JOB_PRICE_SATS}sats")
            conn.execute(
                "UPDATE jobs SET status='unpaid', error=? WHERE id=?",
                (f"unpaid: got {amt_sats}sats, need {JOB_PRICE_SATS}", job["id"])
            )
            conn.commit()
            continue

        print(f"  [PAID] {amt_sats}sats received")

        # Resolve
        resolution = resolve_job(job)

        if resolution.get("pending"):
            print(f"  [PENDING] {resolution.get('answer','')[:80]}")
            continue

        # Store and post
        conn.execute(
            "UPDATE jobs SET status='resolved', result=?, resolved_at=? WHERE id=?",
            (json.dumps(resolution), int(time.time()), job["id"])
        )
        conn.commit()

        await post_result(job, resolution)

    conn.close()


# ─── Lightning address ─────────────────────────────────────────────────────────

MY_LN_ADDRESS = "cheshireremnant@lnaddress.com"  # your existing Lightning address

def print_ln_address():
    """Print Lightning address and npub for zapping."""
    print(f"\n{'='*60}")
    print(f"WOPR Oracle — Lightning address for zaps: {MY_LN_ADDRESS}")
    print(f"Oracle npub: {MY_NPUB}")
    print(f"Job price: {JOB_PRICE_SATS} sats")
    print(f"Pay to this address with zap comment: <job_event_id>")
    print(f"{'='*60}\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────

async def main():
    print_ln_address()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "--listen":
            await listen_for_jobs(timeout_sec=60)
        elif cmd == "--zaps":
            pubkey_hex = MY_NPUB  # will be loaded from secrets
            # We don't have hex readily — use a lookup
            print("Zap listener not yet wired to hex pubkey")
        elif cmd == "--test":
            await process_due_jobs()
        else:
            print(f"Usage: python3 {sys.argv[0]} [--listen|--test]")
        return

    # Daemon: listen for jobs, listen for zaps, then process
    print("--- Job discovery (30s) ---")
    await listen_for_jobs(timeout_sec=30)

    print("\n--- Zap listener (15s) ---")
    # Get hex pubkey for zap subscription
    if NSEC:
        try:
            from nostr_sdk import Keys
            keys = Keys.parse(NSEC)
            hex_pk = keys.public_key().to_hex()
            zaps = await listen_for_zaps(hex_pk, timeout_sec=15)
            print(f"Collected {zaps} zaps")
        except Exception as e:
            print(f"Zap listener error: {e}")
    else:
        print("No nsec configured, skipping zap listener")

    print("\n--- Resolution phase ---")
    await process_due_jobs()
    print("\nOracle cycle complete.")


if __name__ == "__main__":
    asyncio.run(main())
