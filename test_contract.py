"""Dependency-free regression tests for the HTTP handler contract."""

import unittest

import bot
from bot import CtxBody, ReplyBody, TickBody
from composer import compose


class ContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await bot.teardown()

    async def test_expired_trigger_is_not_sent(self):
        await bot.push_context(CtxBody(
            scope="trigger",
            context_id="expired",
            version=1,
            payload={
                "id": "expired",
                "merchant_id": "not-needed-for-expired-trigger",
                "expires_at": "2026-01-01T00:00:00Z",
            },
            delivered_at="2025-01-01T00:00:00Z",
        ))

        result = await bot.tick(TickBody(
            now="2026-02-01T00:00:00Z", available_triggers=["expired"]
        ))

        self.assertEqual(result, {"actions": []})

    async def test_stale_context_returns_conflict(self):
        body = CtxBody(
            scope="trigger", context_id="trigger", version=1, payload={}, delivered_at="2026-01-01T00:00:00Z"
        )
        await bot.push_context(body)

        response = await bot.push_context(body.model_copy(update={"version": 0}))

        self.assertEqual(response.status_code, 409)
        self.assertIn(b'"reason":"stale_version"', response.body)

    async def test_repeated_auto_reply_with_new_conversation_ids_ends(self):
        first = await bot.reply(ReplyBody(
            conversation_id="conv_auto_1", merchant_id="m_001", from_role="merchant",
            message="Thank you for contacting us! Our team will respond shortly.",
            received_at="2026-04-26T10:00:00Z", turn_number=1,
        ))
        second = await bot.reply(ReplyBody(
            conversation_id="conv_auto_2", merchant_id="m_001", from_role="merchant",
            message="Thank you for contacting us! Our team will respond shortly.",
            received_at="2026-04-26T10:05:00Z", turn_number=2,
        ))

        self.assertEqual(first["action"], "send")
        self.assertEqual(second["action"], "end")

    def test_new_digest_entry_is_used_when_a_known_trigger_lacks_an_id(self):
        category = {"slug": "dentists", "voice": {}, "digest": [
            {"id": "old", "kind": "research", "title": "Older item", "source": "Old source"},
            {"id": "new", "kind": "research", "title": "Fresh injected finding", "source": "New source", "trial_n": 120},
        ]}
        merchant = {"identity": {"name": "Clinic", "owner_first_name": "Meera", "languages": ["en"]}}
        trigger = {"kind": "research_digest", "payload": {}, "suppression_key": "fresh"}

        result = compose(category, merchant, trigger)

        self.assertIn("fresh injected finding", result["body"].lower())


if __name__ == "__main__":
    unittest.main()
