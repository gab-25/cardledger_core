import json

import httpx
from django.test import SimpleTestCase, TestCase

from .models import Game
from .openrouter import SCHEMA, OpenRouterClient, OpenRouterError, prompt_for

READ = {"name": "Counterspell", "set_code": "DMR", "collector_number": "045", "language": "IT", "foil": True}


GAMES = {"mtg": "Magic: The Gathering", "pokemon": "Pokémon", "lorcana": "Disney Lorcana", "onepiece": "One Piece",
         "yugioh": "Yu-Gi-Oh!", "riftbound": "Riftbound"}

# The Magic prompt as it was before prompts per game: it must not change.
MAGIC_PROMPT = (
    "You are looking at a photo of one Magic: The Gathering card. Read it and answer with JSON only.\n"
    "- name: the card's ENGLISH name, even when the card is printed in another language.\n"
    "- set_code: the expansion code printed at the bottom left (e.g. DSK), or empty if not visible.\n"
    "- collector_number: the collector number printed at the bottom left without the total "
    "(\"0187/0286\" -> \"0187\"), or empty if not visible.\n"
    "- language: the language the card is printed in, one of EN, IT, DE, FR, ES, PT, JP, KO, ZHS, RU; "
    "empty if unreadable or another language.\n"
    "- foil: true if the card is foil (holographic sheen, star next to the set code).\n"
    "Never guess a collector number you cannot read. "
    "If the photo shows no Magic card (or it is unreadable), answer with an empty name and empty fields."
)


def completion(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class RecognizeTest(SimpleTestCase):
    def client_for(self, handler):
        return OpenRouterClient("key", model="test/model", transport=httpx.MockTransport(handler))

    def test_recognize_parses_json(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            seen["auth"] = request.headers["authorization"]
            return httpx.Response(200, json=completion(json.dumps(READ)))

        rec = self.client_for(handler).recognize_card(b"\xff\xd8jpeg")
        self.assertEqual((rec.name, rec.set_code, rec.collector_number), ("Counterspell", "dmr", "045"))
        self.assertEqual((rec.language, rec.foil), ("IT", True))
        self.assertFalse(hasattr(rec, "color") or hasattr(rec, "type_line"))
        self.assertNotIn("type_line", json.dumps(seen["body"]))
        self.assertEqual(seen["body"]["model"], "test/model")
        self.assertIn("image_url", json.dumps(seen["body"]))
        self.assertEqual(seen["auth"], "Bearer key")
        # Reading a card needs no reasoning: with it, reasoning models (e.g. Qwen) get slow and costly.
        self.assertEqual(seen["body"]["reasoning"], {"enabled": False})

    def test_missing_model_raises(self):
        with self.assertRaises(OpenRouterError):
            OpenRouterClient("key", model="")

    def test_recognize_bad_content_raises(self):
        with self.assertRaises(OpenRouterError):
            self.client_for(lambda r: httpx.Response(200, json=completion("sorry"))).recognize_card(b"x")

    def test_recognize_http_error_raises(self):
        with self.assertRaises(OpenRouterError):
            self.client_for(lambda r: httpx.Response(401, json={"error": {"message": "bad key"}})).recognize_card(b"x")

    def test_recognize_timeout_raises(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        with self.assertRaises(OpenRouterError):
            self.client_for(handler).recognize_card(b"x")

    def test_unsupported_language_blank(self):
        data = dict(READ, language="zh-TW")
        rec = self.client_for(lambda r: httpx.Response(200, json=completion(json.dumps(data)))).recognize_card(b"x")
        self.assertEqual(rec.language, "")

    def test_schema_has_no_color_or_type(self):
        self.assertEqual(SCHEMA["required"], ["name", "set_code", "collector_number", "language", "foil"])


class PromptTest(TestCase):
    def test_magic_prompt_unchanged(self):
        self.assertEqual(prompt_for("mtg"), MAGIC_PROMPT)

    def test_each_game_has_its_prompt(self):
        games = Game.objects.all()
        self.assertTrue(games)
        for game in games:
            slug = game.slug
            with self.subTest(slug):
                prompt = prompt_for(slug)
                self.assertIn(game.name, prompt)
                if slug != "mtg":
                    self.assertNotIn("Magic", prompt)
                    self.assertIn("JSON only", prompt)
        self.assertEqual(len({prompt_for(g.slug) for g in games}), len(games))

    def test_request_uses_the_game_prompt(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=completion(json.dumps(READ)))

        client = OpenRouterClient("key", model="test/model", transport=httpx.MockTransport(handler))
        client.recognize_card(b"x", "pokemon")
        self.assertEqual(seen["body"]["messages"][0]["content"][0]["text"], prompt_for("pokemon"))
        client.recognize_card(b"x")
        self.assertEqual(seen["body"]["messages"][0]["content"][0]["text"], MAGIC_PROMPT)
