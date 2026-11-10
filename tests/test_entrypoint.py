import base64
import hashlib
import hmac
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


sys.modules.setdefault("boto3", types.ModuleType("boto3"))
spec = importlib.util.spec_from_file_location(
    "entrypoint", Path(__file__).resolve().parents[1] / "src/generate_summary/entrypoint.py"
)
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)


class StartupFailureTest(unittest.TestCase):
    def test_signed_event_gets_safe_reply(self):
        secret = {"LINE_CHANNEL_SECRET": "secret", "LINE_CHANNEL_ACCESS_TOKEN": "token"}
        body = json.dumps({"events": [{"type": "message", "replyToken": "reply"}]})
        signature = base64.b64encode(hmac.new(b"secret", body.encode(), hashlib.sha256).digest()).decode()
        client = Mock()
        client.get_secret_value.return_value = {"SecretString": json.dumps(secret)}
        with patch.object(entrypoint.boto3, "client", return_value=client, create=True), \
             patch.dict(entrypoint.os.environ, {"SECRET_ARN": "arn"}), \
             patch.object(entrypoint, "urlopen") as send:
            response = entrypoint.reply_to_startup_failure({"headers": {"X-Line-Signature": signature}, "body": body})
        self.assertEqual(response["statusCode"], 200)
        payload = json.loads(send.call_args.args[0].data)
        self.assertEqual(payload["replyToken"], "reply")
        self.assertNotIn("secret", payload["messages"][0]["text"])

    def test_invalid_signature_does_not_send(self):
        client = Mock()
        client.get_secret_value.return_value = {"SecretString": json.dumps({"LINE_CHANNEL_SECRET": "secret"})}
        with patch.object(entrypoint.boto3, "client", return_value=client, create=True), \
             patch.dict(entrypoint.os.environ, {"SECRET_ARN": "arn"}), \
             patch.object(entrypoint, "urlopen") as send:
            response = entrypoint.reply_to_startup_failure({"headers": {"x-line-signature": "invalid"}, "body": "{}"})
        self.assertEqual(response["statusCode"], 403)
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
