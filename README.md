# Reminder Bot

## A Discord bot for doing reminders

### Running:

* Ensure the languages folder has your desired language files (a `git submodule init` should get all of them from the official repo, or check https://github.com/reminder-bot/languages for specifics)

* Create file `config.ini` in the same location as `main.py`
* Fill it with the following stub:

```ini
[DEFAULT]
token =
dbl_token =
patreon_enabled = no
patreon_server = 0
patreon_role = 0
strings_location = ./languages/
local_timezone = UTC
local_language = EN
ignore_bots = 1

[MYSQL]
user = reminderbot
passwd =
host = db
database = reminders
```

* Insert values into `token` and `passwd` for your MySQL setup and your bot's authorization token (can be found at https://discordapp.com/developers/applications)
* Set `local_timezone` to a time region that is representative of your local time. For example, for the UK this is *Europe/London*

* Move to the postman directory (`cd postman-rs`) and create a file `.env` and fill with the following:

```
DISCORD_TOKEN="auth token as above"
DATABASE_URL="mysql://user:passwd@db/reminders"
INTERVAL=15
```
N: You can change `INTERVAL` to be higher for less CPU usage or lower for reminders that are more on time. Any value less than 15 is fairly excessive; the live bot uses 5. Interval is the number of seconds between checking for reminders.

N: **ON OLDER VERSIONS ONLY** Modifying the `THREADS` value is NOT recommended. This increases the amount of threads used for sending reminders. If you're sending many reminders in a single interval, increase this value. The live bot uses 1. New versions only run on one thread with asynchronous capability provided by Tokio and Reqwest

#### Docker:

* Create a file in `docker/` named `db-password` with the password set for `passwd` in `config.ini`.

```bash
docker-compose up -d
```

#### No Docker:

##### Deps:

* Python 3.8+
* MySQL 5+
* libmysqlclient21 and libmysqlclient-dev
	* *Debian* `sudo apt-get install default-libmysqlclient-dev libssl-dev`
* Poetry
	* pymysql, discord.py>=1.4.0a, pytz, dateparser, sqlalchemy, tinyconf
* Rust 1.42 with Cargo (for compilation only)

##### Optional Deps:

* Mypy (for static type checking if modifying code)

##### Running:

* Log into MySQL and execute

```SQL
CREATE DATABASE reminders;
```

* In `config.ini` set `host` to `localhost`

* `poetry install` to install python package dependencies with Poetry
* `poetry run python languages/to_database.py` to set up the database
* `poetry run python main.py` to run the bot client

* Move to the postman directory (`cd postman-rs`) and perform `cargo build --release` to compile it
* In `.env` change `db` under `DATABASE_URL` to `localhost`
* Run the release binary in `./target/release/main` alongside the python bot client.
