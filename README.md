> **Project Status: Migration to ComfyUI Complete**
> This project has been successfully migrated from Replicate API to a self-hosted ComfyUI backend. The documentation below is preserved for historical context, but the live codebase uses ComfyUI.
> For details on the new architecture, see **[MIGRATION_SUMMARY.md](./MIGRATION_SUMMARY.md)**.

# Nano Banana Bot v3.0 (Archived Documentation)

Telegram-бот для AI-редактирования изображений с помощью Google Replicate.
Оплата через YooKassa, кредитная система, очередь задач, полный аудит операций.

---

## Что нового в v3.0

- **Экран «Тарифы»** — отдельная кнопка в меню с красивым отображением цен и кнопками пополнения
- **Глобальный admin-free guard** — единый модуль `shared/admin_guard.py`, админам не списываются кредиты
- **Реальная отмена генерации** — `/cancel` останавливает задачу в очереди или при обработке, кредиты возвращаются
- **Тикет-система поддержки** — каждое обращение получает `ticket_id`, ответ по `/reply_TICKET_ID`
- **Рассылка** — `/broadcast <текст>` для массовой отправки сообщений
- **Проверка ENV на старте** — обязательные переменные проверяются при запуске, понятные ошибки
- **Health check без 307** — `redirect_slashes=False` в FastAPI

---

## Архитектура

```
FastAPI (Uvicorn)
├── /health (и /health/)        — Health check (GET, без 307-редиректа)
├── /webhook/telegram/{secret}  — Telegram webhook
├── /yookassa/webhook/{secret} — YooKassa webhook (секрет в URL)
├── Queue Worker                — Фоновый обработчик генераций
└── Redis                       — FSM, rate-limit, очередь, блокировки
```

**Стек:** Python 3.11, FastAPI, SQLAlchemy async + asyncpg, Redis, python-telegram-bot v21.

---

## Быстрый старт (Railway)

> **Note:** These instructions are for the original Replicate version. For the new ComfyUI version, please see **[DEPLOYMENT_GUIDE.md](./DEPLOYMENT_GUIDE.md)**.

### 1. Подготовка

1. Создайте бота через [@BotFather](https://t.me/BotFather) и получите токен.
2. Получите API-ключ [Google Replicate](https://aistudio.google.com/apikey).
3. Зарегистрируйте магазин в [YooKassa](https://yookassa.ru/) и получите `shop_id` + `secret_key`.

### 2. Деплой на Railway

1. Создайте проект на [Railway](https://railway.app/).
2. Добавьте сервисы **PostgreSQL** и **Redis** из маркетплейса.
3. Создайте сервис из GitHub-репозитория (или загрузите код).
4. Установите переменные окружения (см. `.env.example`):

| Переменная | Описание | Обязательная |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен бота от @BotFather | **Да** |
| `DATABASE_URL` | PostgreSQL connection string | **Да** |
| `REDIS_URL` | Redis connection string | **Да** |
| `TELEGRAM_WEBHOOK_URL` | Публичный URL приложения | Рекомендуется |
| `TELEGRAM_WEBHOOK_SECRET` | Секрет для верификации вебхуков Telegram | Рекомендуется |
| `REPLICATE_API_TOKEN` | API-ключ Google Replicate | Для генерации |
| `YOOKASSA_SHOP_ID` | ID магазина YooKassa | Для платежей |
| `YOOKASSA_SECRET_KEY` | Секретный ключ YooKassa | Для платежей |
| `YOOKASSA_WEBHOOK_SECRET` | Секрет для URL вебхука YooKassa | **Да** (для платежей) |
| `ADMIN_IDS` | Telegram ID администраторов через запятую | Нет |
| `DB_POOL_SIZE` | Размер пула подключений к БД (по умолчанию 3) | Нет |
| `DB_MAX_OVERFLOW` | Макс. дополнительных подключений (по умолчанию 2) | Нет |
| `PORT` | Порт сервера (Railway задаёт автоматически) | Нет |

5. Railway автоматически соберёт Docker-образ и запустит приложение.

### 3. Проверка обязательных ENV

При старте бот проверяет наличие **обязательных** переменных:
- `TELEGRAM_BOT_TOKEN`
- `DATABASE_URL`
- `REDIS_URL`

Если чего-то не хватает — приложение падает с понятной ошибкой в логах.

Опциональные переменные (`REPLICATE_API_TOKEN`, `YOOKASSA_SHOP_ID` и т.д.) — предупреждение в лог, но запуск продолжается.

### 4. Настройка вебхуков YooKassa

В личном кабинете YooKassa → Настройки → HTTP-уведомления:
- URL: `https://ваш-домен.up.railway.app/yookassa/webhook/{YOOKASSA_WEBHOOK_SECRET}`
  (замените `{YOOKASSA_WEBHOOK_SECRET}` на значение из ENV)
- События: `payment.succeeded`

### 5. Проверка

- Health check: `GET https://ваш-домен.up.railway.app/health`
- Откройте бота в Telegram и отправьте `/start`

---

## Тарифы и кредиты

### Стоимость генерации

| Тариф | Стоимость |
|---|---|
| Nano Banana | 5 ₽ (5 кредитов) |
| Nano Banana Pro | 11 ₽ (11 кредитов) |

> 1 ₽ = 1 кредит

### Пакеты пополнения

| Сумма | Кредиты |
|---|---|
| 100₽ | 100 кредитов |
| 200₽ | 200 кредитов |
| 300₽ | 300 кредитов |
| 500₽ | 500 кредитов |

### Реферальная программа

Пригласи друга — оба получат по **5 кредитов**.

---

## Безопасность

- **Telegram webhook:** проверка заголовка `X-Telegram-Bot-Api-Secret-Token`
- **YooKassa webhook:** секрет в URL (`/yookassa/webhook/{secret}`), верификация платежа через API SDK, проверка суммы, идемпотентность по `payment_id`
- **Rate-limit:** 5 команд/мин, 2 медиа/мин через Redis
- **1 активная генерация** на пользователя одновременно
- **Идемпотентное списание** кредитов по `request_id` — защита от дублей при ретраях
- **Credit Ledger** — полный аудит всех операций с кредитами
- **Единый обработчик ошибок** с `trace_id` — пользователю безопасное сообщение, в лог — полный стектрейс
- **Admin-free guard** — единая точка проверки прав и списания кредитов
- **Тикет-система поддержки** — ответ привязан к `ticket_id`, а не к пользователю

---

## Структура проекта

```
nano_banana_v3/
├── bot_api/
│   ├── handlers/
│   │   ├── start.py        — /start, /help, /balance, меню, тарифы
│   │   ├── generate.py     — Генерация с выбором тарифа
│   │   ├── topup.py        — Пополнение баланса
│   │   ├── cancel.py       — Реальная отмена задач
│   │   ├── referral.py     — Реферальная программа
│   │   ├── support.py      — Поддержка с ticket_id
│   │   └── admin.py        — Админ-команды + /broadcast
│   ├── webhooks/
│   │   ├── telegram.py     — Telegram webhook
│   │   └── yookassa.py     — YooKassa webhook
│   ├── keyboards.py        — Inline-клавиатуры
│   ├── bot.py              — Инициализация бота
│   └── main.py             — FastAPI приложение
├── services/
│   ├── user_service.py     — Логика пользователей
│   ├── generation_service.py — Логика генераций
│   ├── payment_service.py  — Логика платежей
│   ├── ledger_service.py   — Credit Ledger
│   ├── replicate_client.py    — Replicate API клиент
│   └── queue_worker.py     — Фоновый обработчик очереди
├── shared/
│   ├── config.py           — Конфигурация из ENV + проверка
│   ├── database.py         — SQLAlchemy модели
│   ├── redis_client.py     — Redis: FSM, rate-limit, очередь
│   ├── admin_guard.py      — Глобальный admin-free guard
│   └── errors.py           — Обработка ошибок
├── Dockerfile
├── railway.json
├── requirements.txt
├── .env.example
└── README.md
```

---

## Команды бота

| Команда | Описание |
|---|---|
| `/start` | Главное меню |
| `/help` | Справка |
| `/balance` | Баланс и тарифы |
| `/cancel` | Реальная отмена текущей генерации |
| `/stats` | Статистика (админ) |
| `/addadmin <id>` | Назначить админа (админ) |
| `/removeadmin <id>` | Снять админа (админ) |
| `/ban <id>` | Заблокировать пользователя (админ) |
| `/unban <id>` | Разблокировать пользователя (админ) |
| `/broadcast <текст>` | Рассылка всем пользователям (админ) |
| `/reply_TICKET_ID <текст>` | Ответ на тикет поддержки (админ) |

---

## Локальная разработка

```bash
# Установить зависимости
pip install -r requirements.txt

# Скопировать и заполнить .env
cp .env.example .env

# Запустить (нужны PostgreSQL и Redis)
uvicorn bot_api.main:app --host 0.0.0.0 --port 8080 --reload
```

Для локального тестирования вебхуков используйте [ngrok](https://ngrok.com/):
```bash
ngrok http 8080
```
