EGTAOnline Api
==============

[![Build](https://img.shields.io/travis/egtaonline/egtaonline-api.svg?style=flat-square)](https://travis-ci.org/egtaonline/egtaonline-api)
[![Coverage](https://img.shields.io/coveralls/egtaonline/egtaonline-api.svg?style=flat-square)](https://coveralls.io/github/egtaonline/egtaonline-api)

Command line and python access to egtaonline.


Install
-------

```
pip install egtaonlineapi
```


Usage
-----

- The command line entry point is `eo`.
  `eo --help` will list all the options available.
- The python entry point is `egtaonline.api`.
  This has slightly more functionality than the command line api.
- There is also a mock server at `egtaonline.mockserver` that handles all requests without actually modifying egta.


Cookbook
--------

These are useful scripts that illustrate what can be done with the api.

- Monitor a scheduler and report when it's done:

  ```
  while ! eo sched <sched-id> -r | jq -e '.scheduling_requirements | map(.current_count >= .requirement) | all' > /dev/null; do sleep <sleep-interval>; done; <notify-script>
  ```

  This will poll `<sched-id>` every `<sleep-interval>` seconds and run `<notify-script>` when the scheduler is done.
