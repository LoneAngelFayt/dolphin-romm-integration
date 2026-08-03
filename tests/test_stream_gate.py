"""The nginx auth_request gate that stands in front of the desktop stream.

Without it the selkies vhosts serve an interactive desktop, with the ROM
library mounted, to anyone who learns the address: RomM's own auth never sits
on that socket. The gate is a second credential independent of BROKER_SECRET,
because nginx cannot forward the shared secret on a subrequest and the browser
has to be able to carry this one.

Also covers _header_token, the single filter standing between a filename and a
response header, and the byte comparison behind the shared secret.
"""

import time
import unittest
import unittest.mock as mock

from support import broker, reset_session


class StreamTokenTests(unittest.TestCase):
    def setUp(self):
        broker._clear_stream_token()

    def tearDown(self):
        reset_session()

    def test_issue_returns_nonempty_and_stores(self):
        tok = broker._issue_stream_token()
        self.assertTrue(tok)
        self.assertEqual(broker._session["stream_token"], tok)

    def test_check_accepts_issued_token(self):
        tok = broker._issue_stream_token()
        self.assertIsNone(broker._check_stream_token(tok))

    def test_check_rejects_wrong_token(self):
        broker._issue_stream_token()
        self.assertIn("does not match", broker._check_stream_token("nope"))

    def test_check_rejects_when_no_token_set(self):
        self.assertIn("no stream session", broker._check_stream_token("anything"))
        self.assertIn("no stream token", broker._check_stream_token(""))

    def test_clear_invalidates(self):
        tok = broker._issue_stream_token()
        broker._clear_stream_token()
        self.assertIsNone(broker._session["stream_token"])
        self.assertIsNotNone(broker._check_stream_token(tok))


class StreamTokenLifetimeTests(unittest.TestCase):
    """The TTL closes an abandoned session; the grace window keeps an open tab
    alive across a relaunch. Both are wall-clock behaviours, so these tests
    move the deadlines rather than sleeping."""

    def setUp(self):
        broker._clear_stream_token()

    def tearDown(self):
        reset_session()

    def test_issued_token_carries_a_ttl(self):
        broker._issue_stream_token()
        remaining = broker._session["stream_expires"] - time.monotonic()
        self.assertGreater(remaining, broker.STREAM_TOKEN_TTL - 5)

    def test_expired_token_is_rejected(self):
        tok = broker._issue_stream_token()
        with broker._session_lock:
            broker._session["stream_expires"] = time.monotonic() - 1
        self.assertIn("expired", broker._check_stream_token(tok))

    def test_use_slides_the_expiry_forward(self):
        tok = broker._issue_stream_token()
        with broker._session_lock:
            broker._session["stream_expires"] = time.monotonic() + 5
        self.assertIsNone(broker._check_stream_token(tok))
        remaining = broker._session["stream_expires"] - time.monotonic()
        self.assertGreater(remaining, broker.STREAM_TOKEN_TTL - 5)

    def test_reissue_keeps_the_old_token_alive_briefly(self):
        first = broker._issue_stream_token()
        second = broker._issue_stream_token()
        self.assertNotEqual(first, second)
        # Both work while the grace window is open: an already-open tab is
        # still replaying the old cookie when RomM navigates to the new URL.
        self.assertIsNone(broker._check_stream_token(first))
        self.assertIsNone(broker._check_stream_token(second))

    def test_superseded_token_dies_when_the_grace_window_closes(self):
        first = broker._issue_stream_token()
        second = broker._issue_stream_token()
        with broker._session_lock:
            broker._session["stream_prev_expires"] = time.monotonic() - 1
        self.assertIn("superseded", broker._check_stream_token(first))
        self.assertIsNone(broker._check_stream_token(second))

    def test_grace_does_not_survive_an_explicit_clear(self):
        first = broker._issue_stream_token()
        broker._issue_stream_token()
        broker._clear_stream_token()
        self.assertIsNotNone(broker._check_stream_token(first))

    def test_live_token_reported_only_while_valid(self):
        tok = broker._issue_stream_token()
        self.assertEqual(broker._live_stream_token(), tok)
        with broker._session_lock:
            broker._session["stream_expires"] = time.monotonic() - 1
        self.assertIsNone(broker._live_stream_token())


class StreamProxyHelperTests(unittest.TestCase):
    def test_extract_token_from_query(self):
        self.assertEqual(
            broker._extract_stream_token("stream_token=abc&x=1", None), "abc"
        )

    def test_extract_token_from_cookie(self):
        self.assertEqual(
            broker._extract_stream_token("", "stream_sid=abc; other=1"), "abc"
        )

    def test_query_beats_cookie(self):
        self.assertEqual(
            broker._extract_stream_token("stream_token=q", "stream_sid=c"), "q"
        )

    def test_extract_none_when_absent(self):
        self.assertIsNone(broker._extract_stream_token("y=2", "foo=bar"))
        self.assertIsNone(broker._extract_stream_token("", None))

    def test_cookie_value_has_required_attributes(self):
        # The iframe is cross-site to RomM, so the cookie is third-party:
        # without SameSite=None, Secure and Partitioned the browser drops it
        # and every asset after the first arrives with no credential at all.
        self.assertEqual(
            broker._stream_cookie_value("abc"),
            "stream_sid=abc; HttpOnly; Secure; SameSite=None; Partitioned; Path=/",
        )


class VerifyStreamDecisionTests(unittest.TestCase):
    """The nginx auth_request decision: 200 admits, 403 rejects, and a query
    bootstrap hands back the stream_sid Set-Cookie."""

    def setUp(self):
        broker._clear_stream_token()
        with broker._session_lock:
            broker._session["stream_token"] = "good"
            broker._session["stream_expires"] = time.monotonic() + 3600

    def tearDown(self):
        reset_session()

    def test_no_token_is_403(self):
        status, cookie, reason = broker._verify_stream_decision("/", None)
        self.assertEqual(status, 403)
        self.assertIsNone(cookie)
        self.assertIn("no stream token", reason)

    def test_valid_query_token_admits_and_sets_cookie(self):
        status, cookie, reason = broker._verify_stream_decision(
            "/?stream_token=good", None
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            cookie,
            "stream_sid=good; HttpOnly; Secure; SameSite=None; Partitioned; Path=/",
        )
        self.assertIsNone(reason)

    def test_valid_cookie_admits_without_set_cookie(self):
        status, cookie, _ = broker._verify_stream_decision(
            "/", "stream_sid=good; other=1"
        )
        self.assertEqual(status, 200)
        self.assertIsNone(cookie)

    def test_wrong_query_token_is_403(self):
        status, cookie, reason = broker._verify_stream_decision(
            "/?stream_token=bad", None
        )
        self.assertEqual(status, 403)
        self.assertIsNone(cookie)
        self.assertIn("does not match", reason)

    def test_wrong_cookie_is_403(self):
        status, _, _ = broker._verify_stream_decision("/", "stream_sid=bad")
        self.assertEqual(status, 403)

    def test_websocket_upgrade_is_gated_like_everything_else(self):
        # /websocket is the one that matters: it is the interactive channel,
        # and the base image proxies it from the same server block.
        status, _, _ = broker._verify_stream_decision("/websocket", None)
        self.assertEqual(status, 403)

    def test_stale_cookie_plus_fresh_query_token_recookies_the_browser(self):
        # The relaunch case: the tab still holds the superseded stream_sid but
        # the new iframe URL carries the current token. The query wins, and the
        # response must carry the new cookie or the tab drops out the moment
        # the grace window closes.
        with broker._session_lock:
            broker._session["stream_prev_token"] = "stale"
            broker._session["stream_prev_expires"] = time.monotonic() + 60
        status, cookie, _ = broker._verify_stream_decision(
            "/?stream_token=good", "stream_sid=stale"
        )
        self.assertEqual(status, 200)
        self.assertIn("stream_sid=good", cookie)

    def test_superseded_cookie_alone_still_admits_during_grace(self):
        with broker._session_lock:
            broker._session["stream_prev_token"] = "stale"
            broker._session["stream_prev_expires"] = time.monotonic() + 60
        status, _, reason = broker._verify_stream_decision("/", "stream_sid=stale")
        self.assertEqual(status, 200)
        self.assertIsNone(reason)


class RedactedLoggingTests(unittest.TestCase):
    """The gate admits on a query token, so an unredacted access line would put
    a live credential in stdout, which is the text an operator pastes into a
    bug report."""

    def test_the_stream_token_never_reaches_the_log(self):
        self.assertEqual(
            broker._redact_uri("/websocket?stream_token=SUPERSECRET&x=1"),
            "/websocket?stream_token=REDACTED&x=1",
        )

    def test_a_uri_with_no_token_is_untouched(self):
        self.assertEqual(broker._redact_uri("/files/?a=1"), "/files/?a=1")


class HeaderTokenTests(unittest.TestCase):
    """Header values go out unvalidated by http.server, so a CR or LF in a
    filename would split the response. _header_token is the one filter."""

    def test_crlf_is_stripped(self):
        out = broker._header_token("evil\r\nX-Injected: yes.s01", "state.s01")
        self.assertNotIn("\r", out)
        self.assertNotIn("\n", out)

    def test_non_ascii_is_stripped(self):
        self.assertEqual(broker._header_token("Pokémon.s01", "x"), "Pokmon.s01")

    def test_a_name_with_nothing_left_falls_back(self):
        self.assertEqual(broker._header_token("\r\n\t", "state.s01"), "state.s01")

    def test_an_ordinary_name_survives_intact(self):
        self.assertEqual(
            broker._header_token("Super Mario Sunshine.s01", "x"),
            "Super Mario Sunshine.s01",
        )


class SecretCheckTests(unittest.TestCase):
    """The shared secret is compared as bytes: hmac.compare_digest rejects a
    str carrying non-ASCII, so a UTF-8 BROKER_SECRET would raise inside every
    _check_secret and answer 500 to every request rather than 403."""

    class _Req:
        def __init__(self, header):
            self.headers = {} if header is None else {"X-Broker-Secret": header}

    def _check(self, secret, sent):
        with mock.patch.object(broker, "SECRET", secret), \
             mock.patch.object(broker, "_SECRET_BYTES", secret.encode("utf-8")):
            return broker.BrokerHandler._check_secret(self._Req(sent))

    def test_utf8_secret_accepts_the_bytes_that_arrive_on_the_wire(self):
        secret = "pässwort"
        # http.server hands header values back latin-1-decoded, so this is what
        # _check_secret actually sees when a client sends the UTF-8 secret.
        on_the_wire = secret.encode("utf-8").decode("latin-1")
        self.assertTrue(self._check(secret, on_the_wire))

    def test_utf8_secret_rejects_a_wrong_one_rather_than_raising(self):
        self.assertFalse(self._check("pässwort", "nope"))

    def test_ascii_secret_still_matches(self):
        self.assertTrue(self._check("plain-secret", "plain-secret"))

    def test_a_missing_header_is_rejected(self):
        self.assertFalse(self._check("plain-secret", None))

    def test_an_unset_secret_accepts_everything(self):
        # Documented debug-only posture: no secret means no gate on the broker
        # API. The stream gate is separate and stays closed regardless.
        with mock.patch.object(broker, "SECRET", ""):
            self.assertTrue(broker.BrokerHandler._check_secret(self._Req(None)))


if __name__ == "__main__":
    unittest.main()
