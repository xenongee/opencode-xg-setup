"""Workkit recipe installation, safe synchronization, and read-only checks."""

import argparse

from check import check
from environment import EnvironmentError, discover_env_file
from install import install_components
from paths import _config_dir, _data_root, checkout_root, detect_platform, is_checkout, source_root
from sync import sync_plugins


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="workkit",
        description="Run trusted component recipes and safely apply the selected OpenCode composition.",
    )
    commands = parser.add_subparsers(dest="command")
    for name in ("install", "update"):
        command = commands.add_parser(name)
        command.add_argument("id", nargs="*", help="component IDs; omitted install IDs include saved selection and current defaults")
        command.add_argument("--config-dir", default=_config_dir(), help="target OpenCode configuration directory")
        if name == "install":
            command.add_argument("--all", action="store_true", help="install all catalog components")
            command.add_argument("--env-file", help="import a dotenv file after successful application")
        command.add_argument("--data-root", help="private Workkit data directory")
        command.add_argument("--platform", choices=("auto", "linux", "wsl", "windows"), default="auto")
        command.add_argument("--dry-run", action="store_true", help="print recipes without execution or writes")
    sync_parser = commands.add_parser("sync", help="verify local selected components and apply their config")
    sync_parser.add_argument("--config-dir", default=_config_dir(), help="target OpenCode configuration directory")
    sync_parser.add_argument("--data-root", help="private Workkit data directory")
    sync_parser.add_argument("--platform", choices=("auto", "linux", "wsl", "windows"), default="auto")
    sync_parser.add_argument("--dry-run", action="store_true", help="preview without verification or writes")
    sync_parser.add_argument("--env-file", help="import a dotenv file after successful application")
    check_parser = commands.add_parser("check", help="read-only local recipe and ownership checks")
    check_parser.add_argument("--config-dir", default=_config_dir(), help="target OpenCode configuration directory")
    check_parser.add_argument("--data-root", help="private Workkit data directory")
    check_parser.add_argument("--platform", choices=("auto", "linux", "wsl", "windows"), default="auto")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "install" and args.all and args.id:
        parser.error("--all cannot be combined with component IDs")
    data_root = args.data_root if args.data_root is not None else _data_root()
    try:
        target = detect_platform(args.platform)
    except ValueError:
        print(f"{args.command}: blocked: target platform or architecture is unknown")
        return 2

    env_file = None
    if args.command in ("install", "sync") and not args.dry_run:
        try:
            checkout = source_root()
            if not is_checkout(checkout):
                checkout = checkout_root()
            env_file = discover_env_file(args.env_file, checkout=checkout)
        except EnvironmentError:
            print(f"{args.command}: blocked: unsafe or inaccessible environment source")
            return 2

    if args.command == "check":
        status, report = check(args.config_dir, target, data_root=data_root)
    elif args.command == "sync":
        status, report = sync_plugins(
            args.config_dir, target, data_root=data_root, source_root=source_root(),
            dry_run=args.dry_run,
            env_file=env_file,
        )
    else:
        status, report = install_components(
            args.id, args.config_dir, data_root, target, operation=args.command,
            dry_run=args.dry_run, source_root=source_root(),
            all_components=args.all if args.command == "install" else False,
            env_file=env_file,
        )
    print(report, end="" if report.endswith("\n") else "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
