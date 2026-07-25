# Valuation Ratio Dashboard

미국·한국 주식 티커를 입력하면 최근 3년간의 주요 밸류에이션 비율(PER, PBR, PSR, PEG, EV/EBITDA, 배당수익률, FCF수익률) 추이를 차트와 표로 보여주는 Flask 웹 대시보드입니다.

데이터는 [yfinance](https://github.com/ranaroussi/yfinance)에서 가져오고, 원본 재무 수치를 SQLite에 캐시해 반복 조회 시 네트워크 호출을 최소화합니다.

## 주요 기능

- **티커 조회**: `AAPL`, `MSFT` 등 yfinance가 지원하는 티커를 입력해 조회
- **한글 종목명 · 6자리 코드 자동 변환**: `삼성전자`, `에코프로비엠`처럼 한글로 입력하거나 `005930`처럼 6자리 코드만 입력하면 yfinance 티커(`005930.KS` / `247540.KQ`)로 자동 변환. 코스피는 `.KS`, 코스닥은 `.KQ`를 시장 구분에 따라 자동으로 붙임 (매핑 데이터: FinanceDataReader / KRX)
- **7가지 밸류에이션 비율** 시각화
  | 지표 | 의미 | 계산식 |
  |------|------|--------|
  | PER | 주가수익비율 | 시가총액 / 순이익 |
  | PBR | 주가순자산비율 | 시가총액 / 자기자본 |
  | PSR | 주가매출비율 | 시가총액 / 매출 |
  | PEG | PER 대비 이익성장률 | PER / EPS 성장률(%) |
  | EV/EBITDA | 기업가치 / EBITDA | (시총 + 순부채) / EBITDA |
  | Dividend Yield | 배당수익률(%) | 최근 12개월 배당 / 주가 |
  | FCF Yield | 잉여현금흐름 수익률(%) | FCF / 시가총액 |
- **의존성 없는 순수 SVG 차트**: 프론트엔드는 외부 라이브러리 없이 바닐라 JS로 라인 차트를 그림
- **SQLite 캐싱**: 원본 재무 수치(raw)만 저장하고 비율은 매번 재계산 → 계산식이 바뀌어도 캐시 재활용 가능
- **TTM 이력 누적**: 분기가 지날 때마다 새로운 TTM(최근 12개월) 스냅샷이 DB에 쌓여, 시간이 지날수록 분기별 이력이 촘촘해짐

## 요구 사항

- Python 3.10+ (개발 환경 기준 3.13)
- 인터넷 연결 (yfinance 최초 조회 시)

## 설치

```bash
git clone <repository-url>
cd valuation-ratio-dashboard

python -m venv .venv
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

## 실행

### 로컬 개발 (기본)

```bash
python app.py
```

- 기본 주소: <http://127.0.0.1:5000>
- 디버그 모드가 켜진 상태로 실행됩니다.

환경 변수로 동작을 조정할 수 있습니다.

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `HOST` | `127.0.0.1` | 바인딩할 호스트 |
| `PORT` | `5000` | 포트 |
| `FLASK_DEBUG` | `1` | `1`이면 디버그 모드 |

### 네트워크 공개 실행

같은 네트워크의 다른 PC에서 접근하려면 `run.sh`를 사용합니다. (`0.0.0.0`으로 바인딩하고 디버그 모드를 끕니다.)

```bash
./run.sh
```

> ⚠️ **보안 주의**: Flask 디버거를 외부에 노출하면 원격 코드 실행 위험이 있습니다.
> 외부 접근용으로 띄울 때는 반드시 디버그 모드를 끄세요(`run.sh`는 자동으로 꺼짐).

### CLI로 확인

웹 서버 없이 특정 티커/종목명의 결과를 터미널에서 바로 볼 수 있습니다.

```bash
python ratios.py AAPL
python ratios.py 삼성전자   # 한글명도 가능 -> 005930.KS 로 자동 변환
```

## API

### `GET /api/ratios/<ticker>`

지정한 티커의 비율 이력을 JSON으로 반환합니다. `<ticker>` 자리에는 영문 티커뿐 아니라 **한글 종목명**(`삼성전자`)이나 **6자리 코드**(`005930`)도 넣을 수 있으며, 서버가 yfinance 티커로 변환합니다.

**응답 예시**

```json
{
  "ticker": "005930.KS",
  "display": "삼성전자 (005930)",
  "dates": ["2023-12-31", "2024-12-31", "2025-12-31"],
  "metrics": {
    "PER": [30.9, 9.1, 17.9],
    "PBR": [1.5, 1.0, 1.9],
    "PSR": [1.3, 1.1, 2.2],
    "PEG": [null, 0.4, null],
    "EV_EBITDA": [7.7, 3.3, 7.5],
    "DividendYield": [1.9, 2.7, 1.3],
    "FCFYield": [-3.6, 6.2, 4.1]
  },
  "cache_info": { "annual": "cache", "ttm": "fetched" }
}
```

- `ticker`는 변환된 yfinance 티커, `display`는 사람이 읽기 좋은 이름(한글명이 아니면 티커와 동일)입니다.
- 값이 계산 불가(적자, 데이터 부족 등)면 `null`로 반환됩니다.
- `cache_info`의 각 값은 `cache`(캐시 사용) / `fetched`(새로 조회) / `cache(stale)`(조회 실패로 오래된 캐시 폴백) 중 하나입니다.
- 종목명이 정확히 일치하지 않으면 `400`과 함께 후보 목록을 담은 `{"error": "..."}`를 반환합니다.

## 프로젝트 구조

```
valuation-ratio-dashboard/
├── app.py              # Flask 앱 · 라우팅 · JSON 직렬화
├── ratios.py           # 재무 스냅샷 → 밸류에이션 비율 계산 로직 (핵심)
├── tickers.py          # 한글 종목명/6자리 코드 → yfinance 티커 변환
├── db.py               # SQLite 캐시 레이어 (재무 수치 + 종목명 매핑)
├── requirements.txt    # 파이썬 의존성
├── run.sh              # 외부 공개용 실행 스크립트 (0.0.0.0, 디버그 off)
├── cache.sqlite3       # SQLite 캐시 DB (자동 생성)
├── templates/
│   └── index.html      # 대시보드 페이지
└── static/
    ├── main.js         # SVG 차트 렌더링 · API 호출
    └── style.css       # 스타일
```

## 동작 방식

1. **캐시 확인** — `ratios.get_ratio_history()`가 먼저 SQLite 캐시와 TTL을 확인합니다.
   - 연간 재무제표(annual): 30일 TTL
   - 분기 TTM: 24시간 TTL
2. **필요한 부분만 조회** — TTL이 지난 종류만 yfinance에서 새로 받아 원본 수치를 upsert합니다. 조회에 실패해도 캐시가 있으면 그 값으로 폴백합니다.
3. **비율 재계산** — 캐시된 원본 수치로부터 매번 비율을 계산합니다. 원본만 저장하므로 계산식이 바뀌어도 데이터를 다시 받을 필요가 없습니다.
4. **응답** — 최근 `years`년(기본 3년) 구간만 잘라 프론트엔드로 전달합니다.

### 종목명 변환 (`tickers.py`)

- 입력에 한글이 있거나 6자리 숫자면, KRX 상장 목록에서 코드·시장을 찾아 yfinance 티커로 변환합니다.
- 상장 목록은 FinanceDataReader로 받아 SQLite(`stock_map`)에 캐시하며 **7일 TTL**을 둡니다. 처음 한글 조회가 필요할 때 한 번만 내려받고, 이후에는 네트워크 호출 없이 재사용합니다.
- 한글명은 **정확 일치**를 우선합니다(예: `삼성전자` ≠ `삼성전자우`). 정확히 일치하는 게 없으면 부분일치 후보를 에러 메시지로 안내합니다.

### 데이터에 대한 참고

- yfinance 무료 소스는 연간 재무제표 4~5개, 분기 재무제표 4~5개 정도만 제공합니다. 따라서 초기에는 데이터 포인트가 성깁니다.
- 앱을 계속 사용하면 새 분기 TTM 스냅샷이 DB에 누적되어 시간이 지날수록 이력이 촘촘해집니다.
- PEG는 EPS가 역성장하거나 적자인 구간에서는 의미가 없어 `null`로 처리됩니다.

## 라이선스

별도 명시가 없습니다. 개인·학습 용도로 사용하세요.
