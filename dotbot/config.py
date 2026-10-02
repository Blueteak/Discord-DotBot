import json
import os
import tempfile
from pathlib import Path


def snowflake(value):
    value = str(value).strip()
    if not value.isascii() or not value.isdecimal() or not 15 <= len(value) <= 20:
        raise ValueError("Discord IDs must be 15–20 digits. Use Copy ID in Discord.")
    return value


def validate(config):
    config = dict(config)
    config.setdefault("scope", "scoped")
    if config["scope"] not in ("scoped", "accessible"):
        raise ValueError("scope must be scoped or accessible.")
    config["owner_id"] = snowflake(config["owner_id"])
    handle = config.get("owner_handle", "")
    if not isinstance(handle, str) or len(handle) > 100:
        raise ValueError("owner_handle must be text of at most 100 characters.")
    config["owner_handle"] = handle.strip()
    for key in ("guild_ids", "channel_ids"):
        values = config.get(key, [])
        if not isinstance(values, list) or (not values and config["scope"] == "scoped"):
            raise ValueError(f"{key} must be a nonempty list of Discord IDs.")
        config[key] = list(dict.fromkeys(snowflake(v) for v in values))
    # Keep older installations' access scope unchanged.
    config.setdefault("listen", "mentions")
    config.setdefault("audience", "owner")
    if config["listen"] not in ("channels", "mentions"):
        raise ValueError("listen must be channels or mentions.")
    if config["audience"] not in ("channel", "owner"):
        raise ValueError("audience must be channel or owner.")
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
        private_write(directory / name, content)


def private_write(path, content):
    # Atomic replacement, with private permissions before writing any bytes.
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".dotbot-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def allowed(config, author_id, guild_id, channel_id, is_bot=False, webhook=False):
    return (not is_bot and not webhook
            and (config.get("audience", "owner") == "channel" or str(author_id) == config["owner_id"])
            and guild_id is not None and str(guild_id) != "None"
            and (config.get("scope", "scoped") == "accessible"
                 or (str(guild_id) in config["guild_ids"] and str(channel_id) in config["channel_ids"])))
