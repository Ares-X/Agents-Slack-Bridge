"""Offline standard-library tests. Every Slack-looking ID is synthetic."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from policy import Ledger, Policy, evaluate, safe_echo


CHANNEL = "CSYNTHETICCHAN01"
PEER = "USYNTHETICPEER01"
TARGET = "USYNTHETICTARGET"
SELF_BOT = "BSYNTHETICSELF01"
MARKER = "BRIDGE_TEST_SYNTHETIC_001"
MENTION = f"<@{TARGET}>"


def policy(**changes):
    return replace(Policy(
        channels=frozenset({CHANNEL}),
        peer_user_ids=frozenset({PEER}),
        target_user_id=TARGET,
        self_bot_id=SELF_BOT,
        test_markers=frozenset({MARKER}),
    ), **changes)


def envelope(text=None, **changes):
    event = {"type": "message", "subtype": "bot_message", "channel": CHANNEL,
             "user": PEER, "bot_id": "BSYNTHETICPEER01", "ts": "1000000000.000001",
             "text": f"{MENTION} Can you review this approach?" if text is None else text}
    event.update(changes)
    return {"type": "event_callback", "event_id": "EvSYNTHETIC0001", "event": event}


class PolicyTests(unittest.TestCase):
    def decide(self, value=None, config=None, kind="question"):
        return evaluate(envelope() if value is None else value,
                        policy() if config is None else config, classification=kind)

    def test_explicit_peer_question(self):
        decision = self.decide()
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.candidate.destination, {"channel": CHANNEL})

    def test_message_without_subtype_is_eligible(self):
        value = envelope()
        del value["event"]["subtype"]
        self.assertTrue(self.decide(value).allowed)

    def test_question_and_collaboration_are_caller_classifications(self):
        # Punctuation cannot decide intent. The trusted caller must classify it.
        value = envelope(f"{MENTION} Please work through the approach with me")
        self.assertTrue(self.decide(value, kind="collaboration").allowed)
        for kind in ("progress", "ack", "other", "quote", "copy", "echo", "", "question?", None, []):
            with self.subTest(kind=kind):
                self.assertEqual(self.decide(value, kind=kind).reason, "intent_not_eligible")

    def test_message_cannot_supply_classification(self):
        value = envelope(classification="question")
        self.assertFalse(self.decide(value, kind="progress").allowed)

    def test_only_explicit_channels_and_peers(self):
        self.assertEqual(self.decide(envelope(channel="CSYNTHETICOTHER1")).reason, "channel_not_allowed")
        self.assertEqual(self.decide(envelope(user="USYNTHETICOTHER1")).reason, "peer_not_allowed")

    def test_self_user_rejected_even_if_peer_allowlisted(self):
        config = policy(peer_user_ids=frozenset({PEER, TARGET}))
        self.assertEqual(self.decide(envelope(user=TARGET), config).reason, "self_message")

    def test_optional_self_user_and_bot_are_excluded(self):
        config = policy(peer_user_ids=frozenset({PEER, "USYNTHETICALIAS1"}), self_user_id="USYNTHETICALIAS1")
        self.assertEqual(self.decide(envelope(user="USYNTHETICALIAS1"), config).reason, "self_message")
        self.assertEqual(self.decide(envelope(bot_id=SELF_BOT)).reason, "self_message")

    def test_bot_id_only_fails_closed(self):
        value = envelope()
        del value["event"]["user"]
        self.assertEqual(self.decide(value).reason, "unresolved_user")
        # Identity resolution must happen externally from an authoritative source.
        resolved = deepcopy(value)
        resolved["event"]["user"] = PEER
        self.assertTrue(self.decide(resolved).allowed)

    def test_bot_profile_cannot_claim_identity(self):
        value = envelope(user=None, bot_profile={"user_id": PEER})
        self.assertEqual(self.decide(value).reason, "unresolved_user")

    def test_edits_deletes_status_and_other_events_are_rejected(self):
        for subtype in ("message_changed", "message_deleted", "message_replied", "channel_join",
                        "assistant_app_thread", "tool_status", "", {}, []):
            with self.subTest(subtype=subtype):
                self.assertEqual(self.decide(envelope(subtype=subtype)).reason, "not_new_message")
        for key in ("edited", "deleted_ts", "previous_message", "message"):
            self.assertEqual(self.decide(envelope(**{key: {}})).reason, "not_new_message")
        self.assertFalse(self.decide(envelope(type="app_mention")).allowed)
        self.assertFalse(self.decide(envelope(hidden=True)).allowed)

    def test_thread_replies_require_configuration_and_destination_is_top_level(self):
        value = envelope(thread_ts="1000000000.000000")
        self.assertEqual(self.decide(value).reason, "thread_reply_disabled")
        decision = self.decide(value, policy(allow_thread_replies=True))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.candidate.input_thread_ts, "1000000000.000000")
        self.assertEqual(decision.candidate.destination, {"channel": CHANNEL})
        self.assertNotIn("thread_ts", decision.candidate.destination)

    def test_root_with_thread_metadata_is_not_a_reply(self):
        self.assertTrue(self.decide(envelope(thread_ts="1000000000.000001")).allowed)

    def test_quoted_and_coded_mentions_do_not_trigger(self):
        texts = [f"`{MENTION}` can you review?", f"```python\n{MENTION}\n``` review?",
                 f"``{MENTION}`` review?", f"> {MENTION} question\nplease review",
                 f"   > {MENTION} question", f">>> quoted\n{MENTION} review?",
                 f'He said "{MENTION} review?"', f"He said '{MENTION} review?'",
                 f"He said “{MENTION} review?”", f"He said ‘{MENTION} review?’",
                 f"```unclosed\n{MENTION}", f'"unclosed {MENTION}']
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(self.decide(envelope(text)).reason, "missing_direct_mention")

    def test_single_quotes_with_contractions_mask_mentions(self):
        for opening, apostrophe, closing in (("'", "'", "'"), ("‘", "’", "’")):
            quoted = f"{opening}Don{apostrophe}t {MENTION} reply{closing}"
            with self.subTest(quote=opening):
                self.assertEqual(self.decide(envelope(f"He said {quoted}")).reason,
                                 "missing_direct_mention")
                self.assertEqual(self.decide(envelope(quoted[:-1])).reason,
                                 "missing_direct_mention")
                self.assertTrue(self.decide(envelope(
                    f"He said {quoted}. {MENTION} Can you review the wording?")).allowed)

    def test_quoted_contractions_do_not_expose_test_markers(self):
        for opening, apostrophe, closing in (("'", "'", "'"), ("‘", "’", "’")):
            text = f"{MENTION} Review {opening}Don{apostrophe}t {MARKER} reuse{closing}?"
            with self.subTest(quote=opening):
                self.assertEqual(self.decide(envelope(text)).candidate.test_markers, ())
                self.assertEqual(self.decide(envelope(text), kind="test").reason,
                                 "missing_test_marker")
                self.assertEqual(self.decide(envelope(f"{text} {MARKER}"), kind="test")
                                 .candidate.test_markers, (MARKER,))

    def test_corner_quoted_mentions_do_not_trigger(self):
        for text in (f"「{MENTION} review?」", f"『{MENTION} review?』",
                     f"「unclosed {MENTION}", f"『unclosed {MENTION}"):
            with self.subTest(text=text):
                self.assertEqual(self.decide(envelope(text)).reason, "missing_direct_mention")
        self.assertTrue(self.decide(envelope(f"「old quote」 {MENTION} review?")).allowed)

    def test_rich_content_fallback_fails_closed(self):
        quoted_block = {"type": "rich_text", "elements": [
            {"type": "rich_text_quote", "elements": [{"type": "user", "user_id": TARGET}]}]}
        for field in ("blocks", "attachments"):
            for value in ([quoted_block], [{"text": f"> {MENTION}"}], {}, "unsupported", None, False):
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.decide(envelope(**{field: value})).reason,
                                     "unsupported_rich_content")
            self.assertTrue(self.decide(envelope(**{field: []})).allowed)

    def test_encoded_escaped_display_names_and_wrong_users_do_not_trigger(self):
        texts = [f"&lt;@{TARGET}&gt; question?", f"&#60;@{TARGET}&#62; question?",
                 f"\\{MENTION} question?", f"\\\\{MENTION} question?",
                 f"<@{TARGET}|demo> question?", "@demo question?", "<@USYNTHETICOTHER1> question?"]
        for text in texts:
            with self.subTest(text=text):
                self.assertFalse(self.decide(envelope(text)).allowed)

    def test_actual_mention_after_irrelevant_or_quoted_mentions_is_eligible(self):
        for text in (f"<@USYNTHETICOTHER1> and {MENTION} can you review?",
                     f"`<@USYNTHETICOTHER1>` {MENTION} can you review?",
                     f"> {MENTION} old quote\n{MENTION} can you review?",
                     f'"{MENTION} old quote" {MENTION} can you review?',
                     f"Don't stop, {MENTION} can you review?"):
            with self.subTest(text=text):
                self.assertTrue(self.decide(envelope(text)).allowed)

    def test_mentions_inside_slack_links_and_complex_markup_are_rejected(self):
        for text in (f"<https://example.invalid/|{MENTION}> review?",
                     f"<https://example.invalid/{MENTION}|label> review?",
                     f"<<{MENTION}>> review?", f"<unclosed {MENTION} review?"):
            with self.subTest(text=text):
                self.assertEqual(self.decide(envelope(text)).reason, "missing_direct_mention")
        text = f"<https://example.invalid/|{MENTION}> {MENTION} review?"
        self.assertTrue(self.decide(envelope(text)).allowed)

    def test_marker_inside_link_label_is_not_a_test_trigger(self):
        text = f"{MENTION} <https://example.invalid/|{MARKER}>"
        self.assertEqual(self.decide(envelope(text), kind="test").reason, "missing_test_marker")

    def test_test_kind_requires_exact_configured_marker(self):
        value = envelope(f"{MENTION} ({MARKER})")
        self.assertEqual(self.decide(value, kind="test").candidate.test_markers, (MARKER,))
        for text in (f"{MENTION} run test", f"{MENTION} X{MARKER}", f"{MENTION} {MARKER}X",
                     f"{MENTION} {MARKER}-extra", f"{MENTION} _{MARKER}",
                     f"{MENTION} é{MARKER}", f"{MENTION} `{MARKER}`",
                     f'{MENTION} "{MARKER}"', f"{MENTION}\n> {MARKER}"):
            with self.subTest(text=text):
                self.assertEqual(self.decide(envelope(text), kind="test").reason, "missing_test_marker")
        self.assertFalse(self.decide(value, policy(test_markers=frozenset()), kind="test").allowed)

    def test_marker_does_not_bypass_identity_intent_or_mention(self):
        self.assertFalse(self.decide(envelope(MARKER), kind="test").allowed)
        self.assertFalse(self.decide(envelope(f"{MENTION} {MARKER}", user="USYNTHETICOTHER1"), kind="test").allowed)
        self.assertFalse(self.decide(envelope(f"{MENTION} {MARKER}"), kind="progress").allowed)

    def test_all_present_markers_are_recorded_even_for_questions(self):
        second = "BRIDGE_TEST_SYNTHETIC_002"
        decision = self.decide(envelope(f"{MENTION} {MARKER}, {second} review?"),
                               policy(test_markers=frozenset({MARKER, second})))
        self.assertEqual(decision.candidate.test_markers, (MARKER, second))

    def test_malformed_inputs_fail_closed_without_exceptions(self):
        for value in (None, [], "event", {}, {"type": "event_callback", "event": []}):
            with self.subTest(value=value):
                self.assertFalse(evaluate(value, policy(), classification="question").allowed)
        for key, values in {"text": [[], {}, 42, "", " " * 5, "x" * 40001],
                            "ts": [None, 1.1, "1", "1.00000", "١.000001", {}],
                            "user": [None, 1, [], "demo"], "channel": [None, [], "demo"],
                            "bot_id": [[], "demo"], "thread_ts": [[], 1.0, "bad"]}.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assertFalse(self.decide(envelope(**{key: value})).allowed)
        value = envelope()
        value["event_id"] = []
        self.assertFalse(self.decide(value).allowed)

    def test_no_event_id_uses_message_identity(self):
        value = envelope()
        del value["event_id"]
        decision = self.decide(value)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.candidate.dedupe_keys, (f"message:{CHANNEL}:1000000000.000001",))

    def test_invalid_policy_is_rejected(self):
        for changes in ({"channels": set()}, {"peer_user_ids": set()}, {"channels": [CHANNEL]},
                        {"target_user_id": "@demo"}, {"self_bot_id": "bad"},
                        {"self_user_id": "bad"}, {"test_markers": frozenset({"loose words"})},
                        {"allow_thread_replies": "yes"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                policy(**changes)

    def test_echo_disables_raw_mentions_and_broadcasts(self):
        value = safe_echo(f"{MENTION} <!channel> <!here> <!subteam^SSYNTHETICGROUP> @everyone & <tag>")
        self.assertNotIn("<", value)
        self.assertNotIn(">", value)
        self.assertNotIn("@everyone", value)
        self.assertIn("&lt;@\u200bUSYNTHETICTARGET&gt;", value)
        self.assertIn("&amp;", value)
        self.assertFalse(self.decide(envelope(value)).allowed)
        with self.assertRaises(TypeError):
            safe_echo(None)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "demo-ledger.sqlite"
        self.ledger = Ledger(self.path)
        self.candidate = evaluate(envelope(), policy(), classification="question").candidate

    def test_replay_and_persistence(self):
        reservation = self.ledger.reserve(self.candidate)
        self.assertEqual(reservation.state, "reserved")
        self.assertIsNone(self.ledger.reserve(self.candidate))
        reopened = Ledger(self.path)
        self.assertIsNone(reopened.reserve(self.candidate))
        self.assertEqual(reopened.find(self.candidate), (reservation,))

    def test_either_event_id_or_channel_and_message_timestamp_blocks(self):
        self.ledger.reserve(self.candidate)
        same_event = replace(self.candidate, message_ts="1000000000.000002")
        same_message = replace(self.candidate, event_id="EvSYNTHETIC0002")
        self.assertIsNone(self.ledger.reserve(same_event))
        self.assertIsNone(self.ledger.reserve(same_message))
        self.assertIsNone(self.ledger.reserve(replace(same_message, event_id=None)))
        distinct = replace(self.candidate, event_id="EvSYNTHETIC0002", message_ts="1000000000.000002")
        self.assertIsNotNone(self.ledger.reserve(distinct))

    def test_single_use_marker_blocks_new_events_and_channels(self):
        first = replace(self.candidate, test_markers=(MARKER,))
        self.ledger.reserve(first)
        later = replace(first, channel="CSYNTHETICCHAN02", message_ts="1000000000.000009", event_id="EvSYNTHETIC0009")
        self.assertIsNone(self.ledger.reserve(later))
        different_marker = replace(later, test_markers=("BRIDGE_TEST_SYNTHETIC_002",))
        self.assertIsNotNone(self.ledger.reserve(different_marker))

    def test_quoted_contraction_does_not_consume_marker(self):
        for index, (opening, apostrophe, closing) in enumerate(
                (("'", "'", "'"), ("‘", "’", "’"))):
            with self.subTest(quote=opening):
                ledger = Ledger(Path(self.directory.name) / f"quoted-{index}.sqlite")
                question = envelope(
                    f"{MENTION} Review {opening}Don{apostrophe}t {MARKER} reuse{closing}?")
                candidate = evaluate(question, policy(), classification="question").candidate
                self.assertIsNotNone(ledger.reserve(candidate))
                actual_test = envelope(f"{MENTION} {MARKER}", ts="1000000000.000002")
                actual_test["event_id"] = "EvSYNTHETIC0002"
                candidate = evaluate(actual_test, policy(), classification="test").candidate
                self.assertIsNotNone(ledger.reserve(candidate))

    def test_all_markers_are_reserved_atomically(self):
        first = replace(self.candidate, test_markers=(MARKER, "BRIDGE_TEST_SYNTHETIC_002"))
        self.ledger.reserve(first)
        for marker in first.test_markers:
            later = replace(first, event_id="EvSYNTHETIC0002", message_ts="1000000000.000002", test_markers=(marker,))
            self.assertIsNone(self.ledger.reserve(later))

    def test_complete_blocks_replay(self):
        reservation = self.ledger.reserve(self.candidate)
        complete = self.ledger.complete(reservation.id, "1000000001.000001")
        self.assertEqual(complete.state, "complete")
        self.assertEqual(self.ledger.find(self.candidate), (complete,))
        self.assertIsNone(self.ledger.reserve(self.candidate))
        with self.assertRaises(ValueError):
            self.ledger.uncertain(reservation.id)

    def test_ambiguous_send_is_not_automatically_retried(self):
        reservation = self.ledger.reserve(self.candidate)
        uncertain = self.ledger.uncertain(reservation.id)
        self.assertEqual(uncertain.state, "uncertain")
        self.assertIsNone(Ledger(self.path).reserve(self.candidate))
        with self.assertRaises(ValueError):
            self.ledger.complete(reservation.id, "1000000001.000001")
        complete = self.ledger.reconcile_sent(reservation.id, "1000000001.000001")
        self.assertEqual(complete.state, "complete")
        self.assertIsNone(self.ledger.reserve(self.candidate))

    def test_confirmed_no_send_preserves_dedupe_for_controlled_retry(self):
        reservation = self.ledger.reserve(self.candidate)
        self.ledger.uncertain(reservation.id)
        # Only after external authoritative reconciliation and sender quiescence.
        retry = self.ledger.reconcile_not_sent(reservation.id)
        self.assertEqual(retry.state, "reserved")
        self.assertIsNone(self.ledger.reserve(self.candidate))
        self.ledger.complete(retry.id, "1000000001.000001")

    def test_invalid_transitions_and_receipts_are_rejected(self):
        reservation = self.ledger.reserve(self.candidate)
        with self.assertRaises(ValueError):
            self.ledger.complete(reservation.id, "not a timestamp")
        with self.assertRaises(ValueError):
            self.ledger.reconcile_not_sent(reservation.id)
        with self.assertRaises(ValueError):
            self.ledger.uncertain("missing")
        self.assertEqual(self.ledger.find(self.candidate)[0].state, "reserved")

    def test_concurrent_connections_only_one_reserves(self):
        ledgers = [Ledger(self.path) for _ in range(12)]
        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(lambda ledger: ledger.reserve(self.candidate), ledgers))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_in_memory_database_is_not_durable(self):
        with self.assertRaises(ValueError):
            Ledger(":memory:")


if __name__ == "__main__":
    unittest.main()
