#!/usr/bin/env python3
"""Paper-trading dashboard for the Trader skill pack. Standard library only.

Serves one page that shows what the trader-* skills have done: the account,
positions against their targets, the proposals with their trader-risk
verdicts, and every decision trader-broker recorded. It is read-only: it
never approves or places an order. The only action is refreshing delayed
quotes through trader-alerts.

    python3 app/server.py                          # reads trader/ in the current folder
    python3 app/server.py --data app/demo/trader   # the bundled demo data
    python3 app/server.py --port 8765 --host 127.0.0.1

With ALPACA_API_KEY and ALPACA_SECRET_KEY set, the account, positions, orders
and equity history come from your Alpaca paper account instead of the local
files (GET requests only). ALPACA_BASE_URL defaults to the paper endpoint.
"""

import argparse
import datetime
import importlib.util
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.join(os.path.dirname(HERE), ".claude", "skills")
PAPER_URL = "https://paper-api.alpaca.markets"
SNAPSHOT_EVERY = datetime.timedelta(minutes=15)


def _load_ticket():
    """trader-broker's ticket.py, so fingerprints and ticket text match the skill exactly."""
    path = os.path.join(SKILLS, "trader-broker", "ticket.py")
    spec = importlib.util.spec_from_file_location("trader_ticket", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ticket = _load_ticket()


def read(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


# ---------------------------------------------------------------- local files

def local_account(data, prices):
    pf = read(os.path.join(data, "portfolio.json"))
    if not pf:
        return None, []
    rows = []
    for p in pf.get("positions", []):
        sym = str(p.get("ticker", "")).upper()
        shares = float(p.get("shares", 0))
        cost = sum(float(l.get("cost", 0)) for l in p.get("lots", []))
        price = prices.get(sym)
        value = shares * price if price is not None else None
        rows.append({"symbol": sym, "qty": shares, "cost": round(cost, 2), "price": price,
                     "value": round(value, 2) if value is not None else None,
                     "first_bought": min((l.get("date", "") for l in p.get("lots", [])), default="")})
    cash = float(pf.get("cash", 0))
    invested = sum(r["cost"] for r in rows)
    priced = all(r["value"] is not None for r in rows)
    equity = cash + sum(r["value"] for r in rows) if priced else None
    return {"source": "local", "cash": round(cash, 2), "invested": round(invested, 2),
            "equity": round(equity, 2) if equity is not None else None,
            "start": round(cash + invested, 2)}, rows


def local_activity(data):
    state = read(os.path.join(data, "state", "broker.json"), {"orders": []})
    out = []
    for r in state.get("orders", []):
        out.append({"at": r.get("at"), "symbol": r.get("symbol"), "side": r.get("side"),
                    "plain": r.get("plain"), "status": r.get("status"), "reply": r.get("reply"),
                    "broker_order_id": r.get("broker_order_id"), "note": r.get("note")})
    out.sort(key=lambda r: r.get("at") or "", reverse=True)
    return out


def snapshot(data, equity):
    """Append to trader/state/equity.json at most every SNAPSHOT_EVERY, so local mode has a history."""
    path = os.path.join(data, "state", "equity.json")
    doc = read(path, {"points": []})
    pts = doc.setdefault("points", [])
    t = now_utc()
    if pts:
        last = datetime.datetime.fromisoformat(pts[-1]["t"].replace("Z", "+00:00"))
        if t - last < SNAPSHOT_EVERY:
            return pts
    pts.append({"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "equity": equity})
    ticket.write_json(path, doc)
    return pts


# ---------------------------------------------------------------- Alpaca (read-only)

def alpaca_keys():
    k, s = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    return (k, s) if k and s else None


def alpaca_get(path):
    k, s = alpaca_keys()
    base = os.environ.get("ALPACA_BASE_URL", PAPER_URL).rstrip("/")
    req = urllib.request.Request(base + path, headers={"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def alpaca_state():
    acct = alpaca_get("/v2/account")
    positions = alpaca_get("/v2/positions")
    orders = alpaca_get("/v2/orders?status=all&limit=100&direction=desc")
    hist = alpaca_get("/v2/account/portfolio/history?period=3M&timeframe=1D")
    equity = float(acct["equity"])
    account = {"source": "alpaca", "cash": float(acct["cash"]), "equity": equity,
               "invested": sum(float(p["cost_basis"]) for p in positions),
               "start": float(hist.get("base_value") or acct.get("last_equity") or equity),
               "account_number": acct.get("account_number"), "status": acct.get("status")}
    rows = [{"symbol": p["symbol"], "qty": float(p["qty"]), "cost": float(p["cost_basis"]),
             "price": float(p["current_price"]), "value": float(p["market_value"]), "first_bought": ""}
            for p in positions]
    activity = []
    for o in orders:
        amount = ("$" + o["notional"]) if o.get("notional") else ("%s sh" % o.get("qty"))
        filled = ""
        if o.get("filled_avg_price"):
            filled = ", filled %s @ $%.2f" % (o.get("filled_qty"), float(o["filled_avg_price"]))
        activity.append({"at": o.get("filled_at") or o.get("submitted_at"), "symbol": o["symbol"],
                         "side": o["side"], "status": o["status"], "broker_order_id": o["id"],
                         "plain": "%s %s of %s, %s%s" % (o["side"].upper(), amount, o["symbol"],
                                                         o.get("type", ""), filled)})
    history = [{"t": datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "equity": e} for ts, e in zip(hist.get("timestamp", []), hist.get("equity", [])) if e is not None]
    return account, rows, activity, history


# ---------------------------------------------------------------- the whole page

def proposals(data, cfg):
    doc = read(os.path.join(data, "orders.json"))
    if not doc:
        return None
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    risk = read(os.path.join(data, "state", "risk.json"))
    state = ticket.load_state(os.path.join(data, "state", "broker.json"))
    today = datetime.date.today().isoformat()
    out = []
    for i, o in enumerate(orders, 1):
        fp = ticket.fingerprint(o)
        rv = next((r for r in (risk or {}).get("results", []) if r.get("fingerprint") == fp), None)
        dec = None
        for rec in state.get("orders", []):
            if rec.get("fingerprint") == fp:
                dec = rec
        ready, label, _ = ticket.status_of(o, risk, state, today)
        out.append({"index": i, "symbol": o.get("symbol"), "side": o.get("side"),
                    "plain": ticket.plain(o), "why": o.get("why", ""),
                    "verdict": rv.get("verdict") if rv else None,
                    "reasons": rv.get("reasons", []) if rv else [],
                    "risk_date": (risk or {}).get("date"),
                    "decision": dec.get("status") if dec else None,
                    "ready": ready, "status": label})
    return {"created_by": doc.get("created_by") if isinstance(doc, dict) else None,
            "created_at": doc.get("created_at") if isinstance(doc, dict) else None,
            "source": doc.get("source") if isinstance(doc, dict) else None, "orders": out}


def build_state(data):
    cfg = read(os.path.join(data, "config.json"), {})
    targets = read(os.path.join(data, "targets.json"))
    pdoc = read(os.path.join(data, "prices.json"), {})
    prices = {k.upper(): float(v) for k, v in pdoc.get("prices", {}).items()}
    warning = None
    if alpaca_keys():
        try:
            account, rows, activity, history = alpaca_state()
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            warning = "Alpaca no respondió (%s); se muestran los ficheros locales." % e
            account = None
    if not alpaca_keys() or warning:
        account, rows = local_account(data, prices)
        activity = local_activity(data)
        history = snapshot(data, account["equity"]) if account and account.get("equity") is not None else \
            read(os.path.join(data, "state", "equity.json"), {}).get("points", [])

    weights = {w["ticker"].upper(): float(w["weight"]) for w in (targets or {}).get("weights", [])}
    total = account["equity"] if account and account.get("equity") else None
    for r in rows:
        r["pnl"] = round(r["value"] - r["cost"], 2) if r.get("value") is not None else None
        r["pnl_pct"] = round(100 * r["pnl"] / r["cost"], 2) if r.get("pnl") is not None and r["cost"] else None
        r["weight"] = round(100 * r["value"] / total, 2) if total and r.get("value") is not None else None
        r["target"] = weights.get(r["symbol"])
    held = {r["symbol"] for r in rows}
    missing = [{"symbol": t, "target": w} for t, w in weights.items() if t not in held]

    return {
        "generated_at": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_dir": os.path.relpath(data),
        "warning": warning,
        "config": {"paper": bool(cfg.get("paper", True)), "broker": cfg.get("broker", "alpaca"),
                   "budget": cfg.get("budget"), "cash_buffer_pct": cfg.get("cash_buffer_pct"),
                   "max_position_pct": cfg.get("max_position_pct")},
        "account": account,
        "positions": sorted(rows, key=lambda r: -(r.get("value") or 0)),
        "missing_targets": missing,
        "targets": targets,
        "proposals": proposals(data, cfg),
        "activity": activity,
        "history": history,
        "prices_as_of": pdoc.get("as_of"),
    }


def refresh_prices(data):
    script = os.path.join(SKILLS, "trader-alerts", "alerts.py")
    cmd = [sys.executable, script, "quote", "--holdings",
           "--portfolio", os.path.join(data, "portfolio.json"),
           "--targets", os.path.join(data, "targets.json"),
           "--write", os.path.join(data, "prices.json")]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return out.returncode == 0, (out.stdout + out.stderr).strip()


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    data = "trader"

    def _send(self, code, body, ctype):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as fh:
                self._send(200, fh.read(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._json(200, build_state(self.data))
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/refresh-prices":
            ok, log = refresh_prices(self.data)
            self._json(200 if ok else 502, {"ok": ok, "log": log})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Paper-trading dashboard for the Trader skill pack.")
    ap.add_argument("--data", default="trader", help="the trader/ folder the skills write (default trader)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args(argv)
    Handler.data = os.path.abspath(a.data)
    if not os.path.isdir(Handler.data):
        print("No %s folder yet. Run a skill first, or try --data app/demo/trader." % a.data, file=sys.stderr)
    mode = "Alpaca paper (read-only)" if alpaca_keys() else "local files"
    print("Trader dashboard on http://%s:%d  data: %s  source: %s" % (a.host, a.port, Handler.data, mode))
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
