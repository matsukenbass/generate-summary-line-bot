import json
from langchain.chat_models import ChatOpenAI
from langchain.schema import HumanMessage
from langchain.callbacks import get_openai_callback

import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse

import boto3


import os

from linebot import LineBotApi, WebhookHandler
from linebot.models import (
    MessageEvent,
    TextMessage,
    TextSendMessage,
)

import uuid


try:
    secrets_manager = boto3.client("secretsmanager")

    secret_arn = os.environ["SECRET_ARN"]
    secret_value_response = secrets_manager.get_secret_value(SecretId=secret_arn)
    secret_value = secret_value_response["SecretString"]

    # JSON文字列を辞書に変換
    secret_data = json.loads(secret_value)

    handler = WebhookHandler(secret_data["LINE_CHANNEL_SECRET"])
    table_name = os.getenv("SUMMARYGENERATETABLE_TABLE_NAME")
    bucket_name = secret_data["BUCKET_NAME"]
    line_bot_api = LineBotApi(secret_data["LINE_CHANNEL_ACCESS_TOKEN"])
    os.environ["OPENAI_API_KEY"] = secret_data["OPENAI_API_KEY"]


except Exception as e:
    print("Error:", e)


# テーブルを参照する
table = boto3.resource("dynamodb").Table(table_name)


def lambda_handler(event, context):
    headers = event["headers"]
    body = event["body"]

    # get X-Line-Signature header value
    signature = headers["x-line-signature"]

    # handle webhook body
    handler.handle(body, signature)

    return {"statusCode": 200, "body": "OK"}


@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    """TextMessage handler"""

    messages = []

    url = event.message.text
    is_valid_url = validate_url(url)

    if not is_valid_url:
        answer = "不正なURLです。"
    elif check_url(url):
        answer = check_url(url)
    else:
        llm = ChatOpenAI(temperature=0, model_name="gpt-4o-2024-05-13")
        content, title = get_content(url)
        prompt = build_prompt(content)
        messages.append(HumanMessage(content=prompt))
        answer, cost = get_answer(llm, messages)
        put_file_to_s3_bucket(convert_md(answer, url, title), title + ".md")
        put_summary_generate_table(url, answer, cost)

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=answer))


# Webページの内容を取得
def get_content(url):
    response = requests.get(url)
    soup = BeautifulSoup(response.text, "html.parser")
    # fetch text from main (change the below code to filter page)
    if soup.main:
        return soup.main.get_text(), soup.title.string
    elif soup.article:
        return soup.article.get_text(), soup.title.string
    else:
        return soup.body.get_text(), soup.title.string


# プロンプト作成
def build_prompt(content, n_chars=1000):
    return f"""あなたはプロのエンジニアである。
    また、以下はとあるWebページのコンテンツである。内容を{n_chars}から{n_chars}程度でわかりやすく要約してください。

========

{content[:4000]}

========

    また要約を作成する際は、以下の制約条件を守ってください。

    # 制約条件：
    ・重要なキーワードを取り残さないこと
    ・要約の冒頭で、当該のページで触れられている技術やサービスの名前を箇条書きで記載すること
    ・日本語で書くこと
"""


# 要約結果取得
def get_answer(llm, messages):
    with get_openai_callback() as cb:
        answer = llm(messages)
    return answer.content, cb.total_cost


# 有効なURLかチェック
def validate_url(url):
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except ValueError:
        return False


# DynamoDBのテーブルに結果を挿入
def put_summary_generate_table(url, answer, cost):
    # テーブルにアイテムを追加する
    table.put_item(
        Item={"id": str(uuid.uuid4()), "url": url, "answer": answer, "cost": str(cost)}
    )


## テーブルに同じURLが存在していたらtrueとanswerを返す関数
def check_url(url):
    response = table.get_item(Key={"url": url})
    if "Item" in response:
        print("already exist")
        return response["Item"]["answer"]
    return False


### 日本語の文章を句点で改行する関数
def split_sentences(text):
    return text.replace("。", "。\n")


# Obsidianでの保存形式に合わせて結果を整形
def convert_md(summary: str, url: str, title: str):
    return f"""
---
tags: 💻 
---
#💻 

### リンク
[{title}]({url})

### 概要
{split_sentences(summary)}

### わかったこと


"""


# mdファイルをS3保存
def put_file_to_s3_bucket(file_content, file_name, bucket_name=bucket_name):
    s3 = boto3.resource("s3")
    s3.Bucket(bucket_name).put_object(Key=file_name, Body=file_content)
    if os.path.isfile(file_content):
        os.remove(file_content)
