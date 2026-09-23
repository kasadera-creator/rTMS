# Stage 9-8: Django Host / Environment Configuration Audit

## 1. 調査範囲と結論

調査日時: 2026-09-02

今回の調査では、コード・データベース・migration・サーバーを変更していない。
現在のHEADは `19c7830dceac55963fb2b3bd3f1a4df7609a3c0c` で、作業開始時の既存dirtyファイルは12件だった。

主な結論:

- `config.settings` は単一の `settings.py` ではなく、`config/settings/__init__.py` が `dev.py` または `prod.py` を選択する構成である。
- ローカルの `DJANGO_ENV` が空または未設定で、`RENDER` も未設定なら dev.py が選択される。
- `dev.py` は base.py の `ALLOWED_HOSTS` を完全に上書きするため、base.pyだけにhostを追加してもdev/runserverには反映されない。
- `CSRF_TRUSTED_ORIGINS`、`SECURE_PROXY_SSL_HEADER`、`USE_X_FORWARDED_HOST` は現在定義されていない。
- `DJANGO_ALLOWED_HOSTS` が空文字として環境に存在すると、base.pyの `os.environ.get()` はデフォルトへフォールバックせず、空文字を `.split(',')` するため `['']` になる。
- `DJANGO_SECRET_KEY` が空文字として存在すると、`env()` は空文字をそのまま返し、固定のdev fallbackにも到達しない。
- 現在のローカル `.env` は値を表示せずに確認した。提示された空値状態とは異なり、少なくとも変数の存在状態は実行時環境と一致していないため、CGI側の環境は別途確認が必要である。
- このworkspaceおよび親ディレクトリには `private_html/index.cgi` は存在せず、CGI実装そのものはローカルコードから検証できなかった。

## 2. 現在のsettings構造

### `manage.py`

1. `.env` をプロジェクトルートから読み込む。
2. `DJANGO_SETTINGS_MODULE` に `config.settings` を `setdefault` する。
3. Djangoを起動する。

### `config/settings/__init__.py`

1. `.env` を再度読み込む。`override=False` なので、既に設定されたOS環境変数は上書きしない。
2. `RENDER` が存在する、または `DJANGO_ENV == 'prod'` の場合は `prod.py` をimportする。
3. それ以外は `dev.py` をimportする。

### `config/settings/base.py`

共通settingsを定義する。DB、middleware、installed apps、logging、SECRET_KEY、DEBUGなどを含む。
`ALLOWED_HOSTS` は `DJANGO_ALLOWED_HOSTS` から読み込むが、dev.pyの後続代入でdev環境では上書きされる。

### `config/settings/dev.py`

`DEBUG = True` を設定し、`ALLOWED_HOSTS` を次の固定リストで上書きする。

- `127.0.0.1`
- `localhost`
- `rtms.local`
- `seichiryo.jp`
- `www.seichiryo.jp`

現在のdev.pyには `rtms.lan` と `192.168.100.50` がない。

### `config/settings/prod.py`

base.pyをimportし、`RENDER_EXTERNAL_HOSTNAME` があればbase.pyの `ALLOWED_HOSTS` に追加する。
prod.pyはdev.pyのような固定host上書きをしない。

## 3. 環境変数の読み込み経路

| 起動経路 | `.env`読み込み | settings選択 |
|---|---|---|
| `manage.py` | manage.pyで読み込み、その後 `config.settings` でも読み込み | `DJANGO_ENV` / `RENDER` による |
| runserver | manage.py経由。親シェルの環境変数が優先 | 通常はdev.py |
| WSGI | `config/wsgi.py`で読み込み、その後 `config.settings` でも読み込み | `DJANGO_ENV` / `RENDER` による |
| ASGI | ASGI自身は読み込まないが、settings import時に `config.settings` が読み込む | `DJANGO_ENV` / `RENDER` による |
| CGI | ユーザー説明ではCGI側が先に読み込み、その後 `config.settings` が読み込む | CGIの環境変数に依存 |

`.env`読み込みは複数箇所にあるが、通常はpython-dotenvの既定動作と `override=False` により同じ値を上書きしない。とはいえ、起動経路ごとの差を減らすため、将来は読み込み責務を一箇所に寄せる方が明確である。

`DJANGO_SETTINGS_MODULE` はmanage.py、wsgi.py、asgi.pyで `setdefault('config.settings')` される。外部環境で別moduleが既に設定されていれば、`setdefault`はそれを変更しない。

## 4. ALLOWED_HOSTSの現在の問題

### 実効値

今回のローカルmanagement commandの実効settingsはdev.py由来であり、`rtms.lan` と `192.168.100.50` が不足していた。

base.pyのデフォルト文字列は、少なくとも次の5経路を共通のfallbackとして持つ設計が望ましい。

- `rtms.lan`
- `seichiryo.jp`
- `192.168.100.50`
- `localhost`
- `127.0.0.1`

IPアドレスを `ALLOWED_HOSTS` に指定すること自体は適切である。DjangoのHostヘッダ検証は、IPアドレスでアクセスされた場合にもその値を許可リストと照合するため、LANの固定IPアクセスには必要である。これはDNSを設定することではなく、許可するHostヘッダを明示するだけである。

### 空文字の問題

現在のコード:

```python
os.environ.get("DJANGO_ALLOWED_HOSTS", "...").split(",")
```

`DJANGO_ALLOWED_HOSTS=` が存在すると戻り値はデフォルト文字列ではなく `''` となり、結果は `['']` になる。prodでは有効なhostを失い、`DisallowedHost`の原因になる。

また、値に空白や重複がある場合も、そのままhost要素として残る。

## 5. CSRF_TRUSTED_ORIGINSの現在の問題

コード上、`CSRF_TRUSTED_ORIGINS` は未定義である。現在のDjango既定値は空リストである。

HTTPSの `seichiryo.jp` からPOSTを行う運用では、reverse proxyやブラウザのOrigin検証条件によっては、少なくとも `https://seichiryo.jp` をtrusted originにする必要がある。`www.seichiryo.jp` を実際に使うなら、それも別originとして検討する。

LAN経路については、実際のschemeを確認してから最小限追加する。

- HTTPの `rtms.lan` を使うだけなら `http://rtms.lan`
- HTTPS終端を行うなら `https://rtms.lan`
- IPでHTTPSアクセスするなら `https://192.168.100.50`
- HTTPのIPアクセスだけなら `http://192.168.100.50`

HTTPとHTTPSは別originなので、両方を実際に提供する場合だけ両方を追加する。`localhost`と`127.0.0.1`も、HTTPS POSTをブラウザで行う実運用がある場合に限って追加を検討する。全schemeを無条件に追加するのは避ける。

CSRF trusted originはHost許可とは別設定であり、`ALLOWED_HOSTS`に追加しただけではCSRF Origin検証を通過しない。

## 6. HTTPS / proxy関連settings

調査対象コードには以下の設定がなかった。

- `SECURE_PROXY_SSL_HEADER`
- `USE_X_FORWARDED_HOST`
- `SECURE_SSL_REDIRECT`
- `CSRF_COOKIE_SECURE`
- `SESSION_COOKIE_SECURE`

CGIがHTTPSを直接受けるのか、前段proxyがHTTPSを終端してCGIへHTTPで渡すのかで必要設定が変わる。proxy終端の場合、実際のwebサーバーが信頼できる転送ヘッダを正しく付与・上書きしていることを確認せずに `SECURE_PROXY_SSL_HEADER` を追加してはいけない。誤設定するとHTTPをHTTPSと誤認できる。

## 7. 空文字の環境変数

### `DJANGO_ENV`

`os.environ.get('DJANGO_ENV', '')` は空文字を返し、`_django_env == 'prod'` はfalseになる。そのため `RENDER` もなければdev.pyが選択される。空文字は「開発環境」の暗黙値になっている。

### `DJANGO_DEBUG`

`env_bool()` は空文字をlowerしても許可値に一致しないためfalseになる。ただしdev.pyが後から `DEBUG = True` を設定するため、dev選択時の実効DEBUGはTrueである。

### `DJANGO_ALLOWED_HOSTS`

空文字はデフォルトへのfallbackにならず、`['']` になる。空値は未設定と同じ扱いに正規化すべきである。

### `DJANGO_SECRET_KEY`

空文字は `env()` のfallbackに到達せず、空文字のSECRET_KEYになる。これは安全ではない。

本番でdev用固定キーをfallbackとして使うべきではない。今回、自動生成や再生成は行わないが、実装時は次のいずれかを明示する必要がある。

- 本番settingsで、空または未設定なら起動失敗にする。
- 本番デプロイ環境で、空でないsecretを必須設定として供給する。
- devだけは固定fallbackを許容し、本番では許容しない。

既存secretの値を変更・再生成せず、CGIとrunserverが同じ環境変数名 `DJANGO_SECRET_KEY` を使う方針にする。

## 8. CGIとrunserverの設定差

ユーザー説明では、`seichiryo.jp`のCGIが`.env`を読み込んだ後に `DJANGO_SETTINGS_MODULE=config.settings` を設定する。

この場合、CGI側の読み込みとwsgi/settings側の読み込みが重複する。`override=False`なら先に設定されたCGI環境変数が優先されるため、CGIの値とプロジェクトルート`.env`の値が異なっていても、後の読み込みで修正されない。

runserverは通常、manage.pyがプロジェクトルート`.env`を読み込む。CGIは配置場所・cwd・実行ユーザーが異なる可能性があり、同じ相対パスを見ているとは限らない。CGI実ファイルがworkspaceにないため、次の点はサーバー上で確認が必要である。

- CGIの実行cwd
- `.env`の絶対パス
- `DJANGO_ENV`の設定値が空か `prod`か
- CGIが使用するPython interpreter / virtualenv
- `DJANGO_ALLOWED_HOSTS`の空白・区切り・設定有無
- `DATABASE_URL`の有無（値そのものは報告しない）

二重loadは直ちに値を破壊する構造ではないが、起動経路ごとに設定源が変わる原因になる。

## 9. 共存させる推奨設計

実装案は次の順序にする。

1. 共通のhost parserをbase.pyに置く。カンマ区切りをtrimし、空要素を除外し、未設定または空値なら安全な共通fallbackを使う。
2. 共通fallbackに `rtms.lan`、`seichiryo.jp`、`192.168.100.50`、`localhost`、`127.0.0.1` を含める。
3. dev.pyで固定hostを完全上書きしない。base.pyの正規化済みリストを使い、必要ならdev固有の追加だけにする。
4. prod.pyではbase.pyのhostを使い、Render等の動的hostnameだけを追加する。
5. `CSRF_TRUSTED_ORIGINS` はhost名から自動的に全schemeを生成せず、実際の公開schemeを環境変数で明示する。最低限、HTTPSの本番公開が確定しているなら `https://seichiryo.jp` を設定する。
6. `DJANGO_SECRET_KEY` は空値を本番で受け入れず、既存secretをそのまま供給する。今回のscopeでは自動生成しない。
7. CGIとrunserverは同じ絶対`.env`または同じ環境変数供給方式を使い、`.env`のload責務を一箇所へ寄せる。

この設計なら5つのHost経路を共存させられる。ただし、Host許可とHTTPS/CSRFの公開schemeは別問題なので、HTTPを使うLAN経路とHTTPSを使う本番経路を混同しない。

## 10. テスト追加案

現在の `rtms_app/tests.py` には、settings/host関連の専用テストは確認できなかった。追加案:

- 実効 `settings.ALLOWED_HOSTS` に5つのhostが含まれるテスト
- `Client.get(..., HTTP_HOST=host)` を5つのhostで行い、`DisallowedHost`にならないテスト
- `DJANGO_ALLOWED_HOSTS` 未設定・空文字・空白混入・重複のparserテスト
- `CSRF_TRUSTED_ORIGINS` のscheme明示とHTTP/HTTPS分離のテスト
- `DJANGO_ENV` が空ならdev、`prod`ならprodになるsettings選択テスト
- 本番相当でsecretが空なら起動失敗または明示的エラーになるテスト

settings moduleのimport stateを伴うため、テストでは環境変数を一時変更してsettingsを再importする際の分離に注意する。既存の患者・Courseテストとは別クラスまたはsettings専用テストmoduleに分ける方が安全である。

## 11. 変更対象ファイル案

実装を承認する場合の最小候補は次のとおり。

- `config/settings/base.py`: 空値を正規化するhost parser、共通host、CSRF origin読み込み、本番secret検証の共通部
- `config/settings/dev.py`: 固定 `ALLOWED_HOSTS` の完全上書きを廃止または共通値利用へ変更
- `config/settings/prod.py`: 本番用CSRF originとsecret検証、動的host追加の整理
- `config/settings/__init__.py`: `DJANGO_ENV`の空値・許可値の扱いを明示
- `manage.py` / `config/wsgi.py`: `.env`の二重読み込みを整理する場合のみ変更
- `rtms_app/tests.py` または新規settings test: host、空値、scheme、settings選択の回帰テスト

CGIファイルはworkspace外のため、実装時にサーバー側の実ファイルを確認し、別途変更対象とするか判断する。`.env`は変更対象にしない。

## 12. 実装方針と承認ポイント

現時点では実装していない。次のどちらかを選択してからコード変更に進む。

### 「この設計で実装してよい」

上記の最小候補を編集し、次を実装する。

- 5つのhostを共通許可
- 空の `DJANGO_ALLOWED_HOSTS` を共通fallbackへ正規化
- dev.pyによるhostの取りこぼしを解消
- 実際に使用するschemeだけを `CSRF_TRUSTED_ORIGINS` に設定
- 本番の空secretを拒否するが、secretの自動生成・再生成はしない
- CGIとrunserverの設定供給経路を一致させる
- settings/host関連の回帰テストを追加

### 「設計を修正する」

少なくとも次を指定する。

- `seichiryo.jp`、LAN、localhostそれぞれの実際のHTTP/HTTPS scheme
- 本番CGIで `DJANGO_ENV` を `prod` にするかどうか
- 空の本番secretを起動失敗にするか、デプロイ環境側の必須設定だけで担保するか
- `CSRF_TRUSTED_ORIGINS` を環境変数で供給するか、prod.pyに固定するか
