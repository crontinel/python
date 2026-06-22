"""
Crontinel CLI — ``crontinel agent`` daemon command.

Usage::

    crontinel agent              Start the agent daemon
    crontinel agent --systemd    Print systemd unit file
    crontinel agent --supervisor Print Supervisor config
"""

from __future__ import annotations

import argparse
import os
import sys

from crontinel import __version__
from crontinel.agent import Agent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="crontinel",
        description="Crontinel monitoring tools.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # ── agent ──────────────────────────────────────────────────────────────
    agent_parser = subparsers.add_parser(
        "agent",
        help="Start the Crontinel agent daemon",
        description=(
            "Start the Crontinel agent daemon — connects to "
            "app.crontinel.com via SSE to receive and execute commands."
        ),
    )
    agent_parser.add_argument(
        "--systemd",
        action="store_true",
        help="Output a systemd unit file and exit",
    )
    agent_parser.add_argument(
        "--supervisor",
        action="store_true",
        help="Output a Supervisor config and exit",
    )

    args = parser.parse_args(argv)

    if args.command == "agent":
        return _handle_agent(args)
    else:
        parser.print_help()
        return 0


def _handle_agent(args: argparse.Namespace) -> int:
    if args.systemd:
        _print_systemd_unit()
        return 0
    if args.supervisor:
        _print_supervisor_config()
        return 0

    api_key = os.environ.get("CRONTINEL_API_KEY")
    api_url = os.environ.get("CRONTINEL_API_URL")

    if not api_key:
        print("CRONTINEL_API_KEY is not set.", file=sys.stderr)
        print("", file=sys.stderr)
        print("  export CRONTINEL_API_KEY=your-api-key", file=sys.stderr)
        print("  export CRONTINEL_API_URL=https://app.crontinel.com (optional)", file=sys.stderr)
        print("", file=sys.stderr)
        print("You can also generate a systemd unit or supervisor config:", file=sys.stderr)
        print("  crontinel agent --systemd", file=sys.stderr)
        print("  crontinel agent --supervisor", file=sys.stderr)
        return 1

    agent = Agent(api_key=api_key, api_url=api_url)
    print(f"Crontinel Agent v{__version__}")
    print("--------------------------")
    print()
    print("Starting agent daemon. Press Ctrl+C to stop.")
    print()

    try:
        agent.run()
    except KeyboardInterrupt:
        # Agent handles SIGINT internally; this is a safety net
        pass

    return 0


def _print_systemd_unit() -> None:
    cwd = os.getcwd()
    user = os.environ.get("USER", "root")

    print("[Unit]")
    print("Description=Crontinel Agent — remote command execution daemon")
    print("After=network.target")
    print()
    print("[Service]")
    print("Type=simple")
    print(f"User={user}")
    print(f"WorkingDirectory={cwd}")
    print(f"ExecStart={sys.executable} -m crontinel agent")
    print("Restart=always")
    print("RestartSec=5")
    print("TimeoutStopSec=30")
    print("KillSignal=SIGTERM")
    print()
    print("[Install]")
    print("WantedBy=multi-user.target")
    print()
    print("# Save to /etc/systemd/system/crontinel-agent.service")
    print("# Then: sudo systemctl daemon-reload && sudo systemctl enable --now crontinel-agent")


def _print_supervisor_config() -> None:
    cwd = os.getcwd()
    user = os.environ.get("USER", "root")

    print("[program:crontinel-agent]")
    print(f"command={sys.executable} -m crontinel agent")
    print(f"directory={cwd}")
    print(f"user={user}")
    print("autostart=true")
    print("autorestart=true")
    print("startretries=3")
    print("stopwaitsecs=30")
    print("stopsignal=SIGTERM")
    print("stdout_logfile=/var/log/crontinel-agent.log")
    print("stderr_logfile=/var/log/crontinel-agent.err")
    print()
    print("# Save to /etc/supervisor/conf.d/crontinel-agent.conf")
    print("# Then: sudo supervisorctl reread && sudo supervisorctl update && sudo supervisorctl start crontinel-agent")


if __name__ == "__main__":
    sys.exit(main())
