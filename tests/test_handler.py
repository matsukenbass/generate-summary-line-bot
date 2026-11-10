import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


def load_handler():
    """Load the Lambda with its external services replaced by local doubles."""
    modules = {}

    def module(name, **attributes):
        result = types.ModuleType(name)
        result.__dict__.update(attributes)
        modules[name] = result
        return result

    secret = {
        "LINE_CHANNEL_SECRET": "secret",
        "LINE_CHANNEL_ACCESS_TOKEN": "token",
        "OPENAI_API_KEY": "key",
        "BUCKET_NAME": "bucket",
    }
    client = Mock()
    client.get_secret_value.return_value = {"SecretString": json.dumps(secret)}
    table = Mock()
    resource = Mock()
    resource.Table.return_value = table
    module("boto3", client=Mock(return_value=client), resource=Mock(return_value=resource))
    module("openai", AuthenticationError=type("AuthenticationError", (Exception,), {}))
    module("langchain_openai", ChatOpenAI=Mock())
    module("langchain_community", __path__=[])
    module("langchain_community.callbacks", get_openai_callback=Mock())
    module("langchain_core", __path__=[])
    module("langchain_core.messages", HumanMessage=lambda content: types.SimpleNamespace(content=content))
    module("requests", get=Mock())
    module("bs4", BeautifulSoup=Mock())
    webhook = Mock()
    webhook.add.return_value = lambda function: function
    line_api = Mock()
    module("linebot", LineBotApi=Mock(return_value=line_api), WebhookHandler=Mock(return_value=webhook), __path__=[])
    module("linebot.models", MessageEvent=object, TextMessage=object,
           TextSendMessage=lambda text: types.SimpleNamespace(text=text))
    module("youtube_transcript_api", YouTubeTranscriptApi=Mock(), __path__=[])
    module("youtube_transcript_api.proxies", WebshareProxyConfig=Mock())
    spec = importlib.util.spec_from_file_location(
        "test_loaded_handler", Path(__file__).resolve().parents[1] / "src/generate_summary/handler.py"
    )
    handler = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules), patch.dict("os.environ", {"SECRET_ARN": "arn"}):
        spec.loader.exec_module(handler)
    return handler


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.handler = load_handler()
        self.event = types.SimpleNamespace(
            message=types.SimpleNamespace(text="https://example.com/article"), reply_token="reply"
        )

    def test_webhook_passes_body_and_signature_to_line(self):
        result = self.handler.lambda_handler(
            {"headers": {"x-line-signature": "signature"}, "body": "body"}, None
        )
        self.handler.handler.handle.assert_called_once_with("body", "signature")
        self.assertEqual(result, {"statusCode": 200, "body": "OK"})

    def test_invalid_url_replies_without_accessing_services(self):
        self.event.message.text = "not a URL"
        self.handler.handle_text_message(self.event)
        self.handler.table.get_item.assert_not_called()
        self.assertEqual(self.handler.line_bot_api.reply_message.call_args.args[1].text, "不正なURLです。")

    def test_existing_summary_is_reused(self):
        self.handler.table.get_item.return_value = {"Item": {"answer": "保存済み"}}
        with patch.object(self.handler, "get_content") as get_content:
            self.handler.handle_text_message(self.event)
        get_content.assert_not_called()
        self.assertEqual(self.handler.line_bot_api.reply_message.call_args.args[1].text, "保存済み")

    def test_new_summary_is_generated_saved_and_returned(self):
        self.handler.table.get_item.return_value = {}
        with patch.object(self.handler, "get_content", return_value=("記事本文", "記事タイトル")), \
             patch.object(self.handler, "get_answer", return_value=("要約。", 0.01)) as answer, \
             patch.object(self.handler, "put_file_to_s3_bucket") as save_file, \
             patch.object(self.handler, "put_summary_generate_table") as save_record:
            self.handler.handle_text_message(self.event)
        self.assertIn("記事本文", answer.call_args.args[1][0].content)
        self.assertIn("[記事タイトル](https://example.com/article)", save_file.call_args.args[0])
        self.assertEqual(save_file.call_args.args[1], "記事タイトル.md")
        save_record.assert_called_once_with(self.event.message.text, "要約。", 0.01)
        self.assertEqual(self.handler.line_bot_api.reply_message.call_args.args[1].text, "要約。")

    def test_failure_returns_generic_message_without_exception_details(self):
        self.handler.table.get_item.side_effect = RuntimeError("sensitive detail")
        with self.assertLogs(self.handler.logger, level="ERROR") as logs:
            self.handler.handle_text_message(self.event)
        reply = self.handler.line_bot_api.reply_message.call_args.args[1].text
        self.assertEqual(reply, "エラーが発生しました。時間をおいて再度お試しください。")
        self.assertNotIn("sensitive detail", " ".join(logs.output))

    def test_youtube_video_ids(self):
        self.assertEqual(self.handler.get_youtube_video_id("https://youtu.be/abc123"), "abc123")
        self.assertEqual(self.handler.get_youtube_video_id("https://www.youtube.com/watch?v=abc123&t=10"), "abc123")
        self.assertIsNone(self.handler.get_youtube_video_id("https://example.com/"))

    def test_prompts_limit_input_length(self):
        self.assertNotIn("END", self.handler.build_prompt("a" * 10000 + "END"))
        self.assertNotIn("END", self.handler.build_youtube_prompt("a" * 4000 + "END"))


if __name__ == "__main__":
    unittest.main()
