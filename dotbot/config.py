import json
import os
from pathlib import Path


def snowflake(value):
    value = str(value).strip()
    if not value.isascii() or not value.isdecimal() or not 15 <= len(value) <= 20:
        raise ValueError("Discord IDs must be 15–20 digits. Use Copy ID in Discord.")
    return value


def validate(config):
    config = dict(config)
    config["owner_id"] = snowflake(config["owner_id"])
    for key in ("guild_ids", "channel_ids"):
        values = config[key]
        if not isinstance(values, list) or not values:
            raise ValueError(f"{key} must be a nonempty list of Discord IDs.")
        config[key] = list(dict.fromkeys(snowflake(v) for v in values))
    return config


def load(directory):
    return validate(json.loads((directory / "config.json").read_text()))


def token(directory):
    value = os.environ.get("DISCORD_BOT_TOKEN")
    if value is None:
        value = (directory / "token").read_text()
    value = value.strip()
    if not value or any(c.isspace() for c in value):
        raise ValueError("A bot token is required. Run dotbot setup or set DISCORD_BOT_TOKEN.")
    return value


def private_directory(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)


def save(directory, config, secret):
    private_directory(directory)
    for name, content in (("config.json", json.dumps(validate(config), indent=2) + "\n"),
                          ("token", secret.strip() + "\n")):
        path = directory / name
        # Permissions apply before any secret bytes are written.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            os.chmod(path, 0o600)
            stream.write(content)


def allowed(config, author_id, guild_id, channel_id, is_bot=False, webhook=False):
    return (not is_bot and not webhook
            and str(author_id) == config["owner_id"]
            and str(guild_id) in config["guild_ids"]
            and str(channel_id) in config["channel_ids"])
