# Stage 9-8: Django Host / Environment Configuration Implementation Plan

## 1. Purpose

Djangoを次のHostで安定して利用できるようにする。

- `rtms.lan`
- `seichiryo.jp`
- `192.168.100.50`
- `localhost`
- `127.0.0.1`

空の環境変数が有効な設定値として採用される問題を解消する。

## 2. Scope

対象はworkspace内のsettingsとsettings専用回帰テストに限定する。`.env`、DB、migration、サーバー上のCGI、proxy/HTTPS運用設定は変更しない。

## 3. Settings structure

`config.settings` が `.env` を読み込み、`DJANGO_ENV=prod` または `RENDER` の存在時に `prod.py`、それ以外に `dev.py` を読み込む。共通値は `base.py` に置く。

## 4. ALLOWED_HOSTS

`base.py` に共通fallbackを置き、カンマ区切り値を前後trimする。空要素を除外し、順序を維持して重複を除去する。環境変数が未設定または空文字の場合はfallbackへ戻す。`*` は使用しない。

既存の `rtms.local` と `www.seichiryo.jp` は互換性のためfallbackに維持する。明示された `DJANGO_ALLOWED_HOSTS` は、正常な値であればfallbackの代わりに使用する。

## 5. Development

`dev.py` は `base.py` の `ALLOWED_HOSTS` を上書きしない。既存どおりdev選択時の `DEBUG=True` は維持する。`DJANGO_ENV` が未設定または空の場合もdevを選択する。

## 6. Production

`prod.py` は共通Host設定を利用し、Renderのhostnameだけ必要に応じて追加する。本番のsecretは `DJANGO_SECRET_KEY` を優先し、互換性のため非空の `SECRET_KEY` も受け付ける。両方が未設定または空文字の場合は `ImproperlyConfigured` とし、dev固定値で起動しない。secretの生成・再生成・変更は行わない。

## 7. CSRF_TRUSTED_ORIGINS

Host許可とは分離してscheme付きoriginを扱う。既定で `https://seichiryo.jp` を設定し、`DJANGO_CSRF_TRUSTED_ORIGINS` が非空ならその明示値をtrim・正規化して使用する。LANのHTTP/HTTPS originを自動で全追加せず、実運用に応じて環境変数で指定する。

## 8. Environment selection

`DJANGO_ENV` は空文字または `dev` をdev、`prod`をproductionとして扱う。未知の値はproductionへ黙って切り替えず、明示的な設定エラーとする。`RENDER` が設定されている場合のproduction選択は既存動作を維持する。

## 9. dotenv and CGI

今回のscopeでは `manage.py`、`wsgi.py`、settingsの重複loadを大規模に変更しない。`override=False`による既存の環境変数優先を維持する。`~/domains/seichiryo.jp/private_html/index.cgi` はworkspace外のため変更しない。実行cwd、`.env`絶対パス、interpreter、環境変数供給は本番サーバーで別途確認する。

## 10. Proxy and HTTPS

`SECURE_PROXY_SSL_HEADER`、`USE_X_FORWARDED_HOST`、`SECURE_SSL_REDIRECT`、`CSRF_COOKIE_SECURE`、`SESSION_COOKIE_SECURE` は、CGI前段のHTTPS終端と転送ヘッダを確認するまで追加しない。

## 11. Tests

settings専用テストで、5つの許可Host、未設定/空/正常値、空白、重複のparser動作を確認する。developmentのfallback、productionの正常secretと空secret拒否、dev/prod選択、scheme付きCSRF originを確認する。secretの実値はテスト出力・文書に記録しない。

## 12. Risks, rollback, and approval

主なリスクは、本番環境でsecretまたはCSRF originの供給が不足して起動・POSTが失敗すること、未知の `DJANGO_ENV` が明示エラーになることである。rollbackはStage 9-8のcommitだけをrevertし、既存dirtyファイルには触れない。実装後は `check`、全 `rtms_app` テスト、`git diff --check`、対象ファイル限定のdiff確認を行う。

本計画は承認済み設計に基づくStage 9-8実装記録であり、CGI側の実環境確認は別作業とする。
