"""
Tiny local web server exposing two things:
  - GET  /              -> serves index.html and static files
  - POST /api/scenario  -> takes {"hypothesis": "..."} and returns a structured card

Run with:
    set GEMINI_API_KEY=your_key_here
    python scenario.py

Then open http://localhost:8765 in your browser.
"""

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import google.generativeai as genai

PORT = 8765
ROOT = os.path.dirname(os.path.abspath(__file__))
TICKERS_PATH = os.path.join(ROOT, "tickers.json")
MODEL_NAME = "gemini-2.5-flash-lite"


def load_universe():
    with open(TICKERS_PATH, encoding="utf-8") as f:
        return json.load(f)["tickers"]


SCENARIO_SCHEMA = """
Return a single JSON object with this exact shape:

{
  "ok": true,
  "error": "",
  "plain_headline": "<one-line restatement of the user's hypothetical in plain English>",
  "interpretation": "<2-3 sentences: how you understood the scenario, including any assumptions (e.g. time horizon assumed)>",
  "scenarios": {
    "base": {
      "label": "<6-10 word label>",
      "condition": "<what would have to hold for this branch>",
      "horizon": "<e.g. 'next quarter'>",
      "likelihood": "<one of: most_likely, plausible, less_likely, tail_risk>",
      "likelihood_pct": "<integer 0-100; rough qualitative estimate; the three scenarios should sum to ~100>",
      "tickers": [{"ticker": "...", "direction": "benefits|hurts|mixed", "expected_move": "small|moderate|large", "reason": "<one specific sentence>"}]
    },
    "upside": { ... same shape ... },
    "downside": { ... same shape ... }
  }
}

Rules:
1. ONLY use tickers from the provided universe. Never invent.
2. Each ticker must include a SPECIFIC mechanism (revenue line, geography, cost, product).
3. Each scenario should list 2-4 tickers max.
4. expected_move buckets: small ~1-2%, moderate ~3-5%, large >5% over the horizon. Use "large" sparingly.
5. likelihood_pct is a rough qualitative estimate — be honest if upside or downside is more likely than the base case. They should sum to ~100.
6. If the hypothesis is too vague or outside the universe's reach, return {"ok": false, "error": "<short explanation>"}.
7. Return ONLY the JSON object. No markdown fences, no preamble.
"""


def build_prompt(hypothesis: str, universe) -> str:
    universe_str = "\n".join(
        f"- {t['ticker']} ({t['sector']}): {t['description']}" for t in universe
    )
    return f"""You are a financial scenario analyst writing for a student audience.
The user proposed a hypothetical. Produce three scenarios (base / upside / downside) showing
how it could play out, with specific affected stocks from the provided universe.

USER HYPOTHESIS:
{hypothesis}

TICKER UNIVERSE (only reference these):
{universe_str}

{SCENARIO_SCHEMA}
"""


class Handler(BaseHTTPRequestHandler):
    universe = None
    model = None

    def log_message(self, fmt, *args):
        # quieter logging
        sys.stderr.write(f"[scenario] {self.address_string()} - {fmt % args}\n")

    def _send(self, status, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, rel_path):
        path = os.path.join(ROOT, rel_path.lstrip("/"))
        if not os.path.isfile(path):
            self._send(404, {"error": "not found"})
            return
        ext = os.path.splitext(path)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            data = f.read()
        self._send(200, data, ctype)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/" or url.path == "":
            self._serve_file("index.html")
        else:
            self._serve_file(url.path)

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/api/scenario":
            self._send(404, {"error": "unknown endpoint"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send(400, {"ok": False, "error": "invalid JSON body"})
            return
        hypothesis = (payload.get("hypothesis") or "").strip()
        if not hypothesis or len(hypothesis) < 5:
            self._send(400, {"ok": False, "error": "please provide a hypothesis"})
            return
        if len(hypothesis) > 500:
            hypothesis = hypothesis[:500]

        try:
            resp = Handler.model.generate_content(build_prompt(hypothesis, Handler.universe))
            text = resp.text.strip()
            if text.startswith("```"):
                text = re.sub(r"^```[a-zA-Z]*\n?|```$", "", text, flags=re.MULTILINE).strip()
            data = json.loads(text)
        except Exception as e:
            self._send(500, {"ok": False, "error": f"model call failed: {e}"})
            return

        self._send(200, data)


def pick_model(preferred):
    candidates = [preferred, "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    try:
        available = [m.name.split("/")[-1] for m in genai.list_models() if "generateContent" in getattr(m, "supported_generation_methods", [])]
    except Exception:
        return preferred
    for c in candidates:
        if c in available:
            if c != preferred:
                print(f"note: '{preferred}' not available; using '{c}'", file=sys.stderr)
            return c
    print(f"ERROR: none of {candidates} are available.", file=sys.stderr)
    sys.exit(1)


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY environment variable is not set.", file=sys.stderr)
        print("Get a free key at https://aistudio.google.com/app/apikey", file=sys.stderr)
        sys.exit(1)
    genai.configure(api_key=api_key)
    model_name = pick_model(MODEL_NAME)
    Handler.model = genai.GenerativeModel(
        model_name,
        generation_config={"response_mime_type": "application/json", "temperature": 0.4},
    )
    Handler.universe = load_universe()

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Open http://localhost:{PORT} in your browser")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
