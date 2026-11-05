# QUNTA X HOST — Premium Telegram Control Bot

Repo contains exactly 4 files:
- `bot.py` — main bot (dashboard, logs, file check, bot control)
- `deploy.py` — Deploy Center (users host their own bot, owner approves)
- `requirements.txt` — Python packages
- `README.md` — this guide

No Procfile or nixpacks.toml is needed. Railway detects Python from `requirements.txt`.

## 1. Create the Telegram bot
1. Open @BotFather, send `/newbot`, copy the token.
2. Open @userinfobot (or send /myid to your bot after step 3) to get your numeric user ID.

## 2. Deploy on Railway
1. Push the 4 files to a GitHub repo root (not inside a folder).
2. Railway → New Project → Deploy from GitHub repo → select the repo.
3. **Variables** tab, add:
   - `BOT_TOKEN` = token from BotFather
   - `OWNER_ID` = your numeric Telegram ID
   - `DATA_DIR` = `/data`
4. **Volume**: Settings → Volumes → add a volume with mount path `/data`.
   Without it, users, channels and hosted bots are lost on every redeploy.
5. **Settings → Deploy → Custom Start Command**:
   ```
   python bot.py
   ```
6. Deploy. Check the logs for `QUNTA X HOST premium online`.

## 3. First use
- Send `/menu` to your bot. You will see the dashboard.
- `/addchannel @channelname` adds a required channel (up to 10).
- `/add <userid>` authorizes a user directly.

## Limits
- 1 hosted bot per user. Owner has no limit.
- 384 MB RAM per hosted bot. Over the limit, the bot turns OFF automatically and the owner is notified.
- Hosted bot code is only started after the owner taps Allow.

## Security notes
- Users must send their bot token in a private chat. Telegram may not let the bot delete that message, so delete it yourself after sending.
- `/sh` and `/run` are owner-only. Hosted bots never receive your `BOT_TOKEN` or `OWNER_ID`.
- Tokens are stored in `DATA_DIR/user_bots/<id>/token.txt` with 600 permission. Do not share the volume.

## Commands
Everyone: `/menu` `/status` `/vitals` `/check` `/myid` `/deploy` `/cancel`
Owner: `/run` `/stop` `/elog` `/sh` `/logs` `/on` `/off` `/add` `/remove` `/addchannel`

## Troubleshooting (check the Railway Deploy Logs)
- `BOT_TOKEN check failed`: the token is wrong or missing. Copy it again from BotFather and update the variable.
- `409 Conflict: terminated by other getUpdates request`: two copies of the bot are running. Remove the old deployment or old service so only one remains.
- `ModuleNotFoundError`: `requirements.txt` is missing from the repo root, or the build did not finish. Redeploy.
- `Start Command` error or app exits at once: set Custom Start Command to `python bot.py` in Settings → Deploy.
- Users or hosted bots disappear after redeploy: the `/data` volume is missing. Add it and set `DATA_DIR=/data`.
- Hosted bot turns OFF: it went over 384 MB RAM. Tap Start again, or make the bot lighter.
