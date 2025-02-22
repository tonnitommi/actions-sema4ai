import json
import logging
import subprocess
import sys
import typing
from pathlib import Path
from typing import Literal

from termcolor import colored

from sema4ai.action_server._robo_utils.callback import Callback, OnExitContextManager
from sema4ai.action_server.vendored_deps.action_package_handling.cli_errors import (
    ActionPackageError,
)
from sema4ai.action_server.vendored_deps.termcolors import bold_red, bold_yellow

if typing.TYPE_CHECKING:
    from sema4ai.actions._protocols import ActionsListActionTypedDict

    from sema4ai.action_server._models import ActionPackage

log = logging.getLogger(__name__)


class IHookOnActionsListCallback(typing.Protocol):
    def __call__(
        self,
        action_package: "ActionPackage",
        actions_list_result: list["ActionsListActionTypedDict"],
    ):
        ...


class IHookOnActionsList(typing.Protocol):
    def register(
        self,
        callback: IHookOnActionsListCallback,
    ) -> OnExitContextManager:
        ...

    def __call__(
        self,
        action_package: "ActionPackage",
        actions_list_result: list["ActionsListActionTypedDict"],
    ):
        ...


# Called as: hook_on_actions_list(action_package, actions_list_result)
hook_on_actions_list: IHookOnActionsList = Callback(raise_exceptions=True)


def _log_deprecated_conda():
    from sema4ai.action_server._settings import is_frozen

    if is_frozen():
        cmd = "action-server"
    else:
        cmd = "python -m sema4ai.action_server"
    log.critical(
        bold_red(
            "Deprecated: The file for defining the environment is now `package.yaml`.\n"
            "It's not a one to one mapping for action-server.yaml, but\n"
            f"`{cmd} package update` can be used to make most of the needed changes.\n"
            "See: https://github.com/robocorp/robocorp/blob/master/action_server/docs/guides/01-package-yaml.md for more details."
        )
    )


def import_action_package(
    *,
    datadir: Path,
    action_package_dir: str,
    disable_not_imported: bool,
    skip_lint: bool,
    whitelist: str,
):
    """
    Imports action packages based on directories given in the filesystem.

    Note that the action package is expected to be in the proper directory at
    this point (meaning that it should have been extracted under the /datadir
    if given as a .zip or a path in the filesystem in any other place when
    running in dev mode).

    Raises:
        ActionPackageError if it was not recognized as an action package.

    Note:
        This can be a slow operation as it may require building the RCC
        environment.
    """

    from sema4ai.action_server._whitelist import accept_action_package
    from sema4ai.action_server.vendored_deps.action_package_handling import (
        create_conda_from_package_yaml,
    )

    from ._errors_action_server import ActionServerValidationError
    from ._gen_ids import gen_uuid
    from ._models import ActionPackage
    from ._rcc import create_hash, get_rcc
    from ._robo_utils.process import build_python_launch_env
    from ._settings import is_frozen

    log.debug("Importing action package from: %s", action_package_dir)

    datadir = datadir.absolute()
    import_path = Path(action_package_dir).absolute()
    if not import_path.exists():
        raise ActionPackageError(
            f"Unable to import action package from directory: {import_path} "
            "(directory does not exist).",
        )
    if not import_path.is_dir():
        raise ActionPackageError(f"Error: expected {import_path} to be a directory.")

    import yaml

    # Verify if it's actually a proper package (meaning that it has
    # the package.yaml as well as actions we can run).
    original_package_yaml = original_conda_yaml = conda_yaml = (
        import_path / "package.yaml"
    )

    action_package_name = ""
    package_yaml_exists = conda_yaml.exists()
    if package_yaml_exists:
        # Ok, new version: we create a conda.yaml automatically from
        # the environment information.
        if not action_package_name:
            try:
                with open(original_package_yaml, "r", encoding="utf-8") as stream:
                    package_yaml_contents = yaml.safe_load(stream)
            except Exception:
                raise ActionPackageError(
                    f"Error loading file as yaml ({original_package_yaml})."
                )
            if isinstance(package_yaml_contents, dict):
                n = package_yaml_contents.get("name")
                if n:
                    action_package_name = n

        conda_yaml = create_conda_from_package_yaml(datadir, conda_yaml)

    if not action_package_name:
        action_package_name = import_path.name

    if whitelist:
        if not accept_action_package(whitelist, action_package_name):
            log.info(
                f"Action package: {action_package_name} not imported because it has no match in the whitelist: {whitelist!r}"
            )
            return

    if not package_yaml_exists:
        # Backward-compatibility
        conda_yaml = original_conda_yaml = import_path / "action-server.yaml"
        package_yaml_exists = conda_yaml.exists()
        if package_yaml_exists:
            _log_deprecated_conda()

    if not package_yaml_exists:
        # Backward-compatibility
        conda_yaml = original_conda_yaml = import_path / "conda.yaml"
        package_yaml_exists = conda_yaml.exists()
        if package_yaml_exists:
            _log_deprecated_conda()

    if not package_yaml_exists:
        if is_frozen():
            raise ActionServerValidationError(
                f"Unable to import actions in standalone action-server because no `package.yaml` is available at: {original_package_yaml}."
            )
        log.info(
            """Adding action without a managed environment (package.yaml unavailable).
Note: no virtual environment will be used for the imported actions, they'll be run in the same environment used to run the action server."""
        )
        condahash = "<unmanaged>"
        use_env = {}
    else:
        try:
            with open(conda_yaml, "r", encoding="utf-8") as stream:
                contents = yaml.safe_load(stream)
        except Exception:
            raise ActionPackageError(f"{conda_yaml} does not seem a valid yaml.")

        if not isinstance(contents, dict):
            raise ActionPackageError(f"{conda_yaml} has no dict as top-level.")

        if not contents.get("dependencies"):
            raise ActionPackageError(f"{conda_yaml} has no 'dependencies' specified.")

        log.debug(
            f"""Actions added with managed environment defined by: {conda_yaml}."""
        )

        # The hash is based only on the parsed contents, not on the file
        # contents per se (so, changing comments or spaces is ok).
        condahash = create_hash(repr(contents))

        log.info(
            "Action package seems ok. "
            "Bootstrapping RCC environment (please wait, this can take a long time)."
        )
        rcc = get_rcc()
        env_info = rcc.create_env_and_get_vars(conda_yaml, condahash)
        if not env_info.success:
            raise ActionPackageError(
                f"It was not possible to bootstrap the RCC environment. "
                f"Error: {env_info.message}"
            )
        if not env_info.result:
            raise ActionPackageError(
                "It was not possible to get the environment when "
                "bootstrapping RCC environment."
            )
        use_env = env_info.result.env

    # Ok, we bootstrapped, now, let's collect the actions.
    try:
        # If the directory can be made relative to the datadir, save the
        # directory as relative.
        directory_path = import_path.relative_to(datadir)
        assert import_path.samefile(directory_path)
    except (AssertionError, ValueError):
        # Otherwise use the absolute path.
        directory_path = import_path

    action_package_id = gen_uuid("action_package")

    python_exe = use_env.get("PYTHON_EXE")
    if python_exe:
        log.info(colored(f"Python interpreter path: {python_exe}", attrs=["dark"]))

    action_package = ActionPackage(
        id=action_package_id,
        name=action_package_name,
        directory=directory_path.as_posix(),
        conda_hash=condahash,
        env_json=json.dumps(use_env),
    )
    log.debug(f"Collecting actions for Action Package: {action_package_name}.")

    env = build_python_launch_env(use_env)

    try:
        # any sema4ai.actions version will do at this point.
        v = _get_actions_version(env, import_path, "sema4ai.actions")
    except Exception:
        ### TODO: Remove in the future!

        found_actions = False
        # Still support robocorp.actions for now (but warn the user).
        try:
            v = _get_actions_version(env, import_path, "robocorp.actions")
            log.critical(
                "Important: 'robocorp.actions' is deprecated!\n"
                "Please change the 'robocorp-actions' dependency to 'sema4ai-actions'\n"
                "in your package.yaml\n"
                "(note: the public API should be the same with the exception that the\n"
                "imports should come from 'sema4ai.actions' instead of 'robocorp.actions).\n"
                "On future versions of the Action Server, using 'robocorp-actions' will no\n"
                "longer be supported.\n"
            )
            found_actions = True
        except Exception:
            pass

        if not found_actions:
            raise  # The sema4ai.actions error, not the robocorp.actions one.

        expected_version = (0, 0, 7)
        expected_version_str = ".".join(str(x) for x in expected_version)
        if v < expected_version:
            v_as_str = ".".join(str(x) for x in v)

            if package_yaml_exists:
                raise ActionServerValidationError(
                    f"Error, the `robocorp-actions` version is: {v_as_str}.\n"
                    f"Expected `robocorp-actions` version to be {expected_version_str} or higher.\n"
                    f"Please update the version in: {original_conda_yaml}\n"
                )
            else:
                raise ActionServerValidationError(
                    f"Error, the `robocorp-actions` version is: {v_as_str}.\n"
                    f"Expected it to be {expected_version_str} or higher.\n"
                    f"Please update the `robocorp-actions` version in your python environment (python: {sys.executable})\n"
                )

        min_version_for_encryption_with_auth_tag = (0, 2, 1)
        if v < min_version_for_encryption_with_auth_tag:
            v_as_str = ".".join(str(x) for x in v)
            log.critical(
                f"Warning: the `robocorp-actions` version is: {v_as_str}.\n"
                f"To receive encrypted secrets, robocorp-actions 0.2.1 or newer is required.\n"
                f"Please update the version in: {original_conda_yaml}\n"
                "(proceeding with initalization but actions receiving encrypted secrets will\n"
                "not work properly -- on future versions of the action server, support for \n"
                "this version of robocorp-actions will be removed)."
            )

    _add_actions_to_db(
        datadir,
        env,
        import_path,
        action_package,
        disable_not_imported=disable_not_imported,
        skip_lint=skip_lint,
        whitelist=whitelist,
    )


def _get_actions_version(
    env, cwd, libname: Literal["robocorp.actions"] | Literal["sema4ai.actions"]
) -> tuple[int, ...]:
    from sema4ai.action_server._settings import get_python_exe_from_env

    python = get_python_exe_from_env(env)
    cmdline: list[str] = [
        python,
        "-c",
        f"import {libname};print({libname}.__version__)",
    ]
    msg = f"""Unable to get {libname} version.

This usually means that `{libname}` is not installed in the python
environment (make sure that `{libname.replace('.', '-')}`
is defined in your `package.yaml`).

Python executable being used:
{python}
"""

    try:
        output = subprocess.check_output(
            cmdline,
            env=env,
            cwd=cwd,
        )
    except Exception:
        raise RuntimeError(msg)
    str_output = output.decode("utf-8", "replace")
    try:
        return tuple(int(x) for x in str_output.strip().split("."))
    except Exception:
        raise RuntimeError(msg)


def _add_actions_to_db(
    datadir: Path,
    env: dict,
    import_path: Path,
    action_package: "ActionPackage",
    disable_not_imported: bool,
    skip_lint: bool,
    whitelist: str,
):
    from dataclasses import asdict

    from sema4ai.actions._lint_action import format_lint_results

    from sema4ai.action_server._errors_action_server import ActionServerValidationError
    from sema4ai.action_server._gen_ids import gen_uuid
    from sema4ai.action_server._models import Action, ActionPackage, get_db
    from sema4ai.action_server._settings import get_python_exe_from_env
    from sema4ai.action_server._whitelist import accept_action

    python = get_python_exe_from_env(env)

    if skip_lint:
        code = """
try:
    from sema4ai.actions import cli
except:
    from robocorp.actions import cli

cli.main(["list", "--skip-lint"])
"""
    else:
        code = """
try:
    from sema4ai.actions import cli
except:
    from robocorp.actions import cli

cli.main(["list"])
"""
    cmdline = [python, "-c", code]

    popen = subprocess.Popen(
        cmdline,
        env=env,
        cwd=str(import_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    stdout, stderr = popen.communicate()
    if popen.poll() != 0:
        if popen.poll() == 1:
            # Let's see if we have linting issues.
            try:
                loaded_from_stdout = json.loads(stdout)
            except Exception:
                pass  # Ok, just go with the "regular" error.
            else:
                # We loaded some json from the stdout. Check if there
                # were linting errors.
                if isinstance(loaded_from_stdout, dict):
                    lint_result = loaded_from_stdout.get("lint_result")
                    if isinstance(lint_result, dict):
                        formatted_lint_result = format_lint_results(lint_result)
                        if formatted_lint_result is not None:
                            raise ActionServerValidationError(
                                formatted_lint_result.message
                            )

        raise RuntimeError(
            f"It was not possible to list the actions.\n"
            f"cmdline: {subprocess.list2cmdline(cmdline)}\n"
            f"cwd: {import_path}\n"
            f"stdout:{stdout.decode('utf-8', 'replace')}\n"
            f"stderr:{stderr.decode('utf-8', 'replace')}"
        )

    # If it didn't fail the import, consider as warning (and thus print in yellow).
    decoded_stderr = stderr.decode("utf-8", "replace").strip()
    if decoded_stderr:
        log.critical(bold_yellow(f"{decoded_stderr}\n"))

    try:
        actions_list_result = json.loads(stdout)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"It was not possible to load as json the contents >>{stdout!r}<<"
        )
    else:
        if not isinstance(actions_list_result, list):
            raise RuntimeError(
                f"Expected sema4ai.actions list to provide a list. Found: >>{stdout!r}<<"
            )

        hook_on_actions_list(
            action_package,
            typing.cast(list["ActionsListActionTypedDict"], actions_list_result),
        )

        actions = []
        for action_fields in actions_list_result:
            action_name = action_fields["name"]
            if whitelist:
                if not accept_action(whitelist, action_package.name, action_name):
                    log.info(
                        f"Action: {action_package.name}/{action_name} not imported because it has no match in the whitelist: {whitelist!r}"
                    )
                    continue

            filepath = Path(action_fields["file"]).absolute()
            try:
                filepath = filepath.relative_to(import_path)
            except ValueError:
                pass

            managed_params_str: str = ""
            if action_fields.get("managed_params_schema"):
                managed_params_str = json.dumps(action_fields["managed_params_schema"])

            actions.append(
                Action(
                    id=gen_uuid("action"),
                    action_package_id=action_package.id,
                    name=action_name,
                    docs=action_fields["docs"],
                    file=filepath.as_posix(),
                    lineno=action_fields["line"],
                    input_schema=json.dumps(action_fields["input_schema"]),
                    output_schema=json.dumps(action_fields["output_schema"]),
                    enabled=True,
                    is_consequential=(action_fields.get("options") or {}).get(
                        "is_consequential", None
                    ),
                    managed_params_schema=managed_params_str,
                )
            )

    db = get_db()
    try:
        existing_action_package = db.first(
            ActionPackage,
            "SELECT * FROM action_package WHERE name = ?",
            [action_package.name],
        )
    except KeyError:
        # ok, insert as new one
        with db.transaction():
            log.info("Found new action package: %s", action_package.name)
            db.insert(action_package)
            for action in actions:
                log.info("Found new action: %s", action.name)
                db.insert(action)
    else:
        # We already have an existing action package with the same name. This
        # means we'll have to update it instead of adding a new one.
        new_action_package_as_dict = asdict(action_package)
        # The id should not be updated.
        del new_action_package_as_dict["id"]

        existing_actions = db.select(
            Action,
            "SELECT * FROM action WHERE action_package_id = ?",
            [existing_action_package.id],
        )

        existing_action_name_to_action = {}
        for action in existing_actions:
            existing_action_name_to_action[action.name] = action

        if disable_not_imported:
            all_actions = db.all(Action)

        seen_action_ids = set()
        with db.transaction():
            log.debug("Updating action package: %s", action_package.name)
            db.update_by_id(
                ActionPackage,
                existing_action_package.id,
                new_action_package_as_dict,
            )
            if not actions:
                log.info("Found no actions in: %s", action_package.name)

            for action in actions:
                action.action_package_id = existing_action_package.id
                existing_action = existing_action_name_to_action.get(action.name)
                if existing_action is not None:
                    # This is an existing action, we need to update it.
                    new_action_as_dict = asdict(action)
                    del new_action_as_dict["id"]
                    log.debug("Updating action: %s", action.name)
                    db.update_by_id(Action, existing_action.id, new_action_as_dict)
                    seen_action_ids.add(existing_action.id)
                else:
                    # This is a new action, insert it.
                    log.info("Found new action: %s", action.name)
                    db.insert(action)
                    seen_action_ids.add(action.id)

            if disable_not_imported:
                for action in all_actions:
                    if action.id not in seen_action_ids:
                        log.info("Disabling action: %s", action.name)
                        db.update_by_id(Action, action.id, dict(enabled=False))
