# Copyright (C) 2018 Leiden University Medical Center
# This file is part of pytest-workflow
#
# pytest-workflow is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# pytest-workflow is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with pytest-workflow.  If not, see <https://www.gnu.org/licenses/

"""core functionality of pytest-workflow plugin"""
import functools
import shutil
from pathlib import Path

import _pytest

import pytest

import yaml

from . import replace_whitespace
from .content_tests import ContentTestCollector
from .file_tests import FileTestCollector
from .schema import WorkflowTest, workflow_tests_from_schema
from .workflow import Workflow


def pytest_addoption(parser: _pytest.config.argparsing.Parser):
    parser.addoption(
        "--keep-workflow-wd",
        action="store_true",
        help="Keep temporary directories where workflows are run for "
             "debugging purposes. This also triggers saving of stdout and "
             "stderr in the workflow directory",
        dest="keep_workflow_wd"
    )


def pytest_collect_file(path, parent):
    """Collection hook
    This collects the yaml files that start with "test" and end with
    .yaml or .yml"""
    if path.ext in [".yml", ".yaml"] and path.basename.startswith("test"):
        return YamlFile(path, parent)
    return None


class YamlFile(pytest.File):
    """
    This class collects YAML files and turns them into test items.
    """

    def __init__(self, path: str, parent: pytest.Collector):
        # This super statement is important for pytest reasons. It should
        # be in any collector!
        super().__init__(path, parent=parent)

    def collect(self):
        """This function collects all the workflow tests from a single
        YAML file."""
        with self.fspath.open() as yaml_file:
            schema = yaml.safe_load(yaml_file)

        return [WorkflowTestsCollector(test, self)
                for test in workflow_tests_from_schema(schema)]


class WorkflowTestsCollector(pytest.Collector):
    """This class starts all the tests collectors per workflow"""

    def __init__(self, workflow_test: WorkflowTest, parent: pytest.Collector):
        self.workflow_test = workflow_test
        super().__init__(workflow_test.name, parent=parent)
        self.terminal_reporter = self.config.pluginmanager.get_plugin(
            "terminalreporter")

    def run_workflow(self):
        """Runs the workflow in a temporary directory

        Running in a temporary directory will prevent the project repository
        from getting filled up with test workflow output.
        The temporary directory is produced from
        self.config._tmp_path_factory.getbasetemp()
        On linux this takes the form: /tmp/pytest-of-$USER/pytest-<number>
        The number is generated by pytest itself and increments each run.

        The temporary directory name is constructed from the test name by
        replacing all whitespaces with '_'. Directory paths with whitespace in
        them are very annoying to inspect.
        Tests should not have colliding names. This will lead to
        WorkflowTestCollectors with the same internal names into pytest. This
        causes pytest to crash during collection. Hence no action was taken
        to prevent name collision in temporary paths. This is handled in the
        schema instead.

        Print statements are used to provide information to the user.  Using
        pytests internal logwriter has no added value. If there are wishes to
        do so in the future, the pytest terminal writer can be acquired with:
        self.config.pluginmanager.get_plugin("terminalreporter")
        Test name is included explicitly in each print command to avoid
        confusion between workflows
        """
        # pylint: disable=protected-access
        # Protected access needed to get the basetemp value.

        basetemp = Path(str(self.config._tmp_path_factory.getbasetemp()))
        tempdir = basetemp / Path(replace_whitespace(self.name, '_'))

        # Copy the project directory to the temporary directory using pytest's
        # rootdir.
        shutil.copytree(str(self.config.rootdir), str(tempdir))
        # Create a workflow and make sure it runs in the tempdir
        workflow = Workflow(self.workflow_test.command, tempdir)

        print("run '{name}' with command '{command}' in '{dir}'".format(
            name=self.name,
            command=self.workflow_test.command,
            dir=str(tempdir)))
        workflow.run()

        if self.config.getoption("keep_workflow_wd"):
            def write_logs():
                log_err = workflow.stderr_to_file()
                log_out = workflow.stdout_to_file()
                # Print statements do not work here.
                self.terminal_reporter.write_line(
                    "'{0}' stdout saved in: {1}".format(
                        self.name, str(log_out)))
                self.terminal_reporter.write_line(
                    "'{0}' stderr saved in: {1}".format(
                        self.name, str(log_err)))

            self.addfinalizer(write_logs)
        else:
            # addfinalizer adds a function that is run when the node tests are
            # completed
            rm_tempdir = functools.partial(shutil.rmtree, str(tempdir))
            self.addfinalizer(rm_tempdir)

        return workflow

    def collect(self):
        """This runs the workflow and starts all the associated tests
        The idea is that isolated parts of the yaml get their own collector or
        item."""

        workflow = self.run_workflow()

        # Below structure makes it easy to append tests
        tests = []

        tests += [FileTestCollector(self, filetest, workflow) for filetest
                  in self.workflow_test.files]

        tests += [ExitCodeTest(parent=self,
                               desired_exit_code=self.workflow_test.exit_code,
                               workflow=workflow)]

        tests += [ContentTestCollector(
            name="stdout", parent=self,
            content_generator=workflow.stdout_lines,
            content_test=self.workflow_test.stdout,
            workflow=workflow)]

        tests += [ContentTestCollector(
            name="stderr", parent=self,
            content_generator=workflow.stderr_lines,
            content_test=self.workflow_test.stderr,
            workflow=workflow)]

        return tests


class ExitCodeTest(pytest.Item):
    def __init__(self, parent: pytest.Collector,
                 desired_exit_code: int,
                 workflow: Workflow):
        name = "exit code should be {0}".format(desired_exit_code)
        super().__init__(name, parent=parent)
        self.workflow = workflow
        self.desired_exit_code = desired_exit_code

    def runtest(self):
        # workflow.exit_code waits for workflow to finish.
        assert self.workflow.exit_code == self.desired_exit_code

    def repr_failure(self, excinfo):
        # pylint: disable=unused-argument
        # excinfo needed for pytest.
        message = ("The workflow exited with exit code " +
                   "'{0}' instead of '{1}'.".format(self.workflow.exit_code,
                                                    self.desired_exit_code))
        return message
