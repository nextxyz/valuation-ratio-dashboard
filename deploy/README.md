# 배포 (systemd)

운영 서버(`oci-free`)에서 실제로 돌고 있는 systemd 유닛 파일입니다.
`/etc/systemd/system/`에 있는 파일과 동일한 내용이며, 서버를 새로 만들 때 그대로 복사해 쓰면 됩니다.

| 파일 | 역할 |
|------|------|
| `valuation-dashboard.service` | 앱 본체. `.venv`의 파이썬으로 `app.py`를 띄우고, 죽으면 5초 뒤 자동 재기동 |
| `valuation-dashboard-restart.timer` | 매주 토 19:00 UTC(= 일요일 04:00 KST) 발화 |
| `valuation-dashboard-restart.service` | 타이머가 실행하는 oneshot. `systemctl restart`만 수행 |

## 설치

경로를 그대로 쓴다는 전제입니다(`/home/ubuntu/workspace/valuation-ratio-dashboard`, 실행 계정 `ubuntu`).
다르면 유닛 파일의 `WorkingDirectory`·`ExecStart`·`User`를 먼저 고치세요.

```bash
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now valuation-dashboard
sudo systemctl enable --now valuation-dashboard-restart.timer
```

## 운영

```bash
# 배포
git pull && sudo systemctl restart valuation-dashboard

# 로그
journalctl -u valuation-dashboard -f

# 주간 재시작 다음 실행 시각 / 이력
systemctl list-timers valuation-dashboard-restart.timer
journalctl -u valuation-dashboard-restart.service
```

## 설계 메모

- **`HOST`/`PORT`를 유닛의 `Environment=`로 준다.** `app.py`가 환경변수를 읽으므로 `run.sh`가 필요 없고, 서버에서 `run.sh`를 로컬 수정할 이유도 사라진다. 예전에 서버의 `run.sh` 로컬 수정 때문에 `git pull`이 거부되고, 이를 `git reset --hard`로 넘기다 그 수정이 유실된 적이 있다.
- **`User=ubuntu`** — `cache.sqlite3`와 `.venv`의 소유자와 맞춘다. root로 돌리면 DB 파일 소유권이 꼬인다.
- **`MALLOC_ARENA_MAX=2`** — 이 앱은 pandas·numpy·yfinance 때문에 import만으로 약 96MB를 쓴다. glibc가 스레드마다 힙 아레나를 잡으며 생기는 단편화를 줄인다.
- **주간 재시작** — 한글 종목 조회 시 지연 import 되는 `FinanceDataReader`가 약 27MB를 물고 프로세스가 끝날 때까지 놓지 않는다. 주 1회 재시작으로 털어낸다.
- **`RuntimeMaxSec=1w` 대신 타이머를 쓴 이유** — `RuntimeMaxSec`은 매주 유닛을 실패 상태로 만들어서, 그 기록이 진짜 장애와 섞인다.
- **`OnCalendar`에 타임존을 안 쓴 이유** — 서버가 UTC이고 systemd 245는 `OnCalendar` 타임존 지정을 지원하지 않는다(252부터 가능).
