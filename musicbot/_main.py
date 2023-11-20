from __future__ import annotations

import argparse
import asyncio
import getpass
import os

import base2048

from .bot import MusicBot, platformdir_info
from .utils import LavalinkCreds, resolve_path_with_links


try:
    import uvloop  # type: ignore
except ModuleNotFoundError:
    uvloop = None


def _get_stored_credentials(filename: str) -> tuple[str, ...] | None:
    secret_file_path = platformdir_info.user_config_path / filename
    secret_file_path = resolve_path_with_links(secret_file_path)
    with secret_file_path.open("r", encoding="utf-8") as fp:
        return tuple(base2048.decode(line.removesuffix("\n")).decode("utf-8") for line in fp.readlines())


def _store_credentials(filename: str, *credentials: str) -> None:
    secret_file_path = platformdir_info.user_config_path / filename
    secret_file_path = resolve_path_with_links(secret_file_path)
    with secret_file_path.open("w", encoding="utf-8") as fp:
        for cred in credentials:
            fp.write(base2048.encode(cred.encode()))
            fp.write("\n")


def _input_token() -> None:
    prompt = "Paste your discord token (won't be visible), then press enter. It will be stored for later use."
    token = getpass.getpass(prompt)
    if not token:
        msg = "Not storing empty token."
        raise RuntimeError(msg)
    _store_credentials("musicbot.token", token)


def _input_lavalink_creds() -> None:
    prompts = (
        "Paste your Lavalink node URI (won't be visible), then press enter. It will be stored for later use.",
        "Paste your Lavalink node password (won't be visible), then press enter. It will be stored for later use.",
    )
    creds: list[str] = []
    for prompt in prompts:
        secret = getpass.getpass(prompt)
        if not secret:
            msg = "Not storing empty lavalink cred."
            raise RuntimeError(msg)
        creds.append(secret)
    _store_credentials("musicbot_lavalink.secrets", *creds)


def _get_token() -> str:
    token = os.getenv("DISCORD_TOKEN") or _get_stored_credentials("musicbot.token")
    if token is None:
        msg = (
            "You're missing a Discord bot token. Use '--token' in the CLI to trigger setup for it, or provide an "
            "environmental variable labelled 'DISCORD_TOKEN'."
        )
        raise RuntimeError(msg)
    return token[0] if isinstance(token, tuple) else token


def _get_lavalink_creds() -> LavalinkCreds:
    if (ll_uri := os.getenv("LAVALINK_URI")) and (ll_pwd := os.getenv("LAVALINK_PASSWORD")):
        lavalink_creds = LavalinkCreds(ll_uri, ll_pwd)
    elif ll_creds := _get_stored_credentials("musicbot_lavalink.secrets"):
        lavalink_creds = LavalinkCreds(ll_creds[0], ll_creds[1])
    else:
        msg = (
            "You're missing Lavalink node credentials. Use '--lavalink' in the CLI to trigger setup for it, or provide "
            "environmental variables labelled 'LAVALINK_URI' and 'LAVALINK_PASSWORD'."
        )
        raise RuntimeError(msg)
    return lavalink_creds


def run_client() -> None:
    """Confirm existence of required credentials and launch the radio bot."""

    async def bot_runner(client: MusicBot) -> None:
        async with client:
            await client.start(token, reconnect=True)

    token = _get_token()
    lavalink_creds = _get_lavalink_creds()

    client = MusicBot(lavalink_creds)

    loop = uvloop.new_event_loop if (uvloop is not None) else None  # type: ignore
    with asyncio.Runner(loop_factory=loop) as runner:  # type: ignore
        runner.run(bot_runner(client))


def main() -> None:
    parser = argparse.ArgumentParser(description="A minimal configuration discord bot for server radios.")
    setup_group = parser.add_argument_group(
        "setup",
        description="Choose credentials to specify. Discord token and Lavalink credentials are required on first run.",
    )
    setup_group.add_argument(
        "--token",
        action="store_true",
        help="Whether to specify the Discord token. Initiates interactive setup.",
        dest="specify_token",
    )
    setup_group.add_argument(
        "--lavalink",
        action="store_true",
        help="Whether you want to specify the Lavalink node URI.",
        dest="specify_lavalink",
    )

    args = parser.parse_args()

    if args.specify_token:
        _input_token()
    if args.specify_lavalink:
        _input_lavalink_creds()

    run_client()


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
