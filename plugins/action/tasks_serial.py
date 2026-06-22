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
from ansible.plugins.connection import ConnectionBase
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
    # delegate_to/delegate_facts on the outer tasks_serial task apply only to that
    # invocation.  Nested tasks must declare their own delegation explicitly.
    _NESTED_DELEGATION_ATTRS = frozenset(('delegate_to', 'delegate_facts'))
    # connection inherited from the outer tasks_serial task (often local on localhost)
    # must not become TaskExecutor's fallback when nested delegate_to omits ansible_connection.
    _NESTED_DELEGATION_CLEAR_ATTRS = frozenset(('connection',))
    _INTERPRETER_FACT_PREFIXES = ('discovered_interpreter_', 'ansible_discovered_interpreter_')

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
        fact_sources = []

        tasks = self._task.args.get('tasks', [])
        if not isinstance(tasks, list):
            return {'failed': True, 'msg': 'The "tasks" parameter must be a list.'}

        # Shared across all nested tasks on this host so register/facts from an earlier nested
        # task are visible to later ones (mirrors behaviour within a normal play).
        plugin_task_vars = self._prepare_nested_task_vars(deepcopy(task_vars))
        inventory_hostname = to_text(
            plugin_task_vars.get('inventory_hostname', self._play_context.remote_addr)
        )
        host = Host(inventory_hostname)
        variable_manager = self._get_variable_manager(plugin_task_vars)

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

            # Drop interpreter facts before each nested run so delegate_to targets
            # (and later outer-loop iterations) rediscover locally.
            self._strip_interpreter_facts_from_task_vars(plugin_task_vars)

            try:
                task_utr = self._execute_nested_task(
                    nested_task=nested_task,
                    host=host,
                    task_vars=plugin_task_vars,
                    variable_manager=variable_manager,
                )
            except Exception as ex:
                results.append({'failed': True, 'msg': "Task '%s' failed: %s" % (nested_task.get_name(), ex)})
                continue

            task_result = self._task_result_as_dict(task_utr)
            self._annotate_delegation_result(nested_task, task_utr, task_result)

            # TaskExecutor updates an internal copy of variables; push results back into
            # plugin_task_vars so subsequent nested tasks can use register/ set_fact values.
            self._update_task_vars(
                nested_task=nested_task,
                task_result=task_result,
                task_utr=task_utr,
                task_vars=plugin_task_vars,
                inventory_hostname=inventory_hostname,
            )
            self._strip_interpreter_facts_from_task_vars(plugin_task_vars)
            self._apply_host_variables(
                nested_task=nested_task,
                task_result=task_result,
                task_utr=task_utr,
                variable_manager=variable_manager,
                inventory_hostname=inventory_hostname,
            )
            fact_sources.append((nested_task, task_utr, inventory_hostname))
            results.append(task_result)

        result['failed'] = self._results_failed(results)
        result['changed'] = any(r.get('changed') for r in results)
        result['_ansible_verbose_always'] = True
        result['results'] = results

        # Bubble nested set_fact results up to the plugin result so Ansible can
        # persist them on the host between outer-loop iterations of tasks_serial.
        ansible_facts = self._collect_ansible_facts(fact_sources)
        if ansible_facts:
            result['ansible_facts'] = ansible_facts

        return result

    def _execute_nested_task(self, nested_task, host, task_vars, variable_manager):
        # TaskExecutor mutates play_context.connection in place.  Use a per-nested-task
        # copy and normalize the connection name so remote hosts do not inherit an
        # unresolvable plugin path left behind by earlier executor runs.
        play_context = self._play_context.copy()
        has_delegate = self._nested_has_delegate_to(nested_task)
        if has_delegate:
            self._clear_nested_delegation_connection(nested_task, play_context)
        else:
            connection = self._resolve_nested_connection(task_vars, nested_task, play_context)
            if connection:
                play_context.connection = connection
                nested_task.connection = connection

        executor_task_vars = self._prepare_executor_task_vars(task_vars, has_delegate)

        if has_delegate and isinstance(variable_manager, _MinimalVariableManager):
            raise AnsibleError(
                'tasks_serial cannot resolve delegate_to without a VariableManager. '
                'Ensure delegated hosts exist in inventory (e.g. add_host) before calling tasks_serial.'
            )

        executor_kwargs = {
            'host': host,
            'play_context': play_context,
            'loader': self._loader,
            'shared_loader_obj': self._shared_loader_obj,
            'final_q': _NoopFinalQueue(),
            'variable_manager': variable_manager,
        }

        # Reuse the connection that already works for this tasks_serial invocation.
        parent_connection = getattr(self, '_connection', None)
        parent_play_context = getattr(parent_connection, '_play_context', None)
        nested_delegate_to = getattr(nested_task, 'delegate_to', None)
        reuse_connection = (
            not nested_delegate_to or nested_delegate_to is Sentinel
        ) and (
            isinstance(parent_connection, ConnectionBase)
            and getattr(parent_connection, 'connected', False)
            and parent_play_context is not None
            and play_context.remote_addr == parent_play_context.remote_addr
        )

        if _TASK_EXECUTOR_USES_TASK_CONTEXT:
            # Ansible 14+: task and variables live in TaskContext for the duration of run().
            from ansible._internal._task import TaskContext

            executor = TaskExecutor(**executor_kwargs)
            if reuse_connection:
                executor._connection = parent_connection

            with TaskContext.create(
                task=nested_task,
                task_vars=executor_task_vars,
                host_name=host.get_name(),
            ):
                return executor.run()

        # Ansible 12–13: pass task and job_vars directly to TaskExecutor.
        executor = TaskExecutor(task=nested_task, job_vars=executor_task_vars, **executor_kwargs)
        if reuse_connection:
            executor._connection = parent_connection
        return executor.run()

    @staticmethod
    def _task_result_as_dict(task_utr):
        if hasattr(task_utr, 'as_result_dict'):
            return task_utr.as_result_dict()
        return task_utr

    def _update_task_vars(self, nested_task, task_result, task_utr, task_vars, inventory_hostname):
        register = nested_task.register
        if register:
            # register is a plain string on Ansible 12–13 and a dict on Ansible 14+.
            if isinstance(register, dict):
                for var_name in register:
                    task_vars[var_name] = task_result
            else:
                task_vars[register] = task_result

        for utr in self._iter_task_utrs(task_utr):
            if self._facts_target_host(nested_task, utr, inventory_hostname) != inventory_hostname:
                continue

            facts = self._sanitize_interpreter_facts(
                self._extract_ansible_facts(self._task_result_as_dict(utr))
            )
            if facts:
                task_vars.update(facts)
                task_vars.setdefault('ansible_facts', {}).update(facts)

    def _get_variable_manager(self, task_vars):
        candidates = []

        hostvars = task_vars.get('hostvars')
        if hostvars is not None:
            candidates.append(hostvars)

        try:
            candidates.append(self._task.get_play())
        except (AttributeError, AnsibleError):
            pass

        for candidate in candidates:
            variable_manager = getattr(candidate, '_variable_manager', None)
            if variable_manager is not None:
                return variable_manager

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

            if attr_name in ActionModule._NESTED_DELEGATION_ATTRS:
                # Do not inherit delegation from the outer tasks_serial task.
                setattr(nested_task, attr_name, None if value is Sentinel else value)
                continue

            if value is Sentinel:
                continue
            # Non-inheritable fields (action, loop, register, ...) always come from the
            # nested task.  Inheritable fields (when, become, tags, ...) are copied only
            # when explicitly present in the YAML so the parent tasks_serial task can
            # still supply defaults such as become: true.
            if isinstance(field_attr, NonInheritableFieldAttribute) or attr_name in task_data:
                setattr(nested_task, attr_name, value)

        if 'delegate_to' in task_data:
            for attr_name in ActionModule._NESTED_DELEGATION_CLEAR_ATTRS:
                setattr(nested_task, attr_name, Sentinel)

        nested_task.name = parsed_task.name
        nested_task._resolved_action = parsed_task._resolved_action
        return nested_task

    @staticmethod
    def _clear_nested_delegation_connection(nested_task, play_context):
        """Stop TaskExecutor falling back to the inventory host's local connection."""
        nested_task.connection = Sentinel
        play_context.connection = Sentinel

    @staticmethod
    def _annotate_delegation_result(nested_task, task_utr, task_result):
        """Expose which host ran the nested task (useful when play host is localhost)."""
        delegated_host = None
        for utr in ActionModule._iter_task_utrs(task_utr):
            delegated_host = getattr(utr, 'delegated_host', None) or delegated_host

        if delegated_host is None and ActionModule._nested_has_delegate_to(nested_task):
            delegate_to = getattr(nested_task, 'delegate_to', None)
            if delegate_to and delegate_to is not Sentinel:
                delegated_host = delegate_to

        if delegated_host:
            task_result['_ansible_delegated_host'] = to_text(delegated_host)

    def _prepare_executor_task_vars(self, task_vars, has_delegate):
        """Build per-execution task vars that cannot leak host-specific interpreters."""
        if not has_delegate:
            return task_vars

        # Shallow-copy and isolate only the nested dicts TaskExecutor mutates.
        # A full deepcopy breaks register/until evaluation (Ansible 14 register
        # projections and hostvars templating).  Isolating ansible_facts is enough
        # to stop interpreter discoveries bleeding across delegate_to targets.
        executor_task_vars = dict(task_vars)
        executor_task_vars.pop('ansible_connection', None)
        executor_task_vars.pop('ansible_delegated_vars', None)

        ansible_facts = executor_task_vars.get('ansible_facts')
        if isinstance(ansible_facts, dict):
            executor_task_vars['ansible_facts'] = deepcopy(ansible_facts)

        self._strip_interpreter_facts_from_task_vars(executor_task_vars)
        return executor_task_vars

    def _prepare_nested_task_vars(self, task_vars):
        # Ansible 12–14 rewrite ansible_connection between outer-loop iterations,
        # sometimes to an internal plugin path that connection_loader cannot resolve.
        connection = self._normalize_connection_name(task_vars.get('ansible_connection'))
        if connection is None:
            connection = self._normalize_connection_name(self._play_context.connection)
        if connection:
            task_vars['ansible_connection'] = connection

        # Outer-loop iterations bubble delegated interpreter facts onto the
        # inventory host; drop them so each delegate_to target rediscovers locally.
        self._strip_interpreter_facts_from_task_vars(task_vars)
        return task_vars

    @staticmethod
    def _normalize_connection_name(connection):
        if connection is None or connection is Sentinel:
            return None

        connection = to_text(connection)

        # TaskExecutor can record fully-qualified plugin paths on the task after
        # the first nested run; reduce those back to a loader-friendly name.
        if 'plugins.connection.' in connection:
            return connection.rsplit('.', 1)[-1]
        if connection.startswith('ansible.builtin.'):
            return connection.split('.', 2)[-1]
        if connection.startswith('ansible.legacy.'):
            return connection.removeprefix('ansible.legacy.')

        return connection

    @staticmethod
    def _resolve_nested_connection(task_vars, nested_task, play_context):
        """Return a short connection plugin name that connection_loader can resolve."""
        candidates = (
            task_vars.get('ansible_connection'),
            getattr(nested_task, 'connection', None),
            getattr(play_context, 'connection', None),
        )

        for candidate in candidates:
            connection = ActionModule._normalize_connection_name(candidate)
            if connection:
                return connection

        return None

    @staticmethod
    def _extract_ansible_facts(task_result):
        ansible_facts = {}
        facts = task_result.get('ansible_facts')
        if facts:
            ansible_facts.update(facts)

        for loop_result in task_result.get('results', []):
            facts = loop_result.get('ansible_facts')
            if facts:
                ansible_facts.update(facts)

        return ansible_facts

    def _apply_host_variables(self, nested_task, task_result, task_utr, variable_manager, inventory_hostname):
        """Persist nested facts on the host that owns them (strategy does this for normal tasks)."""
        for utr in self._iter_task_utrs(task_utr):
            target_host = self._facts_target_host(nested_task, utr, inventory_hostname)
            if target_host == inventory_hostname:
                continue

            if hasattr(utr, 'pending_changes') and utr.pending_changes.register_host_variables:
                self._apply_pending_changes(utr, target_host, variable_manager)
            else:
                self._apply_host_facts_fallback(target_host, self._task_result_as_dict(utr), variable_manager)

    @staticmethod
    def _iter_task_utrs(task_utr):
        if hasattr(task_utr, 'loop_results') and task_utr.loop_results:
            return task_utr.loop_results
        return [task_utr]

    @staticmethod
    def _apply_host_facts_fallback(target_host, task_result, variable_manager):
        set_nonpersistent_facts = getattr(variable_manager, 'set_nonpersistent_facts', None)
        if not callable(set_nonpersistent_facts):
            return

        ansible_facts = ActionModule._extract_ansible_facts(task_result)
        if ansible_facts:
            set_nonpersistent_facts(to_text(target_host), ansible_facts)

    @staticmethod
    def _apply_pending_changes(task_utr, target_host, variable_manager):
        pending_changes = getattr(task_utr, 'pending_changes', None)
        if not pending_changes or not pending_changes.register_host_variables:
            return

        from ansible.plugins.action import VariableLayer

        target_host = to_text(target_host)

        for variable_layer, variables in sorted(pending_changes.register_host_variables.items()):
            if variable_layer == VariableLayer.REGISTER_VARS or not variables:
                continue

            variables = dict(variables)
            ActionModule._strip_interpreter_facts_from_mapping(variables)

            if variable_layer == VariableLayer.CACHEABLE_FACT:
                set_host_facts = getattr(variable_manager, 'set_host_facts', None)
                if callable(set_host_facts):
                    set_host_facts(target_host, variables)
            elif variable_layer == VariableLayer.EPHEMERAL_FACT:
                set_nonpersistent_facts = getattr(variable_manager, 'set_nonpersistent_facts', None)
                if callable(set_nonpersistent_facts):
                    set_nonpersistent_facts(target_host, variables)
            elif variable_layer == VariableLayer.INCLUDE_VARS:
                set_host_variable = getattr(variable_manager, 'set_host_variable', None)
                if callable(set_host_variable):
                    for var_name, var_value in variables.items():
                        set_host_variable(target_host, var_name, var_value)

    @staticmethod
    def _delegate_facts_enabled(nested_task):
        delegate_facts = getattr(nested_task, 'delegate_facts', False)
        if delegate_facts is Sentinel:
            return False
        return bool(delegate_facts)

    @classmethod
    def _facts_target_host(cls, nested_task, task_utr, inventory_hostname):
        """Return the inventory hostname that should receive nested task facts."""
        delegated_host = getattr(task_utr, 'delegated_host', None)
        if delegated_host is None:
            delegate_to = getattr(nested_task, 'delegate_to', None)
            if delegate_to and delegate_to is not Sentinel:
                delegated_host = delegate_to

        if delegated_host and cls._delegate_facts_enabled(nested_task):
            return to_text(delegated_host)

        return inventory_hostname

    @classmethod
    def _collect_ansible_facts(cls, fact_sources):
        """Merge ansible_facts from nested tasks (including loop sub-results)."""
        ansible_facts = {}

        for nested_task, task_utr, inventory_hostname in fact_sources:
            for utr in cls._iter_task_utrs(task_utr):
                if cls._facts_target_host(nested_task, utr, inventory_hostname) != inventory_hostname:
                    continue

                ansible_facts.update(cls._sanitize_interpreter_facts(
                    cls._extract_ansible_facts(cls._task_result_as_dict(utr))
                ))

        return ansible_facts

    @classmethod
    def _is_interpreter_fact_key(cls, key):
        return (
            key == 'ansible_python_interpreter'
            or key.startswith(cls._INTERPRETER_FACT_PREFIXES)
        )

    @classmethod
    def _sanitize_interpreter_facts(cls, facts):
        if not facts:
            return facts
        return {key: value for key, value in facts.items() if not cls._is_interpreter_fact_key(key)}

    @classmethod
    def _strip_interpreter_facts_from_task_vars(cls, task_vars):
        for key in list(task_vars):
            if cls._is_interpreter_fact_key(key):
                del task_vars[key]

        ansible_facts = task_vars.get('ansible_facts')
        if isinstance(ansible_facts, dict):
            for key in list(ansible_facts):
                if cls._is_interpreter_fact_key(key):
                    del ansible_facts[key]

        delegated_vars = task_vars.get('ansible_delegated_vars')
        if isinstance(delegated_vars, dict):
            for delegated_host_vars in delegated_vars.values():
                if isinstance(delegated_host_vars, dict):
                    cls._strip_interpreter_facts_from_mapping(delegated_host_vars)

    @classmethod
    def _strip_interpreter_facts_from_mapping(cls, variables):
        for key in list(variables):
            if cls._is_interpreter_fact_key(key):
                del variables[key]

        ansible_facts = variables.get('ansible_facts')
        if isinstance(ansible_facts, dict):
            for key in list(ansible_facts):
                if cls._is_interpreter_fact_key(key):
                    del ansible_facts[key]

    @staticmethod
    def _nested_has_delegate_to(nested_task):
        delegate_to = getattr(nested_task, 'delegate_to', None)
        return bool(delegate_to and delegate_to is not Sentinel)

    @staticmethod
    def _task_ran_delegated(nested_task, task_utr):
        for utr in ActionModule._iter_task_utrs(task_utr):
            if getattr(utr, 'delegated_host', None):
                return True

        delegate_to = getattr(nested_task, 'delegate_to', None)
        return bool(delegate_to and delegate_to is not Sentinel)

    @staticmethod
    def _results_failed(results):
        return any(r.get('failed') for r in results)

    @staticmethod
    def _task_label(task_data):
        if isinstance(task_data, dict) and task_data.get('name'):
            return "'%s'" % task_data['name']
        return 'task'