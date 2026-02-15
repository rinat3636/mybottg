# Nano Banana Bot v3 - Исправления

## Проблемы, которые были исправлены

### 1. **Улучшенное логирование для диагностики**

#### Проблема
Бот не отвечает на команды /start, но в логах нет информации о том, что происходит с вебхуками.

#### Исправление
Добавлено подробное логирование в:
- `bot_api/webhooks/telegram.py` - логирование каждого входящего вебхука
- `bot_api/handlers/start.py` - логирование всех этапов обработки команды /start

Теперь в логах будет видно:
- Получен ли вебхук от Telegram
- Правильный ли секретный токен
- На каком этапе происходит ошибка (парсинг, создание пользователя, отправка сообщения)

### 2. **Исправлена работа с MIME-типами изображений**

#### Проблема
В `services/replicate_client.py` всегда использовался `mime_type="image/jpeg"`, даже если Telegram отправлял PNG, WebP или другой формат. Это могло вызывать ошибки в Replicate API.

#### Исправление
Добавлена функция `_detect_mime_type()`, которая определяет реальный формат изображения по magic numbers:
- PNG: `\x89PNG`
- JPEG: `\xff\xd8\xff`
- WebP: `RIFF...WEBP`
- GIF: `GIF87a` или `GIF89a`

Теперь Replicate получает правильный MIME-тип для каждого изображения.

### 3. **Улучшенная обработка ошибок**

Добавлена дополнительная обработка ошибок при отправке сообщений об ошибках пользователю.

## Что нужно проверить

### 1. Переменные окружения

Убедитесь, что все необходимые переменные установлены в Railway:

**Обязательные:**
```bash
TELEGRAM_BOT_TOKEN=8093673490:AAE3NnSOWMQNDU6MnajXX6k8zzMOuAah3ec
DATABASE_URL=<ваша PostgreSQL строка подключения>
REDIS_URL=<ваша Redis строка подключения>
TELEGRAM_WEBHOOK_URL=https://ваш-домен.up.railway.app
TELEGRAM_WEBHOOK_SECRET=<сгенерируйте случайную строку>
```

**Важно для работы генерации:**
```bash
REPLICATE_API_TOKEN=<ваш ключ Google Replicate API>
```

**Опциональные (для платежей):**
```bash
YOOKASSA_SHOP_ID=<ваш shop ID>
YOOKASSA_SECRET_KEY=<ваш секретный ключ>
YOOKASSA_WEBHOOK_SECRET=<секрет для вебхуков>
```

**Для администраторов:**
```bash
ADMIN_IDS=123456789,987654321  # через запятую
```

### 2. Проверка вебхука

После деплоя проверьте, что вебхук установлен правильно:

```bash
curl https://api.telegram.org/bot8093673490:AAE3NnSOWMQNDU6MnajXX6k8zzMOuAah3ec/getWebhookInfo
```

Должно быть:
```json
{
  "url": "https://ваш-домен.up.railway.app/webhook/telegram/ваш_секрет",
  "has_custom_certificate": false,
  "pending_update_count": 0,
  "last_error_date": 0
}
```

Если `last_error_date` не равен 0 или есть ошибки - проверьте:
1. Доступен ли ваш сервер извне
2. Правильно ли установлен `TELEGRAM_WEBHOOK_SECRET`
3. Совпадает ли секрет в заголовке `X-Telegram-Bot-Api-Secret-Token`

### 3. Проверка логов

После деплоя нажмите /start в боте и сразу проверьте логи Railway. Вы должны увидеть:

```
[INFO] bot_api.webhooks.telegram: trace_id=XXXXX | Received webhook request
[INFO] bot_api.webhooks.telegram: trace_id=XXXXX | Secret header present: True
[INFO] bot_api.webhooks.telegram: trace_id=XXXXX | Webhook data received: {...}
[INFO] bot_api.webhooks.telegram: trace_id=XXXXX | Update parsed, processing...
[INFO] bot_api.handlers.start: trace_id=XXXXX | start_command called for user 123456789
[INFO] bot_api.handlers.start: trace_id=XXXXX | Creating/fetching user 123456789
[INFO] bot_api.handlers.start: trace_id=XXXXX | User fetched, created=True
[INFO] bot_api.handlers.start: trace_id=XXXXX | Sending welcome message
[INFO] bot_api.handlers.start: trace_id=XXXXX | Welcome message sent successfully
[INFO] bot_api.webhooks.telegram: trace_id=XXXXX | Update processed successfully
```

Если этих логов нет - вебхуки не доходят до вашего сервера.

## Возможные причины, почему бот не отвечает

### 1. Вебхук не установлен
- Проверьте `TELEGRAM_WEBHOOK_URL` - должен быть публичный HTTPS URL
- Проверьте `TELEGRAM_WEBHOOK_SECRET` - должен быть установлен и не равен "changeme"

### 2. Вебхуки не доходят
- Проверьте, что сервер доступен извне: `curl https://ваш-домен.up.railway.app/health`
- Проверьте логи Railway - приходят ли запросы на `/webhook/telegram/...`

### 3. Секретный токен не совпадает
- Telegram отправляет `X-Telegram-Bot-Api-Secret-Token` в заголовке
- Он должен совпадать с `TELEGRAM_WEBHOOK_SECRET`
- Если не совпадает - вебхук вернёт 403 Forbidden

### 4. База данных не отвечает
- Проверьте `DATABASE_URL` - правильно ли указана строка подключения
- Проверьте, что PostgreSQL доступен и работает
- Проверьте логи на ошибки типа "connection refused" или "timeout"

### 5. Redis не отвечает
- Проверьте `REDIS_URL` - правильно ли указана строка подключения
- Если используется TLS - установите `REDIS_SSL=true`

## Деплой исправленной версии

1. Замените файлы в вашем репозитории:
   - `bot_api/webhooks/telegram.py`
   - `bot_api/handlers/start.py`
   - `services/replicate_client.py`

2. Закоммитьте и запушьте изменения:
```bash
git add .
git commit -m "Fix: Add detailed logging and MIME type detection"
git push
```

3. Railway автоматически задеплоит новую версию

4. Проверьте логи и попробуйте отправить /start боту

## Дополнительные рекомендации

1. **Генерируйте сложный TELEGRAM_WEBHOOK_SECRET:**
```bash
openssl rand -hex 32
```

2. **Проверяйте health endpoint:**
```bash
curl https://ваш-домен.up.railway.app/health
# Должен вернуть: {"status":"ok"}
```

3. **Если бот всё ещё не отвечает:**
   - Отправьте полные логи из Railway
   - Отправьте результат `getWebhookInfo`
   - Укажите, видны ли в логах сообщения о получении вебхуков

## Контакты для поддержки

Если проблема не решена после применения исправлений, предоставьте:
1. Полные логи из Railway (последние 100 строк)
2. Результат `getWebhookInfo`
3. Список установленных переменных окружения (без значений секретов)
