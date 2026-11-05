# tgchanreader — дайджест Telegram-каналов

Бот читает каналы вместо вас и раз в день (по умолчанию в 08:00) присылает сводку новых постов:

* **длинные посты сокращаются** нейросетью до 1–3 предложений, к ним идёт ссылка «читать полностью»;
* **реклама пропускается**: маркировка `erid`, `#реклама` и прочие эвристики плюс проверка LLM;
* **посты без текста** (только фото или видео) выводятся строкой «🖼 Только медиа (фото) — открыть пост»;
* у каналов есть **теги** (`политика`, `велосипед`…) и **приоритеты**: сначала идёт приоритет 1, потом 2, потом 3…;
* в начале дайджеста стоит блок **«Главное за день»** с 3–6 пунктами по всем каналам;
* есть **веб-архив и RSS**: лента дайджестов, лента всех постов и ленты по тегам;
* LLM подключается через **OpenRouter** (по умолчанию) или напрямую: **OpenAI**, **Anthropic Claude**, **Ollama**
  (локально) или любой OpenAI-совместимый API (DeepSeek, Groq, LM Studio, vLLM…).

Пример дайджеста:

```
📰 Дайджест за 5 ноября 2026
Постов: 37 · каналов: 12 · пропущено рекламы: 6

🧭 Главное за день
• …

🔴 ПРИОРИТЕТ 1
🏷 #политика
📢 Meduza
▫️ 09:15 ✂️ Краткий пересказ длинного поста…
    → читать полностью
▫️ 11:02 Короткий пост целиком
    → оригинал

🟡 ПРИОРИТЕТ 3
🏷 #велосипед
📢 Velo
▫️ 18:40 🎬 Только медиа (видео) — открыть пост
```

## Как бот читает каналы

Обычный Telegram-бот не может читать чужие каналы. Поэтому есть два источника:

| Режим | Что нужно | Что умеет |
|---|---|---|
| `web` (по умолчанию) | ничего | Читает публичные каналы через веб-превью `t.me/s/<канал>`. Аккаунт не нужен. |
| `telethon` | `TG_API_ID`, `TG_API_HASH`, `TG_SESSION` | Читает от имени **вашего** аккаунта: приватные каналы, каналы без веб-превью, команда `/import` добавляет все ваши подписки разом. |

При `SOURCE=auto` используется Telethon, если заданы переменные `TG_*`, иначе `web`. В режиме Telethon веб-превью
остаётся запасным вариантом. Бот (`BOT_TOKEN`) в обоих режимах нужен только для управления и отправки дайджеста.

## Быстрый старт

1. Создайте бота у [@BotFather](https://t.me/BotFather) и получите `BOT_TOKEN`.
2. Получите ключ на [openrouter.ai/keys](https://openrouter.ai/keys).
3. Подготовьте папку:

```bash
mkdir tgchanreader && cd tgchanreader
```

```bash
curl -O https://raw.githubusercontent.com/shevchukma/tgchanreader/main/docker-compose.yml
```

```bash
curl -o .env https://raw.githubusercontent.com/shevchukma/tgchanreader/main/.env.example
```

4. Впишите в `.env` `BOT_TOKEN`, `LLM_API_KEY` и `ADMIN_IDS`. Свой ID можно не искать заранее: запустите контейнер,
   напишите боту `/start`, он ответит вашим ID. Впишите его и перезапустите.
5. Запустите:

```bash
docker compose up -d
```

6. Добавьте каналы в боте:

```
/add @meduzalive политика,новости 1
/add https://t.me/velo_ru велосипед 3
```

Канал можно добавить и проще: перешлите боту любой пост из него.

Без docker compose:

```bash
docker run -d --name tgchanreader --restart unless-stopped --env-file .env -p 8080:8080 -v $(pwd)/data:/data velikoross/tgchanreader:latest
```

## Команды бота

| Команда | Описание |
|---|---|
| `/add @канал [теги] [приоритет]` | Добавить канал (или обновить теги/приоритет существующего) |
| `/remove @канал` | Удалить канал |
| `/list` | Все каналы по приоритетам |
| `/tag @канал тег1,тег2` | Задать теги (`/tag @канал -` убирает теги, `/tag` без аргументов выводит все теги) |
| `/priority @канал 1` | Задать приоритет (1 — самый важный) |
| `/pause @канал`, `/resume @канал` | Приостановить / возобновить канал |
| `/import` | Добавить все каналы, на которые вы подписаны (только Telethon) |
| `/digest` | Собрать и отправить дайджест сейчас; эти посты не попадут в следующий |
| `/preview [тег]` | Показать будущий дайджест, не отмечая посты прочитанными |
| `/status` | Статистика и время следующего дайджеста |

Бот отвечает только пользователям из `ADMIN_IDS`.

## Подключение Telethon (приватные каналы, импорт подписок)

1. Зайдите на [my.telegram.org](https://my.telegram.org) → *API development tools* и получите `api_id` и `api_hash`.
2. Сгенерируйте строку сессии (бот спросит телефон, код из Telegram и пароль 2FA, если он есть):

```bash
docker run -it --rm velikoross/tgchanreader:latest python -m tgchanreader.login
```

3. Впишите в `.env` `TG_API_ID`, `TG_API_HASH`, `TG_SESSION` и перезапустите контейнер.
4. В боте выполните `/import`, затем расставьте теги и приоритеты.

> ⚠️ `TG_SESSION` даёт полный доступ к аккаунту. Никому её не передавайте и не коммитьте `.env`.
> Сервис только читает сообщения, ничего не отправляет от вашего имени.

## Нейросети

Все «умные» функции (реклама, сокращение, «Главное за день») идут через один LLM-клиент:

```env
# OpenRouter: любая модель из https://openrouter.ai/models
LLM_PROVIDER=openrouter
LLM_API_KEY=sk-or-v1-...
LLM_MODEL=google/gemini-2.5-flash
```

```env
# Claude напрямую
LLM_PROVIDER=anthropic
LLM_API_KEY=sk-ant-...
LLM_MODEL=claude-haiku-5-5
```

```env
# OpenAI или любой OpenAI-совместимый API
LLM_PROVIDER=openai
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

```env
# Локальная Ollama (без ключа)
LLM_PROVIDER=ollama
LLM_BASE_URL=http://ollama:11434/v1
LLM_MODEL=qwen2.5:7b
```

С `LLM_PROVIDER=none` сервис работает и без нейросети: рекламу ловят эвристики, длинные посты обрезаются.
Если LLM временно недоступен, посты обрабатываются так же и дайджест всё равно приходит.

Посты отправляются в модель пачками (`LLM_BATCH_SIZE`, по умолчанию 8), чтобы экономить запросы.

## Веб-архив и RSS

При `WEB_ENABLED=true` на порту 8080 доступны:

| Адрес | Что |
|---|---|
| `/` | Архив дайджестов и список RSS-лент |
| `/digest/<id>` | Дайджест в браузере |
| `/rss/digests.xml` | RSS: каждый дайджест — отдельная запись |
| `/rss/posts.xml` | RSS: все посты без рекламы, длинные сокращены |
| `/rss/posts.xml?tag=велосипед` | RSS по тегу |
| `/healthz` | Проверка работоспособности |

Если сервис открыт в интернет, задайте `WEB_TOKEN`: тогда все адреса работают только с `?token=...`.
Пропишите внешний адрес в `WEB_BASE_URL`, чтобы ссылки в RSS и кнопка «Открыть в браузере» вели куда нужно.

Сервис может работать вообще без бота, только с RSS: не задавайте `BOT_TOKEN` и перечислите каналы в `data/channels.yml`
(пример лежит в `channels.example.yml`).

## Все настройки

Полный список с комментариями лежит в [`.env.example`](.env.example). Основные:

| Переменная | По умолчанию | Описание |
|---|---|---|
| `DIGEST_TIMES` | `08:00` | Время дайджеста, можно несколько: `08:00,19:00` |
| `TZ` | `Europe/Moscow` | Часовой пояс |
| `COLLECT_INTERVAL_MINUTES` | `60` | Как часто опрашивать каналы |
| `FIRST_FETCH_HOURS` | `24` | За сколько часов брать посты у только что добавленного канала |
| `SUMMARY_THRESHOLD` | `600` | Посты длиннее (в символах) сокращаются |
| `SUMMARY_MAX_CHARS` | `350` | Желаемая длина пересказа |
| `SHORT_POST_MAX_CHARS` | `800` | Короткие посты обрезаются до этой длины |
| `SKIP_ADS` | `true` | Пропускать рекламу |
| `DIGEST_OVERVIEW` | `true` | Блок «Главное за день» |
| `DIGEST_LANGUAGE` | `русский` | Язык пересказов |
| `DEFAULT_PRIORITY` | `2` | Приоритет новых каналов |
| `DIGEST_CHAT_IDS` | = `ADMIN_IDS` | Куда слать дайджест (можно в группу) |
| `SEND_EMPTY_DIGEST` | `false` | Слать дайджест, даже если новых постов нет |

Данные (SQLite) хранятся в `/data`, это папка `./data` рядом с `docker-compose.yml`.

## Сборка и публикация образа

GitHub Actions (`.github/workflows/docker.yml`) прогоняет тесты и собирает образ для `linux/amd64` и `linux/arm64`.
При пуше в `main` образ публикуется как `velikoross/tgchanreader:latest`, при теге `v1.2.3` — как `1.2.3` и `1.2`.
Для этого в настройках репозитория (*Settings → Secrets and variables → Actions → New repository secret*) добавьте секрет:

* `DOCKERHUB_TOKEN` — токен доступа с [app.docker.com/settings/personal-access-tokens](https://app.docker.com/settings/personal-access-tokens)
  (права *Read & Write*).
* `DOCKERHUB_USERNAME` — необязательно, по умолчанию `velikoross`.

Без токена workflow не падает: образ собирается для проверки, но не публикуется (в сводке будет предупреждение).

Собрать вручную:

```bash
docker build -t velikoross/tgchanreader:latest .
```

```bash
docker push velikoross/tgchanreader:latest
```

## Разработка

```bash
pip install -r requirements-dev.txt
```

```bash
pytest -q
```

Структура:

```
tgchanreader/
  config.py        настройки из переменных окружения
  db.py            SQLite: каналы, посты, дайджесты
  sources/web.py   чтение t.me/s/<канал>
  sources/telethon_src.py  чтение через аккаунт (Telethon)
  llm/             клиенты OpenRouter / OpenAI-совместимых / Anthropic
  processor.py     реклама и сокращение постов
  digest.py        сборка и вёрстка дайджеста
  service.py       сбор по расписанию, отправка дайджеста
  bot.py           Telegram-бот (aiogram)
  web.py           веб-архив и RSS (aiohttp)
  login.py         получение TG_SESSION
```
