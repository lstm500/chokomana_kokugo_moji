# ちょこまな ひらがな・カタカナ

**構成**: GitHub（`app.py` + `requirements.txt`）→ Streamlit → Supabase（既存のちょこまな共通DB）。独立した学習アプリです。

## 機能

- ひらがな／カタカナ、それぞれ **文字・ことば × 初級・中級**の8チャレンジ。各6問または10問、全284題から抽出。四択・手動の「つぎへ」・誤答時のみ答えの表示と再回答・音声ヒント・一時停止・途中終了。
- **1タップでチャレンジ開始**。回答や問題移動では Streamlit サーバーへの通信や画面再描画を行いません。初回画面のDB通信も行わず、本人識別・同期は後から非同期に開始します。
- **音声→硬筆A4 PDF**。音声入力（対応ブラウザのみ）／キーボード、印刷前の語句確認・修正、ひらがな／カタカナ、縦書き／横書き。薄いなぞり文字、マス目、十字補助線、練習用空白マスのみ（タイトル・名前欄なし）。3文字以下は3回、4〜9文字は2回、10文字以上は1回。
- **ちょこまな共通形式 `chokomana.learning.v1`**。固定メニュー・技能台帳、問題ID、初回／再回答、正誤・誤答内容・ヒント／正解開示、実学習時間、同一／異なる問題、日付、教材版、途中終了を別々に記録。習得・能力を単一正答率から自動断定しません。
- **端末への先行保存 → 本人確認後の共通DBへのACK付き再送**。本人未確認の記録を、後から別の子どもへ自動統合しません。スタンプは結果イベントの検証後に1実施1個（字／語・金銀銅白）。紙の字形は自動採点しません。
- **速度ログ**。`iframe`起動、ホーム初期表示、メニューボタン押下／pointerdown、最初の問題を描画したタイミング、JS長時間処理・エラー、バックエンド応答を記録。設定画面からJSON／CSVで出力できます。計測可能な範囲の中央値・P95も表示。

## GitHub と Streamlit

1. 新しいGitHubリポジトリのルートに `app.py` と `requirements.txt` を配置。
2. Streamlit Community Cloud等でリポジトリとメインファイル `app.py` を指定して公開。
3. Streamlit Secrets に次を設定します。**サービスロールキーは必ずSecretsに入れ、GitHubやフロントエンドへ記載しないでください。**

```toml
SUPABASE_URL = "https://<既存のSupabaseプロジェクト>.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "<既存のservice_roleまたはsecret API key>"
KANA_APP_URL = "https://<公開した新しいStreamlitアプリのURL>/"
CHOKOMANA_HOME_URL = "https://chokomana-hkgx4uyxxezjtr2sj35jwj.streamlit.app/"
```

`CHOKOMANA_HOME_URL` は省略可能です。`CHOKOMANA_PROFILE_ID` は不要。ログイン画面もありません。**Supabase未設定でもチャレンジと端末内の記録は動きますが、DB連携・スタンプの共有保存は動きません。**

## Supabase（既存プロジェクト）

`setup_supabase.sql` を内容確認後に **既存プロジェクトの SQL Editor で一度だけ実行**してください。これは今回まだ適用していません。

- `chokomana_launch_tickets`／`chokomana_device_links` の既存の許可アプリに `kana` を追加。
- `chokomana_append_kana_events_v1` RPC を追加。既存の共通学習台帳／学習イベント／スタンプを利用。
- RLS と既存データはそのまま。イベント書き込みには、DB上の端末連携表で本人を検証し、固定の学習ID・問題の正誤をサーバーで検証してから保存。
- Supabaseの新規テーブルは作りません。GRANTは必要な既存サービスロール権限に限定しています。

### ちょこまな統合アプリとの接続で必要なこと

**現時点で統合アプリのソースは変更していません。** 連携完了には統合アプリ側で `kana` を登録し、本人確認済みの匿名Authセッションから新しい一度限りの `cm_launch` チケットを発行する処理が必要です。既存のチケット方式と共通DBテーブルを使い、新しいログインやユーザーIDの手入力は追加しません。

- 台帳取得: `chokomana_learning_catalog` の `source_app=kana`、`entry_type in (menu,skill)`。
- イベント取得: `chokomana_learning_events` の `source_app=kana`、本人の `profile_id` のみ。
- スタンプ取得: `chokomana_stamps` の `source_app=kana`、本人の `profile_id` のみ。
- チケット: `source_app=kana`, `menu_id`（例 `kana:hiragana:letters:1`）, `content_version=kana-material-1`, `launch_mode=challenge`。検証できた場合のみ直接チャレンジ開始。直接指定できない起動は通常のメニュー画面。
- 台帳JSON: `chokomana_catalog.json`（ラベル・技能・難易度・問題数・次メニュー・暫定の所要時間）。実測結果のない時間は **仮設定** と明記。

このアプリには、**集約アプリの「今日やるリスト」の作成・進級判定・複数アプリの時間和集合計算UI**は含まれません。共通イベントを出力するまでが今回の作業範囲です。

## 細かな注意点

- 音声認識はブラウザ `SpeechRecognition` / `webkitSpeechRecognition` に依存し、対応しない端末では手入力してください。音声APIの課金連携は実装していません。
- `pykakasi` で漢字を**読みのかな表記に転換**しますが、「普通の語の意味を推測し、音声認識ミスを自動で完全訂正する」機能はありません。ふざけた単語を別語に勝手に変えません。印刷前に必ずプレビューで確認してください。
- PDF出力テキストはPDF生成のためアプリのPythonプロセスへ送られますが、学習の正誤ログやSupabaseへは保存しません。
- 存在するメニューだけを台帳へ登録します。問題別の履歴は実際に表示・回答したものだけを記録。未学習と記録不足・未連携は別物です。
- 未送信の端末ログはブラウザのデータ削除や端末故障で失われる可能性があります。別アプリのドメインとはLocalStorageを共有しません。
- 他タブで同じ学習が開かれた場合は学習時間の重複計上を抑制します。完全なクロスアプリ重複排除と日本時間0時の分割は統合アプリの集計処理が必要です。
- **外部アプリのタップからStreamlitサーバー起動まで**の時間はこのアプリだけでは測れません。ちょこまな側に起動時刻を記録すれば実測できます。現版では問題選択ボタンからの計測が可能です。

## 検証状況

- **実施済み**: Python構文／内部テスト、8メニュー・計64問のブラウザでのチャレンジ・正解判定、誤答→再回答、単一タップからの描画時間と回答経過時間のログ、A4横・縦・長語のPDF出力と描画画像確認。
- **未実施**: 実機のマイク・Web Speech、Streamlitデプロイ環境での端末速度、既存DBへの本番INSERT／ACK、集約アプリでの履歴取得→計画作成→学習結果反映。SQLは実DBの現状と照合していますが、適用・本番書込はまだです。

### 開発環境での簡易テスト

`pip install -r requirements.txt` 後に次を実行できます。通常のユーザーには必要ありません。

```bash
python app.py --self-test
python app.py --sample-pdf sample.pdf
python app.py --chokomana-catalog > chokomana_catalog.json
streamlit run app.py
```
