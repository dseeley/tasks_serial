# Copyright 2023 Dougal Seeley <github@dougalseeley.com>
# BSD 3-Clause License

from __future__ import (absolute_import, division, print_function)

__metaclass__ = type

import inspect
import typing as t

from copy import deepcopy

from ansible.errors import AnsibleError, AnsibleParserError
from ansible.executor.task_executor import TaskExecutor
from ansible.inventory.host import Host
from ansible.module_utils.common.sentinel import Sentinel
from ansible.module_utils.common.text.converters import to_text
from ansible.playbook.attribute import NonInheritableFieldAttribute
from ansible.playbook.task import Task
from ansible.plugins.action import ActionBase
from ansible._internal._templating._engine import TemplateEngine
from ansible.utils.display import Display
from ansible.utils.vars import get_unique_id

display = Display()

# Ansible 14 (core 2.21) replaced TaskExecutor(host, task, job_vars, ...) with an ambient
# TaskContext model.  Detect at import time so we can call the appropriate API for 12–13 vs 14+.
_TASK_EXECUTOR_USES_TASK_CONTEXT = 'task' not in inspect.signature(TaskExecutor.__init__).parameters


class _NoopFinalQueue:
    """TaskExecutor publishes callback events to final_q; we have no strategy listener here."""

    def send_callback(self, method_name, *args, **kwargs):
        pass


class _MinimalVariableManager:
    """Fallback when play._variable_manager is unavailable (delegate_to needs the real one)."""

    def get_delegated_vars_and_hostname(self, templar, task, variables):
        return {}, None


class ActionModule(ActionBase):
    _VALID_ARGS = frozenset(('tasks',))

    @classmethod
    def finalize_task_arg(cls, name: str, value: t.Any, templar: TemplateEngine, context: t.Any) -> t.Any:
        # Ansible templates action args before run() is called.  Returning the raw `tasks` value
        # here prevents premature evaluation of loop variables (e.g. `item`) and until/register
        # expressions that only make sense once TaskExecutor runs each nested task.
        if name == 'tasks':
            return value
        return super(ActionModule, cls).finalize_task_arg(name, value, templar, context)

    def run(self, tmp=None, task_vars=None):
        if task_vars is None:
            task_vars = {}

        result = super(ActionModule, self).run(tmp, task_vars)
        del tmp  # tmp no longer has any effect

        results = []

        tasks = self._task.args.get('tasks', [])
        if not isinstance(tasks, list):
            return {'failed': True, 'msg': 'The "tasks" parameter must be a list.'}

        # Shared across all nested tasks on this host so register/facts from an earlier nested
        # task are visible to later ones (mirrors behaviour within a normal play).
        plugin_task_vars = deepcopy(task_vars)
        host = Host(plugin_task_vars.get('inventory_hostname', self._play_context.remote_addr))
        variable_manager = self._get_variable_manager()

        for task_data in tasks:
            if self._results_failed(results):
                results.append({
                    'skipped': True,
                    'msg': "%s skipped due to earlier failures." % self._task_label(task_data),
                })
                continue

            try:
                nested_task = self._build_nested_task(task_data)
            except (AnsibleError, AnsibleParserError) as ex:
                results.append({'failed': True, 'msg': to_text(ex)})
                continue

            display.v("tasks_serial nested task: %s" % nested_task.get_name())

            try:
                task_result = self._execute_nested_task(
                    nested_task=nested_task,
                    host=host,
                    task_vars=plugin_task_vars,
                    variable_manager=variable_manager,
                )
            except Exception as ex:
                results.append({'failed': True, 'msg': "Task '%s' failed: %s" % (nested_task.get_name(), ex)})
                continue

            # TaskExecutor updates an internal copy of variables; push results back into
            # plugin_task_vars so subsequent nested tasks can use register/ set_fact values.
            self._apply_register_vars(nested_task, task_result, plugin_task_vars)
            self._apply_fact_vars(task_result, plugin_task_vars)
            results.append(task_result)

        result['failed'] = self._results_failed(results)
        result['changed'] = any(r.get('changed') for r in results)
        result['_ansible_verbose_always'] = True
        result['results'] = results

        return result

    def _execute_nested_task(self, nested_task, host, task_vars, variable_manager):
        executor_kwargs = {
            'host': host,
            'play_context': self._play_context,
            'loader': self._loader,
            'shared_loader_obj': self._shared_loader_obj,
            'final_q': _NoopFinalQueue(),
            'variable_manager': variable_manager,
        }

        if _TASK_EXECUTOR_USES_TASK_CONTEXT:
            # Ansible 14+: task and variables live in TaskContext for the duration of run().
            from ansible._internal._task import TaskContext

            with TaskContext.create(
                task=nested_task,
                task_vars=task_vars,
                host_name=host.get_name(),
            ):
                return TaskExecutor(**executor_kwargs).run().as_result_dict()

        # Ansible 12–13: pass task and job_vars directly to TaskExecutor.
        return TaskExecutor(task=nested_task, job_vars=task_vars, **executor_kwargs).run()

    @staticmethod
    def _apply_register_vars(nested_task, task_result, task_vars):
        register = nested_task.register
        if not register:
            return

        # register is a plain string on Ansible 12–13 and a dict on Ansible 14+.
        if isinstance(register, dict):
            for var_name in register:
                task_vars[var_name] = task_result
        else:
            task_vars[register] = task_result

    @staticmethod
    def _apply_fact_vars(task_result, task_vars):
        ansible_facts = task_result.get('ansible_facts')
        if not ansible_facts:
            return

        task_vars.update(ansible_facts)
        if 'ansible_facts' in task_vars:
            task_vars['ansible_facts'].update(ansible_facts)
        else:
            task_vars['ansible_facts'] = ansible_facts

    def _get_variable_manager(self):
        try:
            play = self._task.get_play()
            variable_manager = getattr(play, '_variable_manager', None)
            if variable_manager is not None:
                return variable_manager
        except (AttributeError, AnsibleError):
            pass
        return _MinimalVariableManager()

    def _build_nested_task(self, task_data):
        if not isinstance(task_data, dict):
            raise AnsibleError('Each nested task must be a dictionary.')

        parsed_task = Task.load(task_data, loader=self._loader)
        return self._apply_parsed_task(self._task, parsed_task, task_data)

    @staticmethod
    def _apply_parsed_task(parent_task, parsed_task, task_data):
        # Start from the parent tasks_serial task so nested tasks inherit play-level
        # settings (become, environment, connection, etc.), then overlay the parsed
        # nested task's own action, args, and directives (loop, until, register, ...).
        nested_task = parent_task.copy()
        nested_task._uuid = get_unique_id()

        for attr_name, field_attr in parsed_task.fattributes.items():
            value = getattr(parsed_task, attr_name)
            if value is Sentinel:
                continue
            # Non-inheritable fields (action, loop, register, ...) always come from the
            # nested task.  Inheritable fields (when, become, tags, ...) are copied only
            # when explicitly present in the YAML so the parent tasks_serial task can
            # still supply defaults such as become: true.
            if isinstance(field_attr, NonInheritableFieldAttribute) or attr_name in task_data:
                setattr(nested_task, attr_name, value)

        nested_task.name = parsed_task.name
        nested_task._resolved_action = parsed_task._resolved_action
        return nested_task

    @staticmethod
    def _results_failed(results):
        return any(r.get('failed') for r in results)

    @staticmethod
    def _task_label(task_data):
        if isinstance(task_data, dict) and task_data.get('name'):
            return "'%s'" % task_data['name']
        return 'task'