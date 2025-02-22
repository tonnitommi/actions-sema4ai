"""
Important: 

sema4ai.action_server.cli.main() 

is the only public API supported from the action-server.

Everything else is considered private and can be broken/changed without
being considered a backward-incompatible change!
"""

import argparse
import logging
import os.path
import sys
import time
import typing
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, Sequence, Union

from termcolor import colored

from sema4ai.action_server._protocols import IBeforeStartCallback

from . import __version__
from ._errors_action_server import ActionServerValidationError
from ._protocols import (
    ArgumentsNamespace,
    ArgumentsNamespaceBaseImportOrStart,
    ArgumentsNamespaceDownloadRcc,
    ArgumentsNamespaceImport,
    ArgumentsNamespaceMigrateImportOrStart,
    ArgumentsNamespaceNew,
    ArgumentsNamespaceStart,
)

if typing.TYPE_CHECKING:
    from sema4ai.action_server._database import Database
    from sema4ai.action_server._rcc import Rcc

    from ._settings import Settings

log = logging.getLogger(__name__)

# Important: main() is the only public API supported from the action-server.
# Everything else is considered private and can be broken/changed without
# being considered a backward-incompatible change!
__all__ = ["main"]


def _add_skip_lint(parser, defaults):
    parser.add_argument(
        "--skip-lint",
        default=False,
        help="Skip `@action` linting when an action is found (by default any "
        "`@action` is linted for errors when found).",
        action="store_true",
    )


def _add_whitelist_args(parser, defaults):
    parser.add_argument(
        "--whitelist",
        default="",
        help="Allows whitelisting the actions/packages to be used",
    )


def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ("yes", "true", "t", "y", "1"):
        return True
    elif v.lower() in ("no", "false", "f", "n", "0"):
        return False
    else:
        raise argparse.ArgumentTypeError("Boolean value expected.")


def _add_start_server_command(command_parser, defaults):
    from sema4ai.action_server._cli_helpers import add_data_args, add_verbose_args

    start_parser = command_parser.add_parser(
        "start",
        help=(
            "Starts the Sema4.ai Action Server (importing the actions in the "
            "current directory by default)."
        ),
    )

    start_parser.add_argument(
        "-a",
        "--address",
        default=defaults["address"],
        help="Server address (default: %(default)s)",
    )
    start_parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=defaults["port"],
        help="Server port (default: %(default)s)",
    )
    start_parser.add_argument(
        "--expose",
        action="store_true",
        help="Expose the server to the world",
    )
    start_parser.add_argument(
        "--server-url",
        help=(
            "Explicit server url to be defined in the OpenAPI spec 'servers' section."
            "Defaults to the localhost url."
        ),
        default=None,
    )
    start_parser.add_argument(
        "--expose-allow-reuse",
        dest="expose_allow_reuse",
        action="store_true",
        help="Always answer yes to expose reuse confirmation",
    )
    start_parser.add_argument(
        "--api-key",
        dest="api_key",
        help=(
            'Adds authentication. Pass it as `{"Authorization": "Bearer <API_KEY>"}` '
            "header. Pass `--api-key None` to disable authentication."
        ),
        default=None,
    )
    start_parser.add_argument(
        "--actions-sync",
        type=str2bool,
        help=(
            "By default the actions will be synchronized (added/removed) given the "
            "directories provided (if not specified the current directory is used). To "
            "start without synchronizing it's possible to use `--actions-sync=false`"
        ),
        default=True,
    )
    start_parser.add_argument(
        "--dir",
        metavar="PATH",
        help="By default, when starting, actions will be collected from the current "
        "directory to serve, but it's also possible to use `--dir` to load actions "
        "from a different directory",
        action="append",
    )
    _add_skip_lint(start_parser, defaults)
    start_parser.add_argument(
        "--min-processes",
        type=int,
        help=(
            "The minimum number of action processes that should always be kept alive, "
            "ready to process any incoming request."
        ),
        default=2,
    )
    start_parser.add_argument(
        "--max-processes",
        type=int,
        help=(
            "The maximum number of processes that may be created to handle the actions."
        ),
        default=20,
    )
    start_parser.add_argument(
        "--reuse-processes",
        action="store_true",
        help=(
            "By default actions are run once and then after the action runs the "
            "process that ran the action exits. This can be changed by using "
            "--reuse-processes. With this flag, after running the action instead of "
            "exiting the process will be available to run another action in the same "
            "process (note that in this case care must be taken so that memory leakage "
            "does not happen in the action and that global state from one run does not "
            "interfere with a subsequent run)."
        ),
    )

    start_parser.add_argument(
        "--full-openapi-spec",
        action="store_true",
        help="By default, the public OpenAPI specification will include only endpoints to run "
        "individual actions and omit all other endpoints. With this flag, all endpoints "
        "available will be defined in the public OpenAPI specification.",
    )

    add_data_args(start_parser, defaults)
    add_verbose_args(start_parser, defaults)
    _add_whitelist_args(start_parser, defaults)


def _add_migrate_command(command_subparser, defaults):
    from sema4ai.action_server._cli_helpers import add_data_args, add_verbose_args

    migration_parser = command_subparser.add_parser(
        "migrate",
        help="Makes a database migration (if needed) and exits",
    )
    add_data_args(migration_parser, defaults)
    add_verbose_args(migration_parser, defaults)


def _add_import_command(command_subparser, defaults):
    from sema4ai.action_server._cli_helpers import add_data_args, add_verbose_args

    import_parser = command_subparser.add_parser(
        "import",
        help="Imports an Action Package and exits",
    )

    import_parser.add_argument(
        "--dir",
        metavar="PATH",
        help="Can be used to import an action package from the local filesystem",
        action="append",
    )
    _add_skip_lint(import_parser, defaults)
    add_data_args(import_parser, defaults)
    add_verbose_args(import_parser, defaults)
    _add_whitelist_args(import_parser, defaults)


def _create_parser():
    from sema4ai.action_server._cli_helpers import add_verbose_args
    from sema4ai.action_server.package._package_build_cli import add_package_command

    from ._settings import Settings

    defaults = Settings.defaults()
    base_parser = argparse.ArgumentParser(
        prog="action-server",
        description=f"Sema4.ai Action Server ({__version__})",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    command_subparser = base_parser.add_subparsers(dest="command")

    # Starts the server
    _add_start_server_command(command_subparser, defaults)

    # Import
    _add_import_command(command_subparser, defaults)

    # Download RCC
    rcc_parser = command_subparser.add_parser(
        "download-rcc",
        help=(
            "Downloads RCC (by default to the location required by the "
            "Sema4.ai Action Server)"
        ),
    )

    rcc_parser.add_argument(
        "--file",
        metavar="PATH",
        help="Target file to where RCC should be downloaded",
        nargs="?",
    )

    # New project from template
    new_parser = command_subparser.add_parser(
        "new",
        help="Bootstrap new project from template",
    )
    new_parser.add_argument(
        "--name",
        help="Name for the project",
    )
    add_verbose_args(new_parser, defaults)

    # Schema
    # schema_parser = command_subparser.add_parser(
    #     "schema",
    #     help="Prints the schema and exits",
    # )
    #
    # schema_parser.add_argument(
    #     "--file",
    #     metavar="PATH",
    #     help=(
    #         "Write openapi.json schema and exit (if path is given the schema "
    #         "is written to that file instead of stdout)"
    #     ),
    #     nargs="?",
    # )

    # Version
    command_subparser.add_parser(
        "version",
        help="Prints the version and exits",
    )

    _add_migrate_command(command_subparser, defaults)

    add_package_command(command_subparser, defaults)

    return base_parser


def main(args: Optional[list[str]] = None, *, exit=True) -> int:  # noqa
    if args is None:
        args = sys.argv[1:]

    if not args:
        if os.environ.get(
            "RC_ACTION_SERVER_FORCE_DOWNLOAD_RCC", ""
        ).strip().lower() in (
            "1",
            "true",
        ):
            log.info(
                "As RC_ACTION_SERVER_FORCE_DOWNLOAD_RCC is set and no arguments were "
                "passed, rcc will be downloaded."
            )

            from sema4ai.action_server._download_rcc import download_rcc

            download_rcc(force=True)

        if os.environ.get("RC_ACTION_SERVER_DO_SELFTEST", "").strip().lower() in (
            "1",
            "true",
        ):
            from . import _selftest

            log.info(
                "As RC_ACTION_SERVER_DO_SELFTEST is set and no arguments were passed, "
                "a selftest will be run."
            )

            sys.exit(_selftest.do_selftest())

    retcode = _main_retcode(args)
    if exit:
        sys.exit(retcode)
    return retcode


def _setup_stderr_logging(log_level):
    from logging import StreamHandler

    # stderr is the default, but make it explicit.
    stream_handler = StreamHandler(sys.stderr)
    stream_handler.setLevel(log_level)
    if log_level == logging.DEBUG:
        os.environ["NO_COLOR"] = "true"
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="[%Y-%m-%d %H:%M:%S]"
        )
    else:
        from ._robo_utils.log_formatter import FormatterStdout, UvicornLogFilter

        formatter = FormatterStdout("%(message)s", datefmt="[%X]")
        stream_handler.addFilter(UvicornLogFilter())

    stream_handler.setFormatter(formatter)
    logger = logging.root
    logger.addHandler(stream_handler)


def _setup_logging(datadir: Path, log_level):
    from logging.handlers import RotatingFileHandler

    from ._robo_utils.log_formatter import FormatterNoColor

    log_file = str(datadir / "server_log.txt")
    log.info(colored(f"Logs may be found at: {log_file}.", attrs=["dark"]))
    rotating_handler = RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    rotating_handler.setLevel(log_level)
    rotating_handler.setFormatter(
        FormatterNoColor(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="[%Y-%m-%d %H:%M:%S]"
        )
    )
    logger = logging.root
    logger.addHandler(rotating_handler)


def _import_actions(
    base_args: ArgumentsNamespaceBaseImportOrStart,
    settings: "Settings",
    disable_not_imported: bool,
) -> int:
    """
    Args:
        base_args: The base arguments collected from the cli input.
        settings: The settings for the action server.
        disable_not_imported: Whether actions which were not imported should be disabled.

    Returns: 0 if everything is correct and some other number if some error happened
        while importing the actions.
    """
    from sema4ai.action_server.vendored_deps.termcolors import bold_red

    from . import _actions_import

    if not base_args.dir:
        base_args.dir = ["."]

    try:
        for action_package_dir in base_args.dir:
            _actions_import.import_action_package(
                datadir=settings.datadir,
                action_package_dir=os.path.abspath(action_package_dir),
                skip_lint=base_args.skip_lint,
                disable_not_imported=disable_not_imported,
                whitelist=base_args.whitelist,
            )
    except ActionServerValidationError as e:
        log.critical(
            bold_red(
                f"\nUnable to import action. Please fix the error below and retry.\n{e}",
            )
        )
        return 1
    return 0


def _get_log_level(base_args):
    verbose = getattr(base_args, "verbose", False)
    log_level = logging.DEBUG if verbose else logging.INFO
    return log_level


def _main_retcode(
    args: Optional[list[str]],
    is_subcommand: bool = False,
    use_db: Optional["Database"] = None,
    before_start: Sequence[IBeforeStartCallback] = (),
) -> int:
    """
    The main entrypoint that returns the returncode.
    Args:
        args: The (cli) arguments to run.
        is_subcommand: This call may be recursive on cases where one command
            calls another command (such as the package build which makes an in-
            memory import). In these cases, is_subcommand must be True.
        use_db: If given this in the db used to run the command (otherwise
            one is either created or loaded from the datadir).
        before_start: If given it's a list of callbacks which should be called
            before the app is actually started (note that if a callback returns
            False the startup process will be halted).

    Returns: The returncode for the process (0 means all was ok).
    """

    if args is None:
        args = sys.argv[1:]

    if args and args[0] == "server-expose":
        # The process is being called by to make the server expose.
        # Internal usage only, so, don't even do argument parsing
        # for it.
        from . import _server_expose

        try:
            (
                expose_server_parent_pid,
                expose_server_port,
                expose_server_verbose,
                expose_server_host,
                expose_server_expose_url,
                expose_server_datadir,
                expose_server_expose_session,
                expose_server_api_key,
            ) = args[1:]
        except Exception:
            raise RuntimeError(f"Unable to initialize server with sys.argv: {sys.argv}")

        _server_expose.main(
            expose_server_parent_pid,
            expose_server_port,
            expose_server_verbose,
            expose_server_host,
            expose_server_expose_url,
            expose_server_datadir,
            expose_server_expose_session,
            expose_server_api_key,
        )
        return 0

    parser = _create_parser()
    base_args: ArgumentsNamespace = parser.parse_args(args)

    command = base_args.command
    if not command:
        parser.print_help()
        return 0

    if command == "version":
        print(__version__)
        sys.stdout.flush()
        return 0

    if not is_subcommand:
        # Setup logging for the command (only if this is not a subcommand
        # as if it's a subcommand we're actually recursing here).
        log_level = _get_log_level(base_args)

        logger = logging.root
        logger.setLevel(log_level)

        # Log to stderr.
        _setup_stderr_logging(log_level)

    # if command == "schema":
    # This doesn't work at this point because we have to register the
    # actions first for it to work.
    #     file = base_args.file
    #     _write_schema(file)
    #     return

    if command == "download-rcc":
        download_args: ArgumentsNamespaceDownloadRcc = typing.cast(
            ArgumentsNamespaceDownloadRcc, base_args
        )
        from ._download_rcc import download_rcc

        download_rcc(target=download_args.file, force=True)
        return 0

    if command == "package":
        from sema4ai.action_server.package._package_build_cli import (
            handle_package_command,
        )

        return handle_package_command(base_args)

    if command not in (
        "migrate",
        "import",
        "start",
        "new",
    ):
        log.critical(f"Unexpected command: {command}.")
        return 1

    log.info(
        colored("\n  ⚡️ Starting Action Server... ", attrs=["bold"])
        + colored(f"v{__version__}\n", attrs=["dark"])
    )

    if command == "new":
        new_args: ArgumentsNamespaceNew = typing.cast(ArgumentsNamespaceNew, base_args)
        from ._new_project import create_new_project

        create_new_project(directory=new_args.name)
        return 0

    migrate_import_or_start_args = typing.cast(
        ArgumentsNamespaceMigrateImportOrStart, base_args
    )
    with _basic_setup(migrate_import_or_start_args) as setup_info:
        return _make_import_migrate_or_start(
            migrate_import_or_start_args,
            command,
            setup_info,
            use_db=use_db,
            before_start=before_start,
        )


@dataclass
class _SetupInfo:
    rcc: "Rcc"
    settings: "Settings"


@contextmanager
def _basic_setup(
    base_args: ArgumentsNamespaceMigrateImportOrStart,
):
    from ._download_rcc import download_rcc
    from ._rcc import initialize_rcc
    from ._settings import setup_settings

    with setup_settings(base_args) as settings:
        settings.datadir.mkdir(parents=True, exist_ok=True)
        robocorp_home = settings.datadir / ".robocorp_home"
        robocorp_home.mkdir(parents=True, exist_ok=True)

        with initialize_rcc(download_rcc(force=False), robocorp_home) as rcc:
            yield _SetupInfo(rcc=rcc, settings=settings)


def _make_import_migrate_or_start(
    base_args: ArgumentsNamespaceMigrateImportOrStart,
    command: Literal["import"] | Literal["migrate"] | Literal["start"],
    setup_info: _SetupInfo,
    use_db: Optional["Database"] = None,
    before_start: Sequence[IBeforeStartCallback] = (),
) -> int:
    from sema4ai.action_server._settings import is_frozen

    from ._robo_utils.system_mutex import SystemMutex
    from ._runs_state_cache import use_runs_state_ctx

    timeout = 3
    timeout_at = time.time() + timeout
    settings: "Settings" = setup_info.settings

    shown_first_message = False
    while True:
        mutex = SystemMutex("action_server.lock", base_dir=str(settings.datadir))
        acquired = mutex.get_mutex_aquired()
        if acquired:
            if shown_first_message:
                log.info("Exited. Proceeding with action server startup.")
            break

        msg = mutex.mutex_creation_info or ""
        i = msg.find("--- Stack ---")
        if i > 0:
            msg = msg[:i]
        msg = msg.strip()

        if not shown_first_message:
            shown_first_message = True
            log.info(
                f"An action server is already started in this datadir ({settings.datadir}).\n"
                f"\nInformation on mutex holder:\n"
                f"{msg}",
            )

        log.info("Waiting for it to exit...")
        time.sleep(0.3)

        timed_out = time.time() > timeout_at
        if timed_out:
            log.critical(
                "\nAction server not started (timed out waiting for mutex to be released).",
            )
            return 1

    # Log to file in datadir, always in debug mode
    # (only after lock is in place as multiple loggers to the same
    # file would be troublesome).
    _setup_logging(settings.datadir, _get_log_level(base_args))

    try:
        db_path: Union[Path, str]
        if settings.db_file != ":memory:":
            db_path = settings.datadir / settings.db_file
        else:
            db_path = settings.db_file

        from sema4ai.action_server._models import create_db, load_db
        from sema4ai.action_server.migrations import db_migration_pending, migrate_db

        is_new = db_path == ":memory:" or not os.path.exists(db_path)

        if use_db is not None:

            @contextmanager
            def _use_db_ctx(*args, **kwags):
                yield use_db

            use_db_ctx = _use_db_ctx

        elif is_new:
            log.info("Database file does not exist. Creating it at: %s", db_path)
            use_db_ctx = create_db
        else:
            use_db_ctx = load_db

        if command == "migrate":
            if db_path == ":memory:":
                log.critical(
                    "Cannot do migration of in-memory databases",
                )
                return 1
            if not migrate_db(db_path):
                return 1
            return 0
        else:
            if is_frozen():
                cmdline = "action-server"
            else:
                cmdline = "python -m sema4ai.action_server"

            if not is_new and db_migration_pending(db_path):
                log.critical(
                    f"""It was not possible to start the server because a
database migration is required to use with this version of the
Sema4.ai Action Server.

Please run the command:

{cmdline} migrate

To migrate the database to the current version
-- or start from scratch by erasing the file:
{db_path}
"""
                )
                return 1

        with use_db_ctx(db_path) as db:
            if command == "import":
                return _import_actions(
                    typing.cast(ArgumentsNamespaceImport, base_args),
                    settings,
                    disable_not_imported=False,
                )

            elif command == "start":
                start_args: ArgumentsNamespaceStart = typing.cast(
                    ArgumentsNamespaceStart, base_args
                )
                # start imports the current directory by default
                # (unless --actions-sync=false is specified).
                log.debug("Synchronize actions: %s", start_args.actions_sync)

                setup_info.rcc.feedack_metric("action-server.started", __version__)

                if start_args.actions_sync:
                    code = _import_actions(
                        start_args,
                        settings,
                        disable_not_imported=start_args.actions_sync,
                    )
                    if code != 0:
                        return code

                with use_runs_state_ctx(db):
                    from ._server import start_server

                    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)

                    expose_session = None
                    if start_args.expose:
                        from ._server_expose import read_expose_session_json

                        expose_session = read_expose_session_json(
                            datadir=str(settings.datadir)
                        )
                        if expose_session and not start_args.expose_allow_reuse:
                            confirm = input(
                                colored(
                                    "> Resume previous expose URL ",
                                    attrs=["bold"],
                                )
                                + colored(expose_session.url, "light_blue")
                                + colored(" Y/N?", attrs=["bold"])
                                + colored(" [Y]", attrs=["dark"])
                            )
                            if confirm.lower() == "y" or confirm == "":
                                log.debug("Resuming previous expose session")
                            else:
                                expose_session = None

                    api_key = None
                    if start_args.api_key:
                        api_key = start_args.api_key
                    elif start_args.expose:
                        from ._robo_utils.auth import get_api_key

                        api_key = get_api_key(settings.datadir)

                    try:
                        start_server(
                            expose=start_args.expose,
                            api_key=api_key,
                            expose_session=expose_session.expose_session
                            if expose_session
                            else None,
                            whitelist=start_args.whitelist,
                            before_start=before_start,
                        )
                    except KeyboardInterrupt:
                        log.critical("Exiting action server...")
                        return 1
                    return 0

            else:
                log.critical(f"Unexpected command: {command}.")
                return 1
    finally:
        mutex.release_mutex()


if __name__ == "__main__":
    main()
