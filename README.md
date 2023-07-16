# Ansible Collection - dseeley.task_serial

An Ansible action plugin to execute tasks serially

## Execution
A successful run:
```yaml
- name: Execute Series of Plugins
  serial_tasks:
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

A failed run (tasks after the failure do not run)
```yaml
- name: Execute Series of Plugins
  serial_tasks:
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