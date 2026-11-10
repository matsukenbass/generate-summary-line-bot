"""Webhook entry point that can report application startup failures."""

import base64
import hashlib
import hmac
import json
import logging
import os
from urllib.request import Request, urlopen

import boto3

logger = logging.getLogger(__name__)


def lambda_handler(event, context):
    try:
        from handler import lambda_handler as handle_request
    except Exception as exc:
        # Do not include exception text: it may contain secrets.
        logger.error("Application startup failed (%s)", type(exc).__name__)
        return reply_to_startup_failure(event)

    return handle_request(event, context)


def reply_to_startup_failure(event):
    try:
        secret = json.loads(
            boto3.client("secretsmanager").get_secret_value(
                SecretId=os.environ["SECRET_ARN"]
            )["SecretString"]
        )
        body = event["body"]
        if event.get("isBase64Encoded"):
            body_bytes = base64.b64decode(body, validate=True)
        else:
            body_bytes = body.encode("utf-8")
        headers = {key.lower(): value for key, value in (event.get("headers") or {}).items()}
        signature = base64.b64encode(
            hmac.new(secret["LINE_CHANNEL_SECRET"].encode(), body_bytes, hashlib.sha256).digest()
        ).decode()
        if not hmac.compare_digest(signature, headers.get("x-line-signature", "")):
            return {"statusCode": 403, "body": "Invalid signature"}

        for line_event in json.loads(body_bytes).get("events", []):
            if line_event.get("type") != "message" or not line_event.get("replyToken"):
                continue
            payload = json.dumps({
                "replyToken": line_event["replyToken"],
                "messages": [{"type": "text", "text": "サービスの起動に失敗しました。管理者にお問い合わせください。"}],
            }).encode("utf-8")
            request = Request(
                "https://api.line.me/v2/bot/message/reply",
                data=payload,
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + secret["LINE_CHANNEL_ACCESS_TOKEN"]},
                method="POST",
            )
            with urlopen(request, timeout=5):
                pass
        return {"statusCode": 200, "body": "OK"}
    except Exception as exc:
        logger.error("Could not report startup failure (%s)", type(exc).__name__)
        return {"statusCode": 500, "body": "Internal server error"}
