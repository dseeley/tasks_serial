# Ansible Collection - dseeley.tasks_serial

An Ansible action plugin to execute tasks serially on each host.

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
      - name: ansible.builtin.debug
        args:
          msg: "Task 2 - debug!"
      - name: ansible.builtin.shell
        args:
          cmd: echo "Task 3 - shell echo"
      - name: command
        args:
          cmd: "ls -l"
```

A failed run (tasks after the failed task are skipped)
```yaml
- name: Execute tasks on all hosts, in series
  dseeley.tasks_serial.tasks_serial:
    tasks:
      - name: ansible.builtin.debug
        args:
          msg: "Task 1 - debug!"
      - name: command
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
```