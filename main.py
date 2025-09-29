import os
import json
import hashlib
import re
import csv
import io
from datetime import datetime

import requests
from flask import Request
from google.cloud import vision
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, ImageMessage, TextSendMessage

# ====== 環境変数 ======
NOTION_API_KEY = os.getenv("NOTION_API_KEY")
NOTION_ITEMS_DB_ID = os.getenv("NOTION_ITEMS_DB_ID")
NOTION_RECEIPTS_DB_ID = os.getenv("NOTION_RECEIPTS_DB_ID")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# ====== SDK 初期化 ======
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
vision_client = vision.ImageAnnotatorClient()

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

# ====== カテゴリ（確定リスト） ======
ALLOWED_CATEGORIES = [
    "食費",
    "交通",
    "日用品（スーパー・ドラッグストア）",
    "医療",
    "犬関係",
    "趣味・娯楽",
    "教育・学習",
    "サブスク（Netflix, Spotify など）",
    "交際費（飲み会・プレゼント）",
    "その他",
]

# ルールベース（必要に応じて追加/調整）
MERCHANT_MAP = {
    "セブン-イレブン": "食費",
    "ファミリーマート": "食費",
    "ローソン": "食費",
    "阪急電鉄": "交通",
    "JR": "交通",
    "スギ薬局": "日用品（スーパー・ドラッグストア）",
    "ココカラファイン": "日用品（スーパー・ドラッグストア）",
    "カインズ": "日用品（スーパー・ドラッグストア）",
    "スターバックス": "食費",
    "ドトール": "食費",
    # 犬関係の例
    "コーナン": "犬関係",
    "ペット": "犬関係",
}
KEYWORD_RULES = [
    (["切符", "乗車", "運賃", "ICチャージ", "改札"], "交通"),
    (["シャンプー", "洗剤", "トイレットペーパー", "日用品"], "日用品（スーパー・ドラッグストア）"),
    (["病院", "クリニック", "薬", "処方"], "医療"),
    (["犬", "ドッグ", "ペット", "フード", "トリミング", "おやつ"], "犬関係"),
    (["Netflix", "Spotify", "Adobe", "サブスク", "定額"], "サブスク（Netflix, Spotify など）"),
]

# ====== ユーティリティ ======

def build_receipt_id(purchase_date: str, store_name: str, extracted_text: str | None, line_message_id: str | None) -> str:
    base = f"{purchase_date}::{store_name or ''}::{line_message_id or ''}::{(extracted_text or '')[:5000]}"
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]
    return f"{purchase_date}_{(store_name or '').strip()}_{digest}"


def notion_query_by_receipt_id(receipts_db_id: str, receipt_id: str):
    url = f"https://api.notion.com/v1/databases/{receipts_db_id}/query"
    payload = {"filter": {"property": "レシートID", "rich_text": {"equals": receipt_id}}}
    r = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    return r.json().get("results", [])


def upsert_receipt_page(purchase_date: str, store_name: str, receipt_id: str) -> str:
    exists = notion_query_by_receipt_id(NOTION_RECEIPTS_DB_ID, receipt_id)
    if exists:
        return exists[0]["id"]
    title = f"{purchase_date}｜{store_name or '店名不明'}"
    payload = {
        "parent": {"database_id": NOTION_RECEIPTS_DB_ID},
        "properties": {
            "レシート名": {"title": [{"text": {"content": title}}]},
            "購入日付": {"date": {"start": purchase_date}},
            "店名": {"rich_text": [{"text": {"content": store_name or ""}}]},
            "レシートID": {"rich_text": [{"text": {"content": receipt_id}}]},
        },
    }
    r = requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()["id"]


def create_item_row(
    product_name: str,
    price: float | None,
    purchase_date: str,
    store_name: str,
    category: str,
    confidence: float,
    source: str,
    receipt_page_id: str,
    receipt_id: str,
):
    payload = {
        "parent": {"database_id": NOTION_ITEMS_DB_ID},
        "properties": {
            "商品名": {"title": [{"text": {"content": (product_name or "不明")[:200]}}]},
            "金額": {"number": price},
            "購入日付": {"date": {"start": purchase_date}},
            "店名": {"rich_text": [{"text": {"content": store_name or ""}}]},
            "カテゴリ": {"select": {"name": category if category in ALLOWED_CATEGORIES else "その他"}},
            "信頼度": {"number": confidence},
            "分類元": {"select": {"name": source}},
            "レシートID": {"rich_text": [{"text": {"content": receipt_id}}]},
            "レシート": {"relation": [{"id": receipt_page_id}]},
        },
    }
    r = requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json=payload, timeout=20)
    r.raise_for_status()


def ocr_bytes_to_text(image_bytes: bytes) -> str:
    image = vision.Image(content=image_bytes)
    res = vision_client.document_text_detection(image=image)
    if res.error.message:
        raise RuntimeError(res.error.message)
    return (res.full_text_annotation.text or "").strip()


# ====== 分類 ======

def rule_classify(store_name: str, item_name: str):
    if store_name:
        for key, cat in MERCHANT_MAP.items():
            if store_name.strip().startswith(key) or key in store_name:
                return (cat, 1.0, "rule")
    text = f"{store_name} {item_name}".lower()
    for words, cat in KEYWORD_RULES:
        if any(w.lower() in text for w in words):
            return (cat, 0.9, "rule")
    return None


def ai_classify(store_name: str, item_name: str, amount: float | None):
    prompt = f"""
あなたは家計簿のカテゴリ分類器です。次のカテゴリのいずれか1つだけを返してください。
カテゴリ一覧: {", ".join(ALLOWED_CATEGORIES)}

JSONのみを返し、余計な文章は書かないでください。
出力例: {{"category":"食費","confidence":0.82,"reason":"コンビニの食品名"}}

入力:
店名: {store_name or ""}
品目名: {item_name or ""}
金額: {amount if amount is not None else ""}
"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 128},
    }
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0)) if m else {"category": "その他", "confidence": 0.5, "reason": "parse_fallback"}
    cat = data.get("category", "その他")
    if cat not in ALLOWED_CATEGORIES:
        cat = "その他"
    conf = float(data.get("confidence", 0.5))
    return (cat, conf, "ai")


def classify_category(store_name: str, item_name: str, amount: float | None):
    hit = rule_classify(store_name, item_name)
    if hit:
        return hit
    return ai_classify(store_name, item_name, amount)


# ====== Gemini 抽出 ======

def gemini_extract_header(ocr_text: str) -> dict:
    """
    店名, 購入日付(YYYY-MM-DD) を JSON で返す
    """
    prompt = f"""
以下のレシートOCRテキストから店名と購入日付を抽出してください。
日本のレシート日付表記(例: 2025/9/28, 令和, xx年xx月xx日)にも対応し、出力はYYYY-MM-DDに揃えてください。
JSONのみを返し、余計な文章は書かないでください。
出力フォーマット:
{"store_name": "...", "purchase_date": "YYYY-MM-DD"}

OCR:
{ocr_text[:8000]}
"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 128},
    }
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0)) if m else {"store_name": "", "purchase_date": ""}
    # フォールバック（失敗時は今日）
    if not data.get("purchase_date"):
        data["purchase_date"] = datetime.now().strftime("%Y-%m-%d")
    return data


def gemini_extract_items_csv(ocr_text: str) -> str:
    """
    CSVで「商品名, 価格」を複数行で返す（ヘッダー行ありでもOK）
    価格は数値/カンマ・円記号混在OK
    """
    prompt = f"""
以下のレシートOCRテキストから商品明細を抽出し、CSVで出力してください。
列: 商品名, 価格
価格は数値に変換できる形（例: 198, 1234）にしてください。余計な文章は書かないでください。

OCR:
{ocr_text[:8000]}
"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048},
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


def parse_items_csv(csv_text: str):
    f = io.StringIO(csv_text)
    reader = csv.reader(f)
    rows = []
    for row in reader:
        if not row or len(row) < 2:
            continue
        # ヘッダーっぽい行をスキップ
        if "商品" in row[0] and "価格" in (row[1] if len(row) > 1 else ""):
            continue
        rows.append([row[0].strip(), row[1].strip()])
    return rows


# ====== LINE ハンドラ ======

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    # 1) 画像取得
    msg_id = event.message.id
    content = line_bot_api.get_message_content(msg_id)
    image_bytes = content.content  # SDK v2 は .content にバイト列

    # 2) OCR
    ocr_text = ocr_bytes_to_text(image_bytes)

    # 3) ヘッダー抽出
    header = gemini_extract_header(ocr_text)
    store_name = (header.get("store_name") or "").strip()
    purchase_date = (header.get("purchase_date") or datetime.now().strftime("%Y-%m-%d")).strip()

    # 4) レシートID & レシートページUpsert
    receipt_id = build_receipt_id(purchase_date, store_name, ocr_text, msg_id)
    receipt_page_id = upsert_receipt_page(purchase_date, store_name, receipt_id)

    # 5) 明細抽出（CSV→行ごと）
    csv_text = gemini_extract_items_csv(ocr_text)
    items = parse_items_csv(csv_text)

    # 最低1件なければ通知
    if not items:
        line_bot_api.reply_message(
            event.reply_token, TextSendMessage(text="明細が抽出できませんでした。画像が鮮明かご確認ください。")
        )
        return

    created = 0
    for name, price_str in items:
        try:
            price = float(str(price_str).replace("¥", "").replace(",", "").strip())
        except Exception:
            price = None

        category, confidence, source = classify_category(store_name, name, price)
        create_item_row(
            product_name=name,
            price=price,
            purchase_date=purchase_date,
            store_name=store_name,
            category=category,
            confidence=confidence,
            source=source,
            receipt_page_id=receipt_page_id,
            receipt_id=receipt_id,
        )
        created += 1

    # 6) LINEへ返信（サマリ）
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=f"{purchase_date}｜{store_name}\n登録: {created}件\nレシートID: {receipt_id[-8:]}"),
    )


def main(request: Request):
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        return "Invalid signature", 400
    except Exception as e:
        print(f"Error: {e}")
        # 200で返してLINE側の再送を避ける
        return "OK"
    return "OK"
