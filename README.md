# Ansible Collection - dseeley.tasks_serial

An Ansible action plugin to execute tasks serially on each host.  Useful, if, for example, you have an HA cluster, and you want to restart each node in turn, and then wait for it to be alive (e.g. by checking a port).  You can't do this by any other mechanism, except declaring the whole play as `serial`, which would slow the whole play down by a factor of the play size.  

To be used in conjunction with `throttle: 1`.  This is a deliberate requirement, so that it is possible to disable the functionality with `throttle: 0` (e.g. conditionally throttling if necessary).

Designed to overcome the limitation described in https://github.com/ansible/ansible/issues/80374

## Execution
A successful run:
```yaml
- name: Execute tasks on all hosts, in series
  dseeley.tasks_serial.tasks_serial:
    tasks:
      - name: ansible.builtin.debug
        args:
          msg: "Task 1 - debug!"
      - name: debug
        args:
          msg: "Task 2 - debug!"
      - name: ansible.builtin.shell
        args:
          cmd: echo "Task 3 - shell echo"
      - name: ansible.builtin.wait_for
        args:
          host: localhost
          port: 22
          timeout: 5
      - name: ansible.builtin.command
        args:
          cmd: "ls -l"
  throttle: 1
```

A failed run (tasks after the failed task are skipped)
```yaml
- name: Execute tasks on all hosts, in series
  dseeley.tasks_serial.tasks_serial:
    tasks:
      - name: ansible.builtin.debug
        args:
          msg: "Task 1 - debug!"
      - name: ansible.builtin.command
        args:
          cmd: "/bin/false"
      - name: ansible.builtin.debug
        args:
          msg: "Task 2 - debug!"
      - name: ansible.builtin.shell
        args:
          cmd: echo "Task 3 - shell echo"
      - name: command
        args:
          cmd: "ls -l"
  throttle: 1
```