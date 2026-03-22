#!/usr/bin/env python3
"""
WOPR Oracle — Zap webhook server.
Receives HTTP webhooks from Lightning providers (Alby, etc.) when zaps arrive.
Also listens for NIP-57 nostr zaps via WebSocket relay subscription.

Usage:
    python3 webhook.py          # run server on port 8766
    python3 webhook.py --test  # test zap verification
"""

import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs

import websockets

ORACLE_DIR = Path(__file__).parent
DB_PATH = ORACLE_DIR / "data" / "oracle.db"
WEBHOOK_PORT = 8766
LISTEN_PORT = 8767

NSEC_FILE = Path.home() / ".openclaw" / "secrets" / "oracle-nsec"
MY_RELAYS = [
    "wss://relay.damus.io",
    "wss://relay.nostr.net",
    "wss://nos.lol",
    "wss://relay.primal.net",
]

ZAP_KIND = 9735


# ─── DB helpers ───────────────────────────────────────────────────────────────

def init_db():
    from oracle import init_db as oracle_init
    return oracle_init()


def store_zap(conn, zap_event, amount_msat=None, bolt11=None):
    """Store a verified zap in the DB."""
    tags = zap_event.get("tags", [])
    job_id = None
    sender = zap_event.get("pubkey", "")

    for tag in tags:
        if len(tag) >= 2:
            if tag[0] == "e":
                job_id = tag[1]
            elif tag[0] == "amount":
                amount_msat = int(tag[1])

    zap_id = zap_event.get("id", "")

    conn.execute("""
        INSERT OR IGNORE INTO zaps
          (zap_id, job_id, sender, amount_msat, bolt11, verified, received_at)
        VALUES (?, ?, ?, ?, ?, 1, ?)
    """, (zap_id, job_id, sender, amount_msat or 0, bolt11, int(time.time())))
    conn.commit()

    sats = (amount_msat or 0) // 1000
    print(f"  [ZAP-STORED] id={zap_id[:16]}... job={job_id[:16] if job_id else '?'} "
          f"sender={sender[:12]}... {sats}sats")


def verify_nostr_zap(zap_event):
    """
    Verify a NIP-57 nostr zap event.
    Returns (ok, error_message).
    """
    try:
        from nostr_sdk import Event
        evt = Event.from_json(json.dumps(zap_event))
        if not evt.verify():
            return False, "invalid signature"
        # Check it's a zap receipt
        if evt.kind().as_u16() != ZAP_KIND:
            return False, f"not a zap (kind={evt.kind().as_u16()})"
        return True, None
    except Exception as e:
        return False, str(e)


async def listen_for_nostr_zaps():
    """Subscribe to relays for NIP-57 zap receipts targeting our pubkey."""
    if not NSEC_FILE.exists():
        print("No nsec — skipping nostr zap listener")
        return

    from nostr_sdk import Keys
    keys = Keys.parse(NSEC_FILE.read_text().strip())
    hex_pk = keys.public_key().to_hex()

    conn = init_db()
    seen = set()

    print(f"Listening for nostr zaps to {hex_pk[:16]}... on {len(MY_RELAYS)} relays")

    async def from_relay(relay):
        try:
            async with websockets.connect(relay, ping_interval=None) as ws:
                req = ["REQ", "zap receipts",
                       {"kinds": [ZAP_KIND], "#p": [hex_pk]}]
                await ws.send(json.dumps(req))
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=15.0)
                        msg = json.loads(msg)
                        if msg[0] == "EVENT":
                            evt = msg[2]
                            if evt["id"] in seen:
                                continue
                            seen.add(evt["id"])
                            ok, err = verify_nostr_zap(evt)
                            if ok:
                                store_zap(conn, evt)
                            else:
                                print(f"  [ZAP-REJECTED] {err}")
                    except asyncio.TimeoutError:
                        continue
        except Exception as e:
            print(f"  [!] {relay}: {e}")

    tasks = [asyncio.create_task(from_relay(r)) for r in MY_RELAYS]
    await asyncio.gather(*tasks, return_exceptions=True)
    conn.close()


# ─── HTTP webhook handler ──────────────────────────────────────────────────────

def load_keys():
    from nostr_sdk import Keys
    return Keys.parse(NSEC_FILE.read_text().strip())


class ZapWebhookHandler(BaseHTTPRequestHandler):
    """Receives HTTP webhooks from Lightning providers (Alby, etc.)."""

    def log_message(self, fmt, *args):
        print(f"  [WEBHOOK] {fmt % args}")

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        if self.path == "/health":
            self.send_json({"status": "ok", "time": datetime.utcnow().isoformat() + "Z"})
        elif self.path == "/":
            self.send_json({
                "oracle": "WOPR Oracle",
                "pubkey": load_keys().public_key().to_bech32(),
                "lightning_address": "cheshireremnant@lnaddress.com",
                "price_sats": 50,
            })
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        import sqlite3 as sqlite_local

        if self.path != "/webhook":
            self.send_json({"error": "not found"}, 404)
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode()
            data = json.loads(body) if body else {}
        except Exception as e:
            self.send_json({"error": f"bad request: {e}"}, 400)
            return

        # Route based on provider
        provider = data.get("provider", "")
        conn = init_db()

        try:
            if provider == "alby":
                self.handle_alby_webhook(conn, data)
            else:
                # Generic zap handling
                self.handle_generic_webhook(conn, data)
        finally:
            conn.close()

    def handle_alby_webhook(self, conn, data):
        """
        Handle Alby webhook payload.
        Alby sends: { "zap_event": {...}, "amount": msats, "bolt11": "...", "memo": "..." }
        """
        zap_event = data.get("zap_event")
        amount_msat = int(data.get("amount", 0))
        bolt11 = data.get("bolt11", "")
        memo = data.get("memo", "")  # job event ID

        if not zap_event:
            self.send_json({"error": "no zap_event"}, 400)
            return

        # Verify nostr zap signature
        ok, err = verify_nostr_zap(zap_event)
        if not ok:
            print(f"  [ZAP-REJECTED] Alby webhook: {err}")
            self.send_json({"error": f"invalid zap: {err}"}, 400)
            return

        # Store
        tags = zap_event.get("tags", [])
        job_id = memo  # Alby passes job ID in memo field
        sender = zap_event.get("pubkey", "")
        zap_id = zap_event.get("id", "")

        conn.execute("""
            INSERT OR IGNORE INTO zaps
              (zap_id, job_id, sender, amount_msat, bolt11, verified, received_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
        """, (zap_id, job_id, sender, amount_msat, bolt11, int(time.time())))
        conn.commit()

        sats = amount_msat // 1000
        print(f"  [ALBY-ZAP] {sats}sats from {sender[:12]}... job={job_id[:16] if job_id else '?'}")
        self.send_json({"status": "ok", "sats": sats, "zap_id": zap_id[:16]})

    def handle_generic_webhook(self, conn, data):
        """Generic webhook: accept any JSON with amount_msat and optional job_id."""
        amount_msat = int(data.get("amount_msat", data.get("amount", 0)))
        job_id = data.get("job_id", data.get("event_id", ""))
        sender = data.get("pubkey", data.get("sender", ""))
        zap_id = data.get("zap_id", data.get("id", ""))
        bolt11 = data.get("bolt11", "")

        conn.execute("""
            INSERT OR IGNORE INTO zaps
              (zap_id, job_id, sender, amount_msat, bolt11, verified, received_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
        """, (zap_id, job_id, sender, amount_msat, bolt11, int(time.time())))
        conn.commit()

        sats = amount_msat // 1000
        print(f"  [WEBHOOK-ZAP] {sats}sats from {sender[:12]}... job={job_id[:16] if job_id else '?'}")
        self.send_json({"status": "ok", "sats": sats})


# ─── Main: run webhook server + nostr zap listener ─────────────────────────────

async def main():
    print(f"WOPR Oracle Webhook Server")
    print(f"HTTP webhook endpoint: http://localhost:{WEBHOOK_PORT}/webhook")
    print(f"NIP-57 nostr zaps: listening on {len(MY_RELAYS)} relays")

    # Run nostr zap listener in background
    zap_task = asyncio.create_task(listen_for_nostr_zaps())

    # Run HTTP webhook server
    server = HTTPServer(("0.0.0.0", WEBHOOK_PORT), ZapWebhookHandler)
    print(f"HTTP server running on port {WEBHOOK_PORT}")
    print()

    # Run until interrupted
    try:
        async_task = asyncio.create_task(asyncio.to_thread(server.serve_forever))
        await asyncio.gather(zap_task, async_task)
    except asyncio.CancelledError:
        server.shutdown()
        print("Server stopped.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        print("Testing zap verification...")
        from oracle import init_db
        conn = init_db()
        row = conn.execute("SELECT * FROM zaps ORDER BY received_at DESC LIMIT 3").fetchall()
        print(f"Recent zaps in DB: {len(row)}")
        for r in row:
            print(f"  {r}")
        conn.close()
    else:
        asyncio.run(main())
