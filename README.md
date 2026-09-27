# The Silicon Commute 🎧

GAFAM と半導体企業の動きを、**平日毎朝・約10分の英語ポッドキャスト**にして自動配信する仕組みです。
英語の2人の掛け合い音声と、**英日対訳スクリプト・単語リスト**をスマホのアプリ（PWA）で読めます。

```
 04:45 JST（平日）GitHub Actions が自動起動
   │
   ├─ ① ニュース収集   Google News RSS ／ テック系メディアRSS ／ 公式ニュースルーム ／（任意）CEOのXポスト
   ├─ ② 編集          Gemini が重要ニュースを5〜6本選び、記事本文から事実を整理
   ├─ ③ 台本          Gemini が英語2人対話（約1,400語）＋全行の日本語訳＋単語リストを作成
   ├─ ④ 音声化        Gemini TTS（2話者）で音声化 → MP3（音量正規化）
   └─ ⑤ 配信          MP3 は GitHub Releases に保存、アプリと Podcast RSS を GitHub Pages に公開
                                     ↓
          iPhone：Podcast アプリで聴く ＋ アプリで英日対訳を読む
```

---

## セットアップ（約15分・すべて無料）

### 必要なもの
- GitHub アカウント（無料）
- Google AI Studio の Gemini API キー（無料）

### 1. リポジトリを作る
1. GitHub で **New repository** → 名前を `silicon-commute`、**Public** を選んで作成
   （無料プランの GitHub Pages は Public リポジトリが必要です）
2. 作成直後の画面の **「uploading an existing file」** をクリック
3. このフォルダの中身を**すべて**ドラッグ＆ドロップ → **Commit changes**

> ⚠️ **Mac の注意**：`.github` フォルダは Finder で隠れています。
> Finder で **⌘ + Shift + .（ピリオド）** を押すと表示されるので、`.github` も一緒にドラッグしてください。
> アップロード後、リポジトリに `.github/workflows/daily.yml` があれば OK です。

<details><summary>ターミナル派の方（GitHub CLI）</summary>

```bash
brew install gh && gh auth login
cd ~/Downloads/silicon-commute
git init && git add . && git commit -m "init"
gh repo create silicon-commute --public --source=. --push
```
</details>

### 2. Gemini API キーを取得
1. https://aistudio.google.com/apikey を開く（Google アカウントでログイン）
2. **Create API key** → 表示されたキーをコピー

### 3. キーを GitHub に登録（Secrets）
リポジトリの **Settings → Secrets and variables → Actions → New repository secret**

| Name | Secret |
|---|---|
| `GEMINI_API_KEY` | 手順2のキー |
| `X_BEARER_TOKEN`（任意） | X API のトークン（CEOのポストも拾いたい場合のみ・従量課金） |

### 4. GitHub Pages を有効化
**Settings → Pages → Build and deployment → Source** を **「GitHub Actions」** に変更

### 5. 初回を今すぐ作る
**Actions** タブ → 左の **Daily episode** → **Run workflow** → 緑のボタン
→ 5分ほどで完了。以後は平日の朝に自動で届きます。

### 6. iPhone で使う
- **アプリ**：Safari で `https://<ユーザー名>.github.io/silicon-commute/` を開く → 共有ボタン →「ホーム画面に追加」
- **Podcast アプリ**：アプリのホーム下部に表示される RSS の URL をコピー →
  Podcast アプリの「ライブラリ」→ 右上「…」→「URLで番組をフォロー」

> Apple Podcasts は新しい回の反映に時間がかかることがあります。確実に朝聴くなら、
> アプリ側（ホーム画面のアイコン）で再生するか、更新の速い Overcast / Pocket Casts を使うのがおすすめです。

---

## アプリの機能
- 英日対訳スクリプト：再生中の行がハイライト＆自動スクロール、**行をタップするとその場所から再生**
- 表示切替：英＋日／英語のみ／**和訳を隠す（1行ずつ確認できる学習モード）**／日本語のみ
- 再生速度 0.8〜1.5×、15秒戻る/進む、前回の続きから再生、ロック画面操作
- ニュースタブ：各ニュースの日本語要約・「なぜ重要か」・出典リンク、「この話を聴く」でジャンプ
- 単語タブ：今日のフレーズ＋重要語彙（例文をタップすると該当箇所へ）
- オフライン保存（⬇ボタン）：地下・トンネルでも途切れず再生

## カスタマイズ（`config.yaml`）
| 変えたいこと | 項目 |
|---|---|
| 対象企業・検索キーワード | `companies`（`enabled: false` で除外、AI企業も追加可） |
| 長さ・英語の速さ | `episode.target_minutes` / `words_per_minute`（標準150。長くしたいときは上げる） |
| 英語の難しさ | `episode.english_style` |
| 声・キャラクター | `hosts[].voice`（Gemini TTS のボイス名：Charon, Aoede, Kore, Puck など30種） |
| 使うモデル | `models.text` / `models.tts` |
| 配信時刻 | `.github/workflows/daily.yml` の `cron`（UTC表記。JST = UTC + 9時間） |

`config.yaml` やアプリ（`site/`）を編集したら、**Actions → Daily episode → Run workflow** を1回実行すると反映されます
（その日の回がすでにあれば、サイトの作り直しだけが行われます）。

## 費用の目安
| 項目 | 無料で使う場合 | 有料枠にした場合 |
|---|---|---|
| Gemini（台本＋音声） | **0円**（無料枠の範囲） | 1回 約$0.2〜0.3 → 月 約$5前後 |
| Google 検索での裏取り | 使えない（記事本文ベースで作成） | 月5,000回まで無料（毎日1回なので実質0円） |
| X API（任意） | ― | 1ポスト$0.005。既定の9アカウントで最大 月$5程度 |
| GitHub | **0円** | ― |

- 無料枠は1日のリクエスト数に上限があります。上限に達すると音声化が失敗し、GitHub からメールで通知されます（テキスト版は公開されます）。
- 有料枠に切り替えると、Google 検索による事実確認が自動で有効になり、品質が上がります（`models.search_grounding: auto`）。
- 無料枠のデータは Google の品質改善に使われる場合があります（扱うのは公開ニュースのみ）。

## 注意事項
- 個人の学習・情報収集用です。台本は AI が公開情報から作成した要約で、**誤りを含む可能性があります**。重要な判断の前には出典リンクで一次情報を確認してください。
- サイトは検索エンジンに載らない設定（noindex・Podcast ディレクトリ非掲載）ですが、URL を知っている人は閲覧できます。
- CEO 名は `config.yaml` に書いてあります。人事異動があったら更新してください（台本自体は記事に書かれた肩書きを優先します）。

## トラブルシューティング
| 症状 | 対処 |
|---|---|
| Actions が赤くなる | 失敗したジョブを開き、ログの `ERROR` 行を確認。`GEMINI_API_KEY` の登録ミスが最も多い原因です |
| 「音声の生成に失敗」 | 無料枠の上限の可能性。翌日には回復します。**Run workflow** で `force` にチェックして再作成も可 |
| Pages が 404 | 手順4（Source = GitHub Actions）を確認し、もう一度 Run workflow |
| モデル名のエラー | Google がモデルを更新した可能性。`config.yaml` の `models` を最新名に変更 |
| push 権限エラー | Settings → Actions → General → Workflow permissions を「Read and write」に |

## 開発者向け
```bash
pip install -r requirements.txt
python scripts/run_daily.py --mock      # API を使わずに通し動作確認（サンプル台本＋ダミー音声）
python scripts/build_site.py            # _site/ にアプリと feed.xml を生成
python tests/ui_check.py                # ヘッドレスブラウザで UI テスト（要 playwright）
```
