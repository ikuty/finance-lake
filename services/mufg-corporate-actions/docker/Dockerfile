# kabu.comの対象3ページは実データがJavaScript(document.write方式)で描画されて
# おり、単純なHTTP GETでは空のシェルHTMLしか取得できない(2026-09-16実機確認)。
# レンダリング結果（最終的な<table>を含む完成後のHTML）を保存する方針とし、
# 描画プログラム（document.writeの内部実装）の構造に依存しないようにする。
# Playwright公式イメージを使う（Chromiumのシステム依存ライブラリを手動で揃える
# より確実。pipパッケージとブラウザ本体のバージョンを一致させる必要があるため、
# requirements.txtのplaywrightバージョンとイメージタグは必ず揃えること）。
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scripts/fetch_corporate_actions.py .

ENTRYPOINT ["python3", "fetch_corporate_actions.py"]
