"""CLI for probing Plex seek control against an active session."""

from __future__ import annotations

import argparse
import asyncio
import json

from . import database as db
from .config import Config
from .paths import get_data_dir
import leapfrog.plex_client as plex_mod


async def _init_runtime() -> plex_mod.PlexClient:
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    db.set_db_path(data_dir / "leapfrog.db")
    await db.init_db()
    config = await Config.load()
    if not config.is_configured():
        raise RuntimeError("Plex is not configured in Leapfrog settings.")
    return plex_mod.init_client(config.plex_url, config.plex_token)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe Plex seek control for one active session.")
    parser.add_argument("--session-key", help="Active Plex session key to probe.")
    parser.add_argument("--offset-ms", type=int, help="Explicit offset to seek to.")
    parser.add_argument("--delta-ms", type=int, default=1000, help="Offset delta from current position when --offset-ms is omitted.")
    parser.add_argument("--list-sessions", action="store_true", help="List active sessions and exit.")
    parser.add_argument("--list-clients", action="store_true", help="List Companion-discovered Plex clients from /clients and exit.")
    parser.add_argument("--compare-session-key", help="Show one active session alongside its matched Companion client advertisement.")
    return parser


async def _amain(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    client = await _init_runtime()

    try:
        sessions = await client.get_active_sessions()
        if args.list_sessions:
            print(
                json.dumps(
                    [
                        {
                            "session_key": session.session_key,
                            "title": session.full_title,
                            "user": session.user,
                            "client": session.client_title,
                            "client_identifier": session.client_identifier,
                            "client_address": session.client_address,
                            "client_port": session.client_port,
                            "position_ms": session.position_ms,
                            "is_controllable": session.is_controllable,
                        }
                        for session in sessions
                    ],
                    indent=2,
                )
            )
            return 0

        if args.list_clients:
            clients = await client.list_companion_clients()
            print(json.dumps(clients, indent=2))
            return 0

        if args.compare_session_key:
            session = next((item for item in sessions if item.session_key == args.compare_session_key), None)
            if session is None:
                raise RuntimeError(f"Active session not found: {args.compare_session_key}")
            print(json.dumps(await client.compare_session_to_companion(session), indent=2))
            return 0

        if not args.session_key:
            raise RuntimeError("--session-key is required unless --list-sessions, --list-clients, or --compare-session-key is used.")

        session = next((item for item in sessions if item.session_key == args.session_key), None)
        if session is None:
            raise RuntimeError(f"Active session not found: {args.session_key}")

        payload = await client.probe_seek(
            session,
            offset_ms=args.offset_ms,
            delta_ms=args.delta_ms,
        )
        print(json.dumps(payload, indent=2))
        return 0 if payload.get("probe_success") else 1
    finally:
        await client.close()


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
