egtaonline-api
==============

Command line and python access to egtaonline


Cookbook
--------

These are useful scripts that illustrate what can be done with the api.

- Monitor a scheduler and report when it's done:

  ```
  while ! ./bin/eo sched <sched-id> -r | jq -e '.scheduling_requirements | map(.current_count >= .requirement) | all' > /dev/null; do sleep <sleep-interval>; done; <notify-script>
  ```

  This will poll `<sched-id>` every `<sleep-interval>` seconds and run `<notify-script>` when the scheduler is done.
