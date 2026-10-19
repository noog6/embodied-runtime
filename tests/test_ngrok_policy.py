"""Regression contracts for generated ingress; cloud execution needs acceptance."""

import json
import re
import unittest

from embodied_runtime.ngrok_policy import build_traffic_policy, validate_policy_settings
from embodied_runtime.sms import OutboundMediaStore


class NgrokPolicyTest(unittest.TestCase):
    def policy(self, path="/hooks/twilio"):
        return build_traffic_policy(path, "robot-webhooks", "twilio-auth-token")

    def test_webhook_verification_is_enforced_only_for_exact_post_route(self):
        rules = self.policy()["on_http_request"]
        self.assertEqual(set(self.policy()), {"on_http_request"})
        self.assertEqual(len(rules), 4)  # within documented Free five-rule ceiling
        sms = rules[2]
        self.assertEqual(sms["expressions"], ['req.url.path == "/hooks/twilio" && req.method == \'POST\''])
        self.assertEqual([a["type"] for a in sms["actions"]], ["rate-limit", "verify-webhook"])
        self.assertEqual(sms["actions"][1]["config"], {
            "provider": "twilio", "enforce": True,
            "secret": "${secrets.get('robot-webhooks', 'twilio-auth-token')}",
        })
        for path in ("/sms", "/twilio/inbound", "/", "/hooks/sms-v2/"):
            with self.subTest(path=path):
                expression = self.policy(path)["on_http_request"][2]["expressions"][0]
                self.assertIn("req.url.path == " + json.dumps(path), expression)
                self.assertNotIn("startsWith", expression)
        media = rules[3]
        self.assertEqual([a["type"] for a in media["actions"]], ["rate-limit"])
        self.assertIn("req.method in ['GET', 'HEAD']", media["expressions"][0])

    def test_default_rejection_is_complement_of_the_two_allowed_routes(self):
        rules = self.policy()["on_http_request"]
        sms, media = (rules[index]["expressions"][0] for index in (2, 3))
        self.assertEqual(rules[0], {
            "expressions": [f"!(({sms}) || ({media}))"],
            "actions": [{"type": "custom-response", "config": {"status_code": 403}}],
        })

    def test_media_match_covers_current_bearers_and_only_registered_extensions(self):
        expression = self.policy()["on_http_request"][3]["expressions"][0]
        match_literal = expression.split(".matches(", 1)[1].split(") &&", 1)[0]
        pattern = re.compile(json.loads(match_literal))
        token = OutboundMediaStore().stage(b"image")
        for suffix in ("jpg", "png"):
            for candidate in (token, "a" * 40, "a" * 64, "_-A09" * 9):
                self.assertIsNotNone(pattern.fullmatch(f"/outbound-media/{candidate}.{suffix}"))
        for path in (
            "/", "/sms", "/outbound-media/", "/outbound-media/x.jpg",
            "/outbound-media/" + "a" * 39 + ".jpg",
            "/outbound-media/" + "a" * 65 + ".jpg",
            f"/outbound-media/{token}.webp", f"/outbound-media/{token}.jpeg",
            f"/outbound-media/{token}.jpg/extra", f"/other/{token}.jpg",
            f"/outbound-media/nested/{token}.png", f"/outbound-media/{token}.JPG",
        ):
            with self.subTest(path=path):
                self.assertIsNone(pattern.fullmatch(path))

    def test_limits_are_enforced_separate_aggregate_buckets_without_private_keys(self):
        rules = self.policy()["on_http_request"]
        for index, name, capacity in ((2, "sms", 30), (3, "outbound-media", 60)):
            limit = rules[index]["actions"][0]
            self.assertEqual(limit, {"type": "rate-limit", "config": {
                "name": name, "enforce": True, "algorithm": "sliding_window",
                "capacity": capacity, "rate": "60s", "bucket_key": [json.dumps(name)],
            }})
        self.assertEqual(rules[1], {
            "expressions": ["'content-length' in req.headers && req.content_length > 65536"],
            "actions": [{"type": "custom-response", "config": {"status_code": 413}}],
        })
        serialized = json.dumps(self.policy())
        # A request-header map may guard Content-Length, but no namespace-level
        # has() call may return: ngrok rejects it before starting the endpoint.
        for forbidden in ("oauth", "has(req.", "req.url.raw", "on_http_response", "log"):
            self.assertNotIn(forbidden, serialized)

    def test_ambiguous_routes_and_injected_resource_names_are_rejected_without_echo(self):
        for path in ("sms", "/sms?x=1", "/sms#x", "/{route}", "/a/../sms",
                     "/a/./sms", "//sms", "/s%6ds", "/sms\n", "/outbound-media/x.jpg"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "static absolute"):
                self.policy(path)
        for value in (None, "", "actual secret!", "x' + dangerous", "${env.TWILIO_AUTH_TOKEN}", "a\n"):
            for position in (0, 1):
                names = ["vault", "secret"]
                names[position] = value
                with self.subTest(value=value, position=position):
                    with self.assertRaises(ValueError) as caught:
                        validate_policy_settings("/sms", *names)
                    if value:
                        self.assertNotIn(value, str(caught.exception))
