#!/usr/bin/env python3
"""PS5 Pro stock tracker for PlayStation Direct (playstation.com, nl-nl) with ntfy push alerts.

How it works
------------
The buy-now page is server-rendered (Next.js) and embeds the stock state of the
product it sells in two machine-readable places:

1. ``<script type="application/ld+json">`` -> ``offers.availability``
   (``https://schema.org/InStock`` / ``.../OutOfStock`` / ...)
2. The React Server Components payload (``self.__next_f.push(...)``) which
   contains ``"skus":[{"id": ..., "stockLevelStatus": "IN_STOCK" | "OUT_OF_STOCK", ...}]``

Only the page's *own* SKU(s) (from ``<meta name="skus">``) are evaluated, so
"other consoles" carousels on the same page cannot trigger false alarms. The
add-to-cart button type is deliberately ignored: on playstation.com it can say
``ADD_TO_CART`` while the product is out of stock.

Only the Python standard library is used (Python 3.8+).

Usage
-----
    export NTFY_TOPIC=your-secret-topic
    python3 tracker.py                 # check forever (default every 60 s)
    python3 tracker.py --once          # one check, for cron / GitHub Actions
    python3 tracker.py --check         # one check, print details, never notify
    python3 tracker.py --test-notify   # send a test push to your phone
    python3 tracker.py --new-topic     # print a random, hard-to-guess topic name

Run ``python3 tracker.py --help`` for all options.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import html
import http.client
import json
import os
import random
import re
import secrets
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional

__version__ = "1.0.0"

DEFAULT_URL = "https://www.playstation.com/nl-nl/ps5/ps5-pro/buy-now/"
DEFAULT_NTFY_SERVER = "https://ntfy.sh"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Overall statuses
IN_STOCK = "IN_STOCK"
OUT_OF_STOCK = "OUT_OF_STOCK"
UNKNOWN = "UNKNOWN"  # stock data found, but with values we do not recognise
QUEUE = "QUEUE"  # redirected to / served a waiting room: very likely a drop

# schema.org availability values (last path segment)
LD_AVAILABLE = {"InStock", "LimitedAvailability", "PreOrder", "PreSale", "BackOrder", "OnlineOnly"}
LD_UNAVAILABLE = {"OutOfStock", "SoldOut", "Discontinued", "InStoreOnly"}

# playstation.com skus[].stockLevelStatus values
SKU_AVAILABLE = {"IN_STOCK", "LOW_STOCK", "LIMITED_STOCK", "AVAILABLE", "PRE_ORDER", "PREORDER", "BACKORDER", "BACK_ORDER"}
SKU_UNAVAILABLE = {"OUT_OF_STOCK", "SOLD_OUT", "UNAVAILABLE", "NOT_AVAILABLE", "DISCONTINUED", "NO_STOCK"}

NTFY_TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{1,64}$")


class TrackerError(Exception):
    """A check failed (network error, blocked, page layout changed, ...)."""


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Page analysis (pure functions, unit tested)
# --------------------------------------------------------------------------- #

class _PageParser(HTMLParser):
    """Collects <meta name=... content=...>, JSON-LD blocks and inline scripts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: Dict[str, str] = {}
        self.ld_json: List[str] = []
        self.scripts: List[str] = []
        self._in_script = False
        self._script_is_ld = False
        self._buf: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            name = a.get("name") or a.get("property")
            if name and "content" in a and name not in self.meta:
                self.meta[name] = a["content"]
        elif tag == "script":
            self._in_script = True
            self._script_is_ld = a.get("type", "").lower() == "application/ld+json"
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_script:
            text = "".join(self._buf)
            if self._script_is_ld:
                self.ld_json.append(text)
            elif "__next_f" in text:
                self.scripts.append(text)
            self._in_script = False
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._buf.append(data)


@dataclass
class Signal:
    source: str  # "json-ld" | "sku" | "sku-fallback"
    value: str  # raw value, e.g. "OutOfStock" / "IN_STOCK"
    verdict: str  # "available" | "unavailable" | "unknown"
    sku: str = ""
    extra: str = ""

    def describe(self) -> str:
        who = f" {self.sku}" if self.sku else ""
        extra = f" ({self.extra})" if self.extra else ""
        return f"{self.source}{who}: {self.value}{extra}"


@dataclass
class Result:
    status: str
    product: str = "PS5 Pro"
    price: str = ""
    skus: List[str] = field(default_factory=list)
    signals: List[Signal] = field(default_factory=list)
    reason: str = ""
    url: str = ""

    def summary(self) -> str:
        parts = [self.status, self.product]
        if self.price:
            parts.append(self.price)
        line = " | ".join(parts)
        if self.signals:
            line += " | " + "; ".join(s.describe() for s in self.signals)
        if self.reason:
            line += f" | {self.reason}"
        return line


def _ld_verdict(value: str) -> str:
    if value in LD_AVAILABLE:
        return "available"
    if value in LD_UNAVAILABLE:
        return "unavailable"
    return "unknown"


def _sku_verdict(value: str) -> str:
    v = value.upper()
    if v in SKU_AVAILABLE:
        return "available"
    if v in SKU_UNAVAILABLE:
        return "unavailable"
    return "unknown"


def _as_list(x: Any) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _iter_ld_products(node: Any) -> Iterable[dict]:
    """Yield every schema.org Product object in a JSON-LD document."""
    if isinstance(node, list):
        for item in node:
            yield from _iter_ld_products(item)
    elif isinstance(node, dict):
        types = _as_list(node.get("@type"))
        if "Product" in types or "ProductGroup" in types:
            yield node
        for key in ("@graph", "hasVariant", "itemListElement", "item"):
            if key in node:
                yield from _iter_ld_products(node[key])


def _iter_ld_offers(offers: Any) -> Iterable[dict]:
    for offer in _as_list(offers):
        if isinstance(offer, dict):
            yield offer
            if "offers" in offer:  # AggregateOffer
                yield from _iter_ld_offers(offer["offers"])


def _decode_rsc(scripts: List[str]) -> str:
    """Concatenate the string chunks of ``self.__next_f.push([1, "..."])`` calls."""
    decoder = json.JSONDecoder()
    out: List[str] = []
    for text in scripts:
        for m in re.finditer(r"self\.__next_f\.push\(\s*", text):
            try:
                arr, _ = decoder.raw_decode(text, m.end())
            except ValueError:
                continue
            if isinstance(arr, list) and len(arr) > 1 and isinstance(arr[1], str):
                out.append(arr[1])
    return "".join(out)


def _find_sku_objects(text: str) -> List[dict]:
    """Return every object inside a ``"skus":[...]`` array that has a stockLevelStatus."""
    decoder = json.JSONDecoder()
    found: List[dict] = []
    for m in re.finditer(r'"skus"\s*:\s*\[', text):
        try:
            arr, _ = decoder.raw_decode(text, m.end() - 1)
        except ValueError:
            continue
        for obj in _as_list(arr):
            if isinstance(obj, dict) and isinstance(obj.get("id"), str) and "stockLevelStatus" in obj:
                found.append(obj)
    return found


def _fallback_sku_status(raw_html: str, sku: str) -> List[str]:
    """Regex fallback if the RSC payload cannot be decoded: strip JSON escaping and look
    for the first stockLevelStatus that belongs to this SKU's object."""
    flat = raw_html.replace("\\", "")
    values: List[str] = []
    for m in re.finditer(r'"id"\s*:\s*"' + re.escape(sku) + r'"', flat):
        window = flat[m.end(): m.end() + 3000]
        nxt = re.search(r'\{\s*"id"\s*:', window)
        if nxt:
            window = window[: nxt.start()]
        s = re.search(r'"stockLevelStatus"\s*:\s*"([A-Za-z_]+)"', window)
        if s:
            values.append(s.group(1))
    return values


def analyze(raw_html: str, target_skus: Optional[List[str]] = None, final_url: str = "") -> Result:
    """Work out the stock status of the page's own product.

    Raises TrackerError if the page contains no stock information at all.
    """
    host = (urllib.parse.urlsplit(final_url).hostname or "") if final_url else ""
    if "queue" in host:
        return Result(status=QUEUE, reason=f"redirected to waiting room {host}", url=final_url)

    p = _PageParser()
    try:
        p.feed(raw_html)
        p.close()
    except Exception as exc:  # HTMLParser is lenient; this is a last resort
        raise TrackerError(f"could not parse HTML: {exc}") from exc

    meta_skus = [s.strip() for s in p.meta.get("skus", "").split(",") if s.strip()]
    product_id = p.meta.get("productId", "").strip()
    skus = [s for s in (target_skus or meta_skus) if s]

    result = Result(status=UNKNOWN, skus=skus, url=final_url)
    signals: List[Signal] = []

    # 1) JSON-LD Product offers
    ld_products = []
    for block in p.ld_json:
        try:
            ld_products.extend(_iter_ld_products(json.loads(block)))
        except ValueError:
            continue
    # The page's productId only identifies the page's own product, so it is not used to
    # match JSON-LD when the user explicitly asked for other SKU(s).
    match_product_id = product_id if not target_skus else ""
    if skus or match_product_id:
        matching = [
            prod for prod in ld_products
            if str(prod.get("sku", "")).strip() in skus
            or (match_product_id and str(prod.get("productID", "")).strip() == match_product_id)
        ]
        # If nothing matches by id but there's exactly one product, it's the page's own.
        if not matching and len(ld_products) == 1 and not target_skus:
            matching = ld_products
    else:
        matching = ld_products
    for prod in matching:
        name = html.unescape(str(prod.get("name", ""))).strip()
        if name and result.product == "PS5 Pro":
            result.product = name
        for offer in _iter_ld_offers(prod.get("offers")):
            avail = offer.get("availability")
            if not isinstance(avail, str) or not avail.strip():
                continue
            value = avail.strip().rstrip("/").rsplit("/", 1)[-1]
            signals.append(Signal("json-ld", value, _ld_verdict(value), sku=str(prod.get("sku", ""))))
            if not result.price and offer.get("price") not in (None, ""):
                result.price = f"{offer.get('price')} {offer.get('priceCurrency', '')}".strip()

    # 2) skus[].stockLevelStatus from the React Server Components payload
    rsc = _decode_rsc(p.scripts)
    sku_signals: List[Signal] = []
    seen = set()
    if rsc and skus:
        for obj in _find_sku_objects(rsc):
            sid = obj["id"]
            if sid not in skus:
                continue
            value = str(obj.get("stockLevelStatus", ""))
            attrs = obj.get("physicalAttributes") or {}
            override = attrs.get("inventoryOverride") if isinstance(attrs, dict) else None
            key = (sid, value, override)
            if key in seen:
                continue
            seen.add(key)
            extra = f"inventoryOverride={override}" if override and override != "notSpecified" else ""
            sku_signals.append(Signal("sku", value, _sku_verdict(value), sku=sid, extra=extra))
            if obj.get("displayPrice"):
                result.price = str(obj["displayPrice"])
    if not sku_signals and skus:
        for sid in skus:
            for value in dict.fromkeys(_fallback_sku_status(raw_html, sid)):
                sku_signals.append(Signal("sku-fallback", value, _sku_verdict(value), sku=sid))
    signals.extend(sku_signals)
    result.signals = signals

    if not signals:
        if "queue-it" in raw_html.lower():
            # A page without product data that loads a Queue-it waiting room.
            result.status = QUEUE
            result.reason = "waiting room (queue-it) page served instead of the product page"
            return result
        if target_skus and meta_skus and not set(target_skus) & set(meta_skus):
            raise TrackerError(
                f"configured SKU(s) {', '.join(target_skus)} not on page (page sells {', '.join(meta_skus)})"
            )
        raise TrackerError("no stock information found on the page (layout changed, or blocked/captcha page)")

    verdicts = {s.verdict for s in signals}
    if "available" in verdicts:
        result.status = IN_STOCK
        if "unavailable" in verdicts:
            result.reason = "signals disagree; treating as in stock to be safe"
    elif "unknown" in verdicts:
        result.status = UNKNOWN
        odd = sorted({s.value for s in signals if s.verdict == "unknown"})
        result.reason = "unrecognised stock value(s): " + ", ".join(odd)
    else:
        result.status = OUT_OF_STOCK
    return result


# --------------------------------------------------------------------------- #
# Networking
# --------------------------------------------------------------------------- #

def fetch(url: str, timeout: float = 30.0) -> tuple:
    """GET the page. Returns (html_text, final_url)."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            final_url = resp.geturl()
            charset = resp.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        hint = ""
        if exc.code in (403, 429):
            hint = " (blocked or rate-limited by the site; try a longer interval or another network)"
        raise TrackerError(f"HTTP {exc.code} from {url}{hint}") from exc
    except (urllib.error.URLError, OSError) as exc:  # includes timeouts and resets
        raise TrackerError(f"network error: {getattr(exc, 'reason', exc)}") from exc
    except http.client.HTTPException as exc:  # IncompleteRead (truncated body), BadStatusLine, ...
        raise TrackerError(f"bad HTTP response: {exc!r}") from exc
    try:
        if encoding == "gzip":
            body = gzip.decompress(body)
        elif encoding == "deflate":
            try:
                body = zlib.decompress(body)
            except zlib.error:
                body = zlib.decompress(body, -zlib.MAX_WBITS)
    except (OSError, zlib.error, EOFError) as exc:
        raise TrackerError(f"could not decompress response: {exc}") from exc
    try:
        return body.decode(charset, errors="replace"), final_url
    except LookupError:  # unknown charset name in Content-Type
        return body.decode("utf-8", errors="replace"), final_url


def check(url: str, target_skus: Optional[List[str]] = None, timeout: float = 30.0) -> Result:
    text, final_url = fetch(url, timeout=timeout)
    result = analyze(text, target_skus=target_skus, final_url=final_url)
    if not result.url:
        result.url = final_url
    return result


# --------------------------------------------------------------------------- #
# ntfy
# --------------------------------------------------------------------------- #

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would silently turn a redirected POST into a GET (dropping the message)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(req.full_url, code, f"redirect to {newurl} refused - fix NTFY_SERVER",
                                     headers, fp)


_NTFY_OPENER = urllib.request.build_opener(_NoRedirect)
NTFY_RATE_LIMIT_PAUSE = 15 * 60  # seconds to stop publishing after ntfy answers 429


@dataclass
class Notifier:
    server: str
    topic: str
    token: str = ""
    user: str = ""
    password: str = ""
    dry_run: bool = False
    blocked_until: float = 0.0

    def send(self, title: str, message: str, priority: int = 3, tags: Optional[List[str]] = None,
             click: str = "", action_label: str = "") -> bool:
        """Publish via ntfy's JSON API. Returns True only if ntfy accepted the message; never raises."""
        payload: Dict[str, Any] = {
            "topic": self.topic,
            "title": title,
            "message": message,
            "priority": priority,
            "tags": tags or [],
        }
        if click:
            payload["click"] = click
            if action_label:
                payload["actions"] = [{"action": "view", "label": action_label, "url": click, "clear": True}]
        if self.dry_run:
            log(f"[dry-run] would notify: {title} - {message}")
            return True
        if time.time() < self.blocked_until:
            log(f"ntfy: rate-limited, not sending '{title}' until {datetime.fromtimestamp(self.blocked_until):%H:%M}")
            return False
        headers = {"Content-Type": "application/json", "User-Agent": f"ps5-pro-tracker/{__version__}"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.user:
            cred = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            headers["Authorization"] = f"Basic {cred}"
        try:
            req = urllib.request.Request(
                self.server.rstrip("/") + "/",
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
        except ValueError as exc:
            log(f"ntfy: invalid server URL {self.server!r}: {exc}")
            return False
        for attempt in range(3):
            if attempt:
                time.sleep(2 * attempt)
            try:
                with _NTFY_OPENER.open(req, timeout=15) as resp:
                    body = resp.read()
                try:
                    reply = json.loads(body.decode("utf-8", "replace"))
                except ValueError:
                    reply = None
                if isinstance(reply, dict) and reply.get("id"):
                    return True
                log(f"ntfy: unexpected reply from {self.server} (is this an ntfy server?): {body[:120]!r}")
                return False
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read().decode("utf-8", "replace")[:200]
                except Exception:  # noqa: BLE001
                    detail = str(exc.reason)
                log(f"ntfy error HTTP {exc.code}: {detail or exc.reason}")
                if exc.code == 429:
                    self.blocked_until = time.time() + NTFY_RATE_LIMIT_PAUSE
                    return False
                if exc.code < 500:
                    return False  # auth / bad request / redirect: retrying will not help
            except ValueError as exc:  # e.g. a token containing a newline: configuration, not network
                log(f"ntfy: invalid request ({exc}) - check NTFY_SERVER / NTFY_TOKEN")
                return False
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                log(f"ntfy network error: {getattr(exc, 'reason', exc)!r}")
        return False


# --------------------------------------------------------------------------- #
# State machine (shared by loop mode and --once)
# --------------------------------------------------------------------------- #

@dataclass
class State:
    status: Optional[str] = None
    detail: str = ""
    last_alert: float = 0.0  # last in-stock / queue alert (epoch seconds)
    alerts_sent: int = 0  # in-stock / queue alerts sent in the current episode
    unknown_alerted: str = ""  # reason of the last successfully sent "status changed" alert
    consecutive_errors: int = 0
    error_alert_sent: bool = False
    started_sent: bool = False

    @classmethod
    def load(cls, path: str) -> "State":
        if not path or not os.path.exists(path):
            return cls()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
            return cls(**known)
        except (OSError, ValueError, TypeError) as exc:
            log(f"warning: ignoring unreadable state file {path}: {exc}")
            return cls()

    def save(self, path: str) -> None:
        if not path:
            return
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.__dict__, f, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            log(f"warning: could not write state file {path}: {exc}")


@dataclass
class Config:
    url: str
    skus: List[str]
    interval: float
    remind_every: float
    error_alert_after: int
    timeout: float
    start_message: bool


def _now_str() -> str:
    return datetime.now().astimezone().strftime("%H:%M:%S")


def process(result: Optional[Result], error: Optional[str], state: State, cfg: Config,
            notifier: Notifier, now: float, loop_mode: bool) -> None:
    """Update state for one check and send whatever notifications are due."""
    if error is not None:
        state.consecutive_errors += 1
        log(f"check failed ({state.consecutive_errors}x in a row): {error}")
        if state.consecutive_errors >= cfg.error_alert_after and not state.error_alert_sent:
            ok = notifier.send(
                "PS5 tracker: checks are failing",
                f"{state.consecutive_errors} checks in a row failed. Last error: {error}\n"
                "You will NOT get stock alerts until this is fixed.",
                priority=4, tags=["warning"], click=cfg.url,
            )
            state.error_alert_sent = ok
        return

    assert result is not None
    if state.error_alert_sent:
        notifier.send("PS5 tracker: working again", f"Checks succeed again. Status: {result.status}.",
                      priority=2, tags=["white_check_mark"])
    state.consecutive_errors = 0
    state.error_alert_sent = False

    prev = state.status
    price = f" for {result.price}" if result.price else ""
    if result.status in (IN_STOCK, QUEUE):
        if prev != result.status:
            # New episode: alert now (and keep retrying until a send succeeds).
            state.last_alert = 0.0
            state.alerts_sent = 0
        # Reminders back off: remind_every, x2, x4, ... capped at 1 hour, so a long in-stock
        # period stays far below ntfy.sh's 250 messages/day limit.
        gap = min(cfg.remind_every * 2 ** min(max(0, state.alerts_sent - 1), 10), max(3600.0, cfg.remind_every))
        if state.last_alert <= 0 or (now - state.last_alert) >= gap:
            if result.status == IN_STOCK:
                title = "PS5 Pro IN STOCK!"
                msg = f"{result.product} is available on PlayStation Direct{price}. Go go go! ({_now_str()})"
                if result.reason:
                    msg += f"\nNote: {result.reason}"
            else:
                title = "PS5 Pro: queue detected - possible drop!"
                msg = f"The PS5 Pro page sends visitors to a waiting room. Get in line now! ({_now_str()})"
            if notifier.send(title, msg, priority=5, tags=["rotating_light", "video_game"],
                             click=cfg.url, action_label="Open shop"):
                state.last_alert = now
                state.alerts_sent += 1
    elif result.status == OUT_OF_STOCK:
        if prev in (IN_STOCK, QUEUE):
            notifier.send("PS5 Pro sold out again", f"{result.product} is out of stock again. ({_now_str()})",
                          priority=2, tags=["x"], click=cfg.url)
    elif result.status == UNKNOWN:
        if state.unknown_alerted != result.reason:
            if notifier.send("PS5 Pro: stock status changed",
                             f"Unrecognised stock status on the page ({result.reason}). "
                             "Have a look - it may be in stock.",
                             priority=4, tags=["eyes"], click=cfg.url, action_label="Open shop"):
                state.unknown_alerted = result.reason
    if result.status != UNKNOWN:
        state.unknown_alerted = ""

    if loop_mode and cfg.start_message and not state.started_sent:
        if notifier.send("PS5 Pro tracker started",
                         f"Watching {result.product}. Current status: {result.status}{price}. "
                         f"Checking every ~{int(cfg.interval)} s.",
                         priority=2, tags=["eyes"], click=cfg.url):
            state.started_sent = True

    state.status = result.status
    state.detail = result.reason


def run_once(cfg: Config, state: State, notifier: Notifier, loop_mode: bool) -> Optional[Result]:
    try:
        result = check(cfg.url, cfg.skus or None, timeout=cfg.timeout)
    except TrackerError as exc:
        process(None, str(exc), state, cfg, notifier, time.time(), loop_mode)
        return None
    except Exception as exc:  # noqa: BLE001 - one odd response must never stop the tracker
        process(None, f"unexpected error: {exc!r}", state, cfg, notifier, time.time(), loop_mode)
        return None
    log(result.summary())
    process(result, None, state, cfg, notifier, time.time(), loop_mode)
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Watch the PlayStation Direct PS5 Pro page and push an ntfy alert when it is in stock.",
        epilog="Every option can also be set with the environment variable shown in brackets.",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run one check and exit (for cron / GitHub Actions)")
    mode.add_argument("--check", action="store_true", help="one check, print details, never send notifications")
    mode.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    mode.add_argument("--new-topic", action="store_true", help="print a random ntfy topic name and exit")
    ap.add_argument("--url", default=os.environ.get("PRODUCT_URL") or DEFAULT_URL, help="product page [PRODUCT_URL]")
    ap.add_argument("--sku", action="append", default=None,
                    help="only watch these SKU id(s), comma-separated or repeated (default: the page's own SKU) [TARGET_SKU]")
    ap.add_argument("--topic", default=os.environ.get("NTFY_TOPIC", ""), help="ntfy topic [NTFY_TOPIC]")
    ap.add_argument("--server", default=os.environ.get("NTFY_SERVER") or DEFAULT_NTFY_SERVER,
                    help=f"ntfy server (default {DEFAULT_NTFY_SERVER}) [NTFY_SERVER]")
    ap.add_argument("--token", default=os.environ.get("NTFY_TOKEN", ""), help="ntfy access token, optional [NTFY_TOKEN]")
    ap.add_argument("--user", default=os.environ.get("NTFY_USER", ""), help="ntfy username, optional [NTFY_USER]")
    ap.add_argument("--password", default=os.environ.get("NTFY_PASSWORD", ""), help="ntfy password [NTFY_PASSWORD]")
    ap.add_argument("--interval", type=float, default=_env_float("CHECK_INTERVAL", 60),
                    help="seconds between checks in loop mode, min 20 (default 60) [CHECK_INTERVAL]")
    ap.add_argument("--remind-every", type=float, default=_env_float("REMIND_EVERY", 300),
                    help="repeat the in-stock alert this often (s) while it stays in stock (default 300) [REMIND_EVERY]")
    ap.add_argument("--error-alert-after", type=int, default=int(_env_float("ERROR_ALERT_AFTER", 10)),
                    help="send a 'tracker broken' alert after this many failed checks in a row (default 10) [ERROR_ALERT_AFTER]")
    ap.add_argument("--state-file", default=os.environ.get("STATE_FILE", ""),
                    help="JSON file to remember state between --once runs [STATE_FILE]")
    ap.add_argument("--timeout", type=float, default=_env_float("HTTP_TIMEOUT", 30), help="HTTP timeout in seconds [HTTP_TIMEOUT]")
    ap.add_argument("--no-start-message", action="store_true",
                    default=os.environ.get("NO_START_MESSAGE", "").lower() in ("1", "true", "yes"),
                    help="loop mode: do not send the 'tracker started' notification [NO_START_MESSAGE]")
    ap.add_argument("--dry-run", action="store_true", help="log notifications instead of sending them")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.new_topic:
        print("ps5pro-" + secrets.token_hex(8))
        return 0

    raw_skus = args.sku if args.sku is not None else [os.environ.get("TARGET_SKU", "")]
    skus = [s.strip() for value in raw_skus for s in value.split(",") if s.strip()]
    cfg = Config(
        url=args.url.split("#")[0],
        skus=skus,
        interval=max(20.0, args.interval),
        remind_every=max(60.0, args.remind_every),
        error_alert_after=max(1, args.error_alert_after),
        timeout=max(5.0, args.timeout),
        start_message=not args.no_start_message,
    )

    if urllib.parse.urlsplit(cfg.url).scheme not in ("http", "https"):
        print(f"error: product URL must start with https:// (got {cfg.url!r})", file=sys.stderr)
        return 2

    if args.check:
        try:
            result = check(cfg.url, cfg.skus or None, timeout=cfg.timeout)
        except Exception as exc:  # noqa: BLE001
            log(f"ERROR: {exc}")
            return 1
        print(f"status : {result.status}")
        print(f"product: {result.product}")
        print(f"price  : {result.price or '-'}")
        print(f"skus   : {', '.join(result.skus) or '-'}")
        for s in result.signals:
            print(f"signal : {s.describe()} -> {s.verdict}")
        if result.reason:
            print(f"note   : {result.reason}")
        return 0

    topic = args.topic.strip()
    if topic.startswith(("http://", "https://")):  # allow pasting the full topic URL
        parts = urllib.parse.urlsplit(topic)
        args.server = f"{parts.scheme}://{parts.netloc}"
        topic = parts.path.strip("/")
    if not topic:
        print("error: set an ntfy topic with --topic or NTFY_TOPIC (tip: python3 tracker.py --new-topic)", file=sys.stderr)
        return 2
    if not NTFY_TOPIC_RE.match(topic):
        print("error: ntfy topic may only contain letters, digits, '-' and '_' (max 64 chars)", file=sys.stderr)
        return 2
    server = args.server.strip()
    if "://" not in server:
        server = "https://" + server
    server_parts = urllib.parse.urlsplit(server)
    if server_parts.scheme not in ("http", "https") or not server_parts.netloc:
        print(f"error: invalid ntfy server {args.server!r} (expected e.g. https://ntfy.sh)", file=sys.stderr)
        return 2
    token, user, password = args.token.strip(), args.user.strip(), args.password.strip()
    if len(topic) < 12 and not (token or user):
        log("warning: short ntfy topics are guessable - anyone who knows the name can read or send to it")

    notifier = Notifier(server=server, topic=topic, token=token, user=user,
                        password=password, dry_run=args.dry_run)

    if args.test_notify:
        ok = notifier.send("PS5 tracker test", "If you can read this on your phone, ntfy works.",
                           priority=3, tags=["tada"], click=cfg.url, action_label="Open shop")
        log("test notification sent" if ok else "test notification FAILED")
        return 0 if ok else 1

    state_file = args.state_file
    if args.once and not state_file:
        # Without remembered state, --once could never send reminders-with-backoff,
        # "sold out again" or "checks are failing" alerts.
        state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
    state = State.load(state_file)

    if args.once:
        run_once(cfg, state, notifier, loop_mode=False)
        state.save(state_file)
        return 0

    # Loop mode
    stop = {"flag": False}

    def _stop(signum, _frame):  # noqa: ANN001
        stop["flag"] = True
        log(f"received signal {signum}, stopping")

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    log(f"watching {cfg.url} every ~{int(cfg.interval)} s -> ntfy {server.rstrip('/')}/{topic}")
    state.started_sent = False  # announce every (re)start, even when state was loaded from a file
    while not stop["flag"]:
        try:
            run_once(cfg, state, notifier, loop_mode=True)
        except Exception as exc:  # noqa: BLE001 - last line of defence: keep watching
            log(f"internal error (continuing): {exc!r}")
        state.save(state_file)
        # Back off on repeated errors (max 10 min), jitter +-10% so requests are not perfectly periodic.
        backoff = min(2 ** max(0, state.consecutive_errors - 2), 10 * 60 / cfg.interval) if state.consecutive_errors else 1
        delay = cfg.interval * max(1.0, backoff) * random.uniform(0.9, 1.1)
        end = time.time() + delay
        while not stop["flag"] and time.time() < end:
            time.sleep(min(1.0, end - time.time()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
