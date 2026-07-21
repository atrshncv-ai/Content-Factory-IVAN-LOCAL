---
name: tailscale-node-link
description: Связка двух ПК (Node 2 мозг / Node 1 ComfyUI) в рабочий конвейер через Tailscale без многочасовой сетевой отладки. Пошаговая методика + диагностическое дерево + антипаттерны. Использовать при подключении любого нового узла (Windows или macOS) к конвейеру задач.
---

# Связка двух ПК в конвейер задач через Tailscale (боевой протокол)

Методика выстрадана на живой интеграции (Node 2 = macOS «мозг», Node 1 = Windows + ComfyUI).
Следуй шагам **строго по порядку** — каждый проверяется перед следующим. Тогда подключение
занимает ~30–60 минут вместо двух дней.

## 0. Итоговая архитектура (картина результата)

```
Node 2 (мозг)                                Node 1 (GPU + ComfyUI)
┌──────────────────────────┐                 ┌────────────────────────────┐
│ core/orchestrator.py     │   push:         │ ComfyUI --listen 127.0.0.1 │
│  cli-anything-comfyui ───┼─ queue prompt ─►│   :8188/prompt             │
│  --url http://IP1:8188   │  (Tailscale)    │     ▲                      │
│ core/webhook.py :8099 ◄──┼── POST /webhook │ tailscale serve :8188 ─────┘
│   on_done → тик Оркестр. │   + файл в      │   (демон, мимо маршрутов)  │
│ cron → run_once          │   Shared Folder │ notifier: завершение ──────┘
└──────────────────────────┘                 └────────────────────────────┘
```

Ключевые свойства: **push-модель** (Node 2 сам ставит задачу в очередь ComfyUI через
`cli-anything-comfyui --url`), доставка до loopback-ComfyUI через **tailscale serve**
(демон-уровень, не зависит от таблицы маршрутов ОС и VPN-клиентов), возврат результата —
**файл в Shared Folder + POST /webhook** на Node 2 (приёмник `core/webhook.py`,
событийный триггер Оркестратора на статус `done`).

Файлы в репо: `core/orchestrator.py` (рендер workflow + push), `core/webhook.py`
(приёмник + триггер), `core/evaluator.py` (оценка; видео — auto-approve).

## 1. Пререквизиты

- На обоих ПК установлен Tailscale (GUI), оба включены.
- На Node 1: ComfyUI portable с `python_embeded\python.exe` внутри.
- На Node 2: этот репозиторий, `.venv` с `cli-anything-comfyui`, запущенный
  `python -m core.webhook` (или webhook поднимется в daemon-потоке при старте Оркестратора).
- В `.env` Node 2: `COMFYUI_SERVER_URL=http://<IP_Node1>:8188`,
  `SHARED_FOLDER=<локальный путь к шарингу>`, `WEBHOOK_PORT=8099`.

## 2. Tailscale: ОДИН tailnet на обе машины

❗ Главный урок: **node sharing между разными tailnet'ами — односторонний и фильтруется ACL.
Не используй его.** Все машины конвейера должны жить в ОДНОМ tailnet'е.

1. Владелец tailnet'а: admin-консоль → Settings → Keys → **Generate auth key**
   (reusable: по желанию; one-time умирает после первого `up`).
2. На подключаемом ПК (cmd/PowerShell):
   ```bat
   tailscale up --authkey=tskey-auth-XXXX --unattended
   ```
   `logout` перед этим НЕ нужен — `up` сам переключает tailnet. Одноразовый ключ
   расходуется ПЕРВОЙ попыткой: не делай `logout` после успеха и не повторяй `up`
   с тем же ключом — получишь «invalid key».
3. Проверка на подключаемом ПК:
   ```bat
   tailscale status
   tailscale ping <IP_другого_узла>
   ```
   Обе машины должны видеть друг друга в `tailscale status` обеих сторон. Ключевое
   сообщение `no matching peer` = машины в разных tailnet'ах → возврат к шагу 2.
4. **Ключ — секрет.** После входа: reusable-ключ отзови в консоли, one-time — умрёт сам.
   Никогда не коммить в git.

## 3. Windows Firewall на Node 1 (PowerShell от Администратора)

При входе в новый tailnet адаптер Tailscale пересоздаётся, старые правила слетают.
Выполни блоком (вставлять в окно с приглашением `PS`, не в cmd!):

```powershell
Set-NetFirewallProfile -Profile Public -AllowInboundRules True
Set-NetFirewallProfile -Profile Private -AllowInboundRules True
Set-NetFirewallProfile -Profile Public,Private -DisabledInterfaceAliases "Tailscale"
```

- `AllowInboundRules=True` снимает режим «блокировать ВСЕ входящие» (он игнорирует
  allow-правила — классическая ловушка «правила добавлены, ОК×3, а порт закрыт»).
- `DisabledInterfaceAliases "Tailscale"` исключает интерфейс из фильтрации целиком —
  самое надёжное, tailnet и так приватен.
- Альтернатива точечно: `netsh advfirewall firewall add rule name="TS-<port>" dir=in
  action=allow protocol=TCP localport=<port>` для 8188 (cmd от Администратора,
  каждая → «ОК»). На Node 2 (macOS) аналогично нужен вход на 8099 (webhook) —
  проверь, что брандмауэр macOS пропускает Python.

## 4. VPN-клиенты (Happ/v2ray/Clash) — главный скрытый враг

Если на ЛЮБОЙ из машин стоит VPN-клиент в TUN-режиме — он может:
- перехватить маршрут `100.64.0.0/10` (диапазон Tailscale) в свой туннель/роутер
  → TCP до Tailscale-узлов умирает, при этом `tailscale ping` работает (демон ходит мимо ОС);
- его **системный прокси** перехватывает даже `127.0.0.1` → локальные API «висят»/
  `ERR_INVALID_RESPONSE`.

Диагностика на macOS/Linux:
```bash
route -n get <IP_узла>            # interface должен быть utunX Tailscale, не чужой
ifconfig | grep -A2 utun          # чужой VPN: inet 198.18.x.x (fake-ip sing-box/clash)
scutil --nc list                  # список VPN-сервисов
```
На Windows: `route print`, `Get-NetConnectionProfile`.

Фикс по приоритету:
1. В настройках VPN-клиента — режим **System Proxy** вместо TUN (не трогает маршруты).
2. Или исключение `100.64.0.0/10` из его маршрутизации.
3. **Наше спасение, которое всегда работает:** `tailscale serve` на Node 1
   (шаг 5) — трафик идёт через демона tailscaled, мимо таблицы маршрутов ОС.
4. Если системный прокси мешает процессам Node 2 — запускай их с чистым окружением
   (`env -u http_proxy -u https_proxy -u all_proxy ...`).

## 5. Node 1: ComfyUI на loopback + tailscale serve

1. ComfyUI (из папки portable):
   ```bat
   .\python_embeded\python.exe -s ComfyUI\main.py --listen 127.0.0.1
   ```
   Только `127.0.0.1`! Биндинг `0.0.0.0`/`--listen` на Windows ломает accept-цикл asyncio
   при пересоздании сетевых адаптеров (Tailscale/VPN): `[ERROR] Accept failed on a socket,
   WinError 64` → рендер идёт, а новые подключения refused (WinError 10061).
2. Наружу ComfyUI отдаёт tailscaled (демон-уровень, устойчив к churn адаптеров):
   ```bat
   tailscale serve --bg --tcp=8188 tcp://localhost:8188
   ```
   Foreground-serve (без `--bg`) умирает по таймауту/сессии — только `--bg`.
   Проверка с Node 2: `curl http://<IP_Node1>:8188/system_stats`.
3. **Notifier возврата результата** (живёт на Node 1, вне этого репо): по завершении
   генерации копирует файл из output-директории ComfyUI в Shared Folder и шлёт
   `POST http://<IP_Node2>:8099/webhook` с `{"job_id","status":"done","filename"}`.
   ComfyUI сам webhooks не шлёт — без notifier'а задание зависнет в `generating`.

## 6. Node 2: webhook-приёмник + Оркестратор

```bash
cd <repo> && .venv/bin/python -m core.webhook   # слушает 0.0.0.0:8099, триггерит Оркестратор
```

- Постоянный процесс: при статусе `done` пишет `media_jobs` и запускает
  `run_once(trigger_job_id=...)` в отдельном потоке (ответ Node 1 не блокируется).
- Оркестратор по cron: `.venv/bin/python -m core.orchestrator` — один тик доски.
  При старте он тоже пытается поднять webhook в daemon-потоке (best-effort:
  занятый порт — просто warning в логах).
- Push задачи: `.venv/bin/python -m core.orchestrator` сам вызовет
  `cli-anything-comfyui --url <COMFYUI_SERVER_URL> queue prompt --workflow PATH`
  для заданий в `ready_for_render`.

## 7. E2E-проверка

```bash
# Node 2: постановка в обход Оркестратора (прямой контракт CLI):
.venv/bin/cli-anything-comfyui --url http://<IP_Node1>:8188 queue prompt --workflow /path/to/wf.json
.venv/bin/cli-anything-comfyui --url http://<IP_Node1>:8188 queue history   # факт рендера

# Node 2: возвратный путь (эмуляция webhook от Node 1):
mkdir -p shared && printf 'fake' > shared/test_e2e.png
curl -X POST http://localhost:8099/webhook -H 'Content-Type: application/json' \
  -d '{"job_id":"test-e2e","status":"done","filename":"test_e2e.png"}'
sqlite3 data/factory.db "SELECT job_id, status, file_path FROM media_jobs WHERE job_id='test-e2e';"
# ожидание: status=done, file_path заполнен; в logs — тик Оркестратора (evaluated)
```

## 8. Диагностическое дерево (когда что-то не так)

| Симптом | Причина | Проверка | Фикс |
|---|---|---|---|
| `no matching peer` (tailscale ping) | Разные tailnet'ы | `tailscale status` обеих | auth key, шаг 2 |
| ping есть, TCP по портам timeout | Tailscale ACL / firewall | `Test-NetConnection <свой IP> -Port N` на самом узле | локально True → ACL tailnet'а; False → firewall, шаг 3 |
| ping есть, TCP timeout, firewall чист | VPN-клиент перехватил маршрут | `route -n get <IP>` / `route print` | шаг 4 (serve вместо прямого TCP) |
| `127.0.0.1` «виснет» / ERR_INVALID_RESPONSE | Системный прокси VPN | браузер + curl на 127.0.0.1 | шаг 4 (чистое env-окружение) |
| `WinError 10061` при живом ComfyUI | accept-цикл сломан (WinError 64 в консоли) | хвост консоли ComfyUI | перезапуск с `--listen 127.0.0.1` + serve |
| CLI уходит на localhost вместо Node 1 | забыт `--url` | строка команды в логах | `COMFYUI_SERVER_URL` в `.env`, оркестратор сам подставляет |
| `cli-anything-comfyui: command not found` | нет в PATH | `which cli-anything-comfyui` | ставить в `.venv`, в `.env` полный путь в `COMFYUI_COMMAND` |
| Задание висит в `generating` вечно | webhook не пришёл | `sqlite3 data/factory.db "SELECT * FROM logs WHERE agent='webhook'"` | webhook-приёмник не запущен / notifier на Node 1 молчит (шаг 5.3) |
| `file_not_found` в ответе webhook | файла нет в Shared Folder | `ls $SHARED_FOLDER` | notifier не скопировал файл / неверный `SHARED_FOLDER` |
| `python` печатает только «Python» | Заглушка MS Store | `python --version` | embedded python ComfyUI |
| Команды «не найдены»/склеены | Вставка многострочного блока в cmd | — | по ОДНОЙ строке, без приглашения `C:\...>` |

## 9. Антипаттерны (НЕ делать)

- ❌ Node sharing между разными tailnet'ами (односторонний, ACL фильтрует порты).
- ❌ Tailscale Funnel как транспорт (публичный URL, релей нестабилен, NXDOMAIN-кэш).
- ❌ Taildrop между узлами разных владельцев (`peer is owned by a different user`).
- ❌ SMB по Tailscale поверх Windows Firewall без исключения интерфейса (дни боли; шаг 3).
- ❌ Foreground `tailscale serve` (умрёт) — только `--bg`.
- ❌ `--listen`/`0.0.0.0` для ComfyUI на Windows (accept-цикл умирает от churn адаптеров).
- ❌ Хардкод auth-ключей/паролей в git; `git add .` с `.crt`/`.key` в корне
  (проверяй `git status`, в `.gitignore` есть `*.key`, `*.crt`, `.env`, `setup.bat`).
- ❌ Бойлерплейт-фреймворки (FastAPI и т.п.) — stdlib `http.server` достаточно (Ponytail).

## 10. Чеклист готовности нового узла

- [ ] Оба ПК в одном tailnet'е, взаимный `tailscale ping` работает.
- [ ] Firewall Node 1: `AllowInboundRules=True`, Tailscale-интерфейс исключён;
      на Node 2 разрешён вход на 8099.
- [ ] VPN-клиенты: TUN не перехватывает `100.64/10`, прокси не ломает `127.0.0.1`.
- [ ] Node 1: ComfyUI на `127.0.0.1:8188`, `tailscale serve --bg --tcp=8188` активен;
      с Node 2 `curl http://<IP_Node1>:8188/system_stats` отвечает.
- [ ] Node 1: notifier шлёт файл в Shared Folder + POST /webhook по завершении.
- [ ] Node 2: `python -m core.webhook` запущен; `.env`: `COMFYUI_SERVER_URL`, `SHARED_FOLDER`.
- [ ] E2E: `queue history` показывает рендер; тестовый webhook → `done` + `file_path` в БД.
