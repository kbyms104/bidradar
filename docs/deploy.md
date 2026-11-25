# 배포 구성 (pm2 + nginx + Let's Encrypt)

현재 운영 중인 구성입니다. 서버: Ubuntu 24.04, 도메인 `bidradar-kr.duckdns.org` (DuckDNS, A 레코드 → 서버 IP).

## 1. 앱 (pm2)

```bash
cd ~/bidradar
pm2 start server.py --name bidradar --interpreter python3 --cwd /home/ubuntu/bidradar --time
pm2 save
```

- 앱은 `127.0.0.1:8790`에만 바인딩하고, 외부 노출은 nginx가 담당합니다.
- 재시작/로그:

```bash
pm2 restart bidradar
pm2 logs bidradar --lines 50
```

서버를 재부팅해도 살아나도록 `pm2 startup`이 설정되어 있어야 합니다 (`systemctl is-enabled pm2-ubuntu`).

## 2. nginx

`/etc/nginx/sites-available/bidradar` → `sites-enabled` 심볼릭 링크.

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name bidradar-kr.duckdns.org;

    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name bidradar-kr.duckdns.org;

    ssl_certificate /etc/letsencrypt/live/bidradar-kr.duckdns.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bidradar-kr.duckdns.org/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header X-Frame-Options "SAMEORIGIN" always;

    client_max_body_size 1m;
    access_log /var/log/nginx/bidradar.access.log;
    error_log /var/log/nginx/bidradar.error.log;

    location / {
        proxy_pass http://127.0.0.1:8790;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_read_timeout 60s;
        proxy_send_timeout 60s;
    }
}
```

적용:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## 3. TLS

```bash
sudo certbot --nginx -d bidradar-kr.duckdns.org --non-interactive --agree-tos --redirect
sudo certbot renew --dry-run
```

인증서는 90일마다 자동 갱신되며, `systemctl list-timers | grep certbot`으로 타이머를 확인할 수 있습니다.

> certbot은 80 포트 블록의 `server_name`을 기준으로 챌린지를 처리합니다.
> 설정을 직접 수정한 뒤에는 반드시 `nginx -t`와 `certbot renew --dry-run`을 함께 확인하세요.

## 4. 확인

```bash
curl -sI http://bidradar-kr.duckdns.org/            # 301 → https
curl -s  https://bidradar-kr.duckdns.org/api/health # {"ok": true, ...}
curl -s -o /dev/null -w "%{http_code}\n" https://bidradar-kr.duckdns.org/demo        # 200
curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" https://bidradar-kr.duckdns.org/app  # 302 /login
curl -s -o /dev/null -w "%{http_code}\n" -u admin:$BIDRADAR_ADMIN_PASSWORD \
  https://bidradar-kr.duckdns.org/admin                                               # 200
curl -s -X POST https://bidradar-kr.duckdns.org/api/lead \
  -H 'Content-Type: application/json' \
  -d '{"company":"테스트","name":"테스트","contact":"test@example.com","consent":true}'
python3 ~/bidradar/leads.py
```

## 5. 환경변수 (.env)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `BIDRADAR_SOURCE` | `demo` | `live`면 `data/live.db`(나라장터 실데이터) 사용 |
| `DATA_GO_KR_KEY` | — | 공공데이터포털 인증키 |
| `BIDRADAR_HOST` / `BIDRADAR_PORT` | `127.0.0.1` / `8790` | 앱 바인딩 |
| `BIDRADAR_APP_DB` | `data/app.db` | 고객 계정·조건·발송 이력 DB |
| `BIDRADAR_SESSION_DAYS` | `30` | 로그인 유지 기간(일) |
| `BIDRADAR_SIGNUP` | `invite` | `invite`(초대 코드 필요) / `open`(공개 가입) |
| `BIDRADAR_ADMIN_PASSWORD` | — | `/admin` Basic 인증 비밀번호. 비우면 `/admin` 404 |
| `BIDRADAR_CONTACT_EMAIL` | — | 약관·방침에 공개할 문의 이메일. 비우면 상담 폼 안내 |
| `BIDRADAR_BID_ALERTS` | — | `1`이면 발송 파이프라인 가동 표시(고객 LIVE는 발송 성공 이력까지 확인) |
| `DISCORD_WEBHOOK_URL` | — | 운영자 상담·파일럿 채널 (한도·트래픽 경고도 이곳) |
| `BIDRADAR_API_DAILY_LIMIT` | `1000` | 공공데이터 API 일일 한도 |
| `BIDRADAR_API_RESERVE` | `100` | 수동 작업용 예비 호출 |
| `BIDRADAR_TRAFFIC_ALERT_REQ` | `300` | 시간당 요청 경고 기준 |
| `BIDRADAR_TRAFFIC_ALERT_IP` | `150` | 단일 IP 시간당 경고 기준 |
| `BIDRADAR_TRAFFIC_ALERT_MB` | `500` | 시간당 전송량 경고 기준(MB) |
| `BIDRADAR_SELF_IPS` | 자동 감지 | 급증 집계에서 뺄 서버 공인 IP(쉼표 구분) |

환경변수를 바꾼 뒤에는 반드시 `pm2 restart bidradar --update-env`로 재시작해야 반영됩니다.

```bash
cd ~/bidradar
grep BIDRADAR_ADMIN_PASSWORD .env   # 관리자 비밀번호 확인 (화면에 붙여넣지 마세요)
pm2 restart bidradar --update-env
pm2 logs bidradar --lines 30
```

## 5-1. 고객 계정 운영

```bash
cd ~/bidradar
python3 provision.py list
python3 provision.py invite --note "파일럿 A사"
python3 provision.py add --email boss@acme.co.kr --company "에이스테크" --name "김대표"
python3 provision.py show --email boss@acme.co.kr
```

초대 코드는 1회용입니다. 고객이 `/login` → **시작하기**에서 코드를 입력하면 계정이 생기고
곧바로 `/app/settings`로 이동합니다. 계정을 잠시 막으려면 `--status paused`.

## 6. 실데이터 수집 (나라장터)

`collector.py`는 `data/live.db`에만 쓰고, 서버는 `BIDRADAR_SOURCE=live`일 때 그 DB를 읽습니다. 데모 DB는 건드리지 않습니다.

인증키 발급: <https://www.data.go.kr/data/15129394/openapi.do> (`조달청_나라장터 입찰공고정보서비스`) → 활용신청.
API 주소는 `https://apis.data.go.kr/1230000/ad/BidPublicInfoService02`이고, 조회기간은 최대 7일입니다.
정확히 7일은 API가 거부하므로 코드에서는 6일로 제한합니다.

```bash
cd ~/bidradar
cp .env.example .env      # DATA_GO_KR_KEY=... 입력
python3 collector.py --days 6 --limit 3000   # 최초 백필
# .env 에 BIDRADAR_SOURCE=live 추가 후
pm2 restart bidradar --update-env
curl -s localhost:8790/api/health
```

cron (live 전환 후, 15분마다 최근 30분 증분 수집 · `flock`으로 중복 실행 방지):

```cron
*/15 * * * * cd /home/ubuntu/bidradar && /usr/bin/flock -n /tmp/bidradar-collector.lock /usr/bin/python3 collector.py --minutes 30 --limit 200 >> /home/ubuntu/bidradar/logs/collector.log 2>&1
5 * * * * cd /home/ubuntu/bidradar && /usr/bin/python3 traffic.py >> /home/ubuntu/bidradar/logs/traffic.log 2>&1
```

되돌리기: `.env`에서 `BIDRADAR_SOURCE=demo`로 바꾸고 `pm2 restart bidradar`.

## 7. 상담 알림

`.env`의 `DISCORD_WEBHOOK_URL`(권장) 또는 `TELEGRAM_BOT_TOKEN`+`TELEGRAM_CHAT_ID`를 채우면 새 상담 신청이 즉시 옵니다.
디스코드가 설정되어 있으면 디스코드를 우선 사용합니다.
`python3 notify.py`로 테스트할 수 있고, 미설정이면 `data/notifications.log`에만 남습니다.

## 8. 고객 공고 알림 (D-3 / D-1)

`alerts.py`가 가입 고객(`data/app.db`)의 조건에 맞는 공고를 골라 디스코드 웹훅으로 보냅니다.
계정이 하나도 없으면 기존 `alerts.json` 파일 모드로 자동 전환합니다.
같은 고객·조건·공고·단계는 두 번 보내지 않고, 결과는 고객 화면의 **알림 이력**과 `alert_log`에 남습니다.

```bash
cd ~/bidradar
python3 alerts.py --preview                       # DB 자동 선택
python3 alerts.py --send --all                    # 활성 고객 전체
python3 alerts.py --test --customer acc1-c1       # 특정 고객·조건 웹훅 확인
python3 alerts.py --source file --preview         # alerts.json 강제
```

발송 후 `python3 provision.py show --email 고객메일`로 성공·실패 이력을 확인할 수 있습니다.

cron (09:00, 17:00 KST — 서버가 UTC이므로 `0 0,8`):

```cron
0 0,8 * * * cd /home/ubuntu/bidradar && /usr/bin/python3 alerts.py --send --all >> /home/ubuntu/bidradar/logs/alerts.log 2>&1
```

발송이 실제로 돌고 있을 때만 `.env`에 `BIDRADAR_BID_ALERTS=1`을 둡니다.
이 값이 없으면 대시보드 알림 패널은 `PREVIEW`로 표시됩니다. (거짓 LIVE 방지)

## 9. 화면이 비어 보일 때 확인 순서

1. `sudo tail -5 /var/log/nginx/bidradar.access.log` — 요청이 왔는지
   (요청이 없으면 서버·JS 문제가 아니라 링크/네트워크 문제)
2. `python3 clientlog.py --errors` — 브라우저가 보고한 실제 오류
3. `curl -s localhost:8790/demo | grep -c '<article class="bid"'` — 서버 렌더링 확인

`/demo`는 JS가 없어도 공고 15건이 보이도록 서버에서 미리 그립니다.
따라서 "빈 화면"이면 남은 원인은 ① 요청이 서버에 닿지 않음 ② 다른 주소를 보고 있음 둘 중 하나입니다.

## 10. 예전 주소 리다이렉트

`earth-play-guide.duckdns.org`는 `bidradar-kr.duckdns.org`로 301 리다이렉트합니다.
인증서는 별도로 발급되어 있습니다(`certbot certonly --webroot -w /var/www/html -d earth-play-guide.duckdns.org`).

## 11. 한도·트래픽 보호

### API 호출 한도

```bash
cd ~/bidradar
python3 quota.py          # 오늘 사용량
python3 quota.py --reset
```

- 70%를 넘으면 수집이 유형별 1페이지로 자동 절전됩니다.
- 100%면 다음 00:00 KST까지 수집을 멈춥니다. (이미 수집된 공고로 알림은 계속 발송)
- `/admin` 상단에 사용량 막대가 표시됩니다.

### nginx 제한

`/etc/nginx/sites-available/bidradar`에 적용되어 있습니다.

```nginx
limit_req_zone $binary_remote_addr zone=bidradar_req:10m rate=6r/s;
limit_conn_zone $binary_remote_addr zone=bidradar_conn:10m;

location ~ /\.(?!well-known) { access_log off; return 444; }
location ~* ^/(owa|autodiscover|ecp|wp-admin|wp-login\.php|phpmyadmin) { return 444; }

location / {
    limit_req zone=bidradar_req burst=30 nodelay;
    limit_conn bidradar_conn 20;
    proxy_pass http://127.0.0.1:8790;
}
```

정상 사용자는 영향이 없고, 스캐너·과도한 반복 요청만 503 또는 연결 종료됩니다.
변경 후에는 반드시 `sudo nginx -t && sudo systemctl reload nginx`.

### 트래픽 감시

```bash
python3 traffic.py                # 지난 1시간 요약
python3 traffic.py --minutes 10
```

기준을 넘으면 디스코드로 알립니다(1시간 1회). 알림을 받으면:

```bash
tail -100 /var/log/nginx/bidradar.access.log | awk '{print $1}' | sort | uniq -c | sort -rn | head
```

상위 IP가 공격이면 `sudo ufw deny from <IP>` 또는 nginx `deny <IP>;`로 막습니다.

`/admin` 상단에는 **API 사용량**과 **지난 1시간 트래픽**이 항상 표시됩니다.
디스코드 알림을 놓치거나 채널이 스팸으로 지저분해져도 상태는 여기서 확인하세요.

### 운영 알림 웹훅을 교체하지 않는 경우

`DISCORD_WEBHOOK_URL`이 외부에 노출되어도 고객 데이터·계정·서버에는 접근할 수 없습니다.
영향은 **그 채널에 제3자가 글을 쓸 수 있다**는 것뿐입니다.

- 운영 알림과 고객 알림은 분리되어 있습니다. 고객은 각자 자기 웹훅으로 받습니다.
- 경고는 `data/notifications.log`, `logs/traffic.log`와 `/admin`에도 남습니다.
- 스팸이 올라오면 그 메시지를 지우고, 필요할 때만 웹훅을 새로 만들어
  `.env`의 `DISCORD_WEBHOOK_URL`을 바꾼 뒤 `pm2 restart bidradar --update-env` 하면 됩니다.

## 12. 백업

다시 만들 수 있는 데이터(`bids.db`, `live.db`)는 백업하지 않아도 됩니다.
**아래 두 개는 반드시 백업하세요.**

- `data/app.db` — 고객 계정·비밀번호 해시·조건·웹훅·발송 이력
- `data/leads.db` — 랜딩으로 들어온 상담 신청

```bash
cd ~/bidradar
python3 backup.py                  # ~/backup/bidradar/app-YYYY-MM-DD.db, leads-...
python3 backup.py --keep 30        # 30일 보관
```

cron (매일 03:30 KST, 서버 UTC 기준 `30 3`):

```cron
30 3 * * * cd /home/ubuntu/bidradar && /usr/bin/python3 backup.py >> /home/ubuntu/bidradar/logs/backup.log 2>&1
```

`backup.py`는 SQLite 백업 API를 쓰므로 WAL 모드에서도 안전하고, 파일 권한을 `600`으로 저장합니다.

`app.db`에는 디스코드 웹훅 URL과 비밀번호 해시가 들어 있습니다.
백업본은 서버 외부(개인 PC·암호화 저장소)에 두고, 보관 기간을 정해 오래된 파일은 삭제하세요.
