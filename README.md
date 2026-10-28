# 한국을 보다 (Korea Insight) — 공개 페이지와 전시 피드

GitHub Pages 로 공개하는 것:

- `privacy.html` 개인정보처리방침 (원본 `privacy-policy.md`, 개발 저장소의 `release/privacy-policy.md`)
- `feed.json`, `programs.json` 앱이 받아 쓰는 전시·체험 피드. GitHub Actions(`.github/workflows/feed.yml`)가 매일 03:10 KST 에 `tools/build_feed.py` 로 새로 만든다.

`site/` 의 파일이 그대로 올라가고, 피드는 그날 새로 만든 것으로 덮어쓴다. 피드 만들기가 실패하면 `site/` 의 마지막 피드가 나간다.

공공데이터포털 키는 저장소 Settings → Secrets and variables → Actions 의 `DATA_GO_KR_KEY` 에 둔다 (코드에 넣지 않는다).
