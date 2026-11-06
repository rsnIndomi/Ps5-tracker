"""Unit tests for tracker.py. Run: python3 -m unittest discover -s tests -v

The fixtures are small synthetic pages that mirror the structure of the real
playstation.com buy-now pages (meta tags, JSON-LD, Next.js RSC payload).
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tracker  # noqa: E402

PRO = "PSD001-PSDE01942_39-PS5PROCHASASIRSE-E000K3A7V"
PRO_PRODUCT = "PSD001-PSDE01942_39-PS5PROCHASASIRSE"
SLIM = "PSD001-PSDE01111_11-PS5SLIMDIGITALE-E000AAAAA"


def sku_obj(sku, status, override="notSpecified", price="€899,99"):
    return {
        "id": sku,
        "displayPrice": price,
        "price": 89999,
        "eligibilities": [],
        "type": "PHYSICAL",
        "entitlements": [],
        "lowestRecentPrice": {"numberOfDays": 30, "price": 89999},
        "stockLevelStatus": status,
        "physicalAttributes": {"replaceable": True, "highDemand": False, "inventoryOverride": override},
    }


def make_page(ld_availability="OutOfStock", skus=(), meta_skus=PRO, extra_rsc="", rsc_mode="ok",
              ld_sku=PRO, extra_html=""):
    """Build a minimal buy-now page.

    skus: iterable of SKU objects placed in RSC "skus" arrays (e.g. main product + carousel).
    rsc_mode: "ok" (valid push call), "broken" (undecodable push, forces regex fallback), "none".
    """
    parts = ["<!DOCTYPE html><html><head>"]
    if meta_skus is not None:
        parts.append(f'<meta name="productId" content="{PRO_PRODUCT}"/>')
        parts.append(f'<meta name="skus" content="{meta_skus}"/>')
    parts.append("<title>PS5 Pro</title></head><body>")
    if ld_availability is not None:
        ld = {
            "@context": "https://schema.org",
            "@type": "Product",
            "productID": PRO_PRODUCT,
            "sku": ld_sku,
            "name": "PlayStation®5 Pro Console - 2 TB\t",
            "offers": {
                "@type": "Offer",
                "price": "899.99",
                "priceCurrency": "EUR",
                "availability": f"https://schema.org/{ld_availability}",
            },
        }
        parts.append(f'<script id="pdp-product-jsonld" type="application/ld+json">{json.dumps(ld)}</script>')
    rsc_text = "".join(
        '5:{"product":{"name":"x","skus":' + json.dumps([o]) + "}}\n" for o in skus
    ) + extra_rsc
    # A CTA saying ADD_TO_CART must never count as in stock (real VR2 page does this while OOS).
    rsc_text += '9:{"cta":{"type":"ADD_TO_CART"}}\n'
    if rsc_mode == "ok":
        parts.append("<script>self.__next_f.push(" + json.dumps([1, rsc_text]) + ")</script>")
    elif rsc_mode == "broken":
        encoded = json.dumps([1, rsc_text])
        parts.append("<script>self.__next_f.push(" + encoded[:-2] + ")</script>")  # truncated -> invalid JSON
    parts.append(extra_html)
    parts.append("</body></html>")
    return "".join(parts)


class AnalyzeTests(unittest.TestCase):
    def test_out_of_stock(self):
        r = tracker.analyze(make_page("OutOfStock", [sku_obj(PRO, "OUT_OF_STOCK", "forceOutOfStock")]))
        self.assertEqual(r.status, tracker.OUT_OF_STOCK)
        self.assertEqual(r.product, "PlayStation®5 Pro Console - 2 TB")
        self.assertEqual(r.price, "€899,99")
        self.assertEqual({s.source for s in r.signals}, {"json-ld", "sku"})
        self.assertIn("inventoryOverride=forceOutOfStock", r.summary())

    def test_in_stock(self):
        r = tracker.analyze(make_page("InStock", [sku_obj(PRO, "IN_STOCK")]))
        self.assertEqual(r.status, tracker.IN_STOCK)
        self.assertEqual(r.reason, "")

    def test_preorder_counts_as_available(self):
        r = tracker.analyze(make_page("PreOrder", [sku_obj(PRO, "IN_STOCK")]))
        self.assertEqual(r.status, tracker.IN_STOCK)

    def test_other_product_in_stock_is_ignored(self):
        """An 'other consoles' carousel with an in-stock PS5 Slim must not trigger an alert."""
        page = make_page("OutOfStock", [sku_obj(PRO, "OUT_OF_STOCK"), sku_obj(SLIM, "IN_STOCK", price="€499,99")])
        r = tracker.analyze(page)
        self.assertEqual(r.status, tracker.OUT_OF_STOCK)
        self.assertEqual(r.price, "€899,99")
        self.assertTrue(all(s.sku in ("", PRO) for s in r.signals))

    def test_conflicting_signals_err_on_in_stock(self):
        r = tracker.analyze(make_page("OutOfStock", [sku_obj(PRO, "IN_STOCK")]))
        self.assertEqual(r.status, tracker.IN_STOCK)
        self.assertIn("disagree", r.reason)

    def test_only_json_ld(self):
        r = tracker.analyze(make_page("InStock", [], rsc_mode="none"))
        self.assertEqual(r.status, tracker.IN_STOCK)

    def test_only_sku_status(self):
        r = tracker.analyze(make_page(None, [sku_obj(PRO, "OUT_OF_STOCK")]))
        self.assertEqual(r.status, tracker.OUT_OF_STOCK)

    def test_unknown_value(self):
        r = tracker.analyze(make_page("OutOfStock", [sku_obj(PRO, "COMING_SOON")]))
        self.assertEqual(r.status, tracker.UNKNOWN)
        self.assertIn("COMING_SOON", r.reason)

    def test_regex_fallback_when_rsc_undecodable(self):
        page = make_page(None, [sku_obj(SLIM, "IN_STOCK"), sku_obj(PRO, "OUT_OF_STOCK")], rsc_mode="broken")
        r = tracker.analyze(page)
        self.assertEqual(r.status, tracker.OUT_OF_STOCK)
        self.assertEqual([s.source for s in r.signals], ["sku-fallback"])

    def test_regex_fallback_in_stock(self):
        r = tracker.analyze(make_page(None, [sku_obj(PRO, "IN_STOCK")], rsc_mode="broken"))
        self.assertEqual(r.status, tracker.IN_STOCK)

    def test_no_stock_data_raises(self):
        with self.assertRaises(tracker.TrackerError):
            tracker.analyze("<html><body>Access Denied</body></html>")

    def test_queue_page(self):
        page = '<html><head><script src="https://static.queue-it.net/script/queueclient.min.js"></script></head></html>'
        self.assertEqual(tracker.analyze(page).status, tracker.QUEUE)

    def test_queue_it_mention_on_normal_page_is_ignored(self):
        page = make_page("OutOfStock", [sku_obj(PRO, "OUT_OF_STOCK")], extra_html="<!-- queue-it -->")
        self.assertEqual(tracker.analyze(page).status, tracker.OUT_OF_STOCK)

    def test_redirect_to_queue_host(self):
        r = tracker.analyze("<html></html>", final_url="https://playstation.queue-it.net/?c=sonyeu&e=ps5pro")
        self.assertEqual(r.status, tracker.QUEUE)

    def test_explicit_sku_override(self):
        page = make_page("OutOfStock", [sku_obj(PRO, "OUT_OF_STOCK"), sku_obj(SLIM, "IN_STOCK")])
        r = tracker.analyze(page, target_skus=[SLIM])
        self.assertEqual(r.status, tracker.IN_STOCK)
        self.assertTrue(all(s.source == "sku" and s.sku == SLIM for s in r.signals))

    def test_explicit_sku_not_on_page(self):
        page = make_page("OutOfStock", [sku_obj(PRO, "OUT_OF_STOCK")], ld_sku="something-else")
        with self.assertRaises(tracker.TrackerError) as ctx:
            tracker.analyze(page, target_skus=["NOPE-123"])
        self.assertIn("NOPE-123", str(ctx.exception))

    def test_meta_skus_missing_uses_single_ld_product(self):
        r = tracker.analyze(make_page("InStock", [sku_obj(PRO, "OUT_OF_STOCK")], meta_skus=None))
        # Without meta skus we cannot attribute RSC SKUs, so only JSON-LD is used.
        self.assertEqual(r.status, tracker.IN_STOCK)
        self.assertEqual([s.source for s in r.signals], ["json-ld"])


class FakeNotifier:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    def send(self, title, message, priority=3, tags=None, click="", action_label=""):
        self.sent.append({"title": title, "message": message, "priority": priority})
        return self.ok


def cfg(**kw):
    base = dict(url=tracker.DEFAULT_URL, skus=[], interval=60, remind_every=300, error_alert_after=3,
                timeout=30, start_message=True)
    base.update(kw)
    return tracker.Config(**base)


def res(status, reason=""):
    return tracker.Result(status=status, product="PS5 Pro", price="€899,99", reason=reason)


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self._out = io.StringIO()
        self._redirect = redirect_stdout(self._out)
        self._redirect.__enter__()

    def tearDown(self):
        self._redirect.__exit__(None, None, None)

    def titles(self, n):
        return [m["title"] for m in n.sent]

    def test_in_stock_alert_and_reminders(self):
        n, s, c = FakeNotifier(), tracker.State(), cfg(start_message=False)
        tracker.process(res(tracker.OUT_OF_STOCK), None, s, c, n, 1000, loop_mode=True)
        self.assertEqual(n.sent, [])
        tracker.process(res(tracker.IN_STOCK), None, s, c, n, 1060, loop_mode=True)
        self.assertEqual(self.titles(n), ["PS5 Pro IN STOCK!"])
        self.assertEqual(n.sent[0]["priority"], 5)
        tracker.process(res(tracker.IN_STOCK), None, s, c, n, 1120, loop_mode=True)  # within remind window
        self.assertEqual(len(n.sent), 1)
        tracker.process(res(tracker.IN_STOCK), None, s, c, n, 1360, loop_mode=True)  # 300 s later
        self.assertEqual(len(n.sent), 2)
        tracker.process(res(tracker.OUT_OF_STOCK), None, s, c, n, 1420, loop_mode=True)
        self.assertEqual(self.titles(n)[-1], "PS5 Pro sold out again")

    def test_first_check_in_stock_alerts_immediately(self):
        n, s = FakeNotifier(), tracker.State()
        tracker.process(res(tracker.IN_STOCK), None, s, cfg(start_message=False), n, 50, loop_mode=False)
        self.assertEqual(self.titles(n), ["PS5 Pro IN STOCK!"])

    def test_new_episode_alerts_even_shortly_after_previous(self):
        n, s, c = FakeNotifier(), tracker.State(), cfg(start_message=False)
        tracker.process(res(tracker.IN_STOCK), None, s, c, n, 1000, loop_mode=True)
        tracker.process(res(tracker.OUT_OF_STOCK), None, s, c, n, 1060, loop_mode=True)
        tracker.process(res(tracker.IN_STOCK), None, s, c, n, 1120, loop_mode=True)
        self.assertEqual(self.titles(n), ["PS5 Pro IN STOCK!", "PS5 Pro sold out again", "PS5 Pro IN STOCK!"])

    def test_failed_send_is_retried_next_check(self):
        n, s, c = FakeNotifier(ok=False), tracker.State(), cfg(start_message=False)
        tracker.process(res(tracker.OUT_OF_STOCK), None, s, c, n, 1000, loop_mode=True)
        tracker.process(res(tracker.IN_STOCK), None, s, c, n, 1060, loop_mode=True)
        n.ok = True
        tracker.process(res(tracker.IN_STOCK), None, s, c, n, 1120, loop_mode=True)
        self.assertEqual(self.titles(n), ["PS5 Pro IN STOCK!", "PS5 Pro IN STOCK!"])

    def test_queue_alert(self):
        n, s = FakeNotifier(), tracker.State()
        tracker.process(res(tracker.QUEUE), None, s, cfg(start_message=False), n, 10, loop_mode=False)
        self.assertEqual(n.sent[0]["priority"], 5)
        self.assertIn("queue", n.sent[0]["title"])

    def test_unknown_alerts_once_per_value(self):
        n, s, c = FakeNotifier(), tracker.State(), cfg(start_message=False)
        tracker.process(res(tracker.UNKNOWN, "unrecognised stock value(s): X"), None, s, c, n, 1, loop_mode=True)
        tracker.process(res(tracker.UNKNOWN, "unrecognised stock value(s): X"), None, s, c, n, 2, loop_mode=True)
        tracker.process(res(tracker.UNKNOWN, "unrecognised stock value(s): Y"), None, s, c, n, 3, loop_mode=True)
        self.assertEqual(len(n.sent), 2)

    def test_error_alert_once_then_recovery(self):
        n, s, c = FakeNotifier(), tracker.State(), cfg(start_message=False, error_alert_after=3)
        for t in range(5):
            tracker.process(None, "HTTP 403", s, c, n, t, loop_mode=True)
        self.assertEqual(self.titles(n), ["PS5 tracker: checks are failing"])
        tracker.process(res(tracker.OUT_OF_STOCK), None, s, c, n, 10, loop_mode=True)
        self.assertEqual(self.titles(n)[-1], "PS5 tracker: working again")
        self.assertEqual(s.consecutive_errors, 0)
        self.assertFalse(s.error_alert_sent)

    def test_start_message_only_in_loop_mode_and_once(self):
        n, s = FakeNotifier(), tracker.State()
        tracker.process(res(tracker.OUT_OF_STOCK), None, s, cfg(), n, 1, loop_mode=False)
        self.assertEqual(n.sent, [])
        tracker.process(res(tracker.OUT_OF_STOCK), None, s, cfg(), n, 2, loop_mode=True)
        tracker.process(res(tracker.OUT_OF_STOCK), None, s, cfg(), n, 3, loop_mode=True)
        self.assertEqual(self.titles(n), ["PS5 Pro tracker started"])

    def test_state_roundtrip_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            st = tracker.State(status=tracker.IN_STOCK, last_alert=123.0, consecutive_errors=2)
            st.save(path)
            self.assertEqual(tracker.State.load(path), st)
            with open(path, "w") as f:
                f.write("{not json")
            self.assertEqual(tracker.State.load(path), tracker.State())
            self.assertEqual(tracker.State.load(os.path.join(d, "missing.json")), tracker.State())


class CliTests(unittest.TestCase):
    def run_main(self, argv, env=None):
        old = dict(os.environ)
        os.environ.pop("NTFY_TOPIC", None)
        os.environ.update(env or {})
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = tracker.main(argv)
        finally:
            os.environ.clear()
            os.environ.update(old)
        return code, out.getvalue(), err.getvalue()

    def test_new_topic(self):
        code, out, _ = self.run_main(["--new-topic"])
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(), r"^ps5pro-[0-9a-f]{16}$")

    def test_missing_topic(self):
        code, _, err = self.run_main(["--once"])
        self.assertEqual(code, 2)
        self.assertIn("NTFY_TOPIC", err)

    def test_invalid_topic(self):
        code, _, err = self.run_main(["--once", "--topic", "bad topic!"])
        self.assertEqual(code, 2)

    def test_test_notify_dry_run_accepts_topic_url(self):
        code, out, _ = self.run_main(["--test-notify", "--dry-run", "--topic", "https://ntfy.example.com/my-secret-topic-123"])
        self.assertEqual(code, 0)
        self.assertIn("would notify", out)


if __name__ == "__main__":
    unittest.main()
