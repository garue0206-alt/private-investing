# GitHub Actions + Telegram 시장 스크리너 자동화

업로드한 5개 노트북을 GitHub Actions에서 자동 실행하고, 실행 요약과 생성된 CSV·JSON·로그를 Telegram 봇으로 전송하는 저장소입니다.

## 포함된 5개 작업

| Job ID | 원본 파일 | 실행용 파일 |
|---|---|---|
| `leader_all` | 주도주_ALL_IN_ONE_최종 (1).ipynb | `notebooks/01_leader_all_in_one.ipynb` |
| `alt_bull` | 코인시장 불장 스크리너 | `notebooks/02_alt_bull_fire_alarm.ipynb` |
| `news` | 뉴스 (1).ipynb | `notebooks/03_market_news.ipynb` |
| `leader_collection` | 주도주 | `notebooks/04_leader_strategy_collection.ipynb` |
| `crypto_collection` | 코인 | `notebooks/05_crypto_strategy_collection.ipynb` |

원본 계산 조건은 노트북 안에 그대로 보존했습니다. 실행 직전 다음 항목만 자동 변환합니다.

- Colab용 `!pip install` 제거: GitHub Actions의 `requirements.txt`에서 한 번만 설치
- `/content/...` 경로를 `state/`로 변경
- `tqdm.notebook`을 headless 환경에서도 동작하는 `tqdm.auto`로 변경
- 각 노트북을 별도 폴더·별도 커널에서 실행

## 안정성 설계

- 노트북별 전체 제한시간과 셀 제한시간
- 한 노트북이 실패해도 다음 노트북 계속 실행
- 셀 오류가 있어도 가능한 출력과 오류 traceback 수집
- `runner.log`, `notebook_output.txt`, 실행 완료 노트북, CSV·JSON 보존
- Telegram 메시지 4,096자 제한에 맞춰 자동 분할
- Telegram 429·일시 통신오류 재시도
- 50MB를 넘는 파일은 전송하지 않고 오류로 기록
- OBV 캐시와 알트불장 점수 기록을 Actions cache로 복원·저장
- 중복 실행 방지용 `concurrency`
- 결과는 GitHub Actions Artifact에도 30일 보관

> 외부 데이터 제공처(CoinGecko, Binance, Google News RSS, KRX/FDR 등)가 차단되거나 응답 형식을 바꾸면 데이터 수집은 실패할 수 있습니다. 이 저장소는 그런 실패를 숨기지 않고 Telegram과 GitHub 로그에 명확히 표시하도록 만든 것입니다.

---

# 1. Telegram 봇 만들기

1. Telegram에서 `@BotFather`를 엽니다.
2. `/newbot`을 입력합니다.
3. 봇 이름과 사용자 이름을 정합니다.
4. BotFather가 발급한 토큰을 복사합니다.
5. 새로 만든 봇 채팅방에 들어가 `/start`를 한 번 보냅니다.

채널이나 그룹으로 보내려면 봇을 해당 채널·그룹에 추가하고 메시지 전송 권한을 부여해야 합니다.

## Chat ID 확인

로컬 컴퓨터에서 다음처럼 실행할 수 있습니다.

```bash
# Windows PowerShell
$env:TELEGRAM_BOT_TOKEN="발급받은_토큰"
python scripts/get_chat_id.py
```

```bash
# macOS / Linux
export TELEGRAM_BOT_TOKEN="발급받은_토큰"
python scripts/get_chat_id.py
```

출력 예시:

```text
TELEGRAM_CHAT_ID=123456789  # SEONGHYUN
```

그룹 Chat ID는 보통 음수이며, 채널은 `@channelusername`을 사용할 수도 있습니다.

---

# 2. GitHub 저장소에 올리기

1. GitHub에서 새 **Private repository**를 만듭니다.
2. 이 폴더 안의 파일을 전부 저장소 루트에 업로드합니다.
3. 기본 브랜치는 `main`으로 둡니다.

명령어로 올리는 경우:

```bash
git init
git add .
git commit -m "Add market screener automation"
git branch -M main
git remote add origin https://github.com/사용자명/저장소명.git
git push -u origin main
```

---

# 3. GitHub Secrets 등록

저장소에서 다음 메뉴로 이동합니다.

`Settings → Secrets and variables → Actions → New repository secret`

아래 2개를 등록합니다.

| Secret 이름 | 값 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather가 준 전체 토큰 |
| `TELEGRAM_CHAT_ID` | 개인·그룹 Chat ID 또는 `@channelusername` |

토큰을 코드, README, 공개 로그에 직접 적지 마십시오.

---

# 4. Telegram 연결 테스트

1. GitHub 저장소의 `Actions` 탭을 엽니다.
2. `Test Telegram Connection`을 선택합니다.
3. `Run workflow`를 누릅니다.
4. Telegram에 `연결 테스트 성공` 메시지가 오면 설정 완료입니다.

실패하는 대표 원인:

- 봇 채팅에서 `/start`를 보내지 않음
- Chat ID 오타
- 그룹·채널에서 봇의 전송 권한 없음
- Secret 이름 오타

---

# 5. 5개 전체 실행

1. `Actions` 탭에서 `Run 5 Market Screeners`를 선택합니다.
2. `Run workflow`를 누릅니다.
3. `jobs`는 기본값 `all`을 유지합니다.
4. CSV·로그까지 받을 경우 `send_documents`를 체크합니다.

특정 작업만 실행할 수도 있습니다.

```text
leader_all
alt_bull
news
leader_collection
crypto_collection
```

여러 개를 실행할 때:

```text
alt_bull,news,crypto_collection
```

## 자동 실행 시각

기본 설정은 **한국시간 평일 16:35**입니다.

파일:

```text
.github/workflows/run-all.yml
```

현재 설정:

```yaml
schedule:
  - cron: "35 16 * * 1-5"
    timezone: "Asia/Seoul"
```

GitHub Actions 예약 실행은 서버 혼잡으로 몇 분 늦어질 수 있습니다. 정각 혼잡을 피하려고 35분으로 설정했습니다.

---

# 결과 확인

Telegram에는 작업별로 다음 정보가 옵니다.

- `SUCCESS`, `PARTIAL`, `FAILED`, `TIMEOUT`
- 핵심 시장 판정 및 후보 요약
- 실행시간과 셀 오류 개수
- 생성된 CSV·JSON
- 실패한 경우 `notebook_output.txt`와 `runner.log`

GitHub에서는:

`Actions → 해당 실행 → Artifacts → market-screeners-실행번호`

에서 전체 결과를 내려받을 수 있습니다.

결과 구조:

```text
outputs/
└── YYYYMMDD_HHMMSS_KST/
    ├── leader_all/
    ├── alt_bull/
    ├── news/
    ├── leader_collection/
    ├── crypto_collection/
    └── run_summary.json
```

---

# 로컬 테스트

Python 3.11 권장:

```bash
python -m venv .venv
```

Windows PowerShell:

```bash
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m ipykernel install --user --name python3 --display-name "Python 3"
python scripts/validate_repo.py
python -m unittest discover -s tests -v
```

macOS / Linux:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m ipykernel install --user --name python3 --display-name "Python 3"
python scripts/validate_repo.py
python -m unittest discover -s tests -v
```

Telegram 없이 실행 결과만 확인:

```bash
python -m src.main --jobs news --no-telegram
```

전체 실행:

```bash
python -m src.main --jobs all --no-telegram
```

---

# 노트북 교체 방법

동일한 역할의 노트북을 새 버전으로 바꿀 때는 `notebooks/`의 해당 파일을 덮어쓰면 됩니다. 파일명이나 Job ID를 바꾸려면 `config/jobs.json`도 함께 수정하십시오.

각 Job의 제한시간도 `config/jobs.json`에서 조절할 수 있습니다.

```json
{
  "id": "leader_all",
  "total_timeout_seconds": 3000,
  "cell_timeout_seconds": 900
}
```

- `total_timeout_seconds`: 노트북 전체 제한시간
- `cell_timeout_seconds`: 한 셀의 최대 실행시간

---

# 주의

이 시스템은 시장 데이터를 수집하고 조건을 표시하는 자동화 도구입니다. 자동 주문을 넣지 않으며, 출력은 투자수익을 보장하지 않습니다. 외부 API 장애·거래소 심볼 변경·KRX 데이터 변경·휴장일·장중 미완성 봉에 따라 결과가 달라질 수 있습니다.
