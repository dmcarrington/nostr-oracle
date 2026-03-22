#!/bin/bash
# WOPR Oracle — Start all services
set -e
cd ~/.openclaw/workspace/nostr-oracle

echo "Starting WOPR Oracle..."

# Oracle daemon (jobs + zaps + resolution loop)
nohup python3 -u oracle.py >> oracle.log 2>&1 &
ORACLE_PID=$!
echo "Oracle PID: $ORACLE_PID"

# Zap webhook server (HTTP webhooks + nostr WS zap listener)
nohup python3 -u webhook.py >> webhook.log 2>&1 &
WEBHOOK_PID=$!
echo "Webhook PID: $WEBHOOK_PID"

sleep 2

# Verify both are up
if curl -s http://localhost:8766/health > /dev/null; then
    echo "Webhook: OK (http://localhost:8766)"
else
    echo "WARNING: webhook not responding"
fi

echo ""
echo "Logs:"
echo "  tail -f ~/.openclaw/workspace/nostr-oracle/oracle.log"
echo "  tail -f ~/.openclaw/workspace/nostr-oracle/webhook.log"
echo ""
echo "Announce:"
echo "  python3 announce.py"
