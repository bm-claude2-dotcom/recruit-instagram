#!/usr/bin/env python3
"""
analyze_trends.py  ─ 採用コンテンツ 生成ノート  ローカル Web アプリ

  python analyze_trends.py
  → ブラウザが自動で http://localhost:5000 を開きます
  → 画面内の「🔄 最新データに更新」ボタンをクリックするだけで分析が走ります

.env に設定してから実行:
    SLACK_BOT_TOKEN=xoxb-...
    GEMINI_API_KEY=...
"""

import json
import os
import sys
import time
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Windows cp932 環境で絵文字・日本語の print が落ちないようにする
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from google import genai

# ── Flask ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.resolve()
app = Flask(__name__)
CORS(app)

# ── 設定 ──────────────────────────────────────────────────────────────────────
OWN_CHANNEL_ID  = "C0B2AF0FG91"   # 26卒メンバーチャンネル
COMP_CHANNEL_ID = "C0B9NMW0PC3"   # ClaudeCode コンテストチャンネル
DAYS_BACK       = 7
GEMINI_MODEL    = "gemini-2.5-flash"

COMPANY_CTX = """\
【株式会社プリンシプル（Principle Co.,Ltd.）】
・事業: データ解析を軸としたデジタルマーケティング（GA4・GTM・BigQuery・SEO・Web広告など）
・Mission: データとアクションをつなぎ、よりよい世界を実現します
・Vision: 世界で最も信頼されるマーケティングDXパートナー
・Value: 自立したプロフェッショナル / Win-Win / 世界基準・多様性
・従業員数: 約100名、東京都千代田区
・採用ターゲット: 新卒・大学3-4年生。「誰と働くか」「成長できるか」「入社後のリアル」「ビジョンが叶うか」
・トンマナ: 誠実・等身大・データドリブン。煽らない・誇張しない。確証のない情報は [要確認: ◯◯] で明示。"""

PROMPT_TEMPLATE = """\
あなたは株式会社プリンシプルの採用マーケティング専門家です。
以下のSlackデータを分析し、Instagram採用広報コンテンツ企画を【必ず10個】生成してください。
各企画に「Instagramカルーセル投稿のスライド構成案」と「キャプション本文」を同時に生成してください。
以下のJSONスキーマに厳密に従い、JSONのみ出力してください（説明文不要）。

{company_ctx}

## 自社Slackメッセージ（{own_label}） - 直近{days_back}日間 {own_count}件
{own_text}

## 参照Slackメッセージ（{comp_label}） - {comp_count}件
{comp_text}

## 生成ルール
- 10個の企画はすべて異なる切り口（成長/技術/カルチャー/リアル/働く人/ビジョンなど複数軸）
- Slackの具体的なエピソード・雰囲気を最大限に反映させること（抽象的テーマより実体験ベース）
- sns_draft: Instagramカルーセルのスライド構成案（スライド1〜5枚ごとの画像レイアウト説明と文字案を箇条書き）
- content_long: Instagramキャプション本文（絵文字・改行を活用、ハッシュタグ5〜8個込み、250〜400文字）
- 確証のない情報は [要確認: ◯◯] と明記
- category は次の6つから選択: 成長 / 技術 / カルチャー / リアル / 働く人 / ビジョン

## 出力JSONスキーマ（items 10件必須）
{{
  "last_updated": "{last_updated}",
  "slack_status": {{
    "company_messages": {own_count},
    "competitor_messages": {comp_count}
  }},
  "items": [
    {{
      "id": 1,
      "title": "タイトル（20文字以内）",
      "hook": "切り口の狙い・フック（60文字以内）",
      "category": "カテゴリ名",
      "sns_draft": "スライド構成案（1枚目: タイトル文字案 / 2枚目: 〜 / 5枚目: まとめ・CTA）",
      "content_long": "Instagramキャプション本文（絵文字・ハッシュタグ込み、250〜400文字）"
    }},
    ... 合計10件 ...
  ]
}}"""


# ── Slack ─────────────────────────────────────────────────────────────────────
def fetch_messages(client: WebClient, channel_id: str) -> list:
    oldest   = str((datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)).timestamp())
    messages = []
    cursor   = None
    while True:
        try:
            kw = {"channel": channel_id, "oldest": oldest, "limit": 200}
            if cursor:
                kw["cursor"] = cursor
            resp = client.conversations_history(**kw)
            messages.extend(resp.get("messages", []))
            if not resp.get("has_more"):
                break
            cursor = resp["response_metadata"]["next_cursor"]
            time.sleep(0.5)
        except SlackApiError as ex:
            print(f"  Slack API エラー ({channel_id}): {ex.response['error']}")
            break
    return [m for m in messages if m.get("text") and not m.get("bot_id")]


def msgs_to_text(msgs: list, max_n: int = 150) -> str:
    return "\n---\n".join(m["text"] for m in msgs[:max_n])


# ── Gemini ────────────────────────────────────────────────────────────────────
def call_gemini(client, prompt: str) -> dict:
    try:
        from google.genai import types as gt
        cfg = gt.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=16384,
            temperature=0.8,
        )
        resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=cfg)
    except (ImportError, TypeError) as ex:
        print(f"  設定なしでフォールバック: {ex}")
        resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)

    raw = resp.text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        raw = raw.rsplit("```", 1)[0].strip()
    return json.loads(raw)


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(str(BASE_DIR), "prompt_generator.html")


@app.route("/api/credentials-status")
def api_credentials_status():
    return jsonify({
        "slack_token_set": bool(os.environ.get("SLACK_BOT_TOKEN")),
        "gemini_key_set":  bool(os.environ.get("GEMINI_API_KEY")),
    })


@app.route("/api/set-credentials", methods=["POST"])
def api_set_credentials():
    body        = request.get_json(silent=True) or {}
    slack_token = body.get("slack_token", "").strip()
    gemini_key  = body.get("gemini_key",  "").strip()

    if not slack_token:
        return jsonify({"error": "SLACK_BOT_TOKEN が空です"}), 400
    if not gemini_key:
        return jsonify({"error": "GEMINI_API_KEY が空です"}), 400

    # .env ファイルに書き込み（既存のキーは上書き、それ以外は保持）
    env_path = BASE_DIR / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    new_lines, saw_slack, saw_gemini = [], False, False
    for line in lines:
        if line.startswith("SLACK_BOT_TOKEN="):
            new_lines.append(f"SLACK_BOT_TOKEN={slack_token}"); saw_slack = True
        elif line.startswith("GEMINI_API_KEY="):
            new_lines.append(f"GEMINI_API_KEY={gemini_key}"); saw_gemini = True
        else:
            new_lines.append(line)
    if not saw_slack:  new_lines.append(f"SLACK_BOT_TOKEN={slack_token}")
    if not saw_gemini: new_lines.append(f"GEMINI_API_KEY={gemini_key}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    # 現在の Python プロセスにも即時反映
    os.environ["SLACK_BOT_TOKEN"] = slack_token
    os.environ["GEMINI_API_KEY"]  = gemini_key

    print("✅ 認証情報を .env に保存しました")
    return jsonify({"ok": True})

@app.route('/api/slack-events', methods=['GET'])
def get_slack_events():
    try:
        # 1. 認証情報が設定されているか確認
        slack_token = os.environ.get("SLACK_BOT_TOKEN") 
        if not slack_token:
            return jsonify({"error": "SLACK_BOT_TOKEN が未設定です"}), 400
        
        slack = WebClient(token=slack_token)
        
        # 2. すでにある OWN_CHANNEL_ID ("C0B2AF0FG91"：26卒メンバー) からメッセージを取得
        # ※ 過去ログを多めに取得するため、ここでは直接 conversations_history を呼び出します
        history = slack.conversations_history(channel=OWN_CHANNEL_ID, limit=100)
        messages = history.get("messages", [])
        
        events_data = []
        
        # 3. 「#社内イベント」が含まれる投稿をフィルタリング
        for msg in messages:
            text = msg.get("text", "")
            # ボットの投稿を除外し、#社内イベント が入っているものだけを抽出
            if "#社内イベント" in text and not msg.get("bot_id"):
                photo_url = None
                
                # 画像（ファイル）が添付されているかチェック
                if "files" in msg and len(msg["files"]) > 0:
                    first_file = msg["files"][0]
                    # Slack上の画像のURL
                    photo_url = first_file.get("url_private_download") or first_file.get("url_private")
                
                events_data.append({
                    "text": text,
                    "photo": photo_url
                })
                
        return jsonify(events_data)

    except SlackApiError as e:
        return jsonify({"error": f"Slack APIエラー: {e.response['error']}"}), 500
    except Exception as e:
        return jsonify({"error": f"予期せぬエラー: {str(e)}"}), 500


@app.route("/api/update")
def api_update():
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    gemini_key  = os.environ.get("GEMINI_API_KEY")

    if not slack_token:
        return jsonify({"error": "SLACK_BOT_TOKEN が未設定です（.env を確認）"}), 500
    if not gemini_key:
        return jsonify({"error": "GEMINI_API_KEY が未設定です（.env を確認）"}), 500

    try:
        # 1. Slack 取得
        print("[1/2] Slack からメッセージを取得中...")
        slack    = WebClient(token=slack_token)
        own_msgs = fetch_messages(slack, OWN_CHANNEL_ID)
        cmp_msgs = fetch_messages(slack, COMP_CHANNEL_ID)
        print(f"      自社: {len(own_msgs)}件 ／ 参照: {len(cmp_msgs)}件")

        # 2. Gemini 生成
        print("[2/2] Gemini で 10選 × (ショート文 + 長文) を生成中...")
        gemini = genai.Client(api_key=gemini_key)
        now    = datetime.now().strftime("%Y-%m-%d %H:%M")
        own_text_safe = msgs_to_text(own_msgs).replace("{", "{{").replace("}", "}}")
        cmp_text_safe = msgs_to_text(cmp_msgs).replace("{", "{{").replace("}", "}}")
        prompt = PROMPT_TEMPLATE.format(
            company_ctx  = COMPANY_CTX,
            own_label    = "26卒メンバーチャンネル",
            own_count    = len(own_msgs),
            comp_label   = "ClaudeCodeコンテストチャンネル",
            comp_count   = len(cmp_msgs),
            days_back    = DAYS_BACK,
            own_text     = own_text_safe or "(メッセージなし)",
            comp_text    = cmp_text_safe or "(メッセージなし)",
            last_updated = now,
        )
        data = call_gemini(gemini, prompt)
        data["last_updated"] = now
        n = len(data.get("items", []))
        print(f"✅ 完了: {n} 個の切り口を生成")
        return jsonify(data)

    except Exception as ex:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(ex)}), 500


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    def _open():
        webbrowser.open("http://127.0.0.1:5000")

    threading.Timer(1.5, _open).start()
    print("🚀  http://127.0.0.1:5000  (Ctrl+C で停止)")
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
