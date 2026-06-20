# Ansible Collection - dseeley.tasks_serial

An Ansible action plugin to execute tasks serially on each host.  Useful, if, for example, you have an HA cluster, and you want to restart each node in turn, and then wait for it to be alive (e.g. by checking a port).  You can't do this by any other mechanism, except declaring the whole play as `serial`, which would slow the whole play down by a factor of the play size.

To be used in conjunction with `throttle: 1`.  This is a deliberate requirement, so that it is possible to disable the functionality with `throttle: 0` (e.g. conditionally throttling if necessary).

Designed to overcome the limitation described in https://github.com/ansible/ansible/issues/80374

Requires Ansible >= 12.0.0 (ansible-core >= 2.19).

## Usage

Nested tasks use conventional Ansible task syntax.  Task-level directives such as `loop`, `with_items`, `until`, `retries`, `register`, and `when` are supported on each nested task.

## Task directives

### On the `tasks_serial` task itself

Directives on the outer `dseeley.tasks_serial.tasks_serial` task are handled by Ansible in the normal way, before the action plugin's `run()` method is called.  The plugin does not reimplement them.

| Directive | Behaviour |
| --- | --- |
| `become`, `environment`, `connection`, `delegate_to` | Applied by Ansible to the outer task.  Nested tasks inherit these via the parent task unless a nested task overrides them. |
| `throttle` | Standard Ansible host throttling on the outer task. |
| `when` | Evaluated by Ansible before the plugin runs. |
| `loop` / `with_items` | Ansible loops the entire `tasks_serial` block — the plugin's `run()` is called once per outer iteration. |
| `until` / `retries` | Apply to the whole `tasks_serial` execution as a single task. |
| `register` | Registers the aggregated plugin result (the `results` list), not the result of individual nested tasks. |

In most cases, put `loop`, `until`, and `register` on the **nested** tasks (see below), not on the outer `tasks_serial` task.

### On nested tasks

Each entry in the `tasks` list is a normal Ansible task.  Directives such as `loop`, `with_items`, `until`, `retries`, `delay`, `register`, and `when` are executed by `TaskExecutor` for that nested task.

`register` results and facts from one nested task are visible to subsequent nested tasks within the same `tasks_serial` invocation on the same host.

### Loop variables and `{{ item }}`

The `tasks` argument is not templated when the outer task is parsed.  Expressions such as `{{ item }}` inside nested tasks are evaluated later, when each nested task runs.

| Scenario | What `{{ item }}` refers to |
| --- | --- |
| Loop only on a nested task | The nested task's loop item. |
| Loop only on the outer `tasks_serial` task | The outer loop item (for nested tasks that do not have their own loop). |
| Loop on both outer and nested tasks | The **nested** loop item while the nested loop is running. |

If both the outer `tasks_serial` task and a nested task use the default loop variable `item`, the inner loop overwrites `item` for the duration of that nested task.  After the nested loop finishes, `item` retains the last inner-loop value, so a subsequent nested task without its own loop will not see the outer `item`.

To use both values at once, give the outer loop a distinct name via `loop_control`:

```yaml
- name: Process each cluster node
  dseeley.tasks_serial.tasks_serial:
    tasks:
      - name: Act on inner item for this outer node
        ansible.builtin.debug:
          msg: "outer={{ outer_item }}, inner={{ item }}"
        loop: "{{ inner_items }}"
  loop: "{{ outer_items }}"
  loop_control:
    loop_var: outer_item
  throttle: 1
```

**Practical guidance:**

- Put `loop` / `with_items` on **nested tasks** when the serial sequence should repeat per item on each host (the usual HA cluster pattern).
- Put `loop` on the **outer** `tasks_serial` task only when the entire nested sequence should repeat per outer item.
- Avoid using `item` at both levels without renaming one of them.

## Execution

A successful run:

```yaml
- name: Execute tasks on all hosts, in series
  dseeley.tasks_serial.tasks_serial:
    tasks:
      - name: Task 1 - debug
        ansible.builtin.debug:
          msg: "Task 1 - debug!"
      - name: Task 2 - debug
        debug:
          msg: "Task 2 - debug!"
      - name: Task 3 - shell echo
        ansible.builtin.shell:
          cmd: echo "Task 3 - shell echo"
      - name: Task 4 - wait for port
        ansible.builtin.wait_for:
          host: localhost
          port: 22
          timeout: 5
      - name: Task 5 - list files
        ansible.builtin.command:
          cmd: "ls -l"
  throttle: 1
```

A failed run (tasks after the failed task are skipped):

```yaml
- name: Execute tasks on all hosts, in series
  dseeley.tasks_serial.tasks_serial:
    tasks:
      - name: Task 1 - debug
        ansible.builtin.debug:
          msg: "Task 1 - debug!"
      - name: Task 2 - command failure
        ansible.builtin.command:
          cmd: "/bin/false"
      - name: Task 3 - debug
        ansible.builtin.debug:
          msg: "Task 2 - debug!"
      - name: Task 4 - shell echo
        ansible.builtin.shell:
          cmd: echo "Task 3 - shell echo"
      - name: Task 5 - list files
        command:
          cmd: "ls -l"
  throttle: 1
```

### Loops, retries, and register

A typical HA cluster pattern — perform an action for each item, then wait until a condition is met before moving to the next host:

```yaml
- name: Add and wait for new masters serially
  dseeley.tasks_serial.tasks_serial:
    tasks:
      - name: Add new master nodes
        ansible.builtin.shell:
          cmd: >-
            {{ yb_home }}/bin/yb-admin --certs_dir={{ yb_certdir }}
            --master_addresses {{ master_addresses }}
            change_master_config ADD_SERVER {{ hostvars[item]['ansible_facts']['default_ipv4']['address'] }} 7100
        with_items: "{{ nodes_to_add }}"

      - name: Wait until new master is present
        ansible.builtin.uri:
          url: "http://localhost:7000/api/v1/masters"
          method: GET
          return_content: true
        register: r__uri__masters
        until: >
          r__uri__masters.status == 200 and
          r__uri__masters.content is defined and
          (r__uri__masters.content | from_json).masters is defined and
          ((r__uri__masters.content | from_json).masters | selectattr('error', 'defined') | list | length) == 0 and
          item in ((r__uri__masters.content | from_json).masters | json_query('[].registration.http_addresses[].host'))
        retries: 60
        delay: 1
        with_items: "{{ nodes_to_add }}"
  throttle: 1
  become: true
```

## Testing

A test playbook and Docker image are provided under `tests/`:

```bash
docker build -t tasks_serial-test .
docker run --rm tasks_serial-test
```

To verify against the minimum supported Ansible version:

```bash
docker build --build-arg ANSIBLE_VERSION=12.0.0 -t tasks_serial-test .
docker run --rm tasks_serial-test
```