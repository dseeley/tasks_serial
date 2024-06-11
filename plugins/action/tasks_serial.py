# Copyright 2023 Dougal Seeley <github@dougalseeley.com>
# BSD 3-Clause License

from __future__ import (absolute_import, division, print_function)

__metaclass__ = type

from ansible.plugins.action import ActionBase
from ansible.utils.display import Display
from ansible.errors import AnsibleError
from ansible import constants as C
from copy import deepcopy
import traceback

display = Display()


class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        if task_vars is None:
            task_vars = {}

        result = super(ActionModule, self).run(tmp, task_vars)
        del tmp  # tmp no longer has any effect

        # Initialize the list to store results for each action
        results = []

        tasks = self._task.args.get('tasks', [])
        if not isinstance(tasks, list):
            return {'failed': True, 'msg': 'The "tasks" parameter must be a list.'}

        # Execute each task serially
        for task in tasks:
            if True in (('failed' in result and result['failed'] is True) for result in results):
                results.append({'skipped': True, 'msg': f"'" + task.get('name') + f"'" + f" skipped due to earlier failures."})
            else:
                new_task = self._task.copy()
                new_task.action = task.get('name')
                new_task.args = task.get('args', {})

                # From /site-packages/ansible/playbook/task.py, preprocess_data()
                if new_task.action in C._ACTION_HAS_CMD:
                    if 'cmd' in new_task.args:
                        if new_task.args.get('_raw_params', '') != '':
                            raise AnsibleError("The 'cmd' argument cannot be used when other raw parameters are specified."
                                               " Please put everything in one or the other place.")
                        new_task.args['_raw_params'] = new_task.args.pop('cmd')

                task_action = self._shared_loader_obj.action_loader.get(new_task.action,
                                                                        task=new_task,
                                                                        connection=self._connection,
                                                                        play_context=self._play_context,
                                                                        loader=self._loader,
                                                                        templar=self._templar,
                                                                        shared_loader_obj=self._shared_loader_obj)
                display.v(f"task_action ({new_task.action}): {task_action}")

                plugin_task_vars = deepcopy(task_vars)  # Create a deep copy of task_vars for each task
                if not task_action:
                    # Try running as a module instead of an action
                    try:
                        _execute_module_result = self._execute_module(module_name=new_task.action, module_args=new_task.args, task_vars=plugin_task_vars)
                        display.v(f"self._execute_module ({new_task.action}): {_execute_module_result}")
                    except Exception:
                        display.error(f"self._execute_module ({new_task.action}): {traceback.format_exc()}")
                    if _execute_module_result:
                        results.append(_execute_module_result)
                    else:
                        results.append({'failed': True, 'msg': f"'{new_task.action}' not found as an Action or Module"})
                else:
                    task_action_result = task_action.run(task_vars=plugin_task_vars)
                    display.vv(u"task_action_result: %s" % task_action_result)
                    results.append(task_action_result)
                    display.vv(u"results: %s" % results)

        result['failed'] = True in (('failed' in result and result['failed'] is True) for result in results)
        result['changed'] = True in (('changed' in result and result['changed'] is True) for result in results)
        result['_ansible_verbose_always'] = True
        result['results'] = results

        return result
