# line2notion-receipts

LINE に送ったレシート画像を OCR + Gemini で解析し、Notion データベースへ「1商品=1行」で登録します。レシート単位の集計 DB も自動作成/紐付けします。

## 必要なもの
- GCP プロジェクト（Cloud Functions Gen2 / Vision API 有効化）
- LINE Messaging API チャネル（チャネルアクセストークン/シークレット）
- Notion インテグレーション（Internal Integration Token）
- Notion データベース x 2
  - Items（明細）
  - Receipts（レシート集計）

## Notion プロパティ（推奨名）
- Items DB
  - 商品名 (Title)
  - 金額 (Number)
  - 購入日付 (Date)
  - 店名 (Rich text)
  - カテゴリ (Select)
  - 信頼度 (Number)
  - 分類元 (Select: rule / ai / manual)
  - レシートID (Rich text)
  - レシート (Relation → Receipts)
- Receipts DB
  - レシート名 (Title)
  - 購入日付 (Date)
  - 店名 (Rich text)
  - レシートID (Rich text)
  - 明細 (Relation ← Items)
  - 合計金額 (Rollup: 明細→金額, Sum)
  - 品目数 (Rollup: 明細→任意, Count)

カテゴリ（Select 候補）
- 食費
- 交通
- 日用品（スーパー・ドラッグストア）
- 医療
- 犬関係
- 趣味・娯楽
- 教育・学習
- サブスク（Netflix, Spotify など）
- 交際費（飲み会・プレゼント）
- その他

## セットアップ
1) 依存インストールは Cloud Functions 側で自動。ローカルで実行する場合は `pip install -r requirements.txt`。

2) 環境変数を用意
- `.env.sample` をコピーして `.env` を作成し、以下を設定
  - NOTION_API_KEY
  - NOTION_ITEMS_DB_ID
  - NOTION_RECEIPTS_DB_ID
  - LINE_CHANNEL_ACCESS_TOKEN
  - LINE_CHANNEL_SECRET
  - GEMINI_API_KEY

3) デプロイ
- macOS / zsh 例：
  - `chmod +x deploy.sh`
  - `. ./.env`（もしくは `export` で各キーをセット）
  - `./deploy.sh`
- 成功後に表示される URL を LINE Developers の Webhook URL に登録し有効化

## 動作
- Bot にレシート画像を送信すると、Items に 1商品=1行 を作成し、Receipts に紐付けます。
- 同一レシートは「レシートID」で論理的にまとめ、Receipts 側の Rollup が合計/件数を計算します。

## 注意/運用
- 機密情報は Git にコミットしない（`.env` は .gitignore 済み）
- 誤分類は Items のカテゴリを手で修正可。ルールは `main.py` の `MERCHANT_MAP`/`KEYWORD_RULES` で拡張可能

## ライセンス
- Private use intended.

## 作業履歴
- 2025-09-29
  - Cloud Functions Gen2 向けの最小構成を追加（`main.py`, `requirements.txt`, `deploy.sh`, `.env.sample`, `.gitignore`, `README.md`）
  - LINE 画像受信 → Vision OCR → Gemini 解析（ヘッダー/明細） → Notion 2DB（Receipts/Items）登録の一連を実装
  - カテゴリ自動分類（ルール優先＋Geminiフォールバック）、信頼度・分類元の保存を実装
  - レシート単位のUpsert（レシートID生成）とItemsとのRelation付与を実装

## 今後の作業
- 二重登録ガード（商品レベル）
  - Itemsに作成前クエリ（レシートID×商品名×金額）で存在チェックし、重複を回避
- CSVコードブロック対策
  - Geminiが ```csv フェンス付きで返す場合に備え、パーサでフェンス除去
- 低信頼アイテム数をLINE返信に表示
  - 信頼度しきい値（例: 0.6未満）の件数を返信メッセージに併記
- ルール辞書のNotion管理
  - `MERCHANT_MAP` / `KEYWORD_RULES` をNotionの辞書DBから読み込む方式へ
- 秘密情報のSecret Manager移行
  - `--set-env-vars` から `--set-secrets` へ移行して安全性向上
- レシート画像の保存と添付
  - Cloud Storage保存＋署名付きURLをReceiptsに保存（Files/URL）
- リトライとレート制御
  - Notion/Geminiの429/5xxで指数バックオフを実装
- LINE管理コマンド（任意）
  - `#ping` / `#stats` / `#last` などの軽い運用コマンド
