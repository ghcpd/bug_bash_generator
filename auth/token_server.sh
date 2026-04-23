#!/usr/bin/env bash
# ============================================================================
# token_server.sh — Local token server for auth/prompt_generator.html (Linux/macOS)
# ============================================================================
# Usage:   bash scripts/generate_case/auth/token_server.sh
#
# Starts a tiny HTTP server on localhost:18923 using Python3.
# The HTML page's "Fetch Tokens" button calls this server →
# server runs `az` CLI → returns tokens as JSON → page auto-fills.
#
# Requires: python3, az CLI (logged in)
# Keep this running in a terminal. Press Ctrl+C to stop.
# ============================================================================

PORT="${1:-18923}"
RESOURCE_GROUP="${2:-acv-dp-wu2-p-001-rg}"
FACTORY_NAME="${3:-acv-dp-wu2-p-001-adf}"
BATCH_LINKED_SERVICE="${4:-gen_rubric}"

exec python3 -u - "$PORT" "$RESOURCE_GROUP" "$FACTORY_NAME" "$BATCH_LINKED_SERVICE" << 'PYEOF'
import sys, json, subprocess, re
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(sys.argv[1])
RG = sys.argv[2]
FACTORY = sys.argv[3]
BATCH_LS = sys.argv[4]

def run_az(*args):
    r = subprocess.run(["az"] + list(args), capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"az exited with {r.returncode}")
    return r.stdout.strip()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence default logs

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, obj):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/tokens":
            self._handle_tokens()
        elif self.path == "/health":
            self._json(200, {"ok": True})
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_tokens(self):
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"  [{ts}] Fetching tokens...", flush=True)
        try:
            mgmt = run_az("account", "get-access-token",
                          "--resource", "https://management.azure.com",
                          "--query", "accessToken", "-o", "tsv")
            storage = run_az("account", "get-access-token",
                             "--resource", "https://storage.azure.com",
                             "--query", "accessToken", "-o", "tsv")
            batch = run_az("account", "get-access-token",
                           "--resource", "https://batch.core.windows.net",
                           "--query", "accessToken", "-o", "tsv")

            result = {
                "ok": True,
                "azToken": mgmt,
                "azBlobToken": storage,
                "azBatchToken": batch,
            }

            # Try to get batch endpoint
            try:
                ls_json = run_az("datafactory", "linked-service", "show",
                                 "--factory-name", FACTORY,
                                 "--resource-group", RG,
                                 "--name", BATCH_LS,
                                 "--query", "properties.typeProperties", "-o", "json")
                ls = json.loads(ls_json)
                ep = ls.get("accountEndpoint") or ls.get("batchUri", "")
                ep = re.sub(r"^https://", "", ep)
                if ep:
                    result["azBatchEndpoint"] = ep
            except Exception:
                pass

            self._json(200, result)
            ts2 = datetime.now().strftime("%H:%M:%S")
            print(f"  [{ts2}] Tokens served OK", flush=True)

        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})
            ts2 = datetime.now().strftime("%H:%M:%S")
            print(f"  [{ts2}] ERROR: {e}", flush=True)

print()
print(f"  Token Server running on http://localhost:{PORT}")
print(f"  Open auth/prompt_generator.html and click [Fetch Tokens]")
print(f"  Press Ctrl+C to stop")
print()

HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
PYEOF
