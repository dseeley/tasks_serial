# Copyright 2023 Dougal Seeley <github@dougalseeley.com>
# BSD 3-Clause License

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible.executor.task_executor import TaskExecutor
from ansible.plugins.action import ActionBase
from copy import deepcopy
from ansible.utils.display import Display
from ansible.plugins.loader import module_loader
from ansible.errors import AnsibleError
from ansible import constants as C

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

                in_path = module_loader.find_plugin(new_task.action)
                display.v(u"in_path: %s" % in_path)
                if in_path:
                    TaskExecutorArgs = {"host": self._play_context.remote_addr,
                                        "task": new_task,
                                        "job_vars": {},
                                        "play_context": self._play_context,
                                        "new_stdin": {},
                                        "loader": self._loader,
                                        "shared_loader_obj": self._shared_loader_obj,
                                        "final_q": None}

                    display.v(u"Ansible version: %s" % str(task_vars['ansible_version']))
                    ansible_version_tuple = (task_vars['ansible_version']['major'], task_vars['ansible_version']['minor'], task_vars['ansible_version']['revision'])
                    if ansible_version_tuple >= (2, 15, 0):
                        display.v(u"Ansible version >= 2.15.0 - adding required variable_manager parameter")
                        TaskExecutorArgs.update({"variable_manager": None})

                    # Create the task executor
                    executor = TaskExecutor(**TaskExecutorArgs)
                    display.v(u"executor: %s" % executor)

                    # Get the action handler for the task
                    action_handler = executor._get_action_handler(connection=self._connection, templar=self._templar)
                    display.v(u"action_handler: %s" % action_handler)

                    # Create a deep copy of task_vars for each task
                    plugin_task_vars = deepcopy(task_vars)

                    # Execute the task using the action handler
                    task_result = action_handler.run(task_vars=plugin_task_vars)
                    results.append(task_result)
                    display.v(u"action_handler.run - task_result: %s" % task_result)
                else:
                    results.append({'failed': True, 'msg': f"Task '{new_task.action}' not found."})

        result['failed'] = True in (('failed' in result and result['failed'] is True) for result in results)
        result['changed'] = True in (('changed' in result and result['changed'] is True) for result in results)
        result['_ansible_verbose_always'] = True
        result['results'] = results

        return result
