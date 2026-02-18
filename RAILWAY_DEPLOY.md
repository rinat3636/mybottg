# Railway Deployment Guide - Nano Banana Bot v3.1

## Быстрый старт

### 1. Подключение репозитория к Railway

1. Зайдите на [Railway](https://railway.app/)
2. Создайте новый проект или откройте существующий
3. Подключите GitHub репозиторий `rinat3636/mybottg`
4. Railway автоматически обнаружит `Dockerfile` и `railway.json`

### 2. Добавление сервисов

В вашем Railway проекте должны быть 3 сервиса:

1. **PostgreSQL** (из маркетплейса Railway)
2. **Redis** (из маркетплейса Railway)
3. **Web Service** (ваш бот из GitHub)

### 3. Настройка переменных окружения

В настройках вашего Web Service добавьте следующие переменные:

#### Обязательные переменные:

```bash
# Telegram
TELEGRAM_BOT_TOKEN=8093673490:AAE3NnSOWMQNDU6MnajXX6k8zzMOuAah3ec
TELEGRAM_WEBHOOK_URL=https://ваш-домен.up.railway.app
TELEGRAM_WEBHOOK_SECRET=<сгенерируйте случайную строку>

# Database (автоматически из Railway PostgreSQL)
DATABASE_URL=${{Postgres.DATABASE_URL}}

# Redis (автоматически из Railway Redis)
REDIS_URL=${{Redis.REDIS_URL}}

# Replicate API
REPLICATE_API_TOKEN=<ваш токен Replicate>
```

#### Для платежей (опционально):

```bash
YOOKASSA_SHOP_ID=<ваш shop ID>
YOOKASSA_SECRET_KEY=<ваш секретный ключ>
YOOKASSA_WEBHOOK_SECRET=<секрет для webhook URL>
```

#### Для администраторов:

```bash
ADMIN_IDS=<ваш Telegram ID через запятую>
```

### 4. Генерация секретов

Для генерации `TELEGRAM_WEBHOOK_SECRET` и `YOOKASSA_WEBHOOK_SECRET`:

```bash
openssl rand -hex 32
```

Или используйте любой генератор случайных строк.

### 5. Деплой

После настройки переменных:
1. Railway автоматически начнет деплой
2. Дождитесь завершения (обычно 2-3 минуты)
3. Проверьте логи на наличие ошибок

### 6. Проверка деплоя

#### Проверка health endpoint:

```bash
curl https://ваш-домен.up.railway.app/health
```

Должен вернуть: `{"status":"ok"}`

#### Проверка webhook Telegram:

```bash
curl https://api.telegram.org/bot8093673490:AAE3NnSOWMQNDU6MnajXX6k8zzMOuAah3ec/getWebhookInfo
```

Должно быть:
- `url`: правильный URL с вашим секретом
- `pending_update_count`: 0
- `last_error_date`: 0 (или отсутствует)

#### Тест бота:

Откройте бота в Telegram и отправьте `/start`

### 7. Настройка YooKassa webhook (если используете платежи)

В личном кабинете YooKassa → Настройки → HTTP-уведомления:
- URL: `https://ваш-домен.up.railway.app/yookassa/webhook/{YOOKASSA_WEBHOOK_SECRET}`
- События: `payment.succeeded`

## Решение проблем

### Бот не отвечает на команды

1. **Проверьте логи Railway:**
   - Есть ли сообщения о получении webhook?
   - Есть ли ошибки при обработке?

2. **Проверьте webhook:**
   ```bash
   curl https://api.telegram.org/bot<TOKEN>/getWebhookInfo
   ```
   
   Если есть ошибки (`last_error_date` не равен 0):
   - Проверьте, что `TELEGRAM_WEBHOOK_SECRET` установлен правильно
   - Проверьте, что сервер доступен извне

3. **Проверьте переменные окружения:**
   - Все обязательные переменные установлены?
   - `DATABASE_URL` и `REDIS_URL` правильные?

### Ошибки при генерации изображений

1. **Проверьте `REPLICATE_API_TOKEN`:**
   - Токен установлен?
   - Токен действителен?
   - Есть ли баланс на аккаунте Replicate?

2. **Проверьте логи:**
   - Ищите сообщения от `services.replicate_client`
   - Ищите ошибки типа "authentication failed" или "insufficient credits"

### Ошибки базы данных

1. **Проверьте `DATABASE_URL`:**
   ```bash
   # В Railway переменные должны быть так:
   DATABASE_URL=${{Postgres.DATABASE_URL}}
   ```

2. **Проверьте подключение PostgreSQL:**
   - Сервис PostgreSQL запущен?
   - Есть ли ошибки в логах PostgreSQL?

### Ошибки Redis

1. **Проверьте `REDIS_URL`:**
   ```bash
   # В Railway переменные должны быть так:
   REDIS_URL=${{Redis.REDIS_URL}}
   ```

2. **Если используется SSL:**
   ```bash
   REDIS_SSL=true
   # или используйте rediss:// в URL
   ```

## Мониторинг

### Важные метрики в логах:

- `Starting up...` - приложение запускается
- `Database tables ready` - база данных инициализирована
- `Redis connected` - Redis подключен
- `Bot initialized` - бот создан
- `Webhook set: <URL>` - webhook установлен
- `Queue worker started` - обработчик очереди запущен

### Проверка статуса:

```bash
# Health check
curl https://ваш-домен.up.railway.app/health

# Webhook info
curl https://api.telegram.org/bot<TOKEN>/getWebhookInfo

# Логи Railway
railway logs
```

## Обновление бота

1. Внесите изменения в код
2. Закоммитьте и запушьте в GitHub:
   ```bash
   git add .
   git commit -m "Update: описание изменений"
   git push
   ```
3. Railway автоматически задеплоит новую версию
4. Проверьте логи после деплоя

## Полезные команды

### Локальная проверка:

```bash
# Проверка синтаксиса
python3 -m py_compile bot_api/main.py

# Запуск локально (нужны PostgreSQL и Redis)
uvicorn bot_api.main:app --host 0.0.0.0 --port 8080 --reload
```

### Railway CLI:

```bash
# Установка
npm install -g @railway/cli

# Логин
railway login

# Просмотр логов
railway logs

# Переменные окружения
railway variables
```

## Контакты

При проблемах с деплоем предоставьте:
1. Полные логи из Railway (последние 100 строк)
2. Результат `getWebhookInfo`
3. Список установленных переменных окружения (без значений секретов)
