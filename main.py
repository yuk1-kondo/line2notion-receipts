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
    "スーパー玉出": "食費",
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
    (["シャンプー", "洗剤", "トイレットペーパー", "日用品", "ティッシュ", "キッチンペーパー", "スポンジ", "歯ブラシ", "歯磨き", "ボディソープ", "ゴミ袋", "洗濯", "柔軟剤", "マスク", "除菌"], "日用品（スーパー・ドラッグストア）"),
    (["病院", "クリニック", "薬", "処方"], "医療"),
    (["犬", "ドッグ", "ペット", "フード", "トリミング", "おやつ"], "犬関係"),
    (["弁当", "おにぎり", "サンドイッチ", "パン", "牛乳", "卵", "肉", "野菜", "米", "寿司", "刺身", "惣菜", "ビール", "酒", "飲料", "お茶", "コーヒー", "紅茶", "カップ麺"], "食費"),
    (["Netflix", "Spotify", "Adobe", "サブスク", "定額"], "サブスク（Netflix, Spotify など）"),
]

# ====== ユーティリティ ======

def normalize_store_name(raw: str) -> str:
    if not raw:
        return ""
    name = raw.strip()
    # 法人種別やノイズを簡易除去
    for token in [
        "株式会社",
        "合同会社",
        "有限会社",
        "(株)",
        "㈱",
    ]:
        name = name.replace(token, "").strip()
    # 余分な空白や全角スペースを統一
    name = re.sub(r"\s+", " ", name.replace("　", " ")).strip()
    return name[:50]


def heuristic_extract_store_name(ocr_text: str) -> str:
    if not ocr_text:
        return ""
    text = ocr_text.strip()
    lower = text.lower()
    # 1) 既知のチェーン名が含まれる行を優先（最長一致）
    best = ""
    for merchant in MERCHANT_MAP.keys():
        if merchant in text:
            # 含む行のうち最も長い候補を採用
            candidate_lines = [ln for ln in text.splitlines() if merchant in ln]
            if candidate_lines:
                cand = max(candidate_lines, key=len)
                best = normalize_store_name(cand)
                break
    if best:
        return best
    # 2) 英字系の有名ワード
    for word in [
        "FamilyMart",
        "LAWSON",
        "Seven",
        "Starbucks",
        "DOUTOR",
    ]:
        if word.lower() in lower:
            return normalize_store_name(word)
    # 3) 上部数行から店名っぽい行を拾う
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    head = lines[:20]
    ban_words = [
        "領収", "領収書", "レシート", "明細", "控え", "ご利用", "合計", "小計", "税込", "税", "No", "TEL",
        "電話", "日時", "日付", "時間", "売上", "レジ", "お買上"
    ]
    brand_line = ""
    branch_line = ""
    for ln in head:
        if any(b in ln for b in ban_words):
            continue
        # 記号のみ/数値主体を除外
        if len(re.sub(r"[^\w一-龠ぁ-んァ-ヶー・\-\s]", "", ln)) < 2:
            continue
        # 「店」「本店」「支店」などを優先
        if re.search(r"店|本店|支店", ln):
            branch_line = ln
        # チェーン名らしき語（カタカナ長語/スーパー等）を記録
        if re.search(r"スーパー|ドラッグ|マート|コーヒー|カフェ|電鉄|百貨店|ショッピング|モール", ln):
            brand_line = ln
        if branch_line and brand_line:
            combined = brand_line if len(brand_line) >= len(branch_line) else f"{brand_line} {branch_line}"
            return normalize_store_name(combined)
    # 4) 最初の候補をフォールバック
    return normalize_store_name(head[0] if head else "")

def _to_iso_date(year: int, month: int, day: int) -> str:
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except Exception:
        return ""

def _convert_japanese_era(era: str, y: int) -> int:
    era = era.strip()
    if era in ("令和", "R", "r"):
        # Reiwa year 1 = 2019
        return 2018 + y
    if era in ("平成", "H", "h"):
        # Heisei year 1 = 1989
        return 1988 + y
    if era in ("昭和", "S", "s"):
        # Showa year 1 = 1926
        return 1925 + y
    return 0

def extract_purchase_date_local(ocr_text: str) -> str:
    """Japanese receipt date extractor. Returns ISO YYYY-MM-DD or empty string.
    Supports: YYYY/MM/DD, YYYY-MM-DD, YYYY年M月D日, era dates (令和/平成/昭和), R/H/S formats.
    """
    if not ocr_text:
        return ""
    text = ocr_text.replace("年 ", "年").replace("月 ", "月").replace("日 ", "日")
    # 1) Gregorian: 2025/9/28 or 2025-09-28
    m = re.search(r"(20\d{2}|19\d{2})[\-/\.](\d{1,2})[\-/\.](\d{1,2})", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        iso = _to_iso_date(y, mo, d)
        if iso:
            return iso
    # 2) Gregorian (Japanese): 2025年9月28日
    m = re.search(r"(20\d{2}|19\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        iso = _to_iso_date(y, mo, d)
        if iso:
            return iso
    # 3) Era: 令和/平成/昭和 N年M月D日
    m = re.search(r"(令和|平成|昭和)\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if m:
        era, ey, mo, d = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
        y = _convert_japanese_era(era, ey)
        iso = _to_iso_date(y, mo, d)
        if iso:
            return iso
    # 4) Compact era notations: R7.9.28 or R7/9/28 or R7-9-28
    m = re.search(r"([RrHhSs])(\d{1,2})[\./\-](\d{1,2})[\./\-](\d{1,2})", text)
    if m:
        era_letter, ey, mo, d = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
        era_map = {"R": "令和", "r": "令和", "H": "平成", "h": "平成", "S": "昭和", "s": "昭和"}
        y = _convert_japanese_era(era_map.get(era_letter, ""), ey)
        iso = _to_iso_date(y, mo, d)
        if iso:
            return iso
    # 5) Another era form: R7年9月28日 / H31年1月8日
    m = re.search(r"([RrHhSs])\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if m:
        era_letter, ey, mo, d = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
        era_map = {"R": "令和", "r": "令和", "H": "平成", "h": "平成", "S": "昭和", "s": "昭和"}
        y = _convert_japanese_era(era_map.get(era_letter, ""), ey)
        iso = _to_iso_date(y, mo, d)
        if iso:
            return iso
    return ""

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
    if r.status_code != 200:
        print(f"Notion Receipts API Error: {r.status_code} - {r.text}")
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
    if r.status_code != 200:
        print(f"Notion Items API Error: {r.status_code} - {r.text}")
    r.raise_for_status()
    try:
        return r.json().get("id", "")
    except Exception:
        return ""


def ocr_bytes_to_text(image_bytes: bytes) -> str:
    image = vision.Image(content=image_bytes)
    res = vision_client.document_text_detection(image=image)
    if res.error.message:
        raise RuntimeError(res.error.message)
    return (res.full_text_annotation.text or "").strip()


def detect_logo_brand(image_bytes: bytes) -> str:
    """VisionのLogo Detectionでブランド名を抽出する（ロゴ文字がOCRで落ちた場合の補完）。"""
    try:
        image = vision.Image(content=image_bytes)
        res = vision_client.logo_detection(image=image)
        if res.error.message:
            return ""
        annotations = res.logo_annotations or []
        if not annotations:
            return ""
        # 最も確度の高いロゴのdescriptionを返す
        best = max(annotations, key=lambda a: getattr(a, "score", 0.0))
        return normalize_store_name(best.description or "")
    except Exception:
        return ""


# ====== 分類 ======

def rule_classify(store_name: str, item_name: str):
    if store_name:
        for key, cat in MERCHANT_MAP.items():
            if store_name.strip().startswith(key) or key in store_name:
                return (cat, 1.0, "rule")
        # Store heuristics by keywords in store name
        sn = store_name
        if re.search(r"ドラッグ|薬局|ココカラ|マツキヨ|スギ薬局|ウェルシア", sn):
            return ("日用品（スーパー・ドラッグストア）", 0.85, "rule")
        if re.search(r"スーパー|マート|マーケット|百貨店|食品館|生鮮|フレッシュ", sn):
            return ("食費", 0.85, "rule")
        if re.search(r"電鉄|駅|JR|バス|地下鉄|メトロ|IC|切符", sn):
            return ("交通", 0.9, "rule")
        if re.search(r"カフェ|コーヒー|ベーカリー|パン|スターバックス|ドトール", sn):
            return ("食費", 0.85, "rule")
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
注意: JSON以外の文字やコードブロックを含めないでください。

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
    resp = r.json()
    # 安全にテキストを取り出す（partsが無い場合のフォールバック）
    text = ""
    try:
        text = (
            resp.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
            .strip()
        )
    except Exception:
        text = ""
    # Try strict JSON first
    data = None
    try:
        data = json.loads(text)
    except Exception:
        pass
    if data is None:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    if data is None:
        data = {"category": "その他", "confidence": 0.5, "reason": "parse_fallback"}
    cat = data.get("category", "その他")
    if cat not in ALLOWED_CATEGORIES:
        cat = "その他"
    try:
        conf = float(data.get("confidence", 0.5))
    except Exception:
        conf = 0.5
    # Clamp confidence to [0,1]
    conf = max(0.0, min(1.0, conf))
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
    {{"store_name": "...", "purchase_date": "YYYY-MM-DD"}}

    良い例:
    OCR: セブン-イレブン大阪梅田店 2025/9/28 12:34
    出力: {{"store_name":"セブン-イレブン大阪梅田店","purchase_date":"2025-09-28"}}

    OCR: LAWSON 神戸三宮本店 令和7年9月28日
    出力: {{"store_name":"LAWSON 神戸三宮本店","purchase_date":"2025-09-28"}}

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
    resp = r.json()
    # 安全にテキストを取り出す（partsが無い/安全制限時のフォールバック）
    text = ""
    try:
        text = (
            resp.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
            .strip()
        )
    except Exception:
        text = ""
    # Parse robustly
    data = None
    try:
        data = json.loads(text)
    except Exception:
        pass
    if data is None:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    if data is None:
        data = {"store_name": "", "purchase_date": ""}
    # フォールバック（失敗時は今日）
    if not data.get("purchase_date"):
        # Try local extractor as a second chance before defaulting to today
        local = extract_purchase_date_local(ocr_text)
        data["purchase_date"] = local or datetime.now().strftime("%Y-%m-%d")
    # 店名フォールバック（Geminiが空/微妙な場合）
    store = normalize_store_name(data.get("store_name", ""))
    if not store:
        store = heuristic_extract_store_name(ocr_text)
    data["store_name"] = store
    return data


def gemini_extract_items_csv(ocr_text: str) -> str:
    """
    CSVで「商品名, 価格」を複数行で返す（ヘッダー行ありでもOK）
    価格は数値/カンマ・円記号混在OK
    """
    prompt = f"""
以下のレシートOCRテキストから商品明細を抽出し、CSVで出力してください。
列: 商品名, 価格
制約:
- CSVヘッダーは省略可。コードブロックや前後のコメントは付けないでください。
- 価格は整数で、カンマや円記号は除去してください。
例:
おにぎり, 128
牛乳, 198

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
    resp = r.json()
    # 安全にテキストを取り出す
    text = ""
    try:
        text = (
            resp.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
            .strip()
        )
    except Exception:
        text = ""
    # Remove any fencing accidentally returned
    if text:
        text = re.sub(r"```[a-zA-Z]*\n?", "", text)
        text = text.replace("```", "")
    return text


def parse_items_csv(csv_text: str):
    # ```csv や ``` フェンスを除去
    if csv_text:
        csv_text = re.sub(r"```[a-zA-Z]*\n?", "", csv_text)
        csv_text = csv_text.replace("```", "")
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
    # ロゴ検出（ブランド補完）
    logo_brand = detect_logo_brand(image_bytes)

    # 3) ヘッダー抽出（まずローカルで店名/日付を抽出し、不足時のみGeminiにフォールバック）
    store_name = heuristic_extract_store_name(ocr_text)
    # ロゴ由来のブランド名があり、店名に含まれていなければ結合
    if logo_brand and logo_brand not in store_name:
        store_name = f"{logo_brand} {store_name}".strip()
    purchase_date = extract_purchase_date_local(ocr_text)
    if not store_name or not purchase_date:
        header = gemini_extract_header(ocr_text)
        if not store_name:
            store_name = (header.get("store_name") or store_name).strip()
        if not purchase_date:
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
    low_conf_count = 0
    failed = 0
    for name, price_str in items:
        try:
            price = float(str(price_str).replace("¥", "").replace(",", "").strip())
        except Exception:
            price = None

        category, confidence, source = classify_category(store_name, name, price)
        if confidence < 0.6:
            low_conf_count += 1
        try:
            page_id = create_item_row(
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
            if page_id:
                created += 1
            else:
                failed += 1
        except Exception as e:
            print(f"create_item_row failed: {e}")
            failed += 1

    # 6) LINEへ返信（サマリ）
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=f"{purchase_date}｜{store_name}\n登録: {created}件（低信頼: {low_conf_count}／失敗: {failed}）\nレシートID: {receipt_id[-8:]}"),
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
