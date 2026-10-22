# TaskCoin — Mini App Task Management

TaskCoin helps users create bounty tasks, fund them in USDT, and verify
completion through a Mini App and Telegram bot.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# copy the example env and fill in your keys
cp .env.example .env

python -m taskcoin init-db
python -m taskcoin serve
```

## Environment

Key variables are listed in `.env.example`. At minimum you need:

- `TELEGRAM_BOT_TOKEN` — the bot token from @BotFather
- `DATABASE_URL` — path or connection string for the SQLite DB used in dev
- `MINIAPP_SECRET` — shared secret used to validate Mini App init data

## API

The Mini App communicates with the backend over a small JSON API served by
`serve.py`. The main endpoints are:

- `POST /api/task-requests` — create a new task request
- `GET /api/task-requests` — list the current user's requests
- `DELETE /api/task-requests/<id>` — cancel one of the user's requests
- `GET /api/task-requests/<id>/approve` — admin-only approve of a pending
  request

Request bodies and responses are JSON. All endpoints require a valid
Telegram Mini App `init_data` passed in the `X-Init-Data` header.

## Mini App

The Mini App is a small web app embedded in Telegram. It is served from the
same process as the API at `/`. The entry point is `miniapp/index.html` and
the JS lives under `miniapp/js/`.

## Verification

The verification flow sits behind the admin approve endpoint. Only approved
requests become live tasks that users can complete and get paid for.

## Deploy

For production, the app should be run behind a process manager and the DB
path should point to a persistent volume. The `Procfile` shows the expected
web command.

## License

This project is provided as-is for internal use.
