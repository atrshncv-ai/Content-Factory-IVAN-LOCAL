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
│ orchestrator_sender.py   │                 │ node1_watchdog.py          │
│   пишет job_{id}.json ──►│ data/jobs       │  (embedded python ComfyUI) │
│ job_server.py :8899 ◄────┼── GET/DELETE ──►│  pull каждые 2 сек         │
│   (0.0.0.0, stdlib)      │  HTTP через     │     │                      │
│ tailscale serve --bg ────┼── tailscaled ──►│     ▼                      │
│   tcp://:8899→127.0.0.1  │  (демон, мимо   │ ComfyUI --listen 127.0.0.1 │
└──────────────────────────┘   маршрутов ОС)  │  :8188/prompt              │
                                               └────────────────────────────┘
```

Ключевые свойства: **pull-модель** (Node 1 сам забирает задачи исходящим соединением —
брандмауэр Windows ему не мешает), доставка через **tailscale serve** (демон-уровень,
не зависит от таблицы маршрутов ОС и от VPN-клиентов), ComfyUI на **стабильном loopback**.

Файлы в репо: `core/orchestrator_sender.py`, `scripts/job_server.py`, `scripts/node1_watchdog.py`.

## 1. Пререквизиты

- На обоих ПК установлен Tailscale (GUI), оба включены.
- На Node 1: ComfyUI portable с `python_embeded\python.exe` внутри.
- На Node 2: этот репозиторий, `.venv`, запущенный `scripts/job_server.py`.

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
  action=allow protocol=TCP localport=<port>` для 22/445/8188 (cmd от Администратора,
  каждая → «ОК»).

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
3. **Наше спасение, которое всегда работает:** pull-модель через `tailscale serve`
   (шаг 5) — трафик идёт через демона tailscaled, мимо таблицы маршрутов ОС.
4. В `scripts/node1_watchdog.py` уже встроен direct-opener без системного прокси
   (`urllib.request.build_opener(urllib.request.ProxyHandler({}))`) — не удаляй это.

## 5. Node 2: job-server + tailscale serve

```bash
cd <repo> && .venv/bin/python scripts/job_server.py   # слушает 0.0.0.0:8899, раздаёт data/jobs
tailscale serve --bg --tcp=8899 tcp://localhost:8899  # постоянный конфиг демона (НЕ foreground!)
```

Проверка: `curl http://<IP_Node2>:8899/` → `[]`. Foreground-serve (`tailscale serve --tcp=...`
без `--bg`) умирает по таймауту/сессии — только `--bg`.

## 6. Node 1: ComfyUI на loopback + watchdog

1. ComfyUI (из папки portable):
   ```bat
   .\python_embeded\python.exe -s ComfyUI\main.py --listen 127.0.0.1
   ```
   Только `127.0.0.1`! Биндинг `0.0.0.0`/`--listen` на Windows ломает accept-цикл asyncio
   при пересоздании сетевых адаптеров (Tailscale/VPN): `[ERROR] Accept failed on a socket,
   WinError 64` → рендер идёт, а новые подключения refused (WinError 10061).
2. Watchdog (системный python на ПК клиента может быть заглушкой MS Store —
   **всегда используй embedded python ComfyUI**):
   ```bat
   C:\Confy_UI\...\python_embeded\python.exe node1_watchdog.py
   ```
   Дефолты: `NODE2_URL=http://<IP_Node2>:8899`, `COMFYUI_URL=http://127.0.0.1:8188/prompt`.
   POST timeout=180с (занятой сервер отвечает медленно, но принимает — короткий таймаут
   даёт ДУБЛИ задач), бракованные job-файлы (битый JSON, HTTP 4xx) удаляются автоматически.

## 7. E2E-проверка

```bash
# Node 2:
python -m core.orchestrator_sender test_01 "test prompt"
curl http://<IP_Node2>:8899/        # ["job_test_01.json"], через ~2 сек → []
```
На Node 1 в watchdog: `OK job_test_01.json: HTTP 200`. В консоли ComfyUI: `got prompt`
и загрузка моделей. Готово.

## 8. Диагностическое дерево (когда что-то не так)

| Симптом | Причина | Проверка | Фикс |
|---|---|---|---|
| `no matching peer` (tailscale ping) | Разные tailnet'ы | `tailscale status` обеих | auth key, шаг 2 |
| ping есть, TCP по портам timeout | Tailscale ACL / firewall | `Test-NetConnection <свой IP> -Port N` на самом узле | локально True → ACL tailnet'а; False → firewall, шаг 3 |
| ping есть, TCP timeout, firewall чист | VPN-клиент перехватил маршрут | `route -n get <IP>` / `route print` | шаг 4 (serve вместо прямого TCP) |
| `127.0.0.1` «виснет» / ERR_INVALID_RESPONSE | Системный прокси VPN | браузер + curl на 127.0.0.1 | direct-opener в watchdog (уже есть) |
| `WinError 10061` при живом ComfyUI | accept-цикл сломан (WinError 64 в консоли) | хвост консоли ComfyUI | перезапуск с `--listen 127.0.0.1` |
| Дубли задач в ComfyUI (got prompt ×N) | Короткий POST timeout | — | watchdog из git (timeout 180) |
| Задача «ядом» крутится вечно | Битый JSON в очереди | лог watchdog | авто-удаление в watchdog из git |
| `python` печатает только «Python» | Заглушка MS Store | `python --version` | embedded python ComfyUI |
| Команды «не найдены»/склеены | Вставка многострочного блока в cmd | — | по ОДНОЙ строке, без приглашения `C:\...>` |

## 9. Антипаттерны (НЕ делать)

- ❌ Node sharing между разными tailnet'ами (односторонний, ACL фильтрует порты).
- ❌ Tailscale Funnel как транспорт (публичный URL, релей нестабилен, NXDOMAIN-кэш).
- ❌ Taildrop между узлами разных владельцев (`peer is owned by a different user`).
- ❌ SMB по Tailscale поверх Windows Firewall (дни боли; pull-модель проще и надёжнее).
- ❌ Foreground `tailscale serve` (умрёт) — только `--bg`.
- ❌ `--listen`/`0.0.0.0` для ComfyUI на Windows (accept-цикл умирает от churn адаптеров).
- ❌ Хардкод auth-ключей/паролей в git; `git add .` с `.crt`/`.key` в корне
  (проверяй `git status`, в `.gitignore` есть `*.key`, `*.crt`, `.env`, `setup.bat`).
- ❌ Короткий POST timeout в watchdog (дубли задач).
- ❌ Бойлерплейт-фреймворки (FastAPI и т.п.) — stdlib `http.server` достаточно (Ponytail).

## 10. Чеклист готовности нового узла

- [ ] Оба ПК в одном tailnet'е, взаимный `tailscale ping` работает.
- [ ] Firewall Node 1: `AllowInboundRules=True`, Tailscale-интерфейс исключён.
- [ ] VPN-клиенты: TUN не перехватывает `100.64/10`, прокси не ломает `127.0.0.1`.
- [ ] Node 2: `curl http://<IP>:8899/` → `[]`; serve `--bg` в `tailscale serve status`.
- [ ] Node 1: ComfyUI на `127.0.0.1:8188`, watchdog через embedded python запущен.
- [ ] E2E: тестовый job прошёл `OK ... HTTP 200`, очередь `[]`, в ComfyUI `got prompt`.
