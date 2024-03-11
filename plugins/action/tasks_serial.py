# Copyright 2023 Dougal Seeley <github@dougalseeley.com>
# BSD 3-Clause License

from __future__ import (absolute_import, division, print_function)

__metaclass__ = type

from ansible.plugins.action import ActionBase
from copy import deepcopy
from ansible.utils.display import Display
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

                task_action = self._shared_loader_obj.action_loader.get(new_task.action,
                                                                        task=new_task,
                                                                        connection=self._connection,
                                                                        play_context=self._play_context,
                                                                        loader=self._loader,
                                                                        templar=self._templar,
                                                                        shared_loader_obj=self._shared_loader_obj)
                display.v(u"task_action: %s" % task_action)
                if not task_action:
                    results.append({'failed': True, 'msg': f"Action task '{new_task.action}' not found."})
                else:
                    plugin_task_vars = deepcopy(task_vars)  # Create a deep copy of task_vars for each task
                    task_result = task_action.run(task_vars=plugin_task_vars)
                    display.vv(u"task_result: %s" % task_result)
                    results.append(task_result)
                    display.vv(u"results: %s" % results)

        result['failed'] = True in (('failed' in result and result['failed'] is True) for result in results)
        result['changed'] = True in (('changed' in result and result['changed'] is True) for result in results)
        result['_ansible_verbose_always'] = True
        result['results'] = results

        return result
